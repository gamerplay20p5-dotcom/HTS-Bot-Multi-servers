"""Leitura recorrente de mudanças FriendHost e logs nativos do servidor."""

from __future__ import annotations

from .i18n import t

from .tenant_scope import getenv, scoped_guilds, current_scope, allowed_path

import asyncio
import codecs
import logging
import os
import re
from pathlib import Path

import aiohttp
import discord

from .friendhost import resolve_player_file
from .pz_data import PERK_EMOJIS, PERK_LABELS
from .utils import clean_text, read_latest_csv_row, to_number


logger = logging.getLogger(__name__)
IGNORED_PERKS = {"systemdate", "systemtime", "gametime", "steamid", "username", "charname"}
LOG_READ_BYTES = 1024 * 1024


def _latest_log(root: Path, suffix: str) -> Path | None:
    try:
        latest, modified = None, -1
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name.endswith(suffix) and entry.is_file(follow_symlinks=False):
                    stamp = entry.stat(follow_symlinks=False).st_mtime_ns
                    if stamp > modified:
                        latest, modified = Path(entry.path), stamp
        return latest
    except OSError:
        return None


class FriendHostWatcher:
    def __init__(self, bot):
        self.bot = bot
        self.perks: dict[str, dict[str, str]] = {}
        self.death_count = 0
        self.active_logs: dict[str, Path | None] = {"chat": None, "user": None}
        self.offsets: dict[Path, int] = {}
        self.remainders: dict[Path, str] = {}
        self._decoders = {}
        self._identities = {}
        self.backlog_bytes = {}
        self._log_discovery_due = 0.0
        self._event_batch = None
        self._event_omitted = 0
        self._initialized = False
        self._last_data_scan = 0.0

    def _read_data_state(self) -> tuple[dict[str, dict[str, str]], dict]:
        data = self.bot.pz_data
        perks = {}
        if data.players_path and data.players_path.is_dir():
            for nick in (data.online_players() if current_scope() is not None else data.player_names()):
                row = data.latest_row(resolve_player_file(data.players_path, nick, "perks"))
                if row: perks[nick] = row
        return perks, data.death_snapshot()

    async def initialize(self) -> None:
        self.perks, deaths = await asyncio.to_thread(self._read_data_state)
        self.death_count = deaths["total"]
        root_value = clean_text(getenv("LOGS_PATH"))
        root = Path(root_value) if root_value else None
        if root and root.is_dir() and allowed_path(root):
            for kind, suffix in (("chat", "_chat.txt"), ("user", "_user.txt")):
                path = _latest_log(root, suffix)
                self.active_logs[kind] = path
                if path:
                    try: self.offsets[path] = path.stat().st_size
                    except OSError: self.offsets[path] = 0
                    self._identities[path] = path.stat().st_ino
                    self.remainders[path] = ""
        else:
            logger.warning("LOGS_PATH não encontrado; espelho de logs desativado.")
        self._initialized = True

    async def _send_evolution(self, content: str) -> None:
        if self._event_batch is not None:
            if len(self._event_batch) < 50:
                self._event_batch.append(content[:300])
            else:
                self._event_omitted += 1
            return
        channels = []
        for guild in scoped_guilds(self.bot):
            channel_id = self.bot.store.get_settings(guild.id)["channels"].get("evolution")
            channel = guild.get_channel(channel_id) if channel_id else None
            if isinstance(channel, discord.TextChannel): channels.append(channel)
        legacy_id = clean_text(getenv("ID_CANAL_EVOLUCAO"))
        if current_scope() is None and not channels and legacy_id.isdigit():
            try:
                channel = self.bot.get_channel(int(legacy_id)) or await self.bot.fetch_channel(int(legacy_id))
                if isinstance(channel, discord.TextChannel): channels.append(channel)
            except discord.HTTPException:
                logger.exception("Canal legado de evolução não está disponível")
        for channel in channels:
            try: await channel.send(content)
            except discord.HTTPException: logger.exception("Falha ao enviar notificação de evolução")

    async def _process_perks(self, nick: str, latest: dict[str, str]) -> None:
        previous = self.perks.get(nick)
        if previous:
            for key, value in latest.items():
                if key in IGNORED_PERKS: continue
                old_level, new_level = to_number(previous.get(key)), to_number(value)
                if new_level > old_level:
                    label = t(PERK_LABELS.get(key, key))
                    emoji = PERK_EMOJIS.get(key, "✨")
                    await self._send_evolution(''.join((format(t('📈 **LEVEL UP**\n🧍 **'), ''), format(nick, ''), format(t('** evoluiu '), ''), format(emoji, ''), ' **', format(label, ''), format(t('** para o nível **'), ''), format(new_level, ''.join(('g',))), '**.')))
        self.perks[nick] = latest

    @staticmethod
    def _dead_nick(row: list[str]) -> str:
        for candidate in row[4:7]:
            value = clean_text(candidate)
            if value and not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}|\d{2}:\d{2}:\d{2}", value): return value
        return t('Desconhecido')

    async def _scan_data(self) -> None:
        current, deaths = await asyncio.to_thread(self._read_data_state)
        self._event_batch, self._event_omitted = [], 0
        try:
            for nick, row in current.items():
                await self._process_perks(nick, row)
            self.perks = {nick: row for nick, row in self.perks.items() if nick in current}
            for row in deaths["events"]:
                await self._send_evolution(''.join((format(t('[MORTE] **'), ''), format(self._dead_nick(row), ''), format(t('** morreu no servidor.'), ''))))
            self.death_count = deaths["total"]
            pending = self._event_batch
        finally:
            self._event_batch = None
        if self._event_omitted:
            pending.append(''.join((format(t('Mais '), ''), format(self._event_omitted, ''), format(t(' eventos agrupados nesta leitura.'), ''))))
        batch = ""
        for line in pending:
            if len(batch) + len(line) > 1900:
                await self._send_evolution(batch)
                batch = ""
            batch += line + "\n"
        if batch:
            await self._send_evolution(batch)

    async def _read_new_lines(self, path: Path) -> list[str]:
        return await asyncio.to_thread(self._read_new_lines_sync, path)

    def _read_new_lines_sync(self, path: Path) -> list[str]:
        if not allowed_path(path):
            return []
        try:
            stat = path.stat()
            size = stat.st_size
            offset = self.offsets.get(path, 0)
            identity = self._identities.get(path)
            if size < offset or (identity is not None and identity != stat.st_ino):
                offset = 0
                self.remainders[path] = ""
                self._decoders.pop(path, None)
            self._identities[path] = stat.st_ino
            previous = self.remainders.get(path, "")
            raw = b""
            if "\n" not in previous and size > offset:
                with path.open("rb") as stream:
                    stream.seek(offset)
                    raw = stream.read(min(LOG_READ_BYTES, size - offset))
                    self.offsets[path] = stream.tell()
            self.backlog_bytes[path.name] = max(0, size - self.offsets.get(path, 0))
            decoder = self._decoders.setdefault(path, codecs.getincrementaldecoder("utf-8")(errors="replace"))
            combined = previous + decoder.decode(raw)
            parts = combined.split("\n", 2000)
            self.remainders[path] = parts.pop()[-1048576:]
            self.backlog_bytes[path.name] += len(self.remainders[path].encode("utf-8"))
            return [line.strip()[:1900] for line in parts if line.strip()]
        except OSError:
            return []

    async def _mirror_chat(self, line: str) -> None:
        webhook = clean_text(getenv("WEBHOOK_CHAT"))
        match = re.search(r"\[Chat\]\s+\[([^\]]+)\]\s+(.+)", line, re.I)
        if not match: return
        if not webhook:
            for guild in scoped_guilds(self.bot):
                channel_id = self.bot.store.get_settings(guild.id)["channels"].get("chat")
                channel = guild.get_channel(channel_id) if channel_id else None
                if isinstance(channel, discord.TextChannel):
                    await channel.send(f"**{discord.utils.escape_markdown(match.group(1))}**: {match.group(2)}"[:1900], allowed_mentions=discord.AllowedMentions.none())
            return
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(webhook, json={"content": match.group(2).strip(), "username": match.group(1).strip(), "avatar_url": "https://i.imgur.com/7HnOMg0.png"}) as response:
                    if response.status >= 300: logger.warning("Webhook de chat retornou HTTP %s: %s", response.status, (await response.text())[:200])
        except (aiohttp.ClientError, TimeoutError):
            logger.exception("Falha no espelho de chat")

    async def _mirror_user(self, line: str) -> None:
        match = re.search(r'"(.*?)"\s+(fully connected|disconnected)', line, re.I)
        if not match: return
        nick, action = clean_text(match.group(1)), clean_text(match.group(2)).casefold()
        if action == "fully connected": await self._send_evolution(''.join((format(t('[LOGIN] '), ''), format(nick, ''), format(t(' entrou no servidor.'), ''))))
        elif action == "disconnected": await self._send_evolution(''.join(('[LOGOUT] ', format(nick, ''), format(t(' saiu do servidor.'), ''))))

    async def _scan_logs(self) -> None:
        root_value = clean_text(getenv("LOGS_PATH"))
        if not root_value: return
        root = Path(root_value)
        if not root.is_dir(): return
        if not allowed_path(root):
            return
        now = asyncio.get_running_loop().time()
        discover = now >= self._log_discovery_due
        if discover:
            self._log_discovery_due = now + 30
        for kind, suffix in (("chat", "_chat.txt"), ("user", "_user.txt")):
            latest = await asyncio.to_thread(_latest_log, root, suffix) if discover else self.active_logs[kind]
            if latest is None or latest != self.active_logs[kind]:
                self.active_logs[kind] = latest
                if latest:
                    self.offsets = {p: v for p, v in self.offsets.items() if p in self.active_logs.values()}
                    self.remainders = {p: v for p, v in self.remainders.items() if p in self.active_logs.values()}
                    self._decoders = {p: v for p, v in self._decoders.items() if p in self.active_logs.values()}
                    self._identities = {p: v for p, v in self._identities.items() if p in self.active_logs.values()}
                    self.backlog_bytes = {p.name: self.backlog_bytes.get(p.name, 0) for p in self.active_logs.values() if p}
                    self.offsets[latest] = 0
                    self.remainders[latest] = ""
                continue
            if not latest: continue
            lines = await self._read_new_lines(latest)
            if kind == "chat" and current_scope() is not None:
                formatted = []
                total_chars, omitted = 0, 0
                for line in lines:
                    match = re.search(r"\[Chat\]\s+\[([^\]]+)\]\s+(.+)", line, re.I)
                    if match:
                        entry = f"**{discord.utils.escape_markdown(match.group(1))}**: {match.group(2)}"[:500]
                        if total_chars + len(entry) < 3000:
                            formatted.append(entry)
                            total_chars += len(entry) + 1
                        else:
                            omitted += 1
                if omitted:
                    formatted.append(''.join(('[', format(omitted, ''), format(t(' mensagens adicionais agrupadas; consulte o log do servidor para o conteúdo completo.]'), ''))))
                targets = []
                webhook = clean_text(getenv("WEBHOOK_CHAT"))
                if webhook:
                    targets.append(discord.Webhook.from_url(webhook, session=self.bot._ptero_session))
                else:
                    for guild in scoped_guilds(self.bot):
                        channel_id = self.bot.store.get_settings(guild.id)["channels"].get("chat")
                        channel = guild.get_channel(channel_id) if channel_id else None
                        if isinstance(channel, discord.TextChannel):
                            targets.append(channel)
                for channel in targets:
                    batch = ""
                    for line in formatted:
                        if len(batch) + len(line) > 1900:
                            await channel.send(batch, allowed_mentions=discord.AllowedMentions.none())
                            batch = ""
                        batch += line + "\n"
                    if batch:
                        await channel.send(batch, allowed_mentions=discord.AllowedMentions.none())
            elif kind == "user" and current_scope() is not None:
                events = []
                for line in lines:
                    match = re.search(r'"(.*?)"\s+(fully connected|disconnected)', line, re.I)
                    if match and len(events) < 50:
                        action = t('entrou') if match.group(2).casefold() == "fully connected" else t('saiu')
                        events.append(f"{clean_text(match.group(1))[:80]} {action} do servidor.")
                if events:
                    await self._send_evolution("\n".join(events)[:1900])
            else:
                for line in lines:
                    await (self._mirror_chat(line) if kind == "chat" else self._mirror_user(line))

    async def run(self) -> None:
        if not self._initialized:
            await self.initialize()
        while not self.bot.is_closed():
            try:
                await self._scan_logs()
                now = asyncio.get_running_loop().time()
                if now - self._last_data_scan >= 30:
                    await self._scan_data()
                    self._last_data_scan = now
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Falha nas rotinas de monitoramento FriendHost/logs")
            await asyncio.sleep(2)
