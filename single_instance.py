"""Windows single-instance guard and activation signal for Yaoheng."""

from __future__ import annotations

import ctypes
import os
import re
import sys
from typing import Any, Protocol


INSTANCE_NAME = "Local\\Yaoheng.49A035BF-7BEC-4FE1-84C4-EEBFD503A917.Instance.v1"
ACTIVATION_EVENT_NAME = f"{INSTANCE_NAME}.Activate"
TEST_INSTANCE_ENV = "YAO_HENG_TEST_INSTANCE_TOKEN"
_TEST_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")


def resolve_instance_name(test_token: str | None = None) -> str:
    """Return the production name, or a validated namespace for isolated smoke tests."""

    token = os.environ.get(TEST_INSTANCE_ENV, "") if test_token is None else str(test_token)
    normalized = token.strip().lower()
    if _TEST_TOKEN_PATTERN.fullmatch(normalized):
        return f"{INSTANCE_NAME}.Test.{normalized}"
    return INSTANCE_NAME


class SingleInstanceError(RuntimeError):
    """The app could not establish its one-window safety boundary."""


class _KernelBackend(Protocol):
    def create_event(self, name: str) -> Any: ...

    def create_mutex(self, name: str) -> tuple[Any, bool]: ...

    def signal(self, handle: Any) -> None: ...

    def consume(self, handle: Any) -> bool: ...

    def close(self, handle: Any) -> None: ...


class _WindowsKernelBackend:
    ERROR_ALREADY_EXISTS = 183
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258
    WAIT_FAILED = 0xFFFFFFFF

    def __init__(self) -> None:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateEventW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.SetEvent.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32 = kernel32

    @staticmethod
    def _last_error(operation: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{operation} failed with Windows error {code}")

    def create_event(self, name: str) -> Any:
        handle = self.kernel32.CreateEventW(None, False, False, name)
        if not handle:
            raise self._last_error("CreateEventW")
        return handle

    def create_mutex(self, name: str) -> tuple[Any, bool]:
        ctypes.set_last_error(0)
        handle = self.kernel32.CreateMutexW(None, False, name)
        error = ctypes.get_last_error()
        if not handle:
            raise self._last_error("CreateMutexW")
        return handle, error == self.ERROR_ALREADY_EXISTS

    def signal(self, handle: Any) -> None:
        if not self.kernel32.SetEvent(handle):
            raise self._last_error("SetEvent")

    def consume(self, handle: Any) -> bool:
        result = int(self.kernel32.WaitForSingleObject(handle, 0))
        if result == self.WAIT_OBJECT_0:
            return True
        if result == self.WAIT_TIMEOUT:
            return False
        if result == self.WAIT_FAILED:
            raise self._last_error("WaitForSingleObject")
        raise OSError(result, f"unexpected Windows wait result {result}")

    def close(self, handle: Any) -> None:
        if handle:
            self.kernel32.CloseHandle(handle)


class SingleInstance:
    """Own the app mutex and let later launches wake the existing window."""

    def __init__(
        self,
        name: str | None = None,
        *,
        backend: _KernelBackend | None = None,
    ) -> None:
        name = resolve_instance_name() if name is None else name
        self._backend = backend
        self._event: Any = None
        self._mutex: Any = None
        self._closed = False
        self.is_primary = True
        if self._backend is None:
            if sys.platform != "win32":
                return
            try:
                self._backend = _WindowsKernelBackend()
            except (AttributeError, OSError) as exc:
                raise SingleInstanceError("无法初始化 Windows 单实例保护") from exc
        try:
            self._event = self._backend.create_event(f"{name}.Activate")
            self._mutex, already_exists = self._backend.create_mutex(name)
            self.is_primary = not already_exists
        except OSError as exc:
            self.close()
            raise SingleInstanceError("无法建立单实例保护，请重新启动曜衡") from exc

    def notify_existing(self) -> bool:
        if self.is_primary or self._closed or self._backend is None or self._event is None:
            return False
        try:
            self._backend.signal(self._event)
        except OSError:
            return False
        return True

    def consume_activation(self) -> bool:
        if not self.is_primary or self._closed or self._backend is None or self._event is None:
            return False
        try:
            return self._backend.consume(self._event)
        except OSError:
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._backend is not None:
            for handle in (self._mutex, self._event):
                if handle is not None:
                    try:
                        self._backend.close(handle)
                    except OSError:
                        pass
        self._mutex = None
        self._event = None

    def __enter__(self) -> SingleInstance:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


def show_startup_error(message: str) -> None:
    """Show a startup failure even in the windowed, console-free build."""

    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, str(message), "曜衡", 0x10 | 0x0)
    except (AttributeError, OSError):
        pass


__all__ = [
    "ACTIVATION_EVENT_NAME",
    "INSTANCE_NAME",
    "TEST_INSTANCE_ENV",
    "SingleInstance",
    "SingleInstanceError",
    "resolve_instance_name",
    "show_startup_error",
]
