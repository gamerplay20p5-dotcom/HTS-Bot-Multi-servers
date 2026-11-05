from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from manguadu.config_store import ConfigStore
from manguadu.guild_runtime import GuildRuntime, normalize_binding
from manguadu.panel_player_links import get_player_link, set_player_link
from manguadu.panel_theme import get_saved_theme, save_draft_theme, update_draft_theme
from manguadu.server_registry import ServerRegistry
from manguadu.tenant_scope import GuildScope, activate, getenv, use_scope
from manguadu.work_queue import StaggeredQueue, phase
from manguadu.utils import read_latest_csv_row


@pytest.fixture(autouse=True)
def reset_scope():
    activate(None)
    yield
    activate(None)


def make_binding(root: Path, index: int):
    uuid = str(uuid4())
    lua = root / uuid / ".cache" / "Lua"
    player = lua / "FriendHost_Data" / "Jogadores" / "Menta"
    player.mkdir(parents=True)
    (player / "player_Menta.txt").write_text(f"username;zombiekills\nMenta;{index}\n", encoding="utf-8")
    server = lua.parent / "Server"
    server.mkdir()
    (server / "pz.ini").write_text(f"RCONPort={27000 + index}\nRCONPassword=secret-{index}\n", encoding="utf-8")
    return normalize_binding(uuid[:8], uuid, f"Servidor {index}", ".cache/Lua")


def test_25_concurrent_guilds_never_share_data_or_state(tmp_path, monkeypatch):
    from manguadu.discord_bot import ManguaduBot
    from manguadu import tenant_scope

    root = tmp_path / "volumes"
    monkeypatch.setenv("PTERO_VOLUMES_ROOT", str(root))
    monkeypatch.setenv("PTERO_API_KEY", "shared-test-key")
    monkeypatch.setenv("CSV_BASE_PATH", "legacy-must-not-leak")
    monkeypatch.setattr(tenant_scope, "DATA_ROOT", tmp_path / "state")
    store = ConfigStore(tmp_path / "config.sqlite3")
    bot = ManguaduBot(store)
    bindings = {index: make_binding(root, index) for index in range(1, 26)}
    for index, binding in bindings.items():
        store.save_binding(index, binding)
        bot._runtimes[index] = GuildRuntime(bot, index, binding)

    async def check(index):
        with use_scope(bot.scope_for(index)):
            server = ServerRegistry().get_default_server()
            assert server["ptero"]["apiKey"] == "shared-test-key"
            assert server["ptero"]["serverId"] == bindings[index]["identifier"]
            assert server["rcon"]["password"] == f"secret-{index}"
            assert getenv("CSV_BASE_PATH") != "legacy-must-not-leak"
            entry = await asyncio.to_thread(bot.pz_data.find_player, "Menta")
            assert entry["row"]["zombiekills"] == str(index)
            foreign = root / bindings[index % 25 + 1]["uuid"] / ".cache" / "Lua" / "FriendHost_Data" / "Jogadores" / "Menta" / "player_Menta.txt"
            assert read_latest_csv_row(foreign) is None
            set_player_link("Menta", index + 1000)
            update_draft_theme({"title": f"Tema {index}"})
            save_draft_theme()
            await asyncio.sleep(0)
            assert get_player_link("Menta") == str(index + 1000)
            assert get_saved_theme()["title"] == f"Tema {index}"
            assert str(index) in str(bot.automations.path.parent)

    async def run():
        await asyncio.gather(*(check(index) for index in bindings))

    asyncio.run(run())
    assert len({str(runtime.anticheat.state_path) for runtime in bot._runtimes.values()}) == 25
    store.close()


def test_unbound_guild_cannot_fall_back_to_environment(monkeypatch):
    monkeypatch.setenv("CSV_BASE_PATH", "/other/customer")
    monkeypatch.setenv("RCON_PASSWORD", "must-not-use")
    with use_scope(GuildScope(123)):
        assert getenv("CSV_BASE_PATH") is None
        assert getenv("RCON_PASSWORD", "") == ""
        assert ServerRegistry().get_server() is None
        with pytest.raises(RuntimeError):
            ServerRegistry().get_default_server()


def test_volume_can_only_belong_to_one_discord(tmp_path):
    store = ConfigStore(tmp_path / "config.sqlite3")
    binding = {"uuid": str(uuid4()), "identifier": "test"}
    store.save_binding(11, binding)
    with pytest.raises(sqlite3.IntegrityError):
        store.save_binding(22, binding)
    assert store.get_binding(11) == binding
    assert store.get_binding(22) is None
    store.close()


def test_binding_rejects_traversal_and_mismatched_id():
    uuid = str(uuid4())
    for path in ("../other/Lua", "/root/Lua", "C:/Users/Lua"):
        with pytest.raises(ValueError):
            normalize_binding(uuid[:8], uuid, "PZ", path)
    with pytest.raises(ValueError):
        normalize_binding("deadbeef", uuid, "PZ", ".cache/Lua")


def test_staggered_queue_bounds_concurrency_and_deduplicates():
    async def run():
        queue = StaggeredQueue(workers=3, capacity=100)
        active, peak, seen = 0, 0, []

        async def callback(index):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(0.005)
            seen.append(index)
            active -= 1

        for index in range(25):
            async def task(index=index):
                await callback(index)
            queue.add(f"{index}:panels", 600, task)
        phases = [phase(f"{index}:panels", 600) for index in range(25)]
        assert len(set(phases)) == 25
        assert max(phases) - min(phases) > 300
        queue.enqueue_due(10**12)
        queue.enqueue_due(10**12)
        assert queue.queue.qsize() == 25
        workers = [asyncio.create_task(queue._worker()) for _ in range(3)]
        try:
            await asyncio.wait_for(queue.queue.join(), timeout=5)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        assert peak <= 3
        assert sorted(seen) == list(range(25))
        assert all(not job.pending for job in queue.jobs.values())

    asyncio.run(run())


def test_watcher_sends_only_to_the_active_discord(monkeypatch):
    from manguadu import watchers

    class Channel:
        def __init__(self):
            self.sent = []
        async def send(self, content):
            self.sent.append(content)

    channels = {11: Channel(), 22: Channel()}
    guilds = [SimpleNamespace(id=index, get_channel=lambda channel_id, index=index: channels[index]) for index in channels]
    bot = SimpleNamespace(guilds=guilds, store=SimpleNamespace(get_settings=lambda _: {"channels": {"evolution": 1}}))
    monkeypatch.setattr(watchers, "discord", SimpleNamespace(TextChannel=Channel, HTTPException=Exception))

    async def run():
        with use_scope(GuildScope(11)):
            await watchers.FriendHostWatcher(bot)._send_evolution("evento do servidor 11")

    asyncio.run(run())
    assert channels[11].sent == ["evento do servidor 11"]
    assert channels[22].sent == []


def test_claim_requires_matching_uuid_and_proof(tmp_path, monkeypatch):
    from manguadu import guild_ui

    store = ConfigStore(tmp_path / "config.sqlite3")
    uuid = str(uuid4())
    binding = normalize_binding(uuid[:8], uuid, "PZ", ".cache/Lua")
    import time
    store.pending_binding(11, {"binding": binding, "token": "expected-code", "expires": time.time() + 900})
    bot = SimpleNamespace(store=store, _ptero_session=None, _ptero_limiter=None, install_binding=AsyncMock())
    proof = {"text": "wrong-code"}

    class Client:
        def __init__(self, server, **kwargs):
            assert server["ptero"]["serverId"] == binding["identifier"]
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def request(self, path):
            return {"ok": True, "json": {"attributes": {"uuid": uuid}}}
        async def read_file(self, path):
            assert path == "/friendhost-discord-link.txt"
            return {"ok": True, **proof}

    monkeypatch.setattr(guild_ui, "PterodactylClient", Client)
    interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())

    async def run():
        view = guild_ui.ServerConfigView(bot, 11)
        await view.verify.callback(interaction)
        bot.install_binding.assert_not_awaited()
        proof["text"] = "expected-code"
        await view.verify.callback(interaction)
        bot.install_binding.assert_awaited_once_with(11, binding)

    asyncio.run(run())
    store.close()


def test_log_utf8_character_can_span_two_read_batches(tmp_path, monkeypatch):
    from manguadu import watchers
    monkeypatch.setattr(watchers, "LOG_READ_BYTES", 5)
    path = tmp_path / "server_chat.txt"
    path.write_text("abc🙂\nnext\n", encoding="utf-8")
    watcher = watchers.FriendHostWatcher(SimpleNamespace())

    async def run():
        lines = []
        for _ in range(5):
            lines.extend(await watcher._read_new_lines(path))
        assert lines == ["abc🙂", "next"]

    asyncio.run(run())


def test_log_queue_continues_while_server_control_is_waiting():
    async def run():
        control = StaggeredQueue(workers=1)
        logs = StaggeredQueue(workers=1)
        entered, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def slow_server():
            entered.set()
            await release.wait()

        async def read_log():
            delivered.set()

        control.add("11:kick", 10, slow_server)
        logs.add("22:logs", 5, read_log)
        control.trigger("11:kick")
        logs.trigger("22:logs")
        tasks = [asyncio.create_task(control.run()), asyncio.create_task(logs.run())]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(delivered.wait(), 1)
            assert not release.is_set()
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_death_cursor_survives_restart_and_ranking_does_not_consume_notifications(tmp_path):
    from manguadu.pz_data import PzData
    root = tmp_path / "data"
    server = root / "Servidor"
    server.mkdir(parents=True)
    deaths = server / "deaths.txt"
    deaths.write_text("date;time;gametime;steamid;username\n1;2;3;4;Menta\n", encoding="utf-8")
    state = tmp_path / "counts.json"
    original = PzData(root, death_state_path=state)
    assert original.death_counts() == {"Menta": 1}
    restarted = PzData(root, death_state_path=state)
    assert restarted.death_counts() == {"Menta": 1}
    with deaths.open("a", encoding="utf-8") as stream:
        stream.write("1;2;3;4;Menta\n")
    assert restarted.death_counts() == {"Menta": 2}
    assert len(restarted.death_snapshot()["events"]) == 1
    assert restarted.death_snapshot()["events"] == []


def test_long_lore_form_holds_a_disk_path_and_deletes_expired_draft(tmp_path, monkeypatch):
    from manguadu import tenant_scope
    from manguadu.discord_bot import LoreUploadDraftView
    from manguadu.lore_drafts import save_draft

    monkeypatch.setattr(tenant_scope, "DATA_ROOT", tmp_path)
    with use_scope(GuildScope(11)):
        path = save_draft("história longa " * 100000)
    assert path.stat().st_size < 20000

    async def run():
        view = LoreUploadDraftView(None, {}, path)
        assert isinstance(view.lore_text, Path)
        await view.on_timeout()
        assert not path.exists()

    asyncio.run(run())
