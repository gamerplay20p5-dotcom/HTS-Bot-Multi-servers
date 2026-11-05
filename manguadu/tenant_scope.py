"""Task-local server configuration. Never mutate os.environ for a Discord guild."""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
SERVER_KEYS = frozenset({
    "STATS_PROVIDER",
    "CSV_BASE_PATH", "TXT_BASE_PATH", "LOGS_PATH", "PZ_LUA_PATH", "PZ_CACHE_ROOT",
    "PZ_SAVE_ROOT", "PZ_PATH_SCAN_ROOTS", "PTERO_SERVER_ID", "PTERO_VOLUME_ID",
    "PTERO_SERVER_UUID", "PTERO_DOCKER_CONTAINER", "RCON_HOST", "RCON_PORT", "RCON_PASSWORD",
    "ANTICHEAT_CSV_PATH", "ANTICHEAT_PTERO_CSV_PATH", "ANTICHEAT_ALERT_CHANNEL_ID",
    "ID_CANAL_ADMIN", "ID_CANAL_CHAT", "ID_CANAL_EVOLUCAO", "ID_CANAL_STATS", "ID_CANAL_RANKING",
    "WEBHOOK_CHAT", "SERVER_CONNECT_ADDRESS", "SERVER_MAX_SLOTS", "PZ_LOCAL_BACKUP_DIR",
})

@dataclass
class GuildScope:
    guild_id: int
    server: dict[str, Any] | None = None
    environment: dict[str, str] = field(default_factory=dict)
    runtime: Any = None
    language: str = "pt-BR"

_CURRENT: ContextVar[GuildScope | None] = ContextVar("friendhost_guild", default=None)

def current_scope() -> GuildScope | None:
    return _CURRENT.get()

def activate(scope: GuildScope | None) -> None:
    _CURRENT.set(scope)

@contextmanager
def use_scope(scope: GuildScope | None):
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)

def getenv(key: str, default: Any = None) -> Any:
    scope = current_scope()
    if scope is not None:
        if key in scope.environment:
            return scope.environment[key]
        if key in SERVER_KEYS:
            return default
    return os.getenv(key, default)

def tenant_path(name: str, legacy: Path | None = None) -> Path:
    scope = current_scope()
    if scope is not None:
        return DATA_ROOT / "guilds" / str(scope.guild_id) / name
    return legacy if legacy is not None else DATA_ROOT / name

def scoped_guilds(bot) -> list:
    scope = current_scope()
    return [guild for guild in bot.guilds if scope is None or guild.id == scope.guild_id]

def allowed_path(path: str | Path) -> bool:
    scope = current_scope()
    if scope is None:
        return True
    if scope.server is None:
        return False
    if not scope.environment.get("PTERO_VOLUME_ID"):
        return False
    root = Path(os.getenv("PTERO_VOLUMES_ROOT", "/var/lib/pterodactyl/volumes")).resolve()
    volume = (root / scope.environment["PTERO_VOLUME_ID"]).resolve()
    resolved = Path(path).resolve()
    return volume == root / scope.environment["PTERO_VOLUME_ID"] and volume.is_relative_to(root) and resolved.is_relative_to(volume)
