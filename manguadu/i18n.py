"""Offline PT-BR/EN localization. Only developer-owned text is translated."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import discord
from discord import app_commands

from .tenant_scope import current_scope

CATALOG = json.loads((Path(__file__).parent / "locales" / "en.json").read_text(encoding="utf-8"))
COMMAND_NAMES = {
    "escopo": "scope",
    "condig": "condig", "config_bot": "config_bot", "config_api": "config_api", "config_lore": "config_lore",
    "config_kick_automatico": "config_auto_kick", "puxar_lore": "download_lore",
    "painel": "panel", "jogadores": "players", "vincular": "link", "desvincular": "unlink",
    "salvar": "save", "recursos": "resources", "automacao": "automation",
    "criar": "create", "listar": "list", "remover": "remove", "pausar": "pause", "retomar": "resume",
    "executar": "run", "backupagora": "backup_now", "servidor": "server", "mapas": "maps",
    "satelite": "satellite", "localizar_veiculo": "find_vehicle", "deletearquivo": "delete_files",
    "atualizar": "update", "listar_bkp_geral": "list_backups", "executar_bkp_geral": "create_backup",
    "restaurar_bkp_geral": "restore_backup", "wipe_teste": "wipe_test", "wipe_chunk_teste": "wipe_chunk_test",
    "jogador": "player", "arquivo": "file", "usuario": "username", "personagem": "character",
    "senha": "password", "tipo": "type", "horario": "time", "dias": "days", "comando": "command",
    "confirmar": "confirm", "alvo": "target", "destino": "destination", "ativar": "enable",
    "horas": "hours", "metrica": "metric", "mensagem": "message", "manter_backups": "keep_backups",
    "cor": "color", "titulo": "title", "descricao": "description", "imagem": "image",
    "idioma": "language", "desligar": "stop", "reiniciar": "restart", "ligar": "start",
    "acao": "action", "membro": "member", "texto": "text", "cor-secundaria": "secondary-color",
    "fundo-ranking": "ranking-background", "logatualizacao": "update_log",
}

def language() -> str:
    scope = current_scope()
    return scope.language if scope is not None else "pt-BR"

def _commands(text: str) -> str:
    def replace(match):
        return "/" + " ".join(COMMAND_NAMES.get(word, word) for word in match.group(1).split(" "))
    return re.sub(r"/([a-z_-]+(?: [a-z_-]+){0,3})", replace, text)

def t(source: str, *, locale: str | None = None) -> str:
    if not (locale or language()).lower().startswith("en"):
        return source
    return _english(source)

@lru_cache(maxsize=2048)
def _english(source: str) -> str:
    return _commands(CATALOG.get(source, source))

def localize_component(item):
    # IDs, values and defaults carry protocol identifiers or user data.
    for key in ("label", "placeholder"):
        value = getattr(item, key, None)
        if isinstance(value, str):
            translated = t(value)
            if key == "label" and isinstance(item, discord.ui.TextInput):
                translated = translated[:45]
            setattr(item, key, translated)
    for option in ([] if getattr(item, "_hts_user_options", False) else getattr(item, "options", [])):
        option.label = t(option.label)
        if option.description:
            option.description = t(option.description)
    return item

class HTSTranslator(app_commands.Translator):
    async def translate(self, string, locale, context):
        if not locale.value.startswith("en"):
            return None
        source = string.message
        if context.location in {app_commands.TranslationContextLocation.command_name,
                                app_commands.TranslationContextLocation.group_name,
                                app_commands.TranslationContextLocation.parameter_name}:
            return COMMAND_NAMES.get(source)
        translated = t(source, locale="en")
        return translated if translated != source else None

def mark_command_strings(tree):
    """Choice names are opt-in in discord.py; command/parameter strings are automatic."""
    for command in tree.walk_commands():
        for parameter in getattr(command, "parameters", []):
            parameter.choices[:] = [app_commands.Choice(name=app_commands.locale_str(choice.name), value=choice.value) for choice in parameter.choices]
