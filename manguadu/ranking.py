"""Guild ranking preferences and shared, locale-aware integer presentation."""
from __future__ import annotations

import math

from .i18n import language, t

RANKINGS = {
    "kills": "Zumbis mortos",
    "hours": "Horas sobrevividas",
    "skills10": "Habilidades no nível 10",
    "deaths": "Mortes registradas",
    "factions": "Facções",
    "faction_highlight": "Facção em destaque",
    "distance": "Distância (m)",
    "vehiclekills": "Abates com veículos",
    "brokenweapons": "Armas quebradas",
}
DEFAULT_RANKINGS = ["kills", "hours"]


def normalize_rankings(value):
    if not isinstance(value, list):
        return list(DEFAULT_RANKINGS)
    return [key for key in RANKINGS if key in value]


def format_stat(value):
    """Round for display only. Original precision is retained in calculations."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return t("Indisponível")
    if not math.isfinite(number):
        return t("Indisponível")
    if abs(number) > 10**15:
        return f"{number:.4g}"
    result = f"{number:,.0f}"
    return result.replace(",", ".") if language() == "pt-BR" else result
