"""Owner-approved discovery and proof-file writes. Global API access is NOT ownership."""
from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
import time
from collections import OrderedDict

from .i18n import t
from .pterodactyl import PterodactylClient
from .guild_runtime import normalize_binding, resolve_volume
from .stats.discovery import lua_candidates

PROOF = "/friendhost-discord-link.txt"


class SetupError(ValueError):
    pass


def check(result):
    if not result.get("ok"):
        status = result.get("status")
        if status in {401, 403}:
            raise SetupError(t("Credencial sem permissão na API Client do Pterodactyl."))
        if status == 429:
            raise SetupError(t("Limite da API atingido. Aguarde e tente novamente."))
        raise SetupError(t("API Pterodactyl indisponível. Tente novamente ou use o vínculo manual.") + (f" HTTP {status}" if status else ""))
    return result


class ServerSetup:
    def __init__(self, bot):
        self.bot = bot
        self._cache = OrderedDict()
        self._locks = {}  # One per active Discord guild, not per arbitrary API UUID.
        self._owners = (0, set())

    def client(self, identifier=""):
        server = {"ptero": {"url": os.getenv("PTERO_URL", ""), "apiKey": os.getenv("PTERO_API_KEY", ""), "serverId": identifier}}
        return PterodactylClient(server, session=self.bot._ptero_session, limiter=self.bot._ptero_limiter)

    async def is_owner(self, actor_id):
        checked, owners = self._owners
        if not checked or time.monotonic() - checked > 60:
            app = await self.bot.application_info()
            owners = {app.owner.id} if app.owner else set()
            if app.team:
                owners.add(app.team.owner_id)
            self._owners = (time.monotonic(), owners)
        return actor_id in owners

    def authorized(self, guild_id, uuid):
        return any(item["server_uuid"] == uuid for item in self.bot.store.server_authorizations(guild_id))

    async def authorize(self, actor_id, guild_id, server, *, revoke=False):
        if not await self.is_owner(actor_id):
            raise SetupError(t("Somente o proprietário do aplicativo pode autorizar servidores para uma comunidade."))
        if revoke:
            self.bot.store.revoke_server_authorization(guild_id, server["uuid"], actor_id)
        else:
            try:
                self.bot.store.authorize_server(guild_id, server, actor_id)
            except (sqlite3.IntegrityError, ValueError) as exc:
                raise SetupError(t("Este servidor já está autorizado ou vinculado a outro Discord. Revise o mapeamento existente antes de transferir.")) from exc
        self._cache.clear()

    async def catalog(self, actor_id, guild_id, *, page=1, search="", refresh=False):
        owner = await self.is_owner(actor_id)
        grants = self.bot.store.server_authorizations(guild_id)
        binding = self.bot.store.get_binding(guild_id)
        allowed = {item["server_uuid"]: item["identifier"] for item in grants}
        if binding:
            allowed[binding["uuid"]] = binding["identifier"]
        if not owner and not allowed:
            return {"ok": True, "servers": [], "page": 1, "pages": 1}
        key = (actor_id, guild_id, page, search, tuple(allowed))
        cached = self._cache.get(key)
        if not refresh and cached and time.monotonic() - cached[0] < 30:
            return cached[1]
        async with self.client() as client:
            if owner:
                result = check(await client.list_servers(page=page, search=search))
            else:
                # Never expose names or counts of other customers to a guild admin.
                servers = []
                for uuid, identifier in list(allowed.items())[:100]:
                    async with self.client(identifier) as selected:
                        response = check(await selected.request(""))
                    summary = client.server_summary(response.get("json") or {})
                    if summary["uuid"] == uuid and search.casefold() in summary["label"].casefold():
                        servers.append(summary)
                pages = max(1, (len(servers) + 24) // 25)
                page = min(max(1, page), pages)
                result = {"ok": True, "servers": servers[(page-1)*25:page*25], "page": page, "pages": pages}
        self._cache[key] = (time.monotonic(), result)
        while len(self._cache) > 64:
            self._cache.popitem(last=False)
        return result

    async def inspect(self, guild_id, server, *, actor_id):
        if not await self.is_owner(actor_id) and not self.authorized(guild_id, server["uuid"]):
            binding = self.bot.store.get_binding(guild_id)
            if not binding or binding["uuid"] != server["uuid"]:
                raise SetupError(t("Este servidor não está autorizado para este Discord."))
        async with self.client(server["identifier"]) as client:
            info = check(await client.request(""))
            summary = client.server_summary(info.get("json") or {})
        if summary["uuid"] != server["uuid"] or summary["identifier"] != server["identifier"]:
            raise SetupError(t("O ID e o UUID retornados pela API não correspondem ao servidor selecionado."))
        # No filesystem reads until independent grant, even for an operator's picker.
        if not self.authorized(guild_id, summary["uuid"]):
            return {"server": summary, "lua": [], "authorized": False}
        paths = await asyncio.to_thread(self.local_paths, summary)
        return {"server": summary, "lua": paths, "authorized": True}

    @staticmethod
    def local_paths(server):
        from pathlib import Path
        from uuid import UUID
        root = Path(os.getenv("PTERO_VOLUMES_ROOT", "/var/lib/pterodactyl/volumes")).resolve()
        expected = root / str(UUID(server["uuid"]))
        if expected.is_symlink() or expected.resolve() != expected or not expected.is_relative_to(root):
            raise SetupError(t("Volume Wings inseguro: caminho externo ou link simbólico."))
        if not expected.is_dir():
            raise SetupError(t("Volume ausente neste node Wings. Servidores remotos precisam de um volume montado com segurança neste dedicado."))
        candidates = lua_candidates(expected)
        if not candidates:
            raise SetupError(t("Pasta Lua não encontrada ou sem permissão de leitura neste volume."))
        return candidates

    async def auto_bind(self, actor_id, guild_id, server, lua):
        lock = self._locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            if not self.authorized(guild_id, server["uuid"]):
                raise SetupError(t("Vínculo automático exige autorização independente do proprietário do aplicativo. Use o fluxo manual enquanto aguarda."))
            owner = self.bot.store.binding_owner(server["uuid"])
            previous = self.bot.store.get_binding(guild_id)
            if (owner is not None and owner != guild_id) or (previous and previous["uuid"] != server["uuid"]):
                raise SetupError(t("Servidor ou Discord já possui outro vínculo. O operador precisa revisar a associação."))
            inspection = await self.inspect(guild_id, server, actor_id=actor_id)
            if lua not in inspection["lua"]:
                raise SetupError(t("Selecione uma pasta Lua detectada neste volume."))
            binding = normalize_binding(server["uuid"][:8], server["uuid"], server["label"], lua)
            binding["identifier"] = inspection["server"]["identifier"]
            if previous:
                binding["connect_address"] = previous.get("connect_address", "")
            await asyncio.to_thread(resolve_volume, binding)
            async with self.client(binding["identifier"]) as client:
                # Read the directory, not a blind write after any contents error.
                listing = check(await client.list_files("/"))
                exists = any(item["name"] == PROOF[1:] for item in listing["files"])
                pending = self.bot.store.pending_binding(guild_id)
                if exists:
                    proof = check(await client.read_file(PROOF))
                    if not pending or pending["expires"] <= time.time() or pending["binding"] != binding or proof.get("text", "").strip() != pending["token"]:
                        raise SetupError(t("O arquivo de vínculo já existe. Ele foi preservado. Valide o código pendente ou revise o arquivo pelo gerenciador do Pterodactyl."))
                else:
                    if not self.authorized(guild_id, server["uuid"]):
                        raise SetupError(t("A autorização foi revogada antes da conclusão."))
                    pending = {"binding": binding, "token": f"friendhost:{guild_id}:{server['uuid']}:{secrets.token_urlsafe(24)}", "expires": time.time() + 900, "automatic": True}
                    self.bot.store.pending_binding(guild_id, pending)
                    check(await client.write_file(PROOF, pending["token"]))
                proof = check(await client.read_file(PROOF))
                if proof.get("text", "").strip() != pending["token"] or pending["expires"] <= time.time():
                    raise SetupError(t("Não foi possível confirmar o arquivo temporário. Nenhum vínculo foi instalado."))
            if not self.authorized(guild_id, server["uuid"]):
                raise SetupError(t("A autorização foi revogada antes da conclusão."))
            await self.bot.install_binding(guild_id, binding)
            self.bot.store.audit_binding(guild_id, server["uuid"], actor_id, "automatic_binding")
            return binding
