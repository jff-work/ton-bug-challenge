"""
Reproduction: Memory leak in CandidateResolver and StateResolver.

Bug: The CandidateResolver's `state_` map and StateResolver's `state_cache_`/
`finalized_blocks_` maps grow unboundedly. Entries are added for every notarized
candidate but never removed after finalization. Over time this causes monotonically
increasing memory consumption that will eventually OOM a validator.

Files affected:
  - validator/consensus/simplex/candidate-resolver.cpp:208 (state_ map)
  - validator/consensus/simplex/state-resolver.cpp:88 (state_cache_)
  - validator/consensus/simplex/state-resolver.cpp:155 (finalized_blocks_)

Reproduction:
  Run a network with simplex consensus for enough slots to observe memory growth.
  Monitor RSS of validator processes over time. Memory should grow monotonically
  even though old finalized blocks are no longer needed.

Requirements: <10^4 slots, <=100 validators.
"""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from tontester.install import Install
from tontester.network import FullNode, Network
from tontester.zerostate import SimplexConsensusConfig


async def get_process_rss_kb(pid: int) -> int:
    """Get RSS memory in KB for a process on macOS/Linux."""
    proc = await asyncio.create_subprocess_exec(
        "ps", "-o", "rss=", "-p", str(pid),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return int(stdout.decode().strip())


async def main():
    repo_root = Path(__file__).resolve().parents[2]
    working_dir = repo_root / "test/integration/.network-memory-test"
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
    TARGET_SEQNO = 200  # Enough blocks to observe growth pattern

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

        # Wait for network to start
        await network.wait_mc_block(seqno=1)
        print(f"Network started with {NUM_VALIDATORS} validators")
        print(f"Targeting mc seqno {TARGET_SEQNO}")

        # Get PIDs of validator processes
        pids = {}
        for node in nodes:
            proc = getattr(node, "_Node__process", None)
            if proc:
                pids[node.name] = proc.pid
                print(f"  {node.name}: PID {proc.pid}")

        # Monitor memory at intervals
        measurements = []
        start_time = time.time()
        check_interval = 10  # seconds between measurements

        print(f"\n{'Time(s)':>8} {'MC Seqno':>10} ", end="")
        for name in pids:
            print(f"{name + ' RSS(MB)':>16}", end="")
        print()
        print("-" * (20 + 16 * len(pids)))

        client = await nodes[0].tonlib_client()

        while True:
            elapsed = time.time() - start_time

            # Get current masterchain seqno
            try:
                mc_info = await client.get_masterchain_info()
                current_seqno = mc_info.last.seqno if mc_info.last else 0
            except Exception:
                current_seqno = -1

            # Measure memory for each node
            rss_values = {}
            for name, pid in pids.items():
                try:
                    rss_kb = await get_process_rss_kb(pid)
                    rss_values[name] = rss_kb
                except Exception:
                    rss_values[name] = 0

            row = {"time": elapsed, "seqno": current_seqno, "rss": rss_values}
            measurements.append(row)

            print(f"{elapsed:>8.0f} {current_seqno:>10} ", end="")
            for name in pids:
                rss_mb = rss_values.get(name, 0) / 1024
                print(f"{rss_mb:>16.1f}", end="")
            print()

            if current_seqno >= TARGET_SEQNO:
                break

            await asyncio.sleep(check_interval)

        # Analysis
        print("\n" + "=" * 60)
        print("MEMORY GROWTH ANALYSIS")
        print("=" * 60)

        if len(measurements) >= 3:
            first = measurements[1]  # skip first (startup noise)
            last = measurements[-1]
            for name in pids:
                rss_start = first["rss"].get(name, 0) / 1024
                rss_end = last["rss"].get(name, 0) / 1024
                growth = rss_end - rss_start
                growth_pct = (growth / rss_start * 100) if rss_start > 0 else 0
                seqno_delta = last["seqno"] - first["seqno"]
                growth_per_block = (growth / seqno_delta * 1024) if seqno_delta > 0 else 0
                print(f"\n  {name}:")
                print(f"    Start RSS: {rss_start:.1f} MB (seqno {first['seqno']})")
                print(f"    End RSS:   {rss_end:.1f} MB (seqno {last['seqno']})")
                print(f"    Growth:    {growth:.1f} MB ({growth_pct:.1f}%)")
                print(f"    Per block: ~{growth_per_block:.1f} KB/block")
                if growth_per_block > 0:
                    est_1h = growth_per_block * 3600 / 0.3 / 1024  # at 300ms/block
                    print(f"    Projected 1 hour: ~{est_1h:.0f} MB")

        print("\nBUG: CandidateResolver.state_ and StateResolver.state_cache_")
        print("grow without bound. After finalization, entries for old blocks")
        print("are never removed. Each entry holds CandidateRef (block data)")
        print("and ChainStateRef (vm::Cell trees).")
        print("\nFIX: Add cleanup in response to FinalizationObserved events.")
        print("Evict entries for finalized blocks from both maps.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 10 * 60))
