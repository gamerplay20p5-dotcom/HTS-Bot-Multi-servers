"""Coleta ficha estendida de jogador dos arquivos FriendHost e saves locais."""

from __future__ import annotations

from .tenant_scope import getenv, current_scope

import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from .friendhost import resolve_player_file, resolve_server_file
from .pz_data import PzData, PERK_EMOJIS, PERK_LABELS
from .utils import clean_text, get_field, parse_delimited_csv, read_csv_rows, read_latest_csv_row, read_text_file, to_number


logger = logging.getLogger(__name__)
INVENTORY_CATEGORIES = {
    "Ammo", "Clothing", "Container", "Drainable", "Food", "Item", "Key", "Literature", "Material", "Moveable", "Normal", "Weapon", "WeaponPart",
}
IGNORED_PERK_KEYS = {"systemdate", "systemtime", "gametime", "steamid", "username", "charname"}


def parse_players_list(value: str) -> list[str]:
    text = clean_text(value)
    if not text: return []
    if re.match(r"^Players\(", text, re.I): text = re.sub(r"^Players\(", "", text, count=1, flags=re.I)
    text = text.removesuffix(")")
    return [item.strip() for item in text.split(",") if item.strip()]


def get_perk_entries(perks_row: dict | None, include_zero: bool = True) -> list[dict]:
    if not perks_row: return []
    values = [{"key": key, "label": PERK_LABELS.get(key, key), "visualLabel": f"{PERK_EMOJIS.get(key, '✨')} {PERK_LABELS.get(key, key)}", "emoji": PERK_EMOJIS.get(key, "✨"), "value": to_number(value)} for key, value in perks_row.items() if key not in IGNORED_PERK_KEYS]
    if not include_zero: values = [entry for entry in values if entry["value"] > 0]
    return sorted(values, key=lambda entry: (-entry["value"], entry["label"].casefold()))


def _infer_inventory_row(raw: list[str]) -> dict[str, str]:
    row = {"itemindex": "", "itemcategory": "", "itemid": "", "itemdisplayname": "", "itemextrainfo": "", "systemdate": "", "systemtime": "", "raw": " | ".join(clean_text(value) for value in raw)}
    leftovers = []
    for raw_value in raw:
        value = clean_text(raw_value)
        if not value: continue
        if not row["systemdate"] and re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value): row["systemdate"] = value
        elif not row["systemtime"] and re.fullmatch(r"\d{2}:\d{2}:\d{2}", value): row["systemtime"] = value
        elif not row["itemindex"] and re.fullmatch(r"\d+(?:\|\d+)*", value): row["itemindex"] = value
        elif not row["itemid"] and re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", value): row["itemid"] = value
        elif not row["itemcategory"] and value in INVENTORY_CATEGORIES: row["itemcategory"] = value
        else: leftovers.append(value)
    if leftovers:
        row["itemdisplayname"] = leftovers.pop(0)
        if leftovers: row["itemextrainfo"] = " | ".join(leftovers)
    if not row["itemdisplayname"] and row["itemid"]: row["itemdisplayname"] = row["itemid"].split(".")[-1]
    return row


def read_inventory_rows(path: str | Path | None) -> list[dict[str, str]]:
    content = read_text_file(path)
    if not content.strip(): return []
    try: return [_infer_inventory_row(row) for row in parse_delimited_csv(content, delimiter=";")]
    except ValueError: return []


def summarize_inventory(rows: list[dict]) -> dict:
    items: dict[tuple[str, str, str, str], dict] = {}
    categories: dict[str, int] = {}
    for row in rows:
        name = get_field(row, ["itemdisplayname"], get_field(row, ["itemid"], "Item desconhecido"))
        item_id, category, extra = get_field(row, ["itemid"]), get_field(row, ["itemcategory"], "Sem categoria"), get_field(row, ["itemextrainfo"])
        key = (name, item_id, category, extra)
        if key not in items: items[key] = {"name": name, "itemId": item_id, "category": category, "extra": extra, "count": 0}
        items[key]["count"] += 1
        categories[category] = categories.get(category, 0) + 1
    return {
        "totalRows": len(rows), "uniqueItems": len(items),
        "items": sorted(items.values(), key=lambda item: (-item["count"], item["name"].casefold())),
        "categories": sorted(({"name": name, "count": count} for name, count in categories.items()), key=lambda item: -item["count"]),
    }


def _find_faction(nick: str, player_row: dict, faction_rows: list[dict]) -> dict | None:
    lowered = clean_text(nick).casefold()
    faction_name, faction_tag = get_field(player_row, ["factionname"]), get_field(player_row, ["factiontag"])
    for row in faction_rows:
        name, owner = get_field(row, ["name"]), get_field(row, ["owner"])
        players = parse_players_list(get_field(row, ["players"]))
        if name and faction_name and name.casefold() == faction_name.casefold():
            return {"name": name, "owner": owner, "tag": get_field(row, ["tagname"], faction_tag), "players": players, "raw": row}
        if owner.casefold() == lowered or any(item.casefold() == lowered for item in players):
            return {"name": name or faction_name or "Sem facção", "owner": owner, "tag": get_field(row, ["tagname"], faction_tag), "players": players, "raw": row}
    if faction_name or faction_tag:
        return {"name": faction_name or "Sem nome", "owner": "", "tag": faction_tag, "players": [nick] if nick else [], "raw": None}
    return None


def _find_safehouses(nick: str, player_row: dict, safehouse_rows: list[dict]) -> list[dict]:
    lowered = clean_text(nick).casefold()
    safehouse_title = get_field(player_row, ["safehousetitle"])
    result = []
    for row in safehouse_rows:
        owner = get_field(row, ["owner"])
        title = get_field(row, ["title"])
        players = parse_players_list(get_field(row, ["players"]))
        if owner.casefold() == lowered or any(item.casefold() == lowered for item in players) or (safehouse_title and title and title.casefold() == safehouse_title.casefold()):
            result.append(row)
    return result


def _discovery_roots(data_path: Path | None, logs_path: str | Path | None = None) -> list[Path]:
    if current_scope() is not None:
        cache_value = getenv("PZ_CACHE_ROOT")
        if not cache_value:
            return []
        cache = Path(cache_value).resolve()
        return [root for root in (cache / "db", cache / "DB", cache / "Saves" / "Multiplayer") if root.is_dir() and root.resolve().is_relative_to(cache)]
    roots: set[Path] = set()
    for seed in (data_path, Path(logs_path) if logs_path else None):
        if not seed: continue
        current = seed.resolve()
        for _ in range(6):
            roots.update({current / "db", current / "DB", current / ".cache" / "db", current / ".cache" / "DB", current / "Saves" / "Multiplayer", current / ".cache" / "Saves" / "Multiplayer"})
            if current.parent == current: break
            current = current.parent
    return sorted((root for root in roots if root.is_dir()), key=lambda path: str(path).casefold())


def _walk_candidates(root: Path, max_depth: int = 3, max_results: int = 120) -> list[Path]:
    results: list[Path] = []
    pending = [(root, 0)]
    while pending and len(results) < max_results:
        folder, depth = pending.pop()
        try: entries = list(folder.iterdir())
        except OSError: continue
        for path in entries:
            if path.is_symlink(): continue
            if path.is_dir() and depth < max_depth: pending.append((path, depth + 1))
            elif path.is_file() and re.search(r"\.(?:db|sqlite|sqlite3|json|txt|ini|cfg|csv|log)$", path.name, re.I):
                results.append(path)
                if len(results) >= max_results: break
    return results


def _sqlite_matches(path: Path, tokens: list[str]) -> dict | None:
    try: connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error: return None
    result = {"filePath": str(path), "tables": []}
    try:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        for (table,) in tables:
            try: columns = connection.execute(f'PRAGMA table_info("{str(table).replace(chr(34), chr(34) * 2)}")').fetchall()
            except sqlite3.Error: continue
            searchable = [str(column[1]) for column in columns if re.search(r"name|username|user|steam|char|player|owner", str(column[1]), re.I)]
            if not searchable: continue
            where = " OR ".join(f'LOWER(CAST("{column.replace(chr(34), chr(34) * 2)}" AS TEXT)) LIKE ?' for column in searchable for _ in tokens)
            parameters = [f"%{token}%" for _column in searchable for token in tokens]
            try: rows = connection.execute(f'SELECT * FROM "{str(table).replace(chr(34), chr(34) * 2)}" WHERE {where} LIMIT 5', parameters).fetchall()
            except sqlite3.Error: continue
            if not rows: continue
            names = [description[0] for description in connection.execute(f'SELECT * FROM "{str(table).replace(chr(34), chr(34) * 2)}" LIMIT 0').description or []]
            formatted = []
            for row in rows:
                values = {}
                for key, value in zip(names, row):
                    if value is None: text = ""
                    elif isinstance(value, bytes): text = f"<blob {len(value)} bytes>"
                    else: text = str(value)
                    values[key] = text[:220].replace("\r", " ").replace("\n", " ")
                formatted.append(values)
            result["tables"].append({"name": table, "rows": formatted})
    finally:
        connection.close()
    return result if result["tables"] else None


def discover_player_db_data(nick: str, steam_id: str = "", data_path: str | Path | None = None, logs_path: str | Path | None = None) -> dict:
    tokens = list(dict.fromkeys(text.casefold() for text in (clean_text(nick), clean_text(steam_id)) if text))
    roots = _discovery_roots(Path(data_path) if data_path else None, logs_path)
    result = {"roots": [str(root) for root in roots], "sqliteMatches": [], "textMatches": []}
    if not roots or not tokens: return result
    candidates: list[Path] = []
    for root in roots:
        candidates.extend(_walk_candidates(root, max_results=max(0, 120 - len(candidates))))
        if len(candidates) >= 120: break
    for path in dict.fromkeys(candidates):
        if path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}:
            match = _sqlite_matches(path, tokens)
            if match: result["sqliteMatches"].append(match)
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024: continue
        except OSError: continue
        content = read_text_file(path)
        lowered = content.casefold()
        if not content or not any(token in path.name.casefold() or token in lowered for token in tokens): continue
        lines = [line[:220].replace("\r", " ") for line in content.splitlines() if any(token in line.casefold() for token in tokens)][:20]
        result["textMatches"].append({"filePath": str(path), "lines": lines})
    return result


def collect_player_snapshot(nick_input: str, *, data: PzData | None = None, include_inventory: bool = False, include_db: bool = False) -> dict:
    csv_path = clean_text(getenv("CSV_BASE_PATH"))
    data = data or PzData(Path(csv_path) if csv_path else None)
    available = data.player_names()
    requested = clean_text(nick_input)
    if not requested: return {"found": False, "availablePlayers": available, "reason": "missing_nick"}
    lowered = requested.casefold()
    exact = next((name for name in available if name.casefold() == lowered), None)
    matches = [name for name in available if lowered in name.casefold()] if not exact else []
    actual = exact or (matches[0] if len(matches) == 1 else None)
    if not actual: return {"found": False, "availablePlayers": available, "reason": "not_found"}
    player_file = resolve_player_file(data.players_path, actual, "player")
    perks_file = resolve_player_file(data.players_path, actual, "perks")
    inventory_file = resolve_player_file(data.players_path, actual, "inventory")
    player_row = read_latest_csv_row(player_file) or {}
    perks_row = read_latest_csv_row(perks_file) or {}
    inventory_rows = read_inventory_rows(inventory_file) if include_inventory else []
    faction_rows = data.faction_rows()
    safehouse_rows = data.safehouses()
    if not player_row and not perks_row and not inventory_rows:
        return {"found": False, "availablePlayers": available, "reason": "files_missing", "actualNick": actual}
    faction = _find_faction(actual, player_row, faction_rows)
    safehouses = _find_safehouses(actual, player_row, safehouse_rows)
    steam_id = get_field(player_row, ["steamid"])
    db_data = discover_player_db_data(actual, steam_id, data.base_path, getenv("LOGS_PATH")) if include_db else {"roots": [], "sqliteMatches": [], "textMatches": []}
    return {
        "found": True, "requestedNick": requested, "actualNick": actual,
        "files": {"playerFile": str(player_file), "perksFile": str(perks_file), "inventoryFile": str(inventory_file)},
        "availablePlayers": available, "playerRow": player_row, "perksRow": perks_row,
        "inventoryRows": inventory_rows, "inventorySummary": summarize_inventory(inventory_rows),
        "faction": faction, "factionRows": faction_rows, "safehouses": safehouses, "dbData": db_data,
    }
