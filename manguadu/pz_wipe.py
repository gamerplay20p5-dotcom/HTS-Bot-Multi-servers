"""Planejamento e execucao controlada dos wipes de saves Project Zomboid."""

from __future__ import annotations

from .i18n import t

from .tenant_scope import getenv

import asyncio
import os
import re
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .pterodactyl import PterodactylClient, extract_ptero_error
from .pz_data import PzData
from .utils import clean_text, get_field, to_number


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAFEHOUSE_BACKUP_PREFIX = "Safehouses geral - "


def parse_pair(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(-?\d+)\s*[,;:]\s*(-?\d+)\s*", clean_text(value))
    return (int(match.group(1)), int(match.group(2))) if match else None


def _save_root() -> str:
    return re.sub(r"[/\\]chunkdata[/\\]?$", "", clean_text(getenv("PZ_SAVE_ROOT")), flags=re.I)


def _cache_root(save_root: str) -> str:
    configured = clean_text(getenv("PZ_CACHE_ROOT"))
    if configured:
        return configured
    normalized = save_root.replace("\\", "/")
    match = re.match(r"^(.*?/\.cache)(?:/|$)", normalized)
    if match:
        return match.group(1)
    parts = re.split(r"[/\\]+", save_root)
    index = next((i for i, part in enumerate(parts) if part.casefold() == "saves"), -1)
    return os.path.sep.join(parts[:index]) if index > 0 else ""


def _safehouse_rectangles(data: PzData) -> list[dict[str, Any]]:
    rectangles = []
    for row in data.safehouses():
        x1 = to_number(get_field(row, ["x", "x1"]))
        y1 = to_number(get_field(row, ["y", "y1"]))
        x2 = to_number(get_field(row, ["x2"]), x1)
        y2 = to_number(get_field(row, ["y2"]), y1)
        rectangles.append({
            "title": get_field(row, ["title"], t('Sem titulo')),
            "minX": min(x1, x2), "minY": min(y1, y2),
            "maxX": max(x1, x2), "maxY": max(y1, y2),
        })
    return rectangles


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a["minX"] <= b["maxX"] and a["maxX"] >= b["minX"] and a["minY"] <= b["maxY"] and a["maxY"] >= b["minY"]


def _cell_rect(x: int, y: int) -> dict[str, int]:
    return {"minX": x * 300, "minY": y * 300, "maxX": x * 300 + 299, "maxY": y * 300 + 299}


def _chunk_rect(x: int, y: int) -> dict[str, int]:
    return {"minX": x * 10, "minY": y * 10, "maxX": x * 10 + 9, "maxY": y * 10 + 9}


def _chunk_names(x: int, y: int) -> list[str]:
    return [
        f"map_{x}_{y}.bin", f"chunkdata_{x}_{y}.bin",
        f"chunkdata/map_{x}_{y}.bin", f"chunkdata/chunkdata_{x}_{y}.bin",
    ]


def _cell_names(x: int, y: int) -> list[str]:
    start_x, start_y = x * 30, y * 30
    names = [f"zpop_{x}_{y}.bin"]
    for chunk_x in range(start_x, start_x + 30):
        for chunk_y in range(start_y, start_y + 30):
            names.extend(_chunk_names(chunk_x, chunk_y))
    return names


def _map_cells(target: str, data: PzData) -> tuple[str, list[tuple[int, int]]] | None:
    pair = parse_pair(target)
    if pair:
        return ''.join((format(t('celula '), ''), format(pair[0], ''), ',', format(pair[1], ''))), [pair]
    wanted = clean_text(target).casefold()
    map_entry = next((item for item in data.configured_maps() if item["id"].casefold() == wanted or item["name"].casefold() == wanted), None)
    if not map_entry:
        return None
    cells = [
        (x, y)
        for x in range(int(map_entry["minX"] // 300), int(map_entry["maxX"] // 300) + 1)
        for y in range(int(map_entry["minY"] // 300), int(map_entry["maxY"] // 300) + 1)
    ]
    return map_entry["name"], cells


def _local_files(root: Path) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    try:
        entries = list(root.iterdir())
    except OSError:
        return found
    for item in entries:
        if item.is_symlink():
            continue
        found[item.name] = {"name": item.name, "isFile": item.is_file(), "isDirectory": item.is_dir()}
        if item.is_dir() and item.name.casefold() == "chunkdata":
            try:
                for child in item.iterdir():
                    if not child.is_symlink():
                        found[f"chunkdata/{child.name}"] = {
                            "name": f"chunkdata/{child.name}", "isFile": child.is_file(), "isDirectory": child.is_dir(),
                        }
            except OSError:
                pass
    return found


async def _save_files(root: str) -> tuple[str, dict[str, dict[str, Any]]] | tuple[None, str]:
    local = Path(root)
    if local.is_absolute() and local.exists() and local.is_dir():
        return "local", _local_files(local)
    from .server_registry import ServerRegistry

    server = ServerRegistry().get_default_server()
    async with PterodactylClient(server) as client:
        result = await client.list_files(root)
        if not result["ok"]:
            return None, extract_ptero_error(result)
        files = {item["name"]: item for item in result.get("files", [])}
        if any(item.get("isDirectory") and item["name"].casefold() == "chunkdata" for item in files.values()):
            nested = await client.list_files(root.rstrip("/") + "/chunkdata")
            if nested["ok"]:
                files.update({f"chunkdata/{item['name']}": {**item, "name": f"chunkdata/{item['name']}"} for item in nested.get("files", [])})
        return "pterodactyl", files


async def build_wipe_plan(kind: str, target: str = "", *, force: bool = False, data: PzData | None = None) -> dict[str, Any]:
    data = data or PzData(getenv("CSV_BASE_PATH"))
    if kind == "global":
        save_root = _save_root()
        root = _cache_root(save_root)
        if not root:
            return {"ok": False, "error": t('PZ_CACHE_ROOT nao configurado e nao consegui derivar pelo PZ_SAVE_ROOT.')}
        path = Path(root)
        storage = "local" if path.is_absolute() and path.exists() and path.is_dir() else "pterodactyl"
        desired = [
            ("db", ("db", "DB")),
            ("Logs", ("Logs", "logs", "Log", "log")),
            ("Saves", ("Saves", "saves")),
        ]
        files = [
            found for _, aliases in desired
            if (found := next((name for name in aliases if (path / name).exists()), ""))
        ] if storage == "local" else [entry[0] for entry in desired]
        return {"ok": True, "root": root, "storage": storage, "kind": "global", "label": t('wipe global DB/Logs/Saves'), "force": True, "files": files, "allowDirectories": True, "skipped": [], "summary": {"directories": files}}

    root = _save_root()
    if not root:
        return {"ok": False, "error": t('PZ_SAVE_ROOT nao configurado no .env da VM.')}
    listing = await _save_files(root)
    if listing[0] is None:
        return {"ok": False, "error": ''.join((format(t('Falha ao listar save: '), ''), format(listing[1], '')))}
    storage, entries = listing
    existing = {name for name, item in entries.items() if item.get("isFile")}
    requested: set[str] = set()
    skipped: list[dict[str, Any]] = []
    summary = None
    label = ""
    protected = _safehouse_rectangles(data)

    if kind == "zeds":
        if not clean_text(target) or clean_text(target).casefold() == "todos":
            label = t('todos os mapas')
            requested.update(name for name in existing if re.fullmatch(r"zpop_-?\d+_-?\d+\.bin", name, re.I))
        else:
            resolved = _map_cells(target, data)
            if not resolved:
                return {"ok": False, "error": t('Mapa/celula nao encontrado. Use /mapas ou informe x,y.')}
            label, cells = resolved
            requested.update(f"zpop_{x}_{y}.bin" for x, y in cells)
    elif kind == "chunk":
        pair = parse_pair(target)
        if not pair:
            return {"ok": False, "error": t('Chunk invalido. Informe no formato x,y.')}
        x, y = pair
        label = f"chunk {x},{y}"
        rectangle = _chunk_rect(x, y)
        overlaps = [house["title"] for house in protected if _overlap(rectangle, house)]
        if overlaps and not force:
            skipped.append({"target": label, "safehouses": overlaps})
        else:
            requested.update(_chunk_names(x, y))
        cell_x, cell_y = x // 30, y // 30
        summary = {
            "cellStart": {"x": cell_x, "y": cell_y}, "cellEnd": {"x": cell_x, "y": cell_y},
            "chunkStart": {"x": x, "y": y}, "chunkEnd": {"x": x, "y": y}, "world": rectangle,
        }
    else:
        resolved = _map_cells(target, data)
        if not resolved:
            return {"ok": False, "error": t('Mapa/celula nao encontrado. Use /mapas ou informe x,y.')}
        label, cells = resolved
        for x, y in cells:
            overlaps = [house["title"] for house in protected if _overlap(_cell_rect(x, y), house)]
            if overlaps and not force:
                skipped.append({"target": ''.join((format(t('celula '), ''), format(x, ''), ',', format(y, ''))), "safehouses": overlaps})
                continue
            requested.update(_cell_names(x, y))
        xs, ys = [cell[0] for cell in cells], [cell[1] for cell in cells]
        min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
        summary = {
            "cellStart": {"x": min_x, "y": min_y}, "cellEnd": {"x": max_x, "y": max_y},
            "chunkStart": {"x": min_x * 30, "y": min_y * 30},
            "chunkEnd": {"x": max_x * 30 + 29, "y": max_y * 30 + 29},
            "world": {"minX": min_x * 300, "minY": min_y * 300, "maxX": max_x * 300 + 299, "maxY": max_y * 300 + 299},
        }
    return {
        "ok": True, "root": root, "storage": storage, "kind": kind, "label": label, "force": force,
        "files": sorted(requested.intersection(existing)), "skipped": skipped,
        "availableFileCount": len(existing), "summary": summary,
    }


def format_wipe_plan(plan: dict[str, Any], max_files: int = 30) -> str:
    if not plan.get("ok"):
        return plan.get("error", t('Plano de wipe invalido.'))
    lines = [
        ''.join((format(t('Alvo: '), ''), format(plan['label'], ''))), ''.join((format(t('Raiz: '), ''), format(plan['root'], ''))),
        ''.join((format(t('Modo: '), ''), format('local/VM' if plan['storage'] == 'local' else 'Pterodactyl API', ''))),
        ''.join((format(t('Arquivos encontrados: '), ''), format(len(plan['files']), ''))),
    ]
    if plan["kind"] == "global":
        lines.append(t('Operacao: apaga as pastas db/Logs/Saves do cache do servidor.'))
    else:
        lines.extend((''.join((format(t('Protecao de safehouse: '), ''), format(t('IGNORADA (force)') if plan['force'] else t('ATIVA'), ''))), ''.join((format(t('Alvos protegidos ignorados: '), ''), format(len(plan['skipped']), '')))))
    summary = plan.get("summary") or {}
    for key, label in (("cellStart", "Celulas"), ("chunkStart", "Chunks")):
        start, end = summary.get(key), summary.get("cellEnd" if key == "cellStart" else "chunkEnd")
        if start and end:
            lines.append(''.join((format(label, ''), ': ', format(start['x'], ''), ',', format(start['y'], ''), format(t(' ate '), ''), format(end['x'], ''), ',', format(end['y'], ''))))
    world = summary.get("world")
    if world:
        lines.append(''.join((format(t('Coordenadas mundo: X '), ''), format(world['minX'], ''), '-', format(world['maxX'], ''), ' | Y ', format(world['minY'], ''), '-', format(world['maxY'], ''))))
    if summary.get("directories"):
        lines.append(t('Pastas alvo: ') + ", ".join(summary["directories"]))
    lines.extend(f"- {item['target']}: {', '.join(item['safehouses'])}" for item in plan.get("skipped", [])[:10])
    if plan["files"]:
        lines.extend(("", t('Amostra de arquivos:'), *(f"- {name}" for name in plan["files"][:max_files])))
        if len(plan["files"]) > max_files:
            lines.append(''.join((format(t('... e mais '), ''), format(len(plan['files']) - max_files, ''))))
    return "\n".join(lines)


def _safe_relative(name: str) -> PurePosixPath | None:
    normalized = clean_text(name).replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


async def _wait_for_backup(client: PterodactylClient, backup_uuid: str) -> dict[str, Any]:
    wait_ms = max(60_000, int(to_number(getenv("PZ_WIPE_BACKUP_WAIT_MS"), 600_000)))
    deadline = time.monotonic() + wait_ms / 1000
    while time.monotonic() < deadline:
        result = await client.get_backup(backup_uuid)
        if not result["ok"]:
            return {"ok": False, "error": extract_ptero_error(result)}
        backup = result.get("backup", {})
        if backup.get("completedAt"):
            return {"ok": bool(backup.get("isSuccessful")), "backup": backup, "error": t('O backup pre-wipe terminou com falha.')}
        await asyncio.sleep(10)
    return {"ok": False, "error": t('Tempo limite aguardando o backup pre-wipe.')}


def _local_backup(plan: dict[str, Any]) -> dict[str, Any]:
    root = Path(plan["root"]).resolve()
    backup_dir = Path(getenv("PZ_LOCAL_BACKUP_DIR") or PROJECT_ROOT / "data" / "local-wipe-backups").resolve()
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = backup_dir / f"pre-wipe-{timestamp}.tar.gz"
    try:
        with tarfile.open(archive, "w:gz") as tar:
            for name in plan["files"]:
                relative = _safe_relative(name)
                if relative is None:
                    return {"ok": False, "error": ''.join((format(t('Nome de arquivo inseguro no plano: '), ''), format(name, '')))}
                source = (root / Path(*relative.parts)).resolve()
                if source != root and root not in source.parents:
                    return {"ok": False, "error": ''.join((format(t('Arquivo fora da raiz de save: '), ''), format(name, '')))}
                if source.exists():
                    tar.add(source, arcname=relative.as_posix(), recursive=bool(plan.get("allowDirectories")))
        os.chmod(archive, 0o600)
    except (OSError, tarfile.TarError) as exc:
        archive.unlink(missing_ok=True)
        return {"ok": False, "error": ''.join((format(t('Falha ao criar backup local: '), ''), format(exc, '')))}
    return {"ok": True, "backup": {"uuid": f"local:{archive.name}", "name": archive.name, "completedAt": datetime.now(timezone.utc).isoformat(), "isSuccessful": True, "archivePath": str(archive)}}


async def _pre_wipe_backup(plan: dict[str, Any]) -> dict[str, Any]:
    from .server_registry import ServerRegistry

    server = ServerRegistry().get_default_server()
    async with PterodactylClient(server) as client:
        result = await client.create_backup(name=f"Pre-wipe {datetime.now(timezone.utc).isoformat()} - {plan['label']}", is_locked=True)
        if result["ok"]:
            attributes = (result.get("json") or {}).get("attributes") or {}
            backup_uuid = clean_text(attributes.get("uuid") or attributes.get("identifier"))
            if backup_uuid:
                complete = await _wait_for_backup(client, backup_uuid)
                if complete["ok"]:
                    return complete
                if plan["storage"] != "local":
                    return complete
    if plan["storage"] == "local":
        return await asyncio.to_thread(_local_backup, plan)
    return {"ok": False, "error": ''.join((format(t('Falha ao iniciar backup pre-wipe: '), ''), format(extract_ptero_error(result), '')))}


async def execute_wipe_plan(plan: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    if not plan.get("ok"):
        return plan
    if dry_run:
        return {"ok": True, "dryRun": True, "plan": plan}
    if not plan["files"]:
        return {"ok": False, "error": t('Nenhum arquivo correspondente foi encontrado; nada foi apagado.'), "plan": plan}
    from .server_integrations import fetch_server_resources
    from .server_registry import ServerRegistry

    server = ServerRegistry().get_default_server()
    resources = await fetch_server_resources(server)
    if not resources["ok"]:
        from .server_integrations import describe_server_error
        return {"ok": False, "error": ''.join((format(t('Nao foi possivel confirmar o estado do servidor: '), ''), format(describe_server_error(resources), '')))}
    if clean_text(resources.get("state")).casefold() != "offline":
        return {"ok": False, "error": ''.join((format(t('Servidor precisa estar offline para wipe. Estado atual: '), ''), format(resources.get('state'), ''), '.'))}
    backup = await _pre_wipe_backup(plan)
    if not backup.get("ok"):
        return {"ok": False, "error": backup.get("error", t('Backup pre-wipe falhou.')), "backup": backup.get("backup")}
    if plan["storage"] == "local":
        root = Path(plan["root"]).resolve()
        deleted = []
        for name in plan["files"]:
            relative = _safe_relative(name)
            if relative is None:
                return {"ok": False, "error": ''.join((format(t('Arquivo inseguro no plano: '), ''), format(name, ''))), "deleted": deleted, "backup": backup["backup"]}
            target = (root / Path(*relative.parts)).resolve()
            if target != root and root not in target.parents:
                return {"ok": False, "error": ''.join((format(t('Arquivo fora da raiz de save: '), ''), format(name, ''))), "deleted": deleted, "backup": backup["backup"]}
            try:
                if target.is_dir():
                    if not plan.get("allowDirectories"):
                        return {"ok": False, "error": ''.join((format(t('Diretorio nao permitido no plano: '), ''), format(name, ''))), "deleted": deleted, "backup": backup["backup"]}
                    import shutil
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
                deleted.append(name)
            except OSError as exc:
                return {"ok": False, "error": ''.join((format(t('Falha ao apagar '), ''), format(name, ''), ': ', format(exc, ''))), "deleted": deleted, "backup": backup["backup"]}
        return {"ok": True, "plan": plan, "deleted": deleted, "backup": backup["backup"]}
    deleted = []
    async with PterodactylClient(server) as client:
        for index in range(0, len(plan["files"]), 100):
            batch = plan["files"][index:index + 100]
            result = await client.delete_files(plan["root"], batch)
            if not result["ok"]:
                return {"ok": False, "error": ''.join((format(t('Falha ao apagar lote: '), ''), format(extract_ptero_error(result), ''))), "deleted": deleted, "backup": backup["backup"]}
            deleted.extend(batch)
    return {"ok": True, "plan": plan, "deleted": deleted, "backup": backup["backup"]}
