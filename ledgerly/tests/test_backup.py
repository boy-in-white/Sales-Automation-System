from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backup


class BackupTests(unittest.TestCase):
    def test_failed_postgres_backup_leaves_no_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            error = subprocess.CalledProcessError(
                1,
                ["pg_dump"],
                stderr="connection failed",
            )
            with (
                patch.object(backup, "DATABASE_URL", "postgresql://user:pass@localhost/test"),
                patch.object(backup, "BACKUP_DIR", Path(directory)),
                patch("backup.shutil.which", return_value="/usr/bin/pg_dump"),
                patch("backup.subprocess.run", side_effect=error),
            ):
                with self.assertRaisesRegex(RuntimeError, "connection failed"):
                    backup.create_backup()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_postgres_backup_retries_without_gzip_when_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls: list[list[str]] = []

            def run(command: list[str], **_kwargs: object) -> None:
                calls.append(list(command))
                if len(calls) == 1:
                    raise subprocess.CalledProcessError(
                        1,
                        command,
                        stderr="this build does not support compression with gzip",
                    )
                Path(command[command.index("--file") + 1]).write_bytes(b"backup")

            with (
                patch.object(backup, "DATABASE_URL", "postgresql://user:pass@localhost/test"),
                patch.object(backup, "BACKUP_DIR", Path(directory)),
                patch("backup.shutil.which", return_value="/usr/bin/pg_dump"),
                patch("backup.subprocess.run", side_effect=run),
            ):
                result = backup.create_backup()

            self.assertIsNotNone(result)
            self.assertEqual(calls[0][2], "--compress=6")
            self.assertEqual(calls[1][2], "--compress=0")


if __name__ == "__main__":
    unittest.main()
