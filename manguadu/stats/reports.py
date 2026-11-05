from datetime import datetime, timezone

import discord

from ..i18n import t
from .ttf import METRICS

LABELS = {"zombiekills": "Zumbis mortos", "hourssurvived": "Horas sobrevividas", "distance": "Distância (m)",
          "vehiclekills": "Abates com veículos", "brokenweapons": "Armas quebradas"}


def display(row, metric):
    value = row.get(metric)
    if value is None:
        return t("Indisponível")
    number = float(value)
    return f"{number:.4g}" if number > 10**15 else f"{number:,.2f}"


def profile(snapshot):
    entry = snapshot.get("entry") or {}
    row = entry.get("row", {})
    embed = discord.Embed(title=("TTF Stats MP · " + str(snapshot["actualNick"]))[:256], color=0x28D17C)
    embed.description = t("Totais históricos por conta; inclui personagens vivos e mortos. Status refere-se ao personagem visto mais recentemente.")
    for metric, label in LABELS.items():
        embed.add_field(name=t(label), value=display(row, metric))
    embed.add_field(name=t("Personagens / Mortes"), value=f"{row.get('characters', '?')} / {row.get('deaths', '?')}")
    embed.add_field(name=t("Personagem mais recente"), value=discord.utils.escape_markdown(row.get("charname", "?"))[:1024])
    embed.add_field(name=t("Status"), value=t("Vivo") if row.get("isalive") == "true" else t("Morto"))
    embed.set_footer(text=t("TTF não exporta skills, inventário, posição ou saúde neste formato."))
    return embed


def report(snapshot):
    entry = snapshot["entry"]
    lines = ["TTF Stats MP · " + snapshot["actualNick"], t("Totais históricos por conta; inclui personagens vivos e mortos. Status refere-se ao personagem visto mais recentemente.")]
    lines += [t(label) + ": " + display(entry["row"], metric) for metric, label in LABELS.items()]
    size = sum(map(len, lines))
    for character in entry["characters"][:1000]:
        if size > 300000:
            lines.append(t("Relatório limitado por tamanho. Consulte personagens específicos no ranking."))
            break
        start = len(lines)
        lines.extend(("", character["id"], character["row"]["charname"], t("Vivo") if character["row"]["isalive"] == "true" else t("Morto")))
        lines += [t(label) + ": " + display(character["row"], metric) for metric, label in LABELS.items()]
        for field in ("weapons", "categories"):
            lines.append(field + ": " + (", ".join(f"{key}={value:g}" for key, value in character[field].items()) or t("Indisponível")))
        size += sum(map(len, lines[start:]))
    if len(entry["characters"]) > 1000:
        lines.append(t("Relatório limitado aos primeiros 1000 personagens."))
    return "\n".join(lines) + "\n"


def ranking_panel(entries, theme):
    rows = [entry["row"] for entry in entries]
    header = discord.Embed(title=(theme["title"] + " · TTF Stats MP")[:256],
                           description=t("Totais históricos por conta; inclui personagens vivos e mortos. Personagens excluídos pelo mod não entram nos totais."), color=0x28D17C)
    header.add_field(name=t("Contas / Personagens"), value=f"{len(rows)} / {sum(int(row['characters']) for row in rows)}")
    header.add_field(name=t("Mortes registradas"), value=str(sum(int(row["deaths"]) for row in rows)))
    diagnostics = getattr(entries, "diagnostics", {})
    if diagnostics.get("invalid") or diagnostics.get("stale") or diagnostics.get("error"):
        header.add_field(name=t("Qualidade dos dados"), value=t("Registros inválidos / cache anterior: ") + f"{diagnostics.get('invalid', 0)} / {diagnostics.get('stale', 0)}" + (" · " + diagnostics["error"] if diagnostics.get("error") else ""), inline=False)
    embeds = [header]
    for metric in METRICS:
        available = [entry for entry in entries if metric in entry["row"]]
        available.sort(key=lambda item: (-float(item["row"][metric]), item["nick"].casefold()))
        description = "\n".join(f"**{i}.** {discord.utils.escape_markdown(item['nick'])[:150]} · `{display(item['row'], metric)}`" for i, item in enumerate(available[:10], 1))
        embeds.append(discord.Embed(title=t(LABELS[metric]), description=description or t("Indisponível"), color=0x28D17C))
    embeds[-1].set_footer(text=t("Ranking atualizado silenciosamente a cada 10 minutos"))
    embeds[-1].timestamp = datetime.now(timezone.utc)
    return embeds
