"""Synthetic local load; no Discord/Pterodactyl traffic and no production files touched.

Run: python -m scripts.benchmark_multiserver --servers 25 --players 200 --history 200
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from manguadu.guild_runtime import GuildRuntime, normalize_binding
from manguadu.panel_reports import build_ranking_panel
from manguadu.tenant_scope import use_scope

async def benchmark(servers: int, players: int, history: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="friendhost-bench-") as temporary:
        root = Path(temporary)
        old_root = os.environ.get("PTERO_VOLUMES_ROOT")
        os.environ["PTERO_VOLUMES_ROOT"] = str(root)
        try:
            runtimes = []
            for index in range(servers):
                uuid = str(uuid4())
                data = root / uuid / ".cache" / "Lua" / "FriendHost_Data"
                for player in range(players):
                    folder = data / "Jogadores" / f"Player{player}"
                    folder.mkdir(parents=True)
                    # Same player names on every server, with different scores.
                    rows = f"Player{player};{index + player};12\n" * history
                    (folder / f"player_Player{player}.txt").write_text("username;zombiekills;hourssurvived\n" + rows, encoding="utf-8")
                binding = normalize_binding(uuid[:8], uuid, f"PZ {index}", ".cache/Lua")
                runtime = GuildRuntime(SimpleNamespace(), 900000000 + index, binding)
                # Benchmark caches belong to this temporary directory too.
                runtime.data._death_state_path = root / uuid / "death-counts.json"
                runtimes.append(runtime)
            semaphore = asyncio.Semaphore(3)
            tracemalloc.start()
            started = time.perf_counter()

            async def scan(index, runtime):
                async with semaphore:
                    with use_scope(runtime.scope):
                        rows = await asyncio.to_thread(runtime.data.player_rows)
                        assert len(rows) == players
                        assert rows[0]["row"]["zombiekills"] == str(index)
                        embeds, files = await asyncio.to_thread(build_ranking_panel, rows, [], {})
                        assert embeds
                        for file in files:
                            file.close()

            await asyncio.gather(*(scan(index, runtime) for index, runtime in enumerate(runtimes)))
            first_seconds = time.perf_counter() - started
            second_start = time.perf_counter()
            await asyncio.gather(*(scan(index, runtime) for index, runtime in enumerate(runtimes)))
            cached_seconds = time.perf_counter() - second_start
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return {"servers": servers, "players_per_server": players, "history_rows_per_player": history,
                    "concurrent_scans": 3, "first_scan_seconds": round(first_seconds, 3),
                    "cached_scan_seconds": round(cached_seconds, 3), "python_heap_peak_mib": round(peak / 1024**2, 2),
                    "scope": "Synthetic local reads/ranking rendering only; excludes production API/network and total process RSS."}
        finally:
            if old_root is None:
                os.environ.pop("PTERO_VOLUMES_ROOT", None)
            else:
                os.environ["PTERO_VOLUMES_ROOT"] = old_root

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--servers", type=int, default=25)
    parser.add_argument("--players", type=int, default=200)
    parser.add_argument("--history", type=int, default=200)
    args = parser.parse_args()
    if not 1 <= args.servers <= 200 or not 1 <= args.players <= 5000 or not 1 <= args.history <= 10000:
        parser.error("Use 1-200 servidores, 1-5000 jogadores e 1-10000 linhas de historico.")
    print(json.dumps(asyncio.run(benchmark(args.servers, args.players, args.history)), indent=2))

if __name__ == "__main__":
    main()
