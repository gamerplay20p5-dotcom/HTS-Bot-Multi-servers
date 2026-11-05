"""Diagnóstico e entrada do HTS Bot Multi servers: python -m manguadu."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

from .environment import load_project_environment
from .config_store import ConfigStore, DEFAULT_DB_PATH
from .guild_runtime import build_scope
from .tenant_scope import use_scope, getenv
from .pterodactyl import PterodactylClient, extract_ptero_error
from .pz_data import PzData
from .server_registry import ServerRegistry, describe_missing_config, describe_missing_rcon


async def inspect(probe: bool) -> int:
    servers = []
    bindings = []
    if DEFAULT_DB_PATH.is_file():
        store = ConfigStore(DEFAULT_DB_PATH)
        try:
            bindings = store.list_bindings()
        finally:
            store.close()
    sources = bindings or [(None, None)]
    summaries = []
    for guild_id, binding in sources:
        try:
            scope = build_scope(guild_id, binding) if binding else None
        except (ValueError, OSError) as exc:
            servers.append({"guildId": guild_id, "id": binding["identifier"], "label": binding["label"], "error": str(exc)})
            continue
        with use_scope(scope):
            data = PzData(getenv("CSV_BASE_PATH"))
            summary = {"configured": bool(data.base_path and data.base_path.is_dir()), "players": len(data.player_names()), "onlineFallback": data.online_players(), "world": data.world_snapshot()}
            summaries.append(summary)
            for server in ServerRegistry().list_servers():
                entry = {
                    "guildId": guild_id, "id": server["id"], "label": server["label"],
                    "pterodactylMissing": describe_missing_config(server),
                    "rconMissing": describe_missing_rcon(server), "friendHost": summary,
                }
                if probe and not entry["pterodactylMissing"]:
                    async with PterodactylClient(server) as client:
                        response = await client.resources()
                    entry["pterodactyl"] = {"ok": response["ok"], "state": response.get("state")} if response["ok"] else {"ok": False, "error": extract_ptero_error(response)}
                servers.append(entry)
    print(json.dumps({
        "servers": servers,
        "friendHost": summaries[0] if len(sources) == 1 and summaries else {"mode": "multi", "configuredGuilds": len(summaries)},
    }, ensure_ascii=False, indent=2))
    return 0


async def run_bot(token: str) -> None:
    from .discord_bot import ManguaduBot

    bot = ManguaduBot()
    try:
        await bot.start(token)
    finally:
        await bot.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="HTS Bot Multi servers")
    parser.add_argument("--probe", action="store_true", help="Consulta apenas o estado dos servidores na API do Pterodactyl")
    parser.add_argument("--bot", action="store_true", help="Inicia o bot Discord e seus paineis e tarefas")
    args = parser.parse_args()
    load_project_environment()
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    if args.bot:
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            parser.error("DISCORD_TOKEN ausente. Configure o token na VM para iniciar o bot.")
        try:
            asyncio.run(run_bot(token))
        except KeyboardInterrupt:
            logging.info("Encerramento do bot solicitado.")
        return 0
    return asyncio.run(inspect(args.probe))


if __name__ == "__main__":
    raise SystemExit(main())
