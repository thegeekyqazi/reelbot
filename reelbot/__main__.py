"""Entry point: ``python -m reelbot``."""

from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

from .bot import ReelBot
from .config import ConfigError, load_config


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    load_dotenv()  # does not override variables already set (e.g. by systemd)
    try:
        config = load_config()
    except ConfigError as e:
        print(f"reelbot: {e}", file=sys.stderr)
        return 1

    bot = ReelBot(config)
    # log_handler=None: keep the logging setup above instead of discord.py's default.
    bot.run(config.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
