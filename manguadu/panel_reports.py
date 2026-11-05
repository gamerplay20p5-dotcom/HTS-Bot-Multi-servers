"""Construcao dos embeds de status e ranking do servidor."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import discord

from .panel_theme import color_to_number, get_media_url, get_saved_theme, get_theme_attachments
from .utils import get_field, to_number


def _limited_lines(lines: list[str], maximum: int = 4000) -> str:
    output = []
    size = 0
    for line in lines:
        extra = len(line) + (1 if output else 0)
        if size + extra > maximum:
            break
        output.append(line)
        size += extra
    return "\n".join(output)


def _medal(index: int) -> str:
    return ("🥇", "🥈", "🥉")[index] if index < 3 else f"**{index + 1}.**"


def _format_duration(milliseconds: float) -> str:
    seconds = max(0, int(milliseconds / 1000))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _faction_leaderboard(player_rows: list[dict], faction_rows: list[dict]) -> list[dict[str, Any]]:
    factions: dict[str, dict[str, Any]] = {}

    def ensure(name: str) -> dict[str, Any] | None:
        name = name.strip()
        if not name:
            return None
        return factions.setdefault(name, {"name": name, "owner": "", "tag": "", "members": set(), "kills": 0.0, "hours": 0.0})

    for row in faction_rows:
        entry = ensure(get_field(row, ["name"]))
        if not entry:
            continue
        entry["owner"] = get_field(row, ["owner"], entry["owner"])
        entry["tag"] = get_field(row, ["tagname"], entry["tag"])
        players = get_field(row, ["players"])
        for member in players.strip("[]()").replace("'", "").replace('"', "").replace("|", ",").replace(";", ",").split(","):
            if member.strip():
                entry["members"].add(member.strip())
        if entry["owner"]:
            entry["members"].add(entry["owner"])
    for row in player_rows:
        data = row.get("row", row)
        entry = ensure(get_field(data, ["factionname"]))
        if not entry:
            continue
        username = get_field(data, ["username"])
        if username:
            entry["members"].add(username)
        entry["kills"] += to_number(get_field(data, ["zombiekills"]))
        entry["hours"] += to_number(get_field(data, ["hourssurvived"]))
    return sorted(factions.values(), key=lambda item: (-item["kills"], -item["hours"], -len(item["members"])))


def _death_leaderboard(player_rows: list[dict], death_rows: list[list[str]] | dict[str, int]) -> list[tuple[str, int]]:
    if isinstance(death_rows, dict):
        return sorted(death_rows.items(), key=lambda item: (-item[1], item[0].casefold()))
    names = [get_field(item.get("row", item), ["username"]) for item in player_rows]
    counts = {name: 0 for name in names if name}
    for row in death_rows:
        cells = {str(cell or "").strip().casefold() for cell in row}
        matched = next((name for name in counts if name.casefold() in cells), None)
        if matched:
            counts[matched] += 1
    return sorted(((name, count) for name, count in counts.items() if count), key=lambda item: (-item[1], item[0].casefold()))


def _ranking_description(rows: list, format_value, limit: int = 5) -> str:
    return _limited_lines([f"{_medal(index)} **{item['name']}**\n> {format_value(item)}" for index, item in enumerate(rows[:limit])])


def build_ranking_panel(player_rows: list[dict], faction_rows: list[dict], death_rows: list[list[str]] | dict[str, int] | None = None, *, theme: dict | None = None) -> tuple[list[discord.Embed], list[discord.File]]:
    theme = theme or get_saved_theme()
    primary = color_to_number(theme["primaryColor"])
    secondary = color_to_number(theme["secondaryColor"], 0xF0A93B)
    rows = [item.get("row", item) for item in player_rows]
    total_kills = sum(to_number(get_field(row, ["zombiekills"])) for row in rows)
    total_hours = sum(to_number(get_field(row, ["hourssurvived"])) for row in rows)
    factions = _faction_leaderboard(player_rows, faction_rows)
    deaths = _death_leaderboard(player_rows, death_rows or [])
    kills = sorted(({"name": get_field(row, ["username"], "Sobrevivente"), "value": to_number(get_field(row, ["zombiekills"]))} for row in rows), key=lambda item: -item["value"])
    hours = sorted(({"name": get_field(row, ["username"], "Sobrevivente"), "value": to_number(get_field(row, ["hourssurvived"]))} for row in rows), key=lambda item: -item["value"])
    top_factions = []
    for item in factions:
        label = item["name"] + (f" [{item['tag']}]" if item["tag"] else "")
        top_factions.append({**item, "name": label})

    header = discord.Embed(title=f"🏆 {theme['title']} · Hall da Sobrevivência", description=theme["description"], color=primary)
    header.add_field(name="SOBREVIVENTES", value=f"`{len(rows)}`", inline=True)
    header.add_field(name="ABATES", value=f"`{int(total_kills):,}`".replace(",", "."), inline=True)
    header.add_field(name="HORAS VIVIDAS", value=f"`{total_hours:.1f}h`", inline=True)
    if image := get_media_url(theme.get("rankingBackground")):
        header.set_image(url=image)
    if image := get_media_url(theme.get("thumbnail")):
        header.set_thumbnail(url=image)
    embeds = [header]
    sections = (
        (kills, secondary, "🧟 CAÇADORES DE ELITE", lambda item: f"{int(item['value']):,}".replace(",", ".") + " eliminações"),
        (hours, primary, "⌛ VETERANOS DO APOCALIPSE", lambda item: f"{item['value']:.1f} horas sobrevividas"),
        (top_factions, secondary, "🛡️ DOMÍNIO DAS FACÇÕES", lambda item: f"{int(item['kills']):,}".replace(",", ".") + f" abates · {len(item['members'])} membros"),
        ([{"name": name, "value": count} for name, count in deaths], 0xC74343, "☠️ OS QUE MAIS VOLTARAM", lambda item: f"{item['value']} mortes registradas"),
    )
    for items, color, title, formatter in sections:
        if items:
            embeds.append(discord.Embed(title=title, description=_ranking_description(items, formatter), color=color))
    if factions:
        leader = factions[0]
        label = leader["name"] + (f" [{leader['tag']}]" if leader["tag"] else "")
        embeds.append(discord.Embed(title="👑 FACÇÃO EM DESTAQUE", description=f"**{label}** lidera com **{int(leader['kills']):,} abates**, **{leader['hours']:.1f} horas** e **{len(leader['members'])} membros**.".replace(",", "."), color=primary))
    embeds[-1].set_footer(text="Ranking atualizado silenciosamente a cada 10 minutos")
    embeds[-1].timestamp = datetime.now(timezone.utc)
    return embeds, get_theme_attachments(theme, "ranking")


def build_server_panel(overview: dict, *, online_profiles: list[dict] | None = None, theme: dict | None = None, max_slots: int | None = None, connect_address: str = "") -> tuple[list[discord.Embed], list[discord.File]]:
    theme = theme or get_saved_theme()
    primary, secondary = color_to_number(theme["primaryColor"]), color_to_number(theme["secondaryColor"], 0xF0A93B)
    ptero = overview.get("ptero", {})
    is_docker = ptero.get("transport") == "docker"
    has_panel = ptero.get("ok") and not is_docker
    has_rcon = overview.get("rconPlayers", {}).get("ok", False)
    resources = ptero.get("resources", {})
    uptime = to_number(resources.get("uptime"))
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    last_boot = int((now_ms - uptime) / 1000) if uptime > 0 else 0
    players = overview.get("onlinePlayers", {"count": 0, "names": [], "source": "none"})
    state = str(overview.get("currentState") or "desconhecido").lower()
    labels = {"running": "ONLINE", "starting": "INICIANDO", "stopping": "DESLIGANDO", "offline": "OFFLINE"}
    main = discord.Embed(title=theme["title"], description=f"{'🟢 **MUNDO OPERACIONAL**' if overview.get('isOnline') else '🔴 **MUNDO INDISPONÍVEL**'}\n{theme['description']}", color=primary if overview.get("isOnline") else 0xC74343)
    main.add_field(name="STATUS", value=f"`{labels.get(state, state.upper())}`", inline=True)
    main.add_field(name="JOGADORES", value=f"`{players.get('count', 0)}{f' / {max_slots}' if max_slots else ''}`", inline=True)
    latency = ptero.get("latencyMs")
    control = f"`{latency} ms`" if has_panel and latency is not None else "`Docker/VM`" if is_docker and ptero.get("ok") else "`indisponível`"
    main.add_field(name="CONTROLE LOCAL" if is_docker else "PING DO PAINEL", value=control, inline=True)
    world = overview.get("world", {})
    world_lines = []
    if world.get("gameDate"): world_lines.append(f"📅 **Dia:** {world['gameDate']}")
    if world.get("timeOfDay"): world_lines.append(f"🕒 **Hora:** {world['timeOfDay']}")
    if world.get("temperature"): world_lines.append(f"🌡️ **Temperatura:** {world['temperature']} °C")
    if world_lines: main.add_field(name="MUNDO", value="\n".join(world_lines), inline=True)
    infrastructure = [f"Painel {'🟢' if has_panel else '🔴'} · Docker {'🟢' if is_docker else '🟠'} · RCON {'🟢' if has_rcon else '🟠'}", f"Fonte: **{'RCON ao vivo' if players.get('source') == 'rcon' else 'FriendHost' if players.get('source') == 'csv' else 'sem leitura'}**"]
    if uptime > 0: infrastructure.append(f"Uptime: **{_format_duration(uptime)}**")
    if last_boot > 0: infrastructure.append(f"Último boot: <t:{last_boot}:R>")
    main.add_field(name="INFRAESTRUTURA", value="\n".join(infrastructure), inline=True)
    if connect_address: main.add_field(name="CONECTAR", value=f"`{connect_address}`", inline=False)
    if image := get_media_url(theme.get("banner")): main.set_image(url=image)
    if image := get_media_url(theme.get("thumbnail")): main.set_thumbnail(url=image)
    main.set_footer(text="Monitoramento ao vivo · atualização silenciosa a cada 10 min")
    main.timestamp = datetime.now(timezone.utc)
    profiles = online_profiles or []
    embeds = [main]
    can_use_player_cards = 0 < len(players.get("names", [])) <= 8 and any(profile.get("avatarUrl") for profile in profiles)
    if can_use_player_cards:
        for name in players["names"]:
            profile = next((item for item in profiles if item["nick"].casefold() == name.casefold()), {"nick": name})
            details = [f"🛡️ {profile['faction']}" if profile.get("faction") else "", f"⏱️ online há {profile['onlineTime']}" if profile.get("onlineTime") else "", f"<@{profile['discordId']}>" if profile.get("discordId") else ""]
            card = discord.Embed(description=" · ".join(item for item in details if item) or "Sobrevivente em atividade", color=secondary)
            if profile.get("avatarUrl"):
                card.set_author(name=name, icon_url=profile["avatarUrl"])
            else:
                card.set_author(name=name)
            embeds.append(card)
    elif players.get("names"):
        online = discord.Embed(title=f"👥 {players['count']} sobrevivente{'s' if players['count'] != 1 else ''} online", color=secondary)
        lines = []
        for name in players["names"]:
            profile = next((item for item in profiles if item["nick"].casefold() == name.casefold()), {})
            details = [f"🛡️ {profile['faction']}" if profile.get("faction") else "", f"⏱️ online há {profile['onlineTime']}" if profile.get("onlineTime") else "", f"<@{profile['discordId']}>" if profile.get("discordId") else ""]
            lines.append(f"▸ **{name}**" + ("\n  " + " · ".join(item for item in details if item) if any(details) else ""))
        online.description = _limited_lines(lines)
        embeds.append(online)
    else:
        embeds.append(discord.Embed(title="👥 0 sobreviventes online", description="A estrada está silenciosa. Nenhum sobrevivente conectado agora.", color=secondary))
    return embeds, get_theme_attachments(theme, "status")


def build_server_status_embed(overview: dict, footer_text: str = "") -> discord.Embed:
    ptero = overview.get("ptero", {})
    rcon_ok = overview.get("rconPlayers", {}).get("ok", False)
    has_panel = ptero.get("ok") and ptero.get("transport") != "docker"
    has_docker = ptero.get("ok") and ptero.get("transport") == "docker"
    online = overview.get("isOnline", False)
    color = 0x2ECC71 if online and rcon_ok and has_panel else 0xF39C12 if online else 0xE74C3C
    state = str(overview.get("currentState") or "Desconhecido").casefold()
    state_label = {"running": "Online", "starting": "Iniciando", "stopping": "Desligando", "offline": "Offline"}.get(state, state.title())
    players = overview.get("onlinePlayers", {})
    source = {"rcon": "RCON ao vivo", "csv": "FriendHost", "none": "Sem leitura"}.get(players.get("source"), players.get("source", "Desconhecido"))
    names = players.get("names", [])
    embed = discord.Embed(
        title="📡 Painel do servidor",
        description=(f"Servidor operacional. {players.get('count', 0)} sobrevivente(s) ativo(s)." if online else "Servidor indisponível ou em reinicialização."),
        color=color, timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="🖥️ Servidor", value=f"Estado\n`{state_label}`\n\nPainel\n`{'Conectado' if has_panel else 'Fallback Docker' if has_docker else 'Indisponível'}`\n\nRCON\n`{'Conectado' if rcon_ok else 'Indisponível'}`", inline=True)
    world = overview.get("world", {})
    embed.add_field(name="🌍 Mundo", value=f"Hora\n`{world.get('timeOfDay') or 'N/A'}`\n\nDia\n`{world.get('gameDate') or 'N/A'}`\n\nTemperatura\n`{world.get('temperature') + ' C' if world.get('temperature') else 'N/A'}`", inline=True)
    embed.add_field(name="📶 Leitura", value=f"Players\n`{players.get('count', 0)}`\n\nLeitura\n`{source}`\n\nAtualização\n`60s`", inline=True)
    embed.add_field(name="🧍 Sobreviventes online", value=_limited_lines([f"{index}. {name}" for index, name in enumerate(names, 1)], 1024) or "Nenhum sobrevivente online no momento.", inline=False)
    if footer_text: embed.set_footer(text=footer_text)
    return embed
