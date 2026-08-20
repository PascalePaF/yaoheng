"""Authenticated loopback-only HTTP API for 曜衡.

The server is closed until :meth:`LocalAPIServer.start` is called.  It never
touches Tk objects and depends only on the thread-safe :class:`CommandService`
facade plus an injected token verifier.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import queue
import re
import socket
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping
from urllib.parse import unquote_to_bytes, urlsplit

from app_version import APP_VERSION
from command_service import (
    CommandError,
    CommandResult,
    CommandService,
    ParsedCommand,
    parse_command,
)


API_VERSION = "v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17890
MAX_BODY_BYTES = 16 * 1024
_MAX_REJECT_DRAIN_BYTES = 64 * 1024
DEFAULT_RATE_LIMIT = 30
DEFAULT_RATE_WINDOW_SECONDS = 60.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 8.0
DEFAULT_C2C_CONCURRENCY = 2

_ROUTES: dict[str, frozenset[str]] = {
    "/health": frozenset({"GET"}),
    "/v1/capabilities": frozenset({"GET"}),
    "/v1/calculate": frozenset({"POST"}),
    "/v1/convert": frozenset({"POST"}),
    "/v1/command": frozenset({"POST"}),
}
_AUTH_RE = re.compile(r"Bearer ([A-Za-z0-9_-]{1,128})\Z")
_INVALID_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class LocalAPIError(RuntimeError):
    pass


class LocalAPIConfigurationError(LocalAPIError):
    pass


class LocalAPIStartError(LocalAPIError):
    pass


class LocalAPIPortInUseError(LocalAPIStartError):
    pass


class _RequestError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.retryable = bool(retryable)
        self.headers = dict(headers or {})


class _DuplicateJSONKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SafeAccessLog:
    path: str
    status: int
    duration_ms: int
    request_id: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON number")


class _SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float, clock: Callable[[], float]) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._entries: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def allow(self, key: str) -> tuple[bool, int]:
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            entries = self._entries[key]
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if len(entries) >= self._limit:
                retry_after = max(1, int(self._window - (now - entries[0]) + 0.999))
                return False, retry_after
            entries.append(now)
            return True, 0


class _LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    address_family = socket.AF_INET
    request_queue_size = 16

    owner: "LocalAPIServer"

    def handle_error(self, request: object, client_address: object) -> None:
        del request, client_address
        return


class _LocalAPIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Yaoheng-LocalAPI"
    sys_version = ""

    def version_string(self) -> str:
        return f"{self.server_version}/{APP_VERSION}"

    def setup(self) -> None:
        super().setup()
        self._body_consumed = False
        self.connection.settimeout(self.server.owner.read_timeout)

    def log_message(self, _format: str, *args: object) -> None:
        return

    def log_error(self, _format: str, *args: object) -> None:
        return

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del message, explain
        request_id = uuid.uuid4().hex
        status = code if 400 <= int(code) <= 599 else 400
        payload = self._error_envelope(
            request_id,
            "malformed_http_request",
            "HTTP 请求格式无效",
            retryable=False,
        )
        self._send_payload(status, payload)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")

    def do_TRACE(self) -> None:
        self._dispatch("TRACE")

    def do_CONNECT(self) -> None:
        self._dispatch("CONNECT")

    def __getattr__(self, name: str):
        if name.startswith("do_") and len(name) > 3:
            method = name[3:]
            return lambda: self._dispatch(method)
        raise AttributeError(name)

    @property
    def _owner(self) -> "LocalAPIServer":
        return self.server.owner

    @staticmethod
    def _error_envelope(
        request_id: str,
        code: str,
        message: str,
        *,
        retryable: bool,
    ) -> dict[str, object]:
        return {
            "request_id": request_id,
            "api_version": API_VERSION,
            "status": "error",
            "source": "local_api",
            "warnings": [],
            "timestamp": _utc_now(),
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
            },
        }

    @staticmethod
    def _success_envelope(
        request_id: str,
        *,
        source: str,
        data: Mapping[str, object],
        warnings: tuple[str, ...] | list[str] = (),
    ) -> dict[str, object]:
        return {
            "request_id": request_id,
            "api_version": API_VERSION,
            "status": "ok",
            "source": source,
            "warnings": list(warnings),
            "timestamp": _utc_now(),
            "data": dict(data),
        }

    @staticmethod
    def _known_log_path(path: str) -> str:
        return path if path in _ROUTES else "<unknown>"

    def _send_payload(
        self,
        status: int,
        payload: Mapping[str, object],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError):
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            fallback_id = str(payload.get("request_id", uuid.uuid4().hex))
            body = json.dumps(
                self._error_envelope(
                    fallback_id,
                    "internal_error",
                    "服务无法安全序列化响应",
                    retryable=True,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        self.close_connection = True
        try:
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if getattr(self, "command", "") != "HEAD":
                self.wfile.write(body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            self.close_connection = True

    def _parse_path(self) -> str:
        raw_target = self.path
        if not isinstance(raw_target, str) or not raw_target or len(raw_target) > 2048:
            raise _RequestError(400, "invalid_path", "请求路径无效")
        split = urlsplit(raw_target)
        if split.scheme or split.netloc or split.query or split.fragment:
            raise _RequestError(400, "invalid_path", "请求路径无效")
        if _INVALID_PERCENT_RE.search(split.path):
            raise _RequestError(400, "invalid_path", "请求路径无效")
        try:
            path = unquote_to_bytes(split.path).decode("utf-8", "strict")
        except (UnicodeError, ValueError) as exc:
            raise _RequestError(400, "invalid_path", "请求路径无效") from exc
        if (
            not path.startswith("/")
            or "\\" in path
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
            or any(segment == ".." for segment in path.split("/"))
        ):
            raise _RequestError(400, "invalid_path", "请求路径无效")
        return path

    def _validate_peer_and_host(self) -> None:
        try:
            peer = ipaddress.ip_address(self.client_address[0])
        except ValueError as exc:
            raise _RequestError(400, "invalid_host", "请求主机无效") from exc
        if not peer.is_loopback:
            raise _RequestError(403, "non_loopback_client", "只允许本机请求")
        host_values = self.headers.get_all("Host", failobj=[])
        if len(host_values) != 1:
            raise _RequestError(400, "invalid_host", "Host 必须是本机地址和实际端口")
        actual_port = self.server.server_address[1]
        allowed = {
            f"127.0.0.1:{actual_port}",
            f"localhost:{actual_port}",
            f"[::1]:{actual_port}",
        }
        host = host_values[0].strip().lower()
        if host not in allowed:
            raise _RequestError(400, "invalid_host", "Host 必须是本机地址和实际端口")

    def _reject_browser_origin(self) -> None:
        if self.headers.get_all("Origin", failobj=[]) or self.headers.get_all(
            "Access-Control-Request-Method", failobj=[]
        ):
            raise _RequestError(403, "origin_forbidden", "不接受浏览器 Origin 请求")

    def _check_rate_limit(self) -> None:
        allowed, retry_after = self._owner._limiter.allow(self.client_address[0])
        if not allowed:
            raise _RequestError(
                429,
                "rate_limited",
                "本机 API 请求过于频繁",
                retryable=True,
                headers={"Retry-After": str(retry_after)},
            )

    def _authenticated(self) -> bool:
        values = self.headers.get_all("Authorization", failobj=[])
        candidate = ""
        well_formed = False
        if len(values) == 1 and len(values[0]) <= 256:
            match = _AUTH_RE.fullmatch(values[0].strip())
            if match is not None:
                candidate = match.group(1)
                well_formed = True
        verifier = self._owner.token_verifier
        try:
            verified = bool(verifier(candidate)) if verifier is not None else False
        except Exception:
            verified = False
        return verified and well_formed

    def _require_authentication(self) -> None:
        if not self._authenticated():
            raise _RequestError(
                401,
                "unauthorized",
                "需要有效的 Bearer 认证",
                headers={"WWW-Authenticate": 'Bearer realm="local-api"'},
            )

    def _read_json_body(self) -> dict[str, object]:
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            raise _RequestError(400, "unsupported_transfer_encoding", "不支持传输编码")
        content_types = self.headers.get_all("Content-Type", failobj=[])
        if len(content_types) != 1 or content_types[0].strip().lower() != "application/json":
            raise _RequestError(415, "unsupported_media_type", "Content-Type 必须为 application/json")
        lengths = self.headers.get_all("Content-Length", failobj=[])
        raw_length = lengths[0].strip() if len(lengths) == 1 else ""
        if (
            len(raw_length) > 10
            or re.fullmatch(r"0|[1-9][0-9]*", raw_length) is None
        ):
            raise _RequestError(411, "length_required", "需要有效的 Content-Length")
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise _RequestError(413, "payload_too_large", "JSON 请求体超过 16 KiB 上限")
        if length == 0:
            raise _RequestError(400, "invalid_json", "请求体必须是 JSON 对象")
        try:
            self._body_consumed = True
            body = self.rfile.read(length)
        except (socket.timeout, TimeoutError) as exc:
            raise _RequestError(408, "request_timeout", "读取请求体超时", retryable=True) from exc
        if len(body) != length:
            raise _RequestError(400, "incomplete_body", "请求体不完整")
        try:
            value = json.loads(
                body.decode("utf-8", "strict"),
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except _DuplicateJSONKey as exc:
            raise _RequestError(400, "duplicate_json_key", "JSON 对象包含重复字段") from exc
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise _RequestError(400, "invalid_json", "请求体必须是有效 JSON 对象") from exc
        if not isinstance(value, dict):
            raise _RequestError(400, "json_object_required", "请求体必须是 JSON 对象")
        return value

    def _discard_small_unread_body(self) -> None:
        """Drain only an already-declared bounded body before closing TCP.

        On Windows, closing a socket with a bounded unread POST body can reset
        the connection before the client receives the safe JSON error.  Large
        or transfer-encoded bodies remain unread.  Drained oversized bodies
        are discarded only; they are never decoded or parsed as JSON.
        """

        if self._body_consumed or self.command not in {"POST", "PUT", "PATCH"}:
            return
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            return
        lengths = self.headers.get_all("Content-Length", failobj=[])
        raw_length = lengths[0].strip() if len(lengths) == 1 else ""
        if (
            len(raw_length) > 10
            or re.fullmatch(r"0|[1-9][0-9]*", raw_length) is None
        ):
            return
        length = int(raw_length)
        if length <= 0 or length > _MAX_REJECT_DRAIN_BYTES:
            return
        self._body_consumed = True
        try:
            self.rfile.read(length)
        except (OSError, TimeoutError):
            pass

    @staticmethod
    def _validate_fields(
        payload: Mapping[str, object],
        *,
        required: frozenset[str],
        optional: frozenset[str] = frozenset(),
    ) -> None:
        if not required.issubset(payload):
            raise _RequestError(400, "missing_field", "请求缺少必填字段")
        if set(payload) - required - optional:
            raise _RequestError(400, "unknown_field", "请求包含未知字段")

    def _run_operation(
        self,
        operation: Callable[[threading.Event], CommandResult],
        *,
        c2c: bool,
    ) -> CommandResult:
        cancel = threading.Event()
        output: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
        self._owner._register_cancel(cancel)

        def run() -> None:
            acquired = False
            try:
                if c2c:
                    acquired = self._owner._c2c_slots.acquire(
                        timeout=self._owner.request_timeout
                    )
                    if not acquired:
                        output.put(("busy", None))
                        return
                if cancel.is_set():
                    output.put(("cancelled", None))
                    return
                output.put(("ok", operation(cancel)))
            except Exception:
                output.put(("error", None))
            finally:
                if acquired:
                    self._owner._c2c_slots.release()
                self._owner._unregister_cancel(cancel)

        worker = threading.Thread(target=run, name="local-api-operation", daemon=True)
        worker.start()
        try:
            state, value = output.get(timeout=self._owner.request_timeout)
        except queue.Empty as exc:
            cancel.set()
            raise _RequestError(
                504,
                "operation_timeout",
                "本机服务处理超时",
                retryable=True,
            ) from exc
        if state == "busy":
            cancel.set()
            raise _RequestError(503, "c2c_busy", "C2C 并发已达上限", retryable=True)
        if state == "cancelled":
            raise _RequestError(504, "operation_cancelled", "本机服务处理已取消", retryable=True)
        if state != "ok" or not isinstance(value, CommandResult):
            raise _RequestError(500, "internal_error", "本机服务处理失败", retryable=True)
        return value

    @staticmethod
    def _result_status(result: CommandResult) -> int:
        if result.ok:
            return 200
        code = result.error.code if result.error is not None else "internal_error"
        if code in {"service_unavailable", "service_error"}:
            return 503
        return 400

    def _send_command_result(self, request_id: str, result: CommandResult) -> int:
        status = self._result_status(result)
        if result.ok:
            envelope = self._success_envelope(
                request_id,
                source=result.source,
                data=result.data,
                warnings=result.warnings,
            )
        else:
            error = result.error
            if error is None:
                envelope = self._error_envelope(
                    request_id,
                    "internal_error",
                    "本机服务处理失败",
                    retryable=True,
                )
            else:
                envelope = self._error_envelope(
                    request_id,
                    error.code,
                    error.message,
                    retryable=error.retryable,
                )
        self._send_payload(status, envelope)
        return status

    def _handle_health(self, request_id: str) -> int:
        payload = self._success_envelope(
            request_id,
            source="local_api",
            data={
                "version": APP_VERSION,
                "service_status": "running",
                "utc_time": _utc_now(),
            },
        )
        self._send_payload(200, payload)
        return 200

    def _handle_capabilities(self, request_id: str) -> int:
        capabilities = self._owner.command_service.capabilities()
        payload = self._success_envelope(
            request_id,
            source="command_service",
            data={"capabilities": capabilities},
        )
        self._send_payload(200, payload)
        return 200

    def _handle_calculate(self, request_id: str) -> int:
        payload = self._read_json_body()
        self._validate_fields(payload, required=frozenset({"expression"}))
        expression = payload["expression"]
        result = self._run_operation(
            lambda _cancel: self._owner.command_service.calculate(expression),
            c2c=False,
        )
        return self._send_command_result(request_id, result)

    def _handle_convert(self, request_id: str) -> int:
        payload = self._read_json_body()
        self._validate_fields(
            payload,
            required=frozenset({"amount", "source", "target"}),
            optional=frozenset({"mode", "provider", "pay"}),
        )
        amount = payload["amount"]
        source = payload["source"]
        target = payload["target"]
        mode = payload.get("mode", "market")
        if not all(isinstance(value, str) for value in (amount, source, target, mode)):
            raise _RequestError(400, "invalid_field_type", "换算字段必须为文本")
        if str(mode).strip().lower() in {"market", "fx", "normal"}:
            if "provider" in payload or "pay" in payload:
                raise _RequestError(400, "invalid_parameter_combination", "普通换算不接受 C2C 参数")
            result = self._run_operation(
                lambda _cancel: self._owner.command_service.convert(amount, source, target),
                c2c=False,
            )
            return self._send_command_result(request_id, result)
        if str(mode).strip().lower() != "c2c":
            raise _RequestError(400, "invalid_mode", "换算模式无效")
        provider = payload.get("provider", "auto")
        payment = payload.get("pay", "")
        if not isinstance(provider, str) or not isinstance(payment, str):
            raise _RequestError(400, "invalid_field_type", "C2C 参数必须为文本")
        synthesized = f"兑换 {amount} {source} {target} --mode c2c --provider {provider}"
        if payment:
            synthesized += f" --pay {payment}"
        try:
            parsed = parse_command(synthesized)
        except CommandError as exc:
            error_result = CommandResult(
                status="error",
                kind="c2c",
                source="command_service",
                data={},
                error=self._owner.command_error_data(exc),
            )
            return self._send_command_result(request_id, error_result)
        result = self._run_operation(
            lambda cancel: self._owner.command_service.quote_c2c(
                parsed,
                request_id=request_id,
                cancel=cancel,
            ),
            c2c=True,
        )
        return self._send_command_result(request_id, result)

    def _handle_command(self, request_id: str) -> int:
        payload = self._read_json_body()
        self._validate_fields(payload, required=frozenset({"command"}))
        command = payload["command"]
        if not isinstance(command, str):
            raise _RequestError(400, "invalid_field_type", "command 必须为文本")
        try:
            parsed = parse_command(command)
            is_c2c = parsed.kind == "c2c"
        except CommandError:
            is_c2c = False
        result = self._run_operation(
            lambda cancel: self._owner.command_service.execute(
                command,
                request_id=request_id,
                cancel=cancel,
            ),
            c2c=is_c2c,
        )
        return self._send_command_result(request_id, result)

    def _dispatch(self, method: str) -> None:
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        status = 500
        log_path = "<unknown>"
        try:
            path = self._parse_path()
            log_path = self._known_log_path(path)
            self._validate_peer_and_host()
            self._reject_browser_origin()
            self._check_rate_limit()
            allowed = _ROUTES.get(path)
            if allowed is None:
                raise _RequestError(404, "not_found", "端点不存在")
            if method not in allowed:
                raise _RequestError(
                    405,
                    "method_not_allowed",
                    "该端点不允许此方法",
                    headers={"Allow": ", ".join(sorted(allowed))},
                )
            if path != "/health":
                self._require_authentication()
            if path == "/health":
                status = self._handle_health(request_id)
            elif path == "/v1/capabilities":
                status = self._handle_capabilities(request_id)
            elif path == "/v1/calculate":
                status = self._handle_calculate(request_id)
            elif path == "/v1/convert":
                status = self._handle_convert(request_id)
            else:
                status = self._handle_command(request_id)
        except _RequestError as exc:
            status = exc.status
            self._discard_small_unread_body()
            self._send_payload(
                status,
                self._error_envelope(
                    request_id,
                    exc.code,
                    exc.message,
                    retryable=exc.retryable,
                ),
                headers=exc.headers,
            )
        except Exception:
            status = 500
            self._discard_small_unread_body()
            self._send_payload(
                status,
                self._error_envelope(
                    request_id,
                    "internal_error",
                    "本机 API 内部失败",
                    retryable=True,
                ),
            )
        finally:
            self._owner._log_access(
                SafeAccessLog(
                    path=log_path,
                    status=int(status),
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    request_id=request_id,
                )
            )


class LocalAPIServer:
    """Idempotent lifecycle wrapper around a loopback ThreadingHTTPServer."""

    def __init__(
        self,
        command_service: CommandService | None = None,
        token_verifier: Callable[[str], bool] | object | None = None,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        test_mode: bool = False,
        rate_limit: int = DEFAULT_RATE_LIMIT,
        rate_window_seconds: float = DEFAULT_RATE_WINDOW_SECONDS,
        c2c_max_concurrency: int = DEFAULT_C2C_CONCURRENCY,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        read_timeout: float = 5.0,
        access_logger: Callable[[SafeAccessLog], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if host != DEFAULT_HOST:
            raise LocalAPIConfigurationError("生产本机 API 只允许绑定 127.0.0.1")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise LocalAPIConfigurationError("本机 API 端口无效")
        if port == 0 and not test_mode:
            raise LocalAPIConfigurationError("生产本机 API 不允许随机端口")
        if isinstance(rate_limit, bool) or not isinstance(rate_limit, int) or not 1 <= rate_limit <= 10000:
            raise LocalAPIConfigurationError("本机 API 限流配置无效")
        if not 1.0 <= float(rate_window_seconds) <= 3600.0:
            raise LocalAPIConfigurationError("本机 API 限流窗口无效")
        if (
            isinstance(c2c_max_concurrency, bool)
            or not isinstance(c2c_max_concurrency, int)
            or not 1 <= c2c_max_concurrency <= DEFAULT_C2C_CONCURRENCY
        ):
            raise LocalAPIConfigurationError("C2C 最大并发必须为 1 或 2")
        if not 0.05 <= float(request_timeout) <= 60.0:
            raise LocalAPIConfigurationError("本机 API 请求超时配置无效")
        if not 0.1 <= float(read_timeout) <= 30.0:
            raise LocalAPIConfigurationError("本机 API 读取超时配置无效")
        self.command_service = command_service or CommandService()
        if token_verifier is not None and not callable(token_verifier):
            verifier = getattr(token_verifier, "verify", None)
            if not callable(verifier):
                raise LocalAPIConfigurationError("令牌校验器无效")
            self.token_verifier: Callable[[str], bool] | None = verifier
        else:
            self.token_verifier = token_verifier  # type: ignore[assignment]
        self.host = host
        self.configured_port = port
        self.test_mode = bool(test_mode)
        self.request_timeout = float(request_timeout)
        self.read_timeout = float(read_timeout)
        self._access_logger = access_logger
        self._limiter = _SlidingWindowLimiter(rate_limit, float(rate_window_seconds), clock)
        self._c2c_slots = threading.BoundedSemaphore(c2c_max_concurrency)
        self._lifecycle_lock = threading.RLock()
        self._cancel_lock = threading.Lock()
        self._active_cancels: set[threading.Event] = set()
        self._httpd: _LoopbackHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._bound_port: int | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(host={self.host!r}, port={self.configured_port}, "
            f"running={self.is_running})"
        )

    @staticmethod
    def command_error_data(error: CommandError):
        from command_service import CommandErrorData

        return CommandErrorData(error.code, str(error), error.retryable)

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return (
                self._httpd is not None
                and self._thread is not None
                and self._thread.is_alive()
            )

    @property
    def port(self) -> int:
        with self._lifecycle_lock:
            return self._bound_port if self._bound_port is not None else self.configured_port

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _log_access(self, record: SafeAccessLog) -> None:
        if self._access_logger is None:
            return
        try:
            self._access_logger(record)
        except Exception:
            pass

    def _register_cancel(self, cancel: threading.Event) -> None:
        with self._cancel_lock:
            self._active_cancels.add(cancel)

    def _unregister_cancel(self, cancel: threading.Event) -> None:
        with self._cancel_lock:
            self._active_cancels.discard(cancel)

    def _cancel_active_operations(self) -> None:
        with self._cancel_lock:
            active = tuple(self._active_cancels)
        for cancel in active:
            cancel.set()

    def start(self) -> int:
        with self._lifecycle_lock:
            if self._httpd is not None and self._thread is not None and self._thread.is_alive():
                return self.port
            if self._httpd is not None:
                try:
                    self._httpd.server_close()
                finally:
                    self._httpd = None
                    self._thread = None
                    self._bound_port = None
            httpd: _LoopbackHTTPServer | None = None
            try:
                httpd = _LoopbackHTTPServer((self.host, self.configured_port), _LocalAPIHandler)
                httpd.owner = self
            except OSError as exc:
                if httpd is not None:
                    httpd.server_close()
                if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
                    raise LocalAPIPortInUseError(
                        f"本机 API 端口 {self.configured_port} 已被占用"
                    ) from exc
                raise LocalAPIStartError(
                    f"无法在 127.0.0.1:{self.configured_port} 启动本机 API"
                ) from exc
            self._limiter.reset()
            self._httpd = httpd
            self._bound_port = int(httpd.server_address[1])
            thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="yaoheng-local-api",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return self._bound_port

    def stop(self) -> bool:
        with self._lifecycle_lock:
            httpd = self._httpd
            thread = self._thread
            if httpd is None:
                return False
        self._cancel_active_operations()
        if thread is not threading.current_thread():
            httpd.shutdown()
        httpd.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.request_timeout + 0.5))
        with self._lifecycle_lock:
            if self._httpd is httpd:
                self._httpd = None
                self._thread = None
                self._bound_port = None
        return True

    close = stop

    def __enter__(self) -> "LocalAPIServer":
        self.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.stop()


def create_test_server(
    command_service: CommandService | None = None,
    token_verifier: Callable[[str], bool] | object | None = None,
    **kwargs: object,
) -> LocalAPIServer:
    """Create an explicit ephemeral-port server for isolated tests only."""

    kwargs.pop("port", None)
    kwargs.pop("test_mode", None)
    return LocalAPIServer(
        command_service,
        token_verifier,
        port=0,
        test_mode=True,
        **kwargs,
    )


__all__ = [
    "API_VERSION",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "LocalAPIConfigurationError",
    "LocalAPIError",
    "LocalAPIPortInUseError",
    "LocalAPIServer",
    "LocalAPIStartError",
    "MAX_BODY_BYTES",
    "SafeAccessLog",
    "create_test_server",
]
