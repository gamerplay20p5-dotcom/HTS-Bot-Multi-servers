import asyncio
from types import SimpleNamespace, MethodType
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from manguadu.config_store import ConfigStore
from manguadu.panel_reports import build_ranking_panel
from manguadu.panel_theme import normalize_theme
from manguadu.pz_data import PzData
from manguadu.ranking import DEFAULT_RANKINGS, RANKINGS, format_stat
from manguadu.tenant_scope import GuildScope, activate, use_scope


@pytest.fixture(autouse=True)
def clear_scope():
    activate(None)
    yield
    activate(None)


def friendhost(root, username, values):
    player = root / "Jogadores" / username
    player.mkdir(parents=True)
    (player / f"player_{username}.txt").write_text(f"username;zombiekills;hourssurvived\n{username};1250;12.5\n", encoding="utf-8")
    (player / f"playerperks_{username}.txt").write_text("username;steamid;cooking;fitness;strength;aiming;fakeperk\n" + username + ";10;" + values + ";10\n", encoding="utf-8")


def test_rankings_persist_independently_and_do_not_decompress_lore(tmp_path, monkeypatch):
    path = tmp_path / "config.sqlite3"
    store = ConfigStore(path)
    def forbidden(*args): raise AssertionError("must not read lore")
    monkeypatch.setattr(store, "_get_guild_lore", forbidden)
    assert store.get_rankings(1) == DEFAULT_RANKINGS
    store.set_rankings(1, ["skills10", "kills", "skills10", "not_a_rank"])
    store.set_rankings(2, [])
    assert store.get_rankings(1) == ["kills", "skills10"]
    assert store.get_rankings(2) == []
    assert store.get_rankings(3) == DEFAULT_RANKINGS
    store.close()
    store = ConfigStore(path)
    config = store.get_settings(1)
    assert config["ranking_metrics"] == ["kills", "skills10"]
    store.save_settings(1, config)
    assert store.get_rankings(1) == ["kills", "skills10"]
    store.close()


def test_default_is_compact_and_empty_selection_keeps_summary_only():
    rows = [{"nick": "Malaio", "row": {"username": "Malaio", "zombiekills": "1250", "hourssurvived": "12.5", "factionname": "Test"}}]
    embeds, _ = build_ranking_panel(rows, [], {"Malaio": 4})
    assert len(embeds) == 3
    assert "CAÇADORES" in embeds[1].title and "VETERANOS" in embeds[2].title
    assert all(".00" not in str(embed.to_dict()) and ",00" not in str(embed.to_dict()) for embed in embeds)
    embeds, _ = build_ranking_panel(rows, [], {"Malaio": 4}, enabled=[])
    assert len(embeds) == 1
    assert len(embeds[0].fields) == 1
    assert "Apenas o resumo" in embeds[0].description


@pytest.mark.parametrize("locale,expected", [("pt-BR", "1.250"), ("en", "1,250")])
def test_integer_formatting_respects_community_language(locale, expected):
    with use_scope(GuildScope(1, language=locale)):
        assert format_stat(1250.0) == expected
        assert format_stat(9.9) == "10"
        assert format_stat(0) == "0"
        assert format_stat(float("nan")) in {"Indisponível", "Unavailable"}


def test_skills10_uses_current_known_skills_not_metadata_or_historical_sum(tmp_path):
    root = tmp_path / "FriendHost_Data"
    friendhost(root, "Alpha", "10;8;10;9")
    friendhost(root, "Beta", "10;10;10;9")
    friendhost(root, "Zero", "9;9;9;9")
    data = PzData(root)
    rows = data.skills10_rows()
    assert [(item["name"], item["value"]) for item in rows] == [("Beta", 3), ("Alpha", 2)]
    assert set(rows[0]["skills"]) == {"cooking", "fitness", "strength"}
    assert data.player_rank("Alpha", "skills10")["position"] == 2
    assert data.player_rank("Alpha", "skills10", "character") is None
    # New current-life snapshot replaces previous max levels.
    path = root / "Jogadores/Beta/playerperks_Beta.txt"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("Beta;10;1;1;1;1;10\n")
    assert [(row["name"], row["value"]) for row in data.skills10_rows()] == [("Alpha", 2)]
    embeds, _ = build_ranking_panel(data.player_rows(), [], enabled=["skills10"], skills10=data.skills10_rows())
    assert len(embeds) == 2
    assert "Alpha" in embeds[1].description and "2" in embeds[1].description


def test_zero_max_skills_is_distinct_from_missing_export_and_ttf(tmp_path):
    root = tmp_path / "FriendHost_Data"
    friendhost(root, "Zero", "9;9;9;9")
    data = PzData(root)
    assert data.skills10_rows() == []
    embeds, _ = build_ranking_panel(data.player_rows(), [], enabled=["skills10"], skills10=[])
    assert "Nenhum jogador" in embeds[1].description
    (root / "Jogadores/Zero/playerperks_Zero.txt").unlink()
    assert data.skills10_rows() is None
    ttf = tmp_path / "Lua/ttf_stats_mp"
    ttf.mkdir(parents=True)
    (ttf / "index.txt").write_text("", encoding="utf-8")
    data = PzData(ttf)
    assert data.skills10_rows() is None
    embeds, _ = build_ranking_panel(data.player_rows(), [], enabled=["skills10"])
    assert embeds[1].description == "Indisponível"


def test_skills10_cannot_read_a_foreign_guild_cache(tmp_path, monkeypatch):
    uuid = str(uuid4())
    root = tmp_path / uuid / ".cache/Lua/FriendHost_Data"
    friendhost(root, "Alpha", "10;10;10;10")
    monkeypatch.setenv("PTERO_VOLUMES_ROOT", str(tmp_path))
    data = PzData(root)
    with use_scope(GuildScope(1, server={}, environment={"PTERO_VOLUME_ID": uuid})):
        assert data.skills10_rows()[0]["value"] == 4
    with use_scope(GuildScope(2, server={}, environment={"PTERO_VOLUME_ID": str(uuid4())})):
        assert data.skills10_rows() is None


@pytest.mark.asyncio
async def test_config_selector_saves_current_guild_and_refreshes_queue(tmp_path, monkeypatch):
    from manguadu import discord_bot
    store = ConfigStore(tmp_path / "config.sqlite3")
    bot = SimpleNamespace(store=store, work_queue=SimpleNamespace(trigger=Mock()))
    view = discord_bot.RankingsConfigView(bot, 11)
    assert view.selector.min_values == 0 and view.selector.max_values == len(RANKINGS)
    assert {option.value for option in view.selector.options if option.default} == set(DEFAULT_RANKINGS)
    monkeypatch.setattr(discord_bot, "_admin_guard", AsyncMock(return_value=True))
    interaction = SimpleNamespace(guild_id=11, data={"values": ["skills10", "hours"]}, response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
    await view.choose(interaction)
    assert store.get_rankings(11) == ["hours", "skills10"]
    assert store.get_rankings(22) == DEFAULT_RANKINGS
    interaction.data = {"values": ["invalid_metric"]}
    await view.choose(interaction)
    assert store.get_rankings(11) == ["hours", "skills10"]
    bot.work_queue.trigger.assert_called_once_with("11:panels")
    monkeypatch.setattr(discord_bot, "_admin_guard", AsyncMock(return_value=False))
    interaction.data = {"values": []}
    await view.choose(interaction)
    assert store.get_rankings(11) == ["hours", "skills10"]
    interaction.guild_id = 22
    await view.choose(interaction)
    assert store.get_rankings(22) == DEFAULT_RANKINGS
    store.close()


@pytest.mark.asyncio
async def test_english_rankings_menu_and_diagnostic_messages(tmp_path):
    from manguadu import i18n, discord_bot
    from manguadu.stats.diagnostics import REASONS, explanation
    store = ConfigStore(tmp_path / "config.sqlite3")
    with use_scope(GuildScope(1, language="en")):
        view = discord_bot.RankingsConfigView(SimpleNamespace(store=store), 1)
        assert view.embed().title == "Published rankings"
        assert next(option.label for option in view.selector.options if option.value == "skills10") == "Skills at level 10"
        assert "does not export" in view.embed().description
        for code, source in REASONS.items():
            assert source in i18n.CATALOG
            assert explanation({"code": code}) == i18n.CATALOG[source]
    store.close()


@pytest.mark.asyncio
async def test_preview_and_scheduler_share_selection_and_skip_disabled_file_reads(tmp_path, monkeypatch):
    from manguadu import discord_bot, tenant_scope
    monkeypatch.setattr(tenant_scope, "DATA_ROOT", tmp_path / "state")
    monkeypatch.setattr(discord_bot, "_admin_guard", AsyncMock(return_value=True))
    store = ConfigStore(tmp_path / "config.sqlite3")
    store.set_rankings(11, ["skills10"])
    config = store.get_settings(11)
    config["channels"]["ranking"] = 123
    store.save_settings(11, config)
    data = SimpleNamespace(base_path=tmp_path, player_rows=Mock(return_value=[]), faction_rows=Mock(return_value=[]), death_counts=Mock(return_value={}), skills10_rows=Mock(return_value=[{"name": "Alpha", "value": 3}]))
    bot = SimpleNamespace(store=store, pz_data=data, user=object(), guilds=[SimpleNamespace(id=11)], _upsert_panel_message=AsyncMock())
    bot._ranking_payload = MethodType(discord_bot.ManguaduBot._ranking_payload, bot)
    interaction = SimpleNamespace(guild_id=11, response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    with use_scope(GuildScope(11, server={}, runtime=object())):
        await discord_bot.ManguaduBot._panel_preview(bot, interaction, "ranking")
        await discord_bot.ManguaduBot.refresh_panels(bot)
    preview = interaction.followup.send.call_args.kwargs["embeds"]
    published = bot._upsert_panel_message.call_args.args[4]
    assert [item.title for item in preview] == [item.title for item in published]
    assert len(preview) == 2 and "Alpha" in preview[1].description
    data.faction_rows.assert_not_called()
    data.death_counts.assert_not_called()
    assert data.skills10_rows.call_count == 2
    store.close()


def test_every_selected_rank_fits_discord_limits_and_hides_unselected_sections():
    rows = [{"nick": "Survivor" * 30, "row": {"username": "Survivor" * 30, "zombiekills": "123456789", "hourssurvived": "22.2", "factionname": "Faction" * 30}} for _ in range(10)]
    theme = normalize_theme({"title": "T" * 300, "description": "D" * 4000})
    embeds, _ = build_ranking_panel(rows, [], {"Survivor" * 30: 5}, enabled=list(RANKINGS), skills10=[{"name": "Player" * 50, "value": 20} for _ in range(10)], theme=theme)
    assert len(embeds) <= 10
    assert sum(len(embed) for embed in embeds) <= 6000
    assert all(len(embed.title) <= 256 for embed in embeds)
