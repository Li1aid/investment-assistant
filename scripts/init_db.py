"""Create the SQLite database file and apply schema."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_db_path, init_schema


def main() -> None:
    path = get_db_path()
    init_schema(path)
    print(f"[ok] schema applied to {path}")


if __name__ == "__main__":
    main()
