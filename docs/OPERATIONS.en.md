# HTS Bot Multi servers — English operations guide

**Created by Malaio/Erick.** [Português](../README.md) · [Repository](https://github.com/gamerplay20p5-dotcom/HTS-Bot-Multi-servers)

## Purpose and architecture

HTS Bot Multi servers is a Python Discord bot for managing multiple Project Zomboid communities on one dedicated host. Each Discord guild is bound to one PZ server using its Pterodactyl identifier and full Wings volume UUID. One shared Client API key accesses the authorized servers; configuration, files, themes, player links, automations and event state are isolated per guild.

The public product is **HTS Bot Multi servers**, the Python distribution is `hts-bot-multi-servers`, and the Python module remains `manguadu`. Start it with `python -m manguadu --bot`. The FriendHost mod retains its own name. Changing the Discord application's visible name requires the owner to edit it in the Developer Portal.

## Installation

Requirements: Python 3.11+, a Discord bot token, a Pterodactyl Client API key with access to the managed servers, and readable local Wings volumes. PM2 is optional process supervision. `ffmpeg` is optional for video thumbnails.

```bash
git clone --branch feature/hts-multi-servers https://github.com/gamerplay20p5-dotcom/HTS-Bot-Multi-servers.git
cd HTS-Bot-Multi-servers
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
# Edit .env with the shared credentials.
python -m manguadu --bot
```

On Windows, use `python -m venv .venv` and `.venv\Scripts\Activate.ps1`. Existing installations switching from the old distribution should uninstall that package before installing this editable package; preserve `.env` and `data/`.

Global configuration: `DISCORD_TOKEN`, `DISCORD_CLIENT_ID`, `PTERO_URL`, `PTERO_API_KEY`, and optionally `PTERO_VOLUMES_ROOT` (default `/var/lib/pterodactyl/volumes`). `GUILD_ID` is optional and identifies the only guild allowed to import legacy single-server settings. Commands are also registered globally.

## Bind a server to a Discord

1. Invite the bot with access to the configured channels, message/attachment permissions and permission to manage ticket channels.
2. As a guild administrator, run `/config` (`/condig` is an alias). Select **Cadastrar / editar servidor**.
3. Enter the Pterodactyl identifier, full Wings volume UUID, display label and Lua directory relative to that volume, normally `.cache/Lua`.
4. In that PZ server's Pterodactyl file manager, create `friendhost-discord-link.txt` at the root and paste the private code. Select **Validar vínculo** within 15 minutes. The API UUID must also match the supplied volume UUID.
5. Use **Canais, tickets e WL** or `/config_bot` to configure chat, evolution, status, rankings, anticheat, ticket category/panel, staff role, private audit and decisions channels.
6. Select **RCON publicado** to set that server's published host/TCP port. The bot reads `RCONPassword` from the server INI inside its own volume; select the INI name if several exist. Containers sharing a host IP require distinct published ports, even if their internal ports match.

```text
PTERO_VOLUMES_ROOT/
└── SERVER-UUID/
    ├── friendhost-discord-link.txt
    └── .cache/
        ├── Lua/FriendHost_Data/Jogadores/
        ├── Lua/FriendHost_Data/Servidor/
        ├── Server/servertest.ini
        ├── Logs/
        └── Saves/Multiplayer/servertest/
```

One UUID can belong to only one Discord. Changing to another volume requires an operator migration to preserve pending tickets, whitelist state and automations. A guild without a valid binding cannot fall back to another customer's server. Unavailable volumes require correcting access and restarting or validating the binding again.

This release needs local or mounted Wings volumes. Remote Wings hosts must mount their volumes under the configured root. Pterodactyl is used for verification/control; the bot does not continuously download all remote logs and player files through the API. Legacy `config/servers.json` remains available for CLI tools, while bound guilds use their own server exclusively.

## Features and command reference

Command names and Discord interface labels remain Portuguese. The tables explain their behavior in English. Permissions and required parameters are shown by Discord's slash-command interface.

| Commands | Behavior |
|---|---|
| `/config`, `/condig` | Bind the PZ server, configure published RCON endpoint, inspect log backlog. |
| `/config_bot` | Configure channels, staff role, private tickets, whitelist rules and voice enforcement. |
| `/config_api` | Store/test a per-guild encrypted Gemini API key through a private interface. |
| `/config_lore` | Import official server lore as UTF-8 `.txt`. |
| `/wl lore` | Upload a character's long lore in the player's open ticket and continue the private application form. |
| `/wl puxar_lore jogador:<PZ username>` | Staff-only private download of the latest submitted lore, including request status. |
| `/online` | List online players recorded by FriendHost. |
| `/info`, `/skills`, `/traits`, `/rank` | Player profile, skills, traits and rank queries. |
| `/status`, `/statuscomplete` | Detailed player snapshots; the complete variant can inspect additional local records. |
| `/painel config`, `/painel preview`, `/painel salvar` | Edit draft title/colors/media, preview panels and publish the guild's theme. |
| `/painel jogadores vincular`, `/painel jogadores desvincular` | Associate/disassociate a PZ username and Discord member. |
| `/painel restart`, `/painel stop`, `/painel recursos`, `/servidor` | Server power and resource information. |
| `/rcon` | Send a server console command using the bound server's endpoint; relevant operations can use Pterodactyl console fallback. |
| `/automacao criar`, `listar`, `remover`, `pausar`, `retomar`, `executar`, `backupagora` | Per-guild schedules and immediate automation/backup operations. |
| `/config_kick_automatico` | Configure required RP voice channel and enable/pause automatic kicks. |
| `/mapas`, `/gps`, `/satelite`, `/localizar_veiculo` | Configured maps and FriendHost player/vehicle location. |
| `/logs` | Bounded searches in that server's native logs. |
| `/safehouse listar`, `listar_bkp_geral`, `executar_bkp_geral`, `restaurar_bkp_geral` | Safehouse listing and full-save backup/restore operations. |
| `/wipe_zeds`, `/wipe`, `/wipe_force`, `/wipe_teste`, `/wipe_chunk`, `/wipe_chunk_force`, `/wipe_chunk_teste` | Wipe planning, simulation and execution. Real wipes require an offline server, explicit confirmation and backup. Normal variants protect intersecting safehouses. |
| `/adduser`, `/removeuserfromwhitelist`, `/banid`, `/kick`, `/kick_all` | Account, whitelist and moderation commands. |
| `/godmode`, `/invisible`, `/grantadmin`, `/removeadmin`, `/tpto`, `/tp`, `/servermsg` | PZ player/admin utilities and server messages. |
| `/deletearquivo` | Delete selected FriendHost outputs for regeneration; destructive administrative operation. |
| `/bot` | Update/maintain the shared instance. Restricted to the Discord application owner because it affects all guilds. |

### Tickets, whitelist and AI

Tickets are private channels for the applicant and staff. Players supply a PZ account, character name, optional password and lore. An omitted password is generated. After successful RCON `adduser`, credentials are delivered by DM, or privately inside the ticket if DMs are unavailable.

Configure manual review or automatic triage in **Config WL**. With a Gemini key, analysis considers narrative coherence, chronology and contradictions and produces a staff summary. Without a key, local lexical triage is used. Gemini failures or insufficient evidence are routed to human review. AI decisions are not guaranteed correct, and free-tier availability/quotas may change. The code limits text excerpts sent to the model; long-document analysis does not imply every character was inspected by AI.

Approval/rejection results appear in the configured Discord decisions channel, outside the game's chat. The private audit channel receives the reasoning and an approved character's complete downloadable lore. Oversized exports are split into UTF-8 `.txt` parts to fit the guild upload limit. The bot accepts up to 9,999,999 characters and 40 MiB for lore uploads; Discord's own limits still apply.

### RP voice enforcement

Link exact PZ usernames with `/painel jogadores vincular` before activation. The bot queries RCON `players`, compares accounts with members in the required voice channel and starts a 60-second grace period for absence. Unlinked accounts are considered absent; currently online unlinked players prevent initial activation. The kick reason names the required call. Returning to the call clears the timer. If RCON, the list or voice channel cannot be verified, enforcement skips the cycle and clears grace timers. Queue/network delays can extend the grace period.

## Scheduling and optimization

| Resource | Implemented behavior |
|---|---|
| Panels | Approximately every 600 seconds with deterministic, different phases per guild; edits existing messages. |
| Queues | Three general workers and two independent log workers. Duplicate pending jobs are avoided; failed jobs back off. |
| Files | Four file threads. Player CSV reads use the header plus the final 256 KiB; cached by file size/mtime. |
| Cache | Up to 2,048 entries/approximately 4 MiB estimated per guild. This is not a total process RSS limit. |
| Logs | Every 5 seconds, up to 1 MiB/2,000 lines per read; log rotation discovery every 30 seconds. Chat is batched and excess is summarized. Full logs remain on PZ. No startup replay of old chat. |
| Death history | Incremental 4 MiB batches and persisted count/cursor state, up to 50,000 aggregated names. Large initial histories may take several cycles to catch up. |
| API/RCON | Reused HTTP connection pool, up to three Pterodactyl requests and six RCON connections concurrently. HTTP 429 introduces a shared wait. |
| Auxiliary reads | Direct reads/API responses capped at 8 MiB; larger files need incremental processing or maintenance. |
| Long lore | Compressed disk drafts instead of large strings retained by views; two long operations concurrently. |

Intervals are targets, not deadlines: upstream failures, rate limits and workload can delay updates. The `/config` panel shows log bytes pending. Logs use their own queue so a slow control operation cannot block all log processing.

The local synthetic benchmark used **25 servers × 200 players × 200 historical rows**, with three concurrent scans. First scan/ranking render: **31.202 s**; cached repetition: **4.986 s**; measured Python allocation peak: **6.06 MiB**. [Recorded results](../artifacts/multiserver-benchmark.json). These Windows development measurements exclude total process RSS, production network traffic and actual dedicated-host load. They are not a guarantee of production capacity or RAM usage below 2.56 GB.

```bash
python -m pytest -q
python -m compileall -q manguadu scripts
python -m scripts.benchmark_multiserver --servers 25 --players 200 --history 200
python -m manguadu
python -m manguadu --probe
```

The 60-test suite includes guild data isolation, unique volume ownership, proof validation, bounded concurrency, independent log progress, UTF-8 boundaries, persistent death cursors and lore export. Measure RSS, CPU and backlog under real traffic before increasing production load.

## Deployment, backups and recovery

```bash
pm2 start "$PWD/.venv/bin/python" --name hts-bot-multi-servers --interpreter none -- -m manguadu --bot
pm2 startup
pm2 save
pm2 logs hts-bot-multi-servers --lines 100
```

Set `PM2_APP_NAME` to the actual process name and `BOT_UPDATE_BRANCH=feature/hts-multi-servers` while deploying this branch; use `main` after merging it. Existing PM2 names may remain unchanged. Run a single bot process for the token.

Back up `.env`, `data/bot-config.sqlite3`, `data/bot-config.sqlite3.key` and `data/guilds/`. The database stores guild configuration and encrypted Gemini keys; its key file is needed to decrypt them. Guild directories contain themes, player links, automations, anticheat state, cursors and compressed drafts. No production data or credentials should be committed.

On a dedicated systemd service, `MemoryHigh=2G` and `MemoryMax=2500M` provide a conservative memory budget; hitting the hard cap can terminate the process. PM2's `--max-memory-restart 2200M` restarts above a threshold rather than enforcing a hard limit. Avoid limiting an entire PM2 service that also runs unrelated apps. Verify memory behavior on the production host; 1% of 256 GB is a target, not a measured guarantee.

If FriendHost files are absent, check the bound UUID, relative Lua folder, mod output and OS permissions. If RCON fails, check published TCP host/port and that server's INI password. If audit uploads fail, check attachment permissions and guild limits. If a volume is unavailable, restore its mount/access and restart or validate the binding again; another guild's data is never substituted.
