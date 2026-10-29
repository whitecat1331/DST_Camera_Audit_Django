"""TeamViewer DE lane: frame thresholds on real frames, login stops at unlock, fast connect failures."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings
from PIL import Image

from services import teamviewer_capture as tvc

FRAMES = Path(__file__).parent / "testdata" / "tv"  # FX1173 L1, 2026-10-29, at _FRAME_SIZE


def frame(name):
    return Image.open(FRAMES / f"{name}.png").convert("RGB")


class FrameThresholdTests(SimpleTestCase):
    """The thresholds hold on the frames they were measured on."""

    def test_waking_the_lock_screen_counts_as_a_wake_not_an_unlock(self):
        d = tvc.frame_diff(frame("clock"), frame("prompt_typed"))
        self.assertGreaterEqual(d, tvc.WAKE_DIFF)
        self.assertLess(d, tvc.UNLOCK_DIFF)

    def test_unlock_is_far_above_the_threshold(self):
        self.assertGreater(tvc.frame_diff(frame("prompt_typed"), frame("desktop")), 3 * tvc.UNLOCK_DIFF)

    def test_a_live_dragoncam_desktop_still_counts_as_settled(self):
        self.assertLess(tvc.frame_diff(frame("desktop"), frame("desktop_b")), tvc.STABLE_DIFF)
        self.assertLess(tvc.frame_diff(frame("prompt_typed"), frame("prompt_typed_b")), tvc.STABLE_DIFF)


class ReconnectCardTests(SimpleTestCase):
    """A bright DragonCam photo is not a reconnect card (it failed real lanes before)."""

    def test_sunlit_photo_is_not_a_reconnect_card(self):
        self.assertFalse(tvc._looks_like_reconnect_dialog(frame("bright_photo_l2")))

    def test_flat_white_card_with_text_is(self):
        from PIL import ImageDraw

        img = frame("bright_photo_l2").point(lambda v: v // 3)  # dimmed session behind the card
        w, h = img.size
        draw = ImageDraw.Draw(img)
        draw.rectangle((w // 2 - 130, h // 2 - 65, w // 2 + 130, h // 2 + 65), fill=(255, 255, 255))
        draw.text((w // 2 - 100, h // 2), "Reconnecting...", fill=(40, 40, 40))
        self.assertTrue(tvc._looks_like_reconnect_dialog(img))


class FakeLockScreen:
    """A remote Ubuntu session: click wakes the clock, the right password unlocks."""

    def __init__(self, good: str | None, state: str = "clock"):
        self.good = good
        self.state = state
        self.typed: list[str] = []
        self.current = ""

    def small_frame(self, _hwnd):
        return frame({"clock": "clock", "prompt": "prompt_typed", "desktop": "desktop"}[self.state])

    def focus(self, _pyautogui, _hwnd):
        if self.state == "clock":
            self.state = "prompt"

    def type_secret(self, _pyautogui, text):
        self.typed.append(text)
        self.current = text

    def press(self, key):
        if key == "enter" and self.state == "prompt" and self.current == self.good:
            self.state = "desktop"

    def login(self, order, passwords=("pw-one", "pw-two")):
        with (
            mock.patch.object(tvc, "_small_frame", side_effect=self.small_frame),
            mock.patch.object(tvc, "_focus_remote_desktop", side_effect=self.focus),
            mock.patch.object(tvc, "_type_secret", side_effect=self.type_secret),
            mock.patch.object(tvc, "_clear_password_field"),
            mock.patch.object(tvc, "_sleep_cancellable"),
            mock.patch.object(tvc.time, "time", side_effect=range(0, 10_000)),
        ):
            return tvc._login(SimpleNamespace(press=self.press), 1, list(passwords), order, None, lambda _m: None)


class LoginTests(SimpleTestCase):
    def test_remembered_password_unlocks_and_nothing_else_is_typed(self):
        screen = FakeLockScreen(good="pw-two")
        self.assertEqual(screen.login([2, 1]), 2)
        self.assertEqual(screen.typed, ["pw-two"])

    def test_wrong_first_password_then_the_next_one(self):
        screen = FakeLockScreen(good="pw-two")
        self.assertEqual(screen.login([1, 2]), 2)
        self.assertEqual(screen.typed, ["pw-one", "pw-two"])

    def test_prompt_already_up_and_wrong_remembered_password_still_tries_the_other(self):
        # FX1408 2026-10-29: the click woke nothing (prompt already showing) and the
        # remembered #2 was wrong; #1 was never typed.
        screen = FakeLockScreen(good="pw-one", state="prompt")
        self.assertEqual(screen.login([2, 1]), 1)
        self.assertEqual(screen.typed, ["pw-two", "pw-one"])

    def test_nothing_unlocks_after_every_password(self):
        screen = FakeLockScreen(good=None)
        self.assertIsNone(screen.login([1, 2]))
        self.assertEqual(screen.typed, ["pw-one", "pw-two"])


class ConnectTests(SimpleTestCase):
    def connect(self, *, sessions, auth=(), offline=False, leftover=False):
        launched = []
        sessions = list(sessions)
        auth = list(auth)

        def popen(args):
            launched.append(args[-1])
            return mock.Mock(poll=mock.Mock(return_value=None))

        def find(_gw, _tv_id, exclude=frozenset()):
            return sessions.pop(0) if sessions else None

        def auth_window():
            return auth.pop(0) if auth else None

        clock = iter(range(0, 10_000))
        dialog = {"up": leftover}

        def ok_point(_main):
            if offline and launched:
                dialog["up"] = True
            return (10, 10) if dialog["up"] else None

        def clicked(*_a):
            dialog["up"] = False
        with (
            mock.patch.object(tvc.subprocess, "Popen", side_effect=popen),
            mock.patch.object(tvc, "_find_session_window", side_effect=find),
            mock.patch.object(tvc, "_auth_window", side_effect=auth_window),
            mock.patch.object(tvc, "_main_window", return_value=7),
            mock.patch.object(tvc, "_unable_to_connect_ok", side_effect=ok_point),
            mock.patch.object(tvc.ctypes, "windll"),
            mock.patch("pyautogui.click", side_effect=clicked) as click,
            mock.patch.object(tvc.time, "sleep"),
            mock.patch.object(tvc.time, "time", side_effect=lambda: next(clock)),
        ):
            try:
                result = tvc._connect(None, "185604118", "tv.exe", ["c1", "c2"], [1, 2], frozenset(), 30,
                                      lambda _m: None, lambda: False)
            except tvc.PartnerOffline as exc:
                result = exc
            return result, launched, click

    def test_refused_password_moves_straight_to_the_next(self):
        win = tvc._TVWindow(5, "FX L1 - TeamViewer")
        (got, number), launched, _ = self.connect(sessions=[None, None, None, win], auth=[None, 99])
        self.assertEqual((got, number), (win, 2))
        self.assertEqual(launched, ["c1", "c2"])

    def test_offline_partner_stops_after_one_launch_and_clears_the_dialog(self):
        got, launched, click = self.connect(sessions=[None] * 50, offline=True)
        self.assertIsInstance(got, tvc.PartnerOffline)
        self.assertEqual(launched, ["c1"])  # no second password for an offline partner
        click.assert_called_once_with(10, 10)

    def test_dialog_left_from_an_earlier_lane_is_cleared_not_read_as_offline(self):
        win = tvc._TVWindow(5, "FX L1 - TeamViewer")
        (got, number), launched, click = self.connect(sessions=[None, None, None, win], leftover=True)
        self.assertEqual((got, number), (win, 1))
        click.assert_called_once_with(10, 10)  # the leftover, before launching


HINTS = Path(tempfile.mkdtemp(prefix="tv-hints-")) / "tv.json"


@override_settings(TV_PASSWORD_HINTS_PATH=str(HINTS))
class TvHintTests(SimpleTestCase):
    def test_connect_and_camera_numbers_are_kept_per_id(self):
        HINTS.unlink(missing_ok=True)
        tvc._remember("185604118", "camera", 2)
        tvc._remember("185604118", "connect", 1)
        self.assertEqual((tvc._hint("185604118", "camera"), tvc._hint("185604118", "connect")), (2, 1))
        self.assertIsNone(tvc._hint("999", "camera"))
        self.assertNotIn("pw", HINTS.read_text())
