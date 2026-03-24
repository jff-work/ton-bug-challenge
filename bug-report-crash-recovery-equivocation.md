# Crash Recovery Can Cause Honest Validator Equivocation

## Impact

**Byzantine fault tolerance / safety margin erosion.** An honest validator that crashes at the wrong moment can produce conflicting votes for the same slot, appearing Byzantine to other validators. This triggers false misbehavior detection, and if multiple validators crash simultaneously, could theoretically contribute to notarization of conflicting blocks.

## Short Description

The vote broadcast and DB persistence paths are not atomic. In `pool.cpp:573-593`, `handle_our_vote` first signs the vote, then broadcasts it to the network (`OutgoingProtocolMessage`), and only then does the DB write happen asynchronously (via `BroadcastVote` event to `DbImpl`).

If the validator crashes after broadcasting a vote but before the DB write completes:
1. Other validators have received and stored the vote (e.g., NotarizeVote for candidate A at slot S)
2. The crashed validator's DB does NOT contain this vote
3. On restart, `bootstrap_votes` doesn't include the vote for slot S
4. When a candidate B arrives for slot S, the validator votes for B
5. Other validators detect conflicting votes from the same validator for slot S

## Detailed Flow

```
[Before crash]
1. ConsensusImpl receives candidate A for slot S
2. try_notarize → BroadcastVote(NotarizeVote{A})
3. Pool::handle_our_vote signs the vote
4. Pool broadcasts signed vote to network   ← Other validators receive this
5. DbImpl::process(BroadcastVote) starts    ← co_await db->set(...)
6. CRASH before DB write completes

[After restart]
7. DbImpl::init_votes loads from DB → vote for slot S is MISSING
8. ConsensusImpl::start_up loads bootstrap_votes → no notar vote for slot S
9. Pool::start_up processes bootstrap → no vote for slot S
10. Candidate B arrives for slot S
11. ConsensusImpl votes NotarizeVote{B}
12. Pool broadcasts signed vote for B
13. Other validators see: vote A AND vote B from same validator at slot S → MISBEHAVIOR
```

## Vulnerability Window

The gap between step 4 (broadcast) and step 5 (DB write start) is the vulnerability window. In the actor model, these happen in the same event loop iteration, so the window is very small (microseconds). However:

- The DB write itself is async (`co_await db->set`). If RocksDB is slow (disk I/O), the write may take milliseconds.
- A SIGKILL, power failure, or kernel panic during this window would trigger the bug.
- The `BroadcastVote` process handler in DbImpl can fail silently (errors are not propagated back to Pool due to `.start().detach()`).

## Affected Files

- `validator/consensus/simplex/pool.cpp:573-593` — `handle_our_vote` broadcasts before DB write
- `validator/consensus/simplex/db.cpp:56-75` — DB write is async and can fail silently

## Suggested Fix

Option A: Write-ahead approach — save the vote to DB BEFORE broadcasting:
```cpp
// In handle_our_vote, save before broadcast:
co_await owning_bus().publish<BroadcastVote>(vote);  // Wait for DB write
// Only then broadcast:
owning_bus().publish<OutgoingProtocolMessage>(std::nullopt, std::move(serialized));
```

Option B: Use RocksDB WriteBatch to atomically write both the vote and the pool state update together.

Option C: On restart, re-broadcast all bootstrap votes to the network to ensure consistency (currently suppressed with `suppress_vote_broadcast=true`).
