"""Coleta alertas pendentes do PZAntiCheat e os encaminha ao Discord."""

from __future__ import annotations

from .tenant_scope import getenv, scoped_guilds, current_scope, allowed_path

import csv
import asyncio
import io
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from .pterodactyl import PterodactylClient, extract_ptero_error
from .utils import clean_text, to_number


logger = logging.getLogger(__name__)
DEFAULT_PENDING_FILE = Path.home() / "Zomboid" / "Lua" / "PZAntiCheat_pending_alerts.csv"
DEFAULT_HEADER = "timestamp,username,steam_id,cheat,count,detail,pos\n"
STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "anticheat-alerts-state.json"
DEFAULT_TIMEZONE = "America/Sao_Paulo"


def _env_bool(name: str, default: bool) -> bool:
    value = clean_text(getenv(name, "1" if default else "0")).casefold()
    return value not in {"0", "false", "nao", "não", "no", "off"}


def get_pending_csv_path() -> Path:
    return Path(getenv("ANTICHEAT_CSV_PATH", "").strip() or DEFAULT_PENDING_FILE)


def get_pending_ptero_path() -> str:
    return clean_text(getenv("ANTICHEAT_PTERO_CSV_PATH"))


def get_alert_timezone() -> str:
    return clean_text(getenv("ANTICHEAT_TIMEZONE")) or clean_text(getenv("AUTOMATION_TIMEZONE")) or clean_text(getenv("TZ")) or DEFAULT_TIMEZONE


def _zone() -> ZoneInfo | timezone:
    try: return ZoneInfo(get_alert_timezone())
    except ZoneInfoNotFoundError: return timezone.utc


def parse_alerts(content: str) -> list[dict[str, str]]:
    if not clean_text(content): return []
    try:
        rows = list(csv.reader(io.StringIO(content.lstrip("\ufeff")), delimiter=","))
    except csv.Error:
        logger.exception("Falha ao interpretar CSV de alertas PZAntiCheat")
        return []
    if len(rows) <= 1: return []
    headers = [re.sub(r"[^a-z0-9]", "", cell.casefold()) for cell in rows[0]]
    alerts = []
    for row in rows[1:]:
        record = {header: clean_text(row[index]) if index < len(row) else "" for index, header in enumerate(headers) if header}
        alert = {
            "timestamp": clean_text(record.get("timestamp")), "username": clean_text(record.get("username")),
            "steamId": clean_text(record.get("steamid") or record.get("steam")),
            "cheat": clean_text(record.get("cheat") or record.get("type")),
            "count": clean_text(record.get("count")), "detail": clean_text(record.get("detail")),
            "pos": clean_text(record.get("pos") or record.get("position")),
        }
        if any(alert.values()): alerts.append(alert)
    return alerts


def alert_key(alert: dict) -> str:
    return "|".join(clean_text(alert.get(key)) for key in ("timestamp", "username", "steamId", "cheat", "count", "detail", "pos"))


def group_alert_key(alert: dict) -> str:
    player = clean_text(alert.get("steamId") or alert.get("username") or "desconhecido").casefold()
    cheat = clean_text(alert.get("cheat") or "alerta").casefold()
    return f"{player}|{cheat}"


def parse_alert_timestamp(value: str, time_zone: str | None = None) -> datetime | None:
    text = clean_text(value)
    if not text: return None
    try:
        if re.fullmatch(r"\d{10,13}", text):
            number = int(text)
            return datetime.fromtimestamp(number / (1000 if len(text) == 13 else 1), timezone.utc)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?", text):
            parsed = datetime.fromisoformat(text.replace(" ", "T"))
            zone = _zone()
            return parsed.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=_zone())).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def is_alert_recent(alert: dict, now: datetime | None = None) -> bool:
    max_age = max(0, to_number(getenv("ANTICHEAT_MAX_ALERT_AGE_MINUTES"), 10)) * 60
    if not max_age: return True
    parsed = parse_alert_timestamp(alert.get("timestamp", ""))
    if parsed is None: return True
    current = now or datetime.now(timezone.utc)
    age = (current - parsed).total_seconds()
    return -120 <= age <= max_age


def group_alerts(alerts: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for alert in alerts:
        key = group_alert_key(alert)
        current = groups.get(key)
        if current is None:
            groups[key] = {"key": key, "count": 1, "alert": alert}
            continue
        current["count"] += 1
        old_date, new_date = parse_alert_timestamp(current["alert"]["timestamp"]), parse_alert_timestamp(alert["timestamp"])
        if old_date is None or (new_date and new_date >= old_date): current["alert"] = alert
    return list(groups.values())


def build_alert_embed(groups: list[dict], omitted_count: int = 0) -> discord.Embed:
    lines = []
    for group in groups:
        alert = group["alert"]
        source_count = max(0, to_number(alert.get("count")))
        repetitions = max(group["count"], source_count)
        line = f"**{(alert.get('username') or 'Jogador desconhecido')[:80]}** · `{(alert.get('cheat') or 'alerta')[:50]}`"
        metadata = [f"{repetitions} ocorrências" if repetitions > 1 else "", f"pos. {alert['pos'][:60]}" if alert.get("pos") else ""]
        lines.append("\n".join([line, " · ".join(item for item in metadata if item), alert.get("detail", "")[:180]]).strip())
    if omitted_count > 0: lines.append(f"*+ {omitted_count} detecções agrupadas nesta varredura.*")
    embed = discord.Embed(title="Alerta anticheat", description="\n\n".join(lines)[:4096], color=0xD63C3C, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"{len(groups) + omitted_count} detecção(ões) nova(s)")
    return embed


def _read_state(path: Path) -> dict:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict): raise ValueError("Formato invalido")
        recent = parsed.get("recentGroups") if isinstance(parsed.get("recentGroups"), dict) else {}
        # The JS runtime stored Date.now() in milliseconds; Python stores epoch seconds.
        normalized_recent = {
            clean_text(key): (float(value) / 1000 if float(value) > 100_000_000_000 else float(value))
            for key, value in recent.items()
            if clean_text(key) and isinstance(value, (int, float))
        }
        return {"initialized": True, "sentKeys": [clean_text(key) for key in parsed.get("sentKeys", []) if clean_text(key)], "recentGroups": normalized_recent}
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return {"initialized": False, "sentKeys": [], "recentGroups": {}}


def _save_state(path: Path, sent_keys: list[str], recent_groups: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sent = list(dict.fromkeys(key for key in sent_keys if clean_text(key)))[-5000:]
    recent = dict(sorted(((key, value) for key, value in recent_groups.items() if isinstance(value, (int, float))), key=lambda pair: pair[1])[-1000:])
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps({"initializedAt": datetime.now(timezone.utc).isoformat(), "sentKeys": sent, "recentGroups": recent}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class AntiCheatMonitor:
    def __init__(self, state_path: str | Path = STATE_FILE, registry=None):
        self.state_path = Path(state_path)
        self.registry = registry
        self._busy = False
        self._missing_warned = False

    async def _source(self) -> dict:
        local = get_pending_csv_path()
        if not allowed_path(local):
            return {"ok": False, "error": "Arquivo anticheat fora do volume autorizado."}
        if local.is_file():
            if local.stat().st_size > 8 * 1024 * 1024:
                return {"ok": False, "error": "Pending anticheat excedeu 8 MiB; requer manutenção do arquivo.", "path": str(local)}
            try: return {"ok": True, "type": "local", "path": str(local), "content": await asyncio.to_thread(local.read_text, encoding="utf-8-sig")}
            except (OSError, UnicodeError) as exc: return {"ok": False, "path": str(local), "error": str(exc)}
        remote = get_pending_ptero_path()
        if not remote: return {"ok": False, "missing": True, "path": str(local)}
        server = self.registry.get_default_server() if self.registry else {}
        async with PterodactylClient(server) as client:
            result = await client.read_file(remote)
        if not result.get("ok"): return {"ok": False, "path": remote, "error": extract_ptero_error(result)}
        return {"ok": True, "type": "ptero", "path": remote, "content": result.get("text") or ""}

    @staticmethod
    def _header(content: str) -> str:
        first = content.replace("\r\n", "\n").replace("\r", "\n").split("\n", 1)[0]
        return first + "\n" if clean_text(first) else DEFAULT_HEADER

    @classmethod
    def _remaining_content(cls, current: str, original: str) -> str | None:
        header = cls._header(original)
        if current == original: return header
        if current.startswith(original): return header + re.sub(r"^(?:\r?\n)+", "", current[len(original):])
        return None

    async def _clear_source(self, source: dict, original: str) -> None:
        if not _env_bool("ANTICHEAT_CLEAR_PENDING", True): return
        if source["type"] == "local":
            path = Path(source["path"])
            try:
                current = path.read_text(encoding="utf-8-sig")
                remaining = self._remaining_content(current, original)
                if remaining is None:
                    logger.warning("PZAntiCheat CSV mudou durante o envio; mantendo pending para evitar perda.")
                    return
                temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
                temporary.write_text(remaining, encoding="utf-8")
                temporary.replace(path)
            except (OSError, UnicodeError):
                logger.exception("Falha ao limpar CSV pending do PZAntiCheat")
            return
        server = self.registry.get_default_server() if self.registry else {}
        async with PterodactylClient(server) as client:
            current = await client.read_file(source["path"])
            if not current.get("ok"):
                logger.error("Falha ao reler CSV anticheat do Pterodactyl: %s", extract_ptero_error(current))
                return
            remaining = self._remaining_content(current.get("text") or "", original)
            if remaining is None:
                logger.warning("CSV remoto mudou durante o envio; mantendo pending para evitar perda.")
                return
            written = await client.write_file(source["path"], remaining)
            if not written.get("ok"): logger.error("Falha ao limpar CSV anticheat remoto: %s", extract_ptero_error(written))

    async def scan(self, bot) -> None:
        if not _env_bool("ANTICHEAT_ALERTS_ENABLED", True) or self._busy: return
        self._busy = True
        try:
            source = await self._source()
            if not source.get("ok"):
                if not self._missing_warned:
                    logger.warning("CSV PZAntiCheat indisponível em %s: %s", source.get("path"), source.get("error", "arquivo ainda ausente"))
                    self._missing_warned = True
                return
            self._missing_warned = False
            original = source["content"]
            alerts = await asyncio.to_thread(parse_alerts, original)
            if not alerts: return
            state = await asyncio.to_thread(_read_state, self.state_path)
            sent_keys, recent_groups = set(state["sentKeys"]), state["recentGroups"]
            if not state["initialized"] and _env_bool("ANTICHEAT_IGNORE_EXISTING_ON_START", True):
                sent_keys.update(alert_key(alert) for alert in alerts)
                await asyncio.to_thread(_save_state, self.state_path, list(sent_keys), recent_groups)
                await self._clear_source(source, original)
                logger.info("PZAntiCheat iniciado ignorando %d alertas ja existentes.", len(alerts))
                return
            now = datetime.now(timezone.utc)
            unseen = [alert for alert in alerts if alert_key(alert) not in sent_keys]
            recent = []
            for alert in unseen:
                sent_keys.add(alert_key(alert))
                if is_alert_recent(alert, now): recent.append(alert)
            if not unseen:
                await self._clear_source(source, original)
                return
            cooldown = max(0, to_number(getenv("ANTICHEAT_ALERT_COOLDOWN_MINUTES"), 10)) * 60
            pending = [group for group in group_alerts(recent) if cooldown == 0 or now.timestamp() - to_number(recent_groups.get(group["key"])) >= cooldown]
            if not pending:
                await asyncio.to_thread(_save_state, self.state_path, list(sent_keys), recent_groups)
                await self._clear_source(source, original)
                return
            maximum = max(1, min(25, int(to_number(getenv("ANTICHEAT_MAX_ALERTS_PER_TICK"), 10))))
            targets = []
            for guild in scoped_guilds(bot):
                channels = bot.store.get_settings(guild.id)["channels"]
                channel_id = channels.get("anticheat") or channels.get("admin")
                channel = guild.get_channel(channel_id) if channel_id else None
                if isinstance(channel, discord.TextChannel): targets.append(channel)
            fallback_id = clean_text(getenv("ANTICHEAT_ALERT_CHANNEL_ID") or getenv("ID_CANAL_ADMIN"))
            if current_scope() is None and not targets and fallback_id.isdigit():
                try:
                    channel = bot.get_channel(int(fallback_id)) or await bot.fetch_channel(int(fallback_id))
                    if isinstance(channel, discord.TextChannel): targets.append(channel)
                except discord.HTTPException:
                    logger.exception("Canal legado de alertas anticheat indisponivel")
            if not targets:
                logger.warning("Nenhum canal anticheat configurado no /config_bot.")
                return
            delivered = True
            omitted = max(0, len(pending) - maximum)
            embed = build_alert_embed(pending[:maximum], omitted)
            for channel in targets:
                try: await channel.send(embed=embed)
                except discord.HTTPException:
                    delivered = False
                    logger.exception("Falha ao enviar alertas anticheat para canal %s", channel.id)
            if not delivered: return
            for group in pending: recent_groups[group["key"]] = now.timestamp()
            await asyncio.to_thread(_save_state, self.state_path, list(sent_keys), recent_groups)
            await self._clear_source(source, original)
        finally:
            self._busy = False
