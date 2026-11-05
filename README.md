# HTS Bot Multi servers

**Criado por / Created by Malaio/Erick** · Python 3.11+ · Project Zomboid · Discord · Pterodactyl/Wings

[Português: guia abaixo](#o-que-o-bot-faz) · [English: full operations guide](docs/OPERATIONS.en.md) · [Comandos](PZ_COMMANDS.md) · [Deploy](DEPLOY.md)

Uma instância Python atende vários Discords com um servidor PZ por guild, dados isolados e uma chave Pterodactyl compartilhada. / One Python instance serves multiple Discord communities with one PZ server per guild, isolated data and a shared Pterodactyl API key.

Bot Discord em Python para administrar comunidades de **Project Zomboid**, com integrações para o mod FriendHost, RCON e Pterodactyl. O projeto reúne ferramentas para jogadores, atendimento de whitelist, operação do servidor e automações em uma interface de slash commands e mensagens interativas do Discord.

O pacote Python ainda se chama `manguadu` para preservar o comando de inicialização e a estrutura interna do projeto. O nome público do projeto é **HTS Bot Multi servers**.

## O que o bot faz

- **Comunidades PT/EN:** `/language` ou **Idioma / Language** em `/config` e `/config_bot` salva português ou inglês por Discord. Painéis, formulários, respostas e avisos novos seguem o idioma escolhido. O menu de slash commands usa o idioma do aplicativo Discord do usuário, com nomes, descrições, opções e parâmetros localizados. Não há tradução por IA a cada mensagem.

- **Configuração no Discord:** `/config_bot` abre uma interface administrativa por servidor Discord para canais, equipe, categoria de tickets e regras da WL. `/config_api` recebe e testa a chave Gemini por um painel privado. A chave Pterodactyl fica no dedicado; a senha RCON vem do INI do volume vinculado.
- **Tickets e whitelist:** cria canais privados, coleta personagem, senha opcional e lore, permite revisão manual ou triagem automática e executa `adduser` via RCON. Senhas ausentes são geradas pelo bot. Aprovações e reprovações, com motivos, são publicadas no canal de decisões configurado; o resultado não é enviado ao chat do jogo. Credenciais são entregues por DM ou no ticket privado.
- **Controle RP por voz:** `/config_kick_automatico` exige uma call Discord configurada para jogadores online. A cada 10 segundos, compara `/players` do PZ com os vínculos de contas; quem ficar fora da call por 60 segundos recebe kick com motivo e registro no canal administrativo.
- **Validação de lore:** com Gemini configurado, avalia coerência narrativa, cronologia e contradições, produzindo resumo e evidências para a equipe. Sem chave, usa a triagem lexical local. Textos longos podem ser importados em `.txt` até 9.999.999 caracteres; ficam comprimidos no SQLite e apenas trechos limitados são enviados ao modelo.
- **Administração PZ:** consulta jogadores, ficha, skills, traits, safehouses, arquivos e logs; oferece operações administrativas, RCON, controle do servidor, backups e ferramentas de wipe com verificações de segurança.
- **FriendHost e anticheat:** lê arquivos de dados do mod, produz status e rankings, acompanha evoluções e mortes, espelha eventos de chat/login/logout e encaminha alertas do PZAntiCheat.
- **TTF Stats MP e descoberta:** `/config` busca servidores Pterodactyl pelo nome, detecta ID/UUID e pasta Lua e conclui o vínculo por arquivo quando o proprietário do aplicativo autorizou a comunidade. O provedor TTF lê personagens vivos/mortos e rankings históricos por conta ou personagem; fontes diferentes nunca são somadas. [Guia PT/EN de configuração, métricas e segurança](docs/SERVER_SETUP.pt-en.md).
- **Rankings configuráveis:** `/config` → **Rankings publicados** seleciona as tabelas por comunidade. O padrão compacto publica abates e sobrevivência. Há ranking opcional de habilidades no nível 10 pelo FriendHost, números inteiros e diagnóstico dos registros TTF inválidos. A leitura TTF aceita diretórios Linux com ACL apenas de travessia usando `O_PATH`. [Detalhes e testes PT/EN](docs/RANKINGS_AND_TTF_ACL.pt-en.md).
- **Painéis e rotinas:** monta painéis de status e ranking, relaciona nomes PZ a membros Discord, personaliza imagens/temas e agenda tarefas de servidor.
- **Operação e atualização:** resolve os caminhos dentro do volume Wings validado e atualiza o checkout Python pelo comando `/bot atualizar` ou pelo agendador opcional. O grupo `/bot` é restrito ao proprietário do aplicativo Discord porque afeta a instância compartilhada.

## Requisitos

- Python 3.11 ou superior.
- Uma aplicação de bot criada no [Discord Developer Portal](https://discord.com/developers/applications), com `DISCORD_TOKEN`.
- Pterodactyl para validação/controle e volumes Wings acessíveis localmente. A URL e a chave compartilhada ficam no `.env`; os endpoints RCON são configurados por Discord, com senha lida do INI do servidor.
- PM2 é opcional e serve somente para manter o processo Python ativo e reiniciá-lo.
- `ffmpeg` é opcional e serve para gerar thumbnails de vídeos usados nos painéis.

## Instalação local

```bash
git clone --branch main https://github.com/gamerplay20p5-dotcom/HTS-Bot-Multi-servers.git
cd HTS-Bot-Multi-servers
python -m venv .venv
# Linux/macOS:
. .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Copie `.env.example` para `.env` e preencha o token do Discord e as credenciais de infraestrutura que for usar. Não versione `.env`. Depois, teste e inicie:

```bash
python -m compileall -q manguadu scripts/update_bot.py
python -m pytest -q tests_py
python -m manguadu             # diagnóstico local sem conectar ao Discord
python -m manguadu --probe     # consulta Pterodactyl
python -m manguadu --bot       # inicia o bot Discord
```

## Primeira configuração no Discord

1. Convide o bot com permissões para ver canais, enviar mensagens, anexar arquivos no canal de auditoria, gerenciar canais e usar comandos de aplicação. Habilite os intents necessários no Developer Portal se a configuração do aplicativo exigir.
2. Execute `/config` → **Procurar servidores**. O proprietário do aplicativo seleciona o servidor e autoriza o Discord; depois o administrador da comunidade seleciona o servidor permitido, verifica a pasta Lua e conclui o vínculo automático. O cadastro manual por prova de arquivo continua disponível. Configure canais, tickets e WL em `/config_bot`. [Passo a passo PT/EN](docs/SERVER_SETUP.pt-en.md).
3. Em **Config WL**, defina se a lore é obrigatória e selecione revisão manual ou automática. O servidor PZ vem do vínculo de `/config`. Para lore acima de 4.000 caracteres, use `/config_lore` com um arquivo `.txt` UTF-8 em um canal privado da equipe.
4. Use `/config_api`, abra o Google AI Studio pelo botão do painel, crie uma chave, cole-a no modal privado e teste a conexão. A camada gratuita pode usar prompts para melhorar produtos Google; o painel informa isso antes da configuração.
5. Na instalação inicial, configure no `.env` do dedicado a URL e a chave Pterodactyl compartilhada. ID/UUID e pasta Lua são detectados por Discord; o endpoint RCON fica em `/config` e a senha é lida do INI do PZ. O `.env` de servidor único só é importado para a guild legada indicada em `GUILD_ID`.
6. Vincule nomes PZ aos membros Discord com `/painel jogadores vincular`, abra `/config_kick_automatico`, escolha a call e ative a fiscalização. O bot exige RCON válido e vínculos para os jogadores que já estiverem online antes de ativar.
7. Consulte [PZ_COMMANDS.md](PZ_COMMANDS.md) para os comandos e fluxos do servidor e [DEPLOY.md](DEPLOY.md) para o processo de produção.

### Canais da WL

- **Canal de decisões:** recebe aprovações e reprovações, incluindo o motivo. Esse aviso fica no Discord, fora do chat dentro do Project Zomboid.
- **Canal de auditoria:** recebe a lore analisada e o resumo da triagem; toda WL aprovada também anexa a lore completa em `.txt`. Administradores podem recuperar a lore mais recente de um usuário PZ com `/wl puxar_lore jogador:<nome PZ>`.
- **Ticket privado:** mantém a conversa entre quem abriu o pedido e a equipe autorizada. A senha gerada ou informada é entregue por DM; se a DM estiver fechada, o bot usa o canal privado.

## Configuração e dados

`/config_bot` guarda no SQLite IDs de canais, categoria, cargo e regras de WL por servidor Discord. O arquivo local `data/bot-config.sqlite3` deve ser preservado entre atualizações. A chave Gemini fica cifrada nesse banco; preserve também `data/bot-config.sqlite3.key` para que o bot possa decifrá-la após reiniciar ou restaurar backup.

Credenciais do Discord, Pterodactyl, RCON e uma eventual chave privada do GitHub vivem apenas no `.env` da VM. A chave Gemini é cadastrada pela UI e cifrada localmente. `BOT_GITHUB_TOKEN` pode ficar vazio para repositório público. Para instalação, veja [`.env.example`](.env.example); os arquivos de servidor e mapa têm exemplos em `config/`.

## Vários servidores no mesmo dedicado

Uma instância do bot atende vários Discords, com um servidor PZ vinculado a cada Discord. Configure no `.env` apenas as credenciais globais do Discord, `PTERO_URL`, a chave compartilhada `PTERO_API_KEY` e, se necessário, `PTERO_VOLUMES_ROOT` (padrão `/var/lib/pterodactyl/volumes`). A conta da chave precisa ter acesso aos servidores que serão cadastrados. `GUILD_ID` passa a ser opcional e serve para migração da instalação antiga e sincronização imediata naquele Discord.

1. Adicione o bot ao Discord do cliente e abra `/config` (o alias `/condig` também funciona).
2. Em **Cadastrar / editar servidor**, informe o ID do Pterodactyl, o UUID completo do volume, o nome e a pasta Lua relativa ao volume, normalmente `.cache/Lua`.
3. Crie `friendhost-discord-link.txt` na raiz desse servidor pelo painel Pterodactyl, cole o código mostrado na resposta privada e clique em **Validar vínculo**. A confirmação expira em 15 minutos e evita associar um servidor ao Discord errado.
4. Configure os canais pelo botão **Canais, tickets e WL** ou `/config_bot`. Status, ranking, logs, automações, temas e vínculos de jogadores ficam isolados por Discord.
5. Para RCON, o bot lê `RCONPassword` no único INI em `Server/` desse volume. Use **RCON publicado** para informar host/porta publicados ou escolher o INI quando houver vários. A senha não é copiada para o Discord.

O UUID só pode pertencer a um Discord. Uma troca para outro volume requer migração pelo operador para preservar corretamente tickets, WL e automações. Servidores em outras máquinas Wings precisam ter seus volumes montados na raiz local configurada: esta versão usa a API para validar/administrar e leitura local para o fluxo intenso de dados. Não faz cópia completa periódica dos arquivos pela API. `config/servers.json` permanece para ferramentas legadas/CLI; as guilds vinculadas não o usam para escolher outro cliente.

Rankings e status são atualizados aproximadamente a cada **10 minutos**, com fases diferentes por Discord. Uma fila de três trabalhadores impede varreduras simultâneas ilimitadas e não acumula repetições do mesmo trabalho. Logs são lidos a cada 5 segundos, em lotes de até 1 MiB/2.000 linhas; o espelho agrupa mensagens e resume excessos para respeitar os limites do Discord. O painel `/config` mostra bytes de logs ainda pendentes. Não há replay do histórico de chat na inicialização.

Leituras de jogadores usam cabeçalho e últimas 256 KiB do CSV, cache por tamanho/data de alteração (até 4 MiB/2.048 entradas por guild) e quatro threads de arquivos. Mortes são agregadas incrementalmente em lotes de 4 MiB, com cursor persistido. Respostas da API e leituras diretas auxiliares têm limite de 8 MiB; arquivos maiores exigem leitura incremental ou manutenção. Rascunhos de lore ficam comprimidos em disco e as operações de lore longa têm duas vagas compartilhadas.

Preserve `data/bot-config.sqlite3`, sua chave `.key` e `data/guilds/` nos backups. IDs, caminhos e credenciais de um servidor não viram fallback de outro. Instalações antigas só importam o `.env` para a guild indicada explicitamente em `GUILD_ID`.

O orçamento de aproximadamente 2,56 GB (1% de 256 GB) deve ser confirmado no dedicado com carga real; não há promessa de RSS fixa. Para uma medição local reproduzível, execute `python -m scripts.benchmark_multiserver --servers 25 --players 200 --history 200`. Esse teste mede leituras/renderização sintéticas e alocações Python, sem tráfego real de Discord/Pterodactyl. Veja em [DEPLOY.md](DEPLOY.md) como impor um teto de memória no Linux.

## Atualização automática

O atualizador Python executa `git fetch`, `git pull --ff-only`, instala o projeto e compila os módulos antes de reiniciar o processo configurado em `PM2_APP_NAME`. Ele cancela a operação se encontrar alterações rastreadas locais. Para habilitar as verificações periódicas, configure `BOT_AUTO_UPDATE_ENABLED=1`; para uma atualização manual, use `/bot atualizar`.

Use `BOT_UPDATE_BRANCH=main` para acompanhar as atualizações publicadas. Um token GitHub só é necessário se o repositório for privado.

## Validação e resultados medidos

O teste sintético local usou **25 servidores × 200 jogadores × 200 linhas históricas** (5.000 jogadores). A primeira varredura/renderização levou **31,202 segundos**; a repetição com cache, **4,986 segundos**. O pico de alocações Python medido durante esse teste foi **6,06 MiB**. [Relatório reproduzível](artifacts/multiserver-benchmark.json).

O teste foi executado no Windows de desenvolvimento e exclui RSS total do processo, tráfego real do Discord/Pterodactyl e carga do dedicado. Ele verifica leituras/cache/renderização locais; não garante que a produção ficará abaixo de 2,56 GB. O espelho resume mensagens em excesso, os trabalhos podem atrasar sob sobrecarga e arquivos auxiliares acima de 8 MiB exigem leitura incremental ou manutenção. Os logs completos continuam no servidor PZ.

O conjunto de **60 testes automatizados** cobre também isolamento entre 25 guilds, ausência de fallback para outro cliente, validação ID/UUID/código, concorrência limitada, fila de logs independente, UTF-8 entre lotes, cursores de mortes persistidos e exportação de lore. Execute `python -m pytest -q` para conferir a versão local.

## Idioma por comunidade

Administradores podem executar `/language` mesmo antes de vincular o servidor PZ. Escolha **Português (Brasil)** ou **English**; a preferência fica no SQLite e é recuperada após reiniciar. O mesmo menu aparece em `/config` e `/config_bot`. Os rankings/status usam o novo idioma na próxima atualização agendada, e o painel principal de tickets já publicado é atualizado quando possível. Mensagens antigas e formulários já abertos não são reescritos.

O idioma das respostas do bot é da comunidade; o idioma exibido no menu de comandos é do aplicativo Discord de cada usuário, conforme o mecanismo de localização do Discord. Por exemplo, `/painel jogadores vincular` aparece como `/panel players link`, `/servidor` como `/server`, `/automacao` como `/automation` e `/wl puxar_lore` como `/wl download_lore` para usuários com Discord em inglês. Os identificadores internos e valores de protocolo permanecem iguais. Tokens de confirmação como `APAGAR`, `EXPULSAR` e `RESTAURAR` continuam literais, com instruções traduzidas indicando o que digitar.

Nomes de jogadores, servidores e facções, lore original, logs/saída bruta do jogo e textos personalizados de temas não são traduzidos automaticamente. O Gemini recebe a orientação de produzir justificativa, resumo e evidências no idioma atual. As traduções da interface usam um catálogo local versionado em `manguadu/locales/en.json`, sem chamadas adicionais de API. O idioma padrão para instalações existentes é português.

## Identidade e autoria

**Criador: Malaio/Erick.** O nome público é **HTS Bot Multi servers**, e a distribuição Python é `hts-bot-multi-servers`. O módulo continua `manguadu` para manter o comando de inicialização. O mod FriendHost mantém seu nome próprio. O proprietário deve alterar o nome visível do aplicativo no Discord Developer Portal caso ainda mostre a identidade antiga.

Instalações PM2 existentes podem conservar o nome do processo; configure `PM2_APP_NAME` com o nome real. Para instalações novas, use `hts-bot-multi-servers`. Ao trocar a distribuição Python instalada, remova o pacote antigo antes de instalar este com `pip install -e .`, preservando `.env` e `data/`.

## Estrutura

| Caminho | Responsabilidade |
| --- | --- |
| `manguadu/discord_bot.py` | Cliente Discord, comandos, views, tickets e integração WL |
| `manguadu/config_store.py` | Configuração e estado transacional em SQLite |
| `manguadu/server_registry.py`, `pterodactyl.py`, `rcon.py` | Cadastro, painel de hospedagem e console do jogo |
| `manguadu/pz_data.py`, `player_data.py`, `player_reports.py` | Leitura de arquivos FriendHost e relatórios de jogador |
| `manguadu/pz_map_renderer.py`, `pz_wipe.py`, `pz_path_discovery.py` | Mapas, wipes e descoberta de arquivos PZ |
| `manguadu/panel_*.py`, `automation_scheduler.py`, `watchers.py` | Painéis, automações e monitoramento |
| `manguadu/bot_updater.py`, `scripts/update_bot.py` | Atualizador Python usado na VM |
| `tests_py/` | Testes automatizados, sem conexão com Discord ou serviços externos |

Este repositório executa o bot em Python. A dependência Discord é `discord.py`; não há runtime Node/npm no projeto. Veja [PYTHON_MIGRATION.md](PYTHON_MIGRATION.md) para o registro técnico da migração.

## Licença e contribuição

Consulte a licença do repositório antes de redistribuir. Alterações podem ser validadas com `python -m pytest -q tests_py` e `python -m compileall -q manguadu scripts/update_bot.py`.
