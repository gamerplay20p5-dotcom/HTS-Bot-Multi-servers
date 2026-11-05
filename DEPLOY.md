# Deploy do HTS Bot Multi servers

O bot é executado integralmente em Python. O PM2 atua como supervisor do processo Python; Discord, comandos, tickets, WL, tarefas e atualização ficam no pacote `manguadu`.

## Instância compartilhada no dedicado

Use um processo Python para todos os Discords. A chave `PTERO_API_KEY` e a URL `PTERO_URL` ficam no `.env` do dedicado; cada administrador cadastra somente o seu ID/UUID no `/config`. O bot valida o acesso àquele servidor com um código em `friendhost-discord-link.txt` na raiz do volume e verifica se o UUID retornado pela API corresponde ao volume informado. Um UUID não pode ser cadastrado por dois Discords.

`PTERO_VOLUMES_ROOT=/var/lib/pterodactyl/volumes` define a raiz de leitura local. A conta Linux do bot precisa acessar os volumes cadastrados. Se houver Wings em outra máquina, monte esses volumes nessa raiz; leitura remota contínua de logs pela API não é implementada. A pasta Lua do formulário é relativa a cada volume. Não use o `.env` para alternar UUIDs enquanto o processo roda.

`GUILD_ID` é opcional no modo compartilhado. Se preenchido, é a única guild que pode importar os canais e o UUID da configuração antiga. Comandos também são registrados globalmente para os outros Discords. Os arquivos de tema, automações, anticheat, vínculos de jogadores, rascunhos e cursores ficam em `data/guilds/<guild_id>/`. Preserve essa pasta, `data/bot-config.sqlite3` e `data/bot-config.sqlite3.key` nos backups.

O bot mantém três trabalhadores de rotina, quatro threads de arquivos, no máximo três requisições Pterodactyl e seis conexões RCON concorrentes. Status/ranking têm cadência de 600 segundos e fases independentes. Em falhas consecutivas, o trabalho recebe espera progressiva; não há execução acumulada de todos os ciclos perdidos. HTTP 429 da API compartilhada introduz espera entre chamadas. O espelho de chat resume excessos em cada lote; os logs completos continuam no PZ. `/config` exibe o atraso em bytes.

O uso abaixo de 1% dos 256 GB precisa ser medido na produção. Se o bot for um serviço systemd próprio, configure `MemoryHigh=2G` e `MemoryMax=2500M` no serviço para impor um orçamento conservador; atingir o teto pode encerrar o processo. Em PM2, `pm2 restart friendhost-pz-bot --max-memory-restart 2200M` configura uma reinicialização ao superar esse limiar, mas não é um limite rígido. Monitore o RSS e o atraso dos logs antes de ampliar a carga. Evite aplicar um limite ao serviço PM2 inteiro quando ele supervisionar outros aplicativos.

Atualização e reinício da instância pelo grupo `/bot` são restritos ao proprietário do aplicativo Discord, porque afetam todos os servidores. Os administradores de cada guild continuam limitados ao próprio servidor PZ.

## Atualizar uma instalação existente

Faça backup do `.env` e da pasta `data/` antes da troca. O `.env` e o banco `data/bot-config.sqlite3` contêm configuração local que deve ser preservada.

No checkout existente, obtenha e abra a branch de migração:

```bash
git fetch --prune origin
git switch --track origin/feature/hts-multi-servers
```

Se a branch já existir localmente, use `git switch feature/hts-multi-servers`. A URL atual do remote ainda se chama `Manguadu`; se o repositório já tiver sido renomeado no GitHub, use a URL clone exibida na página atual do repositório.

Crie o ambiente e instale o pacote Python:

```bash
cd /caminho/do/Friendhost-PZ-Bot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Mantenha o `.env` existente. Para uma instalação nova, copie `.env.example` para `.env` e preencha `DISCORD_TOKEN`. Mantenha Pterodactyl, RCON e chaves privadas no `.env` da VM. Os canais, categoria, cargo de equipe e regras WL são ajustados pelo `/config_bot` no Discord.

Valide antes de iniciar o processo persistente:

```bash
python -m compileall -q manguadu scripts/update_bot.py
python -m pytest -q tests_py
python -m manguadu
```

## Criar ou substituir o processo PM2

Configure no `.env` da VM:

```env
PM2_APP_NAME=friendhost-pz-bot
BOT_PM2_RUNTIME=python
BOT_UPDATE_BRANCH=feature/hts-multi-servers
```

Se a instalação anterior tiver um processo Node chamado `manguadu`, remova somente esse processo depois de validar a instalação Python. Os arquivos `.env` e `data/` permanecem na pasta:

```bash
pm2 delete manguadu
pm2 start "$PWD/.venv/bin/python" --name friendhost-pz-bot --interpreter none -- -m manguadu --bot
pm2 save
pm2 startup
```

Em uma máquina nova, clone a branch de teste e instale antes de iniciar o PM2:

```bash
git clone --branch feature/hts-multi-servers https://github.com/gamerplay20p5-dotcom/HTS-Bot-Multi-servers.git Friendhost-PZ-Bot
cd Friendhost-PZ-Bot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
nano .env
```

Depois, use o comando `pm2 start` acima. Confira `pm2 logs friendhost-pz-bot` e execute `/bot status` no Discord. Ao iniciar, o bot publica os slash commands Python do aplicativo.

Quando esta branch for integrada em `main`, altere `BOT_UPDATE_BRANCH=main` no `.env` da VM e reinicie o processo.

## Atualizações

Administradores podem executar `/bot atualizar`; o bot também pode consultar atualizações quando `BOT_AUTO_UPDATE_ENABLED=1`. O atualizador faz `git fetch`, aplica `git pull --ff-only`, instala novamente o projeto Python, compila os módulos e reinicia `PM2_APP_NAME`. A operação é cancelada se houver alterações locais rastreadas para não sobrescrever trabalho da VM.

Em repositório público, deixe `BOT_GITHUB_TOKEN` vazio: `git fetch` pode ler o repositório sem autenticação. Se o repositório for privado, use um fine-grained token com acesso somente de leitura ao conteúdo e guarde-o apenas no `.env` da VM.

## Configurar o bot

1. Use `/config_bot` com uma conta administradora.
2. Configure os canais de tickets, lore, auditoria, decisões da WL, status, ranking e anticheat; selecione a categoria e o cargo da equipe.
3. Em **Config WL**, selecione o servidor PZ, defina se a lore será obrigatória, informe a lore base e escolha a revisão manual ou automática.
4. Configure Pterodactyl e RCON no `.env`. A UI Discord não salva essas credenciais.
5. Use `/painel config`, `/painel preview` e `/painel salvar` para montar os painéis públicos.

O canal de decisões recebe aprovações e reprovações com os motivos no Discord, fora do chat do jogo. O canal de auditoria recebe lore e resumos da triagem. A senha da WL é enviada por DM ou pelo ticket privado quando a DM está fechada.

## Preservar entre deploys

- `.env`
- `data/bot-config.sqlite3` e outros estados em `data/`
- `data/bot-config.sqlite3.key` junto do banco, para descriptografar a chave Gemini
- `data/automations.json`, temas, mídias e logs
- `config/pz-maps.json` se tiver coordenadas específicas
- `.venv/`

O bot requer Python 3.11 ou superior. `ffmpeg` é opcional para thumbnails de vídeos nos painéis. Veja [README.md](README.md) para visão geral, requisitos e comandos.

## Diagnostico RCON

Configure no `.env` a mesma senha usada em `RCONPassword` no servidor PZ, alem de `RCON_HOST` e `RCON_PORT`. O bot precisa alcancar esse endpoint por TCP. `127.0.0.1` so funciona quando a porta RCON do container esta publicada na propria maquina do bot; caso contrario, use o endereco e a porta publicados pelo Pterodactyl. Em `config/servers.json`, `rcon.host` e `rcon.port` substituem o endpoint do `.env`; campos omitidos herdam os valores do `.env`.

Depois de atualizar, rode `/rcon` com `players`. A resposta identifica se falhou na conexao TCP, autenticacao ou execucao do comando. Falha TCP normalmente aponta para IP, porta, publicacao da porta ou firewall. Recusa de autenticacao aponta para uma senha diferente entre o `.env` e a configuracao do servidor.

O bot le o arquivo `.env` na raiz desta instalacao; `.env.example` serve apenas como modelo. Se RCON e Pterodactyl aparecerem como incompletos, execute `.venv/bin/python -m manguadu` na pasta do bot. O diagnostico mostra os nomes dos campos ausentes sem imprimir senhas ou chaves. Se `config/servers.json` usar `passwordEnv` ou `apiKeyEnv`, preencha no `.env` os nomes de variaveis indicados por esses campos. Valores preenchidos no `.env` prevalecem sobre valores antigos guardados no ambiente do PM2 apos reiniciar o processo.

## IA e lore longa

Use `/config_api` como administrador para guardar a chave Gemini pelo Discord. O bot cifra a chave em `data/bot-config.sqlite3` usando `data/bot-config.sqlite3.key`; mantenha os dois arquivos juntos nos backups. Se o arquivo `.key` for perdido, sera preciso cadastrar a chave Gemini novamente. A chave nao precisa ser incluida no `.env`.

Com `/config_lore`, anexe um `.txt` UTF-8 em um canal privado da equipe para importar a lore oficial, ate 9.999.999 caracteres. No ticket, o jogador pode usar `/wl lore` para anexar a historia longa; o bot abre um formulario privado para usuario PZ, personagem e senha. O modal de texto do Discord aceita no maximo 4.000 caracteres, entao textos maiores precisam ser enviados como arquivo. O bot limita cada arquivo a 40 MiB e comprime a lore antes de gravar no SQLite.

O Gemini usa `gemini-3.5-flash-lite`, com triagem semantica, evidencias, coerencias e contradicoes. Trechos longos sao pre-selecionados localmente para limitar RAM e tamanho da requisicao; a IA precisa encaminhar para revisao quando os trechos nao sustentarem uma conclusao. Sem chave configurada, o bot continua com a triagem lexical local. O uso gratuito pode permitir que o Google use prompts para melhorar produtos; a tela `/config_api` avisa antes da configuracao. Confira a [tabela oficial de precos e uso de dados do Gemini](https://ai.google.dev/gemini-api/docs/pricing) e as [regras oficiais de componentes modal do Discord](https://github.com/discord/discord-api-docs/blob/main/developers/components/using-modal-components.mdx).
