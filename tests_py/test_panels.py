from __future__ import annotations

from pathlib import Path

from manguadu.panel_player_links import get_player_link, load_player_links, remove_player_link, set_player_link
from manguadu.panel_reports import build_ranking_panel, build_server_panel
from manguadu.panel_theme import get_draft_theme, get_saved_theme, save_draft_theme, update_draft_theme, youtube_thumbnail


def test_panel_theme_draft_and_saved_state(tmp_path: Path, monkeypatch) -> None:
    from manguadu import panel_theme

    monkeypatch.setattr(panel_theme, "THEME_FILE", tmp_path / "theme.json")
    monkeypatch.setattr(panel_theme, "DRAFT_FILE", tmp_path / "draft.json")
    assert get_saved_theme()["primaryColor"] == "#28D17C"
    update_draft_theme({"title": "Meu PZ", "primaryColor": "#abcdef"})
    assert get_draft_theme()["title"] == "Meu PZ"
    assert get_saved_theme()["title"] == "Servidor Project Zomboid"
    save_draft_theme()
    assert get_saved_theme()["primaryColor"] == "#ABCDEF"


def test_youtube_thumbnail_supports_video_and_short_links() -> None:
    assert youtube_thumbnail("https://www.youtube.com/watch?v=abc123") == "https://img.youtube.com/vi/abc123/hqdefault.jpg"
    assert youtube_thumbnail("https://youtu.be/xyz987") == "https://img.youtube.com/vi/xyz987/hqdefault.jpg"
    assert youtube_thumbnail("https://example.com/image.png") == ""


def test_player_links_are_case_insensitive_and_atomic(tmp_path: Path) -> None:
    target = tmp_path / "links.json"
    assert set_player_link("Menta", 123, target)
    assert get_player_link("MENTA", target) == "123"
    assert set_player_link("MENTA", 456, target)
    assert load_player_links(target) == {"MENTA": "456"}
    assert remove_player_link("menta", target)
    assert not remove_player_link("menta", target)


def test_panel_payloads_render_status_and_rankings() -> None:
    players = [
        {"nick": "Menta", "row": {"username": "Menta", "zombiekills": "120", "hourssurvived": "12.5", "factionname": "Lobos"}},
        {"nick": "Nina", "row": {"username": "Nina", "zombiekills": "80", "hourssurvived": "8", "factionname": "Lobos"}},
    ]
    ranking, files = build_ranking_panel(players, [{"name": "Lobos", "tagname": "LB", "owner": "Menta", "players": "Menta,Nina"}], [["Menta"]], enabled=["kills", "hours", "factions", "deaths"])
    assert not files
    assert ranking[0].title == "🏆 Servidor Project Zomboid · Hall da Sobrevivência"
    assert any(embed.title == "🛡️ DOMÍNIO DAS FACÇÕES" for embed in ranking)
    assert any(embed.title == "☠️ OS QUE MAIS VOLTARAM" for embed in ranking)

    status, status_files = build_server_panel({
        "isOnline": True, "currentState": "running", "ptero": {"ok": True, "state": "running", "resources": {}},
        "rconPlayers": {"ok": True}, "onlinePlayers": {"count": 1, "names": ["Menta"], "source": "rcon"},
        "world": {"gameDate": "04.08.2026", "timeOfDay": "12:00"},
    }, online_profiles=[{"nick": "Menta", "faction": "Lobos", "onlineTime": "5m"}])
    assert not status_files
    assert "MUNDO OPERACIONAL" in status[0].description
    assert "Menta" in status[1].description
