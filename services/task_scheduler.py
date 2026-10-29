"""One-time Windows Task Scheduler triggers for scheduled DST audits.

The task runs `manage.py run_scheduled_audit <id>` as the signed-in user
(InteractiveToken: TeamViewer capture needs the unlocked desktop), wakes the
laptop, and never starts late. StartBoundary is written in UTC, so the night
the clocks change has no ambiguous or missing hour; Task Scheduler shows and
fires it on the device's local clock.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings

logger = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000


class TaskSchedulerError(Exception):
    pass


def python_exe() -> str:
    """pythonw.exe beside the running interpreter: no console window stealing focus."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return str(windowless if windowless.exists() else exe)


def start_boundary(fire_at: datetime) -> str:
    if fire_at.tzinfo is None:
        raise ValueError("fire_at must be timezone-aware")
    return fire_at.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def task_xml(schedule_id: int, fire_at: datetime, *, user: str, python: str, workdir: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>DST Camera Audit: one-time scheduled audit #{schedule_id}</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start_boundary(fire_at)}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT14H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(python)}</Command>
      <Arguments>manage.py run_scheduled_audit {schedule_id}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    name = os.environ.get("USERNAME", "")
    return f"{domain}\\{name}" if domain else name


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["schtasks", *args],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TaskSchedulerError(f"schtasks failed: {exc}") from exc


def register(task_name: str, schedule_id: int, fire_at: datetime) -> None:
    """Create or replace the one-time task."""
    xml = task_xml(
        schedule_id,
        fire_at,
        user=current_user(),
        python=python_exe(),
        workdir=str(settings.BASE_DIR),
    )
    fd, path = tempfile.mkstemp(suffix=".xml", prefix="dst-task-")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as fh:
            fh.write(xml)
        out = _schtasks("/Create", "/TN", task_name, "/XML", path, "/F")
    finally:
        os.unlink(path)
    if out.returncode != 0:
        raise TaskSchedulerError((out.stderr or out.stdout or "schtasks /Create failed").strip())
    logger.info("[SCHED] registered task %r at %s", task_name, start_boundary(fire_at))


def delete(task_name: str) -> bool:
    """Remove the task; True when it existed."""
    out = _schtasks("/Delete", "/TN", task_name, "/F")
    if out.returncode == 0:
        logger.info("[SCHED] deleted task %r", task_name)
        return True
    return False


def exists(task_name: str) -> bool:
    return _schtasks("/Query", "/TN", task_name).returncode == 0


def pid_alive(pid: int | None) -> bool:
    """Whether a process is still running (os.kill would terminate it on Windows)."""
    if not pid:
        return False
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def run_now(task_name: str) -> None:
    out = _schtasks("/Run", "/TN", task_name)
    if out.returncode != 0:
        raise TaskSchedulerError((out.stderr or out.stdout or "schtasks /Run failed").strip())
