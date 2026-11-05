import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from manguadu.pz_data import PzData
from manguadu.stats.ttf import TtfStats, parse_character, MAX_FILE
from manguadu.stats.discovery import discover, lua_candidates
from manguadu.tenant_scope import GuildScope, activate, use_scope


@pytest.fixture(autouse=True)
def reset_scope():
    activate(None)
    yield
    activate(None)


def character(root, cid, username="The Butcher", **overrides):
    values = {"version": "1", "deleted": "false", "characterID": cid, "username": username, "forename": "Cuscuz", "surname": "Fênix",
              "lastSeenKills": "37", "gameTimeSeconds": "7200", "distance": "251.59152914253536", "isDead": "false", "lastSeenRealTimestamp": "1793815318",
              "vehicleKills": "3", "brokenWeaponsTotal": "2", "killsAtMidnight": "9999", "dailyRecord": "999", "dailySeries": "1,2,3,4", "weapons": "Base.ShortBat:12", "categories": "SmallBlunt:12"}
    values.update(overrides)
    text = "\n".join(f"{key}={value}" for key, value in values.items() if value is not None) + "\n"
    (root / (cid + ".txt")).write_text(text, encoding="utf-8")
    return text


@pytest.fixture
def ttf(tmp_path):
    root = tmp_path / "Lua" / "ttf_stats_mp"
    root.mkdir(parents=True)
    character(root, "not_a_username_1.793E9_123")
    character(root, "second_character", isDead="true", lastSeenKills="13", gameTimeSeconds="3600", lastSeenRealTimestamp="1")
    (root / "index.txt").write_text("characterID|username|forename|surname\nnot_a_username_1.793E9_123|The Butcher|Cuscuz|Fênix\nsecond_character|The Butcher|Old|Person\n", encoding="utf-8")
    return root


def test_ttf_actual_format_uses_index_not_filename_and_aggregates_distinct_characters(ttf):
    snapshot = TtfStats(ttf).snapshot()
    assert len(snapshot["characters"]) == 2
    account = snapshot["accounts"][0]
    assert account["nick"] == "The Butcher"
    assert account["row"]["charname"] == "Cuscuz Fênix"
    assert float(account["row"]["zombiekills"]) == 50
    assert float(account["row"]["hourssurvived"]) == 3
    assert float(account["row"]["vehiclekills"]) == 6
    assert account["row"]["deaths"] == "1"
    assert account["row"]["characters"] == "2"
    assert float(account["row"]["distance"]) == pytest.approx(503.183058285)
    assert account["characters"][0]["weapons"] == {"Base.ShortBat": 12}


def test_duplicate_index_rows_do_not_duplicate_counters(ttf):
    with (ttf / "index.txt").open("a", encoding="utf-8") as stream:
        stream.write("second_character|The Butcher|Old|Person\n")
    snapshot = TtfStats(ttf).snapshot()
    assert len(snapshot["characters"]) == 2
    assert float(snapshot["accounts"][0]["row"]["zombiekills"]) == 50


def test_deleted_and_removed_characters_are_excluded_and_new_files_appear(ttf):
    provider = TtfStats(ttf)
    provider.snapshot()
    character(ttf, "second_character", deleted="true")
    result = provider.snapshot(force=True)
    assert result["deleted"] == 1
    assert len(result["characters"]) == 1
    (ttf / "not_a_username_1.793E9_123.txt").unlink()
    assert provider.snapshot(force=True)["accounts"] == []
    character(ttf, "new_person", "Malaio")
    (ttf / "index.txt").write_text("new_person|Malaio|Aaron|Hadden\n", encoding="utf-8")
    assert provider.snapshot(force=True)["accounts"][0]["nick"] == "Malaio"
    assert set(provider._cache) == {"new_person"}


def test_unchanged_files_are_cached_and_invalidated_by_metadata(ttf, monkeypatch):
    provider = TtfStats(ttf)
    original = provider._read
    reads = []
    def read(path, limit):
        reads.append(path.name)
        return original(path, limit)
    monkeypatch.setattr(provider, "_read", read)
    provider.snapshot(force=True)
    assert len(reads) == 3
    provider.snapshot(force=True)
    assert len(reads) == 3
    character(ttf, "second_character", lastSeenKills="123")
    provider.snapshot(force=True)
    assert len(reads) == 4
    assert float(provider.snapshot()["accounts"][0]["row"]["zombiekills"]) == 160


def test_partial_write_keeps_previous_character_and_reports_staleness(ttf):
    provider = TtfStats(ttf)
    provider.snapshot()
    (ttf / "second_character.txt").write_text("version=1\ncharacterID=second_character\nlastSeenKills=", encoding="utf-8")
    result = provider.snapshot(force=True)
    assert result["stale"] == result["invalid"] == 1
    assert float(result["accounts"][0]["row"]["zombiekills"]) == 50
    character(ttf, "second_character", lastSeenKills="100")
    assert provider.snapshot(force=True)["stale"] == 0


def test_missing_metric_is_not_zero_or_partial_sum(ttf):
    character(ttf, "second_character", lastSeenKills=None, distance=None)
    row = TtfStats(ttf).snapshot()["accounts"][0]["row"]
    assert "zombiekills" not in row
    assert "distance" not in row
    assert float(row["hourssurvived"]) == 4


@pytest.mark.parametrize("override", [{"version": "2"}, {"lastSeenKills": "nan"}, {"gameTimeSeconds": "-2"}, {"username": "Another account"}, {"isDead": "maybe"}, {"characterID": "other"}])
def test_invalid_character_never_enters_ranking(ttf, override):
    text = character(ttf, "second_character", **override)
    with pytest.raises(ValueError):
        parse_character(text, "second_character", "The Butcher")
    assert TtfStats(ttf).snapshot()["invalid"] == 1


def test_malformed_index_traversal_and_oversized_file_are_rejected(ttf):
    (ttf / "index.txt").write_text("../outside|Victim||\ninvalid line\nsecond_character|The Butcher||\n", encoding="utf-8")
    (ttf / "second_character.txt").write_bytes(b"x" * (MAX_FILE + 1))
    result = TtfStats(ttf).snapshot()
    assert result["accounts"] == []
    assert result["invalid"] == 3


def test_both_providers_are_selected_explicitly_and_never_merged(ttf, monkeypatch):
    friend = ttf.parent / "FriendHost_Data"
    player = friend / "Jogadores" / "Friend Player"
    player.mkdir(parents=True)
    (player / "player_Friend Player.txt").write_text("username;zombiekills\nFriend Player;777\n", encoding="utf-8")
    assert discover(friend)[0] == "friendhost"
    assert discover(friend, "ttf")[0] == "ttf"
    assert discover(friend)[2] == ["friendhost", "ttf"]
    data = PzData(friend)
    assert data.player_names() == ["Friend Player"]
    monkeypatch.setenv("STATS_PROVIDER", "ttf")
    data.stats_source(force=True)
    assert data.player_names() == ["The Butcher"]
    assert len(data.player_rows()) == 1
    assert data.death_counts() == {"The Butcher": 1}
    assert data.player_rank("The Butcher", "hours")["total"] == 1
    rank = data.player_rank("second_character", "kills", "character")
    assert rank["position"] == 2


def test_discovery_lua_ambiguity_and_symlink_protection(tmp_path):
    (tmp_path / ".cache/Lua").mkdir(parents=True)
    (tmp_path / "Zomboid/Lua").mkdir(parents=True)
    assert lua_candidates(tmp_path) == [".cache/Lua", "Zomboid/Lua"]
    try:
        (tmp_path / "Lua").symlink_to(tmp_path / ".cache/Lua", target_is_directory=True)
    except OSError:
        pytest.skip("OS does not allow test symlinks")
    assert "Lua" not in lua_candidates(tmp_path)


@pytest.mark.asyncio
async def test_25_guilds_ttf_concurrent_and_cached_data_cannot_cross_guilds(tmp_path, monkeypatch):
    monkeypatch.setenv("PTERO_VOLUMES_ROOT", str(tmp_path))
    providers = []
    for index in range(25):
        uuid = str(uuid4())
        root = tmp_path / uuid / ".cache/Lua/ttf_stats_mp"
        root.mkdir(parents=True)
        character(root, "same_character", lastSeenKills=str(index))
        (root / "index.txt").write_text("same_character|The Butcher||\n", encoding="utf-8")
        providers.append((GuildScope(index + 1, server={}, environment={"PTERO_VOLUME_ID": uuid}), PzData(root)))
    async def run(index):
        scope, data = providers[index]
        with use_scope(scope):
            entry = await asyncio.to_thread(data.find_player, "The Butcher")
            assert float(entry["row"]["zombiekills"]) == index
            foreign = providers[(index + 1) % 25][1]
            assert foreign.player_rows() == []
    await asyncio.gather(*(run(index) for index in range(25)))


def test_panels_previews_and_reports_use_ttf_metrics_and_unavailable_values(ttf):
    from manguadu.player_data import collect_player_snapshot
    from manguadu.player_reports import build_status_embed, build_status_complete_embed, build_status_complete_report
    from manguadu.panel_reports import build_ranking_panel
    character(ttf, "second_character", lastSeenKills=None)
    data = PzData(ttf)
    snapshot = collect_player_snapshot("The Butcher", data=data, include_db=True, include_inventory=True)
    assert snapshot["found"]
    assert "TTF Stats MP" in build_status_embed(snapshot).title
    assert "TTF Stats MP" in build_status_complete_embed(snapshot).title
    report = build_status_complete_report(snapshot)
    assert "second_character" in report and "Indisponível" in report
    panels, _ = build_ranking_panel(data.player_rows(), [])
    assert "TTF Stats MP" in panels[0].title
    assert panels[1].description == "Indisponível"
    assert "The Butcher" in panels[2].description
    for embed in panels:
        assert len(embed) < 6000


def test_no_data_ttf_panel_does_not_fabricate_friendhost_totals(ttf):
    from manguadu.panel_reports import build_ranking_panel
    (ttf / "index.txt").write_text("", encoding="utf-8")
    embeds, _ = build_ranking_panel(PzData(ttf).player_rows(), [])
    assert "TTF Stats MP" in embeds[0].title
    assert embeds[1].description == "Indisponível"


def test_ttf_snapshot_budget_refuses_incomplete_account_totals(ttf, monkeypatch):
    from manguadu.stats import ttf as module
    provider = TtfStats(ttf)
    previous = provider.snapshot()
    monkeypatch.setattr(module, "MAX_SNAPSHOT_BYTES", 1)
    result = provider.snapshot(force=True)
    assert result["error"] == "snapshot memory limit"
    assert result["stale"] == 2
    assert result["accounts"] == previous["accounts"]


def test_missing_explicit_provider_never_falls_back_to_other_source(ttf, monkeypatch):
    from manguadu.panel_reports import build_ranking_panel
    friend = ttf.parent / "FriendHost_Data/Jogadores/Other"
    friend.mkdir(parents=True)
    (friend / "player_Other.txt").write_text("username;zombiekills\nOther;999\n", encoding="utf-8")
    (ttf / "index.txt").unlink()
    monkeypatch.setenv("STATS_PROVIDER", "ttf")
    data = PzData(ttf.parent / "FriendHost_Data")
    assert data.player_names() == []
    assert data.player_rows() == []
    embeds, _ = build_ranking_panel(data.player_rows(), [])
    assert embeds[0].title == "Ranking indisponível"


def test_character_limit_rejects_partial_aggregate(ttf, monkeypatch):
    from manguadu.stats import ttf as module
    monkeypatch.setattr(module, "MAX_CHARACTERS", 1)
    snapshot = TtfStats(ttf).snapshot()
    assert snapshot["accounts"] == []
    assert snapshot["error"] == "character count limit"


def test_confined_reader_rejects_non_regular_targets(ttf):
    from manguadu.stats.files import confined_reader
    with pytest.raises(ValueError, match="regular file"):
        with confined_reader(ttf, ttf):
            raise AssertionError("must not open directory")


def test_permission_failure_and_missing_index_are_diagnosed(ttf, monkeypatch):
    provider = TtfStats(ttf)
    provider.snapshot()
    def denied(*args): raise PermissionError("denied")
    monkeypatch.setattr(provider, "_read", denied)
    provider._index_version = None
    assert provider.snapshot(force=True)["error"] == "PermissionError"
    assert provider.snapshot()["stale"] == 2
    monkeypatch.undo()
    (ttf / "index.txt").unlink()
    assert provider.snapshot(force=True)["error"] == "FileNotFoundError"


def test_ttf_english_panels_and_reports_preserve_player_content(ttf):
    from manguadu.player_data import collect_player_snapshot
    from manguadu.stats.reports import report, profile
    from manguadu.panel_reports import build_ranking_panel
    with use_scope(GuildScope(1, language="en")):
        # Unbound guilds must not read local data, even in a different language.
        assert PzData(ttf).player_rows() == []
    data = PzData(ttf)
    snapshot = collect_player_snapshot("The Butcher", data=data)
    from manguadu import i18n
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(i18n, "language", lambda: "en")
        panels, _ = build_ranking_panel(data.player_rows(), [])
        assert panels[1].title == "Zombies killed"
        assert "The Butcher" in panels[1].description
        assert "Cuscuz Fênix" in report(snapshot)
        assert profile(snapshot).fields[0].name == "Zombies killed"
