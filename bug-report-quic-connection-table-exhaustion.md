# QUIC Connection Table Memory Exhaustion (Secondary Scope)

## Impact

**Resource exhaustion / Denial of Service.** The QUIC sender maintains unbounded maps for inbound and outbound connections. A peer creating connections from many distinct addresses can grow these maps without limit, eventually exhausting validator memory.

## Short Description

In `quic/quic-sender.h:81-83`, three unbounded maps track QUIC connections:

```cpp
std::map<AdnlPath, std::shared_ptr<Connection>> outbound_;
std::map<AdnlPath, std::shared_ptr<Connection>> inbound_;
std::map<QuicConnectionId, std::shared_ptr<Connection>> by_cid_;
```

`AdnlPath` is a (local_id, peer_id) pair. Old connections in `inbound_` are only replaced when a NEW connection arrives from the SAME path (quic-sender.cpp:521-535). A malicious peer rotating through many distinct peer IDs can accumulate unbounded entries in the inbound map.

Each entry holds a `shared_ptr<Connection>` which contains:
- QUIC connection state
- Send/receive buffers
- QuicServer actor reference

## Attack Scenario

1. Attacker rotates through M distinct ADNL peer IDs
2. For each ID, initiates a QUIC connection to the victim validator
3. Each connection is stored in `inbound_` keyed by the unique AdnlPath
4. Old connections from different paths are NEVER evicted
5. After M connections: M entries in `inbound_` + M entries in `by_cid_`
6. Memory grows linearly with M, no upper bound

## Affected Files

- `quic/quic-sender.h:81-83` — unbounded connection maps
- `quic/quic-sender.cpp:521-535` — only same-path replacement, no eviction

## Suggested Fix

1. Add a maximum connection count per local_id
2. Implement LRU eviction when the limit is reached
3. Add rate limiting for new inbound connections
