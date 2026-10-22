"""KEY=value lines from KittyAPI/.env.local (untracked; local settings and secrets such as TICKET_SECRET).
Variables already set in the environment win."""

import os

from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env.local"


def load_local_env(path: Path = ENV_FILE) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())
