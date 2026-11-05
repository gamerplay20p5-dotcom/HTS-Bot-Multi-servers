"""Consulta local dos dados FriendHost sem dependencia do Discord."""

from __future__ import annotations

from .i18n import t

from .tenant_scope import getenv, allowed_path

import re
import json
import math
import os
import time
import csv
import sys
import logging
from collections import OrderedDict, Counter, deque
from threading import RLock
from pathlib import Path

from .friendhost import (
    get_player_file_candidates,
    get_server_file_candidates,
    is_friendhost_data_file,
    resolve_player_file,
    resolve_server_file,
)
from .utils import clean_text, get_field, read_csv_raw_rows, read_csv_rows, read_latest_csv_row, read_text_file, to_number


PERK_LABELS = {
    "cooking": 'Culinária', "fitness": 'Preparo físico', "strength": 'Força',
    "blunt": 'Contundente longa', "axe": 'Machado', "lightfoot": 'Pés leves',
    "nimble": 'Agilidade', "sprinting": 'Corrida', "sneak": 'Furtividade',
    "woodwork": 'Carpintaria', "aiming": 'Mira', "reloading": 'Recarga',
    "farming": 'Agricultura', "fishing": 'Pesca', "trapping": 'Armadilhas',
    "plantscavenging": 'Busca de alimentos', "doctor": 'Primeiros socorros',
    "electricity": 'Elétrica', "blacksmith": 'Ferraria', "metalwelding": 'Metalurgia',
    "mechanics": 'Mecânica', "spear": 'Lança', "maintenance": 'Manutenção',
    "smallblade": 'Lâmina curta', "longblade": 'Lâmina longa',
    "smallblunt": 'Contundente curta', "tailoring": 'Costura',
}

logger = logging.getLogger(__name__)
PERK_EMOJIS = {
    "cooking": "🍳", "fitness": "💪", "strength": "🏋️", "blunt": "🔨", "axe": "🪓",
    "lightfoot": "🦶", "nimble": "🤸", "sprinting": "🏃", "sneak": "🥷", "woodwork": "🪚",
    "aiming": "🎯", "reloading": "🔁", "farming": "🌾", "fishing": "🎣", "trapping": "🪤",
    "plantscavenging": "🍄", "doctor": "🩹", "electricity": "🔌", "blacksmith": "⚒️",
    "metalwelding": "🧰", "mechanics": "🔧", "spear": "🗡️", "maintenance": "🛠️",
    "smallblade": "🔪", "longblade": "⚔️", "smallblunt": "🔩", "tailoring": "🧵",
}


class PzData:
    def __init__(self, csv_base_path: str | Path | None, *, death_state_path: Path | None = None):
        self.base_path = Path(csv_base_path) if csv_base_path else None
        self._stats_provider = None
        self._stats_discovery = (0, ("none", None, []))
        self._row_cache = OrderedDict()
        self._cache_bytes = 0
        self._cache_lock = RLock()
        self._death_offset = 0
        self._death_inode = None
        self._death_counts = Counter()
        self._death_total = 0
        self._death_remainder = b""
        self._death_caught_up = False
        self._death_events = deque(maxlen=100)
        self._death_state_path = death_state_path
        if death_state_path and death_state_path.is_file() and death_state_path.stat().st_size < 8 * 1024 * 1024:
            try:
                state = json.loads(death_state_path.read_text(encoding="utf-8"))
                self._death_inode = tuple(state["identity"])
                self._death_offset = int(state["offset"])
                self._death_counts = Counter({str(k): int(v) for k, v in list(state["counts"].items())[:50000]})
                self._death_total = int(state["total"])
                self._death_caught_up = True
            except (OSError, ValueError, KeyError, TypeError):
                self._death_inode, self._death_offset = None, 0

    def latest_row(self, file_path):
        if not file_path:
            return None
        path = Path(file_path)
        try:
            if self.base_path and not path.resolve().is_relative_to(self.base_path.resolve()):
                return None
            stat = path.stat()
        except OSError:
            return None
        key, version = str(path), (stat.st_mtime_ns, stat.st_size)
        with self._cache_lock:
            entry = self._row_cache.get(key)
            if entry and entry[0] == version:
                self._row_cache.move_to_end(key)
                return dict(entry[1]) if entry[1] else None
            row = read_latest_csv_row(path)
            if row is None and entry and entry[1]:
                return dict(entry[1])
            cost = sys.getsizeof(row) + sys.getsizeof(key) + sum(sys.getsizeof(k) + sys.getsizeof(v) for k, v in (row or {}).items()) + 256
            if entry:
                self._cache_bytes -= entry[2]
            self._row_cache[key] = (version, row, cost)
            self._cache_bytes += cost
            while self._row_cache and (len(self._row_cache) > 2048 or self._cache_bytes > 4 * 1024 * 1024):
                _, old = self._row_cache.popitem(last=False)
                self._cache_bytes -= old[2]
            return dict(row) if row else None

    def death_snapshot(self, *, consume_events=True):
        if self.stats_source()[0] == "ttf":
            counts = {item["nick"]: int(item["row"]["deaths"]) for item in self.ttf_snapshot()["accounts"]}
            return {"counts": counts, "total": sum(counts.values()), "events": []}
        path = resolve_server_file(self.server_path, "deaths")
        if not path or not allowed_path(path):
            return {"counts": {}, "total": 0, "events": []}
        with self._cache_lock:
            try:
                stat = path.stat()
                identity = (str(path), stat.st_ino)
                if identity != self._death_inode or stat.st_size < self._death_offset:
                    self._death_inode, self._death_offset = identity, 0
                    self._death_counts.clear()
                    self._death_events.clear()
                    self._death_total, self._death_remainder, self._death_caught_up = 0, b"", False
                with path.open("rb") as stream:
                    stream.seek(self._death_offset)
                    raw = stream.read(4 * 1024 * 1024)
                    self._death_offset = stream.tell()
            except OSError:
                return {"counts": dict(self._death_counts), "total": self._death_total, "events": []}
            complete, _, self._death_remainder = (self._death_remainder + raw).rpartition(b"\n")
            if not complete and b"\n" not in raw:
                self._death_remainder = (self._death_remainder or raw)[-65536:]
            events = []
            for row in csv.reader(complete.decode("utf-8-sig", errors="replace").splitlines(), delimiter=";"):
                if not row or any(cell.casefold() == "username" for cell in row):
                    continue
                nick = next((cell.strip() for cell in row[4:7] if cell.strip() and not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}|\d{2}:\d{2}:\d{2}", cell.strip())), "")
                if nick and (nick in self._death_counts or len(self._death_counts) < 50000):
                    self._death_counts[nick] += 1
                self._death_total += 1
                if self._death_caught_up:
                    self._death_events.append(row)
            self._death_caught_up = self._death_offset >= stat.st_size
            if raw and self._death_state_path:
                state = {"identity": self._death_inode, "offset": self._death_offset - len(self._death_remainder), "counts": self._death_counts, "total": self._death_total}
                try:
                    self._death_state_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self._death_state_path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(state), encoding="utf-8")
                    temporary.replace(self._death_state_path)
                except OSError:
                    logger.warning("Nao foi possivel persistir o cursor de mortes em %s", self._death_state_path)
            events = list(self._death_events) if consume_events else []
            if consume_events:
                self._death_events.clear()
            return {"counts": dict(self._death_counts), "total": self._death_total, "events": events}

    def death_counts(self):
        return self.death_snapshot(consume_events=False)["counts"]

    def stats_source(self, *, force=False):
        from .stats.discovery import discover
        with self._cache_lock:
            checked, result = self._stats_discovery
            if force or time.monotonic() - checked > 5:
                result = discover(self.base_path, getenv("STATS_PROVIDER", "auto"))
                self._stats_discovery = (time.monotonic(), result)
            if result[1] is not None and not allowed_path(result[1]):
                return "none", None, []
            return result

    def ttf_snapshot(self, *, force=False):
        from .stats.ttf import TtfStats
        source, root, _ = self.stats_source(force=force)
        if source != "ttf":
            return {"accounts": [], "characters": [], "invalid": 0, "deleted": 0, "stale": 0, "error": "", "updated": 0}
        with self._cache_lock:
            if self._stats_provider is None or self._stats_provider.root != root:
                self._stats_provider = TtfStats(root)
            return self._stats_provider.snapshot(force=force)

    def diagnostics(self):
        source, root, available = self.stats_source(force=True)
        result = {"provider": source, "available": available, "root": str(root or ""), "accounts": 0, "characters": None,
                  "invalid": 0, "deleted": 0, "stale": 0, "error": "", "updated": time.time()}
        if source == "ttf":
            snapshot = self.ttf_snapshot(force=True)
            result.update({key: snapshot[key] for key in ("invalid", "deleted", "stale", "error", "updated")})
            result.update(accounts=len(snapshot["accounts"]), characters=len(snapshot["characters"]))
        rows = self.player_rows()
        if source == "friendhost":
            result["accounts"] = len(rows)
        result["metrics"] = [key for key in ("zombiekills", "hourssurvived", "distance", "vehiclekills", "brokenweapons") if any(key in entry["row"] for entry in rows)]
        return result

    @property
    def players_path(self) -> Path | None:
        source, root, _ = self.stats_source()
        base = root if source == "friendhost" else self.base_path
        return base / "Jogadores" if base else None

    @property
    def server_path(self) -> Path | None:
        source, root, _ = self.stats_source()
        if source == "ttf":
            return None
        base = root or self.base_path
        return base / "Servidor" if base else None

    def player_names(self) -> list[str]:
        source = self.stats_source()[0]
        if source == "none":
            return []
        if source == "ttf":
            return [item["nick"] for item in self.ttf_snapshot()["accounts"]]
        if not self.players_path or not allowed_path(self.players_path) or not self.players_path.is_dir():
            return []
        return sorted((path.name for path in self.players_path.iterdir() if path.is_dir() and not path.is_symlink()), key=str.casefold)

    def player_rows(self) -> list[dict]:
        source = self.stats_source()[0]
        if source == "none":
            from .stats.ttf import UnavailableRows
            return UnavailableRows()
        if source == "ttf":
            from .stats.ttf import ProviderRows
            return ProviderRows(self.ttf_snapshot())
        rows = []
        for nick in self.player_names():
            row = self.latest_row(resolve_player_file(self.players_path, nick))
            if row:
                rows.append({"nick": nick, "row": row})
        return rows

    def find_player(self, nick_input: str) -> dict | None:
        target = clean_text(nick_input).casefold()
        if not target:
            return None
        rows = self.player_rows()
        exact = next((entry for entry in rows if target in {
            entry["nick"].casefold(), get_field(entry["row"], ["username"]).casefold(),
            get_field(entry["row"], ["charname"]).casefold(),
        }), None)
        if exact:
            return exact
        matches = [entry for entry in rows if any(target in value.casefold() for value in (
            entry["nick"], get_field(entry["row"], ["username"]), get_field(entry["row"], ["charname"]),
        ) if value)]
        return matches[0] if len(matches) == 1 else None

    def player_traits(self, nick_input: str) -> dict | None:
        entry = self.find_player(nick_input)
        if not entry:
            return None
        row = entry["row"]

        def parse(keys: list[str]) -> list[str]:
            value = get_field(row, keys)
            value = re.sub(r"^\[|\]$|^\(|\)$", "", value)
            return [clean_text(item).strip("'\"") for item in re.split(r"[,;|]+", value) if clean_text(item).strip("'\"")]

        return {
            **entry,
            "profession": get_field(row, ["profession", "professionname"], t('Não informada')),
            "traits": parse(["traits", "traitlist", "playertraits", "charactertraits"]),
            "positive": parse(["positivetraits", "goodtraits"]),
            "negative": parse(["negativetraits", "badtraits"]),
        }

    def player_skills(self, nick_input: str) -> dict | None:
        entry = self.find_player(nick_input)
        if not entry or not self.players_path:
            return None
        perks_file = resolve_player_file(self.players_path, entry["nick"], "perks")
        perks = self.latest_row(perks_file) or {}
        ignored = {"systemdate", "systemtime", "gametime", "steamid", "username", "charname"}
        skills = [
            {"key": key, "label": t(PERK_LABELS.get(key, key)), "emoji": PERK_EMOJIS.get(key, "✨"), "value": to_number(value)}
            for key, value in perks.items() if key not in ignored
        ]
        skills.sort(key=lambda skill: (-skill["value"], skill["label"].casefold()))
        return {**entry, "skills": skills}

    def player_rank(self, nick_input: str, metric: str = "zombiekills", scope: str = "account") -> dict | None:
        column = {"hours": "hourssurvived", "kills": "zombiekills"}.get(metric, metric)
        rows = self.player_rows()
        if scope == "character":
            rows = [{"nick": item["row"]["charname"] + " · " + item["id"], "row": item["row"], "provider": "ttf"}
                    for item in self.ttf_snapshot()["characters"]]
        rows = [entry for entry in rows if get_field(entry["row"], [column]) != ""]
        rows.sort(key=lambda entry: (-to_number(get_field(entry["row"], [column])), entry["nick"].casefold()))
        target = clean_text(nick_input).casefold()
        for index, entry in enumerate(rows):
            if target in {entry["nick"].casefold(), get_field(entry["row"], ["username"]).casefold(), get_field(entry["row"], ["charname"]).casefold(), get_field(entry["row"], ["characterid"]).casefold()}:
                return {"metric": column, "position": index + 1, "total": len(rows), "entry": entry, "leaderboard": rows[:10]}
        return None

    def online_players(self) -> list[str]:
        content = read_text_file(resolve_server_file(self.server_path, "players_online"))
        return [name for piece in content.split(";") if (name := clean_text(piece))]

    def world_snapshot(self) -> dict[str, str]:
        if self.stats_source()[0] == "ttf":
            return {"source": "indisponivel", "gameTime": "", "gameDate": "", "timeOfDay": "", "temperature": ""}
        path = resolve_server_file(self.server_path, "world")
        lines = [line.strip() for line in read_text_file(path).splitlines() if line.strip()]
        fallback_time = ""
        fallback_source = ""
        player_files = [candidate for nick in self.player_names() if (candidate := resolve_player_file(self.players_path, nick)) and candidate.is_file()]
        if player_files:
            newest = max(player_files, key=lambda candidate: candidate.stat().st_mtime)
            player_row = self.latest_row(newest)
            fallback_time = get_field(player_row, ["gametime"])
            fallback_source = newest.name
        values = [value.strip().strip('"') for value in lines[-1].split(";")] if lines else []
        game_time = next((value for value in values if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2}", value)), "")
        temperature = next((f"{number:.1f}" for value in values if re.fullmatch(r"-?\d+[.,]\d+", value) and -60 <= (number := to_number(value)) <= 60 and not 0 <= number <= 1), "")
        if not game_time:
            game_time = fallback_time if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2}", fallback_time) else ""
        date, time = game_time.split() if game_time else ("", "")
        return {"source": path.name if lines and path else fallback_source or "indisponivel", "gameTime": game_time, "gameDate": date, "timeOfDay": time, "temperature": temperature}

    def safehouses(self) -> list[dict[str, str]]:
        return read_csv_rows(resolve_server_file(self.server_path, "safehouses"))

    def faction_rows(self) -> list[dict[str, str]]:
        return read_csv_rows(resolve_server_file(self.server_path, "factions"))

    def death_rows(self) -> list[list[str]]:
        return read_csv_raw_rows(resolve_server_file(self.server_path, "deaths"))

    def extract_player_logs(self, nick: str, log_type: str = "todos", hours: int = 24) -> dict:
        target = clean_text(nick).casefold()
        kind = clean_text(log_type).casefold()
        period = max(1, min(168, int(hours or 24)))
        root = Path(getenv("LOGS_PATH", ""))
        if not target or not getenv("LOGS_PATH") or not root.is_dir():
            return {"files": [], "lines": [], "hours": period, "type": kind}
        cutoff = time.time() - period * 3600
        candidates = []
        pending = [(root, 0)]
        while pending and len(candidates) < 100:
            folder, depth = pending.pop()
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue
            for path in entries:
                if path.is_symlink():
                    continue
                if path.is_dir() and depth < 2:
                    pending.append((path, depth + 1))
                    continue
                if not path.is_file() or path.suffix.casefold() not in {".txt", ".log"}:
                    continue
                if kind and kind != "todos" and kind not in path.name.casefold():
                    continue
                try:
                    if path.stat().st_mtime >= cutoff:
                        candidates.append(path)
                except OSError:
                    continue
                if len(candidates) >= 100:
                    break
        lines = []
        for path in candidates:
            if not allowed_path(path):
                continue
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 2 * 1024 * 1024))
                if stream.tell():
                    stream.readline(65536)
                content = stream.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
            for line in content.splitlines():
                if target not in line.casefold():
                    continue
                match = re.search(r"(\d{2})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})", line)
                if match:
                    from datetime import datetime
                    try:
                        timestamp = datetime(*(2000 + int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4)), int(match.group(5)), int(match.group(6)))).timestamp()
                        if timestamp < cutoff:
                            continue
                    except ValueError:
                        pass
                shortened = line if len(line) <= 500 else line[:497] + "..."
                lines.append(f"{path.name} | {shortened}")
                if len(lines) >= 1000:
                    break
            if len(lines) >= 1000:
                break
        return {"files": candidates, "lines": lines, "hours": period, "type": kind}

    def delete_targets(self, scope: str, nick: str = "") -> list[Path] | None:
        if not self.base_path or not self.base_path.is_dir():
            return []
        scope = clean_text(scope).casefold()
        candidates: list[Path] = []
        if scope == "ranking":
            if self.players_path:
                for player in self.player_names():
                    candidates.extend(get_player_file_candidates(self.players_path, player, "player"))
                    candidates.extend(get_player_file_candidates(self.players_path, player, "perks"))
            if self.server_path:
                for stem in ("deaths", "factions", "safehouses", "players_online"):
                    candidates.extend(get_server_file_candidates(self.server_path, stem))
        elif scope == "inventarios":
            if self.players_path:
                for player in self.player_names():
                    candidates.extend(get_player_file_candidates(self.players_path, player, "inventory"))
        elif scope == "jogador":
            player = self.find_player(nick)
            if not player or not self.players_path:
                return None
            for kind in ("player", "perks", "inventory"):
                candidates.extend(get_player_file_candidates(self.players_path, player["nick"], kind))
        elif scope == "tudo":
            pending = [(self.base_path, 0)]
            while pending and len(candidates) < 2000:
                folder, depth = pending.pop()
                try:
                    entries = list(folder.iterdir())
                except OSError:
                    continue
                for path in entries:
                    if path.is_symlink():
                        continue
                    if path.is_dir() and depth < 8:
                        pending.append((path, depth + 1))
                    elif path.is_file() and is_friendhost_data_file(path.name):
                        candidates.append(path)
                    if len(candidates) >= 2000:
                        break
        else:
            return []
        root = self.base_path.resolve()
        unique: dict[str, Path] = {}
        for path in candidates:
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
                if resolved.is_file():
                    unique[str(resolved)] = resolved
            except (OSError, ValueError):
                continue
        return list(unique.values())

    def find_vehicle(self, vehicle_id: str) -> dict | None:
        target = clean_text(vehicle_id).casefold()
        if not target or not self.server_path or not self.server_path.is_dir():
            return None
        pending = [(self.server_path, 0)]
        while pending:
            directory, depth = pending.pop()
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for path in entries:
                if path.is_symlink():
                    continue
                if path.is_dir() and depth < 3:
                    pending.append((path, depth + 1))
                    continue
                lowered = path.name.casefold()
                if not path.is_file() or "vehicle" not in lowered or not lowered.endswith((".txt", ".csv", ".csv.txt")):
                    continue
                for row in read_csv_rows(path):
                    identifiers = (get_field(row, keys) for keys in (
                        ["vehicleid"], ["id"], ["sqlid"], ["vehicle_id"],
                    ))
                    if target in {value.casefold() for value in identifiers if value}:
                        return {"file": path, "row": row}
        return None

    @staticmethod
    def configured_maps(config_path: str | Path | None = None) -> list[dict]:
        path = Path(config_path) if config_path else Path(__file__).resolve().parent.parent / "config" / "pz-maps.json"
        try:
            parsed = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []
        raw_maps = parsed if isinstance(parsed, list) else parsed.get("maps") if isinstance(parsed, dict) else None
        if not isinstance(raw_maps, list):
            return []
        maps = []
        for index, raw in enumerate(raw_maps, 1):
            if not isinstance(raw, dict) or raw.get("enabled") is False:
                continue
            x1 = to_number(raw.get("minX", raw.get("x1")))
            y1 = to_number(raw.get("minY", raw.get("y1")))
            x2 = to_number(raw.get("maxX", raw.get("x2")))
            y2 = to_number(raw.get("maxY", raw.get("y2")))
            if x1 == x2 or y1 == y2:
                continue
            name = clean_text(raw.get("name") or raw.get("id") or ''.join((format(t('Mapa '), ''), format(index, ''))))
            maps.append({
                "id": clean_text(raw.get("id") or name.casefold().replace(" ", "-")),
                "name": name,
                "minX": min(x1, x2), "minY": min(y1, y2),
                "maxX": max(x1, x2), "maxY": max(y1, y2),
            })
        return maps

    def player_points(self) -> list[dict[str, Any]]:
        points = []
        for entry in self.player_rows():
            row = entry["row"]
            x_raw = get_field(row, ["x", "posx", "positionx"])
            y_raw = get_field(row, ["y", "posy", "positiony"])
            if not x_raw or not y_raw:
                continue
            x = to_number(x_raw, float("nan"))
            y = to_number(y_raw, float("nan"))
            if not (math.isfinite(x) and math.isfinite(y)):
                continue
            points.append({
                "nick": entry["nick"], "x": x, "y": y,
                "z": to_number(get_field(row, ["z", "posz", "positionz"])),
            })
        return points

    def player_point(self, nick: str) -> dict[str, Any] | None:
        entry = self.find_player(nick)
        if not entry:
            return None
        row = entry["row"]
        x_raw = get_field(row, ["x", "posx", "positionx"])
        y_raw = get_field(row, ["y", "posy", "positiony"])
        if not x_raw or not y_raw:
            return None
        x = to_number(x_raw, float("nan"))
        y = to_number(y_raw, float("nan"))
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        return {"nick": entry["nick"], "x": x, "y": y, "z": to_number(get_field(row, ["z", "posz", "positionz"]))}

    @staticmethod
    def world_bounds(points: list[dict[str, Any]]) -> dict[str, float]:
        maps = PzData.configured_maps()
        xs = [coordinate for item in maps for coordinate in (item["minX"], item["maxX"])]
        ys = [coordinate for item in maps for coordinate in (item["minY"], item["maxY"])]
        for point in points:
            x, y = point.get("x"), point.get("y")
            if isinstance(x, (int, float)) and math.isfinite(x):
                xs.append(float(x))
            if isinstance(y, (int, float)) and math.isfinite(y):
                ys.append(float(y))
        if not xs or not ys:
            return {"minX": 0, "minY": 0, "maxX": 30000, "maxY": 30000}
        return {"minX": min(xs), "minY": min(ys), "maxX": max(xs), "maxY": max(ys)}

    @staticmethod
    def map_for_point(point: dict[str, Any], maps: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        maps = maps if maps is not None else PzData.configured_maps()
        return next((item for item in maps if item["minX"] <= point["x"] <= item["maxX"] and item["minY"] <= point["y"] <= item["maxY"]), None)
