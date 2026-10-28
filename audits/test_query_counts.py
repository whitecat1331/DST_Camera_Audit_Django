from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from audits.runner import _active_dst_installations
from cameras.models import Installation, InstallationDevice


class FleetQueryCountTests(TestCase):
    """Fleet pages must not query per installation (each query is ~32 ms to Supabase)."""

    def setUp(self):
        for n in range(12):
            inst = Installation.objects.create(
                ims_id=1000 + n,
                identifier=f"I-DE-{n:06d}",
                pole_number=f"{n:06d}",
                primary_platform="DE",
                serial_number=f"FX{1200 + n}",
            )
            InstallationDevice.objects.create(
                installation=inst, ims_device_id=5000 + n, unit_serial=f"FX{1300 + n}"
            )

    def test_active_dst_installations_prefetches_devices(self):
        with CaptureQueriesContext(connection) as queries:
            sites = _active_dst_installations()
            fx = [inst.fx_numbers for inst in sites]
        self.assertTrue(sites)
        self.assertTrue(all(len(f) == 2 for f in fx))  # serial + device FX both seen
        self.assertLessEqual(len(queries), 3, [q["sql"][:80] for q in queries])
