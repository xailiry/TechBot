"""Import this FIRST in every test module (before any techhunter import).

Redirects the whole suite to a throwaway SQLite file so tests never touch
the production data/techhunter.db. The file is recreated fresh per process.
"""
import asyncio
import os
os.environ["PROFIT_HAGGLE_PERCENT"] = "0.0"
import pathlib

_DB = pathlib.Path(__file__).resolve().parent.parent / "data" / "test_suite.db"
os.environ["DB_PATH"] = str(_DB)
os.environ["DB_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["DB_URL_SYNC"] = f"sqlite:///{_DB}"

for ext in ("", "-wal", "-shm"):
    p = pathlib.Path(str(_DB) + ext)
    if p.exists():
        p.unlink()

from techhunter.db import create_all  # noqa: E402

asyncio.run(create_all())
