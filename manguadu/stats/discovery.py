from pathlib import Path

from ..tenant_scope import allowed_path


def safe_child(root: Path, path: Path, *, enforce_scope=True) -> bool:
    try:
        if (enforce_scope and not allowed_path(path)) or not path.resolve().is_relative_to(root.resolve()):
            return False
        return not any(part.is_symlink() for part in (path, *path.parents))
    except (OSError, ValueError):
        return False


def discover(base: Path | None, preferred: str = "auto") -> tuple[str, Path | None, list[str]]:
    if base is None:
        return "none", None, []
    lua = base.parent if base.name in {"FriendHost_Data", "ttf_stats_mp"} else base
    friend = base if (base / "Jogadores").is_dir() else lua / "FriendHost_Data"
    ttf = base if base.name == "ttf_stats_mp" else lua / "ttf_stats_mp"
    found = {}
    if safe_child(lua, friend / "Jogadores") and (friend / "Jogadores").is_dir():
        found["friendhost"] = friend
    if safe_child(lua, ttf / "index.txt") and (ttf / "index.txt").is_file():
        found["ttf"] = ttf
    if preferred not in {"auto", "ttf", "friendhost"}:
        preferred = "auto"
    selected = next(iter(found), "none") if preferred == "auto" else preferred
    return (selected, found[selected], list(found)) if selected in found else ("none", None, list(found))


def lua_candidates(volume: Path) -> list[str]:
    """Fixed-depth known layouts only. Never walk saves/mods or follow links."""
    candidates = [volume / ".cache/Lua", volume / "Zomboid/Lua", volume / "Lua", volume / ".local/share/Zomboid/Lua"]
    return [p.relative_to(volume).as_posix() for p in candidates if safe_child(volume, p, enforce_scope=False) and p.is_dir()]
