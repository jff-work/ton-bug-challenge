"""
Reproduction: Premature skip votes on validator restart.

Bug: When a validator restarts, ConsensusImpl's `process_notarization_observed`
fires during bootstrap with `timeout_base_` still at its default value
(Timestamp{0} = epoch). This causes `alarm_timestamp()` to be set to a time
far in the past, making `alarm()` fire immediately. The alarm sends skip votes
for slots in the CURRENT or NEXT window -- slots where the validator should be
actively participating in consensus.

Additionally, ConsensusImpl::start_up() unconditionally sends skip votes for
ALL non-finalized slots in the previous window (line 82-92), even for slots
where the validator previously voted notar. This permanently prevents the
restarted validator from voting final for those slots, reducing finalization
weight.

Combined effect: After restart, a validator:
1. Votes skip for its previous window (even for notar'd slots)
2. Prematurely votes skip for the current window (due to alarm bug)
This reduces the validator's contribution to consensus, and if enough
validators restart simultaneously, could impact liveness.

Files affected:
  - validator/consensus/simplex/consensus.cpp:82-92 (startup skip votes)
  - validator/consensus/simplex/consensus.cpp:239-260 (premature alarm)
  - validator/consensus/simplex/consensus.cpp:279 (uninitialized timeout_base_)

Reproduction:
  Start a network, wait for blocks, kill a validator, restart it, and
  observe premature skip votes in logs.

Requirements: <10^4 slots, <=100 validators.
"""

import asyncio
import logging
import os
import re
import shutil
import signal
import time
from pathlib import Path

from tontester.install import Install
from tontester.network import FullNode, Network


async def main():
    repo_root = Path(__file__).resolve().parents[2]
    working_dir = repo_root / "test/integration/.network-restart-test"
    shutil.rmtree(working_dir, ignore_errors=True)
    working_dir.mkdir(exist_ok=True)

    install = Install(repo_root / "build", repo_root)
    install.tonlibjson.client_set_verbosity_level(1)

    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s][%(asctime)s][%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H-%M-%S",
    )

    NUM_VALIDATORS = 2
    TARGET_SEQNO_BEFORE_KILL = 10  # Wait for some blocks before killing
    TARGET_SEQNO_AFTER_RESTART = 30  # Run until here after restart

    async with Network(install, working_dir) as network:
        dht = network.create_dht_node()
        nodes: list[FullNode] = []
        for _ in range(NUM_VALIDATORS):
            node = network.create_full_node()
            node.make_initial_validator()
            node.announce_to(dht)
            nodes.append(node)

        async with asyncio.TaskGroup() as start_group:
            _ = start_group.create_task(dht.run())
            for node in nodes:
                _ = start_group.create_task(node.run())

        # Phase 1: Wait for network to produce some blocks
        print(f"Phase 1: Waiting for mc seqno {TARGET_SEQNO_BEFORE_KILL}...")
        await network.wait_mc_block(seqno=TARGET_SEQNO_BEFORE_KILL)
        print(f"  Reached seqno {TARGET_SEQNO_BEFORE_KILL}")

        # Record the log position before kill
        victim = nodes[1]
        log_path = victim.log_path
        log_size_before_kill = log_path.stat().st_size if log_path.exists() else 0

        # Phase 2: Kill the victim validator (SIGKILL for hard crash)
        victim_proc = getattr(victim, "_Node__process", None)
        if victim_proc and victim_proc.returncode is None:
            print(f"\nPhase 2: Killing {victim.name} (PID {victim_proc.pid}) with SIGKILL...")
            victim_proc.kill()
            await victim_proc.wait()
            print(f"  {victim.name} killed")

        # Small delay to let the network notice the dead validator
        await asyncio.sleep(2)

        # Phase 3: Restart the victim
        print(f"\nPhase 3: Restarting {victim.name}...")
        await victim.run()
        print(f"  {victim.name} restarted")

        # Phase 4: Wait for network to produce more blocks
        print(f"\nPhase 4: Waiting for mc seqno {TARGET_SEQNO_AFTER_RESTART}...")
        client = await nodes[0].tonlib_client()

        for _ in range(60):  # 60 second timeout
            try:
                mc_info = await client.get_masterchain_info()
                if mc_info.last and mc_info.last.seqno >= TARGET_SEQNO_AFTER_RESTART:
                    print(f"  Reached seqno {mc_info.last.seqno}")
                    break
            except Exception:
                pass
            await asyncio.sleep(1)

        # Phase 5: Analyze logs for premature skip votes
        print("\n" + "=" * 60)
        print("LOG ANALYSIS")
        print("=" * 60)

        if log_path.exists():
            log_size_after = log_path.stat().st_size
            with open(log_path, "r", errors="replace") as f:
                # Read only the post-restart portion
                f.seek(log_size_before_kill)
                restart_logs = f.read()

            # Look for skip vote indicators after restart
            skip_vote_lines = []
            standstill_lines = []
            notar_lines = []
            for line in restart_logs.split("\n"):
                if "SkipVote" in line or "voted_skip" in line:
                    skip_vote_lines.append(line.strip()[:200])
                if "Standstill" in line:
                    standstill_lines.append(line.strip()[:200])
                if "NotarizeVote" in line or "Obtained certificate" in line:
                    notar_lines.append(line.strip()[:200])

            print(f"\nPost-restart logs ({log_size_after - log_size_before_kill} bytes):")
            print(f"  Skip vote events: {len(skip_vote_lines)}")
            if skip_vote_lines:
                print("  First 10 skip vote lines:")
                for line in skip_vote_lines[:10]:
                    print(f"    {line}")

            print(f"\n  Notar/cert events: {len(notar_lines)}")
            if notar_lines:
                print("  First 5 notar lines:")
                for line in notar_lines[:5]:
                    print(f"    {line}")

            print(f"\n  Standstill events: {len(standstill_lines)}")
            if standstill_lines:
                print("  First 3:")
                for line in standstill_lines[:3]:
                    print(f"    {line}")

        print("\n" + "=" * 60)
        print("EXPLANATION")
        print("=" * 60)
        print("""
On restart, ConsensusImpl::start_up() sends skip votes for ALL
non-finalized slots in the previous leader window (lines 82-92),
regardless of whether the validator already voted notar for those slots.

Additionally, NotarizationObserved events from Pool's bootstrap cause
process_notarization_observed to fire with timeout_base_ = Timestamp{0}
(default value, never set because LeaderWindowObserved hasn't fired yet).
This causes Timestamp::in(value, Timestamp{0}) to produce a timestamp
near epoch (at_ ≈ value), which is far in the past relative to the
monotonic clock. The alarm fires immediately, sending premature skip
votes for the current/next window.

Impact:
  - Restarted validator permanently loses ability to vote Final for
    previously-notarized-but-unfinalized slots in the previous window
  - Premature skip votes for the current window reduce the validator's
    consensus contribution
  - With simultaneous restart of >1/3 validators: potential liveness
    failure for slot finalization

Fix: Check voted_notar before sending skip in start_up. Initialize
timeout_base_ = Timestamp::now() in constructor or guard
process_notarization_observed with a started flag.
""")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 5 * 60))
