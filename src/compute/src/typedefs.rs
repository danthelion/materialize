// Copyright Materialize, Inc. and contributors. All rights reserved.
//
// Use of this software is governed by the Business Source License
// included in the LICENSE file.
//
// As of the Change Date specified in that file, in accordance with
// the Business Source License, use of this software will be governed
// by the Apache License, Version 2.0.

//! Convience typedefs for differential types.

#![allow(dead_code, missing_docs)]

use differential_dataflow::operators::arrange::Arranged;
use differential_dataflow::operators::arrange::TraceAgent;
use differential_dataflow::trace::implementations::chunker::ColumnationChunker;
use differential_dataflow::trace::implementations::merge_batcher::MergeBatcher;
use differential_dataflow::trace::implementations::merge_batcher_col::ColumnationMerger;
use differential_dataflow::trace::implementations::ord_neu::OrdValBatch;
use differential_dataflow::trace::wrappers::enter::TraceEnter;
use differential_dataflow::trace::wrappers::frontier::TraceFrontier;
use mz_repr::Diff;
use mz_storage_types::errors::DataflowError;
use timely::dataflow::ScopeParent;

pub use crate::row_spine::{RowRowSpine, RowSpine, RowValSpine};
use crate::typedefs::spines::MzFlatLayout;
pub use crate::typedefs::spines::{ColKeySpine, ColValSpine};

pub(crate) mod spines {
    use std::rc::Rc;

    use differential_dataflow::trace::implementations::chunker::ContainerChunker;
    use differential_dataflow::trace::implementations::merge_batcher::MergeBatcher;
    use differential_dataflow::trace::implementations::merge_batcher_flat::FlatcontainerMerger;
    use differential_dataflow::trace::implementations::ord_neu::{
        OrdKeyBatch, OrdKeyBuilder, OrdValBatch, OrdValBuilder,
    };
    use differential_dataflow::trace::implementations::spine_fueled::Spine;
    use differential_dataflow::trace::implementations::{Layout, Update};
    use differential_dataflow::trace::rc_blanket_impls::RcBuilder;
    use mz_timely_util::containers::stack::StackWrapper;
    use timely::container::columnation::{Columnation, TimelyStack};
    use timely::container::flatcontainer::{Containerized, FlatStack, Push, Region};

    use crate::row_spine::OffsetOptimized;
    use crate::typedefs::{KeyBatcher, KeyValBatcher};

    /// A spine for generic keys and values.
    pub type ColValSpine<K, V, T, R> = Spine<
        Rc<OrdValBatch<MzStack<((K, V), T, R)>>>,
        KeyValBatcher<K, V, T, R>,
        RcBuilder<OrdValBuilder<MzStack<((K, V), T, R)>, TimelyStack<((K, V), T, R)>>>,
    >;

    /// A spine for generic keys
    pub type ColKeySpine<K, T, R> = Spine<
        Rc<OrdKeyBatch<MzStack<((K, ()), T, R)>>>,
        KeyBatcher<K, T, R>,
        RcBuilder<OrdKeyBuilder<MzStack<((K, ()), T, R)>, TimelyStack<((K, ()), T, R)>>>,
    >;

    /// A layout based on chunked timely stacks
    pub struct MzStack<U: Update> {
        phantom: std::marker::PhantomData<U>,
    }

    impl<U: Update> Layout for MzStack<U>
    where
        U::Key: Columnation + 'static,
        U::Val: Columnation + 'static,
        U::Time: Columnation,
        U::Diff: Columnation,
    {
        type Target = U;
        type KeyContainer = StackWrapper<U::Key>;
        type ValContainer = StackWrapper<U::Val>;
        type TimeContainer = StackWrapper<U::Time>;
        type DiffContainer = StackWrapper<U::Diff>;
        type OffsetContainer = OffsetOptimized;
    }

    /// A layout based on timely stacks
    pub struct MzFlatLayout<U: Update> {
        phantom: std::marker::PhantomData<U>,
    }

    impl<U: Update> Layout for MzFlatLayout<U>
    where
        U::Key: Containerized,
        for<'a> <U::Key as Containerized>::Region:
            Push<U::Key> + Push<<<U::Key as Containerized>::Region as Region>::ReadItem<'a>>,
        for<'a> <<U::Key as Containerized>::Region as Region>::ReadItem<'a>: Copy + Ord,
        U::Val: Containerized,
        for<'a> <U::Val as Containerized>::Region:
            Push<U::Val> + Push<<<U::Val as Containerized>::Region as Region>::ReadItem<'a>>,
        for<'a> <<U::Val as Containerized>::Region as Region>::ReadItem<'a>: Copy + Ord,
        U::Time: Containerized,
        <U::Time as Containerized>::Region: Region<Owned = U::Time>,
        for<'a> <U::Time as Containerized>::Region:
            Push<U::Time> + Push<<<U::Time as Containerized>::Region as Region>::ReadItem<'a>>,
        for<'a> <<U::Time as Containerized>::Region as Region>::ReadItem<'a>: Copy + Ord,
        U::Diff: Containerized,
        <U::Diff as Containerized>::Region: Region<Owned = U::Diff>,
        for<'a> <U::Diff as Containerized>::Region:
            Push<U::Diff> + Push<<<U::Diff as Containerized>::Region as Region>::ReadItem<'a>>,
        for<'a> <<U::Diff as Containerized>::Region as Region>::ReadItem<'a>: Copy + Ord,
    {
        type Target = U;
        type KeyContainer = FlatStack<<U::Key as Containerized>::Region>;
        type ValContainer = FlatStack<<U::Val as Containerized>::Region>;
        type TimeContainer = FlatStack<<U::Time as Containerized>::Region>;
        type DiffContainer = FlatStack<<U::Diff as Containerized>::Region>;
        type OffsetContainer = OffsetOptimized;
    }

    /// A trace implementation backed by flatcontainer storage.
    pub type FlatValSpine<K, V, T, R, C> = Spine<
        Rc<OrdValBatch<MzFlatLayout<((K, V), T, R)>>>,
        MergeBatcher<
            C,
            ContainerChunker<FlatStack<<((K, V), T, R) as Containerized>::Region>>,
            FlatcontainerMerger<T, R, <((K, V), T, R) as Containerized>::Region>,
            T,
        >,
        RcBuilder<
            OrdValBuilder<
                MzFlatLayout<((K, V), T, R)>,
                FlatStack<<((K, V), T, R) as Containerized>::Region>,
            >,
        >,
    >;
}

// Spines are data structures that collect and maintain updates.
// Agents are wrappers around spines that allow shared read access.

// Fully generic spines and agents.
pub type KeyValSpine<K, V, T, R> = ColValSpine<K, V, T, R>;
pub type KeyValAgent<K, V, T, R> = TraceAgent<KeyValSpine<K, V, T, R>>;
pub type KeyValEnter<K, V, T, R, TEnter> =
    TraceEnter<TraceFrontier<KeyValAgent<K, V, T, R>>, TEnter>;

// Fully generic key-only spines and agents
pub type KeySpine<K, T, R> = ColKeySpine<K, T, R>;
pub type KeyAgent<K, T, R> = TraceAgent<KeySpine<K, T, R>>;
pub type KeyEnter<K, T, R, TEnter> = TraceEnter<TraceFrontier<KeyAgent<K, T, R>>, TEnter>;

// Row specialized spines and agents.
pub type RowValAgent<V, T, R> = TraceAgent<RowValSpine<V, T, R>>;
pub type RowValArrangement<S, V> = Arranged<S, RowValAgent<V, <S as ScopeParent>::Timestamp, Diff>>;
pub type RowValEnter<V, T, R, TEnter> = TraceEnter<TraceFrontier<RowValAgent<V, T, R>>, TEnter>;
// Row specialized spines and agents.
pub type RowRowAgent<T, R> = TraceAgent<RowRowSpine<T, R>>;
pub type RowRowArrangement<S> = Arranged<S, RowRowAgent<<S as ScopeParent>::Timestamp, Diff>>;
pub type RowRowEnter<T, R, TEnter> = TraceEnter<TraceFrontier<RowRowAgent<T, R>>, TEnter>;
// Row specialized spines and agents.
pub type RowAgent<T, R> = TraceAgent<RowSpine<T, R>>;
pub type RowArrangement<S> = Arranged<S, RowAgent<<S as ScopeParent>::Timestamp, Diff>>;
pub type RowEnter<T, R, TEnter> = TraceEnter<TraceFrontier<RowAgent<T, R>>, TEnter>;

// Error specialized spines and agents.
pub type ErrSpine<T, R> = ColKeySpine<DataflowError, T, R>;
pub type ErrAgent<T, R> = TraceAgent<ErrSpine<T, R>>;
pub type ErrEnter<T, TEnter> = TraceEnter<TraceFrontier<ErrAgent<T, Diff>>, TEnter>;
pub type KeyErrSpine<K, T, R> = ColValSpine<K, DataflowError, T, R>;
pub type RowErrSpine<T, R> = RowValSpine<DataflowError, T, R>;

// Batchers for consolidation
pub type KeyBatcher<K, T, D> = KeyValBatcher<K, (), T, D>;
pub type KeyValBatcher<K, V, T, D> = MergeBatcher<
    Vec<((K, V), T, D)>,
    ColumnationChunker<((K, V), T, D)>,
    ColumnationMerger<((K, V), T, D)>,
    T,
>;

pub type FlatKeyValBatch<K, V, T, R> = OrdValBatch<MzFlatLayout<((K, V), T, R)>>;
pub type FlatKeyValSpine<K, V, T, R, C> = spines::FlatValSpine<K, V, T, R, C>;
pub type FlatKeyValAgent<K, V, T, R, C> = TraceAgent<FlatKeyValSpine<K, V, T, R, C>>;
pub type FlatKeyValEnter<K, V, T, R, C, TEnter> =
    TraceEnter<TraceFrontier<FlatKeyValAgent<K, V, T, R, C>>, TEnter>;
