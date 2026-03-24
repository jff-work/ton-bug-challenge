# CandidateResolver Crash on Missing DB Entry

## Impact

**Validator crash (DoS).** If a candidate's metadata entry exists in the DB but its actual data entry is missing (due to partial write, DB corruption, or incomplete startup), `try_load_candidate_data_from_db` crashes with an unhandled exception.

## Short Description

In `candidate-resolver.cpp:254`, the code calls `.value()` on the result of `bus.db->get()` without checking if the result is empty:

```cpp
td::actor::Task<bool> try_load_candidate_data_from_db(CandidateId id, CandidateState &state) {
    if (state.candidate_and_cert.candidate.has_value()) {
        co_return true;
    }
    if (state.candidate_in_db) {
        auto contents_key = create_serialize_tl_object<tl::db_key_candidate>(id.to_tl());
        auto data = bus.db->get(std::move(contents_key)).value();  // CRASHES if nullopt!
        state.candidate_and_cert.candidate = Candidate::deserialize(data, bus).move_as_ok();  // CRASHES on deser failure!
    }
    co_return false;
}
```

**Two crash paths:**
1. Line 254: `.value()` throws `std::bad_optional_access` if the key doesn't exist in the DB snapshot
2. Line 255: `.move_as_ok()` crashes if deserialization fails (corrupted data)

## How the inconsistency can occur

During startup, `load_from_db()` (line 210) loads candidate metadata by prefix scan:
```cpp
auto candidates = bus.db->get_by_prefix(tl::db_key_candidateResolver_candidateInfo::ID);
```

And in `store_candidate` (line 327), two separate writes occur:
```cpp
co_await bus.db->set(std::move(contents_key), (*candidate)->serialize());  // Write 1: actual data
co_await bus.db->set(std::move(index_key), td::BufferSlice());             // Write 2: metadata entry
```

If the validator crashes between Write 2 and Write 1 completing... wait, Write 1 happens BEFORE Write 2. So if the node crashes after Write 2 but Write 1 was successful, both entries exist. But the DB reader uses a snapshot from startup. If the snapshot was taken before Write 1 completed (because the async writer hadn't flushed), the metadata entry EXISTS but the data entry does NOT.

The DB reader (`bridge.cpp:107`) uses a **snapshot** from DB open time. The writer is async (`KeyValueAsync`). If the last session wrote the metadata index entry but the data entry hadn't been flushed to the RocksDB WAL before crash, the reader snapshot on restart would have the metadata but not the data.

## Affected Files

- `validator/consensus/simplex/candidate-resolver.cpp:254-255` — unchecked `.value()` and `.move_as_ok()`
- `validator/consensus/simplex/candidate-resolver.cpp:327-341` — non-atomic two-write pattern
- `validator/consensus/bridge.cpp:107` — snapshot-based reader

## Suggested Fix

```cpp
if (state.candidate_in_db) {
    auto contents_key = create_serialize_tl_object<tl::db_key_candidate>(id.to_tl());
    auto data = bus.db->get(std::move(contents_key));
    if (data.has_value()) {
        auto result = Candidate::deserialize(*data, bus);
        if (result.is_ok()) {
            state.candidate_and_cert.candidate = result.move_as_ok();
        } else {
            LOG(WARNING) << "Failed to deserialize candidate from DB: " << result.error();
            state.candidate_in_db = false;  // Mark as not in DB
        }
    } else {
        LOG(WARNING) << "Candidate data missing from DB for " << id;
        state.candidate_in_db = false;
    }
}
```
