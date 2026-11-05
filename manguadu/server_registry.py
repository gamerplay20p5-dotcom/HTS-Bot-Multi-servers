"""Registro de servidores via config/servers.json e variaveis de ambiente."""

from __future__ import annotations

from .i18n import t

import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Mapping

from .utils import clean_text, to_number
from .tenant_scope import current_scope


logger = logging.getLogger(__name__)
DEFAULT_SERVER_ID = "default"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "servers.json"


class ServerRegistry:
    def __init__(self, environment: Mapping[str, str] | None = None, config_path: str | Path | None = None):
        self.environment = environment if environment is not None else os.environ
        self.config_path = Path(config_path or clean_text(self.environment.get("PZ_SERVERS_CONFIG")) or DEFAULT_CONFIG_PATH)
        self._cache: list[dict[str, Any]] | None = None

    def reset(self) -> None:
        self._cache = None

    def _env(self, name: str) -> str:
        return clean_text(self.environment.get(name))

    def _secret(self, key: Any, fallback: str) -> str:
        return self._env(clean_text(key) or fallback)

    def _from_environment(self) -> dict[str, Any]:
        return {
            "id": DEFAULT_SERVER_ID,
            "label": self._env("PZ_SERVER_LABEL") or t('Servidor'),
            "difficulty": self._env("PZ_SERVER_DIFFICULTY"),
            "order": 0,
            "ptero": {"url": self._env("PTERO_URL"), "serverId": self._env("PTERO_SERVER_ID"), "apiKey": self._env("PTERO_API_KEY"), "apiKeyEnv": "PTERO_API_KEY"},
            "rcon": {"host": self._env("RCON_HOST"), "port": to_number(self._env("RCON_PORT")), "password": self._env("RCON_PASSWORD"), "passwordEnv": "RCON_PASSWORD"},
            "paths": {"lua": self._env("PZ_LUA_PATH"), "save": self._env("PZ_SAVE_ROOT")},
            "connectAddress": self._env("SERVER_CONNECT_ADDRESS"),
            "isDefault": True,
        }

    def _normalize(self, entry: Any, index: int) -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return None
        server_id = clean_text(entry.get("id"))
        if not server_id:
            return None
        ptero = entry.get("ptero") if isinstance(entry.get("ptero"), dict) else {}
        rcon = entry.get("rcon") if isinstance(entry.get("rcon"), dict) else {}
        paths = entry.get("paths") if isinstance(entry.get("paths"), dict) else {}
        try:
            order = float(entry.get("order", index))
        except (TypeError, ValueError):
            order = float(index)
        if not math.isfinite(order):
            order = float(index)
        return {
            "id": server_id,
            "label": clean_text(entry.get("label")) or server_id,
            "difficulty": clean_text(entry.get("difficulty")),
            "order": order,
            "ptero": {
                "url": clean_text(ptero.get("url")) or self._env("PTERO_URL"),
                "serverId": clean_text(ptero.get("serverId")),
                "apiKey": self._secret(ptero.get("apiKeyEnv"), "PTERO_API_KEY"),
                "apiKeyEnv": clean_text(ptero.get("apiKeyEnv")) or "PTERO_API_KEY",
            },
            "rcon": {
                # Mantem compatibilidade com instalacoes de servidor unico:
                # campos ausentes no JSON usam o endpoint antigo do .env.
                "host": clean_text(rcon.get("host")) or self._env("RCON_HOST"),
                "port": to_number(rcon.get("port"), to_number(self._env("RCON_PORT"))),
                "password": self._secret(rcon.get("passwordEnv"), "RCON_PASSWORD"),
                "passwordEnv": clean_text(rcon.get("passwordEnv")) or "RCON_PASSWORD",
            },
            "paths": {"lua": clean_text(paths.get("lua")), "save": clean_text(paths.get("save"))},
            "connectAddress": clean_text(entry.get("connectAddress")),
            "isDefault": False,
        }

    def _load(self) -> list[dict[str, Any]]:
        try:
            parsed = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return [self._from_environment()]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logger.warning("Config de servidores invalida (%s): %s", self.config_path, exc)
            return [self._from_environment()]
        raw = parsed if isinstance(parsed, list) else parsed.get("servers") if isinstance(parsed, dict) else None
        if not isinstance(raw, list) or not raw:
            logger.warning("Config de servidores sem entradas: %s", self.config_path)
            return [self._from_environment()]
        servers: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, entry in enumerate(raw):
            server = self._normalize(entry, index)
            if server is None:
                logger.warning("Servidor sem id na posicao %s de %s; ignorado", index, self.config_path)
            elif server["id"] in seen:
                logger.warning("Servidor %s duplicado em %s; ignorado", server["id"], self.config_path)
            else:
                seen.add(server["id"])
                servers.append(server)
        servers.sort(key=lambda server: server["order"])
        return servers or [self._from_environment()]

    def list_servers(self) -> list[dict[str, Any]]:
        scope = current_scope()
        if scope is not None:
            return [scope.server] if scope.server is not None else []
        if self._cache is None:
            self._cache = self._load()
        return list(self._cache)

    def get_server(self, target: str | dict[str, Any] | None = None) -> dict[str, Any] | None:
        servers = self.list_servers()
        if not servers:
            return None
        if not target:
            return servers[0]
        if isinstance(target, dict):
            return target if target.get("id") else servers[0]
        wanted = clean_text(target).lower()
        return next((server for server in servers if server["id"].lower() == wanted), None)

    def get_default_server(self) -> dict[str, Any]:
        servers = self.list_servers()
        if not servers:
            raise RuntimeError(t('Nenhum servidor PZ vinculado a este Discord. Use /config.'))
        return servers[0]

    def has_multiple_servers(self) -> bool:
        return len(self.list_servers()) > 1


def describe_missing_config(server: dict[str, Any] | None) -> list[str]:
    if server is None:
        return [t('servidor desconhecido')]
    ptero = server.get("ptero", {})
    missing = [label for key, label in (("url", "ptero.url"), ("serverId", "ptero.serverId")) if not ptero.get(key)]
    if not ptero.get("apiKey"):
        missing.append(''.join((format(t('chave da API do Pterodactyl ('), ''), format(ptero.get('apiKeyEnv') or 'PTERO_API_KEY', ''), ')')))
    return missing


def describe_missing_rcon(server: dict[str, Any] | None) -> list[str]:
    if server is None:
        return [t('servidor desconhecido')]
    rcon = server.get("rcon", {})
    missing = [label for key, label in (("host", "rcon.host"), ("port", "rcon.port")) if not rcon.get(key)]
    if not rcon.get("password"):
        missing.append(''.join((format(t('senha do RCON ('), ''), format(rcon.get('passwordEnv') or 'RCON_PASSWORD', ''), ')')))
    return missing
