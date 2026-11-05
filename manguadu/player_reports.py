"""Embeds e relatorios completos produzidos a partir de uma ficha PZ."""

from __future__ import annotations

from .i18n import t

from datetime import datetime, timezone
from typing import Any

import discord

from .player_data import get_perk_entries
from .utils import get_field, to_number


def _truthy(value: str) -> bool:
    return value.strip().casefold() in {"1", "true", "yes", "sim", "alive"}


def _health_emoji(alive: bool, value: str) -> str:
    if not alive: return "💀"
    health = to_number(value, float("nan"))
    if health != health: return "🫀"
    return "💚" if health >= 75 else "💛" if health >= 40 else "❤️"


def _format_perks(perks: list[dict], limit: int) -> str:
    if not perks: return t('✨ Nenhuma skill acima de 0 registrada.')
    return "\n".join(f"{item['emoji']} {item['label']} • `{item['value']:g}`" for item in perks[:limit])


def _format_safehouse(row: dict) -> str:
    title = get_field(row, ["title"], t('Sem título'))
    owner = get_field(row, ["owner"], t('Sem dono'))
    return ''.join((format(title, ''), format(t(' | dono: '), ''), format(owner, ''), format(t(' | área: ('), ''), format(get_field(row, ['x'], '?'), ''), ', ', format(get_field(row, ['y'], '?'), ''), ') -> (', format(get_field(row, ['x2'], '?'), ''), ', ', format(get_field(row, ['y2'], '?'), ''), ')'))


def build_status_embed(snapshot: dict) -> discord.Embed:
    if snapshot.get("provider") == "ttf":
        from .stats.reports import profile
        return profile(snapshot)
    row = snapshot.get("playerRow") or {}
    alive = _truthy(get_field(row, ["isalive"]))
    emoji = _health_emoji(alive, get_field(row, ["health"]))
    title = get_field(row, ["charname"], snapshot["actualNick"])
    faction = snapshot.get("faction") or {}
    faction_name = faction.get("name") or get_field(row, ["factionname"], t('Lobo solitário'))
    faction_tag = faction.get("tag") or get_field(row, ["factiontag"])
    safehouses = snapshot.get("safehouses", [])
    safehouse = get_field(row, ["safehousetitle"]) or get_field(safehouses[0] if safehouses else {}, ["title"], t('Nenhuma vinculada'))
    perks = get_perk_entries(snapshot.get("perksRow"), False)
    embed = discord.Embed(title=''.join((format(t('🧾 Perfil de '), ''), format(title, ''))), color=0x2ECC71 if alive else 0xE74C3C, timestamp=datetime.now(timezone.utc))
    embed.description = "\n".join((
        ''.join((format(t('🎮 **Nick:** '), ''), format(snapshot['actualNick'], ''))), ''.join((format(emoji, ''), format(t(' **Status:** '), ''), format(t('Vivo') if alive else t('Morto'), ''))),
        ''.join((format(t('🛡️ **Facção:** '), ''), format(faction_name, ''), format(''.join((' [', format(faction_tag, ''), ']')) if faction_tag else '', ''))), ''.join((format(t('🏠 **Safehouse:** '), ''), format(safehouse, ''))),
    ))
    fields = [
        (t('🧰 Profissão'), get_field(row, ["profession"], "N/A"), True),
        (t('🧟 Zumbis mortos'), f"{to_number(get_field(row, ['zombiekills'])):g}", True),
        (t('⏳ Horas vividas'), f"{to_number(get_field(row, ['hourssurvived'])):.1f}", True),
        (''.join((format(emoji, ''), format(t(' Vida'), ''))), get_field(row, ["health"], "N/A"), True),
        ("🆔 SteamID", get_field(row, ["steamid"], "N/A"), True),
        (t('📍 Posição'), f"{get_field(row, ['x'], '?')}, {get_field(row, ['y'], '?')}, {get_field(row, ['z'], '?')}", True),
        (t('✨ Skills em destaque'), "\n".join(f"{item['emoji']} {item['label']} • `{item['value']:g}`" for item in perks[:5]) or t('✨ Nenhuma skill acima de 0 registrada.'), False),
        (t('⚔️ Arma favorita'), get_field(row, ["favoriteweapon"], t('Nenhuma')), False),
    ]
    for name, value, inline in fields: embed.add_field(name=name, value=(value or "N/A")[:1024], inline=inline)
    return embed


def build_status_complete_embed(snapshot: dict) -> discord.Embed:
    if snapshot.get("provider") == "ttf":
        from .stats.reports import profile
        return profile(snapshot)
    row = snapshot.get("playerRow") or {}
    alive = _truthy(get_field(row, ["isalive"]))
    emoji = _health_emoji(alive, get_field(row, ["health"]))
    perks = get_perk_entries(snapshot.get("perksRow"), False)
    faction = snapshot.get("faction") or {}
    faction_name = faction.get("name") or get_field(row, ["factionname"], t('Sem facção'))
    tag = faction.get("tag") or get_field(row, ["factiontag"])
    inventory = snapshot["inventorySummary"]
    db_data = snapshot["dbData"]
    embed = discord.Embed(title=''.join((format(t('📚 Ficha completa de '), ''), format(get_field(row, ['charname'], snapshot['actualNick']), ''))), color=0x1ABC9C if alive else 0xC0392B, timestamp=datetime.now(timezone.utc))
    embed.description = ''.join((format(t('🎮 **Nick:** '), ''), format(snapshot['actualNick'], ''), '\n', format(emoji, ''), format(t(' **Status:** '), ''), format(t('Vivo') if alive else t('Morto'), ''), format(t('\n🛡️ **Facção:** '), ''), format(faction_name, ''), format(''.join((' [', format(tag, ''), ']')) if tag else '', ''), format(t('\n🏠 **Safehouses vinculadas:** '), ''), format(len(snapshot['safehouses']), '')))
    embed.add_field(name=t('🫀 Sobrevivência'), value=''.join((format(t('🧟 Zumbis: `'), ''), format(to_number(get_field(row, ['zombiekills'])), ''.join(('g',))), format(t('`\n⏳ Horas: `'), ''), format(to_number(get_field(row, ['hourssurvived'])), ''.join(('.1f',))), '`\n', format(emoji, ''), format(t(' Vida: `'), ''), format(get_field(row, ['health'], 'N/A'), ''), format(t('`\n🦠 Infecção: `'), ''), format(get_field(row, ['infectionlevel'], '0'), ''), '`')), inline=True)
    embed.add_field(name=t('🎒 Inventário'), value=''.join((format(t('📦 Linhas: `'), ''), format(inventory['totalRows'], ''), format(t('`\n🧩 Itens únicos: `'), ''), format(inventory['uniqueItems'], ''), format(t('`\n🗃️ Categorias: `'), ''), format(len(inventory['categories']), ''), '`')), inline=True)
    embed.add_field(name=t('🛰️ Extras'), value=''.join((format(t('✨ Skills > 0: `'), ''), format(len(perks), ''), format(t('`\n💾 DBs com match: `'), ''), format(len(db_data['sqliteMatches']), ''), format(t('`\n📄 Arquivos com match: `'), ''), format(len(db_data['textMatches']), ''), '`')), inline=True)
    embed.add_field(name=t('🌟 Skills em destaque'), value=_format_perks(perks, 8), inline=False)
    category_lines = [f"• {item['name']}: `{item['count']}`" for item in inventory["categories"][:5]] or [t('📦 Sem categorias registradas.')]
    embed.add_field(name=t('📦 Categorias do inventário'), value="\n".join(category_lines)[:1024], inline=False)
    embed.set_footer(text=t('Relatório completo anexado em .txt'))
    return embed


def build_status_complete_report(snapshot: dict) -> str:
    if snapshot.get("provider") == "ttf":
        from .stats.reports import report
        return report(snapshot)
    row = snapshot.get("playerRow") or {}
    all_perks = get_perk_entries(snapshot.get("perksRow"), True)
    alive = _truthy(get_field(row, ["isalive"]))
    faction = snapshot.get("faction") or {}
    summary = snapshot["inventorySummary"]
    lines: list[str] = []

    def section(title: str) -> None:
        if lines: lines.append("")
        lines.extend((title, "-" * len(title)))

    lines.extend((''.join((format(t('STATUS COMPLETO - '), ''), format(snapshot['actualNick'], ''))), ''.join((format(t('Gerado em: '), ''), format(datetime.now(timezone.utc).isoformat(), '')))))
    section(t('Arquivos lidos'))
    for label, key in (("player", "playerFile"), ("perks", "perksFile"), (t('inventário'), "inventoryFile")): lines.append(f"{label}: {snapshot['files'][key]}")
    section(t('Identificação'))
    for label, keys, fallback in (("Nick", ["username"], snapshot["actualNick"]), (t('Personagem'), ["charname"], snapshot["actualNick"]), ("SteamID", ["steamid"], "N/A"), (t('Profissão'), ["profession"], "N/A"), ("Traits", ["traits"], "N/A")):
        lines.append(f"{label}: {get_field(row, keys, fallback)}")
    section(t('Sobrevivência'))
    for label, keys, fallback in ((t('Status'), [], t('Vivo') if alive else t('Morto')), (t('Zumbis mortos'), ["zombiekills"], "0"), (t('Horas vividas'), ["hourssurvived"], "0"), (t('Tempo sobrevivido'), ["timesurvived"], "N/A"), (t('Vida'), ["health"], "N/A"), (t('Lesão'), ["hasinjury"], "N/A"), (t('Infecção'), ["infectionlevel"], "0"), (t('Falsa infecção'), ["fakeinfectionlevel"], "N/A"), ("Molhado", ["wetness"], "N/A"), ("Peso", ["weight"], "N/A"), (t('Arma favorita'), ["favoriteweapon"], t('Nenhuma')), (t('Golpes com arma favorita'), ["favoriteweaponhit"], "0")):
        value = fallback if not keys else get_field(row, keys, fallback)
        lines.append(f"{label}: {value}")
    lines.append(''.join((format(t('Posição: '), ''), format(get_field(row, ['x'], '?'), ''), ', ', format(get_field(row, ['y'], '?'), ''), ', ', format(get_field(row, ['z'], '?'), ''))))
    section(t('Facção'))
    lines.extend((''.join((format(t('Nome: '), ''), format(faction.get('name') or get_field(row, ['factionname'], t('Sem facção')), ''))), ''.join(('Tag: ', format(faction.get('tag') or get_field(row, ['factiontag'], t('Sem tag')), ''))), ''.join((format(t('Dono: '), ''), format(faction.get('owner') or t('Não informado'), ''))), ''.join((format(t('Membros: '), ''), format(', '.join(faction.get('players') or []) or t('Nenhum/não informado'), '')))))
    section("Safehouses")
    lines.extend([f"{index}. {_format_safehouse(house)}" for index, house in enumerate(snapshot["safehouses"], 1)] or [t('Nenhuma safehouse vinculada.')])
    section("Skills")
    lines.extend([f"{perk['label']}: {perk['value']:g}" for perk in all_perks] or [t('Nenhuma skill registrada.')])
    section(t('Inventário'))
    lines.extend((''.join((format(t('Linhas brutas: '), ''), format(summary['totalRows'], ''))), ''.join((format(t('Itens únicos: '), ''), format(summary['uniqueItems'], ''))), t('Categorias: ') + (", ".join(f"{item['name']}={item['count']}" for item in summary["categories"]) or t('Nenhuma'))))
    if not summary["items"]: lines.append(t('Nenhum item encontrado.'))
    else:
        lines.append(t('Itens agrupados:'))
        for item in summary["items"]:
            details = (f" | id: {item['itemId']}" if item["itemId"] else "") + (f" | extra: {item['extra']}" if item["extra"] else "")
            lines.append(''.join(('x', format(item['count'], ''), ' ', format(item['name'], ''), format(t(' | categoria: '), ''), format(item['category'], ''), format(details, ''))))
    if snapshot["inventoryRows"]:
        lines.extend(("", t('Linhas brutas do inventário (primeiras 250):')))
        lines.extend(f"{index}. {item['raw']}" for index, item in enumerate(snapshot["inventoryRows"][:250], 1))
        if len(snapshot["inventoryRows"]) > 250: lines.append(''.join(('... ', format(len(snapshot['inventoryRows']) - 250, ''), format(t(' linha(s) omitidas.'), ''))))
    db_data = snapshot["dbData"]
    section(t('DB externa'))
    lines.append(t('Diretórios candidatos: ') + (" | ".join(db_data["roots"]) if db_data["roots"] else t('Nenhum diretório candidato de DB perto do FriendHost.')))
    if not db_data["sqliteMatches"] and not db_data["textMatches"]: lines.append(t('Nenhum registro adicional encontrado para este jogador.'))
    for match in db_data["sqliteMatches"]:
        lines.append(f"SQLite: {match['filePath']}")
        for table in match["tables"]:
            lines.append(''.join((format(t('  Tabela: '), ''), format(table['name'], ''))))
            for values in table["rows"]: lines.append("    " + " | ".join(f"{key}={value}" for key, value in values.items()))
    for match in db_data["textMatches"]:
        lines.append(''.join((format(t('Arquivo texto: '), ''), format(match['filePath'], ''))))
        lines.extend(f"  {line}" for line in match["lines"])
    return "\n".join(lines) + "\n"
