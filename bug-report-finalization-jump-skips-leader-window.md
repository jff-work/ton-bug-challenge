# Finalization-Induced Jump Skips Leader Window (Liveness Issue)

## Impact

**Liveness degradation.** When a validator catches up after being offline and receives a finalization certificate for a far-ahead slot, the `advance_present()` function announces the new leader window at a non-zero offset. The `ConsensusImpl` only starts block generation when `offset == 0`, so the leader for the jumped-to window never produces blocks. The entire window is wasted, requiring timeout + skip votes before the chain can advance.

## Short Description

In `pool.cpp:804-806`, when a `FinalCert` arrives for a slot ahead of `now_`, the code jumps `now_` directly to `id.slot + 1` and calls `advance_present()`. This publishes `LeaderWindowObserved` with `start_slot = now_`, which may have a non-zero offset within its leader window (`start_slot % slots_per_leader_window != 0`).

In `consensus.cpp:123-130`, block generation only starts when `offset == 0`:
```cpp
td::uint32 offset = event->start_slot % slots_per_leader_window_;
if (offset == 0) {
    previous_window_had_skip_ = false;
    if (bus.collator_schedule->is_expected_collator(bus.local_id.idx, event->start_slot)) {
        start_generation(event->base, event->start_slot).start().detach();
    }
}
```

When `offset != 0`, the leader misses the window entirely.

## Reproduction Scenario

1. Network with 4 validators, `slots_per_leader_window = 4`
2. Validator V goes offline after slot 10
3. Network continues producing blocks: slots 11-100 are notarized/finalized by other validators
4. Validator V comes back online, receives finalization cert for slot 100
5. V sets `now_ = 101`, announces `LeaderWindowObserved(101, base)`
6. `101 % 4 = 1` → offset is 1 → leader for window 25 does NOT start generation
7. Window 25 times out, skip votes are sent, chain continues at window 26

**Impact per jump:** ~`first_block_timeout + slots_per_leader_window * target_rate` seconds wasted (typically 2-5 seconds per wasted window).

## Affected Files

- `validator/consensus/simplex/pool.cpp:804-806` — jump sets non-zero offset
- `validator/consensus/simplex/consensus.cpp:123-130` — generation only at offset 0

## Suggested Fix

When `advance_present` publishes a `LeaderWindowObserved` with a non-zero offset, round `start_slot` down to the window boundary:

```cpp
// In advance_present():
td::uint32 window_start = (now_ / slots_per_leader_window_) * slots_per_leader_window_;
owning_bus().publish<LeaderWindowObserved>(window_start, base).start().detach();
```

Or alternatively, in `ConsensusImpl::handle(LeaderWindowObserved)`, allow generation to start even at non-zero offsets (generating only the remaining slots in the window).
