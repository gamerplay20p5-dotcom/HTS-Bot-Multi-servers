# Registro técnico da migração para Python

O HTS Bot Multi servers tem runtime Python. A entrada de produção é `python -m manguadu --bot`; `discord.py` processa os comandos de aplicação, as views interativas e os eventos Discord. Não há dependência de Node/npm para executar, testar ou atualizar o bot.

O nome do pacote importável continua `manguadu` para preservar a estrutura do aplicativo e os comandos usados pela instalação. O nome de distribuição do projeto é `friendhost-pz-bot` e a identidade apresentada aos administradores e jogadores é **HTS Bot Multi servers**.

## Escopo portado

| Área | Implementação Python |
| --- | --- |
| Inicialização, comandos Discord e configuração interativa | `manguadu.__main__`, `manguadu.discord_bot`, `manguadu.config_store` |
| Servidores, credenciais e integração Pterodactyl | `manguadu.server_registry`, `manguadu.credentials`, `manguadu.pterodactyl` |
| RCON, controle local e operações do servidor | `manguadu.rcon`, `manguadu.server_integrations`, `manguadu.local_server_control` |
| FriendHost, arquivos de jogador e relatórios | `manguadu.friendhost`, `manguadu.pz_data`, `manguadu.player_data`, `manguadu.player_reports`, `manguadu.utils` |
| Mapas, GPS, descoberta de caminhos e wipes | `manguadu.pz_map_renderer`, `manguadu.pz_path_discovery`, `manguadu.pz_wipe` |
| Tickets, lore e whitelist com `adduser` | `manguadu.discord_bot`, `manguadu.lore`, `manguadu.credentials`, `manguadu.rcon` |
| Painéis, tema e vínculos jogador/Discord | `manguadu.panel_theme`, `manguadu.panel_player_links`, `manguadu.panel_reports` |
| Automações, alertas e observadores de logs | `manguadu.automation_scheduler`, `manguadu.anticheat_alerts`, `manguadu.watchers` |
| Atualizações na VM | `manguadu.bot_updater`, `scripts/update_bot.py` |

O corte inclui também empacotamento com `pyproject.toml`, testes `pytest`, atualização pelo módulo Python e configuração PM2 apontando para o interpretador do ambiente virtual. Fontes JavaScript, testes Node, manifests npm e `node_modules` foram removidos.

## Configuração e persistência

`/config_bot` configura canais, categoria, cargo e opções de WL por servidor Discord; esses dados ficam em `data/bot-config.sqlite3`. Segredos do Discord, Pterodactyl e RCON permanecem no `.env` local da VM. `BOT_GITHUB_TOKEN` só é necessário quando o repositório é privado.

Credenciais WL pendentes são cifradas. O bot usa a senha informada pelo jogador ou gera uma quando ela não é fornecida, envia `adduser` pelo RCON do servidor selecionado e entrega a senha por DM ou ticket privado. Decisões e motivos de aprovação/reprovação são publicadas no canal Discord configurado, nunca no chat dentro do PZ. O canal de auditoria registra a lore e o resumo da revisão; no modo automático, explica as coerências que fundamentaram a decisão.

Estados locais de automações, painéis e anticheat ficam em `data/`. O leitor do anticheat reconhece timestamps em milissegundos já gravados pelo runtime anterior para preservar a janela de cooldown existente.

## Instalação e operação

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m compileall -q manguadu scripts/update_bot.py
python -m pytest -q tests_py
python -m manguadu
```

`python -m manguadu` imprime diagnóstico local sem conectar ao Discord. `--probe` consulta Pterodactyl e `--bot` inicia a conexão Discord. Em produção, PM2 deve executar `.venv/bin/python -m manguadu --bot`; as instruções da branch de teste estão em [DEPLOY.md](DEPLOY.md).

## Validação da migração

Os testes Python cobrem descoberta FriendHost, persistência, slash commands e views, WL, RCON, API Pterodactyl, controle local, mapas, wipes, automações, anticheat, relatórios e watchers. Eles não acessam Discord, Pterodactyl ou servidores PZ reais.

```bash
python -m compileall -q manguadu scripts/update_bot.py
python -m pytest -q tests_py
```

Após integrar a branch em `main`, ajuste `BOT_UPDATE_BRANCH=main` na VM. A substituição do processo PM2 anterior e a preservação de `.env`/`data/` estão descritas em [DEPLOY.md](DEPLOY.md).
