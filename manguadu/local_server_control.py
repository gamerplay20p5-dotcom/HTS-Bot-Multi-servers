"""Controle local opcional do container Pterodactyl via Docker CLI."""

from __future__ import annotations

from .i18n import t

from .tenant_scope import getenv

import asyncio
from datetime import datetime
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .utils import clean_text, to_number


VOLUME_PATTERN = re.compile(r"/var/lib/pterodactyl/volumes/([^/]+)", re.I)


def is_local_fallback_enabled() -> bool:
    value = clean_text(getenv("PTERO_LOCAL_FALLBACK_ENABLED", "1")).casefold()
    return value not in {"0", "false", "nao", "não", "no", "off"}


def get_volume_id_from_path(value: str | None) -> str:
    match = VOLUME_PATTERN.search(clean_text(value).replace("\\", "/"))
    return clean_text(match.group(1)) if match else ""


def get_known_volume_id() -> str:
    for value in (
        getenv("PTERO_VOLUME_ID"), get_volume_id_from_path(getenv("PZ_LUA_PATH")),
        get_volume_id_from_path(getenv("PZ_SAVE_ROOT")), get_volume_id_from_path(getenv("CSV_BASE_PATH")),
        get_volume_id_from_path(getenv("ANTICHEAT_CSV_PATH")),
    ):
        if clean_text(value):
            return clean_text(value)
    return ""


def get_server_identifiers() -> list[str]:
    values = (
        getenv("PTERO_DOCKER_CONTAINER"), getenv("PTERO_SERVER_UUID"),
        getenv("PTERO_VOLUME_ID"), get_known_volume_id(), getenv("PTERO_SERVER_ID"),
    )
    return list(dict.fromkeys(clean_text(value) for value in values if clean_text(value)))


async def run_local_process(command: str, args: list[str] | None = None, *, timeout: float = 30, cwd: str | Path | None = None) -> dict[str, Any]:
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            command, *(args or []), cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        out = stdout.decode("utf-8", "replace")
        err = stderr.decode("utf-8", "replace")
        return {
            "ok": process.returncode == 0, "code": process.returncode, "stdout": out, "stderr": err,
            "durationMs": int((time.monotonic() - started) * 1000),
            "error": "" if process.returncode == 0 else clean_text(err) or ''.join((format(t('Processo finalizou com codigo '), ''), format(process.returncode, ''), '.')),
        }
    except asyncio.TimeoutError:
        try:
            process.kill()
            await process.wait()
        except (UnboundLocalError, ProcessLookupError):
            pass
        return {"ok": False, "code": None, "stdout": "", "stderr": "", "durationMs": int((time.monotonic() - started) * 1000), "error": ''.join((format(t('Tempo limite excedido ('), ''), format(round(timeout), ''), 's).'))}
    except OSError as exc:
        return {"ok": False, "code": None, "stdout": "", "stderr": "", "durationMs": int((time.monotonic() - started) * 1000), "error": str(exc)}


def parse_docker_ps_json(output: str) -> list[dict[str, str]]:
    containers = []
    for line in clean_text(output).splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append({
            "id": clean_text(raw.get("ID")), "names": clean_text(raw.get("Names")),
            "image": clean_text(raw.get("Image")), "state": clean_text(raw.get("State")),
            "status": clean_text(raw.get(t('Status'))), "labels": clean_text(raw.get("Labels")),
            "mounts": clean_text(raw.get("Mounts")),
        })
    return containers


def score_docker_container(container: dict[str, Any], identifiers: list[str] | None = None) -> int:
    fields = [clean_text(container.get(key)).casefold() for key in ("id", "names", "image", "labels", "mounts")]
    score = 0
    for identifier in identifiers if identifiers is not None else get_server_identifiers():
        needle = clean_text(identifier).casefold()
        if needle and any(needle in field for field in fields):
            score += 10 if len(needle) >= 8 else 3
    if re.search(r"pterodactyl|wings|io\.pterodactyl\.server", clean_text(container.get("labels")), re.I):
        score += 2
    return score


async def list_docker_containers() -> dict[str, Any]:
    docker = clean_text(getenv("DOCKER_BIN")) or "docker"
    result = await run_local_process(docker, ["ps", "-a", "--format", "{{json .}}"], timeout=15)
    return {**result, "containers": parse_docker_ps_json(result["stdout"])} if result["ok"] else result


async def find_docker_container() -> dict[str, Any]:
    if not is_local_fallback_enabled():
        return {"ok": False, "error": t('Fallback local/Docker desativado.')}
    configured = clean_text(getenv("PTERO_DOCKER_CONTAINER"))
    if configured:
        return {"ok": True, "container": {"id": configured, "names": configured, "configured": True}}
    listed = await list_docker_containers()
    if not listed["ok"]:
        return {"ok": False, "error": listed.get("error") or clean_text(listed.get("stderr")) or t('Docker indisponivel.')}
    ranked = [{**container, "score": score_docker_container(container)} for container in listed["containers"]]
    ranked = sorted((item for item in ranked if item["score"] > 0), key=lambda item: item["score"], reverse=True)
    if not ranked:
        return {"ok": False, "error": t('Nenhum container Docker do servidor foi encontrado.')}
    best = [item for item in ranked if item["score"] == ranked[0]["score"]]
    if len(best) > 1:
        names = ", ".join(item["names"] or item["id"] for item in best)
        return {"ok": False, "error": ''.join((format(t('Mais de um container possivel: '), ''), format(names, ''), format(t('. Configure PTERO_DOCKER_CONTAINER.'), '')))}
    return {"ok": True, "container": best[0]}


def normalize_docker_state(value: str) -> str:
    state = clean_text(value).casefold()
    return {"running": "running", "created": "offline", "exited": "offline", "dead": "offline", "restarting": "starting", "paused": "stopping"}.get(state, state or t('desconhecido'))


def _human_bytes(value: str) -> int:
    match = re.match(r"^([\d.,]+)\s*([kmgtp]?i?b)?", clean_text(value), re.I)
    if not match:
        return 0
    number = to_number(match.group(1))
    unit = clean_text(match.group(2)).casefold()
    multiplier = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}
    return round(number * multiplier.get(unit, 1))


async def _container_id() -> tuple[str | None, dict[str, Any]]:
    found = await find_docker_container()
    if not found["ok"]:
        return None, found
    container = found["container"]
    return container.get("id") or container.get("names"), found


async def fetch_docker_resources() -> dict[str, Any]:
    started = time.monotonic()
    container_id, found = await _container_id()
    if not container_id:
        return found
    docker = clean_text(getenv("DOCKER_BIN")) or "docker"
    inspected = await run_local_process(docker, ["inspect", container_id], timeout=15)
    if not inspected["ok"]:
        return inspected
    try:
        raw = json.loads(inspected["stdout"])
        state_data = (raw[0] or {}).get("State") or {}
    except (json.JSONDecodeError, IndexError, TypeError):
        return {"ok": False, "error": t('Falha ao interpretar docker inspect.')}
    state = normalize_docker_state(state_data.get(t('Status'), ""))
    resources: dict[str, Any] = {}
    if state_data.get("Running"):
        stats = await run_local_process(docker, ["stats", "--no-stream", "--format", "{{json .}}", container_id], timeout=15)
        try:
            record = json.loads(stats["stdout"].splitlines()[0]) if stats["ok"] else {}
            memory_usage = clean_text(record.get("MemUsage")).split("/")[0]
            resources = {"cpu_absolute": to_number(clean_text(record.get("CPUPerc")).replace("%", "")), "memory_bytes": _human_bytes(memory_usage)}
        except (json.JSONDecodeError, IndexError, AttributeError):
            pass
    started_at = clean_text(state_data.get("StartedAt"))
    uptime = 0
    if started_at:
        try:
            date = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            uptime = max(0, int((time.time() - date.timestamp()) * 1000))
        except (ValueError, OverflowError):
            pass
    container = found["container"]
    return {
        "ok": True, "status": 200, "state": state,
        "resources": {**resources, "uptime": uptime}, "latencyMs": int((time.monotonic() - started) * 1000),
        "transport": "docker", "container": container.get("names") or container_id,
    }


async def manage_docker_server(action: str) -> dict[str, Any]:
    normalized = clean_text(action).casefold()
    if normalized not in {"start", "stop", "restart", "kill"}:
        return {"ok": False, "error": ''.join((format(t('Acao local nao suportada: '), ''), format(action, '')))}
    container_id, found = await _container_id()
    if not container_id:
        return found
    docker = clean_text(getenv("DOCKER_BIN")) or "docker"
    result = await run_local_process(docker, [normalized, container_id], timeout=120)
    container = found["container"]
    return {
        "ok": result["ok"], "status": 204 if result["ok"] else 500, "error": result.get("error", ""),
        "text": result.get("stdout", ""), "transport": "docker", "container": container.get("names") or container_id,
    }


def is_local_absolute_path(value: str) -> bool:
    path = Path(clean_text(value))
    return bool(clean_text(value) and path.is_absolute() and path.exists())
