import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from manguadu.config_store import ConfigStore
from manguadu.server_setup import ServerSetup, SetupError, PROOF
from manguadu.pterodactyl import PterodactylClient
from manguadu.tenant_scope import activate


@pytest.fixture(autouse=True)
def clear_scope():
    activate(None)
    yield
    activate(None)


def server(name="Runners"):
    uuid = str(uuid4())
    return {"uuid": uuid, "identifier": uuid[:8], "label": name, "node": "Node0", "allocations": [], "description": "", "status": None, "suspended": False}


class FakeClient:
    server_summary = staticmethod(PterodactylClient.server_summary)

    def __init__(self, selected):
        self.selected = selected
        self.files = {}
        self.writes = []
        self.status = 200
        self.write_status = 204
        self.request = AsyncMock(side_effect=self.info)

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass

    async def info(self, endpoint):
        return {"ok": self.status == 200, "status": self.status, "json": {"attributes": {
            **self.selected, "name": self.selected["label"], "invocation": "hidden-secret"}}}

    async def list_files(self, root):
        assert root == "/"
        return {"ok": self.status == 200, "status": self.status, "files": [{"name": key[1:]} for key in self.files]}

    async def read_file(self, path):
        return {"ok": path in self.files, "status": 200 if path in self.files else 404, "text": self.files.get(path, "")}

    async def write_file(self, path, content):
        self.writes.append((path, content))
        if self.write_status == 204:
            self.files[path] = content
        return {"ok": self.write_status == 204, "status": self.write_status}


@pytest.fixture
def setup_case(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.sqlite3")
    selected = server()
    root = tmp_path / "volumes"
    (root / selected["uuid"] / ".cache/Lua").mkdir(parents=True)
    monkeypatch.setenv("PTERO_VOLUMES_ROOT", str(root))
    async def install(guild_id, binding):
        store.save_binding(guild_id, binding)
    bot = SimpleNamespace(store=store, _ptero_session=None, _ptero_limiter=None,
                          application_info=AsyncMock(return_value=SimpleNamespace(owner=SimpleNamespace(id=99), team=None)), install_binding=AsyncMock(side_effect=install))
    service = ServerSetup(bot)
    client = FakeClient(selected)
    monkeypatch.setattr(service, "client", lambda identifier="": client)
    yield service, bot, client, selected
    store.close()


@pytest.mark.asyncio
async def test_client_list_uses_real_client_endpoint_pagination_partial_search_and_optional_fields():
    selected = server()
    client = PterodactylClient({"ptero": {"url": "https://panel.test", "apiKey": "secret", "serverId": ""}})
    client.request = AsyncMock(return_value={"ok": True, "json": {"data": [
        {"attributes": {"uuid": selected["uuid"], "identifier": selected["identifier"], "name": "Runners", "invocation": "secret"}},
        {"attributes": {"uuid": str(uuid4()), "identifier": "abcd1234", "name": "Runners"}},
    ], "meta": {"pagination": {"current_page": 2, "total_pages": 3}}}})
    response = await client.list_servers(page=2, search="Runn ers")
    path = client.request.call_args.args[0]
    assert "page=2" in path and "per_page=25" in path and "filter%5Bname%5D=Runn+ers" in path
    assert client.request.call_args.kwargs["client_root"] is True
    assert client.build_url(path, client_root=True).startswith("https://panel.test/api/client?")
    assert "admin" not in path
    assert response["page"] == 2 and response["pages"] == 3
    assert len(response["servers"]) == 2
    assert response["servers"][0]["uuid"] != response["servers"][1]["uuid"]
    assert "invocation" not in repr(response)
    assert response["servers"][0]["node"] == ""


@pytest.mark.asyncio
async def test_guild_admin_sees_no_global_catalog_without_grants(setup_case):
    service, bot, client, selected = setup_case
    assert (await service.catalog(10, 1))["servers"] == []
    client.request.assert_not_awaited()
    bot.install_binding.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_application_owner_can_grant_and_admin_catalog_filters_authorized_servers(setup_case):
    service, bot, client, selected = setup_case
    with pytest.raises(SetupError):
        await service.authorize(10, 1, selected)
    await service.authorize(99, 1, selected)
    result = await service.catalog(10, 1, search="runn")
    assert result["servers"][0]["uuid"] == selected["uuid"]
    assert (await service.catalog(10, 1, search="other"))["servers"] == []
    assert (await service.catalog(10, 2))["servers"] == []
    result["servers"][0]["uuid"] = "tampered"
    # UI data is rechecked against API and authorization before any write.
    with pytest.raises(SetupError):
        await service.auto_bind(10, 1, result["servers"][0], ".cache/Lua")


@pytest.mark.asyncio
async def test_auto_proof_requires_independent_authorization_even_for_app_owner(setup_case):
    service, bot, client, selected = setup_case
    for actor in (10, 99):
        with pytest.raises(SetupError):
            await service.auto_bind(actor, 1, selected, ".cache/Lua")
    assert client.writes == []
    bot.install_binding.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_proof_is_written_read_back_bound_audited_and_consumed(setup_case):
    service, bot, client, selected = setup_case
    await service.authorize(99, 1, selected)
    binding = await service.auto_bind(10, 1, selected, ".cache/Lua")
    assert binding["uuid"] == selected["uuid"]
    assert len(client.writes) == 1 and client.writes[0][0] == PROOF
    assert client.files[PROOF].startswith(f"friendhost:1:{selected['uuid']}:")
    assert bot.store.get_binding(1)["identifier"] == selected["identifier"]
    assert bot.store.pending_binding(1) is None
    assert bot.store._db.execute("SELECT count(*) FROM binding_audit").fetchone()[0] == 2
    with pytest.raises(SetupError):
        await service.auto_bind(10, 1, selected, ".cache/Lua")
    assert len(client.writes) == 1  # Consumed proof cannot be reused or overwritten.


@pytest.mark.asyncio
async def test_volume_granted_to_other_guild_cannot_be_claimed(setup_case):
    service, bot, client, selected = setup_case
    await service.authorize(99, 1, selected)
    with pytest.raises(SetupError):
        await service.auto_bind(10, 2, selected, ".cache/Lua")
    await service.auto_bind(10, 1, selected, ".cache/Lua")
    with pytest.raises(ValueError):
        await service.authorize(99, 2, selected)
    assert len(client.writes) == 1


@pytest.mark.asyncio
async def test_existing_foreign_proof_is_never_overwritten(setup_case):
    service, bot, client, selected = setup_case
    await service.authorize(99, 1, selected)
    client.files[PROOF] = "friendhost:another-guild:do-not-touch"
    with pytest.raises(SetupError):
        await service.auto_bind(10, 1, selected, ".cache/Lua")
    assert not client.writes
    assert client.files[PROOF].endswith("do-not-touch")
    bot.install_binding.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_proof_cannot_be_completed_or_rewritten(setup_case):
    service, bot, client, selected = setup_case
    await service.authorize(99, 1, selected)
    from manguadu.guild_runtime import normalize_binding
    binding = normalize_binding(selected["identifier"], selected["uuid"], selected["label"], ".cache/Lua")
    bot.store.pending_binding(1, {"binding": binding, "token": "old-code", "expires": time.time() - 1, "automatic": True})
    client.files[PROOF] = "old-code"
    with pytest.raises(SetupError):
        await service.auto_bind(10, 1, selected, ".cache/Lua")
    assert not client.writes


@pytest.mark.asyncio
async def test_failed_write_keeps_manual_fallback_and_never_installs_binding(setup_case):
    service, bot, client, selected = setup_case
    await service.authorize(99, 1, selected)
    client.write_status = 403
    with pytest.raises(SetupError, match="permissão"):
        await service.auto_bind(10, 1, selected, ".cache/Lua")
    assert bot.store.get_binding(1) is None
    assert bot.store.pending_binding(1)["automatic"]
    bot.install_binding.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_api_failure_does_not_create_proof(setup_case, status):
    service, bot, client, selected = setup_case
    await service.authorize(99, 1, selected)
    client.status = status
    with pytest.raises(SetupError):
        await service.auto_bind(10, 1, selected, ".cache/Lua")
    assert not client.writes


@pytest.mark.asyncio
async def test_modern_api_identifier_is_preserved_and_uuid_mismatch_rejected(setup_case):
    service, bot, client, selected = setup_case
    selected["identifier"] = "newIdentifier_123"
    await service.authorize(99, 1, selected)
    binding = await service.auto_bind(10, 1, selected, ".cache/Lua")
    assert binding["identifier"] == "newIdentifier_123"
    changed = {**selected, "uuid": str(uuid4())}
    with pytest.raises(SetupError):
        await service.inspect(1, changed, actor_id=99)


@pytest.mark.asyncio
async def test_unauthorized_inspection_never_reads_local_paths(setup_case, monkeypatch):
    service, bot, client, selected = setup_case
    def forbidden(*args): raise AssertionError("Unauthorized local read")
    monkeypatch.setattr(service, "local_paths", forbidden)
    assert not (await service.inspect(1, selected, actor_id=99))["authorized"]
    with pytest.raises(SetupError):
        await service.inspect(1, selected, actor_id=10)


@pytest.mark.asyncio
async def test_discord_diagnostics_report_actual_ttf_counts(setup_case, monkeypatch):
    from pathlib import Path
    import os
    from manguadu.guild_runtime import normalize_binding
    from manguadu.pz_data import PzData
    from manguadu.server_setup_ui import diagnose
    from manguadu.tenant_scope import GuildScope, use_scope
    service, bot, client, selected = setup_case
    binding = normalize_binding(selected["identifier"], selected["uuid"], selected["label"], ".cache/Lua")
    bot.store.save_binding(1, binding)
    bot.server_setup = service
    root = Path(os.getenv("PTERO_VOLUMES_ROOT")) / selected["uuid"] / ".cache/Lua/ttf_stats_mp"
    root.mkdir()
    (root / "index.txt").write_text("character_1|Malaio|Aaron|Hadden\n", encoding="utf-8")
    (root / "character_1.txt").write_text("version=1\ndeleted=false\ncharacterID=character_1\nusername=Malaio\nisDead=true\nlastSeenKills=10\ngameTimeSeconds=7200\n", encoding="utf-8")
    bot._runtimes = {1: SimpleNamespace(data=PzData(root))}
    with use_scope(GuildScope(1, server={}, environment={"PTERO_VOLUME_ID": selected["uuid"]})):
        embed = await diagnose(bot, 1)
    assert "Pterodactyl: conectado" in embed.description
    assert "Fonte: TTF Stats MP" in embed.description
    assert "Contas: 1" in embed.description and "Personagens: 1" in embed.description
    assert "Registros inválidos: 0" in embed.description
    assert "hourssurvived" in embed.description


@pytest.mark.asyncio
async def test_other_wings_node_cannot_be_bound_locally(setup_case, monkeypatch, tmp_path):
    service, bot, client, selected = setup_case
    await service.authorize(99, 1, selected)
    monkeypatch.setenv("PTERO_VOLUMES_ROOT", str(tmp_path / "different-node"))
    with pytest.raises(SetupError, match="node Wings"):
        await service.auto_bind(10, 1, selected, ".cache/Lua")
    assert not client.writes


@pytest.mark.asyncio
async def test_revoke_authorization_hides_server_and_blocks_automatic_write(setup_case):
    service, bot, client, selected = setup_case
    await service.authorize(99, 1, selected)
    await service.catalog(10, 1)
    await service.authorize(99, 1, selected, revoke=True)
    assert (await service.catalog(10, 1))["servers"] == []
    with pytest.raises(SetupError):
        await service.auto_bind(10, 1, selected, ".cache/Lua")


@pytest.mark.asyncio
async def test_interrupted_install_can_resume_same_unexpired_proof_without_rewrite(setup_case):
    service, bot, client, selected = setup_case
    await service.authorize(99, 1, selected)
    original = bot.install_binding.side_effect
    bot.install_binding.side_effect = ValueError("temporary local read failure")
    with pytest.raises(ValueError):
        await service.auto_bind(10, 1, selected, ".cache/Lua")
    assert bot.store.pending_binding(1)
    assert len(client.writes) == 1
    bot.install_binding.side_effect = original
    await service.auto_bind(10, 1, selected, ".cache/Lua")
    assert len(client.writes) == 1
    assert bot.store.get_binding(1)


@pytest.mark.asyncio
async def test_readback_mismatch_never_installs_binding(setup_case, monkeypatch):
    service, bot, client, selected = setup_case
    await service.authorize(99, 1, selected)
    monkeypatch.setattr(client, "read_file", AsyncMock(return_value={"ok": True, "text": "wrong"}))
    with pytest.raises(SetupError):
        await service.auto_bind(10, 1, selected, ".cache/Lua")
    assert bot.store.get_binding(1) is None


def test_grant_and_binding_conflicts_survive_database_reopen(tmp_path):
    path = tmp_path / "config.sqlite3"
    store = ConfigStore(path)
    selected = server()
    store.authorize_server(1, selected, 99)
    store.close()
    store = ConfigStore(path)
    assert store.server_authorizations(1)[0]["server_uuid"] == selected["uuid"]
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store.save_binding(2, selected)
    store.save_binding(1, selected)
    store.close()


@pytest.mark.asyncio
async def test_revoked_automatic_proof_cannot_use_manual_validation_button(setup_case):
    from manguadu.guild_ui import ServerConfigView
    service, bot, client, selected = setup_case
    bot.server_setup = service
    bot.store.pending_binding(1, {"binding": selected, "token": "still-present", "expires": time.time()+900, "automatic": True})
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()))
    await ServerConfigView(bot, 1).verify.callback(interaction)
    interaction.response.defer.assert_not_awaited()
    bot.install_binding.assert_not_awaited()
    assert "revogada" in interaction.response.send_message.call_args.args[0]


@pytest.mark.asyncio
async def test_discord_views_respect_component_limits_and_preserve_duplicate_labels(setup_case, monkeypatch):
    from manguadu.server_setup_ui import ServerPicker, SelectedServer, ProviderView
    from manguadu.guild_ui import ServerConfigView
    service, bot, client, selected = setup_case
    bot.server_setup = service
    duplicates = [server("Same name") for _ in range(25)]
    monkeypatch.setattr(service, "catalog", AsyncMock(return_value={"servers": duplicates, "page": 1, "pages": 2}))
    interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
    picker = ServerPicker(bot, 1, 99)
    await picker.load(interaction)
    assert len(picker.selector.options) == 25
    assert len(set(item.value for item in picker.selector.options)) == 25
    assert len(set(item.description for item in picker.selector.options)) == 25
    assert not picker.next_page.disabled
    for view in (picker, SelectedServer(bot, 1, 99, selected), ProviderView(bot, 1, 99), ServerConfigView(bot, 1)):
        assert len(view.children) <= 25
        assert all(len(getattr(item, "label", "") or "") <= 80 for item in view.children)


@pytest.mark.asyncio
async def test_real_http_client_root_list_and_file_write_routes():
    from aiohttp import web
    selected = server()
    seen = []
    async def listing(request):
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.query["page"] == "1"
        assert request.query["filter[name]"] == "Run"
        return web.json_response({"data": [{"attributes": {**selected, "name": selected["label"]}}]})
    async def write(request):
        seen.append((request.query["file"], await request.text()))
        return web.Response(status=204)
    app = web.Application()
    app.router.add_get("/api/client", listing)
    app.router.add_post("/api/client/servers/{server}/files/write", write)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with PterodactylClient({"ptero": {"url": f"http://127.0.0.1:{port}", "apiKey": "test-key", "serverId": selected["identifier"]}}) as client:
            assert (await client.list_servers(search="Run"))["servers"][0]["uuid"] == selected["uuid"]
            assert (await client.write_file(PROOF, "test-code"))["ok"]
        assert seen == [(PROOF, "test-code")]
    finally:
        await runner.cleanup()
