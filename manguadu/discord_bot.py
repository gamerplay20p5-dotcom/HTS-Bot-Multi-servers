"""Interface Discord para configuracao, tickets e whitelist do PZ."""

from __future__ import annotations

from .i18n import t, HTSTranslator, mark_command_strings

from .tenant_scope import getenv, current_scope, activate, use_scope, scoped_guilds, GuildScope, tenant_path

import asyncio
import io
import logging
import os
import re
import sqlite3
from pathlib import Path
from collections import defaultdict
from typing import Any

import aiohttp
import discord
from cryptography.fernet import InvalidToken
from discord import app_commands

from .config_store import ConfigStore
from .guild_runtime import GuildRuntime, normalize_binding
from .guild_ui import GuildCommandTree, GuildView, GuildModal, ServerConfigView, LanguageView
from .work_queue import StaggeredQueue, TransientJobError
from .automation_scheduler import AutomationScheduler, get_timezone
from .anticheat_alerts import AntiCheatMonitor
from .bot_updater import BotUpdater
from .credentials import decrypt_password, encrypt_password
from .gemini import GEMINI_MODEL, GeminiError, evaluate_lore_with_gemini, test_gemini_key
from .lore_drafts import save_draft, load_draft, cleanup_drafts
from .lore import MAX_LORE_CHARS, LoreResult, evaluate_lore
from .pterodactyl import PterodactylClient, extract_ptero_error
from .pz_data import PzData
from .pz_map_renderer import create_map_png
from .panel_player_links import get_player_link, load_player_links, remove_player_link, set_player_link
from .panel_reports import build_ranking_panel, build_server_panel, build_server_status_embed
from .ranking import RANKINGS, format_stat
from .panel_theme import get_draft_theme, save_draft_theme, store_panel_media, update_draft_theme
from .player_data import collect_player_snapshot
from .player_reports import build_status_complete_embed, build_status_complete_report, build_status_embed
from .pz_wipe import build_wipe_plan, execute_wipe_plan, format_wipe_plan
from .rcon import adduser_command, generate_password, send_rcon_command
from .server_integrations import describe_server_error, fetch_server_resources, manage_server
from .server_registry import ServerRegistry, describe_missing_rcon
from .utils import clean_text, get_field, to_number
from .watchers import FriendHostWatcher


logger = logging.getLogger(__name__)

CHANNEL_LABELS = {
    "admin": 'Administração',
    "chat": 'Chat PZ',
    "evolution": 'Evolução',
    "stats": 'Status',
    "ranking": 'Ranking',
    "anticheat": "Anticheat",
    "ticket_panel": 'Painel de tickets',
    "lore": 'Lore para leitura',
    "wl_audit": 'Auditoria da WL',
    "wl_decisions": 'Decisões da WL',
}
MAX_LORE_UPLOAD_BYTES = 40 * 1024 * 1024


def _channel_display(channel_id: int | None) -> str:
    return f"<#{channel_id}>" if channel_id else t('Não definido')


def _quote_pz(value: str) -> str:
    return '"' + clean_text(value).replace('"', "").replace("\r", "").replace("\n", "") + '"'


def _parse_players_snapshot(value: str) -> list[str] | None:
    """Return a complete PZ players list, or None if RCON output is ambiguous."""
    lines = [clean_text(line) for line in clean_text(value).splitlines() if clean_text(line)]
    header_index = next((index for index, line in enumerate(lines) if re.match(r"players\s+connected\b", line, re.I)), None)
    if header_index is None:
        return None

    header = lines[header_index]
    count_match = re.search(r"\((\d+)\)", header)
    count = int(count_match.group(1)) if count_match else None
    suffix = header.split(":", 1)[1].strip() if ":" in header else ""
    if count is None and suffix.isdigit():
        count, suffix = int(suffix), ""
    elif count is None and (leading_count := re.match(r"^(\d+)\s*[,;]\s*", suffix)):
        count = int(leading_count.group(1))
        suffix = suffix[leading_count.end():].strip()

    names: list[str] = []
    candidates = ([suffix] if suffix and suffix != str(count) else []) + lines[header_index + 1:]
    for candidate in candidates:
        for piece in re.split(r"[,;]+", candidate):
            name = clean_text(re.sub(r"^\d+[.)]\s+", "", re.sub(r"^[-*•]\s*", "", piece)))
            if name and name.casefold() not in {"players connected", "none", "no players"}:
                names.append(name)

    # Duplicate output, count-only output for a non-empty server, and unknown
    # formats must never be treated as a trustworthy snapshot for enforcement.
    names = list(dict.fromkeys(names))
    if count is not None:
        if count == 0 and not names:
            return []
        if count != len(names):
            return None
    return names or None


def _parse_players_output(value: str) -> list[str]:
    return _parse_players_snapshot(value) or []


async def _read_lore_attachment(attachment: discord.Attachment) -> tuple[str | None, str | None]:
    if not attachment.filename.casefold().endswith(".txt"):
        return None, t('Envie um arquivo de texto com extensao .txt.')
    if attachment.size > MAX_LORE_UPLOAD_BYTES:
        return None, t('O arquivo excede o limite de 40 MiB do bot.')
    try:
        raw = await attachment.read()
        if len(raw) > MAX_LORE_UPLOAD_BYTES:
            return None, t('O arquivo excede o limite de 40 MiB do bot.')
        content = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        return None, t('O arquivo precisa estar codificado em UTF-8.')
    except discord.HTTPException:
        return None, t('Nao foi possivel baixar o arquivo anexado. Tente envia-lo novamente.')
    if len(content) > MAX_LORE_CHARS:
        return None, ''.join((format(t('A lore excede o limite de '), ''), format(MAX_LORE_CHARS, ''.join((',',))), format(t(' caracteres.'), '')))
    if not content:
        return None, t('O arquivo esta vazio.')
    return content, None


def _make_lore_files(lore: str, username: str, max_file_bytes: int) -> list[discord.File]:
    """Build UTF-8 text attachments, splitting only when Discord's file limit requires it."""
    payload = lore.encode("utf-8")
    if not payload:
        return []
    limit = max(1, int(max_file_bytes))
    chunks: list[bytes] = []
    offset = 0
    while offset < len(payload):
        end = min(offset + limit, len(payload))
        if end < len(payload):
            while end > offset and payload[end] & 0xC0 == 0x80:
                end -= 1
        if end <= offset:
            raise ValueError(t('O limite de anexo do Discord e pequeno demais para exportar esta lore.'))
        chunks.append(payload[offset:end])
        offset = end
        if len(chunks) > 10:
            raise ValueError(t('A lore excede o limite de anexos deste servidor Discord; aumente o limite de upload ou reduza o arquivo.'))

    safe_username = re.sub(r"[^A-Za-z0-9._-]+", "_", clean_text(username)).strip("._-")[:80] or "jogador"
    total = len(chunks)
    files: list[discord.File] = []
    for index, chunk in enumerate(chunks, start=1):
        suffix = f"-parte-{index:02d}-de-{total:02d}" if total > 1 else ""
        files.append(discord.File(io.BytesIO(chunk), filename=f"lore-{safe_username}{suffix}.txt"))
    return files


def _configured_text_channel(guild: discord.Guild, channel_id: int | None) -> discord.TextChannel | None:
    channel = guild.get_channel(channel_id) if channel_id else None
    return channel if isinstance(channel, discord.TextChannel) else None


def _configured_private_channel(guild: discord.Guild, channel_id: int | None) -> discord.TextChannel | None:
    channel = _configured_text_channel(guild, channel_id)
    if channel and not channel.permissions_for(guild.default_role).view_channel:
        return channel
    return None


def _is_admin(member: discord.abc.User, config: dict[str, Any]) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    role_id = config.get("admin_role_id")
    return bool(role_id and any(role.id == role_id for role in member.roles))


async def _admin_guard(interaction: discord.Interaction, store: ConfigStore) -> bool:
    if interaction.guild is None or not _is_admin(interaction.user, store.get_settings(interaction.guild.id)):
        await interaction.response.send_message(t('Apenas administradores podem usar este painel.'), ephemeral=True)
        return False
    return True


def _home_embed(config: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(title=t('⚙️ Configuração do HTS Bot Multi servers'), description=t('Escolha uma seção abaixo. Alterações são salvas para este servidor Discord.'), color=0x28D17C)
    embed.add_field(name=t('Tickets'), value=''.join((format(t('Painel: '), ''), format(_channel_display(config['channels']['ticket_panel']), ''), format(t('\nCategoria: '), ''), format(_channel_display(config['ticket_category_id']), ''))), inline=False)
    embed.add_field(name="Whitelist", value=''.join((format(t('Ativa') if config['wl']['enabled'] else t('Pausada'), ''), format(t(' • Lore '), ''), format(t('obrigatória') if config['wl']['require_lore'] else t('opcional'), ''), ' • PZ: `', format(config['wl']['server_id'], ''), '`')), inline=False)
    embed.set_footer(text=t('Vincule o servidor em /config. Chaves de infraestrutura ficam no dedicado; Gemini em /config_api.'))
    return embed


class AdminView(GuildView):
    require_binding = False
    def __init__(self, bot: "ManguaduBot", guild_id: int):
        super().__init__(timeout=600)
        self.bot = bot
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await super().interaction_check(interaction) and await _admin_guard(interaction, self.bot.store)


class ConfigHomeView(AdminView):
    @discord.ui.button(label='Rankings publicados', style=discord.ButtonStyle.secondary, row=1)
    async def rankings(self, interaction, _button):
        view = RankingsConfigView(self.bot, self.guild_id)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="Idioma / Language", style=discord.ButtonStyle.secondary, row=1)
    async def choose_language(self, interaction, _button):
        fresh = LanguageView(self.bot, self.guild_id)
        await interaction.response.edit_message(embed=fresh.embed(), view=fresh)

    @discord.ui.button(label='Servidor PZ /config', style=discord.ButtonStyle.secondary)
    async def server_binding(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        view = ServerConfigView(self.bot, self.guild_id)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label='Canais', emoji="📺", style=discord.ButtonStyle.primary)
    async def channels(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=discord.Embed(title=t('📺 Canais do bot'), description=t('Escolha a função e depois selecione o canal. Os IDs ficam salvos pelo Discord.'), color=0x28D17C), view=ChannelsView(self.bot, self.guild_id))

    @discord.ui.button(label='Tickets', emoji="🎫", style=discord.ButtonStyle.primary)
    async def tickets(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=tickets_embed(self.bot.store.get_settings(self.guild_id)), view=TicketsConfigView(self.bot, self.guild_id))

    @discord.ui.button(label='Config WL', emoji="🧟", style=discord.ButtonStyle.success)
    async def whitelist(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=wl_embed(self.bot.store.get_settings(self.guild_id)), view=WhitelistConfigView(self.bot, self.guild_id))

    @discord.ui.button(label='Kick automático por voz', style=discord.ButtonStyle.danger)
    async def kick_automatico(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        view = KickAutoConfigView(self.bot, self.guild_id)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class RankingsConfigView(AdminView):
    def __init__(self, bot, guild_id):
        super().__init__(bot, guild_id)
        selected = bot.store.get_rankings(guild_id)
        self.selector = discord.ui.Select(placeholder=t('Escolha os rankings publicados'), min_values=0, max_values=len(RANKINGS), row=0,
            options=[discord.SelectOption(label=t(label), value=key, default=key in selected) for key, label in RANKINGS.items()])
        self.selector.callback = self.choose
        self.add_item(self.selector)

    def embed(self):
        selected = self.bot.store.get_rankings(self.guild_id)
        description = t('Selecione apenas os rankings que esta comunidade deseja publicar. A escolha vale para o painel automático e o preview.')
        description += "\n\n" + ("\n".join("• " + t(RANKINGS[key]) for key in selected) or t('Nenhum ranking selecionado. Apenas o resumo será publicado.'))
        description += "\n\n" + t('Habilidades no nível 10: quantidade de habilidades no nível máximo por jogador, usando o snapshot atual do FriendHost. O TTF v1 não exporta esses níveis.')
        return discord.Embed(title=t('Rankings publicados'), description=description, color=0x28D17C)

    async def interaction_check(self, interaction):
        return interaction.guild_id == self.guild_id and await super().interaction_check(interaction)

    async def choose(self, interaction):
        if interaction.guild_id != self.guild_id or not await _admin_guard(interaction, self.bot.store):
            return
        values = interaction.data.get("values", [])
        if not isinstance(values, list) or any(not isinstance(key, str) or key not in RANKINGS for key in values):
            await interaction.response.send_message(t('Seleção de rankings inválida.'), ephemeral=True)
            return
        self.bot.store.set_rankings(self.guild_id, values)
        self.bot.work_queue.trigger(f"{self.guild_id}:panels")
        view = RankingsConfigView(self.bot, self.guild_id)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label='Preview do ranking', row=1)
    async def preview(self, interaction, button):
        await self.bot._panel_preview(interaction, "ranking")

    @discord.ui.button(label='Config / Voltar', row=1)
    async def back(self, interaction, button):
        view = ServerConfigView(self.bot, self.guild_id)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class KickAutoConfigView(AdminView):
    def __init__(self, bot: "ManguaduBot", guild_id: int):
        super().__init__(bot, guild_id)
        channel_select = discord.ui.ChannelSelect(
            placeholder=t('Call de voz obrigatória'),
            channel_types=[discord.ChannelType.voice],
            min_values=1,
            max_values=1,
            row=0,
        )
        channel_select.callback = self.select_voice_channel
        self.add_item(channel_select)

    def embed(self) -> discord.Embed:
        config = self.bot.store.get_settings(self.guild_id)
        kick_config = config["kick_automatico"]
        channel_id = kick_config.get("voice_channel_id")
        embed = discord.Embed(
            title=t('Configuração: Kick automático por voz'),
            description=(
                ''.join((format(t('Fiscalização: **'), ''), format(t('ATIVA') if kick_config['enabled'] else t('pausada'), ''), format(t('**\nCall obrigatória: '), ''), format(_channel_display(channel_id), ''), format(t('\n\nServidor PZ: vínculo exclusivo definido em /config.\n\nA cada 10 segundos, o bot consulta `/players` via RCON e compara os usuários PZ com os vínculos PZ/Discord. Quem ficar fora da call escolhida por 60 segundos recebe kick com o nome da call no motivo.\n\nVincule cada jogador antes de ativar usando `/painel jogadores vincular`. Jogadores sem vínculo também contam como fora da call. Se RCON, a lista de jogadores ou o canal de voz não puderem ser verificados, o bot não aplica kicks naquele ciclo.'), '')))
            ),
            color=0xD84A4A if kick_config["enabled"] else 0x7F8C8D,
        )
        return embed

    async def select_voice_channel(self, interaction: discord.Interaction) -> None:
        config = self.bot.store.get_settings(self.guild_id)["kick_automatico"]
        if config["enabled"]:
            await interaction.response.send_message(t('Pause a fiscalização antes de trocar a call obrigatória.'), ephemeral=True)
            return
        channel = interaction.data["values"][0]
        channel_id = int(channel)
        self.bot.store.set_value(self.guild_id, "kick_automatico", "voice_channel_id", channel_id)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label='Ativar / pausar fiscalização', style=discord.ButtonStyle.danger, row=1)
    async def toggle(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        config = self.bot.store.get_settings(self.guild_id)["kick_automatico"]
        if config["enabled"]:
            self.bot.store.set_value(self.guild_id, "kick_automatico", "enabled", False)
            await interaction.response.edit_message(embed=self.embed(), view=self)
            return

        await interaction.response.defer()
        ready, message = await self.bot._validate_kick_auto_setup(interaction.guild)
        if not ready:
            await interaction.followup.send(message, ephemeral=True)
            return
        self.bot.store.set_value(self.guild_id, "kick_automatico", "enabled", True)
        await interaction.edit_original_response(embed=self.embed(), view=self)
        if message:
            await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label='Voltar', style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=_home_embed(self.bot.store.get_settings(self.guild_id)), view=ConfigHomeView(self.bot, self.guild_id))


class ChannelsView(AdminView):
    def __init__(self, bot: "ManguaduBot", guild_id: int):
        super().__init__(bot, guild_id)
        self.selected_key = "admin"
        self.function_select = discord.ui.Select(
            placeholder=t('Qual função usará o canal?'),
            options=[discord.SelectOption(label=label, value=key) for key, label in CHANNEL_LABELS.items()],
        )
        self.function_select.callback = self.choose_function
        self.add_item(self.function_select)
        self.channel_select = discord.ui.ChannelSelect(placeholder=t('Selecione um canal de texto'), channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
        self.channel_select.callback = self.choose_channel
        self.add_item(self.channel_select)

    async def choose_function(self, interaction: discord.Interaction) -> None:
        self.selected_key = self.function_select.values[0]
        await interaction.response.edit_message(embed=discord.Embed(title=t('📺 Canais do bot'), description=''.join((format(t('Função selecionada: **'), ''), format(t(CHANNEL_LABELS[self.selected_key]), ''), format(t('**. Agora escolha o canal.'), ''))), color=0x28D17C), view=self)

    async def choose_channel(self, interaction: discord.Interaction) -> None:
        channel = self.channel_select.values[0]
        self.bot.store.set_value(self.guild_id, "channels", self.selected_key, channel.id)
        await interaction.response.edit_message(embed=discord.Embed(title=t('✅ Canal salvo'), description=''.join(('**', format(t(CHANNEL_LABELS[self.selected_key]), ''), '** → <#', format(channel.id, ''), format(t('>\nEscolha outra função ou volte ao início.'), ''))), color=0x28D17C), view=self)

    @discord.ui.button(label='Voltar', style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=_home_embed(self.bot.store.get_settings(self.guild_id)), view=ConfigHomeView(self.bot, self.guild_id))


def tickets_embed(config: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(title=t('🎫 Configuração de tickets'), description=t('Selecione o canal do painel, a categoria dos tickets e um cargo de equipe. Depois publique a mensagem de abertura.'), color=0x28D17C)
    embed.add_field(name="Painel", value=_channel_display(config["channels"]["ticket_panel"]))
    embed.add_field(name="Categoria", value=_channel_display(config["ticket_category_id"]))
    embed.add_field(name=t('Cargo da equipe'), value=f"<@&{config['admin_role_id']}>" if config["admin_role_id"] else t('Administradores do Discord'))
    return embed


class TicketsConfigView(AdminView):
    def __init__(self, bot: "ManguaduBot", guild_id: int):
        super().__init__(bot, guild_id)
        panel = discord.ui.ChannelSelect(placeholder=t('Canal do painel de tickets'), channel_types=[discord.ChannelType.text])
        panel.callback = self.select_panel
        self.add_item(panel)
        category = discord.ui.ChannelSelect(placeholder=t('Categoria dos tickets'), channel_types=[discord.ChannelType.category])
        category.callback = self.select_category
        self.add_item(category)
        role = discord.ui.RoleSelect(placeholder=t('Cargo da equipe que verá os tickets'))
        role.callback = self.select_role
        self.add_item(role)

    async def select_panel(self, interaction: discord.Interaction) -> None:
        channel = interaction.data["values"][0]
        self.bot.store.set_value(self.guild_id, "channels", "ticket_panel", int(channel))
        await interaction.response.edit_message(embed=tickets_embed(self.bot.store.get_settings(self.guild_id)), view=self)

    async def select_category(self, interaction: discord.Interaction) -> None:
        self.bot.store.set_top_value(self.guild_id, "ticket_category_id", int(interaction.data["values"][0]))
        await interaction.response.edit_message(embed=tickets_embed(self.bot.store.get_settings(self.guild_id)), view=self)

    async def select_role(self, interaction: discord.Interaction) -> None:
        self.bot.store.set_top_value(self.guild_id, "admin_role_id", int(interaction.data["values"][0]))
        await interaction.response.edit_message(embed=tickets_embed(self.bot.store.get_settings(self.guild_id)), view=self)

    @discord.ui.button(label='Publicar painel', emoji="📣", style=discord.ButtonStyle.success, row=3)
    async def publish(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            channel = await self.bot.publish_ticket_panel(interaction.guild)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(''.join((format(t('Não foi possível publicar: '), ''), format(exc, ''))), ephemeral=True)
            return
        await interaction.followup.send(''.join((format(t('Painel publicado em '), ''), format(channel.mention, ''), '.')), ephemeral=True)

    @discord.ui.button(label='Voltar', style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=_home_embed(self.bot.store.get_settings(self.guild_id)), view=ConfigHomeView(self.bot, self.guild_id))


def wl_embed(config: dict[str, Any]) -> discord.Embed:
    wl = config["wl"]
    embed = discord.Embed(title=t('🧟 Config WL'), description=t('Com uma chave em /config_api, o Gemini avalia coerência, cronologia e contradições. Sem chave, a triagem lexical local é usada. A equipe pode revisar cada pedido.'), color=0x28D17C)
    embed.add_field(name=t('WL'), value=t('Ativa') if wl["enabled"] else t('Pausada'))
    embed.add_field(name=t('Lore'), value=t('Obrigatória') if wl["require_lore"] else t('Opcional'))
    embed.add_field(name=t('Falha na triagem'), value=t('Revisão da equipe') if wl["review_failed_lore"] else t('Recusa automática'))
    embed.add_field(name=t('Decisão'), value=t('Automática com revisão de falhas') if wl["auto_approve"] else t('Revisão manual de todos os pedidos'))
    embed.add_field(name=t('Canal da lore'), value=_channel_display(config["channels"]["lore"]))
    embed.add_field(name="Auditoria", value=_channel_display(config["channels"]["wl_audit"]))
    embed.add_field(name=t('Decisões'), value=_channel_display(config["channels"]["wl_decisions"]))
    embed.add_field(name=t('Servidor PZ'), value=t('Vínculo exclusivo definido em /config'))
    embed.add_field(name=t('Lore base'), value=''.join((format(len(wl['lore_reference']), ''), format(t(' caracteres configurados'), ''))), inline=False)
    return embed


class LoreReferenceModal(GuildModal, title='Lore base da whitelist'):
    def __init__(self, bot: "ManguaduBot", guild_id: int):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
        current = bot.store.get_settings(guild_id)["wl"]["lore_reference"]
        self.reference = discord.ui.TextInput(label=t('Lore oficial do servidor (até 4.000 caracteres)'), style=discord.TextStyle.paragraph, default=current[:4000], min_length=80, max_length=4000)
        self.add_item(self.reference)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _admin_guard(interaction, self.bot.store):
            return
        self.bot.store.set_value(self.guild_id, "wl", "lore_reference", str(self.reference.value).strip())
        await interaction.response.send_message(t('Lore base salva. Novos pedidos serão comparados com este texto.'), ephemeral=True)


class WhitelistConfigView(AdminView):
    def __init__(self, bot: "ManguaduBot", guild_id: int):
        super().__init__(bot, guild_id)
        scope = bot.scope_for(guild_id)
        servers = [scope.server] if scope.server else []
        if not servers:
            return
        server_select = discord.ui.Select(placeholder=t('Servidor PZ vinculado a este Discord'), options=[discord.SelectOption(label=s["label"][:100], value=s["id"][:100]) for s in servers])
        server_select.callback = self.select_server
        server_select._hts_user_options = True
        self.add_item(server_select)

    async def select_server(self, interaction: discord.Interaction) -> None:
        if self.bot.store.get_settings(self.guild_id)["kick_automatico"]["enabled"]:
            await interaction.response.send_message(t('Pause a fiscalização por voz antes de trocar o servidor PZ selecionado.'), ephemeral=True)
            return
        server_id = str(interaction.data["values"][0])
        if self.bot.registry.get_server(server_id) is None:
            await interaction.response.send_message(t('Servidor não encontrado.'), ephemeral=True)
            return
        self.bot.store.set_value(self.guild_id, "wl", "server_id", server_id)
        await interaction.response.edit_message(embed=wl_embed(self.bot.store.get_settings(self.guild_id)), view=self)

    async def _toggle(self, interaction: discord.Interaction, key: str) -> None:
        current = self.bot.store.get_settings(self.guild_id)["wl"][key]
        if key == "review_failed_lore" and current:
            config = self.bot.store.get_settings(self.guild_id)
            if not config["channels"]["wl_decisions"]:
                await interaction.response.send_message(t('Configure primeiro o canal de decisões da WL.'), ephemeral=True)
                return
        self.bot.store.set_value(self.guild_id, "wl", key, not current)
        await interaction.response.edit_message(embed=wl_embed(self.bot.store.get_settings(self.guild_id)), view=self)

    @discord.ui.button(label='Ativar/pausar WL', style=discord.ButtonStyle.primary, row=3)
    async def toggle_enabled(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._toggle(interaction, "enabled")

    @discord.ui.button(label='Exigir lore', style=discord.ButtonStyle.primary, row=3)
    async def toggle_lore(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._toggle(interaction, "require_lore")

    @discord.ui.button(label='Revisar falhas', style=discord.ButtonStyle.primary, row=3)
    async def toggle_review(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._toggle(interaction, "review_failed_lore")

    @discord.ui.button(label='Aprovação automática', style=discord.ButtonStyle.success, row=4)
    async def toggle_auto(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        config = self.bot.store.get_settings(self.guild_id)
        if not config["wl"]["auto_approve"] and (
            not _configured_private_channel(interaction.guild, config["channels"]["wl_audit"])
            or not _configured_text_channel(interaction.guild, config["channels"]["wl_decisions"])
        ):
            await interaction.response.send_message(t('Configure primeiro os canais de auditoria e decisões da WL.'), ephemeral=True)
            return
        await self._toggle(interaction, "auto_approve")

    @discord.ui.button(label='Definir lore base', style=discord.ButtonStyle.success, row=4)
    async def set_lore(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if len(self.bot.store.get_settings(self.guild_id)["wl"]["lore_reference"]) > 4000:
            await interaction.response.send_message(t('A lore atual é maior que o limite do modal do Discord. Use /config_lore e anexe um arquivo .txt para substituir a lore base.'), ephemeral=True)
            return
        await interaction.response.send_modal(LoreReferenceModal(self.bot, self.guild_id))

    @discord.ui.button(label='Importar lore .txt', style=discord.ButtonStyle.primary, row=4)
    async def import_lore_file(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            ''.join((format(t('Para configurar lore longa, use `/config_lore` em um canal privado da equipe e anexe um arquivo .txt. Limite do bot: '), ''), format(MAX_LORE_CHARS, ''.join((',',))), format(t(' caracteres. O campo de texto do modal do Discord aceita no maximo 4.000.'), ''))),
            ephemeral=True,
        )

    @discord.ui.button(label='Canais da WL', style=discord.ButtonStyle.secondary, row=4)
    async def wl_channels(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=discord.Embed(title=t('📺 Canais da WL'), description=t('Defina onde fica a lore, onde chegam os resumos e onde chegam as aprovações e reprovações.'), color=0x28D17C), view=WhitelistChannelsView(self.bot, self.guild_id))

    @discord.ui.button(label='Voltar', style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=_home_embed(self.bot.store.get_settings(self.guild_id)), view=ConfigHomeView(self.bot, self.guild_id))


class WhitelistChannelsView(AdminView):
    def __init__(self, bot: "ManguaduBot", guild_id: int):
        super().__init__(bot, guild_id)
        for key, placeholder in (
            ("lore", t('Canal da lore para o jogador ler')),
            ("wl_audit", t('Canal privado de auditoria da WL')),
            ("wl_decisions", t('Canal de aprovações e reprovações')),
        ):
            select = discord.ui.ChannelSelect(placeholder=placeholder, channel_types=[discord.ChannelType.text])
            async def save(interaction: discord.Interaction, setting: str = key) -> None:
                channel_id = int(interaction.data["values"][0])
                if setting == "wl_audit" and not _configured_private_channel(interaction.guild, channel_id):
                    await interaction.response.send_message(t('Use um canal privado para auditoria: @everyone não deve poder vê-lo.'), ephemeral=True)
                    return
                self.bot.store.set_value(self.guild_id, "channels", setting, channel_id)
                await interaction.response.edit_message(embed=wl_embed(self.bot.store.get_settings(self.guild_id)), view=self)
            select.callback = save
            self.add_item(select)

    @discord.ui.button(label='Voltar à WL', style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=wl_embed(self.bot.store.get_settings(self.guild_id)), view=WhitelistConfigView(self.bot, self.guild_id))


class PublicTicketView(GuildView):
    def __init__(self, bot: "ManguaduBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Abrir ticket', emoji="🎫", style=discord.ButtonStyle.success, custom_id="manguadu:ticket:open:v1")
    async def open_ticket(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.bot.open_ticket(interaction)


class TicketActionView(GuildView):
    def __init__(self, bot: "ManguaduBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Criar personagem / WL', emoji="🧟", style=discord.ButtonStyle.primary, custom_id="manguadu:ticket:wl:v1")
    async def apply_whitelist(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        ticket = self.bot.store.get_ticket(interaction.channel_id)
        if not ticket or ticket["status"] != "open" or ticket["user_id"] != interaction.user.id:
            await interaction.response.send_message(t('Somente quem abriu este ticket pode enviar a WL.'), ephemeral=True)
            return
        config = self.bot.store.get_settings(ticket["guild_id"])
        if not config["wl"]["enabled"]:
            await interaction.response.send_message(t('A criação de WL está pausada.'), ephemeral=True)
            return
        if config["wl"]["require_lore"] and not config["wl"]["lore_reference"].strip():
            await interaction.response.send_message(t('A equipe ainda não definiu a lore base. Tente novamente depois.'), ephemeral=True)
            return
        await interaction.response.send_modal(WhitelistModal(self.bot, ticket, config["wl"]["require_lore"]))

    @discord.ui.button(label='Enviar lore longa (.txt)', style=discord.ButtonStyle.secondary, custom_id="manguadu:ticket:wl-file:v1")
    async def apply_whitelist_file(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        ticket = self.bot.store.get_ticket(interaction.channel_id)
        if not ticket or ticket["status"] != "open" or ticket["user_id"] != interaction.user.id:
            await interaction.response.send_message(t('Somente quem abriu este ticket pode enviar a WL.'), ephemeral=True)
            return
        await interaction.response.send_message(t('Anexe o arquivo .txt e execute `/wl lore` selecionando esse arquivo. Depois, preencha usuario, personagem e senha no formulario privado. Limite do bot: 9.999.999 caracteres.'), ephemeral=True)

    @discord.ui.button(label='Fechar ticket', emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="manguadu:ticket:close:v1")
    async def close_ticket(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.bot.close_ticket(interaction)


class WhitelistModal(GuildModal, title='Criação de personagem PZ'):
    require_binding = True
    def __init__(self, bot: "ManguaduBot", ticket: dict[str, Any], require_lore: bool, lore_file_text: str | Path | None = None):
        super().__init__(timeout=300)
        self.bot = bot
        self.ticket = ticket
        self.require_lore = require_lore
        self.lore_file_text = lore_file_text
        self.username = discord.ui.TextInput(label=t('Usuário para entrar no PZ'), min_length=3, max_length=32, placeholder=t('Ex.: Malaio_01'))
        self.character = discord.ui.TextInput(label=t('Nome do personagem'), min_length=2, max_length=80)
        self.password = discord.ui.TextInput(label=t('Senha PZ (opcional; vazio = gerada)'), required=False, min_length=8, max_length=64, placeholder=t('8 a 64 caracteres sem espaços ou aspas'))
        self.add_item(self.username)
        self.add_item(self.character)
        self.add_item(self.password)
        if lore_file_text is None:
            self.lore = discord.ui.TextInput(label=t('História do personagem (até 4.000)'), style=discord.TextStyle.paragraph, required=require_lore, max_length=4000, placeholder=t('Para uma história maior, use Enviar lore longa (.txt).'))
            self.add_item(self.lore)

    async def on_timeout(self) -> None:
        if isinstance(self.lore_file_text, Path):
            self.lore_file_text.unlink(missing_ok=True)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        lore = self.lore_file_text if self.lore_file_text is not None else str(self.lore.value or "").strip()
        self.lore_file_text = None
        await self.bot.submit_whitelist(interaction, self.ticket, str(self.username.value).strip(), str(self.character.value).strip(), lore, str(self.password.value or ""))


class LoreUploadDraftView(GuildView):
    def __init__(self, bot: "ManguaduBot", ticket: dict[str, Any], lore_text: Path):
        super().__init__(timeout=180)
        self.bot = bot
        self.ticket = ticket
        self.lore_text = lore_text

    async def on_timeout(self) -> None:
        if isinstance(self.lore_text, Path):
            self.lore_text.unlink(missing_ok=True)

    @discord.ui.button(label='Preencher dados da WL', style=discord.ButtonStyle.primary)
    async def continue_form(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        current = self.bot.store.get_ticket(interaction.channel_id)
        if not current or current["status"] != "open" or current["user_id"] != interaction.user.id:
            await interaction.response.send_message(t('Este ticket nao esta mais aberto para voce.'), ephemeral=True)
            return
        config = self.bot.store.get_settings(current["guild_id"])
        if not config["wl"]["enabled"]:
            await interaction.response.send_message(t('A criacao de WL esta pausada.'), ephemeral=True)
            return
        if config["wl"]["require_lore"] and not config["wl"]["lore_reference"].strip():
            await interaction.response.send_message(t('A equipe ainda nao definiu a lore base.'), ephemeral=True)
            return
        lore_text, self.lore_text = self.lore_text, ""
        self.stop()
        await interaction.response.send_modal(WhitelistModal(self.bot, current, config["wl"]["require_lore"], lore_text))


class GeminiKeyModal(GuildModal, title='Configurar chave Gemini'):
    def __init__(self, bot: "ManguaduBot", guild_id: int):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
        self.api_key = discord.ui.TextInput(
            label=t('Chave da API do Google AI Studio'),
            min_length=20,
            max_length=256,
            required=True,
            placeholder=t('A chave nao sera exibida pelo bot depois de salva'),
        )
        self.add_item(self.api_key)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _admin_guard(interaction, self.bot.store):
            return
        try:
            self.bot.store.set_gemini_api_key(self.guild_id, str(self.api_key.value))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            t('Chave Gemini salva criptografada neste bot. Ela nao foi enviada a nenhum canal; use Testar conexao para validar.'),
            ephemeral=True,
        )


class GeminiConfigView(AdminView):
    def __init__(self, bot: "ManguaduBot", guild_id: int):
        super().__init__(bot, guild_id)
        self.add_item(discord.ui.Button(
            label=t('Criar chave no Google AI Studio'),
            style=discord.ButtonStyle.link,
            url="https://aistudio.google.com/apikey",
        ))

    def embed(self) -> discord.Embed:
        configured = self.bot.store.has_gemini_api_key(self.guild_id)
        embed = discord.Embed(
            title=t('Configuracao Gemini'),
            description=(
                ''.join((format(t('Modelo: `'), ''), format(GEMINI_MODEL, ''), format(t('`\nChave: '), ''), format('configurada' if configured else t('nao configurada'), ''), format(t('\n\nA chave fica criptografada no armazenamento local do bot. A analise envia somente trechos da lore base e da historia; nao envia senha PZ, ID Discord ou usuario PZ. A camada gratuita do Gemini pode usar prompts para melhorar os produtos Google. Configure a IA somente se isso for aceitavel para a comunidade.'), '')))
            ),
            color=0x4285F4,
        )
        return embed

    @discord.ui.button(label='Configurar / trocar chave', style=discord.ButtonStyle.primary)
    async def configure(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(GeminiKeyModal(self.bot, self.guild_id))

    @discord.ui.button(label='Testar conexao', style=discord.ButtonStyle.success)
    async def test_connection(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            api_key = self.bot.store.get_gemini_api_key(self.guild_id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if not api_key:
            await interaction.response.send_message(t('Configure a chave Gemini primeiro.'), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await test_gemini_key(api_key)
        except GeminiError as exc:
            await interaction.followup.send(''.join((format(t('Teste Gemini falhou: '), ''), format(exc, ''))), ephemeral=True)
            return
        await interaction.followup.send(''.join((format(t('Gemini respondeu pelo modelo `'), ''), format(GEMINI_MODEL, ''), '`.')), ephemeral=True)

    @discord.ui.button(label='Remover chave', style=discord.ButtonStyle.danger)
    async def remove_key(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.bot.store.clear_gemini_api_key(self.guild_id)
        await interaction.response.edit_message(embed=self.embed(), view=self)


class ReviewView(GuildView):
    def __init__(self, bot: "ManguaduBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Aprovar WL', style=discord.ButtonStyle.success, custom_id="manguadu:wl:approve:v1")
    async def approve(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await _admin_guard(interaction, self.bot.store):
            return
        config = self.bot.store.get_settings(interaction.guild.id)
        if not _configured_text_channel(interaction.guild, config["channels"]["wl_decisions"]):
            await interaction.response.send_message(t('Configure o canal de decisões da WL antes de aprovar.'), ephemeral=True)
            return
        previous = self.bot.store.get_request(interaction.channel_id)
        await interaction.response.defer(ephemeral=True)
        message = await self.bot.issue_whitelist(interaction.channel, interaction.channel_id)
        request = self.bot.store.get_request(interaction.channel_id)
        if request and previous and previous["status"] in ("pending", "review", "failed"):
            await self.bot.post_audit(interaction.guild, request, t('Aprovada pela equipe') if request["status"] == "approved" else t('Falha na aprovação manual'), message)
            if request["status"] == "approved":
                await self.bot.post_decision(interaction.guild, request, True, t('A equipe analisou a história do personagem e aprovou a whitelist.'))
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label='Reprovar WL', style=discord.ButtonStyle.danger, custom_id="manguadu:wl:reject:v1")
    async def reject(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await _admin_guard(interaction, self.bot.store):
            return
        config = self.bot.store.get_settings(interaction.guild.id)
        if not _configured_text_channel(interaction.guild, config["channels"]["wl_decisions"]):
            await interaction.response.send_message(t('Configure o canal de decisões da WL antes de reprovar.'), ephemeral=True)
            return
        request = self.bot.store.get_request(interaction.channel_id)
        if not request or request["status"] not in ("review", "failed"):
            await interaction.response.send_message(t('Este pedido já foi concluído ou não existe.'), ephemeral=True)
            return
        await interaction.response.send_modal(RejectReasonModal(self.bot, interaction.channel_id))


class RejectReasonModal(GuildModal, title='Motivo da reprovação'):
    def __init__(self, bot: "ManguaduBot", channel_id: int):
        super().__init__()
        self.bot = bot
        self.channel_id = channel_id
        self.reason = discord.ui.TextInput(label=t('Explique ao jogador por que a WL foi recusada'), style=discord.TextStyle.paragraph, min_length=10, max_length=500)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _admin_guard(interaction, self.bot.store):
            return
        request = self.bot.store.get_request(self.channel_id)
        if not request or request["status"] not in ("review", "failed"):
            await interaction.response.send_message(t('Este pedido já foi concluído.'), ephemeral=True)
            return
        reason = str(self.reason.value).strip()
        self.bot.store.set_request_status(self.channel_id, "rejected")
        self.bot.store.clear_request_password(self.channel_id)
        config = self.bot.store.get_settings(interaction.guild.id)
        lore_id = config["channels"]["lore"]
        lore_link = ''.join((format(t(' Leia a lore em <#'), ''), format(lore_id, ''), format(t('> e envie outro pedido.'), ''))) if lore_id else t(' Consulte a lore oficial e envie outro pedido.')
        await interaction.response.send_message(t('WL reprovada e motivo enviado.'), ephemeral=True)
        await interaction.channel.send(''.join(('<@', format(request['user_id'], ''), format(t('>, sua WL foi reprovada. Motivo: '), ''), format(reason, ''), '.', format(lore_link, ''))), allowed_mentions=discord.AllowedMentions(users=True))
        updated = self.bot.store.get_request(self.channel_id)
        await self.bot.post_decision(interaction.guild, updated, False, reason)
        await self.bot.post_audit(interaction.guild, updated, t('Reprovada pela equipe'), reason)


class ManguaduBot(discord.Client):
    def scope_for(self, guild_id: int | None) -> GuildScope | None:
        if guild_id is None:
            return None
        runtime = self._runtimes.get(guild_id)
        scope = runtime.scope if runtime else GuildScope(guild_id)
        scope.language = self.store.get_language(guild_id)
        return scope

    def activate_guild(self, guild_id: int | None) -> GuildScope | None:
        scope = self.scope_for(guild_id)
        activate(scope)
        return scope

    async def install_binding(self, guild_id: int, binding: dict) -> None:
        previous = self.store.get_binding(guild_id)
        if previous and previous["uuid"] != binding["uuid"]:
            raise ValueError(t('Este Discord ja tem outro volume vinculado. O operador deve migrar o vinculo e os pedidos pendentes antes de trocar de servidor.'))
        if previous:
            binding = {**previous, **binding}
        old_runtime = self._runtimes.get(guild_id)
        runtime = await asyncio.to_thread(GuildRuntime, self, guild_id, binding)
        if old_runtime and old_runtime.scope.environment.get("LOGS_PATH") == runtime.scope.environment.get("LOGS_PATH"):
            # Configuration changes must not skip queued log lines by reseeding
            # a watcher at EOF. Keep its cursors and lock for the same volume.
            runtime.watcher = old_runtime.watcher
            runtime.watcher_lock = old_runtime.watcher_lock
        self.store.save_binding(guild_id, binding)
        self.work_queue.remove_prefix(f"{guild_id}:")
        self.log_queue.remove_prefix(f"{guild_id}:")
        self._runtimes[guild_id] = runtime
        self._clear_kick_auto_state(guild_id)
        self._schedule_guild(guild_id)
        self.activate_guild(guild_id)
        self.work_queue.trigger(f"{guild_id}:panels")

    def _runtime_value(self, attribute: str, fallback: str):
        scope = current_scope()
        if scope is not None:
            if scope.runtime is None:
                if attribute == "data":
                    return PzData(None)
                if attribute == "online_since":
                    return {}
                raise RuntimeError(t('Servidor deste Discord ainda nao foi vinculado em /config.'))
            return getattr(scope.runtime, attribute)
        return getattr(self, fallback)

    @property
    def pz_data(self):
        return self._runtime_value("data", "_legacy_data")

    @pz_data.setter
    def pz_data(self, value):
        scope = current_scope()
        if scope and scope.runtime:
            scope.runtime.data = value
        else:
            self._legacy_data = value

    @property
    def automations(self):
        return self._runtime_value("automations", "_legacy_automations")

    @automations.setter
    def automations(self, value):
        self._legacy_automations = value

    @property
    def anticheat(self):
        return self._runtime_value("anticheat", "_legacy_anticheat")

    @anticheat.setter
    def anticheat(self, value):
        self._legacy_anticheat = value

    @property
    def friendhost_watcher(self):
        return self._runtime_value("watcher", "_legacy_watcher")

    @friendhost_watcher.setter
    def friendhost_watcher(self, value):
        self._legacy_watcher = value

    @property
    def _online_since(self):
        return self._runtime_value("online_since", "_legacy_online_since")

    @_online_since.setter
    def _online_since(self, value):
        self._legacy_online_since = value

    def _schedule_guild(self, guild_id: int) -> None:
        for kind, interval, timeout in (("logs", 5, 30), ("panels", 600, 120), ("data", 600, 120), ("anticheat", 30, 45), ("automation", 30, 180), ("kick", 10, 60)):
            async def callback(kind=kind, guild_id=guild_id):
                runtime = self._runtimes.get(guild_id)
                guild = self.get_guild(guild_id)
                if runtime is None or guild is None or not self.is_ready() or guild.unavailable:
                    if kind == "kick":
                        self._clear_kick_auto_state(guild_id)
                    return
                with use_scope(runtime.scope):
                    runtime.scope.language = self.store.get_language(guild_id)
                    if kind == "panels":
                        await self.refresh_panels()
                    elif kind in {"logs", "data"}:
                        watcher = runtime.watcher
                        if kind == "logs" and runtime.watcher_lock.locked():
                            return
                        async with runtime.watcher_lock:
                            if not watcher._initialized:
                                await watcher.initialize()
                            await (watcher._scan_logs() if kind == "logs" else watcher._scan_data())
                            if kind == "data":
                                await asyncio.to_thread(cleanup_drafts)
                    elif kind == "anticheat":
                        await runtime.anticheat.scan(self)
                    elif kind == "automation":
                        await runtime.automations.run_due(self._notify_updater)
                    elif kind == "kick":
                        if not await self._scan_kick_auto_guild(guild):
                            raise TransientJobError(t('fiscalizacao de voz indisponivel neste servidor'))
            target_queue = self.log_queue if kind == "logs" else self.work_queue
            target_queue.add(f"{guild_id}:{kind}", interval, callback, timeout=timeout)

    async def _run_queues(self) -> None:
        tasks = [asyncio.create_task(self.work_queue.run()), asyncio.create_task(self.log_queue.run())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def __init__(self, store: ConfigStore | None = None):
        activate(None)
        super().__init__(intents=discord.Intents(guilds=True, voice_states=True))
        self.store = store or ConfigStore()
        self._runtimes: dict[int, GuildRuntime] = {}
        self._ptero_session = None
        self._ptero_limiter = asyncio.Semaphore(3)
        self._ptero_retry_at = 0.0
        self._rcon_limiter = asyncio.Semaphore(6)
        self._large_io_slots = asyncio.Semaphore(2)
        self.work_queue = StaggeredQueue(workers=3, capacity=256)
        self.log_queue = StaggeredQueue(workers=2, capacity=128)
        self.registry = ServerRegistry()
        self.pz_data = PzData(getenv("CSV_BASE_PATH"))
        self.automations = AutomationScheduler(server_provider=self.registry.get_default_server)
        self.anticheat = AntiCheatMonitor(registry=self.registry)
        self.friendhost_watcher = FriendHostWatcher(self)
        self.updater = BotUpdater()
        self.tree = GuildCommandTree(self)
        self._ticket_locks: dict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._wl_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._registered = False
        self._ptero_paths_discovered = False
        self._auto_update_task: asyncio.Task | None = None
        self._panel_refresh_task: asyncio.Task | None = None
        self._online_since: dict[str, float] = {}
        self._automation_task: asyncio.Task | None = None
        self._kick_auto_task: asyncio.Task | None = None
        self._kick_auto_missing_since: dict[tuple[int, str], float] = {}
        self._kick_auto_last_attempt: dict[tuple[int, str], float] = {}
        self._kick_auto_last_warning: dict[int, float] = {}
        self._anticheat_task: asyncio.Task | None = None
        self._watcher_task: asyncio.Task | None = None

        @self.tree.command(name="language", description=t('Escolhe português ou inglês para esta comunidade'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def language_settings(interaction: discord.Interaction) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            fresh = LanguageView(self, interaction.guild.id)
            await interaction.response.send_message(embed=fresh.embed(), view=fresh, ephemeral=True)

        @self.tree.command(name="config", description=t('Vincula este Discord ao servidor PZ e configura o bot'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def config_server(interaction: discord.Interaction) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            view = ServerConfigView(self, interaction.guild.id)
            await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

        @self.tree.command(name="condig", description=t('Abre a configuração do servidor PZ deste Discord'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def config_alias(interaction: discord.Interaction) -> None:
            await config_server.callback(interaction)

        @self.tree.command(name="config_bot", description=t('Configura canais, tickets e whitelist do HTS Bot Multi servers'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def config_bot(interaction: discord.Interaction) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            config = self.store.get_settings(interaction.guild.id)
            await interaction.response.send_message(embed=_home_embed(config), view=ConfigHomeView(self, interaction.guild.id), ephemeral=True)

        @self.tree.command(name="config_api", description=t('Configura com privacidade a chave Gemini usada na analise de lore'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def config_api(interaction: discord.Interaction) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            view = GeminiConfigView(self, interaction.guild.id)
            await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

        @self.tree.command(name="config_kick_automatico", description=t('Configura a presença obrigatória na call de voz do RP'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def config_kick_automatico(interaction: discord.Interaction) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            view = KickAutoConfigView(self, interaction.guild.id)
            await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

        @self.tree.command(name="config_lore", description=t('Importa lore base em arquivo .txt (ate 9.999.999 caracteres)'))
        @app_commands.describe(arquivo=t('Arquivo UTF-8 .txt com a lore oficial; use em canal privado da equipe'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def config_lore(interaction: discord.Interaction, arquivo: discord.Attachment) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with self._large_io_slots:
                content, error = await _read_lore_attachment(arquivo)
                if error:
                    await interaction.followup.send(error, ephemeral=True)
                    return
                if len(content) < 80:
                    await interaction.followup.send(t('A lore base precisa ter pelo menos 80 caracteres.'), ephemeral=True)
                    return
                try:
                    await asyncio.to_thread(self.store.set_value, interaction.guild.id, "wl", "lore_reference", content)
                except ValueError as exc:
                    await interaction.followup.send(str(exc), ephemeral=True)
                    return
                compressed_bytes = len(content.encode("utf-8"))
                await interaction.followup.send(
                    ''.join((format(t('Lore base salva: '), ''), format(len(content), ''.join((',',))), format(t(' caracteres. Armazenamento SQLite comprimido (arquivo original: '), ''), format(compressed_bytes, ''.join((',',))), ' bytes).')),
                    ephemeral=True,
                )

        wl_group = app_commands.Group(name="wl", description=t('Envia uma candidatura de whitelist neste ticket'))

        @wl_group.command(name="lore", description=t('Anexa lore longa em .txt e abre o formulario privado da WL'))
        @app_commands.describe(arquivo=t('Arquivo UTF-8 .txt com a historia do personagem'))
        @app_commands.guild_only()
        async def wl_lore_file(interaction: discord.Interaction, arquivo: discord.Attachment) -> None:
            ticket = self.store.get_ticket(interaction.channel_id)
            if not ticket or ticket["status"] != "open" or ticket["user_id"] != interaction.user.id:
                await interaction.response.send_message(t('Use este comando no ticket aberto por voce.'), ephemeral=True)
                return
            config = self.store.get_settings(ticket["guild_id"])
            if not config["wl"]["enabled"]:
                await interaction.response.send_message(t('A criacao de WL esta pausada.'), ephemeral=True)
                return
            if config["wl"]["require_lore"] and not config["wl"]["lore_reference"].strip():
                await interaction.response.send_message(t('A equipe ainda nao definiu a lore base.'), ephemeral=True)
                return
            existing = self.store.get_request(ticket["channel_id"])
            if existing and existing["status"] in ("review", "processing", "approved"):
                await interaction.response.send_message(t('Ja existe um pedido de WL em andamento neste ticket.'), ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with self._large_io_slots:
                content, error = await _read_lore_attachment(arquivo)
                if error:
                    await interaction.followup.send(error, ephemeral=True)
                    return
                if len(content) < 120:
                    await interaction.followup.send(t('A historia do personagem precisa ter pelo menos 120 caracteres.'), ephemeral=True)
                    return
                await interaction.followup.send(
                    ''.join((format(t('Arquivo recebido ('), ''), format(len(content), ''.join((',',))), format(t(' caracteres). Clique para preencher seus dados no formulario privado.'), ''))),
                    view=LoreUploadDraftView(self, ticket, await asyncio.to_thread(save_draft, content)),
                    ephemeral=True,
                )

        @wl_group.command(name="puxar_lore", description=t('Baixa a lore mais recente enviada por um jogador PZ'))
        @app_commands.describe(jogador=t('Nome exato do usuário PZ informado na WL'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def wl_pull_lore(interaction: discord.Interaction, jogador: str) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with self._large_io_slots:
                request = await asyncio.to_thread(
                    self.store.get_latest_request_by_username,
                    interaction.guild.id,
                    jogador,
                )
                if not request:
                    await interaction.followup.send(''.join((format(t('Não encontrei uma WL para o usuário PZ `'), ''), format(discord.utils.escape_markdown(jogador), ''), format(t('` neste servidor.'), ''))), ephemeral=True)
                    return
                lore = request.get("lore", "")
                if not lore:
                    await interaction.followup.send(t('Esse pedido não contém uma lore para baixar.'), ephemeral=True)
                    return
                try:
                    files = await asyncio.to_thread(_make_lore_files, lore, request["username"], interaction.guild.filesize_limit)
                except ValueError as exc:
                    await interaction.followup.send(str(exc), ephemeral=True)
                    return
                status = clean_text(request.get("status", t('desconhecido')))
                message = (
                    ''.join((format(t('Lore mais recente de `'), ''), format(discord.utils.escape_markdown(request['username']), ''), format(t('` (pedido: **'), ''), format(discord.utils.escape_markdown(status), ''), '**).'))
                )
                try:
                    for index, file in enumerate(files, start=1):
                        content = message if index == 1 else ''.join((format(t('Continuação da lore: parte '), ''), format(index, ''), format(t(' de '), ''), format(len(files), ''), '.'))
                        await interaction.followup.send(
                            content=content,
                            file=file,
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                except discord.HTTPException:
                    for file in files:
                        file.close()
                    logger.exception("Falha ao entregar a lore de %s pela interacao WL", request["username"])
                    await interaction.followup.send(
                        t('O Discord recusou o anexo. Confira o limite de upload deste servidor e tente novamente.'),
                        ephemeral=True,
                    )

        self.tree.add_command(wl_group)

        bot_commands = app_commands.Group(name="bot", description=t('Atualizacao e manutencao do bot'))

        @bot_commands.command(name="status", description=t('Mostra o estado da atualizacao do bot'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def bot_status(interaction: discord.Interaction) -> None:
            if not await self._updater_admin_guard(interaction):
                return
            status = self.updater.status()
            lines = [
                ''.join((format(t('Projeto: '), ''), format(status['projectRoot'], ''))),
                ''.join((format(t('Atualizador Python: '), ''), format('encontrado' if status['scriptExists'] else 'ausente', ''))),
                f"PM2 app: {status['pm2AppName']}",
                f"Runtime PM2: {status['pm2Runtime']}",
                f"Timeout: {status['timeoutSeconds']}s",
                ''.join((format(t('Auto-update: '), ''), format('ativo' if status['autoUpdateEnabled'] else t('desativado'), ''))),
                ''.join((format(t('Intervalo: '), ''), format(round(status['autoUpdateIntervalMs'] / 1000), ''), 's')),
                ''.join((format(t('GitHub privado: '), ''), format(t('token configurado') if status['gitHubAuthConfigured'] else t('sem token (adequado para repositorio publico)'), ''))),
                f"Log: {status['logFile']}",
            ]
            await interaction.response.send_message("```\n" + "\n".join(lines)[:1850] + "\n```", ephemeral=True)

        @bot_commands.command(name="logatualizacao", description=t('Mostra o log recente de atualizacao'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def bot_update_log(interaction: discord.Interaction) -> None:
            if not await self._updater_admin_guard(interaction):
                return
            await interaction.response.send_message("```\n" + self.updater.read_last_log(1800) + "\n```", ephemeral=True)

        @bot_commands.command(name="atualizar", description=t('Baixa a versao Python e reinicia o processo PM2'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def bot_update(interaction: discord.Interaction) -> None:
            if not await self._updater_admin_guard(interaction):
                return
            if not self.updater.python_runtime_configured:
                await interaction.response.send_message(t('O processo PM2 precisa executar `python -m manguadu --bot` e ter `BOT_PM2_RUNTIME=python` no .env antes de atualizar.'), ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await self.updater.update(skip_restart=True)
            if not result["ok"]:
                details = "\n".join(part for part in (result.get("error"), result.get("output")) if part)
                await interaction.followup.send(t('Atualizacao falhou.\n```\n') + details[:1800] + "\n```", ephemeral=True)
                return
            await interaction.followup.send(t('Atualizacao Python concluida. Reiniciando o bot em alguns segundos.\n```\n') + result["output"][:1500] + "\n```", ephemeral=True)
            asyncio.create_task(self._restart_pm2())

        self.tree.add_command(bot_commands)

        panel_group = app_commands.Group(name="painel", description=t('Controla e personaliza os paineis do servidor'))
        panel_config_group = app_commands.Group(name="config", description=t('Personaliza o tema dos paineis'))
        panel_players_group = app_commands.Group(name="jogadores", description=t('Vincula nomes PZ a membros Discord'))

        @panel_config_group.command(name="banner", description=t('Define o banner do painel de status'))
        @app_commands.describe(arquivo=t('Imagem, GIF ou vídeo'), url=t('URL de imagem, GIF ou YouTube'))
        @app_commands.default_permissions(administrator=True)
        async def panel_banner(interaction: discord.Interaction, arquivo: discord.Attachment | None = None, url: str | None = None) -> None:
            await self._panel_media_config(interaction, "banner", arquivo, url)

        @panel_config_group.command(name="thumbnail", description=t('Define o logo lateral dos paineis'))
        @app_commands.describe(arquivo=t('Imagem, GIF ou vídeo'), url=t('URL de imagem, GIF ou YouTube'))
        @app_commands.default_permissions(administrator=True)
        async def panel_thumbnail(interaction: discord.Interaction, arquivo: discord.Attachment | None = None, url: str | None = None) -> None:
            await self._panel_media_config(interaction, "thumbnail", arquivo, url)

        @panel_config_group.command(name="cor", description=t('Define a cor principal dos paineis'))
        @app_commands.describe(hex=t('Cor hexadecimal, exemplo #28D17C'))
        @app_commands.default_permissions(administrator=True)
        async def panel_primary_color(interaction: discord.Interaction, hex: str) -> None:
            await self._panel_text_config(interaction, "primaryColor", hex, is_color=True)

        @panel_config_group.command(name="cor-secundaria", description=t('Define a cor complementar dos paineis'))
        @app_commands.describe(hex=t('Cor hexadecimal, exemplo #F0A93B'))
        @app_commands.default_permissions(administrator=True)
        async def panel_secondary_color(interaction: discord.Interaction, hex: str) -> None:
            await self._panel_text_config(interaction, "secondaryColor", hex, is_color=True)

        @panel_config_group.command(name="titulo", description=t('Altera o titulo principal'))
        @app_commands.describe(texto=t('Titulo do servidor'))
        @app_commands.default_permissions(administrator=True)
        async def panel_title(interaction: discord.Interaction, texto: app_commands.Range[str, 1, 100]) -> None:
            await self._panel_text_config(interaction, "title", texto)

        @panel_config_group.command(name="descricao", description=t('Altera a descricao principal'))
        @app_commands.describe(texto=t('Descricao da comunidade'))
        @app_commands.default_permissions(administrator=True)
        async def panel_description(interaction: discord.Interaction, texto: app_commands.Range[str, 1, 500]) -> None:
            await self._panel_text_config(interaction, "description", texto)

        @panel_config_group.command(name="fundo-ranking", description=t('Define a imagem principal do ranking'))
        @app_commands.describe(arquivo=t('Imagem, GIF ou vídeo'), url=t('URL de imagem, GIF ou YouTube; use limpar para remover'))
        @app_commands.default_permissions(administrator=True)
        async def panel_ranking_background(interaction: discord.Interaction, arquivo: discord.Attachment | None = None, url: str | None = None) -> None:
            await self._panel_media_config(interaction, "rankingBackground", arquivo, url)

        @panel_players_group.command(name="vincular", description=t('Vincula um nick PZ a um membro Discord'))
        @app_commands.describe(jogador=t('Nick do jogador'), membro=t('Membro do Discord'))
        @app_commands.default_permissions(administrator=True)
        async def panel_link_player(interaction: discord.Interaction, jogador: str, membro: discord.Member) -> None:
            await self._panel_link_player(interaction, jogador, membro.id)

        @panel_players_group.command(name="desvincular", description=t('Remove o vinculo Discord de um jogador'))
        @app_commands.describe(jogador=t('Nick do jogador'))
        @app_commands.default_permissions(administrator=True)
        async def panel_unlink_player(interaction: discord.Interaction, jogador: str) -> None:
            await self._panel_unlink_player(interaction, jogador)

        panel_group.add_command(panel_config_group)
        panel_group.add_command(panel_players_group)

        @panel_group.command(name="restart", description=t('Reinicia o servidor'))
        @app_commands.default_permissions(administrator=True)
        async def panel_restart(interaction: discord.Interaction) -> None:
            await self._panel_power(interaction, "restart")

        @panel_group.command(name="stop", description=t('Para o servidor'))
        @app_commands.default_permissions(administrator=True)
        async def panel_stop(interaction: discord.Interaction) -> None:
            await self._panel_power(interaction, "stop")

        @panel_group.command(name="recursos", description=t('Mostra CPU, RAM, disco e estado'))
        @app_commands.default_permissions(administrator=True)
        async def panel_resources(interaction: discord.Interaction) -> None:
            await self._panel_resources(interaction)

        @panel_group.command(name="preview", description=t('Mostra o rascunho antes de salvar'))
        @app_commands.choices(tipo=[app_commands.Choice(name="Servidor", value="status"), app_commands.Choice(name="Ranking", value="ranking")])
        @app_commands.default_permissions(administrator=True)
        async def panel_preview(interaction: discord.Interaction, tipo: app_commands.Choice[str] | None = None) -> None:
            await self._panel_preview(interaction, tipo.value if tipo else "status")

        @panel_group.command(name="salvar", description=t('Publica o tema em rascunho'))
        @app_commands.default_permissions(administrator=True)
        async def panel_save(interaction: discord.Interaction) -> None:
            await self._panel_save(interaction)

        self.tree.add_command(panel_group)

        automation_group = app_commands.Group(name="automacao", description=t('Cria e gerencia rotinas do servidor PZ'))

        @automation_group.command(name="criar", description=t('Cria uma automação diária ou em dias específicos'))
        @app_commands.describe(tipo=t('O que o bot deve executar'), horario=t('Horário no formato HH:MM'), dias=t('todos, úteis, fds ou números: 1=domingo ... 7=sábado'), manter_backups=t('Backups automáticos para manter'), comando=t('Obrigatório se tipo = comando'))
        @app_commands.choices(tipo=[
            app_commands.Choice(name="Ligar servidor", value="start"), app_commands.Choice(name="Desligar servidor", value="stop"),
            app_commands.Choice(name="Reiniciar servidor", value="restart"), app_commands.Choice(name="Criar backup", value="backup"),
            app_commands.Choice(name="Salvar mundo", value="save"), app_commands.Choice(name="Comando do console", value="command"),
        ])
        @app_commands.default_permissions(administrator=True)
        async def create_automation(interaction: discord.Interaction, tipo: app_commands.Choice[str], horario: str, dias: str | None = None, manter_backups: app_commands.Range[int, 1, 50] | None = None, comando: str | None = None) -> None:
            if not await _admin_guard(interaction, self.store): return
            result = self.automations.create(tipo.value, horario, dias or "", comando or "", manter_backups)
            if not result["ok"]:
                await interaction.response.send_message(result["error"], ephemeral=True)
                return
            await interaction.response.send_message(t('Automação criada.\n') + self.automations.format_line(result["automation"]), ephemeral=True)

        @automation_group.command(name="listar", description=t('Lista as automações criadas'))
        @app_commands.default_permissions(administrator=True)
        async def list_automations(interaction: discord.Interaction) -> None:
            if not await _admin_guard(interaction, self.store): return
            runtime = self.automations.runtime_status()
            header = ''.join(('Timezone: ', format(get_timezone(), ''), format(t('\nAgora: '), ''), format(runtime['dateKey'], ''), ' ', format(runtime['timeKey'], ''), ' (', format(runtime['weekdayLabel'], ''), format(t(')\nJanela: '), ''), format(runtime['graceMinutes'], ''), format(t(' minuto(s)\nArquivo: '), ''), format(runtime['storeFile'], '')))
            await interaction.response.send_message((header + "\n" + self.automations.format_list())[:1900], ephemeral=True)

        @automation_group.command(name="remover", description=t('Remove uma automação'))
        @app_commands.describe(id=t('ID da automação'))
        @app_commands.default_permissions(administrator=True)
        async def remove_automation(interaction: discord.Interaction, id: str) -> None:
            if not await _admin_guard(interaction, self.store): return
            await interaction.response.send_message(t('Automação removida.') if self.automations.remove(id) else t('Automação não encontrada.'), ephemeral=True)

        @automation_group.command(name="pausar", description=t('Pausa uma automação sem apagar'))
        @app_commands.describe(id=t('ID da automação'))
        @app_commands.default_permissions(administrator=True)
        async def pause_automation(interaction: discord.Interaction, id: str) -> None:
            await self._set_automation(interaction, id, False)

        @automation_group.command(name="retomar", description=t('Reativa uma automação pausada'))
        @app_commands.describe(id=t('ID da automação'))
        @app_commands.default_permissions(administrator=True)
        async def resume_automation(interaction: discord.Interaction, id: str) -> None:
            await self._set_automation(interaction, id, True)

        @automation_group.command(name="executar", description=t('Executa uma automação agora'))
        @app_commands.describe(id=t('ID da automação'))
        @app_commands.default_permissions(administrator=True)
        async def execute_automation(interaction: discord.Interaction, id: str) -> None:
            if not await _admin_guard(interaction, self.store): return
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await self.automations.run_now(id)
            await interaction.followup.send((t('Automação executada.\n') if result.get("ok") else t('Falha na automação.\n')) + (result.get("message") or result.get("error", t('Sem detalhes.'))), ephemeral=True)

        @automation_group.command(name="backupagora", description=t('Cria um backup agora e limpa backups automáticos antigos'))
        @app_commands.describe(manter_backups=t('Backups automáticos para manter'))
        @app_commands.default_permissions(administrator=True)
        async def backup_now(interaction: discord.Interaction, manter_backups: app_commands.Range[int, 1, 50] | None = None) -> None:
            if not await _admin_guard(interaction, self.store): return
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await self.automations.backup_now(manter_backups)
            await interaction.followup.send(result["message"], ephemeral=True)

        self.tree.add_command(automation_group)

        @self.tree.command(name="online", description=t('Lista jogadores online registrados pelo FriendHost'))
        @app_commands.guild_only()
        async def online(interaction: discord.Interaction) -> None:
            names = await asyncio.to_thread(self.pz_data.online_players)
            description = "\n".join(f"{index}. {discord.utils.escape_markdown(name)}" for index, name in enumerate(names, 1)) or t('Nenhum jogador online.')
            embed = discord.Embed(title=''.join((format(t('Jogadores online: '), ''), format(len(names), ''))), description=description[:4000], color=0x2ECC71 if names else 0xE74C3C, timestamp=discord.utils.utcnow())
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="info", description=t('Exibe informacoes de um jogador'))
        @app_commands.describe(jogador=t('Nick do jogador'))
        @app_commands.guild_only()
        async def player_info(interaction: discord.Interaction, jogador: str) -> None:
            await interaction.response.defer(thinking=True)
            entry = await asyncio.to_thread(self.pz_data.find_player, jogador)
            if not entry:
                await interaction.followup.send(t('Jogador não encontrado nos dados do servidor.'), ephemeral=True)
                return
            row = entry["row"]
            if entry.get("provider") == "ttf":
                from .stats.reports import profile
                await interaction.followup.send(embed=profile({"entry": entry, "actualNick": entry["nick"]}))
                return
            alive_value = get_field(row, ["isalive"]).casefold()
            alive = alive_value in {"1", "true", "yes", "sim", "alive"}
            title = get_field(row, ["charname"], entry["nick"])
            color = 0x2ECC71 if alive else 0xE74C3C
            embed = discord.Embed(title=''.join((format(t('🧾 Perfil de '), ''), format(title[:240], ''))), color=color, timestamp=discord.utils.utcnow())
            embed.description = "\n".join((
                ''.join((format(t('🎮 **Nick:** '), ''), format(discord.utils.escape_markdown(entry['nick']), ''))),
                ''.join((format('💚' if alive else '💀', ''), format(t(' **Status:** '), ''), format(t('Vivo') if alive else t('Morto'), ''))),
                ''.join((format(t('🛡️ **Facção:** '), ''), format(get_field(row, ['factionname'], t('Lobo solitário'))[:200], ''))),
                ''.join((format(t('🏠 **Safehouse:** '), ''), format(get_field(row, ['safehousetitle'], t('Nenhuma vinculada'))[:200], ''))),
            ))
            embed.add_field(name=t('Profissão'), value=get_field(row, ["profession"], "N/A")[:1024], inline=True)
            embed.add_field(name=t('Zumbis mortos'), value=str(int(to_number(get_field(row, ["zombiekills"]))),), inline=True)
            embed.add_field(name=t('Horas vividas'), value=f"{to_number(get_field(row, ['hourssurvived'])):.1f}", inline=True)
            embed.add_field(name=t('Vida'), value=get_field(row, ["health"], "N/A")[:1024], inline=True)
            embed.add_field(name="SteamID", value=get_field(row, ["steamid"], "N/A")[:1024], inline=True)
            embed.add_field(name=t('Posição'), value=f"{get_field(row, ['x'], '?')}, {get_field(row, ['y'], '?')}, {get_field(row, ['z'], '?')}"[:1024], inline=True)
            embed.add_field(name=t('Arma favorita'), value=get_field(row, ["favoriteweapon"], t('Nenhuma'))[:1024], inline=False)
            await interaction.followup.send(embed=embed)

        @self.tree.command(name="skills", description=t('Mostra habilidades de um jogador'))
        @app_commands.describe(jogador=t('Nick do jogador'))
        @app_commands.guild_only()
        async def player_skills(interaction: discord.Interaction, jogador: str) -> None:
            await interaction.response.defer(thinking=True)
            data = await asyncio.to_thread(self.pz_data.player_skills, jogador)
            if not data:
                await interaction.followup.send(t('Jogador não encontrado.'), ephemeral=True)
                return
            lines = [f"{item['emoji']} **{item['label']}:** `{item['value']:g}`" for item in data["skills"]]
            embed = discord.Embed(title=''.join((format(t('Skills de '), ''), format(data['nick'], ''))), description=("\n".join(lines) or t('Nenhuma skill exportada.'))[:4000], color=0x3498DB, timestamp=discord.utils.utcnow())
            await interaction.followup.send(embed=embed)

        @self.tree.command(name="traits", description=t('Mostra profissao e traits de um jogador'))
        @app_commands.describe(jogador=t('Nick do jogador'))
        @app_commands.guild_only()
        async def player_traits(interaction: discord.Interaction, jogador: str) -> None:
            await interaction.response.defer(thinking=True)
            data = await asyncio.to_thread(self.pz_data.player_traits, jogador)
            if not data:
                await interaction.followup.send(t('Jogador não encontrado.'), ephemeral=True)
                return
            traits = data["positive"] + data["negative"] + data["traits"]
            embed = discord.Embed(title=''.join((format(t('Profissão e traits de '), ''), format(data['nick'], ''))), color=0x9B59B6, timestamp=discord.utils.utcnow())
            embed.add_field(name=t('Profissão'), value=data["profession"][:1024], inline=False)
            embed.add_field(name="Traits", value="\n".join(f"- {item}" for item in traits)[:1024] if traits else t('Não exportados pelo coletor atual.'), inline=False)
            await interaction.followup.send(embed=embed)

        @self.tree.command(name="rank", description=t('Mostra a posicao de um jogador no ranking'))
        @app_commands.describe(jogador=t('Nick do jogador'), tipo=t('Métrica do ranking'), escopo=t('Escopo do ranking: conta ou personagem TTF'))
        @app_commands.choices(tipo=[app_commands.Choice(name="Zumbis mortos", value="kills"), app_commands.Choice(name="Horas sobrevividas", value="hours"),
                                  app_commands.Choice(name="Distância (m)", value="distance"), app_commands.Choice(name="Abates com veículos", value="vehiclekills"),
                                  app_commands.Choice(name="Armas quebradas", value="brokenweapons"), app_commands.Choice(name="Habilidades no nível 10", value="skills10")],
                             escopo=[app_commands.Choice(name="Conta (histórico acumulado)", value="account"), app_commands.Choice(name="Personagem (TTF)", value="character")])
        @app_commands.guild_only()
        async def player_rank(interaction: discord.Interaction, jogador: str, tipo: app_commands.Choice[str] | None = None, escopo: app_commands.Choice[str] | None = None) -> None:
            metric = tipo.value if tipo else "kills"
            await interaction.response.defer(thinking=True)
            rank = await asyncio.to_thread(self.pz_data.player_rank, jogador, metric, escopo.value if escopo else "account")
            if not rank:
                await interaction.followup.send(t('Jogador/personagem não encontrado ou métrica indisponível nesta fonte.'), ephemeral=True)
                return
            value = to_number(get_field(rank["entry"]["row"], [rank["metric"]]))
            unit = {"hours": " h", "kills": t(' zumbis'), "distance": " m", "skills10": t(' habilidades no nível 10')}.get(metric, "")
            amount = format_stat(value) + unit
            await interaction.followup.send(''.join(('**', discord.utils.escape_markdown(rank['entry']['nick'])[:250], t('** está em **#'), str(rank['position']), t(' de '), str(rank['total']), t('** com **'), amount, '**.')))

        @self.tree.command(name="localizar_veiculo", description=t('Localiza um veículo pelo ID registrado pelo FriendHost'))
        @app_commands.describe(id=t('ID do veículo'))
        @app_commands.guild_only()
        async def locate_vehicle(interaction: discord.Interaction, id: str) -> None:
            vehicle = await asyncio.to_thread(self.pz_data.find_vehicle, id)
            if not vehicle:
                await interaction.response.send_message(t('Veículo não encontrado. Confira os arquivos FriendHost em Servidor.'), ephemeral=True)
                return
            row = vehicle["row"]
            embed = discord.Embed(title=''.join((format(t('Veículo '), ''), format(get_field(row, ['vehicleid', 'id', 'sqlid'], t('sem ID')), ''))), color=0xF39C12, timestamp=discord.utils.utcnow())
            embed.add_field(name="Modelo", value=get_field(row, ["scriptname", "vehicle", "type", "name"], "N/A")[:1024], inline=True)
            embed.add_field(name=t('Posição'), value=f"{get_field(row, ['x'], '?')}, {get_field(row, ['y'], '?')}, {get_field(row, ['z'], '0')}"[:1024], inline=True)
            embed.add_field(name="Dono/chave", value=get_field(row, ["owner", "keyid", "key"], "N/A")[:1024], inline=True)
            embed.set_footer(text=t('Dados exportados pelo servidor PZ'))
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="mapas", description=t('Lista os mapas configurados no servidor'))
        @app_commands.guild_only()
        async def list_maps(interaction: discord.Interaction) -> None:
            maps = self.pz_data.configured_maps()
            if not maps:
                await interaction.response.send_message(t('Nenhum mapa configurado. Crie config/pz-maps.json a partir do exemplo.'), ephemeral=True)
                return
            lines = [f"- `{item['id']}` {item['name']}: ({item['minX']},{item['minY']}) → ({item['maxX']},{item['maxY']})" for item in maps]
            await interaction.response.send_message(t('**Mapas configurados:**\n') + "\n".join(lines)[:1850])

        @self.tree.command(name="gps", description=t('Gera imagem da última localização do jogador'))
        @app_commands.describe(jogador=t('Nick do jogador'))
        @app_commands.guild_only()
        async def gps_player(interaction: discord.Interaction, jogador: str) -> None:
            point = await asyncio.to_thread(self.pz_data.player_point, jogador)
            if not point:
                await interaction.response.send_message(t('Jogador sem coordenadas exportadas.'), ephemeral=True)
                return
            maps = await asyncio.to_thread(self.pz_data.configured_maps)
            safehouses = await asyncio.to_thread(self.pz_data.safehouses)
            radius = max(900, int(to_number(getenv("PZ_GPS_VIEW_RADIUS"), 1500)))
            cell_x, cell_y = int(point["x"] // 300), int(point["y"] // 300)
            chunk_x, chunk_y = int(point["x"] // 10), int(point["y"] // 10)
            map_entry = self.pz_data.map_for_point(point, maps)
            options = {
                "bounds": {"minX": point["x"] - radius, "minY": point["y"] - radius, "maxX": point["x"] + radius, "maxY": point["y"] + radius},
                "maps": maps, "safehouses": safehouses, "points": [point], "focus": point["nick"],
                "focusCell": {"minX": cell_x * 300, "minY": cell_y * 300, "maxX": cell_x * 300 + 299, "maxY": cell_y * 300 + 299},
                "focusChunk": {"minX": chunk_x * 10, "minY": chunk_y * 10, "maxX": chunk_x * 10 + 9, "maxY": chunk_y * 10 + 9},
                "title": f"GPS {point['nick']}",
                "subtitle": f"X {round(point['x'])} Y {round(point['y'])} CELL {cell_x},{cell_y} CHUNK {chunk_x},{chunk_y}",
                "minorGridStep": 100, "majorGridStep": 300, "showPlayerLabels": True,
            }
            png = await asyncio.to_thread(create_map_png, options)
            map_name = map_entry["name"] if map_entry else t('desconhecido')
            description = ''.join((format(t('Última posição de **'), ''), format(point['nick'], ''), '**: ', format(point['x'], ''.join(('g',))), ', ', format(point['y'], ''.join(('g',))), ', ', format(point['z'], ''.join(('g',))), format(t('\nMapa: **'), ''), format(map_name, ''), format(t('** | Célula: '), ''), format(cell_x, ''), ',', format(cell_y, ''), ' | Chunk: ', format(chunk_x, ''), ',', format(chunk_y, '')))
            filename = re.sub(r"[^A-Za-z0-9_.-]", "_", point["nick"])[:80]
            await interaction.response.send_message(content=description, file=discord.File(io.BytesIO(png), filename=f"gps-{filename or 'jogador'}.png"))

        @self.tree.command(name="satelite", description=t('Gera visão PNG do servidor inteiro'))
        @app_commands.guild_only()
        async def satellite_map(interaction: discord.Interaction) -> None:
            points = await asyncio.to_thread(self.pz_data.player_points)
            maps = await asyncio.to_thread(self.pz_data.configured_maps)
            safehouses = await asyncio.to_thread(self.pz_data.safehouses)
            options = {
                "bounds": self.pz_data.world_bounds(points), "maps": maps, "safehouses": safehouses,
                "points": points, "title": t('SATELITE DO SERVIDOR'),
                "subtitle": ''.join((format(len(points), ''), format(t(' jogadores mapeados'), ''))),
                "minorGridStep": 300, "majorGridStep": 900,
            }
            png = await asyncio.to_thread(create_map_png, options)
            await interaction.response.send_message(file=discord.File(io.BytesIO(png), filename="satelite-servidor.png"))

        @self.tree.command(name="wipe_zeds", description=t('Reseta zumbis de um mapa ou célula'))
        @app_commands.describe(alvo=t('todos, nome do mapa ou celula x,y'), confirmar=t('Digite APAGAR'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def wipe_zeds(interaction: discord.Interaction, alvo: str, confirmar: str) -> None:
            await self._handle_wipe(interaction, "zeds", alvo, confirmar, False)

        @self.tree.command(name="wipe", description=t('Wipe global: apaga DB, Logs e Saves'))
        @app_commands.describe(confirmar=t('Digite APAGAR_TUDO'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def wipe_global(interaction: discord.Interaction, confirmar: str) -> None:
            await self._handle_wipe(interaction, "global", "", confirmar, False)

        @self.tree.command(name="wipe_force", description=t('Reseta mapa/célula ignorando safehouses'))
        @app_commands.describe(alvo=t('Nome do mapa ou celula x,y'), confirmar=t('Digite APAGAR'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def wipe_force(interaction: discord.Interaction, alvo: str, confirmar: str) -> None:
            await self._handle_wipe(interaction, "cell", alvo, confirmar, True)

        @self.tree.command(name="wipe_teste", description=t('Simula wipe global sem apagar'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def wipe_global_test(interaction: discord.Interaction) -> None:
            await self._handle_wipe(interaction, "global", "", "", False, dry_run=True)

        @self.tree.command(name="wipe_chunk", description=t('Reseta chunk 10x10 protegendo safehouses'))
        @app_commands.describe(chunk=t('Chunk no formato x,y'), confirmar=t('Digite APAGAR'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def wipe_chunk(interaction: discord.Interaction, chunk: str, confirmar: str) -> None:
            await self._handle_wipe(interaction, "chunk", chunk, confirmar, False)

        @self.tree.command(name="wipe_chunk_force", description=t('Reseta chunk ignorando safehouses'))
        @app_commands.describe(chunk=t('Chunk no formato x,y'), confirmar=t('Digite APAGAR'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def wipe_chunk_force(interaction: discord.Interaction, chunk: str, confirmar: str) -> None:
            await self._handle_wipe(interaction, "chunk", chunk, confirmar, True)

        @self.tree.command(name="wipe_chunk_teste", description=t('Simula exclusão de chunk sem apagar'))
        @app_commands.describe(chunk=t('Chunk no formato x,y'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def wipe_chunk_test(interaction: discord.Interaction, chunk: str) -> None:
            await self._handle_wipe(interaction, "chunk", chunk, "", False, dry_run=True)

        @self.tree.command(name="rcon", description=t('Envia um comando ao servidor por RCON ou console Pterodactyl'))
        @app_commands.describe(comando=t('Comando do servidor'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def rcon(interaction: discord.Interaction, comando: str) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            server = self.registry.get_default_server()
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await send_rcon_command(server["rcon"]["host"], int(server["rcon"]["port"] or 0), server["rcon"]["password"], comando)
            if result.ok:
                output = result.output or t('Comando executado sem retorno.')
                await interaction.followup.send(t('Comando executado via RCON:\n```\n') + output[:1800] + "\n```", ephemeral=True)
                return
            rcon_error = result.error
            if result.stage == "config":
                missing = describe_missing_rcon(server)
                if missing:
                    rcon_error = ''.join((format(t('Configuracao do RCON incompleta: '), ''), format(', '.join(missing), ''), format(t('. Confira o .env e config/servers.json.'), '')))
            async with PterodactylClient(server) as client:
                fallback = await client.command(comando)
            if fallback["ok"]:
                await interaction.followup.send(t('Comando enviado pelo console do Pterodactyl; RCON falhou: ') + rcon_error[:900], ephemeral=True)
            else:
                await interaction.followup.send(''.join((format(t('Falha ao executar.\nRCON: '), ''), format(rcon_error[:700], ''), '\nPterodactyl: ', format(extract_ptero_error(fallback)[:700], ''))), ephemeral=True)

        @self.tree.command(name="servidor", description=t('Gerencia energia e status do servidor PZ'))
        @app_commands.describe(acao=t('Ação desejada'))
        @app_commands.choices(acao=[
            app_commands.Choice(name="Status", value="status"),
            app_commands.Choice(name="Ligar", value="start"),
            app_commands.Choice(name="Desligar", value="stop"),
            app_commands.Choice(name="Reiniciar", value="restart"),
            app_commands.Choice(name="Agendar desligamento (10 min)", value="schedule_stop"),
        ])
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def server_control(interaction: discord.Interaction, acao: app_commands.Choice[str]) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            server = self.registry.get_default_server()
            await interaction.response.defer(ephemeral=True, thinking=True)
            if acao.value == "status":
                overview = await self._build_panel_overview(server)
                await interaction.followup.send(embed=build_server_status_embed(overview, t('Consulta manual do slash command')), ephemeral=True)
                return
            if acao.value == "schedule_stop":
                result = await self._console_command(server, "servermsg " + _quote_pz(t("O SERVIDOR SERA DESLIGADO EM 10 MINUTOS. GUARDEM SEUS ITENS.")))
                asyncio.create_task(self._scheduled_server_stop(server))
                message = t('Aviso enviado; desligamento previsto para 10 minutos.') if result["ok"] else t('Agendamento iniciado, mas o aviso do jogo falhou: ') + result["error"][:500]
                await interaction.followup.send(message, ephemeral=True)
                return
            result = await manage_server(server, acao.value)
            if result["ok"]:
                await interaction.followup.send(''.join((format(t('Comando `'), ''), format(acao.value, ''), format(t('` enviado para **'), ''), format(server['label'], ''), '** via ', format(result.get('transport', 'servidor'), ''), '.')), ephemeral=True)
            else:
                await interaction.followup.send(t('Falha ao controlar o servidor: ') + describe_server_error(result), ephemeral=True)

        @self.tree.command(name="status", description=t('Mostra o perfil resumido de um jogador'))
        @app_commands.describe(nick=t('Nick do jogador; vazio lista jogadores disponíveis'))
        @app_commands.guild_only()
        async def player_status(interaction: discord.Interaction, nick: str | None = None) -> None:
            await interaction.response.defer(thinking=True)
            if not nick:
                names = await asyncio.to_thread(self.pz_data.player_names)
                text = "\n".join(f"{i}. {discord.utils.escape_markdown(name)}" for i, name in enumerate(names[:60], 1)) or t('Nenhum jogador disponível nos arquivos FriendHost.')
                await interaction.followup.send(text[:1900], ephemeral=True)
                return
            snapshot = await asyncio.to_thread(collect_player_snapshot, nick, data=self.pz_data)
            if not snapshot["found"]:
                await interaction.followup.send(t('Jogador ou arquivos FriendHost não encontrados.'), ephemeral=True)
                return
            await interaction.followup.send(embed=build_status_embed(snapshot))

        @self.tree.command(name="statuscomplete", description=t('Gera ficha detalhada do jogador'))
        @app_commands.describe(nick=t('Nick do jogador; vazio lista jogadores disponíveis'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def player_status_complete(interaction: discord.Interaction, nick: str | None = None) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            await interaction.response.defer(thinking=True, ephemeral=True)
            if not nick:
                names = await asyncio.to_thread(self.pz_data.player_names)
                await interaction.followup.send(t('Jogadores disponíveis: ') + (", ".join(names[:60]) or "nenhum"), ephemeral=True)
                return
            snapshot = await asyncio.to_thread(collect_player_snapshot, nick, data=self.pz_data, include_inventory=True, include_db=True)
            if not snapshot["found"]:
                reason = snapshot.get("reason")
                if reason in {"missing_nick", "not_found"}:
                    message = t('Jogador não encontrado nos dados do servidor.')
                else:
                    message = ''.join((format(t('Encontrei **'), ''), format(snapshot.get('actualNick', nick), ''), format(t('**, mas não há arquivos de perfil, skills ou inventário.'), '')))
                await interaction.followup.send(message, ephemeral=True)
                return
            report = build_status_complete_report(snapshot)
            safe_nick = re.sub(r"[^A-Za-z0-9_.-]", "_", snapshot["actualNick"])[:80] or "jogador"
            file = discord.File(io.BytesIO(report.encode("utf-8")), filename=f"statuscomplete-{safe_nick}.txt")
            await interaction.followup.send(embed=build_status_complete_embed(snapshot), file=file, ephemeral=True)

        @self.tree.command(name="logs", description=t('Extrai logs de um jogador em um intervalo'))
        @app_commands.describe(jogador=t('Nick do jogador'), tipo=t('Tipo de log'), horas=t('Intervalo em horas'))
        @app_commands.choices(tipo=[
            app_commands.Choice(name="PVP", value="pvp"), app_commands.Choice(name="Chat", value="chat"),
            app_commands.Choice(name="Usuários", value="user"), app_commands.Choice(name="Administração", value="admin"),
            app_commands.Choice(name="Todos", value="todos"),
        ])
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def player_logs(interaction: discord.Interaction, jogador: str, tipo: app_commands.Choice[str], horas: app_commands.Range[int, 1, 168]) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            result = await asyncio.to_thread(self.pz_data.extract_player_logs, jogador, tipo.value, horas)
            content = "\n".join(result["lines"]) or t('Nenhuma linha encontrada.')
            file = discord.File(io.BytesIO(content.encode("utf-8")), filename=f"logs-{int(discord.utils.utcnow().timestamp())}.txt")
            await interaction.response.send_message(''.join((format(t('Encontradas **'), ''), format(len(result['lines']), ''), format(t('** linha(s) em '), ''), format(len(result['files']), ''), format(t(' arquivo(s).'), ''))), file=file, ephemeral=True)

        safehouse_group = app_commands.Group(name="safehouse", description=t('Gerencia safehouses e backups gerais'))

        @safehouse_group.command(name="listar", description=t('Lista todas as safehouses'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def safehouse_list(interaction: discord.Interaction) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            rows = await asyncio.to_thread(self.pz_data.safehouses)
            lines = [
                ''.join((format(index, ''), '. ', format(get_field(row, ['title'], t('Sem título')), ''), ' | ', format(get_field(row, ['owner'], t('Sem dono')), ''), ' | (', format(get_field(row, ['x'], '?'), ''), ',', format(get_field(row, ['y'], '?'), ''), ') -> (', format(get_field(row, ['x2'], '?'), ''), ',', format(get_field(row, ['y2'], '?'), ''), ')'))
                for index, row in enumerate(rows, 1)
            ]
            await interaction.response.send_message("\n".join(lines)[:1900] if lines else t('Nenhuma safehouse registrada.'), ephemeral=True)

        @safehouse_group.command(name="listar_bkp_geral", description=t('Lista backups gerais de safehouse'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def safehouse_backups(interaction: discord.Interaction) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            server = self.registry.get_default_server()
            async with PterodactylClient(server) as client:
                result = await client.list_backups()
            if not result["ok"]:
                await interaction.response.send_message(extract_ptero_error(result), ephemeral=True)
                return
            backups = [item for item in result.get("backups", []) if item["name"].startswith("Safehouses geral - ")]
            if not backups:
                await interaction.response.send_message(t('Nenhum backup geral encontrado.'), ephemeral=True)
                return
            lines = [
                f"`{item['uuid']}` {item['name']} | {item['completedAt'] or 'processando'} | {int(item['bytes'] or 0) / (1024 ** 2):.1f} MiB"
                for item in backups
            ]
            await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

        @safehouse_group.command(name="executar_bkp_geral", description=t('Cria backup geral antes de manutenção'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def safehouse_create_backup(interaction: discord.Interaction) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            server = self.registry.get_default_server()
            name = "Safehouses geral - " + discord.utils.utcnow().isoformat()
            async with PterodactylClient(server) as client:
                result = await client.create_backup(name=name, is_locked=True)
            if result["ok"]:
                backup = result.get("backup") or {}
                await interaction.response.send_message(''.join((format(t('Backup geral iniciado: `'), ''), format(backup.get('uuid', t('sem UUID')), ''), format(t('`. Ele inclui o save completo para manter safehouses e mundo consistentes.'), ''))), ephemeral=True)
            else:
                await interaction.response.send_message(extract_ptero_error(result), ephemeral=True)

        @safehouse_group.command(name="restaurar_bkp_geral", description=t('Restaura backup geral com o servidor offline'))
        @app_commands.describe(uuid=t('UUID do backup'), confirmar=t('Digite RESTAURAR'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def safehouse_restore_backup(interaction: discord.Interaction, uuid: str, confirmar: str) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            if clean_text(confirmar).upper() != "RESTAURAR":
                await interaction.response.send_message(t('Confirmação inválida. Digite exatamente RESTAURAR.'), ephemeral=True)
                return
            server = self.registry.get_default_server()
            state = await fetch_server_resources(server)
            if not state["ok"] or clean_text(state.get("state")).casefold() != "offline":
                current = state.get("state") or describe_server_error(state)
                await interaction.response.send_message(''.join((format(t('O servidor precisa estar offline. Estado: '), ''), format(current, ''), '.')), ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with PterodactylClient(server) as client:
                result = await client.restore_backup(uuid, truncate=False)
            await interaction.followup.send(t('Restauração solicitada ao Pterodactyl.') if result["ok"] else extract_ptero_error(result), ephemeral=True)

        self.tree.add_command(safehouse_group)

        async def send_player_admin_command(interaction: discord.Interaction, command: str, success: str) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await self._ptero_console_command(self.registry.get_default_server(), command)
            await interaction.followup.send(success if result["ok"] else t('Falha no console do Pterodactyl: ') + result["error"][:1500], ephemeral=True)

        @self.tree.command(name="adduser", description=t('Adiciona um usuário à whitelist do PZ'))
        @app_commands.describe(usuario=t('Usuário PZ'), senha=t('Senha inicial'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def add_pz_user(interaction: discord.Interaction, usuario: str, senha: str) -> None:
            try:
                command = adduser_command(usuario, senha)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await send_player_admin_command(interaction, command, t('Usuário adicionado à whitelist.'))

        @self.tree.command(name="removeuserfromwhitelist", description=t('Remove um usuário da whitelist do PZ'))
        @app_commands.describe(usuario=t('Usuário PZ'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def remove_pz_user(interaction: discord.Interaction, usuario: str) -> None:
            await send_player_admin_command(interaction, f"removeuserfromwhitelist {_quote_pz(usuario)}", t('Usuário removido da whitelist.'))

        @self.tree.command(name="banid", description=t('Bane um SteamID64 no servidor'))
        @app_commands.describe(steamid=t('SteamID64 numérico'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def ban_steam_id(interaction: discord.Interaction, steamid: str) -> None:
            normalized = "".join(character for character in steamid if character.isdigit())
            if not 15 <= len(normalized) <= 20:
                await interaction.response.send_message(t('SteamID inválido. Informe o SteamID64 numérico completo.'), ephemeral=True)
                return
            await send_player_admin_command(interaction, f"banid {normalized}", t('Banimento por SteamID enviado.'))

        @self.tree.command(name="kick", description=t('Expulsa um jogador do servidor'))
        @app_commands.describe(jogador=t('Nick no servidor'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def kick_player(interaction: discord.Interaction, jogador: str) -> None:
            await send_player_admin_command(interaction, f"kickuser {_quote_pz(jogador)}", ''.join((format(clean_text(jogador), ''), format(t(' expulso.'), ''))))

        @self.tree.command(name="kick_all", description=t('Expulsa todos os jogadores online'))
        @app_commands.describe(confirmar=t('Digite EXPULSAR'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def kick_all_players(interaction: discord.Interaction, confirmar: str) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            if clean_text(confirmar).upper() != "EXPULSAR":
                await interaction.response.send_message(t('Confirmação inválida. Digite exatamente EXPULSAR.'), ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            server = self.registry.get_default_server()
            rcon = server.get("rcon", {})
            online = await send_rcon_command(rcon.get("host", ""), int(rcon.get("port") or 0), rcon.get("password", ""), "players")
            names = _parse_players_output(online.output) if online.ok else []
            if not names:
                names = await asyncio.to_thread(self.pz_data.online_players)
            failures = []
            for name in names:
                result = await self._ptero_console_command(server, f"kickuser {_quote_pz(name)}")
                if not result["ok"]:
                    failures.append(name)
            await interaction.followup.send(''.join((format(t('Jogadores processados: '), ''), format(len(names), ''), format(t('. Falhas: '), ''), format(len(failures), ''))) + (f" ({', '.join(failures)[:900]})" if failures else ""), ephemeral=True)

        @self.tree.command(name="godmode", description=t('Ativa ou desativa o god mode de um jogador'))
        @app_commands.describe(jogador=t('Nick no servidor'), ativar=t('Estado desejado'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def set_godmode(interaction: discord.Interaction, jogador: str, ativar: bool) -> None:
            await send_player_admin_command(interaction, f"godmod {_quote_pz(jogador)} -{str(ativar).lower()}", ''.join((format(t('God mode '), ''), format(t('ativado') if ativar else t('desativado'), ''), format(t(' para '), ''), format(clean_text(jogador), ''), '.')))

        @self.tree.command(name="invisible", description=t('Ativa ou desativa a invisibilidade'))
        @app_commands.describe(jogador=t('Nick no servidor'), ativar=t('Estado desejado'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def set_invisible(interaction: discord.Interaction, jogador: str, ativar: bool) -> None:
            await send_player_admin_command(interaction, f"invisible {_quote_pz(jogador)} -{str(ativar).lower()}", ''.join((format(t('Invisibilidade '), ''), format('ativada' if ativar else 'desativada', ''), format(t(' para '), ''), format(clean_text(jogador), ''), '.')))

        @self.tree.command(name="grantadmin", description=t('Concede acesso administrativo a um jogador'))
        @app_commands.describe(jogador=t('Nick no servidor'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def grant_admin(interaction: discord.Interaction, jogador: str) -> None:
            await send_player_admin_command(interaction, f"grantadmin {_quote_pz(jogador)}", ''.join((format(t('Admin concedido para '), ''), format(clean_text(jogador), ''), '.')))

        @self.tree.command(name="removeadmin", description=t('Remove acesso administrativo de um jogador'))
        @app_commands.describe(jogador=t('Nick no servidor'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def remove_admin(interaction: discord.Interaction, jogador: str) -> None:
            await send_player_admin_command(interaction, f"removeadmin {_quote_pz(jogador)}", ''.join((format(t('Admin removido de '), ''), format(clean_text(jogador), ''), '.')))

        @self.tree.command(name="tpto", description=t('Teleporta jogador para coordenadas'))
        @app_commands.describe(jogador=t('Nick no servidor'), x=t('Coordenada X'), y=t('Coordenada Y'), z=t('Andar Z'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def teleport_coordinates(interaction: discord.Interaction, jogador: str, x: int, y: int, z: int | None = None) -> None:
            await send_player_admin_command(interaction, f"teleportto {_quote_pz(jogador)} {x},{y},{z or 0}", ''.join((format(clean_text(jogador), ''), format(t(' teleportado para as coordenadas.'), ''))))

        @self.tree.command(name="tp", description=t('Teleporta um jogador até outro'))
        @app_commands.describe(jogador=t('Nick que será teleportado'), destino=t('Nick de destino'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def teleport_player(interaction: discord.Interaction, jogador: str, destino: str) -> None:
            await send_player_admin_command(interaction, f"teleport {_quote_pz(jogador)} {_quote_pz(destino)}", ''.join((format(clean_text(jogador), ''), format(t(' teleportado.'), ''))))

        @self.tree.command(name="servermsg", description=t('Envia mensagem para o servidor PZ'))
        @app_commands.describe(mensagem=t('Mensagem para os jogadores'))
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def server_message(interaction: discord.Interaction, mensagem: app_commands.Range[str, 1, 500]) -> None:
            await send_player_admin_command(interaction, f"servermsg {_quote_pz(mensagem)}", t('Mensagem enviada ao servidor.'))

        @self.tree.command(name="deletearquivo", description=t('Apaga dados FriendHost para forçar sua recriação'))
        @app_commands.describe(alvo=t('Escopo da limpeza'), nick=t('Obrigatório quando alvo é jogador'))
        @app_commands.choices(alvo=[
            app_commands.Choice(name="Ranking geral", value="ranking"),
            app_commands.Choice(name="Inventários", value="inventarios"),
            app_commands.Choice(name="Jogador específico", value="jogador"),
            app_commands.Choice(name="Tudo do FriendHost", value="tudo"),
        ])
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def delete_friendhost_files(interaction: discord.Interaction, alvo: app_commands.Choice[str], nick: str | None = None) -> None:
            if not await _admin_guard(interaction, self.store):
                return
            if alvo.value == "jogador" and not clean_text(nick):
                await interaction.response.send_message(t('Informe o nick ao escolher o alvo jogador.'), ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            targets = await asyncio.to_thread(self.pz_data.delete_targets, alvo.value, nick or "")
            if targets is None:
                await interaction.followup.send(t('Jogador não encontrado nos dados do FriendHost.'), ephemeral=True)
                return
            deleted, failed = [], []
            for path in targets:
                try:
                    path.unlink()
                    deleted.append(path)
                except OSError as exc:
                    failed.append(f"{path.name} ({exc})")
            details = "\n".join(f"- {path}" for path in deleted[:45])
            message = ''.join((format(t('Arquivos apagados: '), ''), format(len(deleted), '')))
            if details:
                message += "\n" + details
            if len(deleted) > 45:
                message += ''.join((format(t('\n... e mais '), ''), format(len(deleted) - 45, '')))
            if failed:
                message += ''.join((format(t('\nFalhas: '), ''), format(len(failed), ''), '\n')) + "\n".join(f"- {item}" for item in failed[:10])
            self.pz_data = PzData(getenv("CSV_BASE_PATH"))
            await interaction.followup.send(message[:1900], ephemeral=True)

        async def autocomplete_player(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
            names = await asyncio.to_thread(self.pz_data.player_names)
            query = clean_text(current).casefold()
            return [app_commands.Choice(name=name[:100], value=name[:100]) for name in names if query in name.casefold()][:25]

        for command, option_name in (
            (player_info, "jogador"), (player_skills, "jogador"), (player_traits, "jogador"),
            (player_rank, "jogador"), (gps_player, "jogador"), (player_status, "nick"),
            (player_status_complete, "nick"), (player_logs, "jogador"), (panel_link_player, "jogador"),
            (panel_unlink_player, "jogador"), (kick_player, "jogador"), (set_godmode, "jogador"),
            (set_invisible, "jogador"), (grant_admin, "jogador"), (remove_admin, "jogador"),
            (teleport_coordinates, "jogador"), (teleport_player, "jogador"), (teleport_player, "destino"),
            (delete_friendhost_files, "nick"),
        ):
            command.autocomplete(option_name)(autocomplete_player)

    async def _panel_media_config(self, interaction: discord.Interaction, key: str, attachment: discord.Attachment | None, url: str | None) -> None:
        if not await _admin_guard(interaction, self.store):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if clean_text(url).casefold() in {"limpar", "remover", "clear", "remove"}:
            update_draft_theme({key: None})
            await interaction.followup.send(t('Mídia removida do rascunho. Use `/painel preview` e depois `/painel salvar`.'), ephemeral=True)
            return
        try:
            media = await store_panel_media(key, attachment=attachment, url=url or "")
            update_draft_theme({key: media})
        except (ValueError, aiohttp.ClientError, TimeoutError) as exc:
            await interaction.followup.send(''.join((format(t('Não foi possível salvar a mídia: '), ''), format(exc, ''))), ephemeral=True)
            return
        await interaction.followup.send(t('Mídia armazenada no rascunho. Use `/painel preview` e depois `/painel salvar`.'), ephemeral=True)

    async def _panel_text_config(self, interaction: discord.Interaction, key: str, value: str, *, is_color: bool = False) -> None:
        if not await _admin_guard(interaction, self.store):
            return
        normalized = clean_text(value)
        if is_color:
            raw = normalized.removeprefix("#")
            if not re.fullmatch(r"[0-9a-fA-F]{6}", raw):
                await interaction.response.send_message(t('Cor inválida. Use seis dígitos hexadecimais, por exemplo `#28D17C`.'), ephemeral=True)
                return
            normalized = f"#{raw.upper()}"
        update_draft_theme({key: normalized})
        await interaction.response.send_message(t('Rascunho atualizado. Use `/painel preview` e depois `/painel salvar`.'), ephemeral=True)

    async def _panel_link_player(self, interaction: discord.Interaction, nick: str, user_id: int) -> None:
        if not await _admin_guard(interaction, self.store):
            return
        if not set_player_link(nick, user_id):
            await interaction.response.send_message(t('Informe um nick PZ e um membro do Discord.'), ephemeral=True)
            return
        await interaction.response.send_message(''.join(('**', format(clean_text(nick), ''), format(t('** foi vinculado a <@'), ''), format(user_id, ''), '>.')), ephemeral=True)

    async def _panel_unlink_player(self, interaction: discord.Interaction, nick: str) -> None:
        if not await _admin_guard(interaction, self.store):
            return
        removed = remove_player_link(nick)
        await interaction.response.send_message(
            ''.join((format(t('Vínculo de **'), ''), format(clean_text(nick), ''), format(t('** removido.'), ''))) if removed else ''.join(('**', format(clean_text(nick), ''), format(t('** não possuía vínculo.'), ''))), ephemeral=True,
        )

    async def _panel_power(self, interaction: discord.Interaction, action: str) -> None:
        if not await _admin_guard(interaction, self.store):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await manage_server(self.registry.get_default_server(), action)
        via = "Docker/local" if result.get("transport") == "docker" else "Pterodactyl"
        message = ''.join((format(t('Comando `'), ''), format(action, ''), format(t('` enviado via '), ''), format(via, ''), '.')) if result.get("ok") else describe_server_error(result)
        await interaction.followup.send(message[:1900], ephemeral=True)

    async def _panel_resources(self, interaction: discord.Interaction) -> None:
        if not await _admin_guard(interaction, self.store):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await fetch_server_resources(self.registry.get_default_server())
        if not result.get("ok"):
            await interaction.followup.send(describe_server_error(result), ephemeral=True)
            return
        resources = result.get("resources", {})
        def byte_size(value: object) -> str:
            size = max(0, to_number(value))
            if size < 1024: return f"{size:.0f} B"
            if size < 1024 ** 2: return f"{size / 1024:.1f} KB"
            if size < 1024 ** 3: return f"{size / 1024 ** 2:.1f} MB"
            return f"{size / 1024 ** 3:.2f} GB"
        embed = discord.Embed(title=t('Recursos do servidor'), color=0x2C3E50, timestamp=discord.utils.utcnow())
        embed.add_field(name=t('Estado'), value=str(result.get("state", t('Desconhecido'))), inline=True)
        embed.add_field(name="CPU", value=f"{to_number(resources.get('cpu_absolute')):.1f}%", inline=True)
        embed.add_field(name="RAM", value=byte_size(resources.get("memory_bytes")), inline=True)
        embed.add_field(name=t('Disco'), value=byte_size(resources.get("disk_bytes")), inline=True)
        embed.add_field(name=t('Rede recebida'), value=byte_size(resources.get("network_rx_bytes")), inline=True)
        embed.add_field(name=t('Rede enviada'), value=byte_size(resources.get("network_tx_bytes")), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _ranking_payload(self, guild_id, *, theme=None):
        selected = self.store.get_rankings(guild_id)
        data = self.pz_data
        players = await asyncio.to_thread(data.player_rows)
        async def optional(enabled, function, *args):
            return await asyncio.to_thread(function, *args) if enabled else None
        factions, deaths, skills = await asyncio.gather(
            optional(bool({"factions", "faction_highlight"}.intersection(selected)), data.faction_rows),
            optional("deaths" in selected, data.death_counts),
            optional("skills10" in selected, data.skills10_rows, players),
        )
        return await asyncio.to_thread(build_ranking_panel, players, factions or [], deaths or {}, theme=theme, enabled=selected, skills10=skills)

    async def _panel_preview(self, interaction: discord.Interaction, panel_type: str) -> None:
        if not await _admin_guard(interaction, self.store):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        theme = get_draft_theme()
        if panel_type == "ranking":
            embeds, files = await self._ranking_payload(interaction.guild_id, theme=theme)
        else:
            overview = await self._build_panel_overview(self.registry.get_default_server())
            players = await asyncio.to_thread(self.pz_data.player_rows)
            profiles = await self._online_profiles(overview["onlinePlayers"]["names"], players)
            embeds, files = build_server_panel(
                overview, online_profiles=profiles, theme=theme,
                max_slots=int(getenv("SERVER_MAX_SLOTS")) if getenv("SERVER_MAX_SLOTS", "").isdigit() else None,
                connect_address=self.registry.get_default_server().get("connectAddress") or getenv("SERVER_CONNECT_ADDRESS", ""),
            )
        await interaction.followup.send(embeds=embeds, files=files, ephemeral=True)

    async def _panel_save(self, interaction: discord.Interaction) -> None:
        if not await _admin_guard(interaction, self.store):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        theme = save_draft_theme()
        try:
            await self.refresh_panels()
        except Exception:
            logger.exception("Tema salvo, mas a atualizacao dos paineis falhou")
            await interaction.followup.send(''.join((format(t('Tema **'), ''), format(theme['title'], ''), format(t('** salvo, mas a atualização imediata dos painéis falhou. Confira os canais e permissões.'), ''))), ephemeral=True)
            return
        await interaction.followup.send(''.join((format(t('Tema **'), ''), format(theme['title'], ''), format(t('** salvo. Os painéis configurados foram atualizados.'), ''))), ephemeral=True)

    async def _build_panel_overview(self, server: dict[str, Any]) -> dict[str, Any]:
        ptero_task = fetch_server_resources(server)
        rcon_task = send_rcon_command(server["rcon"].get("host", ""), int(server["rcon"].get("port") or 0), server["rcon"].get("password", ""), "players")
        ptero, rcon = await asyncio.gather(ptero_task, rcon_task)
        output = rcon.output if rcon.ok else ""
        names = _parse_players_output(output)
        count_match = re.search(r"\((\d+)\)|players?[^\d]*(\d+)|connected[^\d]*(\d+)", output, re.I)
        count = int(next((value for value in count_match.groups() if value), len(names))) if count_match else len(names)
        current_state = clean_text(ptero.get("state"))
        source = "rcon" if rcon.ok else "none"
        if not rcon.ok and (not ptero.get("ok") or current_state.lower() in {"running", "starting"}):
            names = await asyncio.to_thread(self.pz_data.online_players)
            if names:
                source, count = "csv", len(names)
        if not current_state:
            current_state = "running" if source != "none" else "offline"
        online = current_state.lower() in {"running", "starting"} or (count > 0 and current_state.lower() != "offline")
        return {
            "isOnline": online, "currentState": current_state, "ptero": ptero,
            "rconPlayers": {"ok": rcon.ok, "output": output, "error": rcon.error},
            "onlinePlayers": {"count": count, "names": names, "source": source},
            "world": await asyncio.to_thread(self.pz_data.world_snapshot),
        }

    async def _online_profiles(self, online_names: list[str], player_rows: list[dict]) -> list[dict]:
        normalized = {name.casefold() for name in online_names}
        for key in tuple(self._online_since):
            if key not in normalized:
                self._online_since.pop(key, None)
        now = asyncio.get_running_loop().time()
        profiles = []
        rows_by_name = {get_field(entry.get("row", entry), ["username"]).casefold(): entry.get("row", entry) for entry in player_rows}
        links_by_name = {name.casefold(): user_id for name, user_id in (await asyncio.to_thread(load_player_links)).items()}
        for nick in online_names:
            key = nick.casefold()
            started = self._online_since.setdefault(key, now)
            row = rows_by_name.get(key, {})
            link = links_by_name.get(nick.casefold(), "")
            user = self.get_user(int(link)) if link and link.isdigit() else None
            seconds = max(0, int(now - started))
            duration = f"{seconds // 3600}h {seconds % 3600 // 60}m" if seconds >= 3600 else f"{seconds // 60}m"
            profiles.append({
                "nick": nick, "faction": get_field(row, ["factionname"]),
                "discordId": link, "avatarUrl": user.display_avatar.url if user else "", "onlineTime": duration,
            })
        return profiles

    async def _upsert_panel_message(self, guild: discord.Guild, config: dict[str, Any], channel_key: str, message_key: str, embeds: list[discord.Embed], files: list[discord.File]) -> None:
        channel_id = config["channels"].get(channel_key)
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            try:
                fetched = await self.fetch_channel(channel_id)
                channel = fetched if isinstance(fetched, discord.TextChannel) and fetched.guild.id == guild.id else None
            except discord.HTTPException:
                channel = None
        if channel is None:
            logger.warning("Canal %s configurado para painel %s não foi encontrado na guild %s", channel_id, channel_key, guild.id)
            return
        message = None
        message_id = config.get(message_key)
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                message = None
            except discord.HTTPException:
                logger.exception("Falha ao buscar mensagem de painel %s", message_id)
                return
        elif self.user:
            try:
                async for candidate in channel.history(limit=20):
                    if candidate.author.id == self.user.id and candidate.embeds:
                        message = candidate
                        break
            except discord.HTTPException:
                logger.exception("Falha ao procurar mensagem anterior do painel em %s", channel.id)
        try:
            if message:
                await message.edit(embeds=embeds, attachments=files)
            else:
                message = await channel.send(embeds=embeds, files=files)
                self.store.set_top_value(guild.id, message_key, message.id)
            if message_id and message.id != message_id:
                self.store.set_top_value(guild.id, message_key, message.id)
            elif not message_id and message:
                self.store.set_top_value(guild.id, message_key, message.id)
        except discord.HTTPException:
            logger.exception("Falha ao publicar painel %s na guild %s", channel_key, guild.id)

    async def refresh_panels(self) -> None:
        if current_scope() is None:
            for guild in self.guilds:
                runtime = self._runtimes.get(guild.id)
                if runtime:
                    with use_scope(runtime.scope):
                        await self.refresh_panels()
            return
        if current_scope().runtime is None:
            return
        if not self.user:
            return
        for guild in scoped_guilds(self):
            config = self.store.get_settings(guild.id)
            if config["channels"].get("stats"):
                server = self.registry.get_default_server()
                overview = await self._build_panel_overview(server)
                players = await asyncio.to_thread(self.pz_data.player_rows)
                profiles = await self._online_profiles(overview["onlinePlayers"]["names"], players)
                embeds, files = build_server_panel(
                    overview, online_profiles=profiles,
                    max_slots=int(getenv("SERVER_MAX_SLOTS")) if getenv("SERVER_MAX_SLOTS", "").isdigit() else None,
                    connect_address=server.get("connectAddress") or getenv("SERVER_CONNECT_ADDRESS", ""),
                )
                await self._upsert_panel_message(guild, config, "stats", "stats_panel_message_id", embeds, files)
            if config["channels"].get("ranking") and self.pz_data.base_path:
                embeds, files = await self._ranking_payload(guild.id)
                await self._upsert_panel_message(guild, config, "ranking", "ranking_panel_message_id", embeds, files)


    async def _set_automation(self, interaction: discord.Interaction, automation_id: str, enabled: bool) -> None:
        if not await _admin_guard(interaction, self.store):
            return
        automation = self.automations.set_enabled(automation_id, enabled)
        if not automation:
            await interaction.response.send_message(t('Automação não encontrada.'), ephemeral=True)
            return
        action = t('reativada') if enabled else t('pausada')
        await interaction.response.send_message(''.join((format(t('Automação '), ''), format(action, ''), '.\n', format(self.automations.format_line(automation), ''))), ephemeral=True)


    async def _validate_kick_auto_setup(self, guild: discord.Guild | None) -> tuple[bool, str]:
        if guild is None:
            return False, t('Este comando so funciona dentro de um servidor Discord.')
        settings = self.store.get_settings(guild.id)
        voice_channel = guild.get_channel(settings["kick_automatico"].get("voice_channel_id"))
        if not isinstance(voice_channel, discord.VoiceChannel):
            return False, t('Selecione uma call de voz existente antes de ativar a fiscalização.')

        server = self.registry.get_server(settings["wl"].get("server_id", "default"))
        missing = describe_missing_rcon(server)
        if missing:
            return False, ''.join((format(t('RCON incompleto para o servidor selecionado: '), ''), format(', '.join(missing), ''), '.'))
        rcon = server["rcon"]
        result = await send_rcon_command(rcon["host"], int(rcon["port"]), rcon["password"], "players")
        if not result.ok:
            return False, ''.join((format(t('Nao foi possivel validar o RCON: '), ''), format(result.error, '')))
        names = _parse_players_snapshot(result.output)
        if names is None:
            return False, t('A resposta de `/players` nao trouxe uma lista completa e reconhecivel; a fiscalização nao foi ativada.')

        links = await asyncio.to_thread(load_player_links)
        if names and not links:
            return False, t('O arquivo de vínculos PZ/Discord está vazio ou indisponível; a fiscalização não foi ativada.')
        links_by_name = {name.casefold(): user_id for name, user_id in links.items()}
        unlinked = [name for name in names if not links_by_name.get(name.casefold(), "").isdigit()]
        if unlinked:
            sample = ", ".join(f"`{discord.utils.escape_markdown(name)}`" for name in unlinked[:12])
            remainder = ''.join((format(t(' e mais '), ''), format(len(unlinked) - 12, ''))) if len(unlinked) > 12 else ""
            return False, ''.join((format(t('Vincule os jogadores online antes de ativar usando `/painel jogadores vincular`: '), ''), format(sample, ''), format(remainder, ''), '.'))
        return True, t('RCON e lista de jogadores validados. Jogadores novos também precisam estar vinculados antes de entrar no servidor.')

    def _clear_kick_auto_state(self, guild_id: int) -> None:
        for state in (self._kick_auto_missing_since, self._kick_auto_last_attempt):
            for key in [key for key in state if key[0] == guild_id]:
                state.pop(key, None)

    def _kick_auto_log_warning(self, guild_id: int, message: str) -> None:
        now = asyncio.get_running_loop().time()
        if now - self._kick_auto_last_warning.get(guild_id, -60.0) >= 60:
            self._kick_auto_last_warning[guild_id] = now
            logger.warning("Kick automatico pausado na guild %s: %s", guild_id, message)

    async def _scan_kick_auto_guild(self, guild: discord.Guild) -> bool:
        settings = self.store.get_settings(guild.id)
        kick_config = settings["kick_automatico"]
        if not kick_config["enabled"]:
            self._clear_kick_auto_state(guild.id)
            return True

        voice_channel = guild.get_channel(kick_config.get("voice_channel_id"))
        if not isinstance(voice_channel, discord.VoiceChannel):
            self._clear_kick_auto_state(guild.id)
            self._kick_auto_log_warning(guild.id, t('a call configurada nao existe mais ou nao e uma call de voz'))
            return False

        server = self.registry.get_server(settings["wl"].get("server_id", "default"))
        missing = describe_missing_rcon(server)
        if missing:
            self._clear_kick_auto_state(guild.id)
            self._kick_auto_log_warning(guild.id, ''.join((format(t('RCON incompleto: '), ''), format(', '.join(missing), ''))))
            return False

        rcon = server["rcon"]
        response = await send_rcon_command(rcon["host"], int(rcon["port"]), rcon["password"], "players")
        if not response.ok:
            self._clear_kick_auto_state(guild.id)
            self._kick_auto_log_warning(guild.id, response.error)
            return False
        names = _parse_players_snapshot(response.output)
        if names is None:
            self._clear_kick_auto_state(guild.id)
            self._kick_auto_log_warning(guild.id, t('a resposta de `/players` nao e uma lista completa reconhecivel'))
            return False

        links = await asyncio.to_thread(load_player_links)
        if names and not links:
            self._clear_kick_auto_state(guild.id)
            self._kick_auto_log_warning(guild.id, t('o arquivo de vinculos PZ/Discord esta vazio ou indisponivel'))
            return False
        links_by_name = {name.casefold(): user_id for name, user_id in links.items()}
        voice_member_ids = set(voice_channel.voice_states)
        now = asyncio.get_running_loop().time()
        present_names = {name.casefold() for name in names}
        for key in [key for key in self._kick_auto_missing_since if key[0] == guild.id and key[1] not in present_names]:
            self._kick_auto_missing_since.pop(key, None)
            self._kick_auto_last_attempt.pop(key, None)

        for name in names:
            player_key = (guild.id, name.casefold())
            linked_user_id = links_by_name.get(name.casefold(), "")
            in_required_call = linked_user_id.isdigit() and int(linked_user_id) in voice_member_ids
            if in_required_call:
                self._kick_auto_missing_since.pop(player_key, None)
                self._kick_auto_last_attempt.pop(player_key, None)
                continue

            missing_since = self._kick_auto_missing_since.setdefault(player_key, now)
            if now - missing_since < 60:
                continue
            if now - self._kick_auto_last_attempt.get(player_key, -60.0) < 60:
                continue

            # Recheck live voice state immediately before the destructive action.
            linked_user_id = links_by_name.get(name.casefold(), "")
            if linked_user_id.isdigit() and int(linked_user_id) in set(voice_channel.voice_states):
                self._kick_auto_missing_since.pop(player_key, None)
                self._kick_auto_last_attempt.pop(player_key, None)
                continue

            self._kick_auto_last_attempt[player_key] = now
            call_name = clean_text(voice_channel.name)[:80]
            reason = ''.join((format(t('É necessário estar na call ['), ''), format(call_name, ''), format(t('] para poder permanecer no servidor.'), '')))
            command = f"kickuser {_quote_pz(name)} -r={_quote_pz(reason)}"
            kick_result = await send_rcon_command(rcon["host"], int(rcon["port"]), rcon["password"], command)
            if kick_result.ok:
                logger.info("Kick automatico enviado: jogador=%s guild=%s call=%s", name, guild.id, call_name)
                admin_channel = _configured_text_channel(guild, settings["channels"].get("admin"))
                if admin_channel:
                    try:
                        await admin_channel.send(
                            ''.join((format(t('Kick automático enviado para `'), ''), format(discord.utils.escape_markdown(name), ''), format(t('`: ficou 60 segundos fora de <#'), ''), format(voice_channel.id, ''), format(t('>. Motivo enviado pelo PZ: '), ''), format(reason, ''))),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.HTTPException:
                        logger.warning("Nao foi possivel registrar o kick automatico no canal admin da guild %s", guild.id)
            else:
                self._kick_auto_log_warning(guild.id, ''.join((format(t('falha ao expulsar '), ''), format(name, ''), ': ', format(kick_result.error, ''))))
        return True

    async def setup_hook(self) -> None:
        mark_command_strings(self.tree)
        await self.tree.set_translator(HTSTranslator())
        from concurrent.futures import ThreadPoolExecutor
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=4, thread_name_prefix="pz-files"))
        self._ptero_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=8, limit_per_host=4),
            timeout=aiohttp.ClientTimeout(total=15),
        )
        self.add_view(PublicTicketView(self))
        self.add_view(TicketActionView(self))
        self.add_view(ReviewView(self))
        if self.updater.auto_update_enabled and self.updater.python_runtime_configured:
            self._auto_update_task = asyncio.create_task(self._auto_update_loop(), name="manguadu-auto-update")
        elif self.updater.auto_update_enabled:
            logger.warning("Auto-update Python foi solicitado, mas PM2 ainda nao esta configurado para Python.")

    async def close(self) -> None:
        tasks = [task for task in (self._auto_update_task, self._panel_refresh_task, self._automation_task, self._anticheat_task, self._watcher_task) if task and not task.done()]
        if self._kick_auto_task and not self._kick_auto_task.done():
            tasks.append(self._kick_auto_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._ptero_session is not None:
            await self._ptero_session.close()
            self._ptero_session = None
        await super().close()
        self.store.close()

    async def on_ready(self) -> None:
        activate(None)
        logger.info("Bot Python online como %s", self.user)
        for guild in self.guilds:
            binding = self.store.get_binding(guild.id)
            if not binding and str(guild.id) == os.getenv("GUILD_ID", ""):
                try:
                    volume_id = os.getenv("PTERO_VOLUME_ID", "")
                    if volume_id:
                        relative_lua = ".cache/Lua"
                        if os.getenv("PZ_LUA_PATH"):
                            try:
                                volume = Path(os.getenv("PTERO_VOLUMES_ROOT", "/var/lib/pterodactyl/volumes")) / volume_id
                                relative_lua = Path(os.environ["PZ_LUA_PATH"]).resolve().relative_to(volume.resolve()).as_posix()
                            except ValueError:
                                pass
                        binding = normalize_binding(os.getenv("PTERO_SERVER_ID", ""), volume_id, guild.name, relative_lua, os.getenv("SERVER_CONNECT_ADDRESS", ""))
                        binding["legacy"] = True
                        self.store.save_binding(guild.id, binding)
                        self._migrate_legacy_files(guild.id)
                except (ValueError, sqlite3.IntegrityError):
                    logger.warning("Cadastro legado requer validacao manual em /config na guild %s", guild.id)
            if binding and guild.id not in self._runtimes:
                try:
                    self._runtimes[guild.id] = await asyncio.to_thread(GuildRuntime, self, guild.id, binding)
                except (ValueError, OSError):
                    logger.warning("Volume da guild %s indisponivel; nenhum fallback para outro servidor sera usado", guild.id)
            self._seed_legacy_guild_settings(guild.id)
            if guild.id in self._runtimes:
                self._schedule_guild(guild.id)
        if not self._registered:
            await self.register_python_commands()
            self._registered = True
        if not self._panel_refresh_task or self._panel_refresh_task.done():
            self._panel_refresh_task = asyncio.create_task(self._run_queues(), name="friendhost-guild-queues")

    def _migrate_legacy_files(self, guild_id: int) -> None:
        import shutil
        from .tenant_scope import DATA_ROOT
        for name in ("panel-theme.json", "panel-theme-draft.json", "panel-player-links.json", "automations.json", "anticheat-alerts-state.json"):
            source = DATA_ROOT / name
            target = DATA_ROOT / "guilds" / str(guild_id) / name
            if source.is_file() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        # New guilds remain unbound until their own administrator proves control.
        binding = self.store.get_binding(guild.id)
        if binding:
            try:
                self._runtimes[guild.id] = await asyncio.to_thread(GuildRuntime, self, guild.id, binding)
                self._schedule_guild(guild.id)
            except (ValueError, OSError):
                logger.warning("Volume indisponivel na guild %s", guild.id)
        else:
            logger.info("Guild %s adicionada; aguardando /config", guild.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.work_queue.remove_prefix(f"{guild.id}:")
        self.log_queue.remove_prefix(f"{guild.id}:")
        self._runtimes.pop(guild.id, None)
        self._clear_kick_auto_state(guild.id)

    async def on_disconnect(self) -> None:
        for guild_id in self._runtimes:
            self._clear_kick_auto_state(guild_id)

    def _seed_legacy_guild_settings(self, guild_id: int) -> None:
        """Importa IDs de canal do .env uma vez para preservar instalações existentes."""
        if str(guild_id) != os.getenv("GUILD_ID", ""):
            return
        mapping = {
            "admin": "ID_CANAL_ADMIN", "evolution": "ID_CANAL_EVOLUCAO",
            "stats": "ID_CANAL_STATS", "ranking": "ID_CANAL_RANKING",
            "chat": "ID_CANAL_CHAT", "anticheat": "ANTICHEAT_ALERT_CHANNEL_ID",
        }
        settings = self.store.get_settings(guild_id)
        for key, variable in mapping.items():
            if settings["channels"].get(key):
                continue
            raw_id = clean_text(getenv(variable))
            if raw_id.isdigit():
                self.store.set_value(guild_id, "channels", key, int(raw_id))

    async def register_python_commands(self) -> None:
        """Sincroniza o conjunto completo de comandos deste runtime Python."""
        app_id = self.application_id or int(getenv("DISCORD_CLIENT_ID") or self.user.id)
        guild_id = getenv("GUILD_ID", "").strip()
        endpoint = f"https://discord.com/api/v10/applications/{app_id}"
        endpoints = [endpoint + "/commands"]
        if guild_id:
            endpoints.append(endpoint + f"/guilds/{guild_id}/commands")
        headers = {"Authorization": f"Bot {os.environ['DISCORD_TOKEN']}", "Content-Type": "application/json"}
        mark_command_strings(self.tree)
        translator = self.tree.translator or HTSTranslator()
        payloads = [await command.get_translated_payload(self.tree, translator) for command in self.tree.get_commands()]
        async with aiohttp.ClientSession() as session:
            for target in endpoints:
                async with session.put(target, json=payloads, headers=headers) as response:
                    if response.status >= 300:
                        raise RuntimeError(''.join((format(t('Falha ao sincronizar comandos Python: HTTP '), ''), format(response.status, ''), ': ', format((await response.text())[:500], ''))))
        logger.info("%d comandos Python sincronizados globalmente", len(payloads))

    async def _updater_admin_guard(self, interaction: discord.Interaction) -> bool:
        app = await self.application_info()
        owners = {app.owner.id} if app.owner else set()
        if app.team:
            owners.add(app.team.owner_id)
        if interaction.user.id not in owners:
            await interaction.response.send_message(t('A atualização desta instância compartilhada é restrita ao proprietário do aplicativo Discord.'), ephemeral=True)
            return False
        return True

    async def _restart_pm2(self) -> None:
        await asyncio.sleep(3)
        pm2 = getenv("PM2_BIN", "pm2").strip() or "pm2"
        kwargs: dict[str, Any] = {"cwd": str(self.updater.root), "stdout": asyncio.subprocess.DEVNULL, "stderr": asyncio.subprocess.DEVNULL}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        try:
            await asyncio.create_subprocess_exec(pm2, "restart", self.updater.app_name, "--update-env", **kwargs)
        except OSError:
            logger.exception("Falha ao reiniciar o PM2")

    async def _notify_updater(self, message: str) -> None:
        for guild in scoped_guilds(self):
            config = self.store.get_settings(guild.id)
            channel = _configured_text_channel(guild, config["channels"].get("admin"))
            if channel:
                try:
                    await channel.send(message)
                except discord.HTTPException:
                    logger.exception("Falha ao enviar aviso de auto-update para guild %s", guild.id)

    async def _auto_update_loop(self) -> None:
        while not self.is_closed():
            try:
                await asyncio.sleep(self.updater.auto_update_interval_ms / 1000)
                check = await self.updater.check_for_update()
                if not check.get("ok"):
                    logger.warning("Falha na verificacao do auto-update: %s", check.get("error"))
                    continue
                if not check.get("hasUpdate"):
                    continue
                await self._notify_updater(''.join((format(t('[BOT] Atualizacao Python encontrada: '), ''), format(check['local'][:7], ''), ' -> ', format(check['remote'][:7], ''), '.')))
                result = await self.updater.update(skip_restart=True)
                if not result["ok"]:
                    await self._notify_updater(t('[BOT] Auto-update falhou.\n') + str(result.get("error") or result.get("output") or t('Sem detalhes.'))[:1000])
                    continue
                await self._notify_updater(t('[BOT] Auto-update concluido. Reiniciando o bot.'))
                asyncio.create_task(self._restart_pm2())
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Falha no loop de auto-update")

    async def _handle_wipe(
        self,
        interaction: discord.Interaction,
        kind: str,
        target: str,
        confirmation: str,
        force: bool,
        *,
        dry_run: bool = False,
    ) -> None:
        if not await _admin_guard(interaction, self.store):
            return
        expected = "APAGAR_TUDO" if kind == "global" else "APAGAR"
        if not dry_run and clean_text(confirmation).upper() != expected:
            await interaction.response.send_message(''.join((format(t('Confirmação inválida. Digite exatamente '), ''), format(expected, ''), '.')), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        plan = await build_wipe_plan(kind, target, force=True if kind == "global" else force, data=self.pz_data)
        if not plan.get("ok"):
            await interaction.followup.send(str(plan.get("error", t('Falha ao planejar o wipe.')))[:1900], ephemeral=True)
            return
        if dry_run:
            await interaction.followup.send(t('SIMULAÇÃO, nada foi apagado.\n```\n') + format_wipe_plan(plan)[:1750] + "\n```", ephemeral=True)
            return
        await interaction.followup.send(''.join((format(t('Plano validado para '), ''), format(len(plan['files']), ''), format(t(' arquivo(s). Criando backup antes do wipe; isso pode levar alguns minutos.'), ''))), ephemeral=True)
        result = await execute_wipe_plan(plan)
        if result.get("ok"):
            backup = result.get("backup") or {}
            message = ''.join((format(t('Wipe concluído: '), ''), format(len(result.get('deleted', [])), ''), format(t(' arquivo(s) apagados. Backup: '), ''), format(backup.get('uuid', t('sem identificador')), ''), '.'))
        else:
            message = t('Wipe abortado: ') + str(result.get("error", t('falha desconhecida')))
            if result.get("backup"):
                message += ''.join((format(t(' Backup criado: '), ''), format(result['backup'].get('uuid', t('sem identificador')), ''), '.'))
        await interaction.followup.send(message[:1900], ephemeral=True)

    async def _console_command(self, server: dict[str, Any], command: str) -> dict[str, Any]:
        rcon = server.get("rcon", {})
        result = await send_rcon_command(rcon.get("host", ""), int(rcon.get("port") or 0), rcon.get("password", ""), command)
        if result.ok:
            return {"ok": True, "transport": "rcon", "output": result.output, "error": ""}
        async with PterodactylClient(server) as client:
            fallback = await client.command(command)
        if fallback["ok"]:
            return {"ok": True, "transport": "pterodactyl", "output": "", "error": result.error}
        return {"ok": False, "transport": "", "output": "", "error": f"RCON: {result.error} | Pterodactyl: {extract_ptero_error(fallback)}"}

    async def _ptero_console_command(self, server: dict[str, Any], command: str) -> dict[str, Any]:
        async with PterodactylClient(server) as client:
            result = await client.command(command)
        if result["ok"]:
            return {"ok": True, "error": "", "transport": "pterodactyl"}
        return {"ok": False, "error": extract_ptero_error(result), "transport": ""}

    async def _scheduled_server_stop(self, server: dict[str, Any]) -> None:
        try:
            await asyncio.sleep(600)
            await self._console_command(server, "servermsg " + _quote_pz(t("DESLIGANDO AGORA.")))
            await self._console_command(server, "save")
            await asyncio.sleep(5)
            result = await manage_server(server, "stop")
            if not result["ok"]:
                logger.error("Desligamento agendado falhou: %s", describe_server_error(result))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Falha no desligamento agendado")

    async def publish_ticket_panel(self, guild: discord.Guild) -> discord.TextChannel:
        config = self.store.get_settings(guild.id)
        channel_id = config["channels"]["ticket_panel"]
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            raise ValueError(t('Selecione um canal de texto para o painel de tickets.'))
        embed = discord.Embed(title=t('🎫 Atendimento HTS Bot Multi servers'), description=t('Precisa criar seu personagem e receber whitelist no Project Zomboid?\nClique em **Abrir ticket**. Um canal privado será criado para você e a equipe.'), color=0x28D17C)
        message_id = config["ticket_panel_message_id"]
        message = None
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                pass
        if message:
            await message.edit(embed=embed, view=PublicTicketView(self))
        else:
            message = await channel.send(embed=embed, view=PublicTicketView(self))
            self.store.set_top_value(guild.id, "ticket_panel_message_id", message.id)
        return channel

    async def open_ticket(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(t('Tickets só funcionam no servidor Discord.'), ephemeral=True)
            return
        config = self.store.get_settings(guild.id)
        if interaction.message.id != config["ticket_panel_message_id"] or interaction.channel_id != config["channels"]["ticket_panel"]:
            await interaction.response.send_message(t('Este painel foi substituído. Use a mensagem atual de tickets.'), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with self._ticket_locks[(guild.id, interaction.user.id)]:
            existing = self.store.get_open_ticket(guild.id, interaction.user.id)
            if existing:
                channel = guild.get_channel(existing["channel_id"])
                if channel:
                    await interaction.followup.send(''.join((format(t('Você já tem um ticket aberto: '), ''), format(channel.mention, ''))), ephemeral=True)
                    return
                self.store.close_ticket(existing["channel_id"])
            category_id = config["ticket_category_id"]
            category = guild.get_channel(category_id) if category_id else None
            if category_id and not isinstance(category, discord.CategoryChannel):
                await interaction.followup.send(t('A categoria de tickets configurada não existe mais.'), ephemeral=True)
                return
            bot_member = guild.me or await guild.fetch_member(self.user.id)
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
            }
            role_id = config["admin_role_id"]
            role = guild.get_role(role_id) if role_id else None
            if role_id and role is None:
                await interaction.followup.send(t('O cargo da equipe configurado não existe mais.'), ephemeral=True)
                return
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            name = f"ticket-{interaction.user.id}"
            channel = None
            try:
                channel = await guild.create_text_channel(name, category=category, overwrites=overwrites, topic=''.join((format(t('HTS Bot Multi servers ticket • usuário '), ''), format(interaction.user.id, ''))), reason=''.join((format(t('Ticket aberto por '), ''), format(interaction.user.id, ''))))
                self.store.create_ticket(guild.id, interaction.user.id, channel.id)
                embed = discord.Embed(title=t('🎫 Seu ticket'), description=t('Este canal é privado para você e a equipe. Use o botão abaixo para enviar seu personagem e pedir a WL.'), color=0x28D17C)
                await channel.send(content=interaction.user.mention, embed=embed, view=TicketActionView(self), allowed_mentions=discord.AllowedMentions(users=True))
            except (discord.HTTPException, sqlite3.Error) as exc:
                if channel:
                    try:
                        await channel.delete(reason=t('Falha ao inicializar ticket'))
                    except discord.HTTPException:
                        logger.exception("Falha ao remover ticket incompleto %s", channel.id)
                    self.store.close_ticket(channel.id)
                await interaction.followup.send(''.join((format(t('Não foi possível abrir o ticket: '), ''), format(exc, ''))), ephemeral=True)
                return
            await interaction.followup.send(''.join((format(t('Ticket criado: '), ''), format(channel.mention, ''))), ephemeral=True)

    async def close_ticket(self, interaction: discord.Interaction) -> None:
        ticket = self.store.get_ticket(interaction.channel_id)
        if not ticket or ticket["status"] != "open":
            await interaction.response.send_message(t('Este canal não é um ticket aberto.'), ephemeral=True)
            return
        config = self.store.get_settings(ticket["guild_id"])
        if interaction.user.id != ticket["user_id"] and not _is_admin(interaction.user, config):
            await interaction.response.send_message(t('Somente o dono ou a equipe pode fechar o ticket.'), ephemeral=True)
            return
        request = self.store.get_request(interaction.channel_id)
        if request and request["status"] in ("review", "processing") and not _is_admin(interaction.user, config):
            await interaction.response.send_message(t('A WL ainda está em análise. Peça à equipe para fechar este ticket.'), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.channel.delete(reason=''.join((format(t('Ticket fechado por '), ''), format(interaction.user.id, ''))))
        except discord.HTTPException:
            await interaction.followup.send(t('Não consegui fechar o canal. Verifique minha permissão Gerenciar Canais.'), ephemeral=True)
            return
        if request and request["status"] != "approved":
            self.store.set_request_status(interaction.channel_id, "closed")
            self.store.clear_request_password(interaction.channel_id)
        self.store.close_ticket(interaction.channel_id)

    async def submit_whitelist(self, interaction, ticket, username, character, lore, password_input="") -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._large_io_slots:
            if isinstance(lore, Path):
                try:
                    lore = await asyncio.to_thread(load_draft, lore)
                except (OSError, ValueError) as exc:
                    await interaction.followup.send(t('O arquivo temporario da lore expirou. Envie o .txt novamente.'), ephemeral=True)
                    return
            await self._submit_whitelist_impl(interaction, ticket, username, character, lore, password_input)

    async def _submit_whitelist_impl(self, interaction: discord.Interaction, ticket: dict[str, Any], username: str, character: str, lore: str, password_input: str = "") -> None:
        if interaction.channel_id != ticket["channel_id"] or interaction.user.id != ticket["user_id"] or ticket["status"] != "open":
            await interaction.followup.send(t('Ticket inválido ou fechado.'), ephemeral=True)
            return
        if len(lore) > MAX_LORE_CHARS:
            await interaction.followup.send(''.join((format(t('A lore excede o limite de '), ''), format(MAX_LORE_CHARS, ''.join((',',))), format(t(' caracteres.'), ''))), ephemeral=True)
            return
        try:
            adduser_command(username, password_input or "A" * 12)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        config = self.store.get_settings(ticket["guild_id"])
        wl = config["wl"]
        if not wl["enabled"]:
            await interaction.followup.send(t('A criação de WL está pausada.'), ephemeral=True)
            return
        if self.store.username_owner(ticket["guild_id"], username):
            await interaction.followup.send(t('Este usuário PZ já recebeu WL por este bot.'), ephemeral=True)
            return
        existing = self.store.get_request(ticket["channel_id"])
        if existing and existing["status"] in ("review", "processing", "approved"):
            await interaction.followup.send(t('Já existe um pedido de WL em andamento neste ticket.'), ephemeral=True)
            return
        result = None
        if wl["require_lore"]:
            try:
                api_key = self.store.get_gemini_api_key(ticket["guild_id"])
            except ValueError as exc:
                logger.error("Chave Gemini da guild %s nao pode ser descriptografada: %s", ticket["guild_id"], exc)
                api_key = ""
                result = LoreResult(False, 0.0, (), t('A chave Gemini armazenada esta indisponivel; a equipe precisa revisar este pedido.'), source="gemini_error")
            if api_key:
                try:
                    result = await evaluate_lore_with_gemini(api_key, wl["lore_reference"], lore)
                except GeminiError as exc:
                    logger.warning("Gemini indisponivel para triagem da guild %s: %s", ticket["guild_id"], exc)
                    result = LoreResult(False, 0.0, (), ''.join((format(t('O Gemini nao conseguiu concluir a analise ('), ''), format(exc, ''), format(t('); revisao humana necessaria.'), ''))), source="gemini_error")
            elif result is None:
                result = await asyncio.to_thread(evaluate_lore, wl["lore_reference"], lore, float(wl["lore_min_score"]))
        auto_ready = bool(
            wl["auto_approve"]
            and _configured_private_channel(interaction.guild, config["channels"]["wl_audit"])
            and _configured_text_channel(interaction.guild, config["channels"]["wl_decisions"])
        )
        if not auto_ready:
            status = "review"
        elif result and result.source == "gemini_error":
            status = "review"
        elif result and not result.approved:
            status = "review" if result.decision == "review" or wl["review_failed_lore"] else "rejected"
        else:
            status = "pending"
        secret = getenv("WL_ENCRYPTION_KEY") or getenv("DISCORD_TOKEN", "")
        ciphertext = encrypt_password(password_input, secret) if password_input and status != "rejected" else ""
        await asyncio.to_thread(
            self.store.save_request,
            ticket["channel_id"], ticket["guild_id"], ticket["user_id"], username, character, lore,
            result.score if result else 1.0, status, ciphertext, result.to_payload() if result else None,
        )
        if status == "rejected":
            lore_id = config["channels"]["lore"]
            link = ''.join((format(t(' Leia a lore em <#'), ''), format(lore_id, ''), format(t('> e tente novamente.'), ''))) if lore_id else t(' Leia a lore oficial e tente novamente.')
            await interaction.followup.send(''.join((format(t('Sua história não passou na triagem inicial: '), ''), format(result.reason, ''), format(link, ''))), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            await interaction.channel.send(
                ''.join(('<@', format(ticket['user_id'], ''), format(t('>, sua WL foi recusada pela triagem: '), ''), format(result.reason, ''), format(link, ''))),
                allowed_mentions=discord.AllowedMentions(users=[interaction.user]),
            )
            rejected_request = self.store.get_request(ticket["channel_id"])
            await self.post_decision(interaction.guild, rejected_request, False, result.reason)
            await self.post_audit(interaction.guild, rejected_request, t('Reprovada automaticamente'), result.reason)
            return
        if status == "review":
            await interaction.followup.send(t('Sua história foi encaminhada para revisão da equipe.'), ephemeral=True)
            await self.post_review(interaction.channel, ticket, username, character, lore, result)
            await self.post_audit(interaction.guild, self.store.get_request(ticket["channel_id"]), t('Em revisão'), result.reason if result else t('Lore não exigida.'))
            return
        await self.post_review(interaction.channel, ticket, username, character, lore, result)
        feedback = await self.issue_whitelist(interaction.channel, ticket["channel_id"])
        updated_request = self.store.get_request(ticket["channel_id"])
        if updated_request["status"] == "approved":
            await self.post_decision(interaction.guild, updated_request, True, result.reason if result else t('Lore não exigida pela configuração.'))
        await self.post_audit(interaction.guild, updated_request, t('Aprovação automática') if updated_request["status"] == "approved" else t('Falha na criação automática'), (result.reason if result else t('Lore não exigida.')) if updated_request["status"] == "approved" else feedback)
        await interaction.followup.send(feedback, ephemeral=True)

    async def post_decision(self, guild: discord.Guild, request: dict[str, Any], approved: bool, reason: str) -> None:
        config = self.store.get_settings(guild.id)
        channel_id = config["channels"]["wl_decisions"]
        channel = _configured_text_channel(guild, channel_id)
        if not channel:
            logger.warning("Canal de decisões da WL ausente na guild %s", guild.id)
            return
        embed = discord.Embed(title=t('✅ WL aprovada') if approved else t('❌ WL reprovada'), color=0x28D17C if approved else 0xD84A4A)
        embed.add_field(name="Jogador", value=f"<@{request['user_id']}> • `{request['username']}`", inline=False)
        embed.add_field(name=t('Personagem'), value=discord.utils.escape_markdown(request["character_name"]), inline=False)
        embed.add_field(name=t('Motivo'), value=reason[:1000], inline=False)
        if config["wl"]["require_lore"]:
            triage = LoreResult.from_payload(request.get("triage")) or evaluate_lore(config["wl"]["lore_reference"], request.get("lore", ""), float(config["wl"]["lore_min_score"]))
            embed.add_field(name=t('Coerências encontradas'), value=", ".join(triage.common_terms[:15]) or t('Nenhuma'), inline=False)
            if triage.summary:
                embed.add_field(name=t('Resumo da análise'), value=triage.summary[:900], inline=False)
            if triage.contradictions:
                embed.add_field(name=t('Possíveis contradições'), value="\n".join(triage.contradictions)[:900], inline=False)
        embed.add_field(name=t('Ticket'), value=f"<#{request['channel_id']}>")
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.exception("Falha ao publicar decisão de WL na guild %s", guild.id)

    async def post_audit(self, guild: discord.Guild, request: dict[str, Any], decision: str, reason: str) -> None:
        config = self.store.get_settings(guild.id)
        channel_id = config["channels"]["wl_audit"]
        channel = _configured_private_channel(guild, channel_id)
        if not channel:
            logger.warning("Canal de auditoria da WL ausente na guild %s", guild.id)
            return
        reference = config["wl"]["lore_reference"]
        result = (LoreResult.from_payload(request.get("triage")) or evaluate_lore(reference, request["lore"], float(config["wl"]["lore_min_score"]))) if config["wl"]["require_lore"] else None
        embed = discord.Embed(title=''.join((format(t('📋 WL: '), ''), format(decision, ''))), color=0x28D17C if request["status"] == "approved" else 0xE8B44B)
        embed.add_field(name=t('Candidato'), value=f"<@{request['user_id']}> • `{request['username']}`", inline=False)
        embed.add_field(name=t('Personagem'), value=discord.utils.escape_markdown(request["character_name"]), inline=False)
        embed.add_field(name=t('Motivo'), value=reason[:900], inline=False)
        if result:
            embed.add_field(name=t('Coerências detectadas'), value=", ".join(result.common_terms[:20]) or t('Nenhuma'), inline=False)
            embed.add_field(name=t('Confiança da triagem'), value=f"{result.score:.0%} ({'Gemini' if result.source == 'gemini' else 'local'})")
            if result.summary:
                embed.add_field(name=t('Resumo da análise'), value=result.summary[:900], inline=False)
            if result.contradictions:
                embed.add_field(name=t('Contradições apontadas'), value="\n".join(result.contradictions)[:900], inline=False)
        embed.add_field(name=t('Resumo da lore enviada'), value=(request["lore"][:900] + "…") if len(request["lore"]) > 900 else request["lore"] or t('Não informada'), inline=False)
        embed.add_field(name=t('Ticket'), value=f"<#{request['channel_id']}>")
        lore_files: list[discord.File] = []
        lore = request.get("lore", "")
        if request.get("status") == "approved" and lore:
            try:
                lore_files = await asyncio.to_thread(
                    _make_lore_files,
                    lore,
                    request["username"],
                    getattr(guild, "filesize_limit", MAX_LORE_UPLOAD_BYTES),
                )
            except ValueError as exc:
                embed.add_field(name=t('Arquivo da lore'), value=''.join((format(t('Não foi possível anexar automaticamente: '), ''), format(exc, ''))), inline=False)
        elif request.get("status") == "approved":
            embed.add_field(name=t('Arquivo da lore'), value=t('Este pedido foi aprovado sem envio de uma lore.'), inline=False)
        audit_sent = False
        try:
            if lore_files:
                await channel.send(embed=embed, file=lore_files[0], allowed_mentions=discord.AllowedMentions.none())
            else:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            audit_sent = True
            for index, file in enumerate(lore_files[1:], start=2):
                await channel.send(
                    content=''.join((format(t('Continuação da lore de `'), ''), format(discord.utils.escape_markdown(request['username']), ''), format(t('`: parte '), ''), format(index, ''), format(t(' de '), ''), format(len(lore_files), ''), '.')),
                    file=file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException:
            logger.exception("Falha ao registrar auditoria de WL na guild %s", guild.id)
            for file in lore_files:
                file.close()
            if lore_files:
                note = t('O Discord recusou um dos anexos da lore. Use `/wl puxar_lore` para tentar baixar o arquivo novamente.')
                if audit_sent:
                    try:
                        await channel.send(note, allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException:
                        logger.exception("Falha ao informar erro no anexo de auditoria da WL na guild %s", guild.id)
                else:
                    embed.add_field(name=t('Arquivo da lore'), value=note, inline=False)
                    try:
                        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException:
                        logger.exception("Falha ao publicar auditoria de WL sem o arquivo na guild %s", guild.id)

    async def post_review(self, channel: discord.TextChannel, ticket: dict[str, Any], username: str, character: str, lore: str, result: Any) -> None:
        embed = discord.Embed(title=t('📋 Pedido de whitelist'), color=0xE8B44B if result and not result.approved else 0x28D17C)
        embed.add_field(name=t('Usuário PZ'), value=discord.utils.escape_markdown(username))
        embed.add_field(name=t('Personagem'), value=discord.utils.escape_markdown(character))
        embed.add_field(name=t('Triagem'), value=''.join((format(result.reason, ''), format(t(' (pontuação '), ''), format(result.score, ''.join(('.0%',))), ')')) if result else t('Lore não exigida'), inline=False)
        if result and result.decision:
            suggested = {"approve": t('Aprovar'), "review": t('Revisar manualmente'), "reject": t('Reprovar')}.get(result.decision, t('Revisar manualmente'))
            embed.add_field(name=t('Sugestão do Gemini'), value=suggested)
        if result and result.summary:
            embed.add_field(name=t('Resumo da análise'), value=result.summary[:900], inline=False)
        if result and result.contradictions:
            embed.add_field(name=t('Contradições possíveis'), value="\n".join(result.contradictions)[:900], inline=False)
        embed.add_field(name=t('História'), value=lore[:1000] or t('Não informada'), inline=False)
        role_id = self.store.get_settings(ticket["guild_id"])["admin_role_id"]
        mention = f"<@&{role_id}>" if role_id else ""
        await channel.send(content=mention or None, embed=embed, view=ReviewView(self), allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False))

    async def issue_whitelist(self, channel: discord.TextChannel, channel_id: int) -> str:
        async with self._wl_locks[channel_id]:
            request = self.store.get_request(channel_id)
            if not request or request["status"] not in ("pending", "review", "failed"):
                return t('Não há pedido pendente neste ticket.')
            if self.store.username_owner(request["guild_id"], request["username"]):
                return t('Este usuário PZ já recebeu WL por este bot.')
            config = self.store.get_settings(request["guild_id"])
            server = self.registry.get_server(config["wl"]["server_id"])
            missing = describe_missing_rcon(server)
            if missing:
                self.store.set_request_status(channel_id, "failed")
                return ''.join((format(t('RCON incompleto para este servidor: '), ''), format(', '.join(missing), ''), format(t('. Configure os segredos na VM.'), '')))
            secret = getenv("WL_ENCRYPTION_KEY") or getenv("DISCORD_TOKEN", "")
            try:
                password = decrypt_password(request["password_ciphertext"], secret) if request["password_ciphertext"] else generate_password()
            except (InvalidToken, ValueError):
                self.store.set_request_status(channel_id, "failed")
                return t('Não foi possível recuperar a senha informada. Peça ao jogador para reenviar o pedido.')
            command = adduser_command(request["username"], password)
            self.store.set_request_status(channel_id, "processing")
            result = await send_rcon_command(server["rcon"]["host"], int(server["rcon"]["port"]), server["rcon"]["password"], command)
            if not result.ok or any(word in result.output.casefold() for word in ("unknown command", "already exists", "error", "failed", "invalid", "já existe", "ja existe", "erro", "falha")):
                self.store.set_request_status(channel_id, "failed")
                detail = result.error or result.output or t('Resposta de falha do servidor.')
                await channel.send(''.join((format(t('A WL de `'), ''), format(request['username'], ''), format(t('` não foi confirmada pelo RCON. Equipe: verifique o servidor. Detalhe: '), ''), format(discord.utils.escape_markdown(detail[:500]), ''))))
                return t('Falha ao criar a WL. A equipe foi avisada neste ticket.')
            try:
                self.store.record_whitelist(request["guild_id"], request["username"], request["user_id"], channel_id)
                self.store.set_request_status(channel_id, "approved")
                self.store.clear_request_password(channel_id)
            except sqlite3.IntegrityError:
                self.store.set_request_status(channel_id, "failed")
                return t('O comando foi enviado, mas houve conflito no registro local. A equipe deve verificar a WL no PZ.')
            guild = channel.guild
            member = guild.get_member(request["user_id"])
            if member is None:
                try:
                    member = await guild.fetch_member(request["user_id"])
                except discord.HTTPException:
                    member = None
            credentials = ''.join((format(t('Sua WL foi aprovada!\nUsuário PZ: `'), ''), format(request['username'], ''), format(t('`\nSenha inicial: `'), ''), format(password, ''), format(t('`\nGuarde a senha e não a compartilhe.'), '')))
            delivered = False
            if member:
                try:
                    await member.send(credentials)
                    delivered = True
                except discord.HTTPException:
                    pass
            if not delivered:
                await channel.send(f"<@{request['user_id']}> {credentials}", allowed_mentions=discord.AllowedMentions(users=True))
            await channel.send(''.join((format(t('✅ WL criada para `'), ''), format(request['username'], ''), format(t('`. Credenciais enviadas '), ''), format(t('por DM') if delivered else t('neste ticket privado'), ''), '.')))
            return t('WL criada com sucesso. Confira as credenciais na DM ou neste ticket privado.')

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        logger.exception("Erro no evento Discord %s", event_method)
