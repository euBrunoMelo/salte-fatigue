"""Contrato headless do pacote generico de sincronizacao."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401
from _bootstrap import ROOT


SYNC_DIR = ROOT / "sync"
SCRIPT = SYNC_DIR / "monitoramento-sync.sh"
FILTER = SYNC_DIR / "rsync_filter.conf"


class TestSyncStaticContract(unittest.TestCase):
    def test_only_current_versioned_assets_are_present(self) -> None:
        required = (
            "VERSION",
            "monitoramento-sync.sh",
            "monitoramento-sync.service",
            "monitoramento-sync.timer",
            "99-monitoramento-sync",
            "rsync_filter.conf",
            "wifi-manager-logrotate",
            "install-monitoramento-sync.sh",
        )
        legacy = ("salte-sync.sh", "salte-sync.service", "salte-sync.timer", "99-salte-sync", "install.sh")
        for name in required:
            self.assertTrue((SYNC_DIR / name).is_file(), name)
        for name in legacy:
            self.assertFalse((SYNC_DIR / name).exists(), name)
        self.assertEqual((SYNC_DIR / "VERSION").read_text(encoding="utf-8").strip(), "1.1.3")

    def test_script_has_single_root_single_rsync_and_safe_flags(self) -> None:
        content = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("/mnt/nvme/Monitoramento/", content)
        self.assertIn("salte@office.salte.me:./", content)
        self.assertEqual(len(re.findall(r'^\s*"\$RSYNC_BIN"\s+"\$\{rsync_args\[@\]\}"', content, re.MULTILINE)), 1)
        self.assertIn("--archive", content)
        self.assertIn("--ignore-existing", content)
        self.assertIn("--delay-updates", content)
        self.assertIn("--partial-dir=.rsync-partial", content)
        self.assertIn("--filter=merge $FILTER_FILE", content)
        self.assertIn("|%i|%l|%n", content)
        self.assertIn('SOURCE_ROOT="${SOURCE_ROOT%/}/"', content)
        for forbidden in ("--delete", "--remove-source-files", "--inplace", "--dry-run"):
            self.assertNotIn(forbidden, content)

    def test_script_has_lock_ssh_timeouts_logging_and_env_overrides(self) -> None:
        content = SCRIPT.read_text(encoding="utf-8")
        for required in (
            "flock -n",
            "BatchMode=yes",
            "StrictHostKeyChecking=accept-new",
            "ConnectTimeout=$CONNECT_TIMEOUT",
            "--timeout=$RSYNC_TIMEOUT",
            "logger}",
            "MONITORAMENTO_SYNC_SOURCE",
            "MONITORAMENTO_SYNC_DESTINATION",
            "MONITORAMENTO_SYNC_STATUS_FILE",
            "MONITORAMENTO_SYNC_RSYNC_BIN",
            "MONITORAMENTO_SYNC_MAX_ATTEMPTS",
            "MONITORAMENTO_SYNC_RETRY_DELAY",
        ):
            self.assertIn(required, content)
        for secret_risk in ("sshpass", "password=", "set -x"):
            self.assertNotIn(secret_risk, content.lower())

    def test_status_is_atomic_and_contains_required_fields(self) -> None:
        content = SCRIPT.read_text(encoding="utf-8")
        for field in (
            "last_attempt",
            "last_success",
            "duration",
            "eligible_files",
            "files_transferred",
            "bytes_transferred",
            "attempts",
            "rsync_exit_code",
            "destination",
        ):
            self.assertIn(f'"{field}"', content)
        self.assertIn("mktemp", content)
        self.assertRegex(content, r'mv -f -- "\$STATUS_TEMP" "\$STATUS_FILE"')
        self.assertNotIn("rsync --dry-run", content)

    def test_filter_excludes_unsafe_paths_before_log_inclusions(self) -> None:
        rules = [line.strip() for line in FILTER.read_text(encoding="utf-8").splitlines()]
        exclusions = (
            "- *.partial/", "- *.partial", "- *.partial.avi", "- *.tmp",
            "- .rsync-partial/", "- quarantine/",
            "- /DrowsyDriving/logs/eventos.csv",
        )
        inclusions = ("+ */logs/", "+ */logs/**")
        for rule in exclusions + inclusions:
            self.assertIn(rule, rules)
        first_inclusion = min(rules.index(rule) for rule in inclusions)
        self.assertTrue(all(rules.index(rule) < first_inclusion for rule in exclusions))

    def test_systemd_and_dispatcher_contract(self) -> None:
        service = (SYNC_DIR / "monitoramento-sync.service").read_text(encoding="utf-8")
        timer = (SYNC_DIR / "monitoramento-sync.timer").read_text(encoding="utf-8")
        dispatcher = (SYNC_DIR / "99-monitoramento-sync").read_text(encoding="utf-8")
        self.assertIn("RequiresMountsFor=/mnt/nvme/Monitoramento", service)
        self.assertRegex(service, r"TimeoutStartSec=\S+")
        self.assertIn("OnCalendar=*-*-* *:0/5:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("AccuracySec=1s", timer)
        self.assertIn('"${2:-}" = "up"', dispatcher)
        self.assertIn("systemctl --no-block start monitoramento-sync.service", dispatcher)

    def test_installer_only_copies_new_assets_without_unit_lifecycle(self) -> None:
        content = (SYNC_DIR / "install-monitoramento-sync.sh").read_text(encoding="utf-8")
        for asset in ("monitoramento-sync.sh", "rsync_filter.conf", "monitoramento-sync.service", "monitoramento-sync.timer", "99-monitoramento-sync", "wifi-manager-logrotate"):
            self.assertIn(asset, content)
        self.assertNotRegex(content, r"systemctl\s+(enable|disable|start|stop|restart)")
        self.assertNotIn("salte-sync.sh", content)

    def test_wifi_manager_log_has_bounded_rotation(self) -> None:
        content = (SYNC_DIR / "wifi-manager-logrotate").read_text(encoding="utf-8")
        for required in ("/var/log/wifi_manager.log", "daily", "maxsize 10M", "rotate 7", "compress", "copytruncate"):
            self.assertIn(required, content)

    def test_readme_documents_current_sync_only(self) -> None:
        content = (SYNC_DIR / "README_OFFLOAD.md").read_text(encoding="utf-8")
        self.assertIn("monitoramento-sync.timer", content)
        for asset in ("salte-sync.sh", "salte-sync.service", "salte-sync.timer", "99-salte-sync", "install.sh"):
            self.assertNotIn(f"`{asset}`", content)


class TestRsyncFilterSelection(unittest.TestCase):
    @unittest.skipUnless(shutil.which("rsync"), "rsync nao instalado")
    def test_local_dry_run_selects_only_final_log_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            selected = (source / "ProductA/logs/final.jsonl", source / "ProductB/logs/video.mp4")
            excluded = (
                source / "ProductA/logs/writing.partial",
                source / "ProductA/logs/staged.tmp",
                source / "ProductA/logs/batch.tmp/piece.json",
                source / "ProductA/logs/chunk.partial/piece.bin",
                source / "ProductA/logs/.rsync-partial/staged.bin",
                source / "ProductA/logs/quarantine/rejected.bin",
                source / "ProductA/cache/not-a-log.bin",
                source / "DrowsyDriving/logs/eventos.csv",
                source / "DrowsyDriving/logs/writing.partial.avi",
            )
            destination.mkdir()
            for path in selected + excluded:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"contract")

            completed = subprocess.run(
                (
                    shutil.which("rsync") or "rsync",
                    "--archive",
                    "--dry-run",
                    "--prune-empty-dirs",
                    "--out-format=%n",
                    f"--filter=merge {FILTER}",
                    f"{source}/",
                    f"{destination}/",
                ),
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertIn("ProductA/logs/final.jsonl", completed.stdout)
            self.assertIn("ProductB/logs/video.mp4", completed.stdout)
            for path in excluded:
                self.assertNotIn(path.name, completed.stdout)

    @unittest.skipUnless(shutil.which("rsync"), "rsync nao instalado")
    def test_script_copies_locally_and_writes_transfer_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            final = source / "Product/logs/final.jsonl"
            collision = source / "Product/logs/collision.jsonl"
            partial = source / "Product/logs/writing.partial"
            temporary = source / "Product/logs/staged.tmp"
            temporary_child = source / "Product/logs/batch.tmp/piece.json"
            open_video = source / "DrowsyDriving/logs/writing.partial.avi"
            mutable_csv = source / "DrowsyDriving/logs/eventos.csv"
            final.parent.mkdir(parents=True)
            destination.mkdir()
            final.write_bytes(b"final-log")
            collision.write_bytes(b"new-content")
            partial.write_bytes(b"not-final")
            temporary.write_bytes(b"not-final")
            temporary_child.parent.mkdir()
            temporary_child.write_bytes(b"not-final")
            open_video.parent.mkdir(parents=True)
            open_video.write_bytes(b"not-final")
            mutable_csv.write_bytes(b"mutable")
            destination_collision = destination / "Product/logs/collision.jsonl"
            destination_collision.parent.mkdir(parents=True)
            destination_collision.write_bytes(b"immutable-existing")
            status = root / "state/status.json"
            environment = os.environ | {
                "MONITORAMENTO_SYNC_SOURCE": str(source),
                "MONITORAMENTO_SYNC_DESTINATION": f"{destination}/",
                "MONITORAMENTO_SYNC_FILTER_FILE": str(FILTER),
                "MONITORAMENTO_SYNC_STATUS_FILE": str(status),
                "MONITORAMENTO_SYNC_LOCK_FILE": str(root / "state/sync.lock"),
                "MONITORAMENTO_SYNC_LOGGER_BIN": "/bin/true",
            }

            completed = subprocess.run(
                ("bash", str(SCRIPT)),
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((destination / "Product/logs/final.jsonl").is_file())
            self.assertEqual(destination_collision.read_bytes(), b"immutable-existing")
            self.assertFalse((destination / "Product/logs/writing.partial").exists())
            self.assertFalse((destination / "Product/logs/staged.tmp").exists())
            self.assertFalse((destination / "Product/logs/batch.tmp").exists())
            self.assertFalse((destination / "DrowsyDriving/logs/writing.partial.avi").exists())
            self.assertFalse((destination / "DrowsyDriving/logs/eventos.csv").exists())
            report = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(report["eligible_files"], 2)
            self.assertEqual(report["files_transferred"], 1)
            self.assertEqual(report["bytes_transferred"], len(b"final-log"))
            self.assertEqual(report["attempts"], 1)
            self.assertEqual(report["rsync_exit_code"], 0)
            self.assertEqual(report["destination"], f"{destination}/")
            self.assertIsNotNone(report["last_success"])

    def test_script_propagates_transfer_failure_into_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source/Product/logs"
            source.mkdir(parents=True)
            (source / "final.log").write_text("final", encoding="utf-8")
            fake_rsync = root / "fake-rsync"
            fake_rsync.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
            fake_rsync.chmod(0o755)
            status = root / "state/status.json"
            previous_success = "2026-09-11T11:55:02Z"
            status.parent.mkdir()
            status.write_text(
                json.dumps({"last_success": previous_success}),
                encoding="utf-8",
            )
            environment = os.environ | {
                "MONITORAMENTO_SYNC_SOURCE": str(root / "source"),
                "MONITORAMENTO_SYNC_DESTINATION": "unavailable.invalid:/archive/",
                "MONITORAMENTO_SYNC_FILTER_FILE": str(FILTER),
                "MONITORAMENTO_SYNC_STATUS_FILE": str(status),
                "MONITORAMENTO_SYNC_LOCK_FILE": str(root / "state/sync.lock"),
                "MONITORAMENTO_SYNC_LOGGER_BIN": "/bin/true",
                "MONITORAMENTO_SYNC_RSYNC_BIN": str(fake_rsync),
                "MONITORAMENTO_SYNC_MAX_ATTEMPTS": "1",
                "MONITORAMENTO_SYNC_RETRY_DELAY": "0",
            }

            completed = subprocess.run(
                ("bash", str(SCRIPT)),
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(completed.returncode, 23)
            report = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(report["rsync_exit_code"], 23)
            self.assertEqual(report["last_success"], previous_success)
            self.assertEqual(report["eligible_files"], 1)


if __name__ == "__main__":
    unittest.main()
