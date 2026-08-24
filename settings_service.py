"""Portable, additive schema-v3 settings for Aurora Balance (曜衡)."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import available_timezones

from app_version import SETTINGS_SCHEMA_VERSION
from conversion_core import (
    AmountInputError,
    canonical_amount_string,
    canonical_rate_string,
    normalize_currency_code,
)
from localization import normalize_language
from rate_service import portable_dir
from theme_catalog import SUPPORTED_THEME_NAMES


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
DEFAULT_SAVE_DEBOUNCE_MS = 650
MIN_SAVE_DEBOUNCE_MS = 500
MAX_SAVE_DEBOUNCE_MS = 750
DEFAULT_EXCHANGE_CURRENCIES = ("CNY", "USD", "EUR", "JPY", "HKD", "BTC", "USDT")
CONVERSION_HISTORY_LIMIT = 50
CONVERSION_HISTORY_RETENTION_DAYS = 30
LOCAL_API_DEFAULT_PORT = 17890
LOCAL_API_LOOPBACK = "127.0.0.1"
CURRENT_PAGE_NAMES = (
    "exchange", "market_exchange", "calculator", "fiat", "fiat_market", "crypto", "market", "settings",
)
LEGACY_NAVIGATION_PAGES = frozenset(
    {"calculator", "exchange", "market_exchange", "fiat", "fiat_market", "crypto", "market", "settings"}
)

_SETTINGS_LOCKS_GUARD = threading.Lock()
_SETTINGS_LOCKS: dict[str, threading.RLock] = {}
_INVALID_JSON = object()
_SENSITIVE_KEY_PARTS = (
    "advertisement", "advertiser", "ad_id", "merchant", "payment_identity",
    "payment_account", "credential", "password", "secret", "token", "api_key",
    "authorization", "private_key",
)


def _settings_lock(path: Path) -> threading.RLock:
    """Share one in-process lock between stores that target the same file."""

    try:
        normalized = os.path.normcase(str(path.resolve(strict=False)))
    except (OSError, RuntimeError, ValueError):
        normalized = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _SETTINGS_LOCKS_GUARD:
        return _SETTINGS_LOCKS.setdefault(normalized, threading.RLock())


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


def _default_exchange_page(mode: str = "c2c") -> dict[str, Any]:
    return {
        "currencies": list(DEFAULT_EXCHANGE_CURRENCIES),
        "primary_slot": 0,
        "active_slot": 0,
        "amount": "1",
        "mode": mode,
        "provider": "auto",
        "payment_method": "",
        "settlement_fiat": "CNY",
    }


def _default_conversion_history() -> dict[str, Any]:
    return {
        "enabled": False,
        "limit": CONVERSION_HISTORY_LIMIT,
        "retention_days": CONVERSION_HISTORY_RETENTION_DAYS,
        "entries": [],
    }


def _default_local_api() -> dict[str, Any]:
    return {
        "enabled": False,
        "host": LOCAL_API_LOOPBACK,
        "port": LOCAL_API_DEFAULT_PORT,
    }


def _default_pages() -> dict[str, dict[str, Any]]:
    return {
        "exchange": _default_exchange_page("c2c"),
        "market_exchange": _default_exchange_page("market"),
        "calculator": {
            "default_mode": "standard",
            "remember_mode": True,
            "last_mode": "standard",
            "angle_mode": "DEG",
            "history_limit": 30,
            "retain_history": True,
            "history": [],
            "copy_result_format": "number",
        },
        "fiat": {
            "currencies": ["CNY", "USD", "EUR"],
            "amounts": ["1000", "", ""],
            "active_side": "a",
            "table_base": "CNY",
            "reference_amount": "1",
            "favorites": [],
            "pinned": [],
        },
        "fiat_market": {},
        "crypto": {
            "currencies": ["CNY", "BTC", "ETH"],
            "amounts": ["10000", "", ""],
            "active_side": "a",
            "table_base": "CNY",
            "reference_amount": "1",
            "mode": "market",
            "provider": "auto",
            "payment_method": "",
            "favorites": [],
            "pinned": [],
        },
        "market": {},
        "settings": {},
    }


@dataclass
class AppSettings:
    # The original 3.17 fields deliberately remain top-level for UI and import
    # compatibility.  The v2 page partitions are additive, not a replacement.
    theme: str = "dark"
    language: str = "zh_CN"
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

    # Versioned page partitions and service settings.
    schema_version: int = SETTINGS_SCHEMA_VERSION
    pages: dict[str, dict[str, Any]] = field(default_factory=_default_pages)
    conversion_history: dict[str, Any] = field(default_factory=_default_conversion_history)
    local_api: dict[str, Any] = field(default_factory=_default_local_api)

    # Unknown, non-sensitive top-level extension fields survive additive
    # migration without being nested under a new key on disk.
    _extra_fields: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def resolved_data_dir(self) -> Path:
        if self.keep_data_with_app or not self.data_dir:
            return portable_dir() / "data"
        try:
            path = Path(self.data_dir).expanduser()
            return path.resolve() if path.is_absolute() else portable_dir() / "data"
        except (OSError, RuntimeError, ValueError):
            return portable_dir() / "data"


def _coerce_bool(value: object, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on", "是"}:
            return True
        if normalized in {"false", "0", "no", "off", "否", ""}:
            return False
    return fallback


def _clamp_int(value: object, minimum: int, maximum: int, fallback: int) -> int:
    if isinstance(value, bool):
        parsed = fallback
    else:
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            parsed = fallback
    return max(minimum, min(maximum, parsed))


def _is_sensitive_key(value: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).strip().casefold()).strip("_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _safe_json_copy(value: object, *, depth: int = 0) -> object:
    """Bound and redact extension data before it reaches ordinary exports."""

    if depth > 8:
        return _INVALID_JSON
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str):
            return value[:4096]
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _INVALID_JSON
    if isinstance(value, Mapping):
        if len(value) > 500:
            return _INVALID_JSON
        copied: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or len(raw_key) > 128 or _is_sensitive_key(raw_key):
                continue
            safe_value = _safe_json_copy(raw_value, depth=depth + 1)
            if safe_value is _INVALID_JSON:
                return _INVALID_JSON
            copied[raw_key] = safe_value
        return copied
    if isinstance(value, (list, tuple)):
        if len(value) > 500:
            return _INVALID_JSON
        copied_list: list[Any] = []
        for item in value:
            safe_item = _safe_json_copy(item, depth=depth + 1)
            if safe_item is _INVALID_JSON:
                return _INVALID_JSON
            copied_list.append(safe_item)
        return copied_list
    return _INVALID_JSON


def _normalize_codes(value: object, maximum: int = 500) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for raw_code in value:
        code = str(raw_code).strip().upper()
        if re.fullmatch(r"[A-Z0-9_]{1,24}", code) and code not in normalized:
            normalized.append(code)
        if len(normalized) >= maximum:
            break
    return normalized


def _legacy_page_state(settings: AppSettings) -> dict[str, dict[str, Any]]:
    return {
        "calculator": {
            "default_mode": settings.default_calculator_mode,
            "remember_mode": settings.remember_calculator_mode,
            "last_mode": settings.last_calculator_mode,
            "angle_mode": settings.calculator_angle_mode,
            "history_limit": settings.history_limit,
            "retain_history": settings.retain_history,
            "history": [list(item) for item in settings.calculator_history],
            "copy_result_format": settings.copy_result_format,
        },
        "fiat": {
            "favorites": list(settings.favorite_fiats),
            "pinned": list(settings.pinned_fiats),
        },
        "crypto": {
            "favorites": list(settings.favorite_cryptos),
            "pinned": list(settings.pinned_cryptos),
        },
    }


def _validate_exchange_page(value: object, *, mode: str) -> dict[str, Any]:
    defaults = _default_exchange_page(mode)
    if not isinstance(value, Mapping):
        return defaults
    currencies = value.get("currencies", defaults["currencies"])
    normalized = list(defaults["currencies"])
    if isinstance(currencies, list) and len(currencies) == len(DEFAULT_EXCHANGE_CURRENCIES):
        try:
            candidate = [normalize_currency_code(code) for code in currencies]
            if len(set(candidate)) == len(DEFAULT_EXCHANGE_CURRENCIES):
                normalized = candidate
        except (TypeError, ValueError):
            pass
    primary_slot = value.get("primary_slot", defaults["primary_slot"])
    if isinstance(primary_slot, bool) or not isinstance(primary_slot, int) or not 0 <= primary_slot < len(normalized):
        primary_slot = defaults["primary_slot"]
    active_slot = value.get("active_slot", primary_slot)
    if isinstance(active_slot, bool) or not isinstance(active_slot, int) or not 0 <= active_slot < len(normalized):
        active_slot = primary_slot
    raw_amount = str(value.get("amount", defaults["amount"]) or "").strip()
    if raw_amount:
        try:
            amount = canonical_amount_string(raw_amount)
        except AmountInputError:
            amount = defaults["amount"]
    else:
        amount = ""
    provider = str(value.get("provider", defaults["provider"])).strip().lower()
    if provider not in {"auto", "binance", "okx"}:
        provider = defaults["provider"]
    payment_method = value.get("payment_method", defaults["payment_method"])
    if not isinstance(payment_method, str) or (
        payment_method and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", payment_method) is None
    ):
        payment_method = ""
    try:
        settlement_fiat = normalize_currency_code(
            value.get("settlement_fiat", defaults["settlement_fiat"])
        )
    except (TypeError, ValueError):
        settlement_fiat = defaults["settlement_fiat"]
    return {
        "currencies": normalized,
        "primary_slot": primary_slot,
        "active_slot": active_slot,
        "amount": amount,
        "mode": mode,
        "provider": provider,
        "payment_method": payment_method,
        "settlement_fiat": settlement_fiat,
    }


def _validate_pages(value: object, settings: AppSettings) -> dict[str, dict[str, Any]]:
    defaults = _default_pages()
    source = value if isinstance(value, Mapping) else {}
    validated: dict[str, dict[str, Any]] = {}
    legacy_exchange = source.get("exchange")
    validated["exchange"] = _validate_exchange_page(legacy_exchange, mode="c2c")
    validated["market_exchange"] = _validate_exchange_page(
        source.get("market_exchange", legacy_exchange), mode="market"
    )
    for page_name in CURRENT_PAGE_NAMES[2:]:
        raw_page = source.get(page_name)
        if not isinstance(raw_page, Mapping):
            validated[page_name] = dict(defaults[page_name])
            continue
        safe_page = _safe_json_copy(raw_page)
        validated[page_name] = (
            safe_page if isinstance(safe_page, dict) else dict(defaults[page_name])
        )

    # Preserve safe forward-compatible page partitions while keeping versioned
    # validation isolated to one object at a time.
    for raw_name, raw_page in source.items():
        name = str(raw_name).strip()
        if name in validated or re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) is None:
            continue
        safe_page = _safe_json_copy(raw_page)
        if isinstance(safe_page, dict):
            validated[name] = safe_page

    # The 3.17 UI still writes these top-level fields.  Mirroring them on every
    # validation prevents a stale page copy from undoing legacy UI changes.
    for page_name, legacy_state in _legacy_page_state(settings).items():
        validated[page_name].update(legacy_state)
    # V3.21.2 separates ordinary crypto conversion from the dedicated C2C
    # page, including when an older settings file remembered C2C here.
    validated["crypto"]["mode"] = "market"
    validated["crypto"]["provider"] = "auto"
    validated["crypto"]["payment_method"] = ""
    return validated


def _parse_history_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (OverflowError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _validate_history_entry(value: object, *, cutoff: datetime, now: datetime) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    timestamp = _parse_history_timestamp(value.get("timestamp"))
    if timestamp is None or timestamp < cutoff or timestamp > now + timedelta(days=1):
        return None
    try:
        source = normalize_currency_code(value.get("source"))
        target = normalize_currency_code(value.get("target"))
        amount = canonical_amount_string(value.get("amount"))
        result = canonical_amount_string(value.get("result"))
    except (TypeError, ValueError):
        return None
    mode = value.get("mode", "market")
    if mode not in {"market", "c2c"}:
        return None
    provider = value.get("provider", "auto")
    if not isinstance(provider, str) or re.fullmatch(r"[a-z0-9_-]{1,32}", provider) is None:
        return None
    entry: dict[str, Any] = {
        "timestamp": timestamp.isoformat(),
        "source": source,
        "target": target,
        "amount": amount,
        "result": result,
        "mode": mode,
        "provider": provider,
    }
    if "rate" in value:
        try:
            entry["rate"] = canonical_rate_string(value["rate"])
        except ValueError:
            return None
    if "degraded" in value:
        entry["degraded"] = _coerce_bool(value["degraded"], False)
    return entry


def _validate_conversion_history(value: object) -> dict[str, Any]:
    defaults = _default_conversion_history()
    if not isinstance(value, Mapping):
        return defaults
    enabled = _coerce_bool(value.get("enabled"), False)
    limit = _clamp_int(value.get("limit"), 1, CONVERSION_HISTORY_LIMIT, CONVERSION_HISTORY_LIMIT)
    retention_days = _clamp_int(
        value.get("retention_days"), 1, CONVERSION_HISTORY_RETENTION_DAYS,
        CONVERSION_HISTORY_RETENTION_DAYS,
    )
    entries: list[dict[str, Any]] = []
    raw_entries = value.get("entries")
    if enabled and isinstance(raw_entries, list):
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        for raw_entry in raw_entries:
            entry = _validate_history_entry(raw_entry, cutoff=cutoff, now=now)
            if entry is not None:
                entries.append(entry)
            if len(entries) >= limit:
                break
    return {
        "enabled": enabled,
        "limit": limit,
        "retention_days": retention_days,
        "entries": entries,
    }


def _validate_local_api(value: object) -> dict[str, Any]:
    defaults = _default_local_api()
    if not isinstance(value, Mapping):
        return defaults
    # Host is intentionally frozen to IPv4 loopback.  Tokens and token hashes
    # are owned by the secret store and are never copied from this mapping.
    return {
        "enabled": _coerce_bool(value.get("enabled"), False),
        "host": LOCAL_API_LOOPBACK,
        "port": _clamp_int(value.get("port"), 1, 65535, LOCAL_API_DEFAULT_PORT),
    }


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else portable_dir() / "app_settings.json"
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        self.pre_v2_backup_path = self.path.with_name(
            f"{self.path.stem}.pre-v2{self.path.suffix}"
        )
        self._lock = _settings_lock(self.path)
        self._read_only_future_schema = False
        self._future_schema_version: int | None = None
        self.last_load_migrated = False
        self._debounce_lock = threading.Lock()
        self._save_timer: threading.Timer | None = None
        self._pending_settings: AppSettings | None = None
        self._pending_callback: Callable[[bool], None] | None = None

    @property
    def is_read_only(self) -> bool:
        return self._read_only_future_schema

    @property
    def future_schema_version(self) -> int | None:
        return self._future_schema_version

    @staticmethod
    def _payload_schema_version(payload: Mapping[str, Any]) -> int:
        if "schema_version" not in payload:
            return 1
        raw_version = payload.get("schema_version")
        if isinstance(raw_version, bool):
            raise ValueError("schema_version 无效")
        if isinstance(raw_version, int):
            version = raw_version
        elif isinstance(raw_version, str) and re.fullmatch(r"\d{1,9}", raw_version.strip()):
            version = int(raw_version)
        else:
            raise ValueError("schema_version 无效")
        if version < 1:
            raise ValueError("schema_version 无效")
        return version

    @classmethod
    def from_payload(cls, payload: object) -> AppSettings:
        if not isinstance(payload, Mapping):
            raise TypeError("设置文件顶层必须是 JSON 对象")
        version = cls._payload_schema_version(payload)
        init_fields = {
            item.name for item in fields(AppSettings)
            if item.init and item.name not in {"schema_version", "_extra_fields"}
        }
        known = {key: payload[key] for key in init_fields if key in payload}
        settings = AppSettings(**known)
        known_on_disk = init_fields | {"schema_version", "_extra_fields"}
        extras: dict[str, Any] = {}
        for raw_key, raw_value in payload.items():
            if not isinstance(raw_key, str) or raw_key in known_on_disk or _is_sensitive_key(raw_key):
                continue
            safe_value = _safe_json_copy(raw_value)
            if safe_value is not _INVALID_JSON:
                extras[raw_key] = safe_value
        settings._extra_fields = extras
        settings.schema_version = version if version > SETTINGS_SCHEMA_VERSION else SETTINGS_SCHEMA_VERSION
        return cls.validate(settings)

    @classmethod
    def _decode_payload(cls, text: str) -> tuple[AppSettings, int]:
        payload = json.loads(text)
        if not isinstance(payload, Mapping):
            raise TypeError("设置文件顶层必须是 JSON 对象")
        version = cls._payload_schema_version(payload)
        return cls.from_payload(payload), version

    @classmethod
    def _decode(cls, text: str) -> AppSettings:
        return cls._decode_payload(text)[0]

    @classmethod
    def from_file(cls, path: Path) -> AppSettings:
        return cls._decode(cls._read_text(Path(path)))

    @staticmethod
    def _read_text(path: Path) -> str:
        if path.stat().st_size > MAX_SETTINGS_FILE_BYTES:
            raise ValueError("设置文件过大")
        return path.read_text(encoding="utf-8")

    @classmethod
    def to_payload(cls, settings: AppSettings) -> dict[str, Any]:
        validated = cls.validate(settings)
        payload: dict[str, Any] = {}
        safe_extras = _safe_json_copy(validated._extra_fields)
        if isinstance(safe_extras, dict):
            payload.update(safe_extras)
        payload["schema_version"] = validated.schema_version
        for item in fields(AppSettings):
            if item.name in {"schema_version", "_extra_fields"}:
                continue
            safe_value = _safe_json_copy(getattr(validated, item.name))
            if safe_value is _INVALID_JSON:
                raise ValueError(f"设置字段 {item.name} 无法序列化")
            payload[item.name] = safe_value
        return payload

    @classmethod
    def _serialize(cls, settings: AppSettings) -> str:
        return json.dumps(
            cls.to_payload(settings), ensure_ascii=False, indent=2, allow_nan=False
        )

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

    @staticmethod
    def _restrict_private_file(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _ensure_pre_v2_backup(self, original_text: str) -> bool:
        if self.pre_v2_backup_path.exists():
            try:
                _, version = self._decode_payload(self._read_text(self.pre_v2_backup_path))
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                return False
            return version < SETTINGS_SCHEMA_VERSION
        try:
            self._atomic_write(self.pre_v2_backup_path, original_text)
            self._restrict_private_file(self.pre_v2_backup_path)
            return True
        except (OSError, UnicodeError):
            return False

    def _set_future_read_only(self, version: int) -> None:
        self._read_only_future_schema = True
        self._future_schema_version = version

    def _clear_future_read_only(self) -> None:
        self._read_only_future_schema = False
        self._future_schema_version = None

    def _migrate_primary(self, original_text: str, settings: AppSettings) -> bool:
        if not self._ensure_pre_v2_backup(original_text):
            return False
        try:
            # Keep the prior valid version in the existing recovery channel too.
            self._atomic_write(self.backup_path, original_text)
            self._atomic_write(self.path, self._serialize(settings))
            self.last_load_migrated = True
            return True
        except (OSError, TypeError, ValueError, UnicodeError, RecursionError):
            return False

    def _recover(self) -> AppSettings:
        for recovery_path in (self.backup_path, self.pre_v2_backup_path):
            try:
                raw_text = self._read_text(recovery_path)
                recovered, version = self._decode_payload(raw_text)
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                continue
            if version > SETTINGS_SCHEMA_VERSION:
                self._set_future_read_only(version)
                return recovered
            self._clear_future_read_only()
            try:
                if version < SETTINGS_SCHEMA_VERSION and not self._ensure_pre_v2_backup(raw_text):
                    return recovered
                self._atomic_write(self.path, self._serialize(recovered))
            except (OSError, TypeError, ValueError, UnicodeError, RecursionError):
                pass
            return recovered
        self._clear_future_read_only()
        return AppSettings()

    def load(self) -> AppSettings:
        with self._lock:
            self.last_load_migrated = False
            try:
                original_text = self._read_text(self.path)
                settings, version = self._decode_payload(original_text)
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                return self._recover()
            if version > SETTINGS_SCHEMA_VERSION:
                self._set_future_read_only(version)
                return settings
            self._clear_future_read_only()
            if version < SETTINGS_SCHEMA_VERSION:
                self._migrate_primary(original_text, settings)
            return settings

    def save(self, settings: AppSettings) -> bool:
        """Atomically save current settings while retaining the previous valid version."""

        try:
            validated = self.validate(settings)
            if validated.schema_version > SETTINGS_SCHEMA_VERSION:
                return False
            validated.schema_version = SETTINGS_SCHEMA_VERSION
            serialized = self._serialize(validated)
        except (TypeError, ValueError, OverflowError, RecursionError):
            return False
        with self._lock:
            if self._read_only_future_schema:
                return False
            current_text = ""
            try:
                current_text = self._read_text(self.path)
                _, current_version = self._decode_payload(current_text)
                if current_version > SETTINGS_SCHEMA_VERSION:
                    self._set_future_read_only(current_version)
                    return False
                if current_version < SETTINGS_SCHEMA_VERSION and not self._ensure_pre_v2_backup(current_text):
                    return False
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                current_text = ""
            try:
                if current_text:
                    self._atomic_write(self.backup_path, current_text)
                self._atomic_write(self.path, serialized)
                return True
            except (OSError, UnicodeError):
                return False

    def schedule_save(
        self,
        settings: AppSettings,
        *,
        delay_ms: int = DEFAULT_SAVE_DEBOUNCE_MS,
        callback: Callable[[bool], None] | None = None,
    ) -> threading.Timer:
        """Debounce a save for UI callers; the supported window is 500–750 ms."""

        delay = _clamp_int(
            delay_ms, MIN_SAVE_DEBOUNCE_MS, MAX_SAVE_DEBOUNCE_MS,
            DEFAULT_SAVE_DEBOUNCE_MS,
        )
        # Freeze caller-owned mutable lists/dicts at the request boundary.
        frozen = self._decode(self._serialize(settings))
        with self._debounce_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            self._pending_settings = frozen
            self._pending_callback = callback
            timer = threading.Timer(delay / 1000, self._run_pending_save)
            timer.daemon = True
            self._save_timer = timer
            timer.start()
            return timer

    save_debounced = schedule_save
    request_save = schedule_save

    def _run_pending_save(self) -> None:
        with self._debounce_lock:
            pending = self._pending_settings
            callback = self._pending_callback
            self._pending_settings = None
            self._pending_callback = None
            self._save_timer = None
        result = self.save(pending) if pending is not None else True
        if callback is not None:
            try:
                callback(result)
            except Exception:
                pass

    def flush_pending_save(self) -> bool:
        with self._debounce_lock:
            timer = self._save_timer
            pending = self._pending_settings
            callback = self._pending_callback
            self._save_timer = None
            self._pending_settings = None
            self._pending_callback = None
            if timer is not None:
                timer.cancel()
        result = self.save(pending) if pending is not None else True
        if callback is not None:
            try:
                callback(result)
            except Exception:
                pass
        return result

    def cancel_pending_save(self) -> None:
        with self._debounce_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            self._save_timer = None
            self._pending_settings = None
            self._pending_callback = None

    @staticmethod
    def validate(settings: AppSettings) -> AppSettings:
        if not isinstance(settings, AppSettings):
            raise TypeError("设置对象类型无效")
        defaults = AppSettings()
        zone_names = set(_cached_timezone_names())
        if not isinstance(settings.theme, str) or settings.theme not in SUPPORTED_THEME_NAMES:
            settings.theme = defaults.theme
        settings.language = normalize_language(settings.language)
        if not isinstance(settings.timezone, str) or settings.timezone not in zone_names:
            settings.timezone = defaults.timezone
        settings.fiat_refresh_minutes = _clamp_int(
            settings.fiat_refresh_minutes, 1, 1440, defaults.fiat_refresh_minutes
        )
        settings.crypto_refresh_minutes = _clamp_int(
            settings.crypto_refresh_minutes, 1, 1440, defaults.crypto_refresh_minutes
        )
        settings.history_limit = _clamp_int(
            settings.history_limit, 1, 200, defaults.history_limit
        )
        settings.cache_limit_mb = _clamp_int(
            settings.cache_limit_mb, 0, 10240, defaults.cache_limit_mb
        )
        for attr in (
            "keep_data_with_app", "auto_refresh_enabled", "refresh_when_minimized",
            "remember_last_page", "remember_window_geometry", "remember_calculator_mode",
            "retain_history",
        ):
            setattr(settings, attr, _coerce_bool(getattr(settings, attr, None), getattr(defaults, attr)))
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
            geometry_valid = (
                800 <= width <= 16384 and 600 <= height <= 16384
                and abs(x) <= 32768 and abs(y) <= 32768
            )
        else:
            geometry_valid = False
        if not geometry_valid:
            settings.window_geometry = defaults.window_geometry
        # Since 3.21.3 the title-bar close button is unambiguous: it always
        # exits. Migrate the former "minimize" choice so old installations do
        # not remain hidden in the background after an upgrade.
        settings.close_action = "exit"
        if not isinstance(settings.startup_page, str) or settings.startup_page not in LEGACY_NAVIGATION_PAGES:
            settings.startup_page = defaults.startup_page
        if not isinstance(settings.last_page, str) or settings.last_page not in LEGACY_NAVIGATION_PAGES:
            settings.last_page = defaults.last_page
        if not isinstance(settings.default_calculator_mode, str) or settings.default_calculator_mode not in {"standard", "professional"}:
            settings.default_calculator_mode = defaults.default_calculator_mode
        if not isinstance(settings.last_calculator_mode, str) or settings.last_calculator_mode not in {"standard", "professional"}:
            settings.last_calculator_mode = defaults.last_calculator_mode
        if not isinstance(settings.calculator_angle_mode, str) or settings.calculator_angle_mode not in {"DEG", "RAD"}:
            settings.calculator_angle_mode = defaults.calculator_angle_mode
        if not isinstance(settings.copy_result_format, str) or settings.copy_result_format not in {"number", "grouped", "formula"}:
            settings.copy_result_format = defaults.copy_result_format
        if not isinstance(settings.calculator_history, list):
            settings.calculator_history = []
        settings.calculator_history = [
            [str(item[0])[:512], str(item[1])[:128]] for item in settings.calculator_history
            if isinstance(item, (list, tuple)) and len(item) >= 2
        ][:settings.history_limit]
        for attr in ("favorite_fiats", "pinned_fiats", "favorite_cryptos", "pinned_cryptos"):
            setattr(settings, attr, _normalize_codes(getattr(settings, attr, [])))

        if isinstance(settings.schema_version, bool):
            settings.schema_version = SETTINGS_SCHEMA_VERSION
        else:
            try:
                parsed_schema = int(settings.schema_version)
            except (TypeError, ValueError, OverflowError):
                parsed_schema = SETTINGS_SCHEMA_VERSION
            settings.schema_version = max(SETTINGS_SCHEMA_VERSION, parsed_schema)
        settings.pages = _validate_pages(settings.pages, settings)
        settings.conversion_history = _validate_conversion_history(settings.conversion_history)
        settings.local_api = _validate_local_api(settings.local_api)
        safe_extras = _safe_json_copy(settings._extra_fields)
        settings._extra_fields = safe_extras if isinstance(safe_extras, dict) else {}
        return settings


__all__ = [
    "AppSettings",
    "CONVERSION_HISTORY_LIMIT",
    "CONVERSION_HISTORY_RETENTION_DAYS",
    "CURRENT_PAGE_NAMES",
    "DEFAULT_EXCHANGE_CURRENCIES",
    "DEFAULT_SAVE_DEBOUNCE_MS",
    "LOCAL_API_DEFAULT_PORT",
    "LOCAL_API_LOOPBACK",
    "MAX_SETTINGS_FILE_BYTES",
    "SETTINGS_SCHEMA_VERSION",
    "SettingsStore",
    "timezone_names",
]
