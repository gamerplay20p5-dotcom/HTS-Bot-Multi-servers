from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from manguadu.config_store import ConfigStore
from manguadu.i18n import CATALOG, HTSTranslator, mark_command_strings, t
from manguadu.tenant_scope import GuildScope, activate, use_scope


@pytest.fixture(autouse=True)
def reset_scope():
    activate(None)
    yield
    activate(None)


def test_language_persists_per_guild_without_changing_other_settings(tmp_path):
    path = tmp_path / "config.sqlite3"
    store = ConfigStore(path)
    store.set_value(11, "channels", "admin", 123)
    store.set_language(11, "en")
    assert store.get_language(22) == "pt-BR"
    assert store.get_settings(11)["language"] == "en"
    assert store.get_settings(11)["channels"]["admin"] == 123
    store.close()
    restarted = ConfigStore(path)
    assert restarted.get_language(11) == "en"
    assert restarted.get_language(22) == "pt-BR"
    with pytest.raises(ValueError):
        restarted.set_language(11, "invalid")
    restarted.close()


def test_language_is_task_local_including_file_threads():
    async def check(guild_id, locale, expected):
        with use_scope(GuildScope(guild_id, language=locale)):
            await asyncio.sleep(0)
            assert t("Abrir ticket") == expected
            assert await asyncio.to_thread(t, "Abrir ticket") == expected

    async def run():
        await asyncio.gather(check(11, "en", "Open ticket"), check(22, "pt-BR", "Abrir ticket"))

    asyncio.run(run())
    assert t("Abrir ticket") == "Abrir ticket"


def test_english_setup_and_ticket_components_keep_protocol_values():
    from manguadu.discord_bot import ManguaduBot, ConfigHomeView, ChannelsView, WhitelistModal, PublicTicketView, WhitelistConfigView, _home_embed
    from manguadu.guild_ui import ServerBindingModal, ServerConfigView

    store = ConfigStore(":memory:")
    bot = ManguaduBot(store)
    store.set_language(11, "en")
    bot.activate_guild(11)

    async def run():
        button = PublicTicketView(bot).children[0]
        assert button.label == "Open ticket"
        assert button.custom_id == "manguadu:ticket:open:v1"
        assert "settings" in _home_embed(store.get_settings(11)).title
        assert "Panel:" in _home_embed(store.get_settings(11)).fields[0].value
        assert "Channels" in [item.label for item in ConfigHomeView(bot, 11).children]
        assert ServerBindingModal(bot, 11).title == "Bind this Discord to a PZ server"
        assert ServerBindingModal(bot, 11).children[0].label == "Pterodactyl ID (8 characters)"
        assert ServerConfigView(bot, 11).embed().title == "This Discord's server settings"
        assert len(ServerConfigView(bot, 11).to_components()) <= 5
        modal = WhitelistModal(bot, {"channel_id": 33}, True)
        assert modal.title == "PZ character creation"
        assert modal.username.label == "PZ login username"
        select = ChannelsView(bot, 11).function_select
        assert next(option for option in select.options if option.value == "evolution").label == "Progression"
        # Customer labels must not be translated even if they match a catalog key.
        scope = bot.scope_for(11)
        scope.server = {"id": "default", "label": "Servidor"}
        bot._runtimes[11] = SimpleNamespace(scope=scope)
        bot.activate_guild(11)
        server_select = next(item for item in WhitelistConfigView(bot, 11).children if isinstance(item, discord.ui.Select))
        assert server_select.options[0].label == "Servidor"

    asyncio.run(run())
    store.close()


def test_language_menu_changes_only_this_guild(monkeypatch):
    from manguadu import guild_ui
    from manguadu.discord_bot import ManguaduBot

    store = ConfigStore(":memory:")
    bot = ManguaduBot(store)
    bot.activate_guild(11)
    monkeypatch.setattr(guild_ui, "is_admin", AsyncMock(return_value=True))
    interaction = SimpleNamespace(data={"values": ["en"]}, response=SimpleNamespace(edit_message=AsyncMock()))

    async def run():
        view = guild_ui.LanguageView(bot, 11)
        await view.choose(interaction)
        embed = interaction.response.edit_message.call_args.kwargs["embed"]
        assert "Community language: **English**" in embed.description
        assert store.get_language(11) == "en"
        assert store.get_language(22) == "pt-BR"

    asyncio.run(run())
    store.close()


def test_language_menu_rejects_non_admin(monkeypatch):
    from manguadu import guild_ui
    from manguadu.discord_bot import ManguaduBot

    store = ConfigStore(":memory:")
    bot = ManguaduBot(store)
    monkeypatch.setattr(guild_ui, "is_admin", AsyncMock(return_value=False))
    interaction = SimpleNamespace(data={"values": ["en"]})

    async def run():
        await guild_ui.LanguageView(bot, 11).choose(interaction)
        assert store.get_language(11) == "pt-BR"

    asyncio.run(run())
    store.close()


def test_all_command_payloads_include_english_descriptions_and_valid_names():
    from manguadu.discord_bot import ManguaduBot
    store = ConfigStore(":memory:")
    bot = ManguaduBot(store)
    mark_command_strings(bot.tree)

    def validate(nodes):
        localized_names = []
        for node in nodes:
            name = node.get("name_localizations", {}).get("en-US", node["name"])
            assert len(name) <= 32 and re.fullmatch(r"[a-z0-9_-]+", name)
            localized_names.append(name)
            description = node.get("description", "")
            if description and description != "…":
                assert description in CATALOG or not re.search(r"[^\x00-\x7f]", description)
                translated = node.get("description_localizations", {}).get("en-US", description)
                assert 1 <= len(translated) <= 100
                if description in CATALOG and CATALOG[description] != description:
                    assert "en-US" in node["description_localizations"]
            for choice in node.get("choices", []):
                assert len(choice.get("name_localizations", {}).get("en-US", choice["name"])) <= 100
            validate(node.get("options", []))
        assert len(set(localized_names)) == len(localized_names)

    async def run():
        payloads = [await command.get_translated_payload(bot.tree, HTSTranslator()) for command in bot.tree.get_commands()]
        validate(payloads)
        panel = next(payload for payload in payloads if payload["name"] == "painel")
        assert panel["name_localizations"]["en-US"] == "panel"
        server = next(payload for payload in payloads if payload["name"] == "servidor")
        action = next(option for option in server["options"] if option["name"] == "acao")
        assert action["name_localizations"]["en-US"] == "action"
        start = next(choice for choice in action["choices"] if choice["value"] == "start")
        assert start["name_localizations"]["en-US"] == "Start"

    asyncio.run(run())
    store.close()


def test_english_ranking_preserves_customer_content_and_player_names():
    from manguadu.panel_reports import build_ranking_panel, build_server_panel
    from manguadu.panel_theme import normalize_theme
    with use_scope(GuildScope(11, language="en")):
        theme = normalize_theme({"title": "Servidor", "description": "Minha comunidade"})
        panels, files = build_ranking_panel([{"row": {"username": "Sobrevivente", "zombiekills": "10"}}], [], theme=theme)
        assert "Servidor" in panels[0].title
        assert panels[0].description == "Minha comunidade"
        assert panels[0].fields[0].name == "SURVIVORS"
        assert "Sobrevivente" in panels[1].description
        assert "kills" in panels[1].description
        status, _ = build_server_panel({"isOnline": False, "onlinePlayers": {"count": 0, "names": []}})
        assert "No survivors" in status[-1].description
        assert not files


def test_gemini_prompt_requests_the_selected_language(monkeypatch):
    from manguadu import gemini
    prompts = []

    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self, **kwargs):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps({"decision": "review"})}]}}]}

    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, url, **kwargs):
            prompts.append(kwargs["json"]["systemInstruction"]["parts"][0]["text"])
            return Response()

    monkeypatch.setattr(gemini.aiohttp, "ClientSession", Session)
    async def run():
        for locale in ("en", "pt-BR"):
            with use_scope(GuildScope(11, language=locale)):
                await gemini._generate("test-key", "story")
    asyncio.run(run())
    assert "Write every human-readable JSON field in English" in prompts[0]
    assert "Responda em portugues do Brasil" in prompts[1]
