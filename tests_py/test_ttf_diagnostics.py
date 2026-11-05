import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from manguadu.pz_data import PzData
from manguadu.stats.ttf import TtfStats
from manguadu.tenant_scope import activate


@pytest.fixture(autouse=True)
def clear_scope():
    activate(None)
    yield
    activate(None)


def invalid_fixture(tmp_path):
    root = tmp_path / "ttf_stats_mp"
    root.mkdir()
    (root / "index.txt").write_text("bad line\nmissing|User||\nwrong_bool|User||\nwrong_version|User||\n", encoding="utf-8")
    (root / "wrong_bool.txt").write_text("version=1\ndeleted=false\ncharacterID=wrong_bool\nusername=User\nisDead=unknown\n", encoding="utf-8")
    (root / "wrong_version.txt").write_text("version=2\ndeleted=false\ncharacterID=wrong_version\nusername=User\nisDead=false\n", encoding="utf-8")
    return root


def test_four_distinct_failures_include_file_field_and_index_line_without_modification(tmp_path):
    root = invalid_fixture(tmp_path)
    before = {path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in root.iterdir()}
    snapshot = TtfStats(root).snapshot()
    assert snapshot["invalid"] == 4
    assert snapshot["invalid_reasons"] == {"invalid_index": 1, "missing_file": 1, "invalid_boolean": 1, "unsupported_version": 1}
    details = snapshot["invalid_records"]
    assert [item["index_line"] for item in details] == [1, 2, 3, 4]
    assert details[2]["file"] == "wrong_bool.txt" and details[2]["field"] == "isDead"
    assert details[3]["field"] == "version"
    assert str(tmp_path) not in json.dumps(details)
    assert before == {path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in root.iterdir()}


def test_diagnostics_details_are_bounded_and_omit_raw_record_values(tmp_path):
    root = invalid_fixture(tmp_path)
    (root / "index.txt").write_text("malformed-secret-input\n" * 200, encoding="utf-8")
    snapshot = TtfStats(root).snapshot()
    assert snapshot["invalid"] == 200
    assert len(snapshot["invalid_records"]) == 50
    assert snapshot["invalid_reasons"] == {"invalid_index": 200}
    assert "secret-input" not in json.dumps(snapshot)


def test_partial_write_reason_is_reported_and_recovers_after_valid_write(tmp_path):
    root = tmp_path / "ttf_stats_mp"
    root.mkdir()
    (root / "index.txt").write_text("char|Player||\n", encoding="utf-8")
    path = root / "char.txt"
    valid = "version=1\ndeleted=false\ncharacterID=char\nusername=Player\nisDead=false\nlastSeenKills=20\n"
    path.write_text(valid, encoding="utf-8")
    provider = TtfStats(root)
    assert provider.snapshot()["invalid"] == 0
    path.write_text("version=1\nlastSeenKills=", encoding="utf-8")
    failed = provider.snapshot(force=True)
    assert failed["stale"] == 1
    assert failed["invalid_records"][0]["code"] == "incomplete_write"
    assert failed["accounts"][0]["row"]["zombiekills"] == "20.0"
    path.write_text(valid.replace("20", "30"), encoding="utf-8")
    recovered = provider.snapshot(force=True)
    assert recovered["invalid_records"] == [] and recovered["invalid_reasons"] == {}
    assert recovered["accounts"][0]["row"]["zombiekills"] == "30.0"


@pytest.mark.asyncio
async def test_discord_download_is_private_bounded_diagnostic_json(tmp_path, monkeypatch):
    from manguadu import guild_ui
    root = invalid_fixture(tmp_path)
    data = PzData(root)
    bot = SimpleNamespace(_runtimes={11: SimpleNamespace(data=data)})
    monkeypatch.setattr(guild_ui, "is_admin", AsyncMock(return_value=True))
    interaction = SimpleNamespace(guild_id=11, response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    view = guild_ui.ServerConfigView(bot, 11)
    await view.invalid_records.callback(interaction)
    args = interaction.followup.send.call_args.kwargs
    assert args["ephemeral"]
    attachment = args["file"]
    payload = json.loads(attachment.fp.getvalue())
    assert attachment.filename == "ttf-invalid-records.json"
    assert payload["invalid"] == 4 and len(payload["records"]) == 4
    assert not payload["details_truncated"]
    assert all(record["message"] for record in payload["records"])
    assert "root" not in payload
    interaction.followup.send.reset_mock()
    interaction.guild_id = 22
    await view.invalid_records.callback(interaction)
    interaction.followup.send.assert_not_awaited()
