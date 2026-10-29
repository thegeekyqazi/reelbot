# reelbot

A Discord **user-installable app** that downloads Instagram reels, videos and photo posts (including carousels) and posts them as files in any chat: DMs, **group DMs** (where normal bots can't be added), and servers.

You install it to your own Discord account ("Add to my apps"), so it goes wherever you go. It's private by default: only you and the people you add to the allowlist can use your copy.

> **Self-hosting only.** This repo is the source code. There is no public hosted instance. You create your own Discord application, your own token and run your own copy.

## Features

- `/reel link:<instagram url>` posts the reel as an mp4 that everyone in the chat can see. Photo posts are posted as images.
- **Carousels**: every photo and video in the post is posted, in order. That's up to 10 attachments per message, split across messages when needed.
- **Right-click / long-press a message → Apps → Send reel** finds the first Instagram link in that message and posts its media.
- Oversized videos are re-encoded with ffmpeg to fit Discord's upload limit. Items that still don't fit are skipped, with a note.
- Downloads use [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [gallery-dl](https://github.com/mikf/gallery-dl). `/p/` posts try gallery-dl first because they may contain photos. Reels try yt-dlp first. Each falls back to the other.
- If a download or upload fails, the bot posts an embed-fixer link (default `instagramfix.com`) so the reel still plays inline.
- Allowlist: strangers get an ephemeral "This bot is private." and nothing is downloaded.
- Owner-only commands, always ephemeral:
  - `/allow user:<user>`: add someone to the allowlist
  - `/disallow user:<user>`: remove someone
  - `/allowlist`: show who's allowed

Supported links: `instagram.com/reel/…`, `/reels/…`, `/p/…`, `/tv/…`, with or without `www.` and tracking parameters.

<!-- Screenshots: add images to docs/ and link them here. -->
_Screenshots coming soon._

## Quick start (any machine)

Requirements: Python 3.11+, `ffmpeg` (with `ffprobe`), and a Discord application (see [Discord Developer Portal setup](#discord-developer-portal-setup)).

```bash
git clone https://github.com/<you>/reelbot.git
cd reelbot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then edit DISCORD_TOKEN and OWNER_ID
python -m reelbot
```

Global slash commands sync on startup. They can take a few minutes to appear in Discord the first time. Restarting the Discord client (Ctrl+R) helps.

### Configuration

All settings are environment variables. They're read from `.env` in the working directory, or from the real environment, which wins.

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | | Bot token |
| `OWNER_ID` | yes | | Your Discord user ID. Always allowed, and the only user who can manage the allowlist |
| `ALLOWED_USER_IDS` | no | | Comma-separated IDs that seed the allowlist on first run |
| `ALLOW_EVERYONE` | no | `false` | `true` disables the allowlist |
| `IG_COOKIES` | no | | Path to a Netscape-format Instagram `cookies.txt` |
| `MAX_UPLOAD_MB` | no | `10` | Upload limit. The bot also respects the smaller limit Discord reports per interaction |
| `EMBED_FIXER_DOMAIN` | no | `instagramfix.com` | Domain for the fallback link. Swap it if that service goes down |
| `DATA_DIR` | no | `./data` | Holds `allowlist.json` |
| `MAX_CONCURRENT_DOWNLOADS` | no | `2` | Keep low on small VMs |

To find your user ID: Discord Settings → Advanced → enable **Developer Mode**, then right-click your name → **Copy User ID**.

### Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests never touch the network or Discord.

## Discord Developer Portal setup

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. **Bot** tab → **Reset Token** → copy it into `.env` as `DISCORD_TOKEN`. Never share or commit it.
   No privileged intents are needed. Leave Message Content, Presence and Server Members off.
3. **Installation** tab:
   - Installation Contexts: enable **User Install**. Guild Install is optional.
   - Install Link: **Discord Provided Link**.
   - Default Install Settings → User Install → scopes: `applications.commands`.
4. Copy the install link and share it **only directly** with friends. Each friend opens it → **Add to my apps**. Then you run `/allow user:@friend`.
   If the link leaks, the allowlist still blocks strangers: they can install the app, but every command replies "This bot is private."
5. **Public Bot** (Bot tab): turning it off restricts who can install the app. Discord may then refuse to let friends user-install it. Test it with one friend. If installs fail, turn Public Bot back **on** and rely on the allowlist, which is the real access control.
   _Result for this deployment: not yet tested. Record it here once you've tried it._

## Google Cloud deployment (free tier)

This runs fine on Google Cloud's **Always Free e2-micro**. The bot only makes outbound connections, so no inbound ports or web server are needed.

### 1. Create the VM

These settings must match exactly to stay at $0:

| Setting | Value |
|---|---|
| Machine type | **e2-micro** |
| Region | **us-central1**, **us-west1** or **us-east1** only. Every other region is billed |
| Boot disk | **Standard persistent disk**, ≤ 30 GB. The console defaults to *Balanced*, which is **not** free, so change it |
| OS | Debian 12 (bookworm) |
| Firewall | Leave **Allow HTTP/HTTPS unchecked**. SSH is all you need |

Keep **only one** e2-micro in the billing account. The free allowance covers one.

### 2. Run the setup script

SSH in (`gcloud compute ssh <vm-name>` or the console's SSH button), then:

```bash
curl -fsSLO https://raw.githubusercontent.com/<you>/reelbot/main/deploy/gcp-setup.sh
REPO_URL=https://github.com/<you>/reelbot.git bash gcp-setup.sh
```

The script is idempotent, so it's safe to re-run. It:

1. installs `python3-venv`, `python3-pip`, `ffmpeg` and `git`
2. creates a 1 GB swap file if there's no swap (1 GB of RAM is tight during ffmpeg re-encodes)
3. creates the system user `reelbot` and clones the repo to `/opt/reelbot`
4. creates a venv and installs the requirements
5. creates `/opt/reelbot/.env` from `.env.example` if it's missing (`chmod 600`, owned by `reelbot`)
6. installs and enables `reelbot.service` and `ytdlp-update.timer`, which runs `ytdlp-update.service` daily to upgrade yt-dlp and gallery-dl

Then fill in your settings and start the bot:

```bash
sudo nano /opt/reelbot/.env       # DISCORD_TOKEN, OWNER_ID, ...
sudo systemctl start reelbot
journalctl -u reelbot -f
```

### Day-to-day

| Task | Command |
|---|---|
| Logs | `journalctl -u reelbot -f` |
| Restart | `sudo systemctl restart reelbot` |
| Update code | `sudo bash /opt/reelbot/deploy/update.sh` |
| Upgrade yt-dlp + gallery-dl now | `sudo systemctl start ytdlp-update.service` |
| Next auto-upgrade | `systemctl list-timers ytdlp-update.timer` |

The service runs as the unprivileged `reelbot` user with systemd hardening: `ProtectSystem=strict`, with only `/opt/reelbot/data` and a private `/tmp` writable.

### Cost guardrails

- **Billing → Budgets & alerts**: create a **$1 budget** with email alerts.
- Free egress is **1 GB/month** from North America. That's roughly 100–200 reels. Overage costs cents per GB.

## Instagram cookies

Instagram often blocks anonymous downloads from cloud IPs, especially for photo posts. If downloads fail with a login or rate-limit message, give the bot cookies from a logged-in session. The same file is used by both yt-dlp and gallery-dl:

1. Use a **throwaway Instagram account**, never your personal one. It may get flagged or banned.
2. Log in to it in a browser and export cookies in **Netscape format** with a "cookies.txt" browser extension.
3. Upload and install the cookies on the VM:
   ```bash
   gcloud compute scp cookies.txt <vm-name>:~/cookies.txt
   # then on the VM:
   sudo install -o reelbot -g reelbot -m 600 ~/cookies.txt /opt/reelbot/data/cookies.txt && rm ~/cookies.txt
   ```
4. Set `IG_COOKIES=/opt/reelbot/data/cookies.txt` in `/opt/reelbot/.env`, then `sudo systemctl restart reelbot`.

Cookie files are git-ignored. Never commit them.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Every download fails, or fails suddenly after working | Instagram changed something. Upgrade the downloaders: `sudo systemctl start ytdlp-update.service`. This also runs daily |
| Reels work but photo posts give embed links | Photo posts usually need cookies, so set `IG_COOKIES`. Also check gallery-dl is installed (`/opt/reelbot/.venv/bin/gallery-dl --version`) |
| "Instagram wants a login" | Cookies are missing or expired. Re-export them (see [Instagram cookies](#instagram-cookies)) |
| "too large" / embed link instead of video | Nothing in the post fit under the upload limit, even after compressing. Very long reels won't fit in 10 MB. The embed link still plays inline |
| "N item(s) were too large to post" | Some carousel items didn't fit and were skipped. The note links to the full post |
| Commands don't show up | Wait a few minutes after first start, then reload Discord (Ctrl+R). Check the logs for `Synced N global command(s)` |
| "This bot is private." | Expected for anyone not on the allowlist. The owner runs `/allow user:@them` |
| Bot won't start: `Missing or invalid configuration` | `DISCORD_TOKEN` or `OWNER_ID` isn't set in `.env` |
| Bot crashes during big re-encodes | Make sure swap is on (`swapon --show`) and keep `MAX_CONCURRENT_DOWNLOADS` at 1–2 |

## Limitations / out of scope

- Stories, private accounts, and other sites such as TikTok or YouTube aren't supported (yet).
- No web dashboard or database. The allowlist is a JSON file.

## Usage note

Downloading content from Instagram may violate [Instagram's Terms of Use](https://help.instagram.com/581066165581870). This project is meant for personal use among friends. Respect creators' rights and don't redistribute their content. You're responsible for how you use your copy.

## License

[MIT](LICENSE)
