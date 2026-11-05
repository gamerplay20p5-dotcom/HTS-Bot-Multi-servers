"""Agendador persistente de tarefas recorrentes para os servidores PZ."""

from __future__ import annotations

from .i18n import t, language

from .tenant_scope import getenv

import asyncio
import json
import logging
import os
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .pterodactyl import PterodactylClient, extract_ptero_error
from .server_integrations import manage_server, send_server_console_command
from .utils import clean_text


logger = logging.getLogger(__name__)
STORE_FILE = Path(__file__).resolve().parent.parent / "data" / "automations.json"
DEFAULT_TIMEZONE = "America/Sao_Paulo"
DEFAULT_BACKUP_KEEP = 5
DEFAULT_GRACE_MINUTES = 5
BACKUP_PREFIX = "Bot automatico - "
ACTION_LABELS = {
    "start": 'Ligar servidor', "stop": 'Desligar servidor', "restart": 'Reiniciar servidor',
    "backup": 'Criar backup', "save": 'Salvar mundo', "command": 'Comando do console',
}
DAY_LABELS = ["dom", "seg", "ter", "qua", "qui", "sex", "sab"]
DAY_DISPLAY_LABELS = ["1-dom", "2-seg", "3-ter", "4-qua", "5-qui", "6-sex", "7-sab"]
EN_DAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

def display_day(day: int, *, numbered: bool = True) -> str:
    if language() == "en":
        return f"{day + 1}-{EN_DAY_LABELS[day]}" if numbered else EN_DAY_LABELS[day]
    return DAY_DISPLAY_LABELS[day] if numbered else DAY_LABELS[day]

DAY_ALIASES = {
    "domingo": 0, "dom": 0, "sun": 0, "sunday": 0,
    "segunda": 1, "seg": 1, "mon": 1, "monday": 1,
    "terca": 2, "ter": 2, "tue": 2, "tuesday": 2,
    "quarta": 3, "qua": 3, "wed": 3, "wednesday": 3,
    "quinta": 4, "qui": 4, "thu": 4, "thursday": 4,
    "sexta": 5, "sex": 5, "fri": 5, "friday": 5,
    "sabado": 6, "sab": 6, "sat": 6, "saturday": 6,
}


def _strip_accents(value: object) -> str:
    return "".join(char for char in unicodedata.normalize("NFD", clean_text(value)) if unicodedata.category(char) != "Mn")


def get_timezone() -> str:
    return clean_text(getenv("AUTOMATION_TIMEZONE")) or clean_text(getenv("TZ")) or DEFAULT_TIMEZONE


def _zone() -> ZoneInfo | timezone:
    try:
        return ZoneInfo(get_timezone())
    except ZoneInfoNotFoundError:
        logger.warning("Timezone %s nao encontrada; usando timezone local.", get_timezone())
        return datetime.now().astimezone().tzinfo or timezone.utc


def _grace_minutes() -> int:
    try:
        value = int(getenv("AUTOMATION_GRACE_MINUTES", ""))
        return value if value >= 1 else DEFAULT_GRACE_MINUTES
    except ValueError:
        return DEFAULT_GRACE_MINUTES


def normalize_time(value: object) -> str:
    normalized = _strip_accents(value).lower().replace(" ", "").replace("h", ":")
    match = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))", normalized)
    if not match:
        return ""
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def parse_days(value: object) -> list[int]:
    normalized = _strip_accents(value).lower()
    if not normalized or re.search(r"\b(todo|todos|diario|diaria|daily|all)\b", normalized):
        return list(range(7))
    if re.search(r"\b(uteis|util|weekday|weekdays)\b", normalized):
        return [1, 2, 3, 4, 5]
    if re.search(r"\b(fim|fds|weekend|weekends)\b", normalized):
        return [0, 6]
    result = set()
    for token in re.split(r"[\s,;|/]+", normalized):
        if token.isdigit():
            number = int(token)
            if number == 0: result.add(0)
            elif 1 <= number <= 7: result.add(number - 1)
        elif token in DAY_ALIASES:
            result.add(DAY_ALIASES[token])
    return sorted(result)


def format_days(days: list[int]) -> str:
    normalized = sorted(set(days))
    if len(normalized) == 7: return t('todos os dias (1-7)')
    if normalized == [1, 2, 3, 4, 5]: return t('dias úteis (2-6)')
    if normalized == [0, 6]: return t('fim de semana (1,7)')
    return ", ".join(display_day(day) for day in normalized if 0 <= day <= 6) or t('sem dias')


def _normalize(raw: Any) -> dict | None:
    if not isinstance(raw, dict): return None
    action = clean_text(raw.get("action")).lower()
    time_value = normalize_time(raw.get("time"))
    days = raw.get("days") if isinstance(raw.get("days"), list) else parse_days(raw.get("days"))
    try: days = sorted({int(day) for day in days if 0 <= int(day) <= 6})
    except (TypeError, ValueError): days = []
    if not clean_text(raw.get("id")) or action not in ACTION_LABELS or not time_value or not days: return None
    try: retain = max(1, int(raw.get("retainBackups") or DEFAULT_BACKUP_KEEP))
    except (TypeError, ValueError): retain = DEFAULT_BACKUP_KEEP
    last_result = raw.get("lastResult") if isinstance(raw.get("lastResult"), dict) else None
    return {
        "id": clean_text(raw.get("id")), "action": action, "time": time_value, "days": days,
        "enabled": raw.get("enabled") is not False, "command": clean_text(raw.get("command")),
        "retainBackups": retain, "createdAt": clean_text(raw.get("createdAt")),
        "updatedAt": clean_text(raw.get("updatedAt")), "lastRunKey": clean_text(raw.get("lastRunKey")),
        "lastRunAt": clean_text(raw.get("lastRunAt")), "lastResult": last_result,
    }


class AutomationScheduler:
    def __init__(self, path: str | Path = STORE_FILE, server_provider: Callable[[], dict] | None = None):
        self.path = Path(path)
        self.server_provider = server_provider or (lambda: {})
        self._running = False

    def get_automations(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []
        items = raw if isinstance(raw, list) else raw.get("automations", []) if isinstance(raw, dict) else []
        return [automation for item in items if (automation := _normalize(item))]

    def _save(self, automations: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        temporary.write_text(json.dumps({"version": 1, "automations": automations}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def create(self, action: str, time_value: str, days: str = "", command: str = "", retain_backups: int | None = None) -> dict:
        action = clean_text(action).lower()
        normalized_time, normalized_days = normalize_time(time_value), parse_days(days)
        if action not in ACTION_LABELS: return {"ok": False, "error": t('Tipo de automação inválido.')}
        if not normalized_time: return {"ok": False, "error": t('Horário inválido. Use HH:MM, por exemplo 03:30.')}
        if not normalized_days: return {"ok": False, "error": t('Dias inválidos. Use todos, úteis, fds ou números de 1 a 7 (1=domingo).')}
        command = clean_text(command)
        if action == "command" and not command: return {"ok": False, "error": t('Informe o comando do console para automação do tipo comando.')}
        now = datetime.now(timezone.utc).isoformat()
        automation = {
            "id": f"{int(datetime.now(timezone.utc).timestamp() * 1000):x}-{secrets.token_hex(2)}",
            "action": action, "time": normalized_time, "days": normalized_days, "enabled": True,
            "command": command, "retainBackups": max(1, int(retain_backups or DEFAULT_BACKUP_KEEP)),
            "createdAt": now, "updatedAt": now, "lastRunKey": "", "lastRunAt": "", "lastResult": None,
        }
        items = self.get_automations()
        items.append(automation)
        self._save(items)
        return {"ok": True, "automation": automation}

    def update(self, automation_id: str, **changes: Any) -> dict | None:
        items = self.get_automations()
        target = next((item for item in items if item["id"] == clean_text(automation_id)), None)
        if target is None: return None
        target.update(changes)
        target["updatedAt"] = datetime.now(timezone.utc).isoformat()
        self._save(items)
        return target

    def remove(self, automation_id: str) -> bool:
        items = self.get_automations()
        reduced = [item for item in items if item["id"] != clean_text(automation_id)]
        if len(reduced) == len(items): return False
        self._save(reduced)
        return True

    def set_enabled(self, automation_id: str, enabled: bool) -> dict | None:
        return self.update(automation_id, enabled=bool(enabled))

    @staticmethod
    def _now_parts(now: datetime | None = None) -> dict:
        local = (now or datetime.now(timezone.utc)).astimezone(_zone())
        return {"now": local, "dateKey": local.strftime("%Y-%m-%d"), "timeKey": local.strftime("%H:%M"), "totalMinutes": local.hour * 60 + local.minute, "weekday": (local.weekday() + 1) % 7}

    def next_run(self, automation: dict, now: datetime | None = None) -> dict | None:
        parts = self._now_parts(now)
        hour, minute = map(int, automation["time"].split(":"))
        scheduled = hour * 60 + minute
        for offset in range(8):
            day = parts["now"].date() + timedelta(days=offset)
            weekday = (day.weekday() + 1) % 7
            if weekday not in automation["days"] or (offset == 0 and scheduled < parts["totalMinutes"]): continue
            return {"dateKey": day.isoformat(), "time": automation["time"], "weekday": weekday, "weekdayLabel": display_day(weekday)}
        return None

    async def _prune_backups(self, server: dict, keep: int) -> dict:
        async with PterodactylClient(server) as client:
            result = await client.list_backups()
            if not result.get("ok"): return {"ok": False, "deleted": [], "failed": [extract_ptero_error(result)]}
            managed = sorted((backup for backup in result.get("backups", []) if backup["name"].startswith(BACKUP_PREFIX) and not backup["isLocked"]), key=lambda item: item["createdAt"], reverse=True)
            deleted, failed = [], []
            for backup in managed[keep:]:
                removed = await client.delete_backup(backup["uuid"])
                (deleted if removed.get("ok") else failed).append(backup["uuid"] if removed.get("ok") else extract_ptero_error(removed))
            return {"ok": not failed, "deleted": deleted, "failed": failed}

    async def _backup(self, server: dict, retain_backups: int) -> dict:
        keep = max(1, int(retain_backups or DEFAULT_BACKUP_KEEP))
        local = datetime.now(_zone())
        name = f"{BACKUP_PREFIX}{local:%Y-%m-%d %H:%M}"
        async with PterodactylClient(server) as client:
            created = await client.create_backup(name, getenv("PTERO_BACKUP_IGNORED", ""))
        pruned_before = {"ok": True, "deleted": [], "failed": []}
        if not created.get("ok"):
            error = extract_ptero_error(created)
            if re.search(r"limit|maximum|maximo|too many|atingiu|chegou|quota", _strip_accents(error).lower()):
                pruned_before = await self._prune_backups(server, max(0, keep - 1))
                async with PterodactylClient(server) as client:
                    created = await client.create_backup(name, getenv("PTERO_BACKUP_IGNORED", ""))
            if not created.get("ok"):
                return {"ok": False, "message": ''.join((format(t('Falha ao criar backup: '), ''), format(error, '')))}
        pruned_after = await self._prune_backups(server, keep)
        deleted = len(pruned_before["deleted"]) + len(pruned_after["deleted"])
        if not pruned_before["ok"] or not pruned_after["ok"]:
            return {"ok": False, "message": t('Backup solicitado, mas a limpeza de backups antigos falhou.')}
        backup = created.get("backup") or {}
        return {"ok": True, "message": ''.join((format(t('Backup solicitado ('), ''), format(backup.get('name') or name, ''), format(t('). Backups antigos apagados: '), ''), format(deleted, ''), '.'))}

    async def execute(self, automation: dict, server: dict | None = None) -> dict:
        server = server or self.server_provider()
        action = automation.get("action")
        if action == "backup": return await self._backup(server, automation.get("retainBackups", DEFAULT_BACKUP_KEEP))
        if action == "save" or action == "command":
            command = "save" if action == "save" else clean_text(automation.get("command"))
            if not command: return {"ok": False, "message": t('Comando do console ausente.')}
            result = await send_server_console_command(server, command)
            return {"ok": result.get("ok"), "message": ''.join((format(t('Comando `'), ''), format(command, ''), format(t('` enviado pelo console do Pterodactyl.'), ''))) if result.get("ok") else ''.join((format(t('Falha no comando `'), ''), format(command, ''), '`: ', format(extract_ptero_error(result), '')))}
        last = None
        for attempt in range(1, 3):
            last = await manage_server(server, action)
            if last.get("ok"):
                via = "Docker/local" if last.get("transport") == "docker" else "Pterodactyl"
                return {"ok": True, "message": ''.join((format(t(ACTION_LABELS[action]), ''), format(t(' enviado via '), ''), format(via, ''))) + (''.join((format(t(' na tentativa '), ''), format(attempt, ''), '.')) if attempt > 1 else ".")}
            if attempt < 2: await asyncio.sleep(2)
        return {"ok": False, "message": ''.join((format(t('Falha ao executar '), ''), format(t(ACTION_LABELS.get(action, action)), ''), ': ', format(extract_ptero_error(last), '')))}

    async def run_now(self, automation_id: str) -> dict:
        automation = next((item for item in self.get_automations() if item["id"] == clean_text(automation_id)), None)
        if not automation: return {"ok": False, "error": t('Automação não encontrada.')}
        result = await self.execute(automation)
        self.update(automation["id"], lastRunAt=datetime.now(timezone.utc).isoformat(), lastResult={**result, "at": datetime.now(timezone.utc).isoformat(), "manual": True})
        return {**result, "automation": automation}

    async def run_due(self, notify: Callable[[str], Any] | None = None, now: datetime | None = None) -> None:
        if self._running: return
        self._running = True
        try:
            parts = self._now_parts(now)
            due = []
            for item in self.get_automations():
                hour, minute = map(int, item["time"].split(":"))
                after = parts["totalMinutes"] - (hour * 60 + minute)
                run_key = f"{parts['dateKey']} {item['time']}"
                if item["enabled"] and parts["weekday"] in item["days"] and 0 <= after < _grace_minutes() and item["lastRunKey"] != run_key:
                    due.append((item, run_key))
            for automation, run_key in due:
                self.update(automation["id"], lastRunKey=run_key, lastRunAt=datetime.now(timezone.utc).isoformat())
                result = await self.execute(automation)
                self.update(automation["id"], lastResult={**result, "at": datetime.now(timezone.utc).isoformat()})
                if notify:
                    try: await notify(''.join((format(t('[AUTOMAÇÃO] '), ''), format('OK' if result['ok'] else t('FALHA'), ''), ' ', format(t(ACTION_LABELS[automation['action']]), ''), ' (', format(automation['id'], ''), ')\n', format(result['message'], ''))))
                    except Exception: logger.exception("Falha ao notificar execucao automatica")
        finally:
            self._running = False

    async def backup_now(self, retain_backups: int | None = None) -> dict:
        return await self._backup(self.server_provider(), retain_backups or DEFAULT_BACKUP_KEEP)

    def format_line(self, automation: dict) -> str:
        status = "ativa" if automation["enabled"] else t('pausada')
        next_run = self.next_run(automation)
        next_info = ''.join((format(t(' | próxima: '), ''), format(next_run['dateKey'], ''), ' ', format(next_run['time'], ''), ' (', format(next_run['weekdayLabel'], ''), ')')) if next_run else ""
        backup_info = ''.join((format(t(' | manter '), ''), format(automation['retainBackups'], ''), format(t(' backup(s)'), ''))) if automation["action"] == "backup" else ""
        command_info = f" | {automation['command']}" if automation["action"] == "command" else ""
        last_info = ''.join((format(t(' | última: '), ''), format('OK' if automation['lastResult'].get('ok') else t('FALHA'), ''))) if automation.get("lastResult") else ""
        return ''.join(('`', format(automation['id'], ''), '` ', format(status, ''), ' | ', format(t(ACTION_LABELS[automation['action']]), ''), ' | ', format(automation['time'], ''), ' | ', format(format_days(automation['days']), ''), format(next_info, ''), format(backup_info, ''), format(command_info, ''), format(last_info, '')))

    def runtime_status(self) -> dict:
        parts = self._now_parts()
        return {"timezone": get_timezone(), "dateKey": parts["dateKey"], "timeKey": parts["timeKey"], "weekdayLabel": display_day(parts["weekday"], numbered=False), "graceMinutes": _grace_minutes(), "storeFile": str(self.path), "totalAutomations": len(self.get_automations())}

    def format_list(self) -> str:
        items = self.get_automations()
        return "\n".join(self.format_line(item) for item in items) if items else t('Nenhuma automação criada ainda.')
