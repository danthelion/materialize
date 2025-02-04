# Copyright Materialize, Inc. and contributors. All rights reserved.
#
# Use of this software is governed by the Business Source License
# included in the LICENSE file at the root of this repository.
#
# As of the Change Date specified in that file, in accordance with
# the Business Source License, use of this software will be governed
# by the Apache License, Version 2.0.
#
# ci_annotate_errors.py - Detect errors in junit xml as well as log files
# during CI and find associated open GitHub issues in Materialize repository.

import argparse
import mmap
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import chain
from typing import Any

from junitparser.junitparser import Error, Failure, JUnitXml

from materialize import ci_util, ui
from materialize.buildkite import add_annotation_raw, get_artifact_url
from materialize.buildkite_insights.buildkite_api import builds_api, generic_api
from materialize.buildkite_insights.buildkite_api.buildkite_constants import (
    BUILDKITE_RELEVANT_COMPLETED_BUILD_STEP_STATES,
)
from materialize.buildkite_insights.steps.build_step import (
    BuildStepMatcher,
    BuildStepOutcome,
    extract_build_step_outcomes,
)
from materialize.github import (
    for_github_re,
    get_known_issues_from_github,
)
from materialize.observed_error import ObservedBaseError, WithIssue
from materialize.test_analytics.config.test_analytics_db_config import (
    create_test_analytics_config_with_hostname,
)
from materialize.test_analytics.data.build_annotation import build_annotation_storage
from materialize.test_analytics.data.build_annotation.build_annotation_storage import (
    AnnotationErrorEntry,
)
from materialize.test_analytics.test_analytics_db import TestAnalyticsDb

# Unexpected failures, report them
ERROR_RE = re.compile(
    rb"""
    ^ .*
    ( segfault\ at
    | trap\ invalid\ opcode
    | general\ protection
    | has\ overflowed\ its\ stack
    | internal\ error:
    | \*\ FATAL:
    | (^|\ )fatal: # used in frontegg-mock
    | [Oo]ut\ [Oo]f\ [Mm]emory
    | cannot\ migrate\ from\ catalog
    | halting\ process: # Rust unwrap
    | fatal\ runtime\ error: # stack overflow
    | \[SQLsmith\] # Unknown errors are logged
    | \[SQLancer\] # Unknown errors are logged
    | environmentd:\ fatal: # startup failure
    | clusterd:\ fatal: # startup failure
    | error:\ Found\ argument\ '.*'\ which\ wasn't\ expected,\ or\ isn't\ valid\ in\ this\ context
    | environmentd\ .*\ unrecognized\ configuration\ parameter
    | cannot\ load\ unknown\ system\ parameter\ from\ catalog\ storage
    | SUMMARY:\ .*Sanitizer
    | ----------\ RESULT\ COMPARISON\ ISSUE\ START\ ----------.*----------\ RESULT\ COMPARISON\ ISSUE\ END\ ------------
    # for miri test summary
    | (FAIL|TIMEOUT)\s+\[\s*\d+\.\d+s\]
    )
    .* $
    """,
    re.VERBOSE | re.MULTILINE,
)

# Panics are multiline and our log lines of multiple services are interleaved,
# making them complex to handle in regular expressions, thus handle them
# separately.
# Example 1: launchdarkly-materialized-1  | thread 'coordinator' panicked at [...]
# Example 2: [pod/environmentd-0/environmentd] thread 'coordinator' panicked at [...]
PANIC_IN_SERVICE_START_RE = re.compile(
    rb"^(\[)?(?P<service>[^ ]*)(\s*\||\]) thread '.*' panicked at "
)
# Example 1: launchdarkly-materialized-1  | global timestamp must always go up
# Example 2: [pod/environmentd-0/environmentd] Unknown collection identifier u2082
SERVICES_LOG_LINE_RE = re.compile(rb"^(\[)?(?P<service>[^ ]*)(\s*\||\]) (?P<msg>.*)$")

# Expected failures, don't report them
IGNORE_RE = re.compile(
    rb"""
    # Expected in restart test
    ( restart-materialized-1\ \ \|\ thread\ 'coordinator'\ panicked\ at\ 'can't\ persist\ timestamp
    # Expected in restart test
    | restart-materialized-1\ *|\ thread\ 'coordinator'\ panicked\ at\ 'external\ operation\ .*\ failed\ unrecoverably.*
    # Expected in cluster test
    | cluster-clusterd[12]-1\ .*\ halting\ process:\ new\ timely\ configuration\ does\ not\ match\ existing\ timely\ configuration
    # Emitted by tests employing explicit mz_panic()
    | forced\ panic
    # Emitted by broken_statements.slt in order to stop panic propagation, as 'forced panic' will unwantedly panic the `environmentd` thread.
    | forced\ optimizer\ panic
    # Expected once compute cluster has panicked, brings no new information
    | timely\ communication\ error:
    # Expected once compute cluster has panicked, only happens in CI
    | aborting\ because\ propagate_crashes\ is\ enabled
    # Expected when CRDB is corrupted
    | restart-materialized-1\ .*relation\ \\"fence\\"\ does\ not\ exist
    # Expected when CRDB is corrupted
    | restart-materialized-1\ .*relation\ "consensus"\ does\ not\ exist
    # Will print a separate panic line which will be handled and contains the relevant information (new style)
    | internal\ error:\ unexpected\ panic\ during\ query\ optimization
    # redpanda INFO logging
    | larger\ sizes\ prevent\ running\ out\ of\ memory
    # Old versions won't support new parameters
    | (platform-checks|legacy-upgrade|upgrade-matrix|feature-benchmark)-materialized-.* \| .*cannot\ load\ unknown\ system\ parameter\ from\ catalog\ storage
    # Fencing warnings are OK in fencing tests
    | txn-wal-fencing-mz_first-.* \| .*unexpected\ fence\ epoch
    | txn-wal-fencing-mz_first-.* \| .*fenced\ by\ new\ catalog\ upper
    | txn-wal-fencing-mz_first-.* \| .*fenced\ by\ new\ catalog\ epoch
    | platform-checks-mz_txn_tables.* \| .*unexpected\ fence\ epoch
    | platform-checks-mz_txn_tables.* \| .*fenced\ by\ new\ catalog\ upper
    | platform-checks-mz_txn_tables.* \| .*fenced\ by\ new\ catalog\ epoch
    # For platform-checks upgrade tests
    | platform-checks-clusterd.* \| .* received\ persist\ state\ from\ the\ future
    | cannot\ load\ unknown\ system\ parameter\ from\ catalog\ storage(\ to\ set\ (default|configured)\ parameter)?
    | internal\ error:\ no\ AWS\ external\ ID\ prefix\ configured
    # For tests we purposely trigger this error
    | skip-version-upgrade-materialized.* \| .* incompatible\ persist\ version\ \d+\.\d+\.\d+(-dev)?,\ current:\ \d+\.\d+\.\d+(-dev)?,\ make\ sure\ to\ upgrade\ the\ catalog\ one\ version\ at\ a\ time
    )
    """,
    re.VERBOSE | re.MULTILINE,
)


@dataclass
class ErrorLog:
    match: bytes
    file: str


@dataclass
class JunitError:
    testclass: str
    testcase: str
    message: str
    text: str


@dataclass(kw_only=True, unsafe_hash=True)
class ObservedError(ObservedBaseError):
    # abstract class, do not instantiate
    error_message: str | None
    error_details: str | None = None
    error_type: str
    location: str
    location_url: str | None = None
    max_error_length: int = 10000
    max_details_length: int = 10000


@dataclass(kw_only=True, unsafe_hash=True)
class ObservedErrorWithIssue(ObservedError, WithIssue):
    issue_is_closed: bool

    def _get_issue_presentation(self) -> str:
        issue_presentation = f"#{self.issue_number}"
        if self.issue_is_closed:
            issue_presentation = f"{issue_presentation}, closed"

        return issue_presentation

    def to_text(self) -> str:
        result = f"{self.error_type} {self.issue_title} ({self._get_issue_presentation()}) in {self.location}: {crop_text(self.error_message, self.max_error_length)}"
        if self.error_details is not None:
            result += f"\n{crop_text(self.error_details, self.max_details_length)}"
        return result

    def to_markdown(self) -> str:
        if self.location_url is None:
            location_markdown = self.location
        else:
            location_markdown = f'<a href="{self.location_url}">{self.location}</a>'

        result = f'{self.error_type} <a href="{self.issue_url}">{self.issue_title} ({self._get_issue_presentation()})</a> in {location_markdown}:\n{format_error_message(self.error_message, self.max_details_length)}'
        if self.error_details is not None:
            result += (
                f"\n{format_error_message(self.error_details, self.max_details_length)}"
            )
        return result


@dataclass(kw_only=True, unsafe_hash=True)
class ObservedErrorWithLocation(ObservedError):
    def to_text(self) -> str:
        if self.error_details:
            error_details = f" {crop_text(self.error_details, self.max_details_length)}"
        else:
            error_details = ""

        return f"{self.error_type} in {self.location}: {crop_text(self.error_message, self.max_error_length)}{error_details}"

    def to_markdown(self) -> str:
        if self.error_details:
            formatted_error_details = (
                f"\n{format_error_message(self.error_details, self.max_details_length)}"
            )
        else:
            formatted_error_details = ""

        if self.location_url is None:
            location_markdown = self.location
        else:
            location_markdown = f'<a href="{self.location_url}">{self.location}</a>'

        return f"{self.error_type} in {location_markdown}:\n{format_error_message(self.error_message, self.max_error_length)}{formatted_error_details}"


@dataclass(kw_only=True, unsafe_hash=True)
class FailureInCoverageRun(ObservedError):

    def to_text(self) -> str:
        return f"{self.location}: {crop_text(self.error_message)}"

    def to_markdown(self) -> str:
        return f"{self.location}:\n{format_error_message(self.error_message)}"


@dataclass
class BuildHistoryOnMain:
    pipeline_slug: str
    last_build_step_outcomes: list[BuildStepOutcome]

    def to_markdown(self) -> str:
        return (
            f'<a href="/materialize/{self.pipeline_slug}/builds?branch=main">main</a> history: '
            + "".join(
                [
                    f"<a href=\"{outcome.web_url_to_job}\">{':bk-status-passed:' if outcome.passed else ':bk-status-failed:'}</a>"
                    for outcome in self.last_build_step_outcomes
                ]
            )
        )


@dataclass
class Annotation:
    suite_name: str
    buildkite_job_id: str
    is_failure: bool
    build_history_on_main: BuildHistoryOnMain | None
    unknown_errors: Sequence[ObservedBaseError] = field(default_factory=list)
    known_errors: Sequence[ObservedBaseError] = field(default_factory=list)

    def to_markdown(self) -> str:
        only_known_errors = len(self.unknown_errors) == 0 and len(self.known_errors) > 0
        no_errors = len(self.unknown_errors) == 0 and len(self.known_errors) == 0
        wrap_in_details = only_known_errors
        wrap_in_summary = only_known_errors

        build_link = f'<a href="#{self.buildkite_job_id}">{self.suite_name}</a>'
        outcome = "failed" if self.is_failure else "succeeded"

        title = f"{build_link} {outcome}"

        if only_known_errors:
            title = f"{title} with known error logs"
        elif no_errors:
            title = f"{title}, but no error in logs found"

        markdown = title
        if self.build_history_on_main is not None:
            markdown += ", " + self.build_history_on_main.to_markdown()

        if wrap_in_summary:
            markdown = f"<summary>{markdown}</summary>\n"

        if len(self.unknown_errors) > 0:
            markdown += "\n" + "\n".join(
                f"* {error.to_markdown()}{error.occurrences_to_markdown()}"
                for error in self.unknown_errors
            )
        if len(self.known_errors) > 0:
            markdown += "\n" + "\n".join(
                f"* {error.to_markdown()}{error.occurrences_to_markdown()}"
                for error in self.known_errors
            )

        if wrap_in_details:
            markdown = f"<details>{markdown}\n</details>"

        return markdown


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ci-annotate-errors",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
ci-annotate-errors detects errors in junit xml as well as log files during CI
and finds associated open GitHub issues in Materialize repository.""",
    )

    parser.add_argument("--cloud-hostname", type=str)
    parser.add_argument("log_files", nargs="+", help="log files to search in")
    args = parser.parse_args()

    test_analytics_db = None

    try:
        test_analytics_config = create_test_analytics_config_with_hostname(
            args.cloud_hostname
        )
        test_analytics_db = TestAnalyticsDb(test_analytics_config)

        # always insert a build job regardless whether it has annotations or not
        store_build_job_in_test_analytics(test_analytics_db)
    except Exception as e:
        # An error during an upload must never cause the build to fail
        print(f"Uploading results failed! {e}")

    return annotate_logged_errors(args.log_files, test_analytics_db)


def annotate_errors(
    unknown_errors: Sequence[ObservedBaseError],
    known_errors: Sequence[ObservedBaseError],
    build_history_on_main: BuildHistoryOnMain | None,
    test_analytics_db: TestAnalyticsDb | None,
) -> None:
    assert len(unknown_errors) > 0 or len(known_errors) > 0
    annotation_style = "info" if not unknown_errors else "error"
    unknown_errors = group_identical_errors(unknown_errors)
    known_errors = group_identical_errors(known_errors)
    is_failure = len(unknown_errors) > 0 or not has_successful_buildkite_status()

    annotation = Annotation(
        suite_name=get_suite_name(),
        buildkite_job_id=os.getenv("BUILDKITE_JOB_ID", ""),
        is_failure=is_failure,
        build_history_on_main=build_history_on_main,
        unknown_errors=unknown_errors,
        known_errors=known_errors,
    )

    add_annotation_raw(style=annotation_style, markdown=annotation.to_markdown())

    if test_analytics_db is not None:
        store_annotation_in_test_analytics(test_analytics_db, annotation)


def group_identical_errors(
    errors: Sequence[ObservedBaseError],
) -> Sequence[ObservedBaseError]:
    errors_with_counts: dict[ObservedBaseError, int] = {}

    for error in errors:
        errors_with_counts[error] = 1 + errors_with_counts.get(error, 0)

    consolidated_errors = []

    for error, count in errors_with_counts.items():
        error.occurrences = count
        consolidated_errors.append(error)

    return consolidated_errors


def annotate_logged_errors(
    log_files: list[str], test_analytics_db: TestAnalyticsDb | None
) -> int:
    """
    Returns the number of unknown errors, 0 when all errors are known or there
    were no errors logged. This will be used to fail a test even if the test
    itself succeeded, as long as it had any unknown error logs.
    """

    errors = get_errors(log_files)

    if not errors:
        return 0

    step_key: str = os.getenv("BUILDKITE_STEP_KEY", "")
    buildkite_label: str = os.getenv("BUILDKITE_LABEL", "")

    (known_issues, issues_with_invalid_regex) = get_known_issues_from_github()
    unknown_errors: list[ObservedBaseError] = []
    unknown_errors.extend(issues_with_invalid_regex)

    artifacts = ci_util.get_artifacts()
    job = os.getenv("BUILDKITE_JOB_ID")

    known_errors: list[ObservedBaseError] = []

    # Keep track of known errors so we log each only once
    already_reported_issue_numbers: set[int] = set()

    def handle_error(
        error_message: str,
        error_details: str | None,
        location: str,
        location_url: str | None,
    ):
        search_string = error_message.encode("utf-8")
        if error_details is not None:
            search_string += ("\n" + error_details).encode("utf-8")

        for issue in known_issues:
            match = issue.regex.search(for_github_re(search_string))
            if match and issue.info["state"] == "open":
                if issue.apply_to and issue.apply_to not in (
                    step_key.lower(),
                    buildkite_label.lower(),
                ):
                    continue

                if issue.info["number"] not in already_reported_issue_numbers:
                    known_errors.append(
                        ObservedErrorWithIssue(
                            error_message=error_message,
                            error_details=error_details,
                            error_type="Known issue",
                            internal_error_type="KNOWN_ISSUE",
                            issue_url=issue.info["html_url"],
                            issue_title=issue.info["title"],
                            issue_number=issue.info["number"],
                            issue_is_closed=False,
                            location=location,
                            location_url=location_url,
                        )
                    )
                    already_reported_issue_numbers.add(issue.info["number"])
                break
        else:
            for issue in known_issues:
                match = issue.regex.search(for_github_re(search_string))
                if match and issue.info["state"] == "closed":
                    if issue.apply_to and issue.apply_to not in (
                        step_key.lower(),
                        buildkite_label.lower(),
                    ):
                        continue

                    if issue.info["number"] not in already_reported_issue_numbers:
                        unknown_errors.append(
                            ObservedErrorWithIssue(
                                error_message=error_message,
                                error_details=error_details,
                                error_type="Potential regression",
                                internal_error_type="POTENTIAL_REGRESSION",
                                issue_url=issue.info["html_url"],
                                issue_title=issue.info["title"],
                                issue_number=issue.info["number"],
                                issue_is_closed=True,
                                location=location,
                                location_url=location_url,
                            )
                        )
                        already_reported_issue_numbers.add(issue.info["number"])
                    break
            else:
                unknown_errors.append(
                    ObservedErrorWithLocation(
                        error_message=error_message,
                        error_details=error_details,
                        location=location,
                        location_url=location_url,
                        error_type="Unknown error",
                        internal_error_type="UNKNOWN ERROR",
                    )
                )

    for error in errors:
        if isinstance(error, ErrorLog):
            for artifact in artifacts:
                if artifact["job_id"] == job and artifact["path"] == error.file:
                    location: str = error.file
                    location_url = get_artifact_url(artifact)
                    break
            else:
                location: str = error.file
                location_url = None

            handle_error(error.match.decode("utf-8"), None, location, location_url)
        elif isinstance(error, JunitError):
            if "in Code Coverage" in error.text or "covered" in error.message:
                msg = "\n".join(filter(None, [error.message, error.text]))
                # Don't bother looking up known issues for code coverage report, just print it verbatim as an info message
                known_errors.append(
                    FailureInCoverageRun(
                        error_type="Failure",
                        internal_error_type="FAILURE_IN_COVERAGE_MODE",
                        error_message=msg,
                        location=error.testcase,
                    )
                )
            else:
                handle_error(
                    error.message,
                    error.text,
                    error.testcase,
                    None,
                )
        else:
            raise RuntimeError(f"Unexpected error type: {type(error)}")

    build_history_on_main = get_failures_on_main()
    annotate_errors(
        unknown_errors, known_errors, build_history_on_main, test_analytics_db
    )

    if unknown_errors:
        print(f"+++ Failing test because of {len(unknown_errors)} unknown error(s)")

    # No need for rest of the logic as no error logs were found, but since
    # this script was called the test still failed, so showing the current
    # failures on main branch.
    # Only fetch the main branch status when we are running in CI, but no on
    # main, so inside of a PR or a release branch instead.
    if (
        len(unknown_errors) == 0
        and len(known_errors) == 0
        and ui.env_is_truthy("BUILDKITE")
        and os.getenv("BUILDKITE_BRANCH") != "main"
        and not has_successful_buildkite_status()
        and get_job_state() not in ("canceling", "canceled")
    ):
        annotation = Annotation(
            suite_name=get_suite_name(),
            buildkite_job_id=os.getenv("BUILDKITE_JOB_ID", ""),
            is_failure=True,
            build_history_on_main=build_history_on_main,
            unknown_errors=[],
            known_errors=[],
        )
        add_annotation_raw(style="error", markdown=annotation.to_markdown())

        if test_analytics_db is not None:
            store_annotation_in_test_analytics(test_analytics_db, annotation)

    return len(unknown_errors)


def get_errors(log_file_names: list[str]) -> list[ErrorLog | JunitError]:
    error_logs = []
    for log_file_name in log_file_names:
        # junit_testdrive_* is excluded by this, but currently
        # not more useful than junit_mzcompose anyway
        if "junit_" in log_file_name and "junit_testdrive_" not in log_file_name:
            error_logs.extend(_get_errors_from_junit_file(log_file_name))
        else:
            error_logs.extend(_get_errors_from_log_file(log_file_name))

    return error_logs


def _get_errors_from_junit_file(log_file_name: str) -> list[JunitError]:
    error_logs = []
    xml = JUnitXml.fromfile(log_file_name)
    for suite in xml:
        for testcase in suite:
            for result in testcase.result:
                if not isinstance(result, Error) and not isinstance(result, Failure):
                    continue
                error_logs.append(
                    JunitError(
                        testcase.classname,
                        testcase.name,
                        result.message or "",
                        result.text or "",
                    )
                )
    return error_logs


def _get_errors_from_log_file(log_file_name: str) -> list[ErrorLog]:
    error_logs = []
    with open(log_file_name, "r+") as f:
        try:
            data: Any = mmap.mmap(f.fileno(), 0)
        except ValueError:
            # empty file, ignore
            return error_logs

        error_logs.extend(_collect_errors_in_logs(data, log_file_name))
        data.seek(0)
        error_logs.extend(_collect_service_panics_in_logs(data, log_file_name))

    return error_logs


def _collect_errors_in_logs(data: Any, log_file_name: str) -> list[ErrorLog]:
    collected_errors = []

    for match in ERROR_RE.finditer(data):
        if IGNORE_RE.search(match.group(0)):
            continue
        # environmentd segfaults during normal shutdown in coverage builds, see #20016
        # Ignoring this in regular ways would still be quite spammy.
        if (
            b"environmentd" in match.group(0)
            and b"segfault at" in match.group(0)
            and ui.env_is_truthy("CI_COVERAGE_ENABLED")
        ):
            continue
        collected_errors.append(ErrorLog(match.group(0), log_file_name))

    return collected_errors


def _collect_service_panics_in_logs(data: Any, log_file_name: str) -> list[ErrorLog]:
    collected_panics = []

    open_panics = {}
    for line in iter(data.readline, b""):
        line = line.rstrip(b"\n")
        if match := PANIC_IN_SERVICE_START_RE.match(line):
            service = match.group("service")
            assert (
                service not in open_panics
            ), f"Two panics of same service {service} interleaving: {line}"
            open_panics[service] = line
        elif open_panics:
            if match := SERVICES_LOG_LINE_RE.match(line):
                # Handling every services.log line here, filter to
                # handle only the ones which are currently in a panic
                # handler:
                if panic_start := open_panics.get(match.group("service")):
                    del open_panics[match.group("service")]
                    if IGNORE_RE.search(match.group(0)):
                        continue
                    collected_panics.append(
                        ErrorLog(panic_start + b" " + match.group("msg"), log_file_name)
                    )
    assert not open_panics, f"Panic log never finished: {open_panics}"

    return collected_panics


def crop_text(text: str | None, max_length: int = 10_000) -> str:
    if text is None:
        return ""

    if len(text) > max_length:
        text = text[:max_length] + " [...]"

    return text


def sanitize_text(text: str, max_length: int = 4_000) -> str:
    text = crop_text(text, max_length)
    text = text.replace("```", r"\`\`\`")
    return text


def get_failures_on_main() -> BuildHistoryOnMain | None:
    pipeline_slug = os.getenv("BUILDKITE_PIPELINE_SLUG")
    step_key = os.getenv("BUILDKITE_STEP_KEY")
    step_name = os.getenv("BUILDKITE_LABEL") or step_key
    parallel_job = os.getenv("BUILDKITE_PARALLEL_JOB")
    current_build_number = os.getenv("BUILDKITE_BUILD_NUMBER")
    if parallel_job is not None:
        parallel_job = int(parallel_job)
    assert pipeline_slug is not None
    assert step_key is not None
    assert step_name is not None
    assert current_build_number is not None

    # This is only supposed to be invoked when the build step failed.
    builds_data = builds_api.get_builds(
        pipeline_slug=pipeline_slug,
        max_fetches=1,
        branch="main",
        # also include builds that are still running (the relevant build step may already have completed)
        build_states=["running", "passed", "failing", "failed"],
        # assume and account that at most one build is still running
        items_per_page=5 + 1,
    )

    if not builds_data:
        print(f"Got no finished builds of pipeline {pipeline_slug}")
        return None
    else:
        print(f"Fetched {len(builds_data)} builds of pipeline {pipeline_slug}")

    build_step_matcher = BuildStepMatcher(step_key, parallel_job)
    last_build_step_outcomes = extract_build_step_outcomes(
        builds_data,
        selected_build_steps=[build_step_matcher],
        build_step_states=BUILDKITE_RELEVANT_COMPLETED_BUILD_STEP_STATES,
    )

    # remove the current build
    last_build_step_outcomes = [
        outcome
        for outcome in last_build_step_outcomes
        if outcome.build_number != current_build_number
    ]

    if len(last_build_step_outcomes) > 8:
        # the number of build steps might be higher than the number of requested builds due to retries
        last_build_step_outcomes = last_build_step_outcomes[:8]

    if not last_build_step_outcomes:
        print(
            f"The {len(builds_data)} last fetched builds do not contain a completed build step matching {build_step_matcher}"
        )
        return None

    pipeline_slug = os.getenv("BUILDKITE_PIPELINE_SLUG")
    assert pipeline_slug is not None

    return BuildHistoryOnMain(
        pipeline_slug,
        last_build_step_outcomes,
    )


def get_job_state() -> str:
    pipeline_slug = os.getenv("BUILDKITE_PIPELINE_SLUG")
    build_number = os.getenv("BUILDKITE_BUILD_NUMBER")
    job_id = os.getenv("BUILDKITE_JOB_ID")
    url = f"organizations/materialize/pipelines/{pipeline_slug}/builds/{build_number}"
    build = generic_api.get(url, {})
    for job in build["jobs"]:
        if job["id"] == job_id:
            return job["state"]
    raise ValueError("Job not found")


def get_suite_name(include_retry_info: bool = True) -> str:
    suite_name = os.getenv("BUILDKITE_LABEL", "Unknown Test")

    if include_retry_info:
        retry_count = get_retry_count()
        if retry_count > 0:
            suite_name += f" (#{retry_count + 1})"

    return suite_name


def get_retry_count() -> int:
    return int(os.getenv("BUILDKITE_RETRY_COUNT", "0"))


def has_successful_buildkite_status() -> bool:
    return os.getenv("BUILDKITE_COMMAND_EXIT_STATUS") == "0"


def format_error_message(error_message: str | None, max_length: int = 10_000) -> str:
    if not error_message:
        return ""

    # Don't have too huge output, so truncate
    return f"```\n{sanitize_text(error_message, max_length)}\n```"


def store_build_job_in_test_analytics(test_analytics: TestAnalyticsDb) -> None:
    try:
        test_analytics.builds.insert_build_job(
            was_successful=has_successful_buildkite_status()
        )
    except Exception as e:
        # never cause the whole script to fail
        print(e)


def store_annotation_in_test_analytics(
    test_analytics: TestAnalyticsDb, annotation: Annotation
) -> None:
    try:
        # the build step was already inserted before
        # the buildkite status may have been successful but the build may still fail due to unknown errors in the log
        test_analytics.builds.update_build_job_success(
            was_successful=not annotation.is_failure
        )

        error_entries = [
            AnnotationErrorEntry(
                error_type=error.internal_error_type,
                message=error.to_text(),
                issue=(
                    f"materialize/{error.issue_number}"
                    if isinstance(error, WithIssue)
                    else None
                ),
                occurrence_count=error.occurrences,
            )
            for error in chain(annotation.known_errors, annotation.unknown_errors)
        ]

        test_analytics.build_annotations.insert_annotation(
            build_annotation_storage.AnnotationEntry(
                test_suite=get_suite_name(include_retry_info=False),
                test_retry_count=get_retry_count(),
                is_failure=annotation.is_failure,
                errors=error_entries,
            ),
        )
    except Exception as e:
        # never cause the whole script to fail
        print(e)


if __name__ == "__main__":
    sys.exit(main())
