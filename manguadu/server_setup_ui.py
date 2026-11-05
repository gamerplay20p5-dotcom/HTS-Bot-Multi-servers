"""Ephemeral, administrator-only Pterodactyl picker and integration diagnostics."""
from __future__ import annotations

import asyncio
import logging
import sqlite3

import discord

from .guild_ui import GuildView, GuildModal, is_admin
from .i18n import t
from .server_setup import ServerSetup, SetupError, check


def setup(bot):
    if not hasattr(bot, "server_setup"):
        bot.server_setup = ServerSetup(bot)
    return bot.server_setup


class SetupView(GuildView):
    require_binding = False

    def __init__(self, bot, guild_id, actor_id):
        super().__init__(timeout=600)
        self.bot, self.guild_id, self.actor_id = bot, guild_id, actor_id

    async def interaction_check(self, interaction):
        return interaction.guild_id == self.guild_id and interaction.user.id == self.actor_id and await super().interaction_check(interaction) and await is_admin(interaction, self.bot)

    async def on_error(self, interaction, error, item):
        if isinstance(error, (SetupError, ValueError, OSError, sqlite3.IntegrityError)):
            message = str(error) if isinstance(error, SetupError) else t("Operação não concluída. Confira permissões, volume e vínculo existente; nenhum arquivo do jogo foi alterado.")
        else:
            logging.getLogger(__name__).exception("Server setup failed", exc_info=error)
            message = t("Falha temporária na configuração. Tente novamente.")
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class SearchModal(GuildModal, title="Procurar servidor pelo nome"):
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.search = discord.ui.TextInput(label=t("Nome ou parte do nome"), required=False, max_length=100)
        self.add_item(self.search)

    async def on_submit(self, interaction):
        if not await self.view.interaction_check(interaction):
            return
        self.view.search = str(self.search).strip()
        self.view.page = 1
        await self.view.load(interaction, refresh=True)

    async def on_error(self, interaction, error):
        await self.view.on_error(interaction, error, None)


class AuthorizationModal(GuildModal, title="Autorizar comunidade Discord"):
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.guild = discord.ui.TextInput(label=t("ID do Discord autorizado"), default=str(view.guild_id), min_length=1, max_length=20)
        self.add_item(self.guild)

    async def on_submit(self, interaction):
        if not await self.view.interaction_check(interaction):
            return
        raw = str(self.guild).strip()
        if not raw.isdigit() or not 0 < int(raw) < 2**64:
            raise SetupError(t("Informe um ID de servidor Discord válido."))
        await interaction.response.defer(ephemeral=True, thinking=True)
        await setup(self.view.bot).authorize(interaction.user.id, int(raw), self.view.server)
        await interaction.followup.send(t("Autorização salva para o Discord: ") + raw + ". " + t("O administrador dessa comunidade já pode selecionar o servidor e concluir o vínculo automático."), ephemeral=True)

    async def on_error(self, interaction, error):
        await self.view.on_error(interaction, error, None)


class ServerPicker(SetupView):
    def __init__(self, bot, guild_id, actor_id):
        super().__init__(bot, guild_id, actor_id)
        self.page, self.search = 1, ""
        self.servers = []
        self.selector = discord.ui.Select(placeholder=t("Selecione um servidor autorizado"), row=0, options=[discord.SelectOption(label="—", value="none")], disabled=True)
        self.selector._hts_user_options = True
        self.selector.callback = self.choose
        self.add_item(self.selector)

    async def load(self, interaction, *, refresh=False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await setup(self.bot).catalog(self.actor_id, self.guild_id, page=self.page, search=self.search, refresh=refresh)
        self.servers = result["servers"]
        self.page = result["page"]
        self.previous.disabled = self.page <= 1
        self.next_page.disabled = self.page >= result["pages"]
        self.selector.options = [discord.SelectOption(label=item["label"][:100], value=item["uuid"], description=(item["identifier"] + " · " + (item["node"] or "?") + " · " + item["uuid"])[:100]) for item in self.servers] or [discord.SelectOption(label="—", value="none")]
        self.selector.disabled = not self.servers
        description = t("A lista contém apenas servidores permitidos. A chave global permanece no dedicado.")
        if await setup(self.bot).is_owner(self.actor_id):
            description += "\n" + t("Visão do proprietário do aplicativo: selecione um servidor e autorize explicitamente o Discord do cliente.")
        if not self.servers:
            description += "\n" + t("Nenhum servidor encontrado. Peça autorização ao proprietário do bot ou use o vínculo manual por arquivo.")
        embed = discord.Embed(title=t("Gerenciamento de servidores Pterodactyl"), description=description, color=0x28D17C)
        embed.set_footer(text=f"{self.page}/{result['pages']}")
        await interaction.edit_original_response(embed=embed, view=self, content=None)

    async def choose(self, interaction):
        server = next((item for item in self.servers if item["uuid"] == interaction.data["values"][0]), None)
        if not server:
            raise SetupError(t("Atualize a lista e selecione novamente."))
        await interaction.response.defer(ephemeral=True, thinking=True)
        view = SelectedServer(self.bot, self.guild_id, self.actor_id, server)
        view.owner = await setup(self.bot).is_owner(self.actor_id)
        if not view.owner and not setup(self.bot).authorized(self.guild_id, server["uuid"]):
            binding = self.bot.store.get_binding(self.guild_id)
            if not binding or binding["uuid"] != server["uuid"]:
                raise SetupError(t("Este servidor não está autorizado para este Discord."))
        view.authorize_button.disabled = not view.owner
        view.revoke_button.disabled = not view.owner
        await interaction.edit_original_response(embed=view.embed(), view=view)

    @discord.ui.button(label="Buscar por nome", row=1)
    async def search_button(self, interaction, button):
        await interaction.response.send_modal(SearchModal(self))

    @discord.ui.button(label="Anterior", row=1)
    async def previous(self, interaction, button):
        self.page = max(1, self.page - 1)
        await self.load(interaction)

    @discord.ui.button(label="Próxima", row=1)
    async def next_page(self, interaction, button):
        self.page += 1
        await self.load(interaction)

    @discord.ui.button(label="Atualizar lista", row=1)
    async def refresh(self, interaction, button):
        await self.load(interaction, refresh=True)


class SelectedServer(SetupView):
    def __init__(self, bot, guild_id, actor_id, server):
        super().__init__(bot, guild_id, actor_id)
        self.server, self.paths, self.lua = server, [], ""
        self.owner = False
        self.finish.disabled = True

    def embed(self):
        embed = discord.Embed(title=t("Servidor selecionado"), description=discord.utils.escape_markdown(self.server["label"]), color=0x28D17C)
        for key, value in (("Ptero ID", self.server["identifier"]), ("UUID", self.server["uuid"]), ("Node", self.server["node"] or "?"),
                           (t("Pasta Lua"), self.lua or t("Verifique o servidor")), (t("Autorização"), t("Permitido") if setup(self.bot).authorized(self.guild_id, self.server["uuid"]) else t("Aguardando proprietário do aplicativo"))):
            embed.add_field(name=key, value=str(value)[:1024], inline=False)
        embed.set_footer(text=t("Somente o arquivo temporário de vínculo poderá ser criado. Arquivos do jogo são preservados."))
        return embed

    @discord.ui.button(label="Verificar servidor", style=discord.ButtonStyle.primary, row=1)
    async def verify_server(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await setup(self.bot).inspect(self.guild_id, self.server, actor_id=self.actor_id)
        self.server, self.paths = result["server"], result["lua"]
        for child in list(self.children):
            if isinstance(child, discord.ui.Select):
                self.remove_item(child)
        self.lua = self.paths[0] if len(self.paths) == 1 else ""
        if len(self.paths) > 1:
            selector = discord.ui.Select(placeholder=t("Selecione a pasta Lua deste servidor"), options=[discord.SelectOption(label=p[:100], value=p) for p in self.paths], row=0)
            selector.callback = self.choose_lua
            self.add_item(selector)
        self.finish.disabled = not self.lua
        await interaction.edit_original_response(embed=self.embed(), view=self)

    async def choose_lua(self, interaction):
        value = interaction.data["values"][0]
        if value not in self.paths:
            raise SetupError(t("Selecione uma pasta Lua detectada neste volume."))
        self.lua = value
        self.finish.disabled = False
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Autorizar Discord", row=1)
    async def authorize_button(self, interaction, button):
        if not await setup(self.bot).is_owner(interaction.user.id):
            raise SetupError(t("Somente o proprietário do aplicativo pode autorizar servidores para uma comunidade."))
        await interaction.response.send_modal(AuthorizationModal(self))

    @discord.ui.button(label="Revogar autorização", row=2)
    async def revoke_button(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await setup(self.bot).authorize(interaction.user.id, self.guild_id, self.server, revoke=True)
        await interaction.followup.send(t("Autorização automática revogada. O vínculo instalado foi preservado."), ephemeral=True)

    @discord.ui.button(label="Concluir vínculo automático", style=discord.ButtonStyle.success, row=1)
    async def finish(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await setup(self.bot).auto_bind(interaction.user.id, self.guild_id, self.server, self.lua)
        from .guild_ui import ServerConfigView
        view = ServerConfigView(self.bot, self.guild_id)
        await interaction.edit_original_response(content=t("Vínculo validado. Agora configure os canais em /config_bot."), embed=view.embed(), view=view)


class ProviderView(SetupView):
    def __init__(self, bot, guild_id, actor_id):
        super().__init__(bot, guild_id, actor_id)
        select = discord.ui.Select(placeholder=t("Provedor de estatísticas"), options=[
            discord.SelectOption(label=t("Automático (prioriza FriendHost)"), value="auto"),
            discord.SelectOption(label="FriendHost", value="friendhost"), discord.SelectOption(label="TTF Stats MP", value="ttf")])
        select.callback = self.choose
        self.add_item(select)

    async def choose(self, interaction):
        selected = interaction.data["values"][0]
        binding = self.bot.store.get_binding(self.guild_id)
        if not binding or selected not in {"auto", "friendhost", "ttf"}:
            raise SetupError(t("Valide o vínculo primeiro."))
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.install_binding(self.guild_id, {**binding, "stats_provider": selected})
        await interaction.followup.send(t("Provedor salvo. Fontes distintas nunca são somadas; confira o diagnóstico."), ephemeral=True)


async def diagnose(bot, guild_id):
    binding = bot.store.get_binding(guild_id)
    if not binding:
        return discord.Embed(title=t("Diagnóstico da integração"), description=t("Valide o vínculo primeiro."))
    lines = [discord.utils.escape_markdown(binding["label"]), "UUID: " + binding["uuid"]]
    try:
        async with setup(bot).client(binding["identifier"]) as client:
            info = check(await client.request(""))
            summary = client.server_summary(info.get("json") or {})
            if summary["uuid"] != binding["uuid"]:
                raise SetupError(t("O ID e o UUID retornados pela API não correspondem ao servidor selecionado."))
        lines.append(t("Pterodactyl: conectado"))
    except (ValueError, OSError) as exc:
        lines.append(str(exc) if isinstance(exc, SetupError) else t("API Pterodactyl indisponível."))
    try:
        from .guild_runtime import resolve_volume
        volume, lua = await asyncio.to_thread(resolve_volume, binding)
        lines += [t("Volume Wings: encontrado"), t("Pasta Lua: ") + binding["lua"]]
        runtime = bot._runtimes.get(guild_id)
        if runtime is None:
            await bot.install_binding(guild_id, binding)
            runtime = bot._runtimes[guild_id]
        info = await asyncio.to_thread(runtime.data.diagnostics)
        lines += [t("Fonte: ") + {"ttf": "TTF Stats MP", "friendhost": "FriendHost", "none": t("Nenhuma compatível")}[info["provider"]],
                  t("Contas: ") + str(info["accounts"]), t("Personagens: ") + str(info["characters"] if info["characters"] is not None else t("Indisponível")),
                  t("Registros inválidos: ") + str(info["invalid"]), t("Excluídos / cache anterior: ") + f"{info['deleted']} / {info['stale']}",
                  t("Métricas disponíveis: ") + (", ".join(info["metrics"]) or t("Indisponível")), t("Fontes detectadas: ") + (", ".join(info["available"]) or "—")]
        if info["updated"]:
            lines.append(t("Última leitura: ") + f"<t:{int(info['updated'])}:R>")
        if info["error"]:
            lines.append(t("Falha de leitura: ") + info["error"])
    except (ValueError, OSError) as exc:
        lines.append(t("Leitura local indisponível: verifique o node Wings, a pasta Lua e as permissões do bot."))
    return discord.Embed(title=t("Diagnóstico da integração"), description="\n".join(lines)[:4000], color=0x28D17C)
