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
| `/classpoint start code:12345` | Join as `saumil's assistant` by default. |
| `/classpoint start code:12345 name:Your name` | Join with the supplied classroom name. |
| `/classpoint start code:12345 export:sampled` | Sample rapid scene changes in the PDF; retain all original PNGs. |
| `/classpoint status` | List active recordings, last saved images, connection state, slots, and PDF/upload progress. |
| `/classpoint status code:12345` | Inspect one class. |
| `/classpoint stop code:12345` | Finish that recording and upload its PDFs. |
| `/classpoint stop` | Stop your only active recording; specify a code when ambiguous. |
| `/classpoint retry` | Retry pending uploads, skipping completed parts. |

Up to three different classes record concurrently by default. Each has an independent browser, stop event, and progress task. Duplicate starts for the same class code are rejected. PDF generation and uploads share one queue. Only the starter or a member with **Manage Server** permission can stop it; the allowlist also applies. Replies and progress posts are public in the channel. Progress posts default to every 10 minutes, starting 10 minutes after joining. Status includes the last saved image time; after 30 minutes without a new image, posts include an inactivity reminder. Recording continues during pauses and repeated content.

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

The recorder renders the slide area at up to twice the displayed resolution when the source image has more pixels. It includes visible annotations and records the source dimensions and capture scale in the manifest. It checks after slide image loads and updates in addition to polling, then rechecks new content after the settle window. Brief slides and continuous animations can still be missed. It skips duplicates, waiting screens, sharing-disabled placeholders, and slides covered by dialogs.

Deduplication also recognizes the PowerPoint navigation strip that sometimes appears at the bottom left. The entire patch must match the white strip template or a verified pair of toolbar-on and toolbar-off pixels. The rest of the slide is compared pixel for pixel. Different footer layouts and additional ink can produce separate captures, and saved PNGs retain their original pixels.

A return to the join screen or an end/removal notice ends recording after a grace period. Leaving slideshow mode alone does not end it; use `/classpoint stop` if the class stays open.

| Setting | Default |
| --- | --- |
| `poll_seconds` | 1 second between fallback checks |
| `settle_seconds` | 0.6 seconds |
| `max_capture_scale` | 2 (configurable from 1 to 2) |
| `end_grace_seconds` | 15 seconds |
| `disconnect_grace_seconds` | 120 seconds |
| `max_session_hours` | 12 hours |
| `max_slides` | 3000 slides |
| `min_free_mb` | 512 MiB |
| `status_update_seconds` | 600 seconds |
| `max_concurrent_sessions` | 3 (configurable from 1 to 10) |
| `inactivity_notice_seconds` | 1800 seconds |

Each PDF page holds one selected capture losslessly at its original aspect ratio. PDF generation encodes each image once, caches approximately one attachment's worth of pages, and checks each finished file against the server's attachment limit. Images are neither resized nor JPEG-compressed. Failed uploads retry every minute. After a restart, interrupted recordings are queued as partial sessions without rejoining the class. Upload reconciliation fetches the latest 200 channel messages once per attempt; duplicates after a crash are still possible. Rebuilt PDFs have unique filenames, acknowledged parts are preserved, and failed builds clean up their outputs. A storage error while recording upload failure does not stop the retry worker.

Files stay under `data_dir`:

```text
<code>-<session-id>/
  images/slide-0001.png
  pdfs/classpoint-...-part-01.pdf
  manifest.json
```

Manifests contain classroom names, class codes, Discord IDs, timestamps, and upload state. Files are not deleted automatically. Monitor disk usage and remove old recordings as needed. Credentials, deployment config, recordings, environments, and logs are excluded by `.gitignore`; keep custom output paths out of Git too.

## PDF selection and local exports

`export:all` is the default and includes every unique captured image. Exact deduplication preserves small annotations and treats distinct video frames as new content.

`export:sampled` reduces long bursts of rapid visual changes in the PDF. A burst requires at least eight images, consecutive gaps of at most six seconds, and large changes in at least 40% of the transitions. A large change means at least 25% of a 64×36 thumbnail differs by more than 32 in an RGB channel. The PDF keeps the burst's first and last images and samples at intervals of at least 30 seconds. Small edits alone do not trigger sampling.

Sampling is a visual change heuristic: it can also sample rapidly changing slides and omit annotations within a qualifying burst. Choose `all` when every intermediate state is needed. Every original PNG remains saved in either mode. The manifest records each PDF part's source image numbers.

To export a finished recording again locally, including an older session:

```sh
.venv/bin/python -m runner.export data/<session-directory> --mode all
.venv/bin/python -m runner.export data/<session-directory> --mode sampled --max-mb 9.5
```

These commands create a new folder under the session's `exports/` directory with the PDFs and `export.json`, including source image numbers. They do not change the original manifest or Discord uploads. `--output /path/to/new-folder` selects another destination; an existing destination is refused.

## Tests

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m unittest discover -s tests -v
```

The tests use a local ClassPoint fixture and simulated Discord interactions. They cover cropping, deduplication, navigation controls, one-pixel annotations, stabilization, end detection, chronological recovery, concurrent recording ownership, stop permissions, shutdown, PDF fidelity and attachment limits, sampling, upload retries/reconciliation, failed storage, interrupted export workers, command scope, and progress posts. They do not join a live class or send messages.

ClassPoint interface changes can break the selectors in `runner/capture.py` and `runner/slide.js`. The fixture tests do not check compatibility with the live site.
