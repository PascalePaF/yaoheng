"""Single runtime version source for Aurora Balance (曜衡)."""

from __future__ import annotations


APP_VERSION = "3.19"
APP_NAME = "曜衡"
APP_PRODUCT_NAME = "Yaoheng"
APP_USER_AGENT = f"{APP_PRODUCT_NAME}/{APP_VERSION} (Windows)"

SETTINGS_SCHEMA_VERSION = 2
RATE_CACHE_SCHEMA_VERSION = 2

__version__ = APP_VERSION


__all__ = [
    "APP_NAME",
    "APP_PRODUCT_NAME",
    "APP_USER_AGENT",
    "APP_VERSION",
    "RATE_CACHE_SCHEMA_VERSION",
    "SETTINGS_SCHEMA_VERSION",
    "__version__",
]
