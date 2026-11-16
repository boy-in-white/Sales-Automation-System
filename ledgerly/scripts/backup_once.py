#!/usr/bin/env python3
"""Create and verify one configured database backup."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from backup import create_backup, prune_backups  # noqa: E402


def main() -> None:
    path = create_backup()
    if path is None or not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("Database backup was not created")
    removed = prune_backups()
    print(f"Backup created: {path} ({path.stat().st_size} bytes); pruned={removed}")


if __name__ == "__main__":
    main()

