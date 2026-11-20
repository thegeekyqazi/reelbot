# Spec: Instagram Reel → Discord App ("reelbot")

## 1. Goal

Build a Discord **user-installable app** that downloads Instagram reels and posts them as video files in any Discord chat, including **group DMs** (where normal bots can't be added).

- Publish **all source code to a new public GitHub repo** so anyone can self-host their own copy.
- Deploy **one private hosted instance** on Google Cloud (Always Free e2-micro). It belongs to the owner.
- The owner can let **specific friends** install and use the hosted instance. Nobody else can use it, even if they find the install link or the repo.

## 2. How users interact

The app is installed to a user's account ("Add to my apps"), so it works in DMs, group DMs and servers.

| Action | Result |
|---|---|
| `/reel link:<instagram url>` | Bot posts the reel as an mp4 in the current chat, visible to everyone |
| Long-press / right-click a message → Apps → **Send reel** | Finds the first Instagram link in that message and posts the video |
| Download fails or file too large | Bot posts the link rewritten to an embed fixer (`instagramfix.com`) so the reel still plays inline, plus a short note |
| User not on allowlist | Ephemeral reply: "This bot is private." Nothing is downloaded |

Owner-only commands, which are ephemeral and restricted to `OWNER_ID`:
- `/allow user:<user>`: add a friend to the allowlist
- `/disallow user:<user>`: remove a friend
- `/allowlist`: show allowed users

## 3. Tech stack

- Python 3.11+, `discord.py` ≥ 2.4 (needs `allowed_installs` / `allowed_contexts` support), `yt-dlp`, `python-dotenv`
- `ffmpeg` / `ffprobe` (system package) to shrink videos over the upload limit
- Gateway connection only. No inbound ports or web server needed.
- No Message Content intent (`discord.Intents.default()` is enough)

## 4. Repo structure

```
reelbot/
├── README.md               # what it is, self-hosting guide, GCP guide, privacy/ToS note
├── LICENSE                 # MIT
├── .gitignore              # .env, cookies*.txt, data/, __pycache__, .venv
├── .env.example            # every env var, with placeholder values and comments
├── requirements.txt
├── reelbot/
│   ├── __init__.py
│   ├── __main__.py         # entry point: python -m reelbot
│   ├── config.py           # loads + validates env vars
│   ├── bot.py              # client, command tree, commands, context menu
│   ├── downloader.py       # yt-dlp wrapper, ffmpeg shrink
│   ├── allowlist.py        # allowlist storage (JSON file in DATA_DIR)
│   └── links.py            # URL regex, embed-fixer rewrite
├── tests/
│   ├── test_links.py
│   └── test_allowlist.py
└── deploy/
    ├── gcp-setup.sh        # one-shot VM setup script (run on the VM)
    ├── reelbot.service     # systemd unit
    ├── ytdlp-update.service
    ├── ytdlp-update.timer  # daily yt-dlp upgrade
    └── update.sh           # git pull + pip install + restart
```

## 5. Configuration (env vars, from `.env` on the server)

| Var | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token |
| `OWNER_ID` | yes | Owner's Discord user ID; always allowed, only one who can manage allowlist |
| `ALLOWED_USER_IDS` | no | Comma-separated IDs to seed the allowlist on first run |
| `ALLOW_EVERYONE` | no | `true` disables the allowlist (for self-hosters who want it open). Default `false` |
| `IG_COOKIES` | no | Path to a Netscape-format Instagram cookies.txt |
| `MAX_UPLOAD_MB` | no | Default `10` |
| `EMBED_FIXER_DOMAIN` | no | Default `instagramfix.com` |
| `DATA_DIR` | no | Default `./data` (holds `allowlist.json`) |
| `MAX_CONCURRENT_DOWNLOADS` | no | Default `2` (the VM only has 1 GB RAM) |

`config.py` must fail fast with a clear message if required vars are missing.

## 6. Behaviour details

### Commands
- Every command and the context menu is decorated with:
  - `@app_commands.allowed_installs(guilds=True, users=True)`
  - `@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)`
- Owner commands use the same contexts, check `interaction.user.id == OWNER_ID`, and always respond ephemerally.
- Commands sync globally in `setup_hook` (not `on_ready`, to avoid re-syncing on reconnect).

### Access control (critical)
- Check the allowlist **first** in every reel handler, before deferring or downloading.
- Allowed = `OWNER_ID`, or ID in allowlist, or `ALLOW_EVERYONE=true`.
- Allowlist persists in `DATA_DIR/allowlist.json`. Writes are atomic (write temp file, then rename).

### Reel flow
1. Validate the link with a regex matching `instagram.com/(reel|reels|p|tv)/<id>`, with or without `www.`. Strip tracking query params.
2. `await interaction.response.defer(thinking=True)` (non-ephemeral, so the final video is public).
3. Download with yt-dlp inside `asyncio.to_thread`, into a `tempfile.TemporaryDirectory`.
   - Format: `best[ext=mp4]/best`, `noplaylist`, quiet, `cookiefile` if set.
   - Guard with an `asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)`.
   - Timeout of about 90 s.
4. If the file is larger than the limit and ffmpeg exists, re-encode to fit. Target 90% of the limit, use 96 kbps AAC audio, scale to max 720 px wide, `-preset veryfast`, `+faststart`.
5. Send with `interaction.followup.send(file=discord.File(path, "reel.mp4"))`.
6. On any failure (download error, still too big, upload error), follow up with the embed-fixer link, e.g. `https://instagramfix.com/reel/ABC123/`, and a short reason. Never dump raw tracebacks to chat. Log them instead.

### Logging
- Use Python `logging` to stdout (journald captures it).
- Log user ID, link and outcome. Never log the token or cookie contents.

## 7. Security / privacy requirements

- **The public repo must never contain secrets.** That means no `.env`, cookies, `data/`, tokens, the owner's user ID, the hosted bot's application ID, or its install link.
- `.gitignore` covers all of the above. Before the first push, run a check (e.g. `git grep -nE "[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"` for Discord token shapes) and confirm `git status` shows no secret files.
- The README explains that self-hosters create their **own** Discord application and token. The repo never refers to the owner's hosted instance.
- The hosted instance's private install link is shared only directly with friends. Even if it leaks, the allowlist blocks strangers.

## 8. Google Cloud deployment (owner's hosted instance)

### VM settings (Always Free, must match exactly to stay at $0)
- Machine type: **e2-micro**
- Region: **us-central1, us-west1 or us-east1** only (owner is in Canada; any non-US region is billed)
- Boot disk: **Standard persistent disk**, ≤ 30 GB. The console defaults to *Balanced*, which is NOT free, so this must be changed.
- OS: Debian 12
- Firewall: leave HTTP/HTTPS unchecked. No inbound ports needed besides SSH.
- Only one e2-micro in the account

### `deploy/gcp-setup.sh` (idempotent, run on the VM as a sudo user)
1. `apt install -y python3-venv python3-pip ffmpeg git`
2. Create a 1 GB swap file if none exists (1 GB RAM is tight during ffmpeg re-encodes)
3. Create system user `reelbot` and clone the repo to `/opt/reelbot`
4. Create a venv and `pip install -r requirements.txt`
5. Create `/opt/reelbot/.env` from `.env.example` if missing, `chmod 600`, owned by `reelbot`. Print a reminder to fill it in.
6. Install and enable `reelbot.service`, `ytdlp-update.service` and `ytdlp-update.timer`

### `reelbot.service`
- Runs `python -m reelbot` as user `reelbot`, `WorkingDirectory=/opt/reelbot`, `EnvironmentFile=/opt/reelbot/.env`
- `Restart=always`, `RestartSec=10`
- Hardening: `NoNewPrivileges=true`, `ProtectSystem=strict`, `ReadWritePaths=/opt/reelbot/data /tmp`, `PrivateTmp=true`

### `ytdlp-update.timer`
- Runs daily: `pip install -U yt-dlp` in the venv, then restarts `reelbot` (Instagram breakages are usually fixed by a yt-dlp update)

### Cost guardrails (document in README)
- Set a $1 budget alert in Billing
- Free egress is 1 GB/month (North America), roughly 100–200 reels. Overage costs cents.

## 9. Discord Developer Portal setup (manual, owner does this; document in README)

1. Create a new application → **Bot** tab → reset/copy the token into `.env`.
2. **Installation** tab:
   - Installation contexts: enable **User Install** (Guild Install optional).
   - Install link: **Discord Provided Link**.
   - User Install default scope: `applications.commands`.
3. Share the install link only with friends. Each friend opens it → **Add to my apps**. The owner then runs `/allow` for them.
4. **Bot** tab → "Public Bot": test whether turning it off blocks friends' *user* installs. If it does, leave it on, since the allowlist is the real access control. Record the result in the README.

## 10. Instagram cookies (manual, document in README)

- Use a **throwaway Instagram account**, never a personal one. It may get flagged.
- Export cookies in Netscape format with a browser extension, upload to the VM (`gcloud compute scp`), put at `/opt/reelbot/data/cookies.txt`, `chmod 600`, owned by `reelbot`, set `IG_COOKIES` to that path.
- Only needed if downloads fail without it (likely on a cloud IP).

## 11. Tests

- `test_links.py`: regex accepts reel/reels/p/tv URLs with and without `www.` and query strings, rejects non-Instagram URLs, and the embed-fixer rewrite produces the correct URL.
- `test_allowlist.py`: add/remove/persist/reload, atomic write, owner always allowed, `ALLOW_EVERYONE` override.
- No tests hit the network or Discord.

## 12. Tasks for Claude Code (in order)

1. Scaffold the repo per section 4 and implement sections 5–7.
2. Write tests and make them pass (`pytest`).
3. Write the README covering: features, screenshots placeholder, self-hosting quick start, Developer Portal steps (section 9), GCP guide (section 8), cookies (section 10), troubleshooting (update yt-dlp, cookies expired, file too large), and a usage note that downloading from Instagram may violate its Terms of Use and is meant for personal use.
4. Run the secret check from section 7.
5. Create the **public** GitHub repo `reelbot` with `gh repo create reelbot --public --source . --push`, then confirm the pushed tree contains no secrets.
6. Print a short checklist of the manual steps left for the owner (Developer Portal, GCP VM creation, filling `.env`, cookies, inviting friends).

## 13. Out of scope

- Carousels with multiple videos (send only the first video; mention this in the README)
- Stories, private accounts, TikTok/YouTube (yt-dlp could support them later)
- Web dashboard or database
