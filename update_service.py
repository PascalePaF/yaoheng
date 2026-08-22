"""Secure, user-initiated updates from the public Yaoheng GitHub Releases feed."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import requests

from app_version import APP_USER_AGENT, APP_VERSION


REPOSITORY = "PascalePaF/yaoheng"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_CHECKSUM_BYTES = 64 * 1024
MAX_INSTALLER_BYTES = 200 * 1024 * 1024
MIN_INSTALLER_BYTES = 1 * 1024 * 1024
_VERSION_RE = re.compile(r"v?(\d{1,4}(?:\.\d{1,4}){1,3})\Z", re.IGNORECASE)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DOWNLOAD_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)


class UpdateError(RuntimeError):
    """Expected update-check or download failure safe to show to the user."""


class UpdateSecurityError(UpdateError):
    """Release metadata or downloaded bytes failed a security boundary."""


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    release_name: str
    release_notes: str
    release_url: str
    published_at: str
    installer: ReleaseAsset | None

    @property
    def available(self) -> bool:
        return self.installer is not None and version_is_newer(
            self.latest_version, self.current_version
        )


@dataclass(frozen=True, slots=True)
class DownloadedUpdate:
    info: UpdateInfo
    path: Path
    sha256: str


def parse_version(value: object) -> tuple[int, ...]:
    text = str(value or "").strip()
    match = _VERSION_RE.fullmatch(text)
    if match is None:
        raise UpdateSecurityError("GitHub Release 版本号格式无效")
    parts = tuple(int(part) for part in match.group(1).split("."))
    if any(part > 9999 for part in parts):
        raise UpdateSecurityError("GitHub Release 版本号超出支持范围")
    return parts


def canonical_version(value: object) -> str:
    return ".".join(str(part) for part in parse_version(value))


def version_is_newer(candidate: object, current: object) -> bool:
    left = parse_version(candidate)
    right = parse_version(current)
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UpdateSecurityError("GitHub Release 元数据包含重复字段")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise UpdateSecurityError("GitHub Release 元数据包含无效数值")


def _validate_release_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise UpdateSecurityError("GitHub Release 地址不可信") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.startswith(f"/{REPOSITORY}/releases/")
    ):
        raise UpdateSecurityError("GitHub Release 地址不可信")
    return url


def _validate_asset_url(value: object, *, redirected: bool = False) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise UpdateSecurityError("更新文件下载地址不可信") from exc
    if (
        parsed.scheme != "https"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise UpdateSecurityError("更新文件下载地址不可信")
    if redirected:
        if parsed.hostname not in _DOWNLOAD_HOSTS:
            raise UpdateSecurityError("更新文件重定向到了非 GitHub 主机")
    elif (
        parsed.hostname != "github.com"
        or not parsed.path.startswith(f"/{REPOSITORY}/releases/download/")
    ):
        raise UpdateSecurityError("更新文件不是曜衡 GitHub Release 附件")
    return url


def _validate_api_url(value: object) -> None:
    url = str(value or "").strip()
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise UpdateSecurityError("GitHub API 重定向地址不可信") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path != f"/repos/{REPOSITORY}/releases/latest"
    ):
        raise UpdateSecurityError("GitHub API 重定向地址不可信")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_limited(response: requests.Response, maximum: int) -> bytes:
    try:
        declared = int(response.headers.get("Content-Length", "0") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared < 0 or declared > maximum:
        raise UpdateSecurityError("GitHub 返回内容超过安全上限")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > maximum:
            raise UpdateSecurityError("GitHub 返回内容超过安全上限")
        chunks.append(chunk)
    return b"".join(chunks)


def _asset_mapping(payload: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    assets = payload.get("assets")
    if not isinstance(assets, list) or len(assets) > 64:
        raise UpdateSecurityError("GitHub Release 附件列表无效")
    matches = [asset for asset in assets if isinstance(asset, Mapping) and asset.get("name") == name]
    if len(matches) > 1:
        raise UpdateSecurityError(f"GitHub Release 存在重复附件：{name}")
    return matches[0] if matches else None


def _asset_size(asset: Mapping[str, Any], *, minimum: int, maximum: int) -> int:
    size = asset.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not minimum <= size <= maximum:
        raise UpdateSecurityError("GitHub Release 附件大小无效")
    if asset.get("state") != "uploaded":
        raise UpdateSecurityError("GitHub Release 附件尚未上传完成")
    return size


class GitHubUpdateService:
    """Check, download, verify and launch the published Windows installer."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": APP_USER_AGENT,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def _get(self, url: str, *, timeout: tuple[float, float]) -> requests.Response:
        response: requests.Response | None = None
        try:
            response = self.session.get(url, timeout=timeout, stream=True, allow_redirects=True)
            response.raise_for_status()
        except requests.RequestException as exc:
            if response is not None:
                response.close()
            raise UpdateError("无法连接 GitHub，请检查网络后重试") from exc
        return response

    def _read_checksum(self, asset: Mapping[str, Any], installer_name: str) -> str:
        _asset_size(asset, minimum=32, maximum=MAX_CHECKSUM_BYTES)
        initial_url = _validate_asset_url(asset.get("browser_download_url"))
        response = self._get(initial_url, timeout=(5.0, 12.0))
        try:
            _validate_asset_url(response.url, redirected=True)
            payload = _read_limited(response, MAX_CHECKSUM_BYTES)
        finally:
            response.close()
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise UpdateSecurityError("SHA-256 校验文件编码无效") from exc
        matches: list[str] = []
        for line in text.splitlines():
            match = re.fullmatch(r"([0-9A-Fa-f]{64})\s+\*?(.+)", line.strip())
            if match and match.group(2).strip() == installer_name:
                matches.append(match.group(1).lower())
        if len(matches) != 1:
            raise UpdateSecurityError("SHA-256 校验文件缺少唯一的安装包记录")
        return matches[0]

    def check(self, current_version: str = APP_VERSION) -> UpdateInfo:
        response = self._get(LATEST_RELEASE_API, timeout=(5.0, 12.0))
        try:
            _validate_api_url(response.url)
            payload_bytes = _read_limited(response, MAX_METADATA_BYTES)
        finally:
            response.close()
        try:
            payload = json.loads(
                payload_bytes.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_constant,
            )
        except UpdateSecurityError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateSecurityError("GitHub Release 元数据无法解析") from exc
        if not isinstance(payload, Mapping) or payload.get("draft") is not False or payload.get("prerelease") is not False:
            raise UpdateSecurityError("GitHub 最新版本不是正式公开 Release")

        latest_version = canonical_version(payload.get("tag_name"))
        current = canonical_version(current_version)
        release_url = _validate_release_url(payload.get("html_url"))
        release_name = str(payload.get("name") or f"曜衡 {latest_version}").strip()[:160]
        release_notes = str(payload.get("body") or "").strip()[:20_000]
        published_at = str(payload.get("published_at") or "").strip()[:64]
        if not version_is_newer(latest_version, current):
            return UpdateInfo(
                current, latest_version, release_name, release_notes,
                release_url, published_at, None,
            )

        installer_name = f"Yaoheng-{latest_version}-Windows-x64-Setup.exe"
        installer_asset = _asset_mapping(payload, installer_name)
        checksum_asset = _asset_mapping(payload, "SHA256SUMS.txt")
        if installer_asset is None or checksum_asset is None:
            raise UpdateSecurityError("新版本缺少安装包或 SHA-256 校验文件")
        size = _asset_size(
            installer_asset,
            minimum=MIN_INSTALLER_BYTES,
            maximum=MAX_INSTALLER_BYTES,
        )
        url = _validate_asset_url(installer_asset.get("browser_download_url"))
        checksum = self._read_checksum(checksum_asset, installer_name)
        digest = str(installer_asset.get("digest") or "").strip().lower()
        if digest:
            if not digest.startswith("sha256:") or not _SHA256_RE.fullmatch(digest[7:]):
                raise UpdateSecurityError("GitHub 安装包摘要格式无效")
            if digest[7:] != checksum:
                raise UpdateSecurityError("GitHub 摘要与 SHA-256 校验文件不一致")
        installer = ReleaseAsset(installer_name, url, size, checksum)
        return UpdateInfo(
            current, latest_version, release_name, release_notes,
            release_url, published_at, installer,
        )

    @staticmethod
    def default_download_dir() -> Path:
        return Path(tempfile.gettempdir()) / "YaohengUpdates"

    def download(
        self,
        info: UpdateInfo,
        destination: Path | None = None,
    ) -> DownloadedUpdate:
        asset = info.installer
        if not info.available or asset is None:
            raise UpdateError("当前没有可下载的新版本")
        destination = Path(destination) if destination is not None else self.default_download_dir()
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UpdateError("无法创建更新下载目录") from exc
        target = destination / asset.name
        partial = destination / f"{asset.name}.part"

        if target.is_file() and target.stat().st_size == asset.size:
            existing_hash = _sha256_file(target)
            if existing_hash == asset.sha256:
                return DownloadedUpdate(info, target, existing_hash)

        response = self._get(asset.url, timeout=(5.0, 45.0))
        digest = hashlib.sha256()
        written = 0
        try:
            _validate_asset_url(response.url, redirected=True)
            try:
                declared = int(response.headers.get("Content-Length", "0") or 0)
            except (TypeError, ValueError):
                declared = 0
            if declared and declared != asset.size:
                raise UpdateSecurityError("下载文件大小与 GitHub Release 不一致")
            with partial.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > asset.size or written > MAX_INSTALLER_BYTES:
                        raise UpdateSecurityError("下载文件超过声明大小")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except requests.RequestException as exc:
            partial.unlink(missing_ok=True)
            raise UpdateError("安装包下载中断，请检查网络后重试") from exc
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise UpdateError("无法保存更新安装包") from exc
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        finally:
            response.close()
        actual_hash = digest.hexdigest()
        if written != asset.size or actual_hash != asset.sha256:
            partial.unlink(missing_ok=True)
            raise UpdateSecurityError("安装包 SHA-256 校验失败，文件已丢弃")
        try:
            os.replace(partial, target)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise UpdateError("无法完成更新安装包写入") from exc
        return DownloadedUpdate(info, target, actual_hash)

    @staticmethod
    def launch_installer(download: DownloadedUpdate, install_dir: Path) -> subprocess.Popen[Any]:
        if sys.platform != "win32":
            raise UpdateError("应用内升级安装仅支持 Windows")
        installer = download.info.installer
        path = download.path.resolve()
        if installer is None or path.name != installer.name or not path.is_file():
            raise UpdateSecurityError("待安装文件无效")
        if path.stat().st_size != installer.size:
            raise UpdateSecurityError("待安装文件大小已改变")
        if _sha256_file(path) != download.sha256:
            raise UpdateSecurityError("待安装文件在启动前校验失败")
        destination = Path(install_dir).resolve()
        try:
            return subprocess.Popen(
                [
                    str(path),
                    "/CURRENTUSER",
                    f"/DIR={destination}",
                    "/CLOSEAPPLICATIONS",
                    "/RESTARTAPPLICATIONS",
                ],
                close_fds=True,
            )
        except OSError as exc:
            raise UpdateError("无法启动升级安装包") from exc


__all__ = [
    "DownloadedUpdate",
    "GitHubUpdateService",
    "LATEST_RELEASE_API",
    "RELEASE_PAGE",
    "ReleaseAsset",
    "UpdateError",
    "UpdateInfo",
    "UpdateSecurityError",
    "canonical_version",
    "parse_version",
    "version_is_newer",
]
