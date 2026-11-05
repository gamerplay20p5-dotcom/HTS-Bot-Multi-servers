"""Client API assíncrona do Pterodactyl para um servidor do registro."""

from __future__ import annotations

from .i18n import t

import json
import asyncio
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from .server_registry import describe_missing_config
from .utils import clean_text
from .tenant_scope import current_scope


def extract_ptero_error(result: dict[str, Any] | None) -> str:
    if not result:
        return t('Falha desconhecida.')
    if result.get("error"):
        return str(result["error"])
    payload = result.get("json") or {}
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if isinstance(errors, list) and errors:
        detail = errors[0].get("detail") if isinstance(errors[0], dict) else None
        if detail:
            return str(detail)
    return clean_text(result.get("text")) or ''.join(('HTTP ', format(result.get('status', t('desconhecido')), '')))


class PterodactylClient:
    def __init__(self, server: dict[str, Any], session: aiohttp.ClientSession | None = None, limiter=None):
        self.server = server
        scope = current_scope()
        bot = getattr(getattr(scope, "runtime", None), "bot", None)
        self._bot = bot
        if session is None and bot is not None:
            session = bot._ptero_session
        self._limiter = limiter or (bot._ptero_limiter if bot is not None else asyncio.Semaphore(1))
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "PterodactylClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    def build_url(self, endpoint: str, *, client_root: bool = False) -> str | None:
        ptero = self.server.get("ptero", {})
        base = clean_text(ptero.get("url")).rstrip("/")
        server_id = clean_text(ptero.get("serverId"))
        if not base or (not client_root and not server_id):
            return None
        if client_root:
            return f"{base}/api/client" + (f"/{endpoint}" if endpoint and not endpoint.startswith("?") else endpoint)
        return f"{base}/api/client/servers/{quote(server_id, safe='')}" + (f"/{endpoint}" if endpoint else "")

    async def request(
        self, endpoint: str, *, method: str = "GET", body: dict[str, Any] | None = None,
        raw_body: str | None = None, timeout: float = 15, client_root: bool = False,
    ) -> dict[str, Any]:
        missing = describe_missing_config(self.server)
        if client_root:
            missing = [item for item in missing if item != "ptero.serverId"]
        if missing:
            return {"ok": False, "error": ''.join((format(t('Configuracao do Pterodactyl incompleta: '), ''), format(', '.join(missing), ''), '.'))}
        if self._session is None:
            raise RuntimeError("Use 'async with PterodactylClient(server)' para abrir a sessao HTTP.")
        headers = {
            "Authorization": f"Bearer {self.server['ptero']['apiKey']}",
            "Accept": "Application/vnd.pterodactyl.v1+json",
            "User-Agent": "HTSBotMultiServers-Python/0.2",
        }
        data = None
        if raw_body is not None:
            headers["Content-Type"] = "text/plain; charset=utf-8"
            data = raw_body.encode("utf-8")
        elif body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            if self._bot is not None:
                delay = self._bot._ptero_retry_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(min(60, delay))
            async with self._limiter, self._session.request(
                method, self.build_url(endpoint, client_root=client_root), headers=headers, data=data,
                timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False,
            ) as response:
                # API responses and log exports must not grow process memory without bounds.
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    chunks.extend(chunk)
                    if len(chunks) > 8 * 1024 * 1024:
                        return {"ok": False, "status": response.status, "error": t('Resposta da API excedeu 8 MiB; use leitura local do volume para arquivos grandes.')}
                content = chunks.decode("utf-8", errors="replace")
                if response.status == 429 and self._bot is not None:
                    try:
                        delay = max(1, min(60, float(response.headers.get("Retry-After", "10"))))
                    except ValueError:
                        delay = 10
                    self._bot._ptero_retry_at = time.monotonic() + delay
                try:
                    parsed = json.loads(content) if content else None
                except json.JSONDecodeError:
                    parsed = None
                return {"ok": 200 <= response.status < 300, "status": response.status, "text": content, "json": parsed,
                        "retryAfter": response.headers.get("Retry-After") if response.status == 429 else None}
        except (aiohttp.ClientError, TimeoutError) as exc:
            return {"ok": False, "error": str(exc)}

    async def list_servers(self, *, page: int = 1, search: str = "") -> dict[str, Any]:
        """Client's accessible servers only. Never request admin/admin-all."""
        from urllib.parse import urlencode
        query = {"page": max(1, int(page)), "per_page": 25}
        if search.strip():
            query["filter[name]"] = search.strip()[:100]
        result = await self.request("?" + urlencode(query), client_root=True)
        if not result["ok"]:
            return result
        payload = result.get("json") or {}
        servers = []
        for item in payload.get("data", [])[:25]:
            try:
                servers.append(self.server_summary(item))
            except (ValueError, KeyError, TypeError):
                continue
        pagination = (payload.get("meta") or {}).get("pagination") or {}
        return {"ok": True, "servers": servers, "page": int(pagination.get("current_page", page)),
                "pages": max(1, int(pagination.get("total_pages", 1)))}

    @staticmethod
    def server_summary(item):
        from uuid import UUID
        import re
        attributes = item.get("attributes", item)
        uuid = str(UUID(attributes["uuid"]))
        identifier = str(attributes["identifier"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,36}", identifier):
            raise ValueError("invalid API identifier")
        relationships = attributes.get("relationships") or {}
        allocations = []
        for allocation in (relationships.get("allocations") or {}).get("data", [])[:32]:
            data = allocation.get("attributes", {})
            allocations.append({key: data[key] for key in ("ip", "ip_alias", "port", "is_default") if key in data})
        # Deliberate allowlist: API invocation/variables may contain secrets.
        return {"uuid": uuid, "identifier": identifier, "label": str(attributes.get("name") or identifier)[:100],
                "description": str(attributes.get("description") or "")[:500], "node": str(attributes.get("node") or "")[:100],
                "status": attributes.get("status"), "suspended": attributes.get("is_suspended") is True, "allocations": allocations}

    async def list_files(self, directory: str = "/") -> dict[str, Any]:
        result = await self.request(f"files/list?directory={quote(directory or '/', safe='')}")
        if not result["ok"]:
            return result
        data = (result.get("json") or {}).get("data") or []
        files = []
        for item in data:
            attributes = item.get("attributes", item)
            if attributes.get("name"):
                files.append({
                    "name": clean_text(attributes.get("name")),
                    "size": attributes.get("size") or 0,
                    "isFile": attributes.get("is_file") is True,
                    "isDirectory": attributes.get("is_file") is False,
                    "modifiedAt": clean_text(attributes.get("modified_at")),
                })
        return {**result, "directory": directory or "/", "files": files}

    async def read_file(self, file_path: str) -> dict[str, Any]:
        if not clean_text(file_path):
            return {"ok": False, "error": t('Caminho do arquivo ausente.')}
        return await self.request(f"files/contents?file={quote(file_path, safe='')}")

    async def write_file(self, file_path: str, content: str) -> dict[str, Any]:
        if not clean_text(file_path):
            return {"ok": False, "error": t('Caminho do arquivo ausente.')}
        return await self.request(f"files/write?file={quote(file_path, safe='')}", method="POST", raw_body=content, timeout=60)

    async def delete_files(self, root: str, files: list[str]) -> dict[str, Any]:
        names = list(dict.fromkeys(clean_text(name) for name in files if clean_text(name)))
        if not names:
            return {"ok": True, "status": 204, "deleted": []}
        result = await self.request("files/delete", method="POST", body={"root": root or "/", "files": names}, timeout=60)
        return {**result, "deleted": names if result["ok"] else []}

    async def resources(self) -> dict[str, Any]:
        result = await self.request("resources")
        if not result["ok"]:
            return result
        attributes = (result.get("json") or {}).get("attributes") or result.get("json") or {}
        return {**result, "state": clean_text(attributes.get("current_state") or attributes.get("state") or t('desconhecido')), "resources": attributes.get("resources") or {}}

    async def power(self, action: str) -> dict[str, Any]:
        if action not in {"start", "stop", "restart", "kill"}:
            return {"ok": False, "error": t('Acao de energia invalida.')}
        return await self.request("power", method="POST", body={"signal": action})

    async def command(self, command: str) -> dict[str, Any]:
        if not clean_text(command):
            return {"ok": False, "error": t('Comando vazio.')}
        return await self.request("command", method="POST", body={"command": command})

    async def list_backups(self) -> dict[str, Any]:
        result = await self.request("backups?per_page=100")
        if not result["ok"]:
            return result
        items = (result.get("json") or {}).get("data") or []
        result["backups"] = [self._normalize_backup(item) for item in items]
        return result

    @staticmethod
    def _normalize_backup(item: dict[str, Any]) -> dict[str, Any]:
        attributes = item.get("attributes", item) if isinstance(item, dict) else {}
        return {
            "uuid": clean_text(attributes.get("uuid") or attributes.get("identifier")),
            "name": clean_text(attributes.get("name")) or "Backup sem nome",
            "createdAt": clean_text(attributes.get("created_at")),
            "completedAt": clean_text(attributes.get("completed_at")),
            "isSuccessful": attributes.get("is_successful") is True,
            "isLocked": attributes.get("is_locked") is True,
            "bytes": attributes.get("bytes") or 0,
        }

    async def get_backup(self, backup_uuid: str) -> dict[str, Any]:
        if not clean_text(backup_uuid):
            return {"ok": False, "error": "UUID do backup ausente."}
        result = await self.request(f"backups/{quote(backup_uuid, safe='')}")
        if result["ok"]:
            attributes = (result.get("json") or {}).get("attributes") or result.get("json") or {}
            result["backup"] = {
                "uuid": clean_text(attributes.get("uuid") or attributes.get("identifier")),
                "name": clean_text(attributes.get("name")) or "Backup sem nome",
                "createdAt": clean_text(attributes.get("created_at")),
                "completedAt": clean_text(attributes.get("completed_at")),
                "isSuccessful": attributes.get("is_successful") is True,
                "isLocked": attributes.get("is_locked") is True,
                "bytes": attributes.get("bytes") or 0,
            }
        return result

    async def create_backup(self, name: str = "", ignored: str = "", *, is_locked: bool = False) -> dict[str, Any]:
        body = {key: value for key, value in (("name", clean_text(name)), ("ignored", clean_text(ignored))) if value}
        if is_locked:
            body["is_locked"] = True
        result = await self.request("backups", method="POST", body=body)
        if result["ok"]:
            result["backup"] = self._normalize_backup((result.get("json") or {}).get("attributes") or result.get("json") or {})
        return result

    async def restore_backup(self, backup_uuid: str, *, truncate: bool = False) -> dict[str, Any]:
        if not clean_text(backup_uuid):
            return {"ok": False, "error": "UUID do backup ausente."}
        return await self.request(
            f"backups/{quote(backup_uuid, safe='')}/restore",
            method="POST", body={"truncate": bool(truncate)}, timeout=60,
        )

    async def delete_backup(self, backup_uuid: str) -> dict[str, Any]:
        if not clean_text(backup_uuid):
            return {"ok": False, "error": "UUID do backup ausente."}
        return await self.request(f"backups/{quote(backup_uuid, safe='')}", method="DELETE", timeout=60)
