"""``cbw_firmware_report`` — enumerate CBWs, read firmware, join IMS data, write CSV.

For every active installation IMS knows carries a CBW (device_type='cbw'), probe
that CBW's ControlByWeb firmware endpoint and emit one CSV row joining the
firmware/part-number/serial with the full IMS installation record (identity +
location). CBW-hosting poles with no IMS device row (LTI/ALPR/ASE) are included
via the derived 10.x.y.177 address and flagged ``cbw_source=derived``.
"""

from __future__ import annotations

import csv
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings
from django.core.management.base import BaseCommand

from cameras.models import Installation
from services.cbw_firmware import read_cbw_firmware
from services.ip_map import cbw_ip

logger = logging.getLogger(__name__)

# CBW relay controllers co-exist at LTI/ALPR/ASE poles (see IMS assets sync).
CBW_PLATFORMS = ("LTI", "ALPR", "ASE")

CBW_COLUMNS = [
    "cbw_ip",
    "cbw_firmware",
    "cbw_part_number",
    "cbw_serial",
    "cbw_source",
    "cbw_status",
    "cbw_error",
]
IMS_IDENTITY_COLUMNS = [
    "ims_id",
    "identifier",
    "name",
    "primary_platform",
    "all_platforms",
    "pole_number",
    "serial_number",
    "fl_number",
    "ip_address",
    "vendor",
    "model",
    "status",
]
IMS_LOCATION_COLUMNS = [
    "agency",
    "location",
    "street",
    "county",
    "state",
    "direction",
    "site_type",
    "gps_lat",
    "gps_lon",
]
IMS_OTHER_COLUMNS = [
    "power_type",
    "product_kit",
    "subtype",
    "source",
    "notes",
    "prtg_device_name",
    "created_at",
    "ims_updated_at",
]

COLUMNS = CBW_COLUMNS + IMS_IDENTITY_COLUMNS + IMS_LOCATION_COLUMNS + IMS_OTHER_COLUMNS


class Command(BaseCommand):
    help = "Write a CSV of every CBW (firmware + full IMS installation record)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output", default="cbw_firmware_report.csv", help="Output CSV path."
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Probe at most N CBWs (0 = all)."
        )
        parser.add_argument(
            "--poles",
            default="",
            help="Comma-separated pole numbers to probe (limits the run).",
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=8,
            help="Probe this many CBWs in parallel (1 = serial).",
        )

    def handle(self, *args, **options):
        output = options["output"]
        limit = int(options["limit"] or 0)
        concurrency = max(1, int(options["concurrency"] or 1))
        poles_filter = [
            p.strip() for p in (options["poles"] or "").split(",") if p.strip()
        ]

        username = settings.CBW_USERNAME
        passwords = list(settings.CBW_PASSWORDS)
        if not username or not passwords:
            self.stderr.write(
                self.style.WARNING(
                    "CBW_USERNAME/CBW_PASSWORDS not set — firmware reads will "
                    "report auth_failed."
                )
            )

        candidates = self._enumerate(poles_filter)
        if limit and len(candidates) > limit:
            candidates = candidates[:limit]
        self.stdout.write(
            f"Enumerated {len(candidates)} CBW candidate(s); "
            f"probing with concurrency={concurrency}."
        )

        # Pre-extract IMS fields in the main thread so worker threads only do
        # HTTP (no Django DB access off-thread).
        jobs = [(host, source, self._ims_data(inst)) for host, source, inst in candidates]

        results: dict[int, dict] = {}
        if concurrency <= 1:
            for idx, (host, source, ims) in enumerate(jobs):
                self.stdout.write(f"[{idx + 1}/{len(jobs)}] probing {host} ...")
                results[idx] = self._probe_row(host, source, ims, username, passwords)
        else:
            with ThreadPoolExecutor(
                max_workers=concurrency, thread_name_prefix="cbw-fw"
            ) as pool:
                futures = {
                    pool.submit(
                        self._probe_row, host, source, ims, username, passwords
                    ): idx
                    for idx, (host, source, ims) in enumerate(jobs)
                }
                done = 0
                for fut in as_completed(futures):
                    idx = futures[fut]
                    done += 1
                    host = jobs[idx][0]
                    try:
                        results[idx] = fut.result()
                    except Exception as exc:  # noqa: BLE001 — keep the row
                        logger.exception("[CBW] probe error host=%s", host)
                        results[idx] = {
                            "cbw_ip": host,
                            "cbw_firmware": "",
                            "cbw_part_number": "",
                            "cbw_serial": "",
                            "cbw_source": jobs[idx][1],
                            "cbw_status": "error",
                            "cbw_error": type(exc).__name__,
                            **jobs[idx][2],
                        }
                    self.stdout.write(f"[{done}/{len(jobs)}] {host} done")

        rows = [results[i] for i in sorted(results)]

        with open(output, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

        ok = sum(1 for r in rows if r["cbw_status"] == "ok")
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {len(rows)} row(s) to {output} ({ok} with firmware)."
            )
        )

    def _enumerate(self, poles_filter):
        qs = Installation.objects.filter(is_active=True).exclude(pole_number="")
        if poles_filter:
            qs = qs.filter(pole_number__in=poles_filter)

        seen_hosts: set[str] = set()
        out: list[tuple[str, str, Installation]] = []

        # Primary: installations IMS knows carry a CBW (device_type='cbw').
        with_cbw = (
            qs.filter(devices__device_type__iexact="cbw")
            .distinct()
            .order_by("pole_number")
        )
        for inst in with_cbw:
            host = self._cbw_host(inst)
            if not host or host in seen_hosts:
                continue
            seen_hosts.add(host)
            out.append((host, "ims_device", inst))

        # Fallback: CBW-hosting platforms with no CBW device row → derived IP.
        without_cbw = (
            qs.filter(primary_platform__in=CBW_PLATFORMS)
            .exclude(devices__device_type__iexact="cbw")
            .distinct()
            .order_by("pole_number")
        )
        for inst in without_cbw:
            host = self._derived_host(inst)
            if not host or host in seen_hosts:
                continue
            seen_hosts.add(host)
            out.append((host, "derived", inst))

        return out

    def _cbw_host(self, inst):
        # CBWs are reached via the pole-derived 10.x.y.177 address, matching
        # services.cbw_relays.py / services.cbw_capture.py. The device `host`
        # from IMS is the PRTG host (often the modem's public IP) and is NOT a
        # reliable HTTP endpoint for firmware.
        return self._derived_host(inst)

    def _derived_host(self, inst):
        pole = (inst.pole_number or "").strip()
        if not pole.isdigit() or len(pole) < 4:
            return None
        try:
            return cbw_ip(pole)
        except ValueError:
            return None

    def _probe_row(self, host, source, ims, username, passwords):
        result = read_cbw_firmware(host, username, passwords)
        cbw = {
            "cbw_ip": host,
            "cbw_firmware": result.firmware,
            "cbw_part_number": result.part_number,
            "cbw_serial": result.serial,
            "cbw_source": source,
            "cbw_status": result.status,
            "cbw_error": result.error,
        }
        return {**cbw, **ims}

    def _ims_data(self, inst):
        return {
            "ims_id": inst.ims_id,
            "identifier": inst.identifier,
            "name": inst.name,
            "primary_platform": inst.primary_platform,
            "all_platforms": inst.all_platforms,
            "pole_number": inst.pole_number,
            "serial_number": inst.serial_number,
            "fl_number": inst.fl_number,
            "ip_address": inst.ip_address,
            "vendor": inst.vendor,
            "model": inst.model,
            "status": inst.status,
            "agency": inst.agency,
            "location": inst.location,
            "street": inst.street,
            "county": inst.county,
            "state": inst.state,
            "direction": inst.direction,
            "site_type": inst.site_type,
            "gps_lat": "" if inst.gps_lat is None else inst.gps_lat,
            "gps_lon": "" if inst.gps_long is None else inst.gps_long,
            "power_type": inst.power_type,
            "product_kit": inst.product_kit,
            "subtype": inst.subtype,
            "source": inst.source,
            "notes": inst.notes,
            "prtg_device_name": inst.prtg_device_name,
            "created_at": inst.created_at,
            "ims_updated_at": inst.ims_updated_at,
        }
