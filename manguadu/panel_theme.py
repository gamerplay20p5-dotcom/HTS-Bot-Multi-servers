"""Tema visual compartilhado pelos paineis de status e ranking."""

from __future__ import annotations

from .i18n import t

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import aiohttp
import discord

from .utils import clean_text
from .tenant_scope import tenant_path


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MEDIA_DIR = DATA_DIR / "panel-media"
THEME_FILE = DATA_DIR / "panel-theme.json"
DRAFT_FILE = DATA_DIR / "panel-theme-draft.json"
MAX_MEDIA_BYTES = 25 * 1024 * 1024
DEFAULT_THEME = {
    "title": 'Servidor Project Zomboid',
    "description": 'Sobreviva. Evolua. Deixe sua marca no apocalipse.',
    "primaryColor": "#28D17C",
    "secondaryColor": "#F0A93B",
    "banner": None,
    "thumbnail": None,
    "rankingBackground": None,
}


def _normalize_color(value: object, fallback: str) -> str:
    color = clean_text(value).removeprefix("#")
    return f"#{color.upper()}" if re.fullmatch(r"[0-9a-fA-F]{6}", color) else fallback


def _normalize_media(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    url, file_path = clean_text(value.get("url")), clean_text(value.get("filePath"))
    if not url and not file_path:
        return None
    filename = clean_text(value.get("filename")) or (Path(file_path).name if file_path else "")
    return {"url": url, "filePath": file_path, "filename": filename}


def normalize_theme(raw: object = None) -> dict:
    source = raw if isinstance(raw, dict) else {}
    return {
        "title": clean_text(source.get("title")) or DEFAULT_THEME["title"],
        "description": clean_text(source.get("description")) or DEFAULT_THEME["description"],
        "primaryColor": _normalize_color(source.get("primaryColor"), DEFAULT_THEME["primaryColor"]),
        "secondaryColor": _normalize_color(source.get("secondaryColor"), DEFAULT_THEME["secondaryColor"]),
        "banner": _normalize_media(source.get("banner")),
        "thumbnail": _normalize_media(source.get("thumbnail")),
        "rankingBackground": _normalize_media(source.get("rankingBackground")),
    }


def _read(path: Path, fallback: dict) -> dict:
    try:
        return normalize_theme(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return normalize_theme(fallback)


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def get_saved_theme() -> dict:
    fallback = {**DEFAULT_THEME, "title": t("Servidor Project Zomboid"), "description": t("Sobreviva. Evolua. Deixe sua marca no apocalipse.")}
    return _read(tenant_path("panel-theme.json", THEME_FILE), fallback)


def get_draft_theme() -> dict:
    return _read(tenant_path("panel-theme-draft.json", DRAFT_FILE), get_saved_theme())


def update_draft_theme(patch: dict) -> dict:
    theme = normalize_theme({**get_draft_theme(), **patch})
    _write(tenant_path("panel-theme-draft.json", DRAFT_FILE), theme)
    return theme


def save_draft_theme() -> dict:
    theme = get_draft_theme()
    _write(tenant_path("panel-theme.json", THEME_FILE), theme)
    _write(tenant_path("panel-theme-draft.json", DRAFT_FILE), theme)
    return theme


def color_to_number(value: object, fallback: int = 0x28D17C) -> int:
    color = _normalize_color(value, "")
    return int(color[1:], 16) if color else fallback


def youtube_thumbnail(value: str) -> str:
    try:
        parsed = urlparse(clean_text(value))
        host = (parsed.hostname or "").lower()
        video_id = ""
        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]
        elif host.endswith("youtube.com"):
            video_id = parse_qs(parsed.query).get("v", [""])[0]
            if not video_id:
                match = re.search(r"/(?:shorts|embed)/([^/?]+)", parsed.path)
                video_id = match.group(1) if match else ""
        return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg" if video_id else ""
    except ValueError:
        return ""


def get_media_url(media: dict | None) -> str:
    if not media:
        return ""
    if media.get("url"):
        return media["url"]
    return f"attachment://{media['filename']}" if media.get("filename") else ""


def get_theme_attachments(theme: dict, panel_type: str = "status") -> list[discord.File]:
    items = [theme.get("thumbnail"), theme.get("rankingBackground") if panel_type == "ranking" else theme.get("banner")]
    seen: set[str] = set()
    files = []
    for media in items:
        if not isinstance(media, dict) or not media.get("filePath"):
            continue
        path = Path(media["filePath"])
        key = str(path)
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        files.append(discord.File(str(path), filename=media.get("filename") or path.name))
    return files


def _safe_filename(value: str, fallback: str) -> str:
    source = Path(clean_text(value)).name
    suffix = re.sub(r"[^.a-z0-9]", "", Path(source).suffix.lower()) or ".bin"
    stem = re.sub(r"[^a-z0-9_-]", "-", Path(source).stem, flags=re.I)[:50]
    return f"{stem or fallback}{suffix}"


async def _download(url: str, destination: Path) -> tuple[str, int]:
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as response:
            if response.status < 200 or response.status >= 300:
                raise ValueError(''.join((format(t('Falha ao baixar mídia: HTTP '), ''), format(response.status, ''), '.')))
            length = int(response.headers.get("Content-Length") or 0)
            if length > MAX_MEDIA_BYTES:
                raise ValueError(t('A mídia excede o limite de 25 MB.'))
            total = 0
            with destination.open("wb") as stream:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > MAX_MEDIA_BYTES:
                        raise ValueError(t('A mídia excede o limite de 25 MB.'))
                    stream.write(chunk)
            return response.headers.get("Content-Type", "").split(";", 1)[0].lower(), total


async def _video_thumbnail(source: Path, output: Path) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(source), "-frames:v", "1", "-vf", "scale=1280:-2", str(output),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ValueError(t('ffmpeg não encontrado. Instale-o na VM para converter vídeos.')) from exc
    _, stderr = await process.communicate()
    if process.returncode:
        raise ValueError(''.join((format(t('Falha ao converter vídeo com ffmpeg: '), ''), format(stderr.decode(errors='replace')[-300:], ''))))


async def store_panel_media(kind: str, *, url: str = "", attachment: discord.Attachment | None = None) -> dict[str, str]:
    external = clean_text(url)
    thumb = youtube_thumbnail(external)
    if thumb:
        return {"url": thumb, "filePath": "", "filename": ""}
    if external and attachment is None:
        parsed = urlparse(external)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(t('Informe uma URL HTTP/HTTPS válida.'))
        if re.search(r"\.(?:mp4|webm|mov|mkv)(?:$|\?)", external, re.I):
            tenant_path("panel-media", MEDIA_DIR).mkdir(parents=True, exist_ok=True)
            suffix = Path(parsed.path).suffix.lower() or ".mp4"
            source = tenant_path("panel-media", MEDIA_DIR) / f"{kind}-{int(time.time() * 1000)}{suffix}"
            output = tenant_path("panel-media", MEDIA_DIR) / f"{kind}-{int(time.time() * 1000)}.png"
            try:
                await _download(external, source)
                await _video_thumbnail(source, output)
            finally:
                source.unlink(missing_ok=True)
            return {"url": "", "filePath": str(output), "filename": output.name}
        return {"url": external, "filePath": "", "filename": ""}
    if attachment is None or not attachment.url:
        raise ValueError(t('Envie um arquivo ou informe uma URL.'))
    tenant_path("panel-media", MEDIA_DIR).mkdir(parents=True, exist_ok=True)
    filename = f"{kind}-{int(time.time() * 1000)}-{_safe_filename(attachment.filename, kind)}"
    destination = tenant_path("panel-media", MEDIA_DIR) / filename
    content_type, _ = await _download(attachment.url, destination)
    is_video = content_type.startswith("video/") or Path(attachment.filename).suffix.lower() in {".mp4", ".webm", ".mov", ".mkv"}
    if is_video:
        output = tenant_path("panel-media", MEDIA_DIR) / f"{kind}-{int(time.time() * 1000)}.png"
        try:
            await _video_thumbnail(destination, output)
        finally:
            destination.unlink(missing_ok=True)
        return {"url": "", "filePath": str(output), "filename": output.name}
    if content_type and not content_type.startswith("image/"):
        destination.unlink(missing_ok=True)
        raise ValueError(t('O arquivo precisa ser imagem, GIF ou vídeo.'))
    return {"url": "", "filePath": str(destination), "filename": filename}
