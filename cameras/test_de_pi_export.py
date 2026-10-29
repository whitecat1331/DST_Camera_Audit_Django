"""Tests for DragonEye Post-Install image pack export."""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings

from audits.models import AuditJob, AuditScreenshot
from services.de_pi_export import (
    build_de_pi_pack_zip,
    build_de_pi_tv_zip,
    clear_site_documents_root_cache,
    export_filename,
    pole_folder_candidates,
    site_documents_status,
)


def _png_bytes() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )


class ExportFilenameTests(SimpleTestCase):
    def test_prefers_fx(self):
        self.assertEqual(
            export_filename("P1", "ID", "FX1403"),
            "FX1403_post_install_images.zip",
        )


class PoleCandidatesTests(SimpleTestCase):
    def test_prefers_digits(self):
        cands = pole_folder_candidates("266134", "I-DE-266134", "FX1403")
        self.assertEqual(cands[0], "266134")


class BuildDePiPackZipTests(TestCase):
    def test_includes_tv_and_site_docs(self):
        job = AuditJob.objects.create(
            pole_number="266134",
            target_host="FX1403",
            device_type=AuditJob.DeviceType.DE_BUNDLE,
            status=AuditJob.Status.SUCCEEDED,
        )
        for label in ("de_l1", "de_l2"):
            shot = AuditScreenshot(job=job, label=label)
            shot.image.save(f"{label}.png", ContentFile(_png_bytes()), save=True)

        root = Path(tempfile.mkdtemp(prefix="de-pi-site-docs-"))
        pole = root / "266134"
        photos = pole / "Site Photos"
        photos.mkdir(parents=True)
        (photos / "north.png").write_bytes(_png_bytes())
        # New Install screenshots must be ignored.
        shots = pole / "FX" / "New Install" / "screenshots"
        shots.mkdir(parents=True)
        (shots / "01_prtg.png").write_bytes(_png_bytes())

        with override_settings(DE_PI_SITE_DOCUMENTS_ROOT=str(root)):
            data, names, counts = build_de_pi_pack_zip(
                pole_number="266134",
                identifier="I-DE-266134",
                fx_number="FX1403",
            )

        self.assertEqual(counts["tv"], 2)
        self.assertEqual(counts["site_photos"], 1)
        self.assertNotIn("screenshots", counts)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            listing = sorted(zf.namelist())
        self.assertIn("tv/de_l1.png", listing)
        self.assertIn("tv/de_l2.png", listing)
        self.assertIn("site_photos/north.png", listing)
        self.assertFalse(any(n.startswith("screenshots/") for n in listing))
        self.assertTrue(any(n.startswith("tv/") for n in names))

    def test_tv_only_still_works(self):
        job = AuditJob.objects.create(
            pole_number="POLE1",
            device_type=AuditJob.DeviceType.DE_BUNDLE,
            status=AuditJob.Status.SUCCEEDED,
        )
        shot = AuditScreenshot(job=job, label="de_tv")
        shot.image.save("de_tv.png", ContentFile(_png_bytes()), save=True)
        data, names = build_de_pi_tv_zip("POLE1")
        self.assertTrue(data[:2] == b"PK")
        self.assertTrue(any("de_l1" in n or "de_tv" in n for n in names))

    def test_missing_raises(self):
        with self.assertRaises(ValueError):
            build_de_pi_pack_zip(pole_number="MISSING")


class SiteDocumentsStatusTests(SimpleTestCase):
    def setUp(self):
        clear_site_documents_root_cache()

    def tearDown(self):
        clear_site_documents_root_cache()

    def test_missing_root(self):
        with override_settings(DE_PI_SITE_DOCUMENTS_ROOT=r"C:\no-such-site-docs-root-xyz"):
            status = site_documents_status("266134")
        self.assertFalse(status["root_found"])
        self.assertFalse(status["ok"])

    def test_found_with_images(self):
        root = Path(tempfile.mkdtemp(prefix="de-pi-status-"))
        pole = root / "266134"
        photos = pole / "Site Photos"
        photos.mkdir(parents=True)
        (photos / "north.png").write_bytes(_png_bytes())
        # Screenshots outside Site Photos must not count.
        shots = pole / "FX" / "New Install" / "screenshots"
        shots.mkdir(parents=True)
        (shots / "01_prtg.png").write_bytes(_png_bytes())

        with override_settings(DE_PI_SITE_DOCUMENTS_ROOT=str(root)):
            status = site_documents_status("266134", "I-DE-266134", "FX1403")

        self.assertTrue(status["root_found"])
        self.assertTrue(status["pole_found"])
        self.assertTrue(status["ok"])
        self.assertEqual(status["site_photos"], 1)
        self.assertNotIn("screenshots", status)

    def test_pole_folder_empty(self):
        root = Path(tempfile.mkdtemp(prefix="de-pi-status-empty-"))
        (root / "266134").mkdir()
        with override_settings(DE_PI_SITE_DOCUMENTS_ROOT=str(root)):
            status = site_documents_status("266134")
        self.assertTrue(status["pole_found"])
        self.assertFalse(status["ok"])
        self.assertEqual(status["site_photos"], 0)
