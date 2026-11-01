import argparse
import asyncio
import logging
import os
from pathlib import Path
import signal
import sys

from .bot import ClassPointBot
from .config import Config


async def run(config, token):
    client = ClassPointBot(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(client.close()))
    async with client:
        await client.start(token)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Discord ClassPoint runner")
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/classpoint-screenshotter/config.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = Config.load(args.config)
        token = config.token()
    except (OSError, ValueError, TypeError) as error:
        logging.error("Configuration incomplete: %s. Run runner/setup_token.py for a missing token.", error)
        return 78
    try:
        asyncio.run(run(config, token))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
