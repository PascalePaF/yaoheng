from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import requests

from app_ui import SettingsPage, YaohengApp
from single_instance import INSTANCE_NAME, SingleInstance, resolve_instance_name
from theme_catalog import THEMES, THEME_LABELS, contrast_ratio
from update_service import (
    GitHubUpdateService,
    LATEST_RELEASE_API,
    ReleaseAsset,
    UpdateError,
    UpdateInfo,
    UpdateSecurityError,
)


def release_payload(version: str = "3.20.1") -> bytes:
    return json.dumps(
        {
            "tag_name": f"v{version}",
            "name": f"曜衡 {version}",
            "body": "fixture",
            "html_url": f"https://github.com/PascalePaF/yaoheng/releases/tag/v{version}",
            "published_at": "2026-08-22T00:00:00Z",
            "draft": False,
            "prerelease": False,
            "assets": [],
        },
        ensure_ascii=False,
    ).encode("utf-8")


class Response:
    def __init__(
        self,
        payload: bytes,
        url: str,
        *,
        status: int = 200,
        retry_after: str = "",
        interrupt_stream: bool = False,
    ) -> None:
        self.payload = payload
        self.url = url
        self.status_code = status
        self.headers = {"Content-Length": str(len(payload))}
        if retry_after:
            self.headers["Retry-After"] = retry_after
        self.interrupt_stream = interrupt_stream
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, chunk_size: int = 64 * 1024):
        if self.interrupt_stream:
            midpoint = max(1, len(self.payload) // 2)
            yield self.payload[:midpoint]
            raise requests.exceptions.ChunkedEncodingError("connection reset")
        for offset in range(0, len(self.payload), max(1, chunk_size)):
            yield self.payload[offset:offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class SequenceSession:
    def __init__(self, items: list[object], *, proxy: str = "") -> None:
        self.items = list(items)
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}
        self.proxies = {"https": proxy} if proxy else {}
        self.trust_env = False

    def get(self, url: str, **_kwargs):
        self.calls.append(url)
        if not self.items:
            raise AssertionError("unexpected HTTP request")
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class UpdateResilienceTests(unittest.TestCase):
    def test_connection_timeout_retries_then_recovers(self):
        delays: list[float] = []
        response = Response(release_payload(), LATEST_RELEASE_API)
        session = SequenceSession(
            [requests.exceptions.ConnectTimeout("timeout"), response]
        )

        info = GitHubUpdateService(session, sleep=delays.append).check("3.20.1")

        self.assertFalse(info.available)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(delays, [0.35])
        self.assertTrue(response.closed)

    def test_retryable_http_response_is_closed_and_retry_after_is_bounded(self):
        delays: list[float] = []
        unavailable = Response(b"", LATEST_RELEASE_API, status=503, retry_after="120")
        recovered = Response(release_payload(), LATEST_RELEASE_API)

        GitHubUpdateService(
            SequenceSession([unavailable, recovered]),
            sleep=delays.append,
        ).check("3.20.1")

        self.assertTrue(unavailable.closed)
        self.assertEqual(delays, [5.0])

    def test_non_retryable_http_error_fails_immediately(self):
        missing = Response(b"", LATEST_RELEASE_API, status=404)
        session = SequenceSession([missing])

        with self.assertRaisesRegex(UpdateError, "HTTP 404"):
            GitHubUpdateService(session, sleep=lambda _delay: None).check("3.20.1")

        self.assertEqual(len(session.calls), 1)
        self.assertTrue(missing.closed)

    def test_proxy_diagnostic_never_exposes_credentials(self):
        session = SequenceSession(
            [requests.exceptions.ProxyError("offline") for _ in range(3)],
            proxy="http://private-user:private-password@127.0.0.1:7897",
        )

        with self.assertRaises(UpdateError) as raised:
            GitHubUpdateService(session, sleep=lambda _delay: None).check("3.20.1")

        detail = str(raised.exception)
        self.assertIn("127.0.0.1:7897", detail)
        self.assertIn("已尝试 3 次", detail)
        self.assertNotIn("private-user", detail)
        self.assertNotIn("private-password", detail)

    def test_metadata_stream_reset_restarts_the_whole_request(self):
        interrupted = Response(
            release_payload(),
            LATEST_RELEASE_API,
            interrupt_stream=True,
        )
        recovered = Response(release_payload(), LATEST_RELEASE_API)
        session = SequenceSession([interrupted, recovered])

        info = GitHubUpdateService(session, sleep=lambda _delay: None).check("3.20.1")

        self.assertFalse(info.available)
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(interrupted.closed)
        self.assertTrue(recovered.closed)

    def test_installer_stream_reset_restarts_atomically(self):
        payload = b"v3202-installer" * 10_000
        digest = hashlib.sha256(payload).hexdigest()
        asset = ReleaseAsset(
            "Yaoheng-3.20.2-Windows-x64-Setup.exe",
            "https://github.com/PascalePaF/yaoheng/releases/download/v3.20.2/Yaoheng-3.20.2-Windows-x64-Setup.exe",
            len(payload),
            digest,
        )
        info = UpdateInfo(
            "3.20.1",
            "3.20.2",
            "曜衡 3.20.2",
            "",
            "https://github.com/PascalePaF/yaoheng/releases/tag/v3.20.2",
            "",
            asset,
        )
        redirected = "https://release-assets.githubusercontent.com/github-production-release-asset/update.exe"
        first = Response(payload, redirected, interrupt_stream=True)
        second = Response(payload, redirected)
        session = SequenceSession([first, second])

        with tempfile.TemporaryDirectory() as directory:
            downloaded = GitHubUpdateService(
                session,
                sleep=lambda _delay: None,
            ).download(info, Path(directory))
            self.assertEqual(downloaded.path.read_bytes(), payload)
            self.assertFalse(Path(directory, f"{asset.name}.part").exists())

        self.assertEqual(len(session.calls), 2)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_untrusted_redirect_is_never_retried(self):
        response = Response(release_payload(), "https://example.com/latest")
        session = SequenceSession([response])

        with self.assertRaises(UpdateSecurityError):
            GitHubUpdateService(session, sleep=lambda _delay: None).check("3.20.1")

        self.assertEqual(len(session.calls), 1)


class MemoryKernelBackend:
    def __init__(self) -> None:
        self.mutex_refs: dict[str, int] = {}
        self.event_refs: dict[str, int] = {}
        self.signaled: dict[str, bool] = {}

    def create_event(self, name: str):
        self.event_refs[name] = self.event_refs.get(name, 0) + 1
        self.signaled.setdefault(name, False)
        return ("event", name)

    def create_mutex(self, name: str):
        existed = self.mutex_refs.get(name, 0) > 0
        self.mutex_refs[name] = self.mutex_refs.get(name, 0) + 1
        return ("mutex", name), existed

    def signal(self, handle) -> None:
        self.signaled[handle[1]] = True

    def consume(self, handle) -> bool:
        name = handle[1]
        value = self.signaled.get(name, False)
        self.signaled[name] = False
        return value

    def close(self, handle) -> None:
        kind, name = handle
        refs = self.event_refs if kind == "event" else self.mutex_refs
        refs[name] = max(0, refs.get(name, 0) - 1)


class SingleInstanceTests(unittest.TestCase):
    def test_later_launch_signals_primary_and_exits(self):
        token = "a" * 32
        self.assertEqual(resolve_instance_name(token), f"{INSTANCE_NAME}.Test.{token}")
        self.assertEqual(resolve_instance_name("not-a-valid-test-token"), INSTANCE_NAME)
        backend = MemoryKernelBackend()
        first = SingleInstance("Local\\Yaoheng.Test", backend=backend)
        second = SingleInstance("Local\\Yaoheng.Test", backend=backend)
        try:
            self.assertTrue(first.is_primary)
            self.assertFalse(second.is_primary)
            self.assertTrue(second.notify_existing())
            self.assertTrue(first.consume_activation())
            self.assertFalse(first.consume_activation())
        finally:
            second.close()
            first.close()

        replacement = SingleInstance("Local\\Yaoheng.Test", backend=backend)
        try:
            self.assertTrue(replacement.is_primary)
        finally:
            replacement.close()

    @unittest.skipUnless(sys.platform == "win32", "Windows named objects only")
    def test_real_windows_named_mutex_and_event(self):
        name = f"Local\\Yaoheng.Test.{uuid.uuid4()}"
        first = SingleInstance(name)
        second = SingleInstance(name)
        try:
            self.assertTrue(first.is_primary)
            self.assertFalse(second.is_primary)
            self.assertTrue(second.notify_existing())
            self.assertTrue(first.consume_activation())
        finally:
            second.close()
            first.close()


class CloseBehaviorTests(unittest.TestCase):
    @staticmethod
    def app(close_action: str):
        app = object.__new__(YaohengApp)
        app.settings = SimpleNamespace(close_action=close_action)
        app.update_installing = False
        app.exiting = False
        app.history_open = False
        app.root = MagicMock()
        app.force_exit = MagicMock()
        app.toggle_history = MagicMock()
        return app

    def test_close_setting_minimizes_without_exiting(self):
        app = self.app("minimize")

        YaohengApp.on_close_request(app)

        app.root.iconify.assert_called_once_with()
        app.force_exit.assert_not_called()

    def test_close_setting_can_fully_exit(self):
        app = self.app("exit")

        YaohengApp.on_close_request(app)

        app.force_exit.assert_called_once_with()
        app.root.iconify.assert_not_called()

    def test_repeated_launch_restores_and_foregrounds_window(self):
        app = object.__new__(YaohengApp)
        app.exiting = False
        app.root = MagicMock()
        app.root.state.return_value = "iconic"
        app.root.after_idle.side_effect = lambda callback: callback()

        YaohengApp.restore_window(app)

        app.root.deiconify.assert_called_once_with()
        app.root.lift.assert_called_once_with()
        app.root.focus_force.assert_called_once_with()
        app.root.attributes.assert_any_call("-topmost", True)
        app.root.attributes.assert_any_call("-topmost", False)

    def test_close_action_labels_are_explicit(self):
        self.assertEqual(
            set(SettingsPage.CLOSE_LABELS),
            {"minimize", "exit"},
        )
        self.assertIn("任务栏", SettingsPage.CLOSE_LABELS["minimize"])
        self.assertIn("彻底退出", SettingsPage.CLOSE_LABELS["exit"])


class ThemeReadabilityPolicyTests(unittest.TestCase):
    def test_catalog_includes_light_and_dark_surfaces_with_explicit_text_mode(self):
        text_modes = {palette["text"] for palette in THEMES.values()}
        self.assertEqual(text_modes, {"#111111", "#FFFFFF"})
        for name, palette in THEMES.items():
            with self.subTest(theme=name):
                self.assertGreaterEqual(contrast_ratio(palette["text"], palette["bg"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(palette["text"], palette["card"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(palette["sidebar_text"], palette["sidebar"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(palette["input_text"], palette["input_bg"]), 4.5)

    def test_contextual_controls_and_market_colours_remain_readable(self):
        for name, palette in THEMES.items():
            with self.subTest(theme=name):
                self.assertGreaterEqual(contrast_ratio(palette["on_accent"], palette["accent"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(palette["calc_number_text"], palette["calc_number_bg"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(palette["calc_function_text"], palette["calc_function_bg"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(palette["calc_operator_text"], palette["calc_operator_bg"]), 4.5)
                self.assertGreaterEqual(contrast_ratio(palette["up"], palette["card"]), 3.0)
                self.assertGreaterEqual(contrast_ratio(palette["down"], palette["card"]), 3.0)

    def test_every_visible_name_is_new_and_nonempty(self):
        self.assertEqual(set(THEMES), set(THEME_LABELS))
        for label in THEME_LABELS.values():
            self.assertTrue(label.strip())
            self.assertNotIn("Catppuccin", label)


if __name__ == "__main__":
    unittest.main()
