"""Back up or reset the v2 database (stop the server first for a reset).

    python tools/db_tools.py backup            copy to <db folder>/backups/catino-v2-<time>.db (safe while running)
    python tools/db_tools.py reset             move the database aside as a backup; the next start makes a fresh one
    python tools/db_tools.py list              show the backups

The database comes from KITTY_DB_PATH, like the server. The v1 file (data/kittycat.db) is refused, and
nothing is ever deleted: a reset only renames the file.
"""

import sqlite3
import sys
import time

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.connection import DB_PATH, V1_DB_PATH   # noqa: E402


def _check(path: Path) -> None:
    if path.resolve() == V1_DB_PATH.resolve():
        raise SystemExit("Refusing to touch the v1 database (data/kittycat.db)")
    if not path.is_file():
        raise SystemExit(f"No database at {path}")


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def backup(path: Path = DB_PATH) -> Path:
    _check(path)
    folder = path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{path.stem}-{_stamp()}.db"
    src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)                       # consistent copy even while the server writes
    finally:
        dst.close()
        src.close()
    return target


def reset(path: Path = DB_PATH) -> Path:
    _check(path)
    target = backup(path)
    for suffix in ("", "-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.exists():
            side.rename(side.with_name(side.name + f".reset-{_stamp()}"))
    return target


def main(argv: list[str]) -> None:
    command = argv[0] if argv else "backup"
    if command == "backup":
        print("Backup written:", backup())
    elif command == "reset":
        print("Old database kept as", reset(), "and moved aside. The next server start creates a fresh one.")
    elif command == "list":
        folder = DB_PATH.parent / "backups"
        for item in sorted(folder.glob("*.db")) if folder.is_dir() else []:
            print(item.name, item.stat().st_size, "bytes")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
