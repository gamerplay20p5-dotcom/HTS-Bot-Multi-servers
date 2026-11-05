from __future__ import annotations

import asyncio
import sqlite3
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from manguadu.config_store import ConfigStore
from manguadu.credentials import decrypt_password, encrypt_password
from manguadu.lore import evaluate_lore
from manguadu.rcon import _packet, adduser_command, generate_password, send_rcon_command


def test_ticket_and_whitelist_state_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "bot.sqlite3"
    store = ConfigStore(path)
    store.set_value(11, "channels", "ticket_panel", 123)
    store.set_value(11, "channels", "wl_decisions", 456)
    store.set_value(11, "wl", "auto_approve", True)
    store.create_ticket(11, 22, 33)
    with pytest.raises(sqlite3.IntegrityError):
        store.create_ticket(11, 22, 34)
    secret = "token-internal-test"
    ciphertext = encrypt_password("SenhaEscolhida123!", secret)
    store.save_request(33, 11, 22, "Jogador", "Sobrevivente", "Minha lore", 0.4, "review", ciphertext)
    assert "SenhaEscolhida123!" not in path.read_bytes().decode("latin1")
    store.close()

    store = ConfigStore(path)
    assert store.get_settings(11)["channels"]["ticket_panel"] == 123
    assert store.get_settings(11)["channels"]["wl_decisions"] == 456
    assert store.get_settings(11)["wl"]["auto_approve"]
    assert store.get_open_ticket(11, 22)["channel_id"] == 33
    assert decrypt_password(store.get_request(33)["password_ciphertext"], secret) == "SenhaEscolhida123!"
    store.clear_request_password(33)
    assert store.get_request(33)["password_ciphertext"] == ""
    store.record_whitelist(11, "Jogador", 22, 33)
    assert store.username_owner(11, "jogador") == 22
    store.close_ticket(33)
    assert store.get_open_ticket(11, 22) is None
    store.close()


def test_latest_wl_request_can_be_loaded_by_pz_username(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "bot.sqlite3")
    store.save_request(33, 11, 22, "Jogador", "Primeiro", "Lore antiga", 0.4, "rejected")
    store.save_request(34, 11, 23, "jogador", "Segundo", "Lore mais recente", 0.8, "approved")
    store.save_request(35, 12, 24, "Jogador", "Outro servidor", "Lore de outra guild", 0.8, "approved")

    request = store.get_latest_request_by_username(11, "JOGADOR")
    assert request is not None
    assert request["channel_id"] == 34
    assert request["lore"] == "Lore mais recente"
    assert store.get_latest_request_by_username(99, "Jogador") is None
    store.close()


def test_lore_export_files_preserve_utf8_when_split() -> None:
    from manguadu.discord_bot import _make_lore_files

    lore = "á🙂café\n" * 20
    files = _make_lore_files(lore, "Jogador de Teste", max_file_bytes=32)
    try:
        assert len(files) > 1
        assert all(file.filename.endswith(".txt") for file in files)
        assert b"".join(file.fp.read() for file in files).decode("utf-8") == lore
    finally:
        for file in files:
            file.close()


def test_lore_triage_explains_overlap_and_missing_context() -> None:
    reference = "Knox ficou isolada após a epidemia. Muldraugh perdeu comunicação, Rosewood virou abrigo, sobreviventes buscaram água e remédios."
    good = "Meu personagem saiu de Muldraugh quando a epidemia isolou Knox. Em Rosewood, procurou abrigo para sobreviventes e reuniu água e remédios com sua irmã antes do inverno chegar."
    bad = "Meu personagem nasceu em um reino mágico de dragões e cavaleiros. Sua missão era encontrar uma espada dourada escondida em um castelo e defender os portões de uma invasão de monstros antigos."
    accepted = evaluate_lore(reference, good)
    rejected = evaluate_lore(reference, bad)
    assert accepted.approved and "muldraugh" in accepted.common_terms
    assert not rejected.approved and "não traz referências" in rejected.reason


def test_adduser_rejects_console_injection_and_generated_password() -> None:
    password = generate_password()
    assert adduser_command("Malaio_01", password) == f'adduser "Malaio_01" "{password}"'
    assert adduser_command("Malaio_01", "SenhaEscolhida123!")
    with pytest.raises(ValueError):
        adduser_command('nick";stop', password)
    with pytest.raises(ValueError):
        adduser_command("Jogador", 'senha" stop')


def test_rcon_auth_and_multipart_adduser_response() -> None:
    async def scenario() -> None:
        commands = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                size = struct.unpack("<i", await reader.readexactly(4))[0]
                payload = await reader.readexactly(size)
                auth_id, packet_type = struct.unpack("<ii", payload[:8])
                assert packet_type == 3 and payload[8:-2] == b"secret"
                writer.write(_packet(auth_id, 0, "") + _packet(auth_id, 2, ""))
                await writer.drain()
                size = struct.unpack("<i", await reader.readexactly(4))[0]
                payload = await reader.readexactly(size)
                command_id, packet_type = struct.unpack("<ii", payload[:8])
                assert packet_type == 2
                commands.append(payload[8:-2].decode())
                size = struct.unpack("<i", await reader.readexactly(4))[0]
                payload = await reader.readexactly(size)
                delimiter_id, delimiter_type = struct.unpack("<ii", payload[:8])
                assert delimiter_id == command_id and delimiter_type == 0 and payload[8:-2] == b""
                writer.write(
                    _packet(command_id, 0, "User ")
                    + _packet(command_id, 0, "added")
                    + _packet(command_id, 0, "\x00\x01\x00\x00")
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            result = await send_rcon_command("127.0.0.1", port, "secret", 'adduser "Jogador" "Senha123456"')
        finally:
            server.close()
            await server.wait_closed()
        assert result.ok and result.output == "User added"
        assert commands == ['adduser "Jogador" "Senha123456"']

    asyncio.run(scenario())


def test_rcon_reports_password_rejection_without_exposing_secret() -> None:
    async def scenario() -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                size = struct.unpack("<i", await reader.readexactly(4))[0]
                payload = await reader.readexactly(size)
                auth_id, _ = struct.unpack("<ii", payload[:8])
                writer.write(_packet(-1, 2, ""))
                await writer.drain()
                assert auth_id != -1
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            result = await send_rcon_command("127.0.0.1", port, "dont-print-this", "players")
        finally:
            server.close()
            await server.wait_closed()
        assert not result.ok
        assert result.stage == "autenticacao"
        assert "Senha RCON recusada" in result.error
        assert "dont-print-this" not in result.error

    asyncio.run(scenario())


def test_rcon_accepts_single_response_when_server_closes_without_sentinel() -> None:
    async def scenario() -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                size = struct.unpack("<i", await reader.readexactly(4))[0]
                auth_payload = await reader.readexactly(size)
                auth_id, _ = struct.unpack("<ii", auth_payload[:8])
                writer.write(_packet(auth_id, 2, ""))
                await writer.drain()

                size = struct.unpack("<i", await reader.readexactly(4))[0]
                command_payload = await reader.readexactly(size)
                command_id, _ = struct.unpack("<ii", command_payload[:8])
                size = struct.unpack("<i", await reader.readexactly(4))[0]
                await reader.readexactly(size)  # pacote vazio de delimitacao
                writer.write(_packet(command_id, 0, "Players connected: 1"))
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            result = await send_rcon_command("127.0.0.1", port, "secret", "players")
        finally:
            server.close()
            await server.wait_closed()

        assert result.ok
        assert result.output == "Players connected: 1"

    asyncio.run(scenario())


def test_decisions_go_to_configured_discord_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from manguadu import discord_bot

    store = ConfigStore(":memory:")
    store.set_value(11, "channels", "wl_decisions", 456)
    bot = discord_bot.ManguaduBot(store)
    sent = []

    class Channel:
        async def send(self, *, embed: object, allowed_mentions: object) -> None:
            sent.append(embed)

    channel = Channel()
    monkeypatch.setattr(discord_bot, "_configured_text_channel", lambda _guild, channel_id: channel if channel_id == 456 else None)
    guild = SimpleNamespace(id=11)
    request = {"user_id": 22, "username": "Jogador", "character_name": "Sobrevivente", "channel_id": 33, "lore": "História de teste"}

    async def scenario() -> None:
        await bot.post_decision(guild, request, True, "Lore coerente")
        await bot.post_decision(guild, request, False, "Lore incompatível")

    asyncio.run(scenario())
    assert len(sent) == 2
    assert sent[0].title == "✅ WL aprovada"
    assert sent[1].title == "❌ WL reprovada"
    assert sent[0].fields[2].value == "Lore coerente"
    assert sent[1].fields[2].value == "Lore incompatível"
    store.close()


def test_seed_legacy_guild_settings_imports_valid_channel_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    from manguadu.discord_bot import ManguaduBot

    store = ConfigStore(":memory:")
    bot = ManguaduBot(store)
    monkeypatch.setenv("GUILD_ID", "11")
    monkeypatch.setenv("ID_CANAL_ADMIN", "123456")
    monkeypatch.setenv("ID_CANAL_EVOLUCAO", " 234567 ")
    monkeypatch.setenv("ID_CANAL_STATS", "invalid")
    monkeypatch.setenv("ID_CANAL_RANKING", "")

    bot._seed_legacy_guild_settings(11)

    channels = store.get_settings(11)["channels"]
    assert channels["admin"] == 123456
    assert channels["evolution"] == 234567
    assert channels["stats"] is None
    assert channels["ranking"] is None
    bot._seed_legacy_guild_settings(22)
    assert store.get_settings(22)["channels"]["admin"] is None
    store.close()


def test_discord_views_fit_rows_and_public_buttons_persist() -> None:
    from manguadu.discord_bot import (
        ManguaduBot, PublicTicketView, ReviewView, TicketActionView,
        WhitelistChannelsView, WhitelistConfigView,
    )

    store = ConfigStore(":memory:")
    bot = ManguaduBot(store)
    commands = bot.tree.get_commands()
    assert {command.name for command in commands} == {
        "config", "condig", "config_bot", "config_api", "config_lore", "config_kick_automatico", "wl", "bot", "online", "info", "skills", "traits", "rank",
        "localizar_veiculo", "mapas", "gps", "satelite", "wipe_zeds", "wipe", "wipe_force",
        "wipe_teste", "wipe_chunk", "wipe_chunk_force", "wipe_chunk_teste",
        "rcon", "servidor", "status", "statuscomplete", "logs", "safehouse",
        "adduser", "removeuserfromwhitelist", "banid", "kick", "kick_all",
        "godmode", "invisible", "grantadmin", "removeadmin", "tpto", "tp",
        "servermsg", "deletearquivo", "painel", "automacao",
    }
    wl_group = next(command for command in commands if command.name == "wl")
    assert {command.name for command in wl_group.commands} == {"lore", "puxar_lore"}
    assert all(command.to_dict(bot.tree)["name"] == command.name for command in commands)
    assert len(WhitelistConfigView(bot, 11).to_components()) <= 5
    assert len(WhitelistChannelsView(bot, 11).to_components()) <= 5
    assert all(view.is_persistent() for view in (PublicTicketView(bot), TicketActionView(bot), ReviewView(bot)))
    store.close()


def test_audit_requires_private_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from manguadu import discord_bot

    guild = SimpleNamespace(default_role=object())

    class Channel:
        def __init__(self, public: bool):
            self.public = public

        def permissions_for(self, _role: object) -> SimpleNamespace:
            return SimpleNamespace(view_channel=self.public)

    monkeypatch.setattr(discord_bot, "_configured_text_channel", lambda _guild, _id: Channel(public=False))
    assert discord_bot._configured_private_channel(guild, 1) is not None
    monkeypatch.setattr(discord_bot, "_configured_text_channel", lambda _guild, _id: Channel(public=True))
    assert discord_bot._configured_private_channel(guild, 1) is None


def test_approved_lore_is_attached_to_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    from manguadu import discord_bot

    store = ConfigStore(":memory:")
    store.set_value(11, "channels", "wl_audit", 456)
    store.set_value(11, "wl", "require_lore", False)
    bot = discord_bot.ManguaduBot(store)
    sent = []

    class Channel:
        async def send(self, **kwargs: object) -> None:
            sent.append(kwargs)

    channel = Channel()
    monkeypatch.setattr(discord_bot, "_configured_private_channel", lambda _guild, channel_id: channel if channel_id == 456 else None)
    guild = SimpleNamespace(id=11, filesize_limit=1024)
    approved = {
        "status": "approved", "user_id": 22, "username": "Jogador", "character_name": "Sobrevivente",
        "channel_id": 33, "lore": "Lore aprovada com conteúdo completo.",
    }
    rejected = {**approved, "status": "rejected"}

    async def scenario() -> None:
        await bot.post_audit(guild, approved, "Aprovada", "A equipe aceitou a WL.")
        await bot.post_audit(guild, rejected, "Reprovada", "A lore não foi aceita.")

    asyncio.run(scenario())
    assert len(sent) == 2
    file = sent[0]["file"]
    assert file.filename == "lore-Jogador.txt"
    assert file.fp.read().decode("utf-8") == approved["lore"]
    assert "file" not in sent[1]
    file.close()
    store.close()
