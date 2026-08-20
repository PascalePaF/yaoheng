"""Private, hash-only storage for the local API bearer token.

Only a salted scrypt verifier is persisted.  The 256-bit bearer token is
returned directly from :meth:`generate` or :meth:`rotate` and is never kept on
the store object, serialized, logged, or included in an exception.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import secrets
import stat
import subprocess
import tempfile
import threading
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


STORE_VERSION = 1
TOKEN_BYTES = 32
SALT_BYTES = 32
MAX_STORE_BYTES = 16 * 1024

DEFAULT_SCRYPT_N = 1 << 14
DEFAULT_SCRYPT_R = 8
DEFAULT_SCRYPT_P = 1
DEFAULT_DKLEN = 32

MIN_SCRYPT_N = 1 << 12
MAX_SCRYPT_N = 1 << 17
MAX_SCRYPT_R = 16
MAX_SCRYPT_P = 4
MAX_SCRYPT_REQUIRED_MEMORY = 128 * 1024 * 1024
MAX_SCRYPT_MEMORY = 256 * 1024 * 1024
MAX_SCRYPT_WORK = 1 << 18


class SecretStoreError(RuntimeError):
    """Base class for redacted secret-store failures."""


class SecretAlreadyExistsError(SecretStoreError):
    pass


class SecretStoreCorruptError(SecretStoreError):
    pass


class SecretStoreLockError(SecretStoreError):
    pass


class SecretStorePermissionWarning(UserWarning):
    """Raised visibly when a best-effort filesystem restriction fails."""


@dataclass(frozen=True, slots=True)
class _ScryptParameters:
    n: int = DEFAULT_SCRYPT_N
    r: int = DEFAULT_SCRYPT_R
    p: int = DEFAULT_SCRYPT_P
    dklen: int = DEFAULT_DKLEN

    def validate(self) -> None:
        if (
            isinstance(self.n, bool)
            or not isinstance(self.n, int)
            or self.n < MIN_SCRYPT_N
            or self.n > MAX_SCRYPT_N
            or self.n & (self.n - 1)
        ):
            raise SecretStoreCorruptError("令牌存储的 KDF 参数无效")
        if (
            isinstance(self.r, bool)
            or not isinstance(self.r, int)
            or not 1 <= self.r <= MAX_SCRYPT_R
        ):
            raise SecretStoreCorruptError("令牌存储的 KDF 参数无效")
        if (
            isinstance(self.p, bool)
            or not isinstance(self.p, int)
            or not 1 <= self.p <= MAX_SCRYPT_P
        ):
            raise SecretStoreCorruptError("令牌存储的 KDF 参数无效")
        if self.dklen not in {32, 48, 64}:
            raise SecretStoreCorruptError("令牌存储的 KDF 参数无效")
        if (
            128 * self.n * self.r > MAX_SCRYPT_REQUIRED_MEMORY
            or self.n * self.p > MAX_SCRYPT_WORK
        ):
            raise SecretStoreCorruptError("令牌存储的 KDF 参数超出安全上限")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": "scrypt",
            "n": self.n,
            "r": self.r,
            "p": self.p,
            "dklen": self.dklen,
        }


@dataclass(frozen=True, slots=True)
class _VerifierRecord:
    salt: bytes
    digest: bytes
    parameters: _ScryptParameters

    def to_bytes(self) -> bytes:
        payload = {
            "version": STORE_VERSION,
            "salt": _encode_bytes(self.salt),
            "kdf": self.parameters.to_dict(),
            "hash": _encode_bytes(self.digest),
        }
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_bytes(value: object, *, minimum: int, maximum: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise SecretStoreCorruptError("令牌存储编码无效")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise SecretStoreCorruptError("令牌存储编码无效") from exc
    if not minimum <= len(decoded) <= maximum:
        raise SecretStoreCorruptError("令牌存储编码无效")
    return decoded


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SecretStoreCorruptError("令牌存储包含重复字段")
        result[key] = value
    return result


def _parse_record(data: bytes) -> _VerifierRecord:
    if not data or len(data) > MAX_STORE_BYTES:
        raise SecretStoreCorruptError("令牌存储为空或过大")
    try:
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                SecretStoreCorruptError("令牌存储包含非法数值")
            ),
        )
    except SecretStoreCorruptError:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SecretStoreCorruptError("令牌存储已损坏") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "salt", "kdf", "hash"}:
        raise SecretStoreCorruptError("令牌存储结构无效")
    if type(payload.get("version")) is not int or payload.get("version") != STORE_VERSION:
        raise SecretStoreCorruptError("令牌存储版本不受支持")
    raw_kdf = payload.get("kdf")
    if not isinstance(raw_kdf, dict) or set(raw_kdf) != {"name", "n", "r", "p", "dklen"}:
        raise SecretStoreCorruptError("令牌存储的 KDF 结构无效")
    if raw_kdf.get("name") != "scrypt":
        raise SecretStoreCorruptError("令牌存储的 KDF 不受支持")
    parameters = _ScryptParameters(
        n=raw_kdf.get("n"),  # type: ignore[arg-type]
        r=raw_kdf.get("r"),  # type: ignore[arg-type]
        p=raw_kdf.get("p"),  # type: ignore[arg-type]
        dklen=raw_kdf.get("dklen"),  # type: ignore[arg-type]
    )
    parameters.validate()
    salt = _decode_bytes(payload.get("salt"), minimum=16, maximum=64)
    digest = _decode_bytes(
        payload.get("hash"),
        minimum=parameters.dklen,
        maximum=parameters.dklen,
    )
    return _VerifierRecord(salt=salt, digest=digest, parameters=parameters)


def _derive(candidate: bytes, salt: bytes, parameters: _ScryptParameters) -> bytes:
    parameters.validate()
    try:
        return hashlib.scrypt(
            candidate,
            salt=salt,
            n=parameters.n,
            r=parameters.r,
            p=parameters.p,
            maxmem=MAX_SCRYPT_MEMORY,
            dklen=parameters.dklen,
        )
    except (ValueError, OverflowError) as exc:
        raise SecretStoreCorruptError("令牌校验参数不可用") from exc


def _candidate_bytes(token: object) -> tuple[bytes, bool]:
    valid = isinstance(token, str) and 1 <= len(token) <= 128
    raw = b""
    if valid:
        try:
            encoded = token.encode("ascii")  # type: ignore[union-attr]
            padded = encoded + b"=" * (-len(encoded) % 4)
            raw = base64.b64decode(padded, altchars=b"-_", validate=True)
            valid = len(raw) == TOKEN_BYTES and _encode_bytes(raw) == token
        except (UnicodeError, ValueError, TypeError):
            valid = False
    return (raw if valid else b"invalid-local-api-token"), bool(valid)


class SecretStore:
    """Atomic, concurrent verifier storage for one local API token."""

    _registry_guard = threading.Lock()
    _thread_locks: dict[str, threading.RLock] = {}
    _windows_sid: str | None = None

    __slots__ = (
        "_path",
        "_backup_path",
        "_lock_path",
        "_parameters",
        "_lock_timeout",
        "_warning_callback",
        "_security_warnings",
        "_thread_lock",
    )

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        lock_timeout: float = 5.0,
        warning_callback: Callable[[str], None] | None = None,
        scrypt_n: int = DEFAULT_SCRYPT_N,
        scrypt_r: int = DEFAULT_SCRYPT_R,
        scrypt_p: int = DEFAULT_SCRYPT_P,
    ) -> None:
        resolved = Path(path).expanduser().resolve(strict=False)
        if resolved.name in {"", ".", ".."}:
            raise SecretStoreError("令牌存储路径无效")
        if not 0.1 <= float(lock_timeout) <= 60:
            raise SecretStoreError("令牌存储锁超时配置无效")
        parameters = _ScryptParameters(n=scrypt_n, r=scrypt_r, p=scrypt_p)
        parameters.validate()
        self._path = resolved
        self._backup_path = resolved.with_name(resolved.name + ".bak")
        self._lock_path = resolved.with_name(resolved.name + ".lock")
        self._parameters = parameters
        self._lock_timeout = float(lock_timeout)
        self._warning_callback = warning_callback
        self._security_warnings: list[str] = []
        registry_key = os.path.normcase(str(resolved))
        with type(self)._registry_guard:
            self._thread_lock = type(self)._thread_locks.setdefault(registry_key, threading.RLock())

    def __repr__(self) -> str:
        return f"{type(self).__name__}(path={str(self._path)!r})"

    @property
    def path(self) -> Path:
        return self._path

    @property
    def backup_path(self) -> Path:
        return self._backup_path

    @property
    def security_warnings(self) -> tuple[str, ...]:
        with self._thread_lock:
            return tuple(self._security_warnings)

    def _report_permission_failure(self, message: str) -> None:
        first_report = message not in self._security_warnings
        if first_report:
            self._security_warnings.append(message)
            warnings.warn(message, SecretStorePermissionWarning, stacklevel=3)
        if self._warning_callback is not None:
            try:
                self._warning_callback(message)
            except Exception:
                pass

    @classmethod
    def _current_windows_sid(cls) -> str | None:
        with cls._registry_guard:
            if cls._windows_sid:
                return cls._windows_sid
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                completed = subprocess.run(
                    ["whoami", "/user", "/fo", "csv", "/nh"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=3,
                    creationflags=creation_flags,
                )
                rows = list(csv.reader(completed.stdout.splitlines()))
                sid = rows[0][1].strip() if completed.returncode == 0 and rows and len(rows[0]) > 1 else ""
                if re_full_sid(sid):
                    cls._windows_sid = sid
                    return sid
            except (OSError, subprocess.SubprocessError, UnicodeError):
                pass
            return None

    def _apply_private_permissions(self, path: Path) -> None:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            self._report_permission_failure("无法限制令牌校验文件权限；请检查配置目录权限。")
            return
        if os.name != "nt":
            try:
                if stat.S_IMODE(path.stat().st_mode) & 0o077:
                    self._report_permission_failure("令牌校验文件仍可被其他账户访问。")
            except OSError:
                self._report_permission_failure("无法确认令牌校验文件权限。")
            return
        sid = self._current_windows_sid()
        if sid is None:
            self._report_permission_failure("无法确认 Windows 当前账户，未能收紧令牌校验文件 ACL。")
            return
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                [
                    "icacls",
                    str(path),
                    "/inheritance:r",
                    "/grant:r",
                    f"*{sid}:(F)",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=creation_flags,
            )
            if completed.returncode != 0:
                self._report_permission_failure("Windows 未能收紧令牌校验文件 ACL。")
        except (OSError, subprocess.SubprocessError):
            self._report_permission_failure("Windows 未能收紧令牌校验文件 ACL。")

    def _ensure_parent(self) -> None:
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise SecretStoreError("无法创建令牌存储目录") from exc

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        self._ensure_parent()
        self._check_regular(self._lock_path)
        try:
            descriptor = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            handle = os.fdopen(descriptor, "r+b", buffering=0)
        except OSError as exc:
            raise SecretStoreLockError("无法打开令牌存储锁") from exc
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                deadline = time.monotonic() + self._lock_timeout
                while True:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError as exc:
                        if time.monotonic() >= deadline:
                            raise SecretStoreLockError("等待令牌存储锁超时") from exc
                        time.sleep(0.025)
            else:
                import fcntl

                deadline = time.monotonic() + self._lock_timeout
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except BlockingIOError as exc:
                        if time.monotonic() >= deadline:
                            raise SecretStoreLockError("等待令牌存储锁超时") from exc
                        time.sleep(0.025)
            yield
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        acquired = self._thread_lock.acquire(timeout=self._lock_timeout)
        if not acquired:
            raise SecretStoreLockError("等待令牌存储线程锁超时")
        try:
            with self._process_lock():
                yield
        finally:
            self._thread_lock.release()

    @staticmethod
    def _check_regular(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SecretStoreError("无法检查令牌存储文件") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SecretStoreCorruptError("令牌存储路径不是普通文件")

    def _read_record(self, path: Path) -> _VerifierRecord:
        self._check_regular(path)
        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_STORE_BYTES + 1)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise SecretStoreError("无法读取令牌存储") from exc
        return _parse_record(data)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        try:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass

    def _replace_bytes(self, target: Path, data: bytes) -> None:
        self._check_regular(target)
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=str(target.parent),
            )
            temporary = Path(raw_path)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._apply_private_permissions(temporary)
            os.replace(temporary, target)
            self._apply_private_permissions(target)
            self._sync_directory(target.parent)
        except OSError as exc:
            raise SecretStoreError("无法原子写入令牌存储") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _write_record(self, record: _VerifierRecord) -> None:
        data = record.to_bytes()
        # Never retain a previous verifier as a fallback after rotation.  The
        # old backup is removed first, the primary changes atomically, and the
        # backup is then synchronized to that same new verifier.
        self._check_regular(self._backup_path)
        try:
            self._backup_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SecretStoreError("无法更新令牌存储备份") from exc
        self._replace_bytes(self._path, data)
        try:
            self._replace_bytes(self._backup_path, data)
        except SecretStoreError:
            warnings.warn(
                "令牌校验主文件已安全写入，但私有备份更新失败。",
                RuntimeWarning,
                stacklevel=2,
            )

    def _record_with_recovery(self) -> _VerifierRecord:
        try:
            return self._read_record(self._path)
        except FileNotFoundError:
            primary_error: Exception | None = None
        except SecretStoreCorruptError as exc:
            primary_error = exc
        try:
            backup = self._read_record(self._backup_path)
        except FileNotFoundError:
            if primary_error is None:
                raise
            raise SecretStoreCorruptError("令牌存储已损坏且没有可用备份") from primary_error
        except SecretStoreCorruptError as exc:
            raise SecretStoreCorruptError("令牌存储及备份均已损坏") from exc
        self._replace_bytes(self._path, backup.to_bytes())
        warnings.warn("已从私有备份恢复令牌校验文件。", RuntimeWarning, stacklevel=3)
        return backup

    def _new_record(self, token_bytes: bytes) -> _VerifierRecord:
        salt = secrets.token_bytes(SALT_BYTES)
        digest = _derive(token_bytes, salt, self._parameters)
        return _VerifierRecord(salt=salt, digest=digest, parameters=self._parameters)

    @staticmethod
    def _issue_token() -> tuple[str, bytes]:
        raw = secrets.token_bytes(TOKEN_BYTES)
        return _encode_bytes(raw), raw

    def generate(self) -> str:
        """Create a verifier and return the new plaintext token exactly once."""

        with self._locked():
            if self._path.exists() or self._backup_path.exists():
                raise SecretAlreadyExistsError("本机 API 令牌已经存在")
            token, raw = self._issue_token()
            self._write_record(self._new_record(raw))
            return token

    def rotate(self) -> str:
        """Replace any verifier and return a fresh plaintext token once."""

        with self._locked():
            if self._path.exists():
                try:
                    self._read_record(self._path)
                except SecretStoreCorruptError:
                    warnings.warn(
                        "损坏的令牌校验文件已由显式轮换替换；现有私有备份未覆盖。",
                        RuntimeWarning,
                        stacklevel=2,
                    )
            token, raw = self._issue_token()
            self._write_record(self._new_record(raw))
            return token

    def verify(self, token: object) -> bool:
        """Verify a bearer token using scrypt and constant-time comparison."""

        with self._locked():
            try:
                record = self._record_with_recovery()
            except FileNotFoundError:
                return False
            candidate, well_formed = _candidate_bytes(token)
            derived = _derive(candidate, record.salt, record.parameters)
            matches = secrets.compare_digest(derived, record.digest) and well_formed
            if matches and record.parameters != self._parameters:
                migrated = self._new_record(candidate)
                self._write_record(migrated)
            return matches

    def exists(self) -> bool:
        """Return whether a primary or recoverable-backup slot is present."""

        with self._locked():
            return self._path.exists() or self._backup_path.exists()

    def delete(self) -> bool:
        """Explicitly disable API authentication by deleting verifier files."""

        removed = False
        with self._locked():
            for path in (self._path, self._backup_path):
                self._check_regular(path)
                try:
                    path.unlink()
                    removed = True
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise SecretStoreError("无法删除令牌校验文件") from exc
            self._sync_directory(self._path.parent)
        return removed

    disable = delete


def re_full_sid(value: str) -> bool:
    """Validate a Windows SID without accepting account names or switches."""

    if not isinstance(value, str) or not value.startswith("S-") or len(value) > 184:
        return False
    parts = value.split("-")
    return len(parts) >= 4 and all(part.isascii() and part.isdigit() for part in parts[1:])


__all__ = [
    "DEFAULT_SCRYPT_N",
    "SecretAlreadyExistsError",
    "SecretStore",
    "SecretStoreCorruptError",
    "SecretStoreError",
    "SecretStoreLockError",
    "SecretStorePermissionWarning",
    "STORE_VERSION",
    "TOKEN_BYTES",
]
