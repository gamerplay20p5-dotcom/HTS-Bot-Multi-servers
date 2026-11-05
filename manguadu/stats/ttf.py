"""TTF Stats MP version=1 key/value exports (not FriendHost CSV).

Only per-character cumulative counters are added across distinct character IDs.
Daily baselines/records/series are deliberately excluded from cumulative kills.
"""
from __future__ import annotations

import math
import os
import time
from collections import OrderedDict, Counter
from pathlib import Path
from threading import RLock

from .discovery import safe_child
from .files import confined_reader
from .diagnostics import InvalidRecord, error_code

MAX_FILE = 256 * 1024
MAX_INDEX = 4 * 1024 * 1024
MAX_CHARACTERS = 10000
MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
METRICS = {"zombiekills": "lastSeenKills", "hourssurvived": "gameTimeSeconds", "distance": "distance",
           "vehiclekills": "vehicleKills", "brokenweapons": "brokenWeaponsTotal"}


class ProviderRows(list):
    provider = "ttf"

    def __init__(self, snapshot):
        super().__init__(snapshot["accounts"])
        self.diagnostics = {key: snapshot[key] for key in ("invalid", "deleted", "stale", "error", "updated")}


class UnavailableRows(list):
    provider = "none"


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def pairs(value: str) -> dict[str, float]:
    result = {}
    for item in value.split(",")[:512]:
        key, sep, raw = item.rpartition(":")
        count = number(raw)
        if sep and key and count is not None:
            result[key[:200]] = count
    return result


def parse_character(text: str, character_id: str, username: str) -> dict:
    values = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        key, sep, value = line.partition("=")
        if not sep or not key or key in values:
            raise InvalidRecord("invalid_key_value", line=line_number)
        if key in {"version", "deleted", "characterID", "username", "forename", "surname", "isDead", "lastSeenRealTimestamp", *METRICS.values(), "weapons", "categories"}:
            values[key] = value
    if values.get("version") != "1":
        raise InvalidRecord("unsupported_version", field="version")
    if values.get("characterID") != character_id:
        raise InvalidRecord("character_mismatch", field="characterID")
    if values.get("username") != username:
        raise InvalidRecord("username_mismatch", field="username")
    if values.get("deleted") not in {"true", "false"}:
        raise InvalidRecord("invalid_boolean", field="deleted")
    if values["deleted"] == "true":
        return {"deleted": True}
    if values.get("isDead") not in {"true", "false"}:
        raise InvalidRecord("invalid_boolean", field="isDead")
    row = {"username": username, "charname": (" ".join(filter(None, (values.get("forename"), values.get("surname")))) or username)[:200],
           "characterid": character_id, "isalive": str(values["isDead"] == "false").lower()}
    for key, source in METRICS.items():
        parsed = number(values.get(source))
        if source in values and parsed is None:
            raise InvalidRecord("invalid_number", field=source)
        if parsed is not None:
            row[key] = str(parsed / 3600 if key == "hourssurvived" else parsed)
    return {"id": character_id, "username": username, "row": row, "deleted": False,
            "last_seen": number(values.get("lastSeenRealTimestamp")) or 0,
            "weapons": pairs(values.get("weapons", "")), "categories": pairs(values.get("categories", ""))}


def aggregate(characters: list[dict]) -> list[dict]:
    grouped = {}
    for character in characters:
        grouped.setdefault(character["username"], []).append(character)
    result = []
    for username, entries in grouped.items():
        newest = max(entries, key=lambda item: (item["last_seen"], item["id"]))
        row = {key: value for key, value in newest["row"].items() if key not in METRICS}
        row["characters"] = str(len(entries))
        row["deaths"] = str(sum(item["row"]["isalive"] == "false" for item in entries))
        for metric in METRICS:
            if all(metric in item["row"] for item in entries):
                total = sum(float(item["row"][metric]) for item in entries)
                if math.isfinite(total):
                    row[metric] = str(total)
        result.append({"nick": username, "row": row, "provider": "ttf", "characters": entries})
    return sorted(result, key=lambda item: item["nick"].casefold())


class TtfStats:
    def __init__(self, root: Path):
        self.root = root
        self._cache = OrderedDict()
        self._bytes = 0
        self._lock = RLock()
        self._checked = 0.0
        self._index_version, self._index_text = None, ""
        self._snapshot = {"accounts": [], "characters": [], "invalid": 0, "deleted": 0, "stale": 0, "error": "", "updated": 0,
                          "invalid_records": [], "invalid_reasons": {}, "error_detail": None}

    def _read(self, path, limit):
        with confined_reader(self.root, path) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > limit:
                raise InvalidRecord("file_size_limit")
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > limit or (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise InvalidRecord("changed_during_read")
        # A writer may have truncated/reopened a file. Retry on the next snapshot.
        if raw and not raw.endswith(b"\n"):
            raise InvalidRecord("incomplete_write")
        return raw.decode("utf-8-sig"), (after.st_mtime_ns, after.st_size, after.st_ino)

    def snapshot(self, *, force=False):
        with self._lock:
            if not safe_child(self.root, self.root):
                return {"accounts": [], "characters": [], "invalid": 0, "deleted": 0, "stale": 0, "error": "unsafe provider root", "updated": 0,
                        "invalid_records": [], "invalid_reasons": {}, "error_detail": {"file": "index.txt", "code": "unsafe_path"}}
            if not force and time.monotonic() - self._checked < 5:
                return self._snapshot
            self._checked = time.monotonic()
            try:
                index = self.root / "index.txt"
                if not safe_child(self.root, index):
                    raise ValueError("unsafe index path")
                stat = index.stat()
                version = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
                if version == self._index_version:
                    text = self._index_text
                else:
                    text, self._index_version = self._read(index, MAX_INDEX)
                    self._index_text = text
            except (OSError, ValueError, UnicodeError) as exc:
                self._snapshot = {**self._snapshot, "error": type(exc).__name__, "stale": len(self._snapshot["characters"]),
                                  "error_detail": {"file": "index.txt", "code": error_code(exc)}}
                return self._snapshot
            invalid, deleted, stale, snapshot_bytes = 0, 0, 0, 0
            details, reasons = [], Counter()
            def record_invalid(file, code, index_line, error=None):
                reasons[code] += 1
                if len(details) < 50:
                    details.append({"file": file, "code": code, "index_line": index_line,
                                    "field": getattr(error, "field", ""), "record_line": getattr(error, "line", 0)})
            seen, characters = set(), []
            for index_line, line in enumerate(text.splitlines(), 1):
                if line == "characterID|username|forename|surname" or not line:
                    continue
                parts = line.split("|")
                if len(parts) != 4:
                    invalid += 1
                    record_invalid("index.txt", "invalid_index", index_line)
                    continue
                cid, username, _, _ = parts
                if cid in seen:
                    continue
                if not cid or not username or len(username) > 200 or len(cid) > 240 or any(c in cid for c in "/\\:\x00") or cid in {".", ".."}:
                    invalid += 1
                    record_invalid("index.txt", "invalid_identifier", index_line)
                    continue
                if len(seen) >= MAX_CHARACTERS:
                    self._snapshot = {**self._snapshot, "error": "character count limit", "stale": len(self._snapshot["characters"])}
                    return self._snapshot
                seen.add(cid)
                path = self.root / (cid + ".txt")
                entry = self._cache.get(cid)
                try:
                    if not safe_child(self.root, path):
                        raise ValueError("unsafe path")
                    stat = path.stat()
                    version = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
                    if entry and entry[0] == version and entry[3] == username:
                        character = entry[1]
                        cost = entry[2]
                        self._cache.move_to_end(cid)
                    else:
                        raw, version = self._read(path, MAX_FILE)
                        character = parse_character(raw, cid, username)
                        cost = len(repr(character).encode("utf-8")) * 4 + 512
                        if entry:
                            self._bytes -= entry[2]
                        self._cache[cid] = (version, character, cost, username)
                        self._bytes += cost
                        while self._cache and self._bytes > MAX_CACHE_BYTES:
                            _, old = self._cache.popitem(last=False)
                            self._bytes -= old[2]
                except FileNotFoundError as exc:
                    invalid += 1
                    record_invalid(cid + ".txt", "missing_file", index_line, exc)
                    continue
                except (OSError, ValueError, UnicodeError) as exc:
                    invalid += 1
                    record_invalid(cid + ".txt", error_code(exc), index_line, exc)
                    if not entry or entry[3] != username or not safe_child(self.root, path):
                        continue
                    character = entry[1]
                    cost = entry[2]
                    stale += 1
                if character.get("deleted"):
                    deleted += 1
                else:
                    snapshot_bytes += cost
                    if snapshot_bytes > MAX_SNAPSHOT_BYTES:
                        self._snapshot = {**self._snapshot, "error": "snapshot memory limit", "stale": len(self._snapshot["characters"])}
                        return self._snapshot
                    characters.append(character)
            for cid in list(self._cache):
                if cid not in seen:
                    self._bytes -= self._cache.pop(cid)[2]
            self._snapshot = {"accounts": aggregate(characters), "characters": characters, "invalid": invalid, "deleted": deleted,
                              "stale": stale, "error": "", "updated": time.time(), "invalid_records": details,
                              "invalid_reasons": dict(reasons), "error_detail": None}
            return self._snapshot
