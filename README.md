# ClassPoint Screenshotter

Record ClassPoint slides from Discord. The bot runs headless Chromium on a Linux host, saves each distinct slide, and posts PDFs to the configured channel.

## Setup

Use Linux and Python 3.12 or later. Place the checkout at `~/classpoint-screenshotter`, or adjust the paths in the service file.

```sh
cd ~/classpoint-screenshotter
python3 -m venv .venv
.venv/bin/python -m pip install -r runner/requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
```

Installing Chromium's system dependencies may require administrator access. Drop `--with-deps` if they are already installed.

Create an application in the [Discord Developer Portal](https://discord.com/developers/applications) and invite its bot to your server:

- Use **Guild Install** with the `bot` and `applications.commands` scopes.
- Grant **View Channels**, **Send Messages**, **Attach Files**, and **Read Message History** in the output channel.
- Leave privileged intents disabled and **Interactions Endpoint URL** blank.

Copy the application ID from the portal. Enable Discord's Developer Mode to copy your server and text-channel IDs.

```sh
install -d -m 700 "$HOME/.config/classpoint-screenshotter"
install -m 600 runner/config.example.json "$HOME/.config/classpoint-screenshotter/config.json"
```

Edit `~/.config/classpoint-screenshotter/config.json`:

- Replace `application_id` and `guild_id` with your numeric IDs.
- Set `channel_id` to your text-channel ID. Alternatively, leave it `null` and set `channel_name` to a name used by exactly one text channel in that server.
- Keep the default `token_file` when using the setup helper. Both `token_file` and `data_dir` support a leading `~`.
- Set `allowed_user_ids` to restrict command access. An empty list allows everyone with access to the channel.

Save the bot token through the hidden prompt, then start the bot:

```sh
.venv/bin/python runner/setup_token.py
.venv/bin/python -m runner --config "$HOME/.config/classpoint-screenshotter/config.json"
```

The helper validates the token against `application_id` and saves it to `~/.config/classpoint-screenshotter/bot-token` with mode `600`. It also restarts the service if already installed. Keep the token out of Git, command arguments, and shell history.

The bot registers slash commands in the configured server on startup. It connects through Discord's Gateway and needs no inbound port.

## Commands

Use these in the configured channel:

| Command | Action |
| --- | --- |
| `/classpoint start code:12345` | Join with your Discord display name, truncated to 25 characters. |
| `/classpoint start code:12345 name:Your name` | Join with the supplied classroom name. |
| `/classpoint status` | Show recording state, slide count, and pending uploads. |
| `/classpoint stop` | Finish recording and upload the PDFs. |
| `/classpoint retry` | Retry pending uploads, skipping completed parts. |

One class records at a time. Only the starter or a member with **Manage Server** permission can stop it; the allowlist also applies. Replies and progress posts are public in the channel. Progress posts default to every 10 minutes, starting 10 minutes after joining.

Use only for classes you have permission to record and share.

## Service

Stop the foreground process before installing the systemd user service:

```sh
install -d "$HOME/.config/systemd/user"
install -m 644 runner/classpoint-screenshotter.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now classpoint-screenshotter
```

To run after logout and at boot, an administrator may need to enable lingering with `loginctl enable-linger YOUR_USERNAME`.

```sh
systemctl --user status classpoint-screenshotter --no-pager
journalctl --user -u classpoint-screenshotter -n 50 --no-pager
systemctl --user restart classpoint-screenshotter
systemctl --user stop classpoint-screenshotter
```

## Capture and storage

The recorder captures the slide area and compares decoded pixels against earlier captures in the session. It skips duplicates, waiting screens, sharing-disabled placeholders, and slides covered by dialogs. Edits and annotations produce new images. Short-lived slides and continuous animations can be missed because capture requires stable content.

A return to the join screen or an end/removal notice ends recording after a grace period. Leaving slideshow mode alone does not end it; use `/classpoint stop` if the class stays open.

| Setting | Default |
| --- | --- |
| `poll_seconds` | 1 second |
| `settle_seconds` | 0.6 seconds |
| `end_grace_seconds` | 15 seconds |
| `disconnect_grace_seconds` | 120 seconds |
| `max_session_hours` | 12 hours |
| `max_slides` | 3000 slides |
| `min_free_mb` | 512 MiB |
| `status_update_seconds` | 600 seconds |

Each PDF page holds one slide at its original aspect ratio. PDFs split to fit the server's attachment limit. Failed uploads retry every minute. After a restart, interrupted recordings are queued as partial sessions without rejoining the class. Upload reconciliation checks the latest 200 channel messages; duplicates after a crash are still possible.

Files stay under `data_dir`:

```text
<code>-<session-id>/
  images/slide-0001.png
  pdfs/classpoint-...-part-01.pdf
  manifest.json
```

Manifests contain classroom names, class codes, Discord IDs, timestamps, and upload state. Files are not deleted automatically. Monitor disk usage and remove old recordings as needed. Credentials, deployment config, recordings, environments, and logs are excluded by `.gitignore`; keep custom output paths out of Git too.

## Tests

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m unittest discover -s tests -v
```

The tests use a local ClassPoint fixture and simulated Discord interactions. They cover cropping, deduplication, annotations, stabilization, end detection, recovery, PDF fidelity and splitting, upload retries, command scope, and progress posts. They do not join a live class or send messages.

ClassPoint interface changes can break the selectors in `runner/capture.py` and `runner/slide.js`. The fixture tests do not check compatibility with the live site.
