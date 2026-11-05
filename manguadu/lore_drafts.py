"""Keep pending long forms on disk instead of retaining many 40 MB texts in views."""
import time
import zlib
from pathlib import Path
from uuid import uuid4

from .tenant_scope import tenant_path

def cleanup_drafts() -> None:
    root = tenant_path("lore-drafts")
    for previous in root.glob("*.z"):
        if previous.is_file() and not previous.is_symlink() and time.time() - previous.stat().st_mtime > 1200:
            previous.unlink(missing_ok=True)

def save_draft(content: str) -> Path:
    root = tenant_path("lore-drafts")
    root.mkdir(parents=True, exist_ok=True)
    cleanup_drafts()
    target = root / f"{uuid4().hex}.z"
    target.write_bytes(zlib.compress(content.encode("utf-8"), level=3))
    return target

def load_draft(path: Path) -> str:
    try:
        try:
            return zlib.decompress(path.read_bytes()).decode("utf-8")
        except zlib.error as exc:
            raise ValueError("Arquivo temporario corrompido.") from exc
    finally:
        path.unlink(missing_ok=True)
