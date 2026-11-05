# Configuração pelo Discord e TTF Stats MP / Discord setup and TTF Stats MP

**HTS Bot Multi servers · Malaio/Erick**

## Português

### Configurar sem terminal

As credenciais compartilhadas `PTERO_URL` e `PTERO_API_KEY` continuam no `.env` do dedicado. Elas não são solicitadas nem exibidas a administradores das comunidades. Depois da instalação inicial, o fluxo de cada comunidade é pelo Discord:

1. O **proprietário do aplicativo Discord** abre `/config` → **Procurar servidores**. Somente ele vê o catálogo completo acessível à chave Client. Use **Buscar por nome** para busca parcial, as setas para páginas de até 25 servidores ou **Atualizar lista** para recarregar. Servidores com nomes iguais são diferenciados pelo identificador, UUID e node.
2. Selecione o servidor e clique em **Autorizar Discord**. Confirme o ID da comunidade do cliente; o campo vem preenchido com o Discord atual. A autorização fica no SQLite e é auditada com o ID do operador. Para autorizar outra comunidade, informe seu ID Discord, sem procurar UUIDs no terminal.
3. O administrador da comunidade abre `/config` → **Procurar servidores**. Ele vê exclusivamente os servidores autorizados para sua guild e o servidor já vinculado, se houver.
4. Selecione o servidor → **Verificar servidor**. O bot confirma ID/UUID na API e procura pastas Lua conhecidas exclusivamente dentro desse volume. Se houver mais de uma pasta, selecione a correta.
5. Clique em **Concluir vínculo automático**. O bot verifica conflitos, cria `friendhost-discord-link.txt` pela API oficial, relê o conteúdo e instala o vínculo. O código expira em 15 minutos e é consumido após a instalação.
6. Configure os canais em `/config_bot`; o canal **Ranking** receberá o painel. Use **Diagnosticar integração** em `/config` para confirmar fonte, contas, personagens, registros inválidos, cache anterior e métricas disponíveis.
7. Em **Provedor de estatísticas**, escolha **Automático**, **FriendHost** ou **TTF Stats MP**. No modo automático, a presença de `FriendHost_Data/Jogadores` tem prioridade; caso ela exista vazia e o TTF contenha os dados desejados, selecione TTF explicitamente. As duas fontes nunca são somadas.

Use `/language` para selecionar PT-BR ou EN por comunidade. As novas telas também respeitam esse idioma. O nome localizado de cada slash command depende do idioma do aplicativo Discord do usuário.

### Autorizações e recuperação

A chave global **não comprova a propriedade do servidor por um administrador Discord**. Apenas o proprietário do aplicativo pode conceder ou revogar o mapeamento independente guild/UUID. Um UUID não pode estar autorizado nem vinculado a comunidades diferentes ao mesmo tempo. Revogar a autorização impede novas instalações automáticas; preserva um vínculo já instalado. Para uma transferência de cliente, o operador deve revisar o vínculo e os dados pendentes antes de migrar. Não há transferência automática.

O botão **Revogar autorização** atua sobre o Discord em que a tela foi aberta. Para outra comunidade, abra a configuração naquele Discord. O botão **Atualizar leitura do vínculo** recarrega caminhos, provedor e RCON do servidor atual, sem criar uma nova prova e sem trocar de UUID.

O fluxo **Cadastrar / editar servidor** → **Validar vínculo** continua disponível. Sem autorização do proprietário do aplicativo, a prova precisa ser criada manualmente por quem tem acesso ao gerenciador de arquivos do servidor. Não existe autoaprovação com a mesma chave global. Uma prova automática pendente também não pode ser validada depois de revogada a autorização.

Um arquivo de prova existente **não é sobrescrito**. Se ele contiver exatamente o código pendente da mesma guild, UUID e pasta, ainda válido, o bot pode concluir uma tentativa interrompida. Depois de consumido ou expirado, um novo cadastro não o reutiliza. Se precisar de uma nova prova, revise o arquivo pelo gerenciador do Pterodactyl; o bot não apaga arquivos automaticamente. Falha de escrita, ausência de `file.create`, bloqueio da API ou divergência de leitura impedem a instalação e deixam o fluxo manual disponível. A escrita da API não oferece uma operação exclusiva com comparação atômica: este bot serializa tentativas por guild em uma única instância e preserva arquivos detectados, mas um operador externo não deve editar a prova durante a operação.

A API usada é a **Client**, sem `type=admin`, `type=admin-all` ou Application/Admin API. O bot usa `GET /api/client`, `GET /api/client/servers/{identifier}`, listagem/leitura de arquivos e `POST .../files/write?file=...`. Os campos opcionais de node, status e allocations são tratados como opcionais; `status=null` não é apresentado como confirmação de servidor online. O identificador retornado pela API é preservado, inclusive quando não corresponde aos primeiros oito caracteres do UUID.

Referências do projeto Pterodactyl: [rotas Client](https://github.com/pterodactyl/panel/blob/1.0-develop/routes/api-client.php), [listagem e paginação](https://github.com/pterodactyl/panel/blob/1.0-develop/app/Http/Controllers/Api/Client/ClientController.php), [campos de servidor](https://github.com/pterodactyl/panel/blob/1.0-develop/app/Transformers/Api/Client/ServerTransformer.php), [permissão de escrita](https://github.com/pterodactyl/panel/blob/1.0-develop/app/Http/Requests/Api/Client/Servers/Files/WriteFileContentRequest.php). A versão instalada no painel do Node0 não foi consultada nesta sessão; a aplicação confirma acesso e esquema em cada vínculo.

### Volumes e dados suportados

O UUID retornado pela API resolve `<PTERO_VOLUMES_ROOT>/<UUID>`. O padrão da raiz é `/var/lib/pterodactyl/volumes`. A descoberta verifica apenas `.cache/Lua`, `Zomboid/Lua`, `Lua` e `.local/share/Zomboid/Lua`, sem percorrer saves, chunks ou mods. Links simbólicos e caminhos externos são rejeitados. A conta Linux do bot precisa de leitura nesses volumes; esta integração não pede root, Docker socket ou alteração de permissões do jogo.

Um servidor em outro node pode aparecer para o operador pela API, mas não pode concluir a leitura local se seu volume não estiver montado de forma segura neste dedicado. O diagnóstico informa a ausência. Não há download periódico completo de estatísticas pela API nem leitura de outra guild como fallback.

**FriendHost:** preserva os leitores existentes de `FriendHost_Data/Jogadores` e `FriendHost_Data/Servidor`. O mod local fornecido escreve arquivos `.txt` contendo CSV com ponto e vírgula; `.csv` e `.csv.txt` legados continuam aceitos. Nenhum arquivo do mod foi modificado.

**TTF Stats MP:** lê `ttf_stats_mp/index.txt` no formato `characterID|username|forename|surname`. Cada personagem vem de `<characterID>.txt`, com `chave=valor` e `version=1`. A conta vem do índice e é conferida contra o arquivo; o nome do arquivo nunca é dividido por underscore. Identificadores repetidos não duplicam estatísticas. Personagens removidos do índice, arquivos removidos e `deleted=true` são excluídos dos totais na próxima varredura. `isDead=true` permanece no histórico e conta uma morte.

| Métrica | Campo TTF | Regra |
|---|---|---|
| Zumbis mortos | `lastSeenKills` | Total do personagem; soma entre personagens distintos da mesma conta |
| Horas | `gameTimeSeconds` | Segundos exportados ÷ 3.600; soma histórica por conta |
| Distância em metros | `distance` | Acumulado do personagem; soma histórica por conta |
| Mortes | `isDead` | Quantidade de personagens mortos incluídos |
| Veículos / armas quebradas | `vehicleKills` / `brokenWeaponsTotal` | Soma dos contadores por personagem |
| Armas / categorias | `weapons` / `categories` | Pares `chave:quantidade`, exibidos por personagem no relatório TXT |

`killsAtMidnight`, `dailyRecord` e `dailySeries` **não** são convertidos em abates acumulados. Se um campo não existe em algum personagem, o total daquela métrica por conta fica indisponível, em vez de somar um subtotal incompleto. Zero é exibido apenas quando o arquivo efetivamente exportou zero. Saúde, skills, inventário, posição, profissão e SteamID não são inventados para esse formato.

O código TTF local relacionado (`TwisTonFireStats.lua`) confirma `lastSeenKills = player:getZombieKills()`, baselines diárias separadas e distância acumulada com exibição metros/quilômetros. Ele não contém o exportador servidor `ttf_stats_mp` do Runners. O adaptador implementa o esquema v1 fornecido no pedido; a equivalência de `gameTimeSeconds` com outras medidas de sobrevivência do jogo e a versão exata do exportador precisam ser conferidas com os arquivos reais do Runners. Versões diferentes de `1` são rejeitadas, sem interpretação silenciosa.

### Rankings, relatórios e diagnóstico

- `/rank jogador:<conta> tipo:<métrica> escopo:account` mostra a posição nos totais históricos da conta.
- `/rank jogador:<characterID> tipo:<métrica> escopo:character` mostra um personagem TTF. Também aceita o nome do personagem; se informar uma conta com vários personagens, retorna o mais bem classificado naquela métrica. Use o ID para eliminar ambiguidade.
- O painel automático e o preview usam a mesma camada. No TTF, mostram contas/personagens e cinco métricas disponíveis, com indisponibilidade explícita.
- `/info`, `/status` e `/statuscomplete` usam o provedor selecionado. O relatório completo TTF anexa os valores por personagem, armas e categorias; limita a exportação a 1.000 personagens por conta. O status vivo/morto refere-se ao personagem visto mais recentemente pelo timestamp exportado, não à presença online. Presença online continua sendo consultada por RCON, sem deduzir conexões pelo histórico TTF.
- `/config` → **Diagnosticar integração** consulta a API e mede o provedor local. Registros inválidos e leituras reaproveitadas do cache são contados; falha temporária do índice informa erro e pode manter a última leitura válida da mesma guild.

### Desempenho e limites

Cada runtime tem seu próprio provedor, cache e estado. Os rankings continuam na fila existente, com três trabalhadores e fases independentes a cada 600 segundos; as quatro threads de arquivos e o limite compartilhado de três requisições Pterodactyl são reutilizados. Operações de leitura e parsing não executam no loop de eventos Discord. Comandos de consulta adiam a resposta antes de iniciar a leitura.

No TTF, uma fotografia tem validade de cinco segundos. Depois disso, o bot confere tamanho, `mtime_ns` e inode dos arquivos; arquivos inalterados não são relidos. O índice tem limite de 4 MiB, cada personagem 256 KiB, cada guild até 10.000 IDs distintos, cache de registros estimado em 8 MiB e fotografia estimada em 16 MiB. Ao ultrapassar o orçamento da fotografia, o bot rejeita a atualização e informa o erro; pode manter a última leitura válida. Mapas de armas/categorias são limitados a 512 pares. Esses limites não representam o RSS total: índices, objetos agregados, Discord, lore e outras funções também usam memória. Não há promessa de ficar abaixo de 1% de 256 GB sem medição de produção. Relatórios TXT também têm corte por tamanho, além do limite de personagens.

Arquivos que mudaram durante a leitura ou terminaram sem uma quebra de linha são tratados como gravação incompleta. O registro anterior pode ser mantido com contador de cache anterior; arquivos efetivamente removidos e personagens deletados saem da agregação. O painel avisa quando há dados inválidos ou antigos. Uma recuperação válida substitui o cache na varredura seguinte. O código não escreve estatísticas processadas nem duplica o histórico em disco.

### Atualização e validação

A implementação é distribuída pela `main`, conforme autorização do proprietário. A publicação no GitHub não implanta o bot no dedicado. Antes de atualizar a instalação de destino, preserve `.env`, `data/bot-config.sqlite3`, a chave `.key` e `data/guilds/` no backup. As tabelas novas são criadas de forma aditiva, sem apagar configurações existentes.

```bash
git fetch origin
git switch main
git pull --ff-only origin main
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q manguadu scripts
pm2 restart hts-bot-multi-servers --update-env
pm2 save
pm2 logs hts-bot-multi-servers --lines 50
```

Use o nome efetivo do seu processo PM2 se for diferente. Configure `BOT_UPDATE_BRANCH=main` para o atualizador. Não reinicie Wings, Docker ou PZ para esta atualização. A implementação local não conecta ao Node0 nem valida o servidor Runners em produção; faça a confirmação pelo diagnóstico e pelo preview antes da troca do processo em uso.

Para reproduzir uma carga sintética local sem acessar a produção:

```bash
python -m scripts.benchmark_ttf --servers 25 --accounts 100 --characters 2
```

Medição local desta implementação: 25 servidores, 100 contas e dois personagens por conta (5.000 arquivos de personagem), três varreduras concorrentes. Primeira leitura/renderização: **23,800 s**; conferência por metadados com cache: **5,251 s**; pico de alocações Python: **13,41 MiB**. [Resultado](../artifacts/ttf-multiserver-benchmark.json). Medido no Windows com `tracemalloc`, exclui rede, RSS total e o dedicado; não é uma garantia de consumo em produção. Tempos variam com cache do sistema operacional e carga da máquina; esta foi a última execução com o código final.

## English

### Local verification / Verificação local

**116 tests passed; 1 skipped.** The skipped test requires creating an actual symbolic link, which this Windows account did not permit. The suite covers Client API routes and pagination, duplicate names, independent grants, cross-guild denial, proof expiry/reuse/write failures, TTF parsing/aggregation/cache/limits, 25 concurrent guilds, FriendHost compatibility, Discord embeds, English output and diagnostics. A local HTTP server validates the real Client listing/file-write request paths without contacting production. Existing manual proof tests remain passing. There were 300 deprecation warnings from the installed test/dependency stack, including `pytest-asyncio` event-loop policy calls on Python 3.14.

`compileall`, Python 3.11 syntax parsing, whitespace checks (excluding the preexisting local `.env.example` customization) and a temporary wheel build passed on Python 3.14.2 / Windows. The wheel includes the new statistics/setup modules and translation catalog. The final synthetic benchmark measured **23.800 s** initially, **5.251 s** when rechecking cached file metadata and **13.41 MiB** peak Python allocations; see [the JSON artifact](../artifacts/ttf-multiserver-benchmark.json). OS caching and machine load affect timings. Measurements exclude production network traffic and total RSS. Node0, the live Runners exporter, live Discord interactions and the POSIX-only descriptor path still need validation in the Linux staging environment. No production service was changed.

### Configure from Discord

The shared `PTERO_URL` and `PTERO_API_KEY` stay in the dedicated machine's `.env`. Community administrators never enter or view that key. Following initial installation:

1. The **Discord application owner** opens `/config` → **Find servers**, searches by partial name and selects a server. Only the application owner sees the complete catalog accessible to the Client key. Pages contain at most 25 servers; IDs, UUIDs and node names distinguish duplicate names.
2. Click **Authorize Discord** and confirm the customer's guild ID. The current guild is prefilled. Authorization and operator ID are recorded in SQLite. The owner can approve another community by its Discord guild ID without finding server UUIDs in a terminal.
3. The customer's administrator opens the same picker and sees only that guild's explicitly authorized or already bound servers.
4. Select a server → **Verify server** → choose a Lua folder if several are found → **Complete automatic linking**.
5. The bot rechecks the API ID/UUID, prevents binding conflicts, writes the temporary proof using the official file API, reads it back and persists the runtime. Codes expire after 15 minutes and are consumed on success.
6. Configure channels in `/config_bot`, select a ranking channel and inspect **Diagnose integration**. Use **Statistics provider** to select Automatic, FriendHost or TTF Stats MP. Automatic prefers an existing `FriendHost_Data/Jogadores` directory; explicitly select TTF if that directory is empty or the other provider is desired. Provider totals are never merged.

Use `/language` for community PT-BR/English messages. Slash command names follow each user's Discord application locale; canonical Portuguese names remain valid in documentation.

### Authorization and recovery

Access through the shared key is **not** evidence that a guild administrator owns a server. Only the Discord application owner can grant or revoke the independent guild/UUID mapping. A UUID cannot be authorized or bound to two different guilds. Revocation blocks new automatic installations and preserves an installed binding. **Revoke authorization** targets the current guild; open the target guild's setup to revoke its mapping. Transfers require operator review of existing bindings and pending data. **Refresh bound server** reloads the current runtime without writing another proof or changing UUIDs.

Manual registration/file verification remains available without an application-owner grant. Someone with independent server-file access must place that manual proof. The bot does not generate its own proof with the shared key and treat it as ownership. Revoked automatic pending proofs cannot bypass authorization through manual validation.

Existing proof files are preserved. An interrupted attempt can resume only when its exact pending code, guild, UUID and Lua path match and the code has not expired. Consumed or expired proofs cannot be reused. Review an old proof using the Pterodactyl file manager when starting a new attempt; this flow never deletes files automatically. Failed file writes, missing `file.create` permission, API failures or read-back mismatches leave manual fallback available. The Client write API has no exclusive compare-and-create operation: this single-process implementation serializes attempts by guild and preserves detected files, but external operators must not edit the proof concurrently.

Only the **Client API** is used: no unrestricted admin listing or Application API. Server names/descriptions/node/status/allocations are optional; a null status is not reported as proof the game is online. API-provided identifiers are preserved even when they differ from the UUID prefix. Official implementation references are linked in the Portuguese section above. The installed Node0 Panel version was not inspected in this session; the bot checks the returned schema and permissions during linking.

### Local Wings data and statistics

The verified UUID selects `<PTERO_VOLUMES_ROOT>/<UUID>`; the default root is `/var/lib/pterodactyl/volumes`. Discovery checks only `.cache/Lua`, `Zomboid/Lua`, `Lua` and `.local/share/Zomboid/Lua`. It does not traverse saves/chunks/mods, rejects symbolic links and external paths, and requires ordinary read access. This feature does not request root, Docker socket access or game-file permission changes. A server on another node needs a securely mounted volume on the bot's machine; missing local data is diagnosed and never replaced by another customer's files.

FriendHost continues using its existing semicolon CSV readers, including `.txt` exports and legacy `.csv`/`.csv.txt`. The supplied mod was inspected and left unchanged.

TTF reads `ttf_stats_mp/index.txt` as `characterID|username|forename|surname` and each version-1 character file as key/value text. It verifies ID and username against the index and never infers accounts by splitting filenames. Duplicate IDs count once. Deleted characters, removed files and IDs removed from the index leave the totals. Dead characters remain historical records and count one death each.

| Metric | Export field | Account rule |
|---|---|---|
| Zombie kills | `lastSeenKills` | Sum distinct characters' cumulative counters |
| Hours | `gameTimeSeconds` | Exported seconds / 3,600, summed across characters |
| Distance (m) | `distance` | Sum character counters |
| Deaths | `isDead` | Count included dead characters |
| Vehicle kills / broken weapons | `vehicleKills` / `brokenWeaponsTotal` | Sum per-character counters |
| Weapons / categories | `weapons` / `categories` | Per-character key/count pairs in TXT reports |

Daily baselines/records/series are never treated as cumulative kills. An account metric is unavailable if any included character lacks it, avoiding partial totals or invented zeroes. This export does not provide health, skills, inventory, position, profession or SteamID.

The related local TTF client source confirms `lastSeenKills` uses `player:getZombieKills()`, daily baselines are separate, and its distance tracker displays metres/kilometres. That local copy does not contain Runners' server exporter. This adapter implements the supplied version-1 schema; verify the exact exporter and the interpretation of its exported seconds with actual Runners files before equating them to other in-game survival measures. Unsupported versions are rejected.

Use `/rank` with account scope for cumulative history, or character scope with a character ID for an individual TTF character. A character name is also accepted; selecting an account in character scope returns its highest-ranked character for that metric. `/info`, `/status` and `/statuscomplete`, previews and scheduled rankings use the same selected provider. Complete TXT reports include up to 1,000 characters and their weapons/categories. Living/dead status follows the most recently seen character, while actual online presence continues using RCON. Historical TTF records are not treated as online players.

### Performance, diagnostics and deployment

The existing staggered 600-second ranking queue, three routine workers, four file threads and shared three-request API semaphore are reused. File parsing runs off the Discord event loop; query interactions are deferred before scanning. Each guild owns a five-second snapshot and a metadata cache keyed by file size, nanosecond mtime and inode. Unchanged indexes and character files are not reread. Limits: 4 MiB index, 256 KiB per character, 10,000 distinct IDs per guild, estimated 8 MiB character cache, estimated 16 MiB snapshot budget, 512 weapon/category pairs per record. Snapshots exceeding the budget or character limit are rejected with an error; a previous valid snapshot may be retained. TXT reports also enforce a size cutoff. These limits exclude total process RSS and do not guarantee a percentage of machine RAM.

Files changed during a read or without a final newline are treated as incomplete writes. A previous valid record may be reused and flagged stale; removed/deleted records are excluded. Panels report invalid/stale data and diagnostics show API/local-volume/provider failures, measured counts and available metrics. The next valid scan recovers automatically. No duplicate processed history is stored on disk.

The implementation is distributed from `main` with the owner's authorization. GitHub publication does not deploy the bot on the dedicated machine. Back up `.env`, SQLite, its encryption key and guild state before adoption. Use the update/test/PM2 commands in the Portuguese section and adjust the PM2 process name if needed. Set `BOT_UPDATE_BRANCH=main` to follow published updates. Database changes are additive. Do not restart Wings, Docker or PZ. Node0/Runners production validation was not available from this Windows workspace; use Discord diagnostics and ranking preview to verify the actual installation. The synthetic benchmark command above uses temporary files only and excludes network and production RSS.
