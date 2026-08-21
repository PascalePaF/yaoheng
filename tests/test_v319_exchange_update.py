from __future__ import annotations

import hashlib
import json
import tempfile
import tkinter as tk
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import requests

from app_ui import SearchSelect, YaohengApp, format_chinese_datetime
from calculator_core import CalculationError, evaluate_basic_amount_decimal
from exchange_page import ExchangePage
from rate_service import RateSnapshot
from settings_service import AppSettings, SettingsStore
from update_service import (
    DownloadedUpdate,
    GitHubUpdateService,
    LATEST_RELEASE_API,
    ReleaseAsset,
    UpdateError,
    UpdateInfo,
    UpdateSecurityError,
    version_is_newer,
)


class FakeResponse:
    def __init__(self, payload: bytes, url: str, *, content_length: int | None = None) -> None:
        self.payload = payload
        self.url = url
        self.headers = {
            "Content-Length": str(len(payload) if content_length is None else content_length)
        }
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 64 * 1024):
        for offset in range(0, len(self.payload), max(1, chunk_size)):
            yield self.payload[offset:offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class FailingResponse(FakeResponse):
    def raise_for_status(self) -> None:
        raise requests.HTTPError("fixture failure")


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs) -> FakeResponse:
        self.calls.append(url)
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def release_metadata(installer_hash: str, *, installer_url: str | None = None) -> bytes:
    installer_name = "Yaoheng-3.19-Windows-x64-Setup.exe"
    return json.dumps(
        {
            "tag_name": "v3.19",
            "name": "曜衡 3.19",
            "body": "V3.19 update fixture",
            "html_url": "https://github.com/alokxfox/yaoheng/releases/tag/v3.19",
            "published_at": "2026-08-21T00:00:00Z",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": installer_name,
                    "state": "uploaded",
                    "size": 1024 * 1024,
                    "digest": f"sha256:{installer_hash}",
                    "browser_download_url": installer_url
                    or f"https://github.com/alokxfox/yaoheng/releases/download/v3.19/{installer_name}",
                },
                {
                    "name": "SHA256SUMS.txt",
                    "state": "uploaded",
                    "size": 104,
                    "browser_download_url": "https://github.com/alokxfox/yaoheng/releases/download/v3.19/SHA256SUMS.txt",
                },
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")


class DecimalAmountExpressionTests(unittest.TestCase):
    def test_exact_basic_operations_full_width_and_modulo(self):
        self.assertEqual(evaluate_basic_amount_decimal("(1,200+300)/3"), "500")
        self.assertEqual(evaluate_basic_amount_decimal("0.1+0.2"), "0.3")
        self.assertEqual(evaluate_basic_amount_decimal("（１０－１）％４＝"), "1")

    def test_unsafe_or_invalid_amount_expressions_are_rejected(self):
        for expression in ("2**8", "9//2", "1/0", "-1", "__import__('os')"):
            with self.subTest(expression=expression):
                with self.assertRaises(CalculationError):
                    evaluate_basic_amount_decimal(expression)

    def test_chinese_timestamp_format_is_locale_independent(self):
        value = datetime(2026, 8, 21, 9, 7, 5).astimezone()
        self.assertEqual(format_chinese_datetime(value), "2026年08月21日 09:07:05")


class GitHubUpdateServiceTests(unittest.TestCase):
    def test_version_comparison_is_numeric_not_lexicographic(self):
        self.assertTrue(version_is_newer("3.19", "3.9"))
        self.assertFalse(version_is_newer("v3.19.0", "3.19"))

    def test_latest_release_requires_matching_installer_and_checksums(self):
        checksum = "a" * 64
        checksum_payload = (
            f"{checksum}  Yaoheng-3.19-Windows-x64-Setup.exe\n"
        ).encode("utf-8")
        session = FakeSession(
            [
                FakeResponse(release_metadata(checksum), LATEST_RELEASE_API),
                FakeResponse(
                    checksum_payload,
                    "https://github.com/alokxfox/yaoheng/releases/download/v3.19/SHA256SUMS.txt",
                ),
            ]
        )

        info = GitHubUpdateService(session).check("3.18")

        self.assertTrue(info.available)
        self.assertEqual(info.latest_version, "3.19")
        self.assertEqual(info.installer.sha256, checksum)
        self.assertEqual(len(session.calls), 2)

    def test_up_to_date_release_does_not_download_assets(self):
        checksum = "a" * 64
        session = FakeSession([FakeResponse(release_metadata(checksum), LATEST_RELEASE_API)])

        info = GitHubUpdateService(session).check("3.19")

        self.assertFalse(info.available)
        self.assertIsNone(info.installer)
        self.assertEqual(len(session.calls), 1)

    def test_http_failure_closes_response(self):
        response = FailingResponse(b"", LATEST_RELEASE_API)

        with self.assertRaisesRegex(UpdateError, "无法连接 GitHub"):
            GitHubUpdateService(FakeSession([response])).check("3.18")

        self.assertTrue(response.closed)

    def test_non_github_asset_and_digest_mismatch_fail_closed(self):
        checksum = "b" * 64
        checksum_payload = (
            f"{'c' * 64}  Yaoheng-3.19-Windows-x64-Setup.exe\n"
        ).encode("utf-8")
        bad_url_session = FakeSession(
            [FakeResponse(release_metadata(checksum, installer_url="https://example.com/update.exe"), LATEST_RELEASE_API)]
        )
        with self.assertRaises(UpdateSecurityError):
            GitHubUpdateService(bad_url_session).check("3.18")

        mismatch_session = FakeSession(
            [
                FakeResponse(release_metadata(checksum), LATEST_RELEASE_API),
                FakeResponse(
                    checksum_payload,
                    "https://github.com/alokxfox/yaoheng/releases/download/v3.19/SHA256SUMS.txt",
                ),
            ]
        )
        with self.assertRaises(UpdateSecurityError):
            GitHubUpdateService(mismatch_session).check("3.18")

    def test_download_is_atomic_and_hash_verified(self):
        payload = b"v319-installer" * 80_000
        digest = hashlib.sha256(payload).hexdigest()
        asset = ReleaseAsset(
            "Yaoheng-3.19-Windows-x64-Setup.exe",
            "https://github.com/alokxfox/yaoheng/releases/download/v3.19/Yaoheng-3.19-Windows-x64-Setup.exe",
            len(payload),
            digest,
        )
        info = UpdateInfo(
            "3.18", "3.19", "曜衡 3.19", "", "https://github.com/alokxfox/yaoheng/releases/tag/v3.19", "", asset,
        )
        response = FakeResponse(
            payload,
            "https://release-assets.githubusercontent.com/github-production-release-asset/update.exe",
        )
        service = GitHubUpdateService(FakeSession([response]))
        with tempfile.TemporaryDirectory() as directory:
            downloaded = service.download(info, Path(directory))
            self.assertEqual(downloaded.sha256, digest)
            self.assertEqual(downloaded.path.read_bytes(), payload)
            self.assertFalse((Path(directory) / f"{asset.name}.part").exists())

    def test_failed_download_discards_partial_file(self):
        payload = b"wrong-installer" * 80_000
        asset = ReleaseAsset(
            "Yaoheng-3.19-Windows-x64-Setup.exe",
            "https://github.com/alokxfox/yaoheng/releases/download/v3.19/Yaoheng-3.19-Windows-x64-Setup.exe",
            len(payload),
            "f" * 64,
        )
        info = UpdateInfo(
            "3.18", "3.19", "曜衡 3.19", "", "https://github.com/alokxfox/yaoheng/releases/tag/v3.19", "", asset,
        )
        service = GitHubUpdateService(
            FakeSession(
                [
                    FakeResponse(
                        payload,
                        "https://release-assets.githubusercontent.com/github-production-release-asset/update.exe",
                    )
                ]
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with self.assertRaisesRegex(UpdateSecurityError, "SHA-256"):
                service.download(info, target)
            self.assertFalse((target / asset.name).exists())
            self.assertFalse((target / f"{asset.name}.part").exists())

    def test_launch_uses_argument_list_and_current_install_directory(self):
        payload = b"fixture-installer"
        digest = hashlib.sha256(payload).hexdigest()
        asset = ReleaseAsset(
            "Yaoheng-3.19-Windows-x64-Setup.exe",
            "https://github.com/alokxfox/yaoheng/releases/download/v3.19/Yaoheng-3.19-Windows-x64-Setup.exe",
            len(payload),
            digest,
        )
        info = UpdateInfo(
            "3.18", "3.19", "曜衡 3.19", "", "https://github.com/alokxfox/yaoheng/releases/tag/v3.19", "", asset,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / asset.name
            path.write_bytes(payload)
            downloaded = DownloadedUpdate(info, path, digest)
            with patch("update_service.sys.platform", "win32"), patch("update_service.subprocess.Popen") as popen:
                GitHubUpdateService.launch_installer(downloaded, Path(directory) / "installed")
            args = popen.call_args.args[0]
            self.assertEqual(args[0], str(path.resolve()))
            self.assertIn(f"/DIR={(Path(directory) / 'installed').resolve()}", args)
            self.assertIn("/CLOSEAPPLICATIONS", args)
            self.assertIn("/RESTARTAPPLICATIONS", args)
            self.assertTrue(popen.call_args.kwargs["close_fds"])


def ui_snapshot() -> RateSnapshot:
    strings = {
        "USD": "1", "CNY": "7.1", "EUR": "0.91", "JPY": "150",
        "HKD": "7.8", "BTC": "0.00002", "USDT": "1", "ETH": "0.0005",
    }
    return RateSnapshot(
        rates={code: float(value) for code, value in strings.items()},
        rate_strings=strings,
        names={code: code for code in strings},
        kinds={code: "crypto" if code in {"BTC", "USDT", "ETH"} else "fiat" for code in strings},
        changes={code: 0.0 for code in strings},
        fetched_at=datetime.now().astimezone().isoformat(),
        errors=[],
        coin_ids={"BTC": "bitcoin", "USDT": "tether", "ETH": "ethereum"},
    )


class ExchangePageV319TkTests(unittest.TestCase):
    def test_primary_row_search_select_keyboard_and_formula_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "app_settings.json")
            store.save(AppSettings(data_dir=directory, auto_refresh_enabled=False))
            app: YaohengApp | None = None
            try:
                with patch("app_ui.SettingsStore", return_value=store):
                    app = YaohengApp()
            except tk.TclError as exc:
                self.skipTest(f"Tk display unavailable: {exc}")
            try:
                app.root.withdraw()
                for attr in ("startup_job", "api_start_job"):
                    job = getattr(app, attr, None)
                    if job:
                        app.root.after_cancel(job)
                        setattr(app, attr, None)
                snapshot = ui_snapshot()
                app.service.snapshot = snapshot
                page = app.pages["exchange"]
                self.assertIsInstance(page, ExchangePage)
                page.apply_snapshot(snapshot)
                app.show_page("exchange")
                app.root.update_idletasks()

                primary_card = page.card_widgets[page.state.primary_slot]
                grid = primary_card.grid_info()
                self.assertEqual(int(grid["column"]), 0)
                self.assertEqual(int(grid["columnspan"]), 3)
                self.assertEqual(page.primary_entry.cget("justify"), "left")
                self.assertTrue(all(isinstance(selector, SearchSelect) for selector in page.currency_selectors.values()))
                settings_page = app.pages["settings"]
                self.assertEqual(settings_page.update_check_button.cget("text"), "检查更新")
                self.assertEqual(settings_page.update_install_button.cget("text"), "下载并升级")

                page.amount_var.set("(1,200+300)/3")
                page._commit_amount_expression()
                self.assertEqual(page.amount_var.get(), "500")
                self.assertEqual(page.state.amount, "500")
                self.assertEqual(page.state.to_dict()["amount"], "500")

                old_code = page.state.primary_code
                selector = page.currency_selectors[page.state.primary_slot]
                selector._filter("")
                selector._move(1)
                selector._confirm()
                app.root.update_idletasks()
                self.assertNotEqual(page.state.primary_code, old_code)
            finally:
                if app is not None and not app.exiting:
                    app.force_exit()


if __name__ == "__main__":
    unittest.main()
