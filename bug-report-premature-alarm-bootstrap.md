# Premature Skip Votes During Bootstrap Due to Uninitialized timeout_base_

## Impact

**Liveness degradation after restart.** On validator restart, `ConsensusImpl` receives `NotarizationObserved` events from Pool's bootstrap before `LeaderWindowObserved` has fired, leaving `timeout_base_` at its default value `Timestamp{0}`. The alarm timestamp calculation produces a time far in the past, causing the alarm to fire immediately and send premature skip votes for the current/next window.

Additionally, `start_up()` unconditionally sends skip votes for ALL non-finalized slots in the previous window, including slots where the validator already voted notar. This permanently prevents the restarted validator from voting Final for those slots.

## Short Description

### Issue 1: Uninitialized timeout_base_

`timeout_base_` is declared as `td::Timestamp timeout_base_` (consensus.cpp:279), which default-initializes to `Timestamp{at_=0}` (Time.h:116). It's only properly set in `handle(LeaderWindowObserved)` (consensus.cpp:134).

During bootstrap, Pool publishes `NotarizationObserved` from `handle_saved_certificate` (pool.cpp:766). ConsensusImpl's `process_notarization_observed` receives this and computes:

```cpp
alarm_timestamp() = td::Timestamp::in(
    (timeout_slot_ - current_window_ * slots_per_leader_window_) * target_rate_s_, timeout_base_);
```

With `timeout_base_ = Timestamp{0}`, `Timestamp::in(value, Timestamp{0})` = `Timestamp{0 + value}` ≈ `Timestamp{2.0}`, which is far in the past relative to the monotonic clock (which could be thousands of seconds since boot). The alarm fires immediately.

### Issue 2: Unconditional skip votes in start_up

In `start_up()` (consensus.cpp:82-92):
```cpp
if (auto window = bus.first_nonannounced_window) {
    for (td::uint32 i = start_slot; i < end_slot; ++i) {
      auto slot = state_->slot_at(i);
      if (slot.has_value() && !slot->state->voted_final) {
        slot->state->voted_skip = true;
        owning_bus().publish<BroadcastVote>(SkipVote{i}).start().detach();
      }
    }
}
```

The check is `!voted_final`, not `!voted_final && !voted_notar`. So slots where the validator previously voted notar (but not yet final) get skip votes. This sets `voted_skip = true`, which permanently prevents `try_vote_final` from firing (consensus.cpp:271 checks `!voted_skip`).

## Combined Effect

After restart:
1. Skip votes are sent for all non-finalized slots in the previous window (even notar'd ones)
2. Premature alarm sends additional skip votes for the current window
3. The validator contributes less to finalization, reducing the total finalization weight

If >1/3 of validators restart simultaneously (e.g., rolling upgrade with bad timing, or a network event), the combined loss of finalization weight could prevent blocks from being finalized. Skip certs would form instead, and the notarized-but-unfinalized blocks in the previous window would never be finalized.

For masterchain: non-finalized blocks are not accepted (state-resolver.cpp:185), potentially creating a gap in the accepted chain.

## Affected Files

- `validator/consensus/simplex/consensus.cpp:82-92` — unconditional skip on restart
- `validator/consensus/simplex/consensus.cpp:239-260` — alarm with uninitialized timeout_base_
- `validator/consensus/simplex/consensus.cpp:279` — timeout_base_ declaration

## Suggested Fix

1. Guard `process_notarization_observed` with a `started_` flag, only processing events after `LeaderWindowObserved` has fired at least once:
```cpp
td::actor::Task<> process_notarization_observed(...) {
    if (!timeout_initialized_) co_return {};  // Wait for LeaderWindowObserved
    ...
}
```

2. In `start_up()`, check `voted_notar` before sending skip:
```cpp
if (slot.has_value() && !slot->state->voted_final && !slot->state->voted_notar.has_value()) {
    slot->state->voted_skip = true;
    ...
}
```
