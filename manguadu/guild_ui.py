"""Guild setup and public Discord hooks that establish task-local isolation."""
from __future__ import annotations

from .i18n import t

import secrets
import sqlite3
import time

import discord
from discord import app_commands
from .i18n import localize_component

from .guild_runtime import normalize_binding
from .pterodactyl import PterodactylClient

async def enter_guild(interaction, *, required: bool) -> bool:
    scope = interaction.client.activate_guild(interaction.guild_id)
    if scope is not None and (not required or scope.server is not None):
        return True
    if interaction.type == discord.InteractionType.autocomplete:
        await interaction.response.autocomplete([])
    else:
        await interaction.response.send_message(t('Configure e valide o servidor PZ deste Discord com `/config` antes de usar esta função.'), ephemeral=True)
    return False

class GuildCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        root = (interaction.data or {}).get("name", "")
        options = (interaction.data or {}).get("options", [])
        safe = root in {"config", "condig", "language", "config_bot", "config_api", "config_lore", "bot"}
        safe = safe or (root == "wl" and any(item.get("name") == "puxar_lore" for item in options))
        return await enter_guild(interaction, required=not safe)

class GuildView(discord.ui.View):
    require_binding = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for item in self.children:
            localize_component(item)

    def add_item(self, item):
        return super().add_item(localize_component(item))

    async def interaction_check(self, interaction):
        return await enter_guild(interaction, required=self.require_binding)

class GuildModal(discord.ui.Modal):
    require_binding = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.title = t(self.title)

    def add_item(self, item):
        return super().add_item(localize_component(item))

    async def interaction_check(self, interaction):
        return await enter_guild(interaction, required=self.require_binding)

async def is_admin(interaction, bot) -> bool:
    from .discord_bot import _admin_guard
    return await _admin_guard(interaction, bot.store)

class LanguageView(GuildView):
    require_binding = False

    def __init__(self, bot, guild_id):
        super().__init__(timeout=600)
        self.bot, self.guild_id = bot, guild_id
        chosen = bot.store.get_language(guild_id)
        select = discord.ui.Select(placeholder="Idioma / Language", options=[
            discord.SelectOption(label="Português (Brasil)", value="pt-BR", default=chosen == "pt-BR"),
            discord.SelectOption(label="English", value="en", default=chosen == "en"),
        ])
        select.callback = self.choose
        self.add_item(select)

    def embed(self):
        selected = self.bot.store.get_language(self.guild_id)
        return discord.Embed(
            title="Idioma / Language",
            description=("Idioma desta comunidade: **Português (Brasil)**. Novas mensagens, formulários e painéis usarão português. "
                         "O menu de comandos segue o idioma do aplicativo Discord de cada usuário."
                         if selected == "pt-BR" else
                         "Community language: **English**. New messages, forms and panels will use English. "
                         "The slash-command menu follows each user's Discord application language."),
            color=0x28D17C,
        )

    async def interaction_check(self, interaction):
        return await super().interaction_check(interaction) and await is_admin(interaction, self.bot)

    async def choose(self, interaction):
        # Explicit guard also covers direct callback invocation and stale views.
        if not await is_admin(interaction, self.bot):
            return
        selected = interaction.data["values"][0]
        if selected not in {"pt-BR", "en"}:
            await interaction.response.send_message("Invalid language / Idioma inválido.", ephemeral=True)
            return
        self.bot.store.set_language(self.guild_id, selected)
        self.bot.activate_guild(self.guild_id)
        fresh = LanguageView(self.bot, self.guild_id)
        await interaction.response.edit_message(embed=fresh.embed(), view=fresh)
        self.bot.work_queue.trigger(f"{self.guild_id}:panels")
        settings = self.bot.store.get_settings(self.guild_id)
        if settings.get("ticket_panel_message_id") and self.bot.scope_for(self.guild_id).server:
            try:
                await self.bot.publish_ticket_panel(interaction.guild)
            except (discord.HTTPException, ValueError):
                await interaction.followup.send(
                    "Language saved. Republish the ticket panel after checking channel permissions."
                    if selected == "en" else "Idioma salvo. Confira as permissões e republique o painel de tickets.",
                    ephemeral=True,
                )

    @discord.ui.button(label="Config / Voltar", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        fresh = ServerConfigView(self.bot, self.guild_id)
        await interaction.response.edit_message(embed=fresh.embed(), view=fresh)

class ServerBindingModal(GuildModal, title='Vincular este Discord ao servidor PZ'):
    def __init__(self, bot, guild_id):
        super().__init__()
        self.bot, self.guild_id = bot, guild_id
        current = bot.store.get_binding(guild_id) or {}
        self.identifier = discord.ui.TextInput(label=t('ID Pterodactyl (8 caracteres)'), default=current.get("identifier", ""), max_length=36)
        self.uuid = discord.ui.TextInput(label=t('UUID completo do volume Wings'), default=current.get("uuid", ""), max_length=36)
        self.label = discord.ui.TextInput(label=t('Nome do servidor'), default=current.get("label", ""), max_length=100)
        self.lua = discord.ui.TextInput(label=t('Pasta Lua relativa ao volume'), default=current.get("lua", ".cache/Lua"), max_length=180)
        self.address = discord.ui.TextInput(label=t('Endereço de conexão dos jogadores (opcional)'), default=current.get("connect_address", ""), required=False, max_length=150)
        for item in (self.identifier, self.uuid, self.label, self.lua, self.address):
            self.add_item(item)

    async def on_submit(self, interaction):
        if not await is_admin(interaction, self.bot):
            return
        try:
            binding = normalize_binding(str(self.identifier), str(self.uuid), str(self.label), str(self.lua), str(self.address))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        pending = {"binding": binding, "token": f"friendhost:{self.guild_id}:{secrets.token_urlsafe(24)}", "expires": time.time() + 900}
        self.bot.store.pending_binding(self.guild_id, pending)
        await interaction.response.send_message(
            ''.join((format(t('No gerenciador de arquivos deste servidor no Pterodactyl, crie na raiz o arquivo `friendhost-discord-link.txt` com este conteúdo:\n```\n'), ''), format(pending['token'], ''), format(t('\n```\nDepois clique em **Validar vínculo**. Código válido por 15 minutos. Essa verificação comprova acesso ao servidor antes de associá-lo a este Discord.'), ''))),
            view=ServerConfigView(self.bot, self.guild_id), ephemeral=True,
        )

class RconEndpointModal(GuildModal, title='Endpoint RCON deste servidor'):
    def __init__(self, bot, guild_id):
        super().__init__()
        self.bot, self.guild_id = bot, guild_id
        binding = bot.store.get_binding(guild_id) or {}
        self.host = discord.ui.TextInput(label=t('Host publicado do RCON'), default=binding.get("rcon_host", "127.0.0.1"), max_length=200)
        self.port = discord.ui.TextInput(label=t('Porta TCP publicada'), default=binding.get("rcon_port", ""), max_length=5)
        self.config_name = discord.ui.TextInput(label=t('Nome do INI (se existir mais de um)'), default=binding.get("pz_config", ""), required=False, max_length=80)
        self.add_item(self.host)
        self.add_item(self.port)
        self.add_item(self.config_name)

    async def on_submit(self, interaction):
        if not await is_admin(interaction, self.bot):
            return
        binding = self.bot.store.get_binding(self.guild_id)
        if not binding:
            await interaction.response.send_message(t('Valide o vínculo primeiro.'), ephemeral=True)
            return
        if not str(self.port).isdigit() or not 1 <= int(str(self.port)) <= 65535:
            await interaction.response.send_message(t('Informe uma porta TCP de 1 a 65535.'), ephemeral=True)
            return
        binding.update(rcon_host=str(self.host).strip(), rcon_port=str(self.port), pz_config=str(self.config_name).strip())
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.install_binding(self.guild_id, binding)
        except (ValueError, OSError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(t('Endpoint salvo. A senha é lida somente do INI desse servidor PZ e nunca aparece neste painel.'), ephemeral=True)

class ServerConfigView(GuildView):
    require_binding = False

    def __init__(self, bot, guild_id):
        super().__init__(timeout=900)
        self.bot, self.guild_id = bot, guild_id

    def embed(self):
        binding = self.bot.store.get_binding(self.guild_id)
        description = t('Procure o servidor pelo nome. O proprietário do bot autoriza a comunidade; depois o vínculo pode ser concluído aqui, sem terminal. O cadastro manual por arquivo continua disponível.')
        if binding:
            description += ''.join((format(t('\n\nServidor: **'), ''), format(discord.utils.escape_markdown(binding['label']), ''), '**\nID: `', format(binding['identifier'], ''), '`\nVolume: `', format(binding['uuid'], ''), '`'))
            description += t('\n\nRankings/status: a cada 10 minutos, em fila e com horários distribuídos. Logs são acompanhados separadamente.')
            runtime = self.bot._runtimes.get(self.guild_id)
            if runtime:
                backlog = sum(runtime.watcher.backlog_bytes.values())
                description += ''.join((format(t('\nAtraso de leitura dos logs: '), ''), format(backlog, ''.join((',',))), format(t(' bytes pendentes.'), '')))
                source = runtime.data._stats_discovery[1][0]
                description += "\n" + t('Fonte: ') + {"ttf": "TTF Stats MP", "friendhost": "FriendHost", "none": t('Nenhuma compatível')}[source]
            else:
                description += t('\nLeitura local indisponível: confira o volume e as permissões no dedicado.')
        else:
            description += t('\n\nEste Discord ainda não tem um servidor PZ validado.')
        return discord.Embed(title=t('Configuração do servidor deste Discord'), description=description, color=0x28D17C)

    async def interaction_check(self, interaction):
        return await super().interaction_check(interaction) and await is_admin(interaction, self.bot)

    @discord.ui.button(label="Idioma / Language", style=discord.ButtonStyle.secondary, row=2)
    async def choose_language(self, interaction, button):
        fresh = LanguageView(self.bot, self.guild_id)
        await interaction.response.edit_message(embed=fresh.embed(), view=fresh)

    @discord.ui.button(label='Procurar servidores', style=discord.ButtonStyle.primary, row=3)
    async def discover_servers(self, interaction, button):
        from .server_setup_ui import ServerPicker
        await ServerPicker(self.bot, self.guild_id, interaction.user.id).load(interaction)

    @discord.ui.button(label='Diagnosticar integração', row=3)
    async def diagnose(self, interaction, button):
        from .server_setup_ui import diagnose
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(embed=await diagnose(self.bot, self.guild_id), ephemeral=True)

    @discord.ui.button(label='Provedor de estatísticas', row=3)
    async def stats_provider(self, interaction, button):
        from .server_setup_ui import ProviderView
        await interaction.response.send_message(t('Escolha a fonte de estatísticas deste servidor.'), view=ProviderView(self.bot, self.guild_id, interaction.user.id), ephemeral=True)

    @discord.ui.button(label='Atualizar leitura do vínculo', row=4)
    async def reload_binding(self, interaction, button):
        binding = self.bot.store.get_binding(self.guild_id)
        if not binding:
            await interaction.response.send_message(t('Valide o vínculo primeiro.'), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.install_binding(self.guild_id, binding)
            await interaction.edit_original_response(embed=self.embed(), view=self)
        except (OSError, ValueError):
            await interaction.followup.send(t('Leitura local indisponível: verifique o node Wings, a pasta Lua e as permissões do bot.'), ephemeral=True)

    @discord.ui.button(label='Cadastrar / editar servidor', style=discord.ButtonStyle.primary)
    async def configure(self, interaction, button):
        await interaction.response.send_modal(ServerBindingModal(self.bot, self.guild_id))

    @discord.ui.button(label='Validar vínculo', style=discord.ButtonStyle.success)
    async def verify(self, interaction, button):
        pending = self.bot.store.pending_binding(self.guild_id)
        if not pending or pending["expires"] < time.time():
            await interaction.response.send_message(t('Gere um novo cadastro; o código anterior está ausente ou expirou.'), ephemeral=True)
            return
        if pending.get("automatic"):
            from .server_setup_ui import setup
            if not setup(self.bot).authorized(self.guild_id, pending["binding"]["uuid"]):
                await interaction.response.send_message(t('A autorização foi revogada antes da conclusão.'), ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        import os
        binding = pending["binding"]
        server = {"ptero": {"url": os.getenv("PTERO_URL", ""), "apiKey": os.getenv("PTERO_API_KEY", ""), "serverId": binding["identifier"]}}
        async with PterodactylClient(server, session=self.bot._ptero_session, limiter=self.bot._ptero_limiter) as client:
            info = await client.request("")
            proof = await client.read_file("/friendhost-discord-link.txt") if info.get("ok") else {}
        attributes = (info.get("json") or {}).get("attributes", {})
        if not info.get("ok") or attributes.get("uuid", "").lower() != binding["uuid"]:
            await interaction.followup.send(t('Não foi possível confirmar esse ID/UUID na API compartilhada. Confira os IDs e o acesso da chave ao servidor.'), ephemeral=True)
            return
        if not proof.get("ok") or proof.get("text", "").strip() != pending["token"]:
            await interaction.followup.send(t('O arquivo de confirmação não contém o código esperado. Confira o arquivo na raiz do servidor escolhido.'), ephemeral=True)
            return
        try:
            await self.bot.install_binding(self.guild_id, binding)
        except sqlite3.IntegrityError:
            await interaction.followup.send(t('Esse volume já está vinculado a outro Discord. O operador do bot precisa revisar o vínculo existente.'), ephemeral=True)
            return
        except (ValueError, OSError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.edit_original_response(content=t('Vínculo validado. Agora configure os canais em /config_bot.'), embed=self.embed(), view=self)

    @discord.ui.button(label='RCON publicado', style=discord.ButtonStyle.secondary, row=1)
    async def rcon(self, interaction, button):
        await interaction.response.send_modal(RconEndpointModal(self.bot, self.guild_id))

    @discord.ui.button(label='Canais, tickets e WL', style=discord.ButtonStyle.secondary, row=1)
    async def channels(self, interaction, button):
        from .discord_bot import ConfigHomeView, _home_embed
        await interaction.response.edit_message(embed=_home_embed(self.bot.store.get_settings(self.guild_id)), view=ConfigHomeView(self.bot, self.guild_id))
