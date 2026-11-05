"""Operacoes de servidor que combinam Pterodactyl e fallback Docker local."""

from __future__ import annotations

from .i18n import t

import time
from typing import Any

from .local_server_control import fetch_docker_resources, manage_docker_server
from .pterodactyl import PterodactylClient, extract_ptero_error
from .server_registry import describe_missing_config
from .utils import clean_text


async def fetch_server_resources(server: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    async with PterodactylClient(server) as client:
        ptero = await client.resources()
    ptero["latencyMs"] = round((time.monotonic() - started) * 1000)
    if ptero["ok"]:
        return {**ptero, "transport": "pterodactyl"}
    docker = await fetch_docker_resources()
    if docker.get("ok"):
        return {**docker, "pteroError": extract_ptero_error(ptero)}
    return {**ptero, "dockerError": docker.get("error", "Docker local indisponivel.")}


async def manage_server(server: dict[str, Any], action: str) -> dict[str, Any]:
    if action not in {"start", "stop", "restart", "kill"}:
        return {"ok": False, "error": f"Acao de servidor invalida: {action}"}
    async with PterodactylClient(server) as client:
        ptero = await client.power(action)
    if ptero["ok"]:
        return {**ptero, "transport": "pterodactyl"}
    docker = await manage_docker_server(action)
    if docker.get("ok"):
        return {**docker, "pteroError": extract_ptero_error(ptero)}
    return {**ptero, "dockerError": docker.get("error", "Docker local indisponivel.")}


def describe_server_error(result: dict[str, Any]) -> str:
    if result.get("error"):
        return clean_text(result["error"])
    ptero_error, docker_error = clean_text(result.get("pteroError")), clean_text(result.get("dockerError"))
    if ptero_error and docker_error:
        return f"Pterodactyl: {ptero_error} | Docker/local: {docker_error}"
    return ptero_error or docker_error or extract_ptero_error(result)


async def send_server_console_command(server: dict[str, Any], command: str) -> dict[str, Any]:
    missing = describe_missing_config(server)
    if missing:
        return {"ok": False, "error": ''.join((format(t('Configuracao do Pterodactyl incompleta: '), ''), format(', '.join(missing), ''), '.'))}
    async with PterodactylClient(server) as client:
        result = await client.command(command)
    return {**result, "transport": "pterodactyl"}
