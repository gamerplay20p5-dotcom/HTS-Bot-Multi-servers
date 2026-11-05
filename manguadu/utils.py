"""Leitura dos arquivos FriendHost e pequenos utilitarios compartilhados."""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import Any
from .tenant_scope import allowed_path, current_scope


logger = logging.getLogger(__name__)


def clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def to_number(value: Any, fallback: float = 0) -> float:
    match = re.match(r"^[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?", clean_text(value).replace(",", "."))
    if not match:
        return fallback
    try:
        return float(match.group())
    except ValueError:
        return fallback


def get_field(record: dict[str, str] | None, keys: list[str] | tuple[str, ...], fallback: str = "") -> str:
    for key in keys:
        value = clean_text((record or {}).get(key))
        if value:
            return value
    return fallback


def read_text_file(file_path: str | Path | None) -> str:
    if not file_path:
        return ""
    if not allowed_path(file_path):
        return ""
    try:
        if current_scope() is not None and Path(file_path).stat().st_size > 8 * 1024 * 1024:
            logger.warning("Arquivo excede o limite de leitura direta de 8 MiB: %s", file_path)
            return ""
        return Path(file_path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        logger.error("Falha ao ler %s: %s", file_path, exc)
        return ""


def parse_delimited_csv(content: str, *, delimiter: str = ";", trim: bool = True) -> list[list[str]]:
    normalized = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    rows = csv.reader(normalized.splitlines(keepends=True), delimiter=delimiter)
    result = []
    for row in rows:
        cells = [cell.strip() if trim else cell for cell in row]
        if any(clean_text(cell) for cell in cells):
            result.append(cells)
    return result


def read_csv_raw_rows(file_path: str | Path | None) -> list[list[str]]:
    content = read_text_file(file_path)
    if not content.strip():
        return []
    try:
        return parse_delimited_csv(content)
    except csv.Error as exc:
        logger.error("Falha ao ler CSV %s: %s", file_path, exc)
        return []


def read_csv_rows(file_path: str | Path | None) -> list[dict[str, str]]:
    rows = read_csv_raw_rows(file_path)
    if not rows:
        return []
    headers = [clean_text(value).lstrip("\ufeff\u200b").lower() for value in rows[0]]
    return [
        {header: clean_text(row[index]) if index < len(row) else "" for index, header in enumerate(headers) if header}
        for row in rows[1:]
    ]


def read_latest_csv_row(file_path: str | Path | None) -> dict[str, str] | None:
    """Read the header and a bounded tail instead of replaying years of CSV history."""
    if not file_path:
        return None
    if not allowed_path(file_path):
        return None
    try:
        import io
        with Path(file_path).open("rb") as stream:
            header = stream.readline(65536)
            if not header.endswith(b"\n") and len(header) >= 65536:
                return None
            header_end = stream.tell()
            stream.seek(0, 2)
            start = max(header_end, stream.tell() - 256 * 1024)
            stream.seek(start)
            tail = stream.read(256 * 1024)
            if start > header_end:
                tail = tail.partition(b"\n")[2]
        headers = next(csv.reader(io.StringIO(header.decode("utf-8-sig")), delimiter=";"), [])
        headers = [cell.strip().lstrip("\ufeff\u200b").lower() for cell in headers]
        latest = None
        for row in csv.reader(io.StringIO(tail.decode("utf-8")), delimiter=";"):
            if any(cell.strip() for cell in row):
                latest = {key: row[i].strip() if i < len(row) else "" for i, key in enumerate(headers) if key}
        return latest
    except (OSError, UnicodeError, csv.Error) as exc:
        logger.debug("Falha ao ler ultima linha CSV %s: %s", file_path, exc)
        return None
