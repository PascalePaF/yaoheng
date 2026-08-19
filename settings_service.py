"""Portable application settings for Aurora Balance (曜衡)."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from functools import lru_cache
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
MAX_SETTINGS_FILE_BYTES = 2 * 1024 * 1024


@lru_cache(maxsize=1)
def _cached_timezone_names() -> tuple[str, ...]:
    try:
        zones = sorted(available_timezones())
    except (OSError, UnicodeError):
        zones = []
    return tuple(zones or FALLBACK_TIMEZONES)


def timezone_names() -> list[str]:
    """Return a caller-owned timezone list without rescanning tzdata on every save."""
    return list(_cached_timezone_names())


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
        try:
            path = Path(self.data_dir).expanduser()
            return path.resolve() if path.is_absolute() else portable_dir() / "data"
        except (OSError, RuntimeError, ValueError):
            return portable_dir() / "data"


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else portable_dir() / "app_settings.json"
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        self._lock = threading.RLock()

    @classmethod
    def from_payload(cls, payload: object) -> AppSettings:
        if not isinstance(payload, Mapping):
            raise TypeError("设置文件顶层必须是 JSON 对象")
        known = {key: payload[key] for key in AppSettings.__dataclass_fields__ if key in payload}
        return cls.validate(AppSettings(**known))

    @classmethod
    def _decode(cls, text: str) -> AppSettings:
        return cls.from_payload(json.loads(text))

    @classmethod
    def from_file(cls, path: Path) -> AppSettings:
        return cls._decode(cls._read_text(Path(path)))

    @staticmethod
    def _read_text(path: Path) -> str:
        if path.stat().st_size > MAX_SETTINGS_FILE_BYTES:
            raise ValueError("设置文件过大")
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _serialize(settings: AppSettings) -> str:
        return json.dumps(asdict(settings), ensure_ascii=False, indent=2)

    def _atomic_write(self, target: Path, text: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def load(self) -> AppSettings:
        with self._lock:
            try:
                return self._decode(self._read_text(self.path))
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                try:
                    recovered = self._decode(self._read_text(self.backup_path))
                except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                    return AppSettings()
                try:
                    self._atomic_write(self.path, self._serialize(recovered))
                except (OSError, UnicodeError):
                    pass
                return recovered

    def save(self, settings: AppSettings) -> bool:
        """Atomically save validated settings and retain the previous valid version."""
        try:
            validated = self.validate(settings)
            serialized = self._serialize(validated)
        except (TypeError, ValueError, OverflowError, RecursionError):
            return False
        with self._lock:
            try:
                try:
                    current_text = self._read_text(self.path)
                    current_text = self._serialize(self._decode(current_text))
                except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                    current_text = ""
                if current_text:
                    self._atomic_write(self.backup_path, current_text)
                self._atomic_write(self.path, serialized)
                return True
            except (OSError, UnicodeError):
                return False

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
        zone_names = set(_cached_timezone_names())
        if not isinstance(settings.theme, str) or settings.theme not in {"dark", "light"}:
            settings.theme = "dark"
        if not isinstance(settings.timezone, str) or settings.timezone not in zone_names:
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
        else:
            settings.data_dir = settings.data_dir.strip()
            try:
                custom_path = Path(settings.data_dir).expanduser()
                path_tail = settings.data_dir[2:] if re.match(r"^[A-Za-z]:", settings.data_dir) else settings.data_dir
                windows_invalid = os.name == "nt" and any(character in '<>:"|?*' for character in path_tail)
                reserved_names = {
                    "CON", "PRN", "AUX", "NUL", *(f"COM{index}" for index in range(1, 10)),
                    *(f"LPT{index}" for index in range(1, 10)),
                }
                reserved_component = os.name == "nt" and any(
                    part.rstrip(" .").split(".", 1)[0].upper() in reserved_names
                    for part in custom_path.parts[1:]
                )
                invalid_path = bool(settings.data_dir) and (
                    not custom_path.is_absolute()
                    or any(ord(character) < 32 for character in settings.data_dir)
                    or len(settings.data_dir) > 32767
                    or windows_invalid
                    or reserved_component
                )
            except (OSError, RuntimeError, ValueError):
                invalid_path = True
            if invalid_path:
                settings.data_dir = ""
        geometry_match = re.fullmatch(
            r"(?P<width>\d{3,5})x(?P<height>\d{3,5})(?:(?P<x>[+-]\d{1,5})(?P<y>[+-]\d{1,5}))?",
            settings.window_geometry,
        ) if isinstance(settings.window_geometry, str) else None
        if geometry_match is not None:
            width = int(geometry_match.group("width"))
            height = int(geometry_match.group("height"))
            x = int(geometry_match.group("x") or 0)
            y = int(geometry_match.group("y") or 0)
            geometry_valid = 800 <= width <= 16384 and 600 <= height <= 16384 and abs(x) <= 32768 and abs(y) <= 32768
        else:
            geometry_valid = False
        if not geometry_valid:
            settings.window_geometry = defaults.window_geometry
        if not isinstance(settings.close_action, str) or settings.close_action not in {"exit", "minimize"}:
            settings.close_action = "exit"
        if not isinstance(settings.startup_page, str) or settings.startup_page not in pages:
            settings.startup_page = "calculator"
        if not isinstance(settings.last_page, str) or settings.last_page not in pages:
            settings.last_page = "calculator"
        if not isinstance(settings.default_calculator_mode, str) or settings.default_calculator_mode not in {"standard", "professional"}:
            settings.default_calculator_mode = "standard"
        if not isinstance(settings.last_calculator_mode, str) or settings.last_calculator_mode not in {"standard", "professional"}:
            settings.last_calculator_mode = "standard"
        if not isinstance(settings.calculator_angle_mode, str) or settings.calculator_angle_mode not in {"DEG", "RAD"}:
            settings.calculator_angle_mode = "DEG"
        if not isinstance(settings.copy_result_format, str) or settings.copy_result_format not in {"number", "grouped", "formula"}:
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
            setattr(settings, attr, list(dict.fromkeys(value for value in normalized if re.fullmatch(r"[A-Z0-9_]{1,24}", value)))[:500])
        return settings
