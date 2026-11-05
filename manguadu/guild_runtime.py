"""A server's local files and state, resolved only inside its verified Wings volume."""
from __future__ import annotations

import os
import re
import asyncio
from pathlib import Path, PurePosixPath
from uuid import UUID
from itertools import islice

from .pz_data import PzData
from .tenant_scope import GuildScope

def normalize_binding(identifier: str, volume_uuid: str, label: str, lua_path: str, address: str = "") -> dict:
    volume_uuid = str(UUID(volume_uuid.strip()))
    identifier = identifier.strip().lower()
    if identifier not in {volume_uuid, volume_uuid[:8]}:
        raise ValueError("O identificador Pterodactyl deve corresponder ao UUID informado (UUID completo ou primeiros 8 caracteres).")
    relative = PurePosixPath(lua_path.strip().replace("\\", "/") or ".cache/Lua")
    if relative.is_absolute() or ".." in relative.parts or ":" in str(relative):
        raise ValueError("Informe uma pasta Lua relativa ao volume, como .cache/Lua; caminhos externos nao sao aceitos.")
    return {"identifier": volume_uuid[:8], "uuid": volume_uuid, "label": label.strip()[:100] or "Servidor PZ",
            "lua": str(relative), "connect_address": address.strip()[:150]}

def resolve_volume(binding: dict) -> tuple[Path, Path]:
    root = Path(os.getenv("PTERO_VOLUMES_ROOT", "/var/lib/pterodactyl/volumes")).resolve()
    expected = root / str(UUID(binding["uuid"]))
    volume = expected.resolve()
    lua = (volume / binding["lua"]).resolve()
    if volume != expected or not volume.is_relative_to(root) or not lua.is_relative_to(volume):
        raise ValueError("O volume ou a pasta Lua apontam para fora da raiz Wings autorizada.")
    if not volume.is_dir() or not lua.is_dir():
        raise ValueError("Volume/Pasta Lua indisponivel neste dedicado. Monte o volume Wings na raiz configurada e confira a permissao de leitura do bot.")
    return volume, lua

def build_scope(guild_id: int, binding: dict) -> GuildScope:
    volume, lua = resolve_volume(binding)
    cache = lua.parent
    data = next((path for path in (lua / "FriendHost_Data", lua) if (path / "Jogadores").is_dir()), lua / "FriendHost_Data")
    for path in (data, data / "Jogadores", data / "Servidor", cache / "Logs", cache / "Server", cache / "Saves"):
        if not path.resolve().is_relative_to(volume):
            raise ValueError("Uma pasta do servidor aponta para fora do volume Wings autorizado.")
    settings: dict[str, str] = {}
    config_name = binding.get("pz_config", "")
    ini_files = list(islice((cache / "Server").glob("*.ini"), 50))
    if config_name:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", config_name) or ".." in config_name:
            raise ValueError("Nome do INI invalido.")
        ini_files = [cache / "Server" / (config_name if config_name.endswith(".ini") else config_name + ".ini")]
    if len(ini_files) == 1 and ini_files[0].resolve().is_relative_to(volume):
        if ini_files[0].stat().st_size > 1024 * 1024:
            raise ValueError("INI do servidor excede 1 MiB; revise o arquivo antes de cadastrar.")
        # PZ already stores these credentials. Read only this volume; never copy
        # RCON passwords into Discord settings, messages or registry files.
        with ini_files[0].open(encoding="utf-8-sig", errors="replace") as stream:
            for line in stream:
                key, sep, value = line.strip().partition("=")
                if sep and key in {"RCONPort", "RCONPassword", "MaxPlayers"}:
                    settings[key] = value
    saves = list(islice((cache / "Saves" / "Multiplayer").glob("*"), 50))
    saves = [path for path in saves if path.is_dir() and path.resolve().is_relative_to(volume)]
    state_root = Path(__file__).resolve().parent.parent / "data" / "guilds" / str(guild_id)
    env = {
        "PTERO_SERVER_ID": binding["identifier"], "PTERO_VOLUME_ID": binding["uuid"],
        "PTERO_SERVER_UUID": binding["uuid"], "PTERO_DOCKER_CONTAINER": binding["uuid"],
        "CSV_BASE_PATH": str(data), "PZ_LUA_PATH": str(lua), "PZ_CACHE_ROOT": str(cache),
        "LOGS_PATH": str(cache / "Logs"), "PZ_SAVE_ROOT": str(saves[0]) if len(saves) == 1 else "",
        "ANTICHEAT_CSV_PATH": str(lua / "PZAntiCheat_pending_alerts.csv"),
        "SERVER_CONNECT_ADDRESS": binding.get("connect_address", ""), "SERVER_MAX_SLOTS": settings.get("MaxPlayers", ""),
        "PZ_LOCAL_BACKUP_DIR": str(state_root / "local-wipe-backups"),
        "RCON_HOST": binding.get("rcon_host", "127.0.0.1"),
        "RCON_PORT": binding.get("rcon_port", settings.get("RCONPort", "")),
        "RCON_PASSWORD": settings.get("RCONPassword", ""), "WEBHOOK_CHAT": "",
    }
    if binding.get("legacy") and str(guild_id) == os.getenv("GUILD_ID", ""):
        for key in ("RCON_HOST", "RCON_PORT", "RCON_PASSWORD", "WEBHOOK_CHAT", "SERVER_MAX_SLOTS"):
            if key == "RCON_HOST" and binding.get("rcon_host"):
                continue
            if key == "RCON_PORT" and binding.get("rcon_port"):
                continue
            if key == "RCON_PASSWORD" and config_name:
                continue
            if os.getenv(key):
                env[key] = os.environ[key]
        for key in ("CSV_BASE_PATH", "LOGS_PATH", "PZ_SAVE_ROOT", "PZ_CACHE_ROOT", "ANTICHEAT_CSV_PATH"):
            value = os.getenv(key)
            if value and Path(value).resolve().is_relative_to(volume):
                env[key] = value
    port = env["RCON_PORT"]
    server = {
        "id": "default", "label": binding["label"], "order": 0, "difficulty": "", "isDefault": True,
        "ptero": {"url": os.getenv("PTERO_URL", ""), "serverId": binding["identifier"], "apiKey": os.getenv("PTERO_API_KEY", ""), "apiKeyEnv": "PTERO_API_KEY"},
        "rcon": {"host": env["RCON_HOST"], "port": int(port) if str(port).isdigit() else 0, "password": env["RCON_PASSWORD"], "passwordEnv": "RCONPassword no INI do PZ"},
        "paths": {"lua": str(lua), "save": env["PZ_SAVE_ROOT"]}, "connectAddress": env["SERVER_CONNECT_ADDRESS"],
    }
    return GuildScope(guild_id, server, env)

class GuildRuntime:
    def __init__(self, bot, guild_id: int, binding: dict):
        from .automation_scheduler import AutomationScheduler
        from .anticheat_alerts import AntiCheatMonitor
        from .server_registry import ServerRegistry
        from .watchers import FriendHostWatcher
        from .tenant_scope import use_scope, tenant_path
        self.bot = bot
        self.watcher_lock = asyncio.Lock()
        self.scope = build_scope(guild_id, binding)
        self.scope.runtime = self
        self.binding = binding
        self.online_since: dict[str, float] = {}
        with use_scope(self.scope):
            self.data = PzData(self.scope.environment["CSV_BASE_PATH"], death_state_path=tenant_path("death-counts.json"))
            self.automations = AutomationScheduler(path=tenant_path("automations.json"), server_provider=lambda: self.scope.server)
            self.anticheat = AntiCheatMonitor(state_path=tenant_path("anticheat-alerts-state.json"), registry=ServerRegistry())
            self.watcher = FriendHostWatcher(bot)
