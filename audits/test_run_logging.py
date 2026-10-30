"""Logs and errors that survive a full run: a held-open log file, a long error list."""

import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from audits import runner
from audits.models import AuditJob
from config.logging import LockTolerantRotatingFileHandler

MEDIA = tempfile.mkdtemp(prefix="dst-run-errors-test-")


def record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("dst", logging.INFO, __file__, 1, msg, None, None)


class LockTolerantRotationTests(SimpleTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dst-log-test-"))
        self.base = self.dir / "dst.log"
        (self.dir / "dst.log.1").write_text("older backup\n", encoding="utf-8")
        self.handler = LockTolerantRotatingFileHandler(
            str(self.base), maxBytes=200, backupCount=3, encoding="utf-8", delay=True
        )
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.errors: list[logging.LogRecord] = []
        self.handler.handleError = self.errors.append

    def tearDown(self):
        self.handler.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def emit_lines(self, n: int, tag: str) -> None:
        for i in range(n):
            self.handler.emit(record(f"{tag} line {i:03d} " + "x" * 40))

    def assert_rotation_held_off(self) -> None:
        self.assertEqual(self.errors, [])
        text = self.base.read_text(encoding="utf-8")
        self.assertIn("held line 019", text)  # nothing lost while it couldn't rotate
        self.assertGreater(self.base.stat().st_size, 200)  # grew past maxBytes instead
        self.assertEqual((self.dir / "dst.log.1").read_text(encoding="utf-8"), "older backup\n")
        self.assertFalse((self.dir / "dst.log.2").exists())  # backups weren't shifted

    def test_rename_refused_keeps_writing_and_leaves_backups_alone(self):
        real_replace = os.replace
        base = str(self.base)

        def refuse_base(src, dst):
            if os.fspath(src) == base:
                raise PermissionError(32, "being used by another process")
            return real_replace(src, dst)

        with mock.patch("config.logging.os.replace", side_effect=refuse_base):
            self.emit_lines(20, "held")
        self.assert_rotation_held_off()

        # Once the other process lets go, the next due rollover rotates normally.
        self.handler._retry_at = 0.0
        self.emit_lines(1, "after")
        self.assertEqual(self.errors, [])
        self.assertIn("held line 019", (self.dir / "dst.log.1").read_text(encoding="utf-8"))
        self.assertEqual((self.dir / "dst.log.2").read_text(encoding="utf-8"), "older backup\n")

    @unittest.skipUnless(sys.platform == "win32", "Windows file locking")
    def test_real_second_handle_open(self):
        self.base.write_text("", encoding="utf-8")
        with open(self.base, encoding="utf-8"):  # a second process's handle, as runserver's reloader
            self.emit_lines(20, "held")
            self.assert_rotation_held_off()


class WriteRunErrorsTests(SimpleTestCase):
    def test_one_error_per_line(self):
        out = Path(tempfile.mkdtemp(prefix="dst-errs-"))
        try:
            path = runner._write_run_errors(out, ["a: one", "b: two"])
            self.assertEqual(path.read_text(encoding="utf-8"), "a: one\nb: two\n")
        finally:
            shutil.rmtree(out, ignore_errors=True)


@override_settings(MEDIA_ROOT=MEDIA)
class DstRunKeepsEveryErrorTests(TransactionTestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def test_ovrc_error_after_2000_chars_is_kept(self):
        errors = [f"2200{i:02d}: CBW power-on failed twice" for i in range(80)]
        errors.append("OvrC fleet: TimeoutException: customer search")
        self.assertGreater(len("; ".join(errors[:-1])), 2000)
        job = AuditJob.objects.create(
            pole_number="DST-ALL", device_type=AuditJob.DeviceType.DST_AUDIT
        )
        with (
            mock.patch.object(runner, "_run_dst_audit", return_value=(errors, 3)),
            mock.patch.object(runner, "_start_dst_auto_publish", return_value=None),
        ):
            runner._run_job(job.pk)

        job.refresh_from_db()
        self.assertEqual(job.status, AuditJob.Status.SUCCEEDED)
        self.assertIn("OvrC fleet: TimeoutException", job.error_message)
        saved = (Path(MEDIA) / "audits" / str(job.pk) / "run_errors.txt").read_text(encoding="utf-8")
        self.assertEqual(saved.splitlines(), errors)
