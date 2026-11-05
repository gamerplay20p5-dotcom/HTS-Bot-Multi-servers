# Rankings configuráveis e leitura TTF com ACL / Ranking selection and TTF ACL reads

## Português

### Configurar os rankings no Discord

Abra **`/config` → Rankings publicados** ou a mesma seção em **`/config_bot`**. Marque apenas os rankings desejados. A seleção é salva por comunidade no SQLite, sobrevive a reinícios e atualiza o painel pela fila existente. O **Preview do ranking** usa exatamente a mesma seleção do painel automático.

O padrão compacto é **Zumbis mortos + Horas sobrevividas**, inclusive para configurações antigas que ainda não possuem essa preferência. Distância, veículos, armas quebradas, mortes, facções, destaque da facção e habilidades no nível 10 são opcionais. Se desmarcar todos, apenas o resumo é publicado. Cada tabela mostra até cinco posições. A seleção controla os painéis; `/rank` continua disponível para consultas individuais a outras métricas.

As tabelas e consultas de ranking exibem valores inteiros: `1250.00` aparece como **1.250** em PT-BR ou **1,250** em inglês. Valores fracionários são arredondados somente na apresentação; os arquivos originais, a agregação e a ordenação mantêm a precisão. O total de sobrevivência também é apresentado sem casas decimais. A identidade visual e a linguagem da comunidade continuam aplicadas.

### Habilidades no nível 10

O ranking ordena **jogadores pela quantidade de habilidades distintas no nível 10**. Exemplo: um jogador com culinária, força e carpintaria no nível 10 recebe três pontos. Empates são ordenados pelo nome da conta. SteamID, username e outras colunas de metadados não contam como habilidades.

O dado vem da **última linha do arquivo `playerperks_<conta>.txt` do FriendHost**, aceitando também as extensões legadas. Não soma personagens antigos nem conserva níveis máximos de vidas anteriores. Apenas colunas de habilidades conhecidas com valor exatamente 10 entram na contagem. Contas sem esse nível não ocupam posições; se nenhum arquivo compatível existir, o resultado fica indisponível, sem inventar zeroes.

Para consultar a posição: `/rank jogador:<conta> tipo:skills10`. Esse ranking usa o escopo de conta e o snapshot atual. O TTF Stats MP v1 fornecido não exporta níveis de habilidades: selecionar essa opção com o provedor TTF exibe **Indisponível**. O bot não mistura silenciosamente dados FriendHost e TTF.

Arquivos de perks só são consultados pelo painel quando esse ranking está selecionado. Facções e mortes também deixam de ser lidas pelo painel quando suas opções estão desmarcadas. Os leitores independentes de eventos continuam funcionando. Cache, filas e isolamento por volume são preservados.

### Correção permanente para ACL de travessia

`manguadu/stats/files.py` abre diretórios intermediários com:

```python
directory_flags = (
    getattr(os, "O_PATH", os.O_RDONLY)
    | os.O_DIRECTORY
    | os.O_NOFOLLOW
)
```

O mesmo conjunto de flags é aplicado à raiz e a cada diretório relativo. No Linux, `O_PATH` permite usar o descritor para resolução relativa sem exigir a leitura/listagem do diretório; a travessia do caminho continua sujeita às permissões correspondentes. O arquivo final é aberto com `O_RDONLY | O_NOFOLLOW | O_NONBLOCK` e validado como arquivo regular. Mantêm-se os testes de confinamento ao volume, proteção contra symlinks e limites de leitura. A checagem de symlinks também inclui o diretório imediatamente acima da fonte de estatísticas.

Em sistemas POSIX sem `O_PATH`, o fallback conserva `O_RDONLY`, `O_DIRECTORY`, `O_NOFOLLOW` e `dir_fd`. Se o sistema não conseguir atravessar um diretório sem acesso de leitura, a operação falha com diagnóstico; não amplia ACLs nem tenta uma abertura que contorne o confinamento. O fallback existente para plataformas sem essas APIs continua verificando os caminhos. Referências: [Linux `open(2)`, seção O_PATH](https://man7.org/linux/man-pages/man2/open.2.html), [constantes e operações `os` do Python](https://docs.python.org/3.14/library/os.html).

O operador informou que a alteração aplicada no Node0 eliminou o `PermissionError` e permitiu ler 15 contas e 40 personagens do Runners. Essa é a validação de produção relatada pelo operador. Nesta sessão, a implementação foi verificada com simulações de permissões e arquivos temporários; não houve conexão ao Node0, alteração de credenciais, permissões Wings ou reinício de PZ.

### Investigar registros inválidos sem modificar os originais

Abra **`/config` → Diagnosticar integração** para um resumo. O botão **Registros TTF inválidos** anexa um JSON privado com:

- Nome relativo do arquivo e linha correspondente do `index.txt`.
- Código do erro, campo envolvido e linha do arquivo, quando conhecidos.
- Explicação em PT-BR/EN, contagem por motivo e indicação de truncamento.
- Contagens de contas, personagens, registros inválidos e cache anterior.

São distinguidos: linha de índice malformada, identificador inseguro, arquivo ausente, versão incompatível, divergência de personagem/conta, booleano inválido, número negativo/não finito/inválido, UTF-8 inválido, arquivo grande, gravação incompleta, mudança durante a leitura e permissão negada. Os detalhes são limitados aos primeiros **50 registros**, enquanto as contagens abrangem todos os registros inválidos da varredura. Não são incluídos valores brutos das linhas nem caminhos absolutos. O diagnóstico não apaga, corrige ou regrava arquivos do mod.

A contagem de quatro inválidos do Runners, sozinha, não determina quatro causas reais. Use esse relatório para examinar os arquivos correspondentes; a implementação não relaxa a validação para ocultar o contador. Arquivos válidos posteriores são incorporados normalmente; cache anterior usado por gravações incompletas continua identificado como antigo.

### Testes e atualização

A suíte local resultou em **137 testes aprovados, 1 pulado e nenhuma falha**, com 361 avisos de depreciação do ambiente/dependências. O teste pulado exige criar um symlink nativo, restrito pela conta Windows. Os novos testes de permissões usam um backend que simula `openat` e ACLs e executam também no Windows: diretórios somente de travessia, ausência de `O_PATH`, leitura negada do arquivo final, liberação de descritores, symlinks e outro volume. Também cobrem preferências persistentes por guild, permissões administrativas, preview/publicação, exclusão de leituras desnecessárias, contagem de habilidades, números sem casas decimais, diagnóstico limitado e integridade dos arquivos originais.

No Node0, o arquivo `stats/files.py` já foi editado manualmente segundo o relato do operador. Antes de atualizar o checkout, preserve e revise essa diferença local: o atualizador do bot rejeita alterações rastreadas para evitar perda de trabalho. As configurações Discord continuam no SQLite existente. Publicar no GitHub não atualiza nem reinicia o processo de produção automaticamente.

## English

### Choose community rankings

Open **`/config` → Published rankings**, also available in `/config_bot`. Select the tables to publish; SQLite stores the choice per guild. The automatic panel and **Ranking preview** share that preference and use the existing queue. The compact default is **Zombie kills + Survival hours** for installations without a saved preference. Other tables are optional. Deselecting everything keeps only the summary; each table shows at most five entries. Individual `/rank` queries remain available independently of the broadcast selection.

Ranking numbers are integers: `1250.00` becomes **1,250** in English or **1.250** in Portuguese. Fractional values are rounded for display only; source files, aggregation and ordering retain their original precision.

### Level-10 skills

Players are ordered by the number of distinct known skills at exactly level 10, with account-name tie breaking. The latest FriendHost `playerperks_<account>` snapshot is used, including legacy extensions; metadata columns and previous lives are excluded. Only accounts with qualifying skills appear. Missing skill exports are unavailable rather than fabricated zeroes. Query `/rank` with `type:skills10` and account scope. The supplied TTF Stats MP v1 export has no skill levels, so this table is explicitly unavailable when TTF is selected. Providers are never silently combined.

Disabled skills, faction and death tables do not trigger their auxiliary panel reads. Independent event readers, guild isolation, metadata caches and staggered queues remain intact.

### Traversal-only ACL fix

Directory descriptors now prefer `getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW`, for both the anchor and every intermediate directory. Linux can resolve known children through traversal-only directories without requesting directory-listing permission. Final files still require ordinary read permission and must be regular files; no-follow flags, bounded reads and volume confinement remain enforced. The immediate parent of the statistics root is also checked for symlinks.

POSIX systems without `O_PATH` retain the confined read-only/no-follow fallback. If directory permissions are insufficient there, access fails safely; the bot does not broaden ACLs or bypass confinement. Existing fallback path checks remain for platforms without these descriptor APIs. Official Linux/Python references are linked above.

The operator reported successful live validation on Node0 after applying this change: 15 accounts, 40 characters, no `PermissionError`. This session incorporated the fix and tested simulated ACLs and temporary files; it did not connect to Node0 or change game-server permissions, credentials or running processes.

### Inspect invalid records

Use **Diagnose integration** for a summary and **Invalid TTF records** for a private JSON attachment. It contains relative filenames, index/record line numbers, stable reason codes, affected fields and translated explanations. Detailed samples are capped at 50; reason totals cover every invalid record in the scan. Reasons distinguish malformed indexes, missing files, unsupported versions, identity mismatches, invalid booleans/numbers/UTF-8, oversized/incomplete/changed files, unsafe paths and denied permissions. Raw line values and absolute paths are omitted; original files are never rewritten or deleted.

The four invalid Runners records cannot be classified from the aggregate count alone. The new report provides evidence for inspecting them without hiding errors or weakening validation. Subsequent valid writes recover normally; reused historical cache remains flagged stale.

Local verification: **137 passed, 1 skipped, no failures**, with 361 environment/dependency deprecation warnings. The skipped test requires a native Windows symlink; simulated descriptor/ACL tests run on Windows and cover traversal-only access, fallback behavior, final-file permissions, cleanup, symlinks and foreign volumes. New tests also cover guild preferences, admin permissions, scheduled/preview parity, optional reads, level-10 counts, integer rendering, bounded diagnostics and unchanged source files.

Node0 already has a manual edit to `stats/files.py`. Preserve/review that tracked difference before updating: the updater intentionally stops on tracked local changes. Existing Discord preferences remain in SQLite. GitHub publication does not deploy or restart production services.
