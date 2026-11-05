"""Synthetic TTF/Wings load. Temporary files only; no network or production access.

python -m scripts.benchmark_ttf --servers 25 --accounts 100 --characters 2
"""
import argparse
import asyncio
import json
import os
import platform
import tempfile
import time
import tracemalloc
from pathlib import Path
from uuid import uuid4

from manguadu.pz_data import PzData
from manguadu.panel_reports import build_ranking_panel
from manguadu.tenant_scope import GuildScope, use_scope


async def benchmark(servers, accounts, characters):
    previous = os.environ.get("PTERO_VOLUMES_ROOT")
    with tempfile.TemporaryDirectory(prefix="hts-ttf-benchmark-") as temp:
        os.environ["PTERO_VOLUMES_ROOT"] = temp
        try:
            providers = []
            for server in range(servers):
                uuid = str(uuid4())
                root = Path(temp) / uuid / ".cache/Lua/ttf_stats_mp"
                root.mkdir(parents=True)
                index = []
                for account in range(accounts):
                    for character in range(characters):
                        cid = f"character_{account}_{character}"
                        nick = f"Player {account}"
                        index.append(f"{cid}|{nick}|Survivor|{character}")
                        (root / (cid + ".txt")).write_text(f"version=1\ndeleted=false\ncharacterID={cid}\nusername={nick}\nforename=Survivor\nsurname={character}\nisDead=false\nlastSeenKills={server}\ngameTimeSeconds=3600\ndistance=100\nvehicleKills=0\nbrokenWeaponsTotal=0\n", encoding="utf-8")
                (root / "index.txt").write_text("\n".join(index) + "\n", encoding="utf-8")
                scope = GuildScope(server+1, server={}, environment={"PTERO_VOLUME_ID": uuid})
                providers.append((scope, PzData(root)))
            slots = asyncio.Semaphore(3)
            async def scan(index, item, force):
                async with slots:
                    scope, data = item
                    with use_scope(scope):
                        if force:
                            await asyncio.to_thread(data.ttf_snapshot, force=True)
                        rows = await asyncio.to_thread(data.player_rows)
                        assert len(rows) == accounts
                        assert float(rows[0]["row"]["zombiekills"]) == index * characters
                        embeds, files = await asyncio.to_thread(build_ranking_panel, rows, [])
                        assert embeds
                        for file in files:
                            file.close()
            tracemalloc.start()
            started = time.perf_counter()
            await asyncio.gather(*(scan(i, item, False) for i, item in enumerate(providers)))
            first = time.perf_counter() - started
            started = time.perf_counter()
            # Recheck file metadata, not merely the five-second snapshot cache.
            await asyncio.gather(*(scan(i, item, True) for i, item in enumerate(providers)))
            cached = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return {"servers": servers, "accounts_per_server": accounts, "characters_per_account": characters,
                    "concurrent_scans": 3, "first_scan_seconds": round(first, 3), "metadata_cached_scan_seconds": round(cached, 3),
                    "python_heap_peak_mib": round(peak/1024**2, 2), "scope": f"Synthetic {platform.system()} local TTF reads and Discord embed construction; excludes network, total process RSS and production load."}
        finally:
            if previous is None:
                os.environ.pop("PTERO_VOLUMES_ROOT", None)
            else:
                os.environ["PTERO_VOLUMES_ROOT"] = previous


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--servers", type=int, default=25)
    parser.add_argument("--accounts", type=int, default=100)
    parser.add_argument("--characters", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.servers <= 100 or not 1 <= args.accounts <= 1000 or not 1 <= args.characters <= 10:
        parser.error("servers 1-100, accounts 1-1000, characters 1-10")
    print(json.dumps(asyncio.run(benchmark(args.servers, args.accounts, args.characters)), indent=2))


if __name__ == "__main__":
    main()
