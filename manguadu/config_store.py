"""Configuracao por guild e estado de tickets persistidos em SQLite."""

from __future__ import annotations

from .i18n import t

import json
import os
import sqlite3
import zlib
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .lore import MAX_LORE_CHARS


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "bot-config.sqlite3"


DEFAULT_SETTINGS: dict[str, Any] = {
    "language": "pt-BR",
    "channels": {
        "admin": None, "chat": None, "evolution": None, "stats": None,
        "ranking": None, "anticheat": None, "ticket_panel": None,
        "lore": None, "wl_audit": None, "wl_decisions": None,
    },
    "ticket_category_id": None,
    "admin_role_id": None,
    "ticket_panel_message_id": None,
    "stats_panel_message_id": None,
    "ranking_panel_message_id": None,
    "wl": {
        "enabled": True,
        "require_lore": True,
        "lore_reference": "",
        "lore_min_score": 0.28,
        "review_failed_lore": True,
        "auto_approve": False,
        "server_id": "default",
    },
    "kick_automatico": {
        "enabled": False,
        "voice_channel_id": None,
    },
}


def _merged_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    config = deepcopy(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return config
    config["channels"].update(raw.get("channels") if isinstance(raw.get("channels"), dict) else {})
    config["wl"].update(raw.get("wl") if isinstance(raw.get("wl"), dict) else {})
    config["kick_automatico"].update(raw.get("kick_automatico") if isinstance(raw.get("kick_automatico"), dict) else {})
    if raw.get("language") in {"pt-BR", "en"}:
        config["language"] = raw["language"]
    for key in ("ticket_category_id", "admin_role_id", "ticket_panel_message_id", "stats_panel_message_id", "ranking_panel_message_id"):
        if key in raw:
            config[key] = raw[key]
    return config


class ConfigStore:
    def get_language(self, guild_id: int) -> str:
        with self._lock:
            row = self._db.execute("SELECT payload FROM guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
            language = json.loads(row[0]).get("language", "pt-BR") if row else "pt-BR"
            return language if language in {"pt-BR", "en"} else "pt-BR"

    def set_language(self, guild_id: int, language: str) -> None:
        if language not in {"pt-BR", "en"}:
            raise ValueError("Unsupported language")
        with self._lock:
            config = self.get_settings(guild_id)
            config["language"] = language
            self.save_settings(guild_id, config)

    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._lore_cache: OrderedDict[int, str] = OrderedDict()
        self._request_lore_cache: OrderedDict[int, str] = OrderedDict()
        self._fernet = self._load_secret_cipher()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS server_authorizations (
                guild_id INTEGER NOT NULL,
                server_uuid TEXT NOT NULL UNIQUE,
                identifier TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(guild_id, server_uuid)
            );
            CREATE TABLE IF NOT EXISTS binding_audit (
                id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                server_uuid TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS server_bindings (
                guild_id INTEGER PRIMARY KEY,
                server_uuid TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_bindings (
                guild_id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS guild_lore (
                guild_id INTEGER PRIMARY KEY,
                content_z BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gemini_credentials (
                guild_id INTEGER PRIMARY KEY,
                api_key_ciphertext TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_ticket_per_user
                ON tickets(guild_id, user_id) WHERE status = 'open';
            CREATE TABLE IF NOT EXISTS wl_requests (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                character_name TEXT NOT NULL,
                lore TEXT NOT NULL,
                lore_compressed BLOB,
                password_ciphertext TEXT NOT NULL DEFAULT '',
                score REAL NOT NULL,
                status TEXT NOT NULL,
                triage_json TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS whitelist (
                guild_id INTEGER NOT NULL,
                username_key TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(guild_id, username_key)
            );
        """)
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(wl_requests)")}
        if "password_ciphertext" not in columns:
            self._db.execute("ALTER TABLE wl_requests ADD COLUMN password_ciphertext TEXT NOT NULL DEFAULT ''")
        if "lore_compressed" not in columns:
            self._db.execute("ALTER TABLE wl_requests ADD COLUMN lore_compressed BLOB")
        if "triage_json" not in columns:
            self._db.execute("ALTER TABLE wl_requests ADD COLUMN triage_json TEXT NOT NULL DEFAULT ''")
        self._migrate_legacy_lore()
        self._db.commit()

    def get_binding(self, guild_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT payload FROM server_bindings WHERE guild_id = ?", (guild_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def binding_owner(self, server_uuid: str) -> int | None:
        with self._lock:
            row = self._db.execute("SELECT guild_id FROM server_bindings WHERE server_uuid=?", (server_uuid,)).fetchone()
            return int(row[0]) if row else None

    def authorize_server(self, guild_id: int, server: dict, actor_id: int):
        """Caller must verify the application owner; no guild-admin self-grants."""
        with self._lock, self._db:
            owner = self.binding_owner(server["uuid"])
            if owner is not None and owner != guild_id:
                raise ValueError("Server already bound to another guild")
            self._db.execute("INSERT INTO server_authorizations(guild_id,server_uuid,identifier,actor_id) VALUES (?,?,?,?) "
                             "ON CONFLICT(guild_id,server_uuid) DO UPDATE SET identifier=excluded.identifier, actor_id=excluded.actor_id",
                             (guild_id, server["uuid"], server["identifier"], actor_id))
            self.audit_binding(guild_id, server["uuid"], actor_id, "authorized")

    def server_authorizations(self, guild_id: int):
        with self._lock:
            return [dict(row) for row in self._db.execute("SELECT * FROM server_authorizations WHERE guild_id=?", (guild_id,))]

    def revoke_server_authorization(self, guild_id: int, server_uuid: str, actor_id: int):
        with self._lock, self._db:
            self._db.execute("DELETE FROM server_authorizations WHERE guild_id=? AND server_uuid=?", (guild_id, server_uuid))
            self.audit_binding(guild_id, server_uuid, actor_id, "authorization_revoked")

    def audit_binding(self, guild_id: int, server_uuid: str, actor_id: int, event: str):
        with self._lock, self._db:
            self._db.execute("INSERT INTO binding_audit(guild_id,server_uuid,actor_id,event) VALUES (?,?,?,?)", (guild_id, server_uuid, actor_id, event))

    def list_bindings(self) -> list[tuple[int, dict[str, Any]]]:
        with self._lock:
            return [(int(row[0]), json.loads(row[1])) for row in self._db.execute("SELECT guild_id, payload FROM server_bindings ORDER BY guild_id")]

    def save_binding(self, guild_id: int, binding: dict[str, Any]) -> None:
        with self._lock, self._db:
            grant = self._db.execute("SELECT guild_id FROM server_authorizations WHERE server_uuid=?", (binding["uuid"],)).fetchone()
            if grant is not None and int(grant[0]) != guild_id:
                raise sqlite3.IntegrityError("Server authorization belongs to another guild")
            self._db.execute(
                "INSERT INTO server_bindings(guild_id, server_uuid, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET server_uuid=excluded.server_uuid, payload=excluded.payload",
                (guild_id, binding["uuid"], json.dumps(binding, ensure_ascii=False)),
            )
            self._db.execute("DELETE FROM pending_bindings WHERE guild_id = ?", (guild_id,))

    def pending_binding(self, guild_id: int, value: dict[str, Any] | None = None) -> dict[str, Any] | None:
        with self._lock, self._db:
            if value is not None:
                self._db.execute("INSERT OR REPLACE INTO pending_bindings VALUES (?, ?)", (guild_id, json.dumps(value)))
            row = self._db.execute("SELECT payload FROM pending_bindings WHERE guild_id = ?", (guild_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def _load_secret_cipher(self) -> Fernet:
        if str(self.path) == ":memory:":
            return Fernet(Fernet.generate_key())
        key_path = self.path.with_name(self.path.name + ".key")
        try:
            key = key_path.read_bytes().strip()
        except FileNotFoundError:
            key = Fernet.generate_key()
            try:
                descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                key = key_path.read_bytes().strip()
            else:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(key)
        try:
            cipher = Fernet(key)
        except ValueError as exc:
            raise RuntimeError(''.join((format(t('Chave local invalida em '), ''), format(key_path, ''), format(t('; restaure o backup junto com o banco SQLite.'), '')))) from exc
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return cipher

    def _migrate_legacy_lore(self) -> None:
        for row in self._db.execute("SELECT guild_id, payload FROM guild_settings").fetchall():
            try:
                payload = json.loads(row["payload"])
                reference = payload.get("wl", {}).get("lore_reference", "")
            except (AttributeError, json.JSONDecodeError):
                continue
            if not isinstance(reference, str) or not reference:
                continue
            self._db.execute(
                "INSERT OR IGNORE INTO guild_lore(guild_id, content_z) VALUES (?, ?)",
                (row["guild_id"], zlib.compress(reference.encode("utf-8"), level=6)),
            )
            payload.setdefault("wl", {})["lore_reference"] = ""
            self._db.execute("UPDATE guild_settings SET payload = ? WHERE guild_id = ?", (json.dumps(payload, ensure_ascii=False), row["guild_id"]))
        for row in self._db.execute("SELECT channel_id, lore FROM wl_requests WHERE lore_compressed IS NULL AND lore != ''").fetchall():
            encoded = row["lore"].encode("utf-8")
            if len(encoded) > 4096:
                self._db.execute(
                    "UPDATE wl_requests SET lore = '', lore_compressed = ? WHERE channel_id = ?",
                    (zlib.compress(encoded, level=6), row["channel_id"]),
                )

    def close(self) -> None:
        self._db.close()

    def get_settings(self, guild_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT payload FROM guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
            config = _merged_settings(json.loads(row["payload"]) if row else None)
            config["wl"]["lore_reference"] = self._get_guild_lore(guild_id)
            return config

    def _get_guild_lore(self, guild_id: int) -> str:
        if guild_id in self._lore_cache:
            self._lore_cache.move_to_end(guild_id)
            return self._lore_cache[guild_id]
        row = self._db.execute("SELECT content_z FROM guild_lore WHERE guild_id = ?", (guild_id,)).fetchone()
        try:
            reference = zlib.decompress(row["content_z"]).decode("utf-8") if row else ""
        except (zlib.error, UnicodeDecodeError):
            reference = ""
        self._remember_lore(self._lore_cache, guild_id, reference)
        return reference

    @staticmethod
    def _remember_lore(cache: OrderedDict[int, str], key: int, value: str) -> None:
        """Keep decoded lore in a small LRU cache bounded by characters."""
        cache.pop(key, None)
        cache[key] = value
        total_chars = sum(map(len, cache.values()))
        while (total_chars > MAX_LORE_CHARS or len(cache) > 8) and len(cache) > 1:
            _old_key, old_value = cache.popitem(last=False)
            total_chars -= len(old_value)

    def _save_guild_lore(self, guild_id: int, reference: str) -> None:
        if len(reference) > MAX_LORE_CHARS:
            raise ValueError(''.join((format(t('A lore base pode ter no maximo '), ''), format(MAX_LORE_CHARS, ''.join((',',))), format(t(' caracteres.'), ''))))
        current = self._get_guild_lore(guild_id)
        if current == reference:
            return
        if reference:
            content_z = zlib.compress(reference.encode("utf-8"), level=6)
            self._db.execute(
                "INSERT INTO guild_lore(guild_id, content_z) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET content_z=excluded.content_z",
                (guild_id, content_z),
            )
        else:
            self._db.execute("DELETE FROM guild_lore WHERE guild_id = ?", (guild_id,))
        self._remember_lore(self._lore_cache, guild_id, reference)

    def save_settings(self, guild_id: int, config: dict[str, Any]) -> dict[str, Any]:
        normalized = _merged_settings(config)
        with self._lock, self._db:
            self._save_guild_lore(guild_id, normalized["wl"]["lore_reference"])
            stored = deepcopy(normalized)
            stored["wl"]["lore_reference"] = ""
            self._db.execute("INSERT INTO guild_settings(guild_id, payload) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET payload=excluded.payload", (guild_id, json.dumps(stored, ensure_ascii=False)))
        return normalized

    def set_value(self, guild_id: int, section: str, key: str, value: Any) -> dict[str, Any]:
        if section not in ("channels", "wl", "kick_automatico") or key not in DEFAULT_SETTINGS[section]:
            raise ValueError(t('Campo de configuracao desconhecido'))
        with self._lock:
            config = self.get_settings(guild_id)
            config[section][key] = value
            return self.save_settings(guild_id, config)

    def has_gemini_api_key(self, guild_id: int) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM gemini_credentials WHERE guild_id = ?", (guild_id,)).fetchone() is not None

    def get_gemini_api_key(self, guild_id: int) -> str:
        with self._lock:
            row = self._db.execute("SELECT api_key_ciphertext FROM gemini_credentials WHERE guild_id = ?", (guild_id,)).fetchone()
        if not row:
            return ""
        try:
            return self._fernet.decrypt(row["api_key_ciphertext"].encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise ValueError(t('A chave Gemini armazenada nao pode ser descriptografada; restaure o arquivo .key do bot-config.')) from exc

    def set_gemini_api_key(self, guild_id: int, api_key: str) -> None:
        cleaned = api_key.strip()
        if not cleaned or len(cleaned) > 256:
            raise ValueError(t('A chave Gemini esta vazia ou excede o tamanho permitido.'))
        ciphertext = self._fernet.encrypt(cleaned.encode("utf-8")).decode("ascii")
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO gemini_credentials(guild_id, api_key_ciphertext) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET api_key_ciphertext=excluded.api_key_ciphertext",
                (guild_id, ciphertext),
            )

    def clear_gemini_api_key(self, guild_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM gemini_credentials WHERE guild_id = ?", (guild_id,))

    def set_top_value(self, guild_id: int, key: str, value: Any) -> dict[str, Any]:
        if key not in ("ticket_category_id", "admin_role_id", "ticket_panel_message_id", "stats_panel_message_id", "ranking_panel_message_id"):
            raise ValueError(t('Campo de configuracao desconhecido'))
        with self._lock:
            config = self.get_settings(guild_id)
            config[key] = value
            return self.save_settings(guild_id, config)

    def get_open_ticket(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tickets WHERE guild_id = ? AND user_id = ? AND status = 'open'", (guild_id, user_id)).fetchone()
            return dict(row) if row else None

    def get_ticket(self, channel_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tickets WHERE channel_id = ?", (channel_id,)).fetchone()
            return dict(row) if row else None

    def create_ticket(self, guild_id: int, user_id: int, channel_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO tickets(channel_id, guild_id, user_id) VALUES (?, ?, ?)", (channel_id, guild_id, user_id))

    def close_ticket(self, channel_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE tickets SET status = 'closed' WHERE channel_id = ?", (channel_id,))

    def get_request(self, channel_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM wl_requests WHERE channel_id = ?", (channel_id,)).fetchone()
            if not row:
                return None
            request = dict(row)
            compressed = request.pop("lore_compressed", None)
            if compressed:
                if channel_id in self._request_lore_cache:
                    self._request_lore_cache.move_to_end(channel_id)
                else:
                    try:
                        lore = zlib.decompress(compressed).decode("utf-8")
                    except (zlib.error, UnicodeDecodeError):
                        lore = ""
                    self._remember_lore(self._request_lore_cache, channel_id, lore)
                request["lore"] = self._request_lore_cache[channel_id]
            else:
                self._remember_lore(self._request_lore_cache, channel_id, request.get("lore", ""))
            try:
                request["triage"] = json.loads(request.pop("triage_json", "") or "{}")
            except json.JSONDecodeError:
                request["triage"] = {}
            return request

    def get_latest_request_by_username(self, guild_id: int, username: str) -> dict[str, Any] | None:
        wanted = username.strip().casefold()
        if not wanted:
            return None
        with self._lock:
            rows = self._db.execute(
                "SELECT channel_id, username FROM wl_requests WHERE guild_id = ? ORDER BY updated_at DESC, rowid DESC",
                (guild_id,),
            ).fetchall()
            channel_id = next(
                (int(row["channel_id"]) for row in rows if str(row["username"]).strip().casefold() == wanted),
                None,
            )
        return self.get_request(channel_id) if channel_id is not None else None

    def save_request(self, channel_id: int, guild_id: int, user_id: int, username: str, character_name: str, lore: str, score: float, status: str, password_ciphertext: str = "", triage: dict[str, Any] | None = None) -> None:
        if len(lore) > MAX_LORE_CHARS:
            raise ValueError(''.join((format(t('A lore de personagem pode ter no maximo '), ''), format(MAX_LORE_CHARS, ''.join((',',))), format(t(' caracteres.'), ''))))
        encoded = lore.encode("utf-8")
        compressed = zlib.compress(encoded, level=6) if len(encoded) > 4096 else None
        stored_lore = "" if compressed is not None else lore
        with self._lock, self._db:
            self._db.execute("""INSERT INTO wl_requests(channel_id, guild_id, user_id, username, character_name, lore, lore_compressed, password_ciphertext, score, status, triage_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET username=excluded.username,
                    character_name=excluded.character_name, lore=excluded.lore,
                    lore_compressed=excluded.lore_compressed,
                    password_ciphertext=excluded.password_ciphertext,
                    score=excluded.score, status=excluded.status,
                    triage_json=excluded.triage_json,
                    updated_at=CURRENT_TIMESTAMP""",
                (channel_id, guild_id, user_id, username, character_name, stored_lore, compressed, password_ciphertext, score, status, json.dumps(triage or {}, ensure_ascii=False)))
            self._remember_lore(self._request_lore_cache, channel_id, lore)

    def set_request_status(self, channel_id: int, status: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE wl_requests SET status = ?, updated_at=CURRENT_TIMESTAMP WHERE channel_id = ?", (status, channel_id))

    def clear_request_password(self, channel_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE wl_requests SET password_ciphertext = '' WHERE channel_id = ?", (channel_id,))

    def username_owner(self, guild_id: int, username: str) -> int | None:
        with self._lock:
            row = self._db.execute("SELECT user_id FROM whitelist WHERE guild_id = ? AND username_key = ?", (guild_id, username.casefold())).fetchone()
            return int(row["user_id"]) if row else None

    def record_whitelist(self, guild_id: int, username: str, user_id: int, channel_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO whitelist(guild_id, username_key, user_id, channel_id) VALUES (?, ?, ?, ?)", (guild_id, username.casefold(), user_id, channel_id))
