"""Portable application settings for Aurora Balance (曜衡)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from zoneinfo import available_timezones

from rate_service import portable_dir


FALLBACK_TIMEZONES = [
    "UTC", "Asia/Tokyo", "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Singapore", "Asia/Seoul",
    "Asia/Bangkok", "Asia/Kolkata", "Asia/Dubai", "Asia/Jakarta", "Asia/Manila", "Asia/Taipei",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Rome", "Europe/Madrid", "Europe/Moscow",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Toronto",
    "America/Vancouver", "America/Mexico_City", "America/Sao_Paulo", "America/Argentina/Buenos_Aires",
    "Australia/Sydney", "Australia/Melbourne", "Australia/Perth", "Pacific/Auckland", "Pacific/Honolulu",
    "Africa/Cairo", "Africa/Johannesburg", "Africa/Lagos", "Africa/Nairobi",
]


def timezone_names() -> list[str]:
    zones = sorted(available_timezones())
    return zones or FALLBACK_TIMEZONES


@dataclass
class AppSettings:
    theme: str = "dark"
    timezone: str = "Asia/Tokyo"
    data_dir: str = ""
    keep_data_with_app: bool = True
    auto_refresh_enabled: bool = True
    fiat_refresh_minutes: int = 60
    crypto_refresh_minutes: int = 10
    refresh_when_minimized: bool = True
    close_action: str = "exit"
    startup_page: str = "calculator"
    remember_last_page: bool = False
    last_page: str = "calculator"
    remember_window_geometry: bool = True
    window_geometry: str = "1380x820"
    default_calculator_mode: str = "standard"
    remember_calculator_mode: bool = True
    last_calculator_mode: str = "standard"
    calculator_angle_mode: str = "DEG"
    history_limit: int = 30
    retain_history: bool = True
    calculator_history: list[list[str]] = field(default_factory=list)
    copy_result_format: str = "number"
    cache_limit_mb: int = 500
    favorite_fiats: list[str] = field(default_factory=list)
    pinned_fiats: list[str] = field(default_factory=list)
    favorite_cryptos: list[str] = field(default_factory=list)
    pinned_cryptos: list[str] = field(default_factory=list)

    def resolved_data_dir(self) -> Path:
        if self.keep_data_with_app or not self.data_dir:
            return portable_dir() / "data"
        return Path(self.data_dir).expanduser().resolve()


class SettingsStore:
    def __init__(self) -> None:
        self.path = portable_dir() / "app_settings.json"

    def load(self) -> AppSettings:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            known = {key: payload[key] for key in AppSettings.__dataclass_fields__ if key in payload}
            settings = AppSettings(**known)
            return self.validate(settings)
        except (OSError, ValueError, TypeError):
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        try:
            self.path.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def validate(settings: AppSettings) -> AppSettings:
        pages = {"calculator", "fiat", "fiat_market", "crypto", "market", "settings"}
        if settings.theme not in {"dark", "light"}:
            settings.theme = "dark"
        if settings.timezone not in timezone_names():
            settings.timezone = "Asia/Tokyo"
        settings.fiat_refresh_minutes = max(1, min(1440, int(settings.fiat_refresh_minutes)))
        settings.crypto_refresh_minutes = max(1, min(1440, int(settings.crypto_refresh_minutes)))
        settings.history_limit = max(1, min(200, int(settings.history_limit)))
        settings.cache_limit_mb = max(0, min(10240, int(settings.cache_limit_mb)))
        if settings.close_action not in {"exit", "minimize"}:
            settings.close_action = "exit"
        if settings.startup_page not in pages:
            settings.startup_page = "calculator"
        if settings.last_page not in pages:
            settings.last_page = "calculator"
        if settings.default_calculator_mode not in {"standard", "professional"}:
            settings.default_calculator_mode = "standard"
        if settings.last_calculator_mode not in {"standard", "professional"}:
            settings.last_calculator_mode = "standard"
        if settings.calculator_angle_mode not in {"DEG", "RAD"}:
            settings.calculator_angle_mode = "DEG"
        if settings.copy_result_format not in {"number", "grouped", "formula"}:
            settings.copy_result_format = "number"
        if not isinstance(settings.calculator_history, list):
            settings.calculator_history = []
        settings.calculator_history = [
            [str(item[0]), str(item[1])] for item in settings.calculator_history
            if isinstance(item, (list, tuple)) and len(item) >= 2
        ][:settings.history_limit]
        for attr in ("favorite_fiats", "pinned_fiats", "favorite_cryptos", "pinned_cryptos"):
            values = getattr(settings, attr, [])
            if not isinstance(values, list):
                values = []
            setattr(settings, attr, list(dict.fromkeys(str(value).upper() for value in values if str(value).isalpha())))
        return settings
