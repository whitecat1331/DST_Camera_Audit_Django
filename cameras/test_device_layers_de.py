"""Tests for DragonEye L/R → L1/L2 lane detection and TV layer slots."""

from __future__ import annotations

from django.test import SimpleTestCase, TestCase

from cameras.models import DragonEyeTeamViewerId, Installation, InstallationDevice
from services.device_layers import _lane_token, _lanes_from_devices, build_device_layers
from services.dragoneye_ids import parse_fx_label


class LaneTokenTests(SimpleTestCase):
    def test_explicit_l1_l2(self):
        self.assertEqual(_lane_token("FX1070 L1 Sheridan"), "L1")
        self.assertEqual(_lane_token("camera L2"), "L2")

    def test_directional_fx_lr(self):
        self.assertEqual(_lane_token("FX1403L EB CPU .215"), "L1")
        self.assertEqual(_lane_token("FX1403R WB CPU .225"), "L2")
        self.assertEqual(_lane_token("FX1403 L EB"), "L1")

    def test_fx_l1_glued_not_directional(self):
        self.assertEqual(_lane_token("FX1074L1"), "L1")


class ParseFxLabelTests(SimpleTestCase):
    def test_directional(self):
        self.assertEqual(parse_fx_label("FX1403L EB"), ("FX1403", "L1", "EB"))
        self.assertEqual(parse_fx_label("FX1403R WB"), ("FX1403", "L2", "WB"))

    def test_l1(self):
        self.assertEqual(parse_fx_label("FX1070 L1 Sheridan"), ("FX1070", "L1", "Sheridan"))


class BuildDragoneyeTvSlotsTests(TestCase):
    def test_two_tv_edit_slots_for_lr_cpus(self):
        inst = Installation.objects.create(
            ims_id=266134,
            identifier="I-DE-266134",
            primary_platform="DE",
            pole_number="266134",
            serial_number="FX1403",
            is_active=True,
        )
        InstallationDevice.objects.create(
            installation=inst,
            ims_device_id=1,
            name="FX1403L EB CPU .215",
            unit_serial="FX1403",
            host="10.6.134.215",
        )
        InstallationDevice.objects.create(
            installation=inst,
            ims_device_id=2,
            name="FX1403R WB CPU .225",
            unit_serial="FX1403",
            host="10.6.134.225",
        )
        InstallationDevice.objects.create(
            installation=inst,
            ims_device_id=3,
            name="FX1403 Modem",
            unit_serial="FX1403",
            host="10.6.134.1",
        )
        DragonEyeTeamViewerId.objects.create(
            fx_number="FX1403",
            lane="",
            teamviewer_id="542494654",
            label="legacy unlaned",
        )

        layers = build_device_layers(inst)
        tv_layers = [ly for ly in layers if ly.editable_tv]
        self.assertEqual([ly.tv_lane for ly in tv_layers], ["L1", "L2"])
        self.assertEqual(tv_layers[0].ip, "542494654")
        self.assertIsNone(tv_layers[1].ip)
        # R CPU should be on the TV L2 slot, not a plain leftover device card.
        plain_r = [
            ly
            for ly in layers
            if ly.device
            and "FX1403R" in (ly.device.name or "")
            and not ly.editable_tv
        ]
        self.assertEqual(plain_r, [])
        self.assertIn("FX1403R", tv_layers[1].device.name)

    def test_resolve_targets_labels_unlaned_as_l1(self):
        inst = Installation.objects.create(
            ims_id=266135,
            identifier="I-DE-266135",
            primary_platform="DE",
            pole_number="266135",
            serial_number="FX1403",
            is_active=True,
        )
        InstallationDevice.objects.create(
            installation=inst,
            ims_device_id=11,
            name="FX1403L EB CPU .215",
            unit_serial="FX1403",
        )
        InstallationDevice.objects.create(
            installation=inst,
            ims_device_id=12,
            name="FX1403R WB CPU .225",
            unit_serial="FX1403",
        )
        DragonEyeTeamViewerId.objects.create(
            fx_number="FX1403",
            lane="",
            teamviewer_id="542494654",
        )
        DragonEyeTeamViewerId.objects.create(
            fx_number="FX1403",
            lane="L2",
            teamviewer_id="719714104",
        )
        from services.device_layers import resolve_de_tv_capture_targets

        targets = resolve_de_tv_capture_targets(inst)
        self.assertEqual(
            [(t["lane"], t["thumb_key"], t["teamviewer_id"]) for t in targets],
            [("L1", "de_l1", "542494654"), ("L2", "de_l2", "719714104")],
        )
    def test_lanes_from_devices_lr(self):
        inst = Installation.objects.create(
            ims_id=99,
            identifier="I-DE-99",
            primary_platform="DE",
            serial_number="FX1403",
            is_active=True,
        )
        devices = [
            InstallationDevice(
                installation=inst,
                ims_device_id=1,
                name="FX1403L EB CPU .215",
                unit_serial="FX1403",
            ),
            InstallationDevice(
                installation=inst,
                ims_device_id=2,
                name="FX1403R WB CPU .225",
                unit_serial="FX1403",
            ),
        ]
        self.assertEqual(_lanes_from_devices(devices, "FX1403"), ["L1", "L2"])
