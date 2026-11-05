# HTS Bot Multi servers: comandos Project Zomboid

Este é o guia operacional do bot Python para administradores da comunidade. Criado por Malaio/Erick, com identidade p?blica HTS Bot Multi servers; `manguadu` continua como o nome do pacote e módulo Python para inicialização.

Leia [README.md](README.md) para visão geral do projeto e [DEPLOY.md](DEPLOY.md) para instalar o runtime Python.

O novo `/config` permite procurar servidores pelo nome, receber autorização do proprietário do aplicativo, detectar ID/UUID/pasta Lua e concluir o vínculo por arquivo. Inclui diagnóstico e seleção FriendHost/TTF Stats MP. Veja o [guia completo PT/EN](docs/SERVER_SETUP.pt-en.md). No `/rank`, `escopo:account` usa totais históricos da conta e `escopo:character` aceita o ID de um personagem TTF. As métricas incluem abates, horas, distância, veículos e armas quebradas quando exportadas.

Em **Rankings publicados**, selecione quais tabelas entram no painel e no preview. `/rank jogador:<conta> tipo:skills10` consulta a quantidade de habilidades no nível 10 do snapshot atual FriendHost; o TTF v1 não exporta esses níveis. **Registros TTF inválidos** baixa um diagnóstico privado por arquivo. Veja [configuração, ACL e critérios PT/EN](docs/RANKINGS_AND_TTF_ACL.pt-en.md).

O runtime completo do bot e Python. Ele oferece `/config_bot`, `/bot`, `/servidor`, `/rcon`,
`/status`, `/statuscomplete`, `/automacao`, tickets, WL com `adduser` e os comandos publicos
`/online`, `/info`, `/skills`, `/traits`, `/rank`, `/localizar_veiculo`,
`/mapas`, `/gps`, `/satelite`, `/logs`, `/safehouse`, `/deletearquivo`, os comandos de whitelist,
administracao/jogador e teleport, e os comandos de wipe. Os painéis, automações,
alertas PZAntiCheat, espelho de logs e tarefas recorrentes também rodam no runtime Python.

O bot registra os comandos pelo sistema oficial de slash commands do Discord. Com `GUILD_ID` preenchido, eles aparecem imediatamente naquela guild; sem `GUILD_ID`, o registro e global e pode demorar para propagar.

## Configuração por Discord no dedicado

Use `/language` ou **Idioma / Language** no painel para selecionar português ou inglês por comunidade. As respostas novas seguem essa preferência persistida; o menu de slash commands segue o idioma do aplicativo Discord do usuário. Com Discord em inglês, por exemplo, `/servidor` aparece como `/server`, `/painel` como `/panel` e `/automacao` como `/automation`. Dados dos jogadores, lore original e logs não são traduzidos automaticamente.

Criado por **Malaio/Erick**. [English operations and commands](docs/OPERATIONS.en.md).

Configure o token Discord, a URL/chave Pterodactyl compartilhada e a raiz dos volumes no `.env` do dedicado. Cada Discord cadastra seu próprio ID/UUID, nome, pasta Lua e endpoint RCON em `/config` (alias `/condig`). A validação exige um código temporário em `friendhost-discord-link.txt` na raiz do servidor Pterodactyl e a correspondência do UUID na API.

A pasta Lua é relativa ao volume, normalmente `.cache/Lua`; o bot lê `FriendHost_Data/Jogadores` e `FriendHost_Data/Servidor` apenas desse volume. O FriendHost atual grava arquivos delimitados por ponto e vírgula em `.txt`; `.csv.txt` e `.csv` continuam compatíveis. A leitura intensa é local ou em volumes montados no dedicado. Não há descoberta global automática que alterne entre clientes ou reescreva o `.env` durante a execução do bot.

A senha RCON vem do INI do PZ dentro do volume; o host e a porta publicados são configuráveis em **RCON publicado**, incluindo o nome do INI se houver vários. Os canais e regras ficam em `/config_bot`. `GUILD_ID` é opcional e limita a importação da configuração antiga àquela guild; os comandos também são registrados globalmente.

Mapas continuam em `config/pz-maps.json`, criado a partir do exemplo. Reinicie o processo PM2 usando o nome real definido em `PM2_APP_NAME`. Consulte [README.md](README.md) para os limites das filas e [DEPLOY.md](DEPLOY.md) para migração e orçamento de memória.

## Comandos publicos

- `/online`, `/info`, `/skills`, `/traits` e `/rank`
- `/localizar_veiculo`, `/gps`, `/satelite` e `/mapas`

Os comandos dependem dos arquivos FriendHost apontados por `CSV_BASE_PATH`. A localizacao de veiculos aceita `.txt`, `.csv.txt` ou `.csv` cujo nome contenha `vehicle` dentro da pasta `Servidor`.

## Painéis e automações

- `/painel config` edita banner, thumbnail, cores, título, descrição e imagem do ranking; `/painel preview` mostra o rascunho e `/painel salvar` aplica o tema.
- `/painel jogadores vincular|desvincular` relaciona o nick PZ a uma conta Discord.
- `/painel restart|stop|recursos` controla o servidor e exibe recursos.
- `/automacao criar|listar|remover|pausar|retomar|executar|backupagora` gerencia rotinas diárias ou por dia da semana.
- `/config` (ou `/condig`) vincula o Discord ao ID/UUID do servidor Pterodactyl, com confirmação por arquivo. `/config_bot` escolhe os canais de status, ranking, chat, evolução e anticheat. As mensagens de status e ranking são atualizadas aproximadamente a cada 10 minutos, em fila distribuída entre as guilds.

As rotinas monitoram aumentos de skills, novas mortes FriendHost e os logs de chat/login/logout. Alertas PZAntiCheat são agrupados e encaminhados ao canal configurado. `WEBHOOK_CHAT` continua opcional para espelhar as mensagens do jogo.

## Fiscalização RP por call de voz

1. Em `/config`, valide o servidor PZ deste Discord e configure o endpoint RCON publicado se necessário. A fiscalização usa somente esse servidor vinculado.
2. Para cada conta, vincule o usuário PZ ao membro correto do Discord com `/painel jogadores vincular` usando o nome de login exato do PZ.
3. Como administrador, execute `/config_kick_automatico`, selecione a call obrigatória e pressione **Ativar / pausar fiscalização**. Antes de ativar, o bot valida RCON, `/players` e os vínculos dos jogadores que já estão online.

Enquanto ativo, o bot consulta `/players` a cada 10 segundos. Se uma conta PZ vinculada ficar fora da call selecionada por 60 segundos, o bot executa `kickuser` com o motivo `É necessário estar na call [nome da call] para poder permanecer no servidor.` e registra o kick no canal administrativo configurado. Jogadores novos também precisam estar vinculados antes de entrar; sem vínculo, são considerados fora da call. Pause a fiscalização no mesmo painel para interrompê-la.

Se RCON estiver indisponível, a resposta de `/players` estiver incompleta ou a call configurada não puder ser encontrada, o ciclo não aplica kicks e o bot reinicia a contagem de tolerância. A resposta `/players` precisa incluir a lista de nomes; uma contagem sem nomes não é suficiente para autorizar expulsões.

## Tickets, WL e Gemini

- `/config_bot` abre a configuração de tickets e da WL. Escolha a categoria e o canal do painel de tickets, depois publique o painel pela interface.
- Em **Config WL**, defina a lore obrigatória, os canais de auditoria e decisões e o modo manual ou automático. Aprovações e reprovações, com seus motivos, vão para o canal de decisões do Discord; nada é enviado ao chat do Project Zomboid.
- `/config_lore` importa a lore oficial por arquivo `.txt` UTF-8 em um canal privado da equipe. O limite do bot é 9.999.999 caracteres e o arquivo é comprimido no SQLite. O Discord limita cada campo de modal a 4.000 caracteres; textos maiores precisam ser anexados como arquivo.
- `/config_api` abre o painel privado da chave Gemini: crie uma chave no Google AI Studio pelo botão, configure-a no modal e selecione **Testar conexão**. A chave é cifrada no banco local `data/bot-config.sqlite3`; preserve também `data/bot-config.sqlite3.key` nos backups.
- Dentro do ticket, o jogador pode usar **Criar personagem / WL** para a história curta ou anexar um `.txt` e executar `/wl lore` para a história longa. O bot então coleta usuário PZ, personagem e senha opcional em um formulário privado; se não houver senha, gera uma.
- Quando a WL é aprovada, o canal privado de auditoria recebe a lore completa como anexo `.txt`. Administradores podem baixar o pedido mais recente de um usuário PZ com `/wl puxar_lore jogador:<nome PZ>`; a resposta é privada e lores muito grandes são divididas em arquivos `.txt` para respeitar o limite de upload do servidor Discord.
- O Gemini faz a análise narrativa da WL e registra resumo, coerências, contradições e confiança no canal de auditoria. Senha, usuário PZ e ID Discord não são enviados ao modelo. Quando faltar evidência ou o Gemini falhar, o pedido segue para revisão humana; a cota gratuita pode impor limites temporários.

## Administracao

- Wipes: `/wipe_zeds`, `/wipe`, `/wipe_force`, `/wipe_teste`, `/wipe_chunk`, `/wipe_chunk_force` e `/wipe_chunk_teste`
- Safehouses: `/safehouse listar`, `/safehouse listar_bkp_geral`, `/safehouse executar_bkp_geral` e `/safehouse restaurar_bkp_geral`
- Jogadores: `/adduser`, `/removeuserfromwhitelist`, `/banid`, `/kick`, `/kick_all`, `/godmode`, `/invisible`, `/grantadmin`, `/removeadmin`, `/tpto` e `/tp`
- Servidor: `/servermsg` e `/painel restart|stop|recursos`
- Auditoria: `/logs`

Wipes reais exigem servidor offline, confirmacao escrita e um backup completo concluido antes da exclusao. As variantes normais ignoram celulas/chunks que cruzem safehouses; as variantes `force` removem essa protecao. Sempre use o comando de teste antes do wipe real e confirme que `PZ_SAVE_ROOT` aponta exatamente para o save correto.

O backup “geral de safehouses” e propositalmente um backup completo do save. Isso evita restaurar apenas parte dos dados e deixar safehouses, jogadores e mundo fora de sincronia.

## Personalizacao dos paineis

Use `/painel config` para montar um rascunho de banner, thumbnail, cores, titulo, descricao e fundo do ranking. `/painel preview` mostra o resultado sem alterar os canais publicos; `/painel salvar` aplica o tema e edita as mensagens existentes.

Imagens e GIFs enviados sao guardados em `data/panel-media`. Links externos e links do YouTube tambem sao aceitos. Para converter videos enviados ou URLs diretas de video em thumbnail, instale `ffmpeg` na VM:

```bash
apt update
apt install -y ffmpeg
```

Digite `remover` no campo URL de uma configuracao de midia para limpa-la. Vinculos entre nick PZ e membro Discord podem ser gerenciados por `/painel jogadores vincular` e `/painel jogadores desvincular`.

O IP e o limite de slots exibidos no painel sao opcionais:

```env
SERVER_CONNECT_ADDRESS=connect.seuservidor.com:16261
SERVER_MAX_SLOTS=32
```
