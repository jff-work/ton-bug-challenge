# Memory Leak in CandidateResolver and StateResolver

## Impact

**Resource exhaustion / Denial of Service.** Validator memory grows monotonically without bound, eventually causing OOM. In our reproduction (2 validators, 165 blocks, ~8.5 minutes), memory grew by 170-245 KB per block. At production block rates (1 block/sec), this projects to **12-20 GB/day** of accumulated leaked memory.

## Short Description

The `CandidateResolverImpl::state_` map (`std::map<CandidateId, CandidateState>`) in `validator/consensus/simplex/candidate-resolver.cpp` and the `StateResolverImpl::state_cache_`/`finalized_blocks_` maps in `validator/consensus/simplex/state-resolver.cpp` grow with every processed block but are **never cleaned up** after finalization. Each entry holds `CandidateRef` (containing full block data + collated data) and `ChainStateRef` (containing vm::Cell Merkle trees). Over time, this accumulates all ever-seen block data in memory, causing a linear-in-time memory leak that will eventually OOM the validator.

## Affected Files

1. **`validator/consensus/simplex/candidate-resolver.cpp:208`** — `std::map<CandidateId, CandidateState> state_`
   - Entries added via `NotarizationObserved` (line 192), `StoreCandidate` (line 166), `ResolveCandidate` (line 147)
   - Each `CandidateState` holds `CandidateAndCert` which contains `std::optional<CandidateRef>` (block data) and `std::optional<NotarCertRef>` (certificate)
   - No entries are ever removed

2. **`validator/consensus/simplex/state-resolver.cpp:88`** — `std::map<ParentId, CachedState> state_cache_`
   - Entries added via `resolve_state` (line 91) for every unique ParentId
   - Each `CachedState` holds `ResolvedState` which contains `ChainStateRef` (vm::Cell tree references)
   - No entries are ever evicted

3. **`validator/consensus/simplex/state-resolver.cpp:155`** — `std::map<CandidateId, FinalizedBlock> finalized_blocks_`
   - Entries added via `finalize_blocks` (line 159) for every finalized block
   - Never cleaned up

## Reproduction

### Environment
- TON testnet branch (commit at time of contest)
- macOS (tested) or Linux
- 2 validators, standard catchain consensus

### Steps
```bash
cd ton-repo
# Build
mkdir -p build && cd build && cmake -GNinja -DCMAKE_BUILD_TYPE=Release .. && ninja validator-engine create-state tonlibjson && cd ..
# Install tontester
python3 -m venv .venv && .venv/bin/pip install test/tontester
# Generate TL bindings
.venv/bin/python3 test/tontester/generate_tl.py
# Run reproduction
PYTHONPATH=test/tontester/src .venv/bin/python3 test/integration/test_memory_leak.py
```

### Script: `test/integration/test_memory_leak.py`
See attached file. Creates a 2-validator network, monitors RSS memory of validator processes every 10 seconds, and reports growth rate per block.

### Observed Results
```
 Time(s)   MC Seqno   node-1 RSS(MB)  node-2 RSS(MB)
----------------------------------------------------
       0          1             74.1            73.5
      50         17             78.5            77.0
     100         33             82.0            79.8
     200         66             88.0            83.1
     300         98             98.3           103.3
     400        130            100.0           108.4
     500        162            101.7           113.8
```

Memory grows monotonically: +27.7 MB (node-1) and +40.5 MB (node-2) over 165 blocks.

## Suggested Fix

Add cleanup hooks in response to `FinalizationObserved` events:

### candidate-resolver.cpp
```cpp
template <>
void handle(BusHandle, std::shared_ptr<const FinalizationObserved> event) {
    auto &state = state_[event->id];
    state.candidate_and_cert.notar_cert = event->certificate;
    maybe_resume_resolve_awaiters(state);

    // NEW: Clean up finalized entries
    cleanup_finalized(event->id);
}

void cleanup_finalized(CandidateId finalized_id) {
    // Remove entries for blocks at or before the finalized slot
    for (auto it = state_.begin(); it != state_.end(); ) {
        if (it->first.slot <= finalized_id.slot && it->second.resolve_awaiters.empty()
            && it->second.store_awaiters.empty()) {
            it = state_.erase(it);
        } else {
            ++it;
        }
    }
}
```

### state-resolver.cpp
```cpp
template <>
void handle(BusHandle, std::shared_ptr<const FinalizationObserved> event) {
    finalize_blocks(event->id, event->certificate, std::nullopt).start().detach();

    // NEW: Evict old state cache entries
    for (auto it = state_cache_.begin(); it != state_cache_.end(); ) {
        if (it->first.has_value() && it->first->slot <= event->id.slot
            && it->second.promises.empty()) {
            it = state_cache_.erase(it);
        } else {
            ++it;
        }
    }
}
```
