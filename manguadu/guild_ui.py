"""Guild setup and public Discord hooks that establish task-local isolation."""
from __future__ import annotations

import secrets
import sqlite3
import time

import discord
from discord import app_commands

from .guild_runtime import normalize_binding
from .pterodactyl import PterodactylClient

async def enter_guild(interaction, *, required: bool) -> bool:
    scope = interaction.client.activate_guild(interaction.guild_id)
    if scope is not None and (not required or scope.server is not None):
        return True
    if interaction.type == discord.InteractionType.autocomplete:
        await interaction.response.autocomplete([])
    else:
        await interaction.response.send_message("Configure e valide o servidor PZ deste Discord com `/config` antes de usar esta função.", ephemeral=True)
    return False

class GuildCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        root = (interaction.data or {}).get("name", "")
        options = (interaction.data or {}).get("options", [])
        safe = root in {"config", "condig", "config_bot", "config_api", "config_lore", "bot"}
        safe = safe or (root == "wl" and any(item.get("name") == "puxar_lore" for item in options))
        return await enter_guild(interaction, required=not safe)

class GuildView(discord.ui.View):
    require_binding = True

    async def interaction_check(self, interaction):
        return await enter_guild(interaction, required=self.require_binding)

class GuildModal(discord.ui.Modal):
    require_binding = False

    async def interaction_check(self, interaction):
        return await enter_guild(interaction, required=self.require_binding)

async def is_admin(interaction, bot) -> bool:
    from .discord_bot import _admin_guard
    return await _admin_guard(interaction, bot.store)

class ServerBindingModal(GuildModal, title="Vincular este Discord ao servidor PZ"):
    def __init__(self, bot, guild_id):
        super().__init__()
        self.bot, self.guild_id = bot, guild_id
        current = bot.store.get_binding(guild_id) or {}
        self.identifier = discord.ui.TextInput(label="ID Pterodactyl (8 caracteres)", default=current.get("identifier", ""), max_length=36)
        self.uuid = discord.ui.TextInput(label="UUID completo do volume Wings", default=current.get("uuid", ""), max_length=36)
        self.label = discord.ui.TextInput(label="Nome do servidor", default=current.get("label", ""), max_length=100)
        self.lua = discord.ui.TextInput(label="Pasta Lua relativa ao volume", default=current.get("lua", ".cache/Lua"), max_length=180)
        self.address = discord.ui.TextInput(label="Endereço de conexão dos jogadores (opcional)", default=current.get("connect_address", ""), required=False, max_length=150)
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
            "No gerenciador de arquivos deste servidor no Pterodactyl, crie na raiz o arquivo "
            "`friendhost-discord-link.txt` com este conteúdo:\n"
            f"```\n{pending['token']}\n```\nDepois clique em **Validar vínculo**. Código válido por 15 minutos. "
            "Essa verificação comprova acesso ao servidor antes de associá-lo a este Discord.",
            view=ServerConfigView(self.bot, self.guild_id), ephemeral=True,
        )

class RconEndpointModal(GuildModal, title="Endpoint RCON deste servidor"):
    def __init__(self, bot, guild_id):
        super().__init__()
        self.bot, self.guild_id = bot, guild_id
        binding = bot.store.get_binding(guild_id) or {}
        self.host = discord.ui.TextInput(label="Host publicado do RCON", default=binding.get("rcon_host", "127.0.0.1"), max_length=200)
        self.port = discord.ui.TextInput(label="Porta TCP publicada", default=binding.get("rcon_port", ""), max_length=5)
        self.config_name = discord.ui.TextInput(label="Nome do INI (se existir mais de um)", default=binding.get("pz_config", ""), required=False, max_length=80)
        self.add_item(self.host)
        self.add_item(self.port)
        self.add_item(self.config_name)

    async def on_submit(self, interaction):
        if not await is_admin(interaction, self.bot):
            return
        binding = self.bot.store.get_binding(self.guild_id)
        if not binding:
            await interaction.response.send_message("Valide o vínculo primeiro.", ephemeral=True)
            return
        if not str(self.port).isdigit() or not 1 <= int(str(self.port)) <= 65535:
            await interaction.response.send_message("Informe uma porta TCP de 1 a 65535.", ephemeral=True)
            return
        binding.update(rcon_host=str(self.host).strip(), rcon_port=str(self.port), pz_config=str(self.config_name).strip())
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.install_binding(self.guild_id, binding)
        except (ValueError, OSError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send("Endpoint salvo. A senha é lida somente do INI desse servidor PZ e nunca aparece neste painel.", ephemeral=True)

class ServerConfigView(GuildView):
    require_binding = False

    def __init__(self, bot, guild_id):
        super().__init__(timeout=900)
        self.bot, self.guild_id = bot, guild_id

    def embed(self):
        binding = self.bot.store.get_binding(self.guild_id)
        description = "Cadastre o ID Pterodactyl e o UUID do volume Wings. A chave da API é compartilhada e fica apenas no .env do dedicado."
        if binding:
            description += f"\n\nServidor: **{discord.utils.escape_markdown(binding['label'])}**\nID: `{binding['identifier']}`\nVolume: `{binding['uuid']}`"
            description += "\n\nRankings/status: a cada 10 minutos, em fila e com horários distribuídos. Logs são acompanhados separadamente."
            runtime = self.bot._runtimes.get(self.guild_id)
            if runtime:
                backlog = sum(runtime.watcher.backlog_bytes.values())
                description += f"\nAtraso de leitura dos logs: {backlog:,} bytes pendentes."
                if not runtime.data.base_path.is_dir():
                    description += "\nFriendHost_Data ainda não está disponível neste volume."
            else:
                description += "\nLeitura local indisponível: confira o volume e as permissões no dedicado."
        else:
            description += "\n\nEste Discord ainda não tem um servidor PZ validado."
        return discord.Embed(title="Configuração do servidor deste Discord", description=description, color=0x28D17C)

    async def interaction_check(self, interaction):
        return await super().interaction_check(interaction) and await is_admin(interaction, self.bot)

    @discord.ui.button(label="Cadastrar / editar servidor", style=discord.ButtonStyle.primary)
    async def configure(self, interaction, button):
        await interaction.response.send_modal(ServerBindingModal(self.bot, self.guild_id))

    @discord.ui.button(label="Validar vínculo", style=discord.ButtonStyle.success)
    async def verify(self, interaction, button):
        pending = self.bot.store.pending_binding(self.guild_id)
        if not pending or pending["expires"] < time.time():
            await interaction.response.send_message("Gere um novo cadastro; o código anterior está ausente ou expirou.", ephemeral=True)
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
            await interaction.followup.send("Não foi possível confirmar esse ID/UUID na API compartilhada. Confira os IDs e o acesso da chave ao servidor.", ephemeral=True)
            return
        if not proof.get("ok") or proof.get("text", "").strip() != pending["token"]:
            await interaction.followup.send("O arquivo de confirmação não contém o código esperado. Confira o arquivo na raiz do servidor escolhido.", ephemeral=True)
            return
        try:
            await self.bot.install_binding(self.guild_id, binding)
        except sqlite3.IntegrityError:
            await interaction.followup.send("Esse volume já está vinculado a outro Discord. O operador do bot precisa revisar o vínculo existente.", ephemeral=True)
            return
        except (ValueError, OSError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.edit_original_response(content="Vínculo validado. Agora configure os canais em /config_bot.", embed=self.embed(), view=self)

    @discord.ui.button(label="RCON publicado", style=discord.ButtonStyle.secondary, row=1)
    async def rcon(self, interaction, button):
        await interaction.response.send_modal(RconEndpointModal(self.bot, self.guild_id))

    @discord.ui.button(label="Canais, tickets e WL", style=discord.ButtonStyle.secondary, row=1)
    async def channels(self, interaction, button):
        from .discord_bot import ConfigHomeView, _home_embed
        await interaction.response.edit_message(embed=_home_embed(self.bot.store.get_settings(self.guild_id)), view=ConfigHomeView(self.bot, self.guild_id))
