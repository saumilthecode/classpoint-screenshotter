# ClassPoint Screenshotter

A Discord bot that joins a ClassPoint class in headless Chromium, captures unique slides, and uploads them as PDFs when the recording ends. It runs on a Linux host; users control it with Discord slash commands.

## What is included

- `runner/`: the Discord bot, browser capture, slide detection, session storage, PDF generation, and upload/retry code.
- `runner/config.example.json`: configuration template with placeholder Discord IDs.
- `runner/setup_token.py`: interactive token setup and validation.
- `runner/classpoint-screenshotter.service`: a systemd user service.
- `tests/`: offline browser fixtures and tests for capture, PDFs, recovery, uploads, command scope, and progress messages.

You supply a Discord application and bot token, a server/channel, a Linux host, and a ClassPoint class you have permission to record and share. Credentials, deployment configuration, recorded slides, PDFs, and session metadata are local files and are excluded from this repository.

## Install

Use Python 3.12 or later. The service file expects this checkout at `~/classpoint-screenshotter`; adjust its paths if you install elsewhere.

From the checkout directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r runner/requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
```

Installing Chromium's operating system dependencies may require administrator access. For a host where those dependencies are already installed, use `.venv/bin/python -m playwright install chromium`.

Create a Discord application in the [Developer Portal](https://discord.com/developers/applications) and add its bot to your server:

1. Enable **Guild Install** with scopes `bot` and `applications.commands`.
2. Grant **View Channels**, **Send Messages**, **Attach Files**, and **Read Message History** for the output channel. No administrator permission or privileged intents are needed. Leave **Interactions Endpoint URL** blank.
3. Copy your application, server, and output-channel IDs. Discord's Developer Mode exposes **Copy ID** in context menus.

Create a private configuration:

```sh
install -d -m 700 "$HOME/.config/classpoint-screenshotter"
install -m 600 runner/config.example.json "$HOME/.config/classpoint-screenshotter/config.json"
```

Edit `~/.config/classpoint-screenshotter/config.json`:

- Replace `application_id` and `guild_id` with your numeric Discord IDs; the template's zero values are placeholders.
- Set `channel_id` to your numeric text-channel ID. When it is `null`, the bot requires exactly one channel matching `channel_name` in the configured server.
- Set `data_dir` and `token_file` to your chosen paths. Leading `~` is supported for these two settings. The token helper saves to `~/.config/classpoint-screenshotter/bot-token`, so keep that token path when using the helper.
- Optionally set `allowed_user_ids` to a list of permitted Discord user IDs. An empty list allows anyone with access to the configured channel to use the commands, subject to the stop restriction below.

Store the bot token using the hidden prompt:

```sh
.venv/bin/python runner/setup_token.py
```

With the configuration above in place, the helper checks that the token belongs to the configured application. It saves the token with mode `600`. If this project's systemd service is already installed, the helper also restarts it.

Run in the foreground:

```sh
.venv/bin/python -m runner --config "$HOME/.config/classpoint-screenshotter/config.json"
```

The bot connects through Discord's Gateway, so it needs no inbound port. Slash commands are registered for the configured server on startup.

## Run as a service

After confirming the foreground run works, stop it and install the user service:

```sh
install -d "$HOME/.config/systemd/user"
install -m 644 runner/classpoint-screenshotter.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now classpoint-screenshotter
```

To keep the user service running after logout and start it at boot, an administrator may need to enable lingering for your account with `loginctl enable-linger YOUR_USERNAME`.

```sh
systemctl --user status classpoint-screenshotter --no-pager
journalctl --user -u classpoint-screenshotter -n 50 --no-pager
systemctl --user restart classpoint-screenshotter
systemctl --user stop classpoint-screenshotter
```

The service reads the token from the private file. Keep tokens out of the service unit, command arguments, shell history, and Git.

## Use in Discord

Run commands in the configured channel:

- `/classpoint start code:12345`: join using your Discord display name, truncated to 25 characters.
- `/classpoint start code:12345 name:Your name`: choose the classroom name.
- `/classpoint status`: show recording state, slide count, and pending uploads.
- `/classpoint stop`: finish and upload the PDF. Only the starter or someone with **Manage Server** permission can stop the recording; the configured allowlist also applies.
- `/classpoint retry`: wake up the upload retry loop. Completed PDF parts are skipped.

One class records at a time. Replies and progress posts are visible to everyone in the channel. Progress posts start after 10 minutes and repeat at `status_update_seconds` intervals (default `600`) until recording ends.

The recorder saves stable, slide-only PNGs. It compares decoded pixels against all slides in that session, so repeated content is saved once and small edits or annotations are kept. Sharing-disabled placeholders, dialogs, participant UI, and waiting screens are excluded. Polling and stabilization mean very brief slides or continuous animations may be missed.

Returning to the join screen or displaying an end/removal notice for `end_grace_seconds` (default `15`) ends the recording. A presenter leaving slideshow mode alone does not end it. Use `/classpoint stop` if the class remains open. Lost connections get a recovery window of `disconnect_grace_seconds` (default `120`); sessions are capped at `max_session_hours` (default `12`).

Each PDF page contains one slide at its original aspect ratio. Large recordings split into numbered PDFs based on the server's attachment limit. Uploads retry every minute. After a restart, interrupted recordings are finalized as partial sessions and queued for upload; the bot does not rejoin the class. Upload reconciliation checks the latest 200 channel messages, which reduces duplicates after a crash but does not guarantee exactly-once delivery.

## Local data and limits

Files remain under the configured `data_dir`:

```text
<code>-<session-id>/
  images/slide-0001.png
  images/slide-0002.png
  pdfs/classpoint-...-part-01.pdf
  manifest.json
```

Manifests contain class codes, classroom names, Discord IDs, timestamps, and upload state. Original images, PDFs, and manifests are never automatically deleted. Monitor disk usage and arrange your own retention policy. Recording stops at `max_slides` (default `3000`) or before free space drops below `min_free_mb` (default `512`). Keep recordings and deployment configuration out of Git, even if you choose custom output paths.

## Development checks

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m unittest discover -s tests -v
```

Tests serve ClassPoint browser requests from local fixtures and simulate Discord interactions and uploads. They do not join a live class or send Discord messages. They cover slide cropping, exact deduplication, annotations, stabilization, lifecycle detection, recovery, PDF fidelity/splitting, upload retries, command scope, and timed progress posts.

The captured-page selectors target ClassPoint's rendered interface and may need updating when that interface changes. The offline tests verify behavior against the included fixture; they do not establish compatibility with future ClassPoint changes.
