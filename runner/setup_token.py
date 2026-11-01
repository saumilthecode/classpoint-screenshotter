"""Prompt for a bot token and save it to a private file."""

import getpass
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request


def main():
    directory = Path.home() / ".config" / "classpoint-screenshotter"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = getpass.getpass("Discord Bot token (hidden): ").strip()
    if not token or any(character.isspace() for character in token):
        raise SystemExit("No valid token supplied; nothing changed.")
    config_path = directory / "config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        request = urllib.request.Request(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": "Bot " + token, "User-Agent": "ClassPointScreenshotter/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                identity = json.load(response)
        except (urllib.error.URLError, ValueError):
            raise SystemExit("Discord could not validate that token. Nothing was saved; try again.") from None
        if int(identity["id"]) != config["application_id"] or not identity.get("bot"):
            raise SystemExit("That token belongs to a different app. Nothing was saved.")
    temporary = directory / "bot-token.new"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(token + "\n")
    temporary.chmod(0o600)
    temporary.replace(directory / "bot-token")
    print("Token saved privately. No token was printed or put in shell history.")
    unit = Path.home() / ".config/systemd/user/classpoint-screenshotter.service"
    if unit.exists():
        subprocess.run(["systemctl", "--user", "reset-failed", "classpoint-screenshotter"], check=False)
        result = subprocess.run(["systemctl", "--user", "restart", "classpoint-screenshotter"], check=False)
        if result.returncode == 0:
            print("Runner started. Use /classpoint start in #classpoint-screenshotter.")
            return
    print("Start the runner with: systemctl --user restart classpoint-screenshotter")


if __name__ == "__main__":
    main()
