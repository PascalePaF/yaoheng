"""Portable application settings for Aurora Balance (曜衡)."""

from __future__ import annotations

import json
import re
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
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            pass

    @staticmethod
    def validate(settings: AppSettings) -> AppSettings:
        defaults = AppSettings()

        def clamp_int(value: object, minimum: int, maximum: int, fallback: int) -> int:
            try:
                parsed = int(value)  # type: ignore[arg-type]
            except (TypeError, ValueError, OverflowError):
                parsed = fallback
            return max(minimum, min(maximum, parsed))

        def coerce_bool(value: object, fallback: bool) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                normalized = value.strip().casefold()
                if normalized in {"true", "1", "yes", "on", "是"}:
                    return True
                if normalized in {"false", "0", "no", "off", "否", ""}:
                    return False
            return fallback

        pages = {"calculator", "fiat", "fiat_market", "crypto", "market", "settings"}
        if settings.theme not in {"dark", "light"}:
            settings.theme = "dark"
        if settings.timezone not in timezone_names():
            settings.timezone = "Asia/Tokyo"
        settings.fiat_refresh_minutes = clamp_int(settings.fiat_refresh_minutes, 1, 1440, defaults.fiat_refresh_minutes)
        settings.crypto_refresh_minutes = clamp_int(settings.crypto_refresh_minutes, 1, 1440, defaults.crypto_refresh_minutes)
        settings.history_limit = clamp_int(settings.history_limit, 1, 200, defaults.history_limit)
        settings.cache_limit_mb = clamp_int(settings.cache_limit_mb, 0, 10240, defaults.cache_limit_mb)
        for attr in (
            "keep_data_with_app", "auto_refresh_enabled", "refresh_when_minimized",
            "remember_last_page", "remember_window_geometry", "remember_calculator_mode", "retain_history",
        ):
            setattr(settings, attr, coerce_bool(getattr(settings, attr, None), getattr(defaults, attr)))
        if not isinstance(settings.data_dir, str):
            settings.data_dir = ""
        if not isinstance(settings.window_geometry, str) or not re.fullmatch(r"\d+x\d+(?:[+-]\d+){0,2}", settings.window_geometry):
            settings.window_geometry = defaults.window_geometry
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
            [str(item[0])[:512], str(item[1])[:128]] for item in settings.calculator_history
            if isinstance(item, (list, tuple)) and len(item) >= 2
        ][:settings.history_limit]
        for attr in ("favorite_fiats", "pinned_fiats", "favorite_cryptos", "pinned_cryptos"):
            values = getattr(settings, attr, [])
            if not isinstance(values, list):
                values = []
            normalized = (str(value).upper() for value in values)
            setattr(settings, attr, list(dict.fromkeys(value for value in normalized if re.fullmatch(r"[A-Z0-9_]{1,24}", value))))
        return settings
