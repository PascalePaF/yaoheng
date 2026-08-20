"""Release artifact integrity, privacy, and license checks for Yaoheng."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import os
import re
import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable


class ReleaseCheckError(RuntimeError):
    """Raised when a release artifact fails a required check."""


_PRIVATE_TOP_LEVEL_NAMES = {
    "app_settings.json",
    "app_settings.json.bak",
    "data",
}
_PRIVATE_FILE_PATTERNS = (
    re.compile(r"^app_settings\.json(?:\.bak)?$", re.IGNORECASE),
    re.compile(r"^rates_cache\.json$", re.IGNORECASE),
    re.compile(r"^(?:fiat_)?chart_.+\.json$", re.IGNORECASE),
    re.compile(r"^\.env(?:\..+)?$", re.IGNORECASE),
)
_TEXT_SUFFIXES = {
    ".cfg", ".csv", ".ini", ".iss", ".json", ".md", ".ps1", ".py",
    ".toml", ".txt", ".xml", ".yaml", ".yml",
}
_SECRET_PATTERNS = (
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "assigned credential",
        re.compile(
            r"(?i)\b(?:api[_-]?key|client[_-]?secret|password)\b\s*[:=]\s*"
            r"[\"'][^\"'\r\n]{8,}[\"']"
        ),
    ),
)
_LICENSE_NAMES = re.compile(r"^(?:license|copying|notice|authors)(?:[._-].*)?$", re.IGNORECASE)


def _sha256_stream(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_stream(handle)


def _is_reparse_point(path: Path) -> bool:
    details = path.lstat()
    attributes = getattr(details, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _staging_files(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        raise ReleaseCheckError(f"staging directory does not exist: {root}")
    files: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if _is_reparse_point(path):
            raise ReleaseCheckError(f"staging contains a reparse point: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        folded = relative.casefold()
        if folded in files:
            raise ReleaseCheckError(f"case-insensitive duplicate path in staging: {relative}")
        files[folded] = path
    return files


def _validate_private_names(relative_paths: Iterable[str]) -> None:
    for relative in relative_paths:
        parts = PurePosixPath(relative).parts
        if not parts:
            continue
        if parts[0].casefold() in _PRIVATE_TOP_LEVEL_NAMES:
            raise ReleaseCheckError(f"private top-level path found in artifact: {relative}")
        if any(pattern.fullmatch(parts[-1]) for pattern in _PRIVATE_FILE_PATTERNS):
            raise ReleaseCheckError(f"private runtime file found in artifact: {relative}")


def _encoded_needles(values: Iterable[str]) -> list[tuple[str, bytes]]:
    encoded: list[tuple[str, bytes]] = []
    seen: set[bytes] = set()
    for value in values:
        if not value:
            continue
        variants = {value, value.replace("\\", "/"), value.replace("/", "\\")}
        for variant in variants:
            for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
                needle = variant.encode(encoding)
                if needle and needle not in seen:
                    seen.add(needle)
                    encoded.append((value, needle))
    return encoded


def _scan_file_for_needles(path: Path, needles: list[tuple[str, bytes]]) -> None:
    if not needles:
        return
    maximum = max(len(needle) for _, needle in needles)
    overlap = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            payload = overlap + chunk
            for label, needle in needles:
                if needle in payload:
                    raise ReleaseCheckError(f"forbidden build-machine string found in {path}: {label}")
            overlap = payload[-(maximum - 1):] if maximum > 1 else b""


def _scan_text_for_secrets(path: Path) -> None:
    if path.suffix.casefold() not in _TEXT_SUFFIXES or path.stat().st_size > 4 * 1024 * 1024:
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise ReleaseCheckError(f"possible {label} found in release text file: {path}")


def verify_staging(root: Path, app_name: str, forbidden_strings: Iterable[str] = ()) -> dict[str, Path]:
    root = root.resolve()
    files = _staging_files(root)
    required = {
        f"{app_name}.exe",
        "app.ico",
        "app.png",
        "使用说明.txt",
        "THIRD-PARTY-NOTICES.txt",
    }
    missing = sorted(name for name in required if name.casefold() not in files)
    if missing:
        raise ReleaseCheckError(f"staging is missing required files: {', '.join(missing)}")
    license_files = [name for name in files if name.startswith("licenses/")]
    if not license_files:
        raise ReleaseCheckError("staging is missing the generated licenses directory")
    relative_names = [path.relative_to(root).as_posix() for path in files.values()]
    _validate_private_names(relative_names)
    needles = _encoded_needles(forbidden_strings)
    for path in files.values():
        _scan_file_for_needles(path, needles)
        _scan_text_for_secrets(path)
    return files


def _safe_zip_path(raw_name: str) -> PurePosixPath:
    if "\\" in raw_name:
        raise ReleaseCheckError(f"ZIP entry uses a non-portable separator: {raw_name}")
    path = PurePosixPath(raw_name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseCheckError(f"unsafe ZIP entry path: {raw_name}")
    if path.parts[0].endswith(":"):
        raise ReleaseCheckError(f"absolute Windows path in ZIP entry: {raw_name}")
    return path


def verify_zip(zip_path: Path, staging_root: Path, app_name: str) -> None:
    staging_root = staging_root.resolve()
    staging_files = _staging_files(staging_root)
    archive_files: dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            path = _safe_zip_path(info.filename)
            if path.parts[0] != app_name:
                raise ReleaseCheckError(f"ZIP entry is outside the {app_name} root: {info.filename}")
            if info.is_dir():
                continue
            if len(path.parts) < 2:
                raise ReleaseCheckError(f"ZIP contains a root-level file: {info.filename}")
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            folded = relative.casefold()
            if folded in archive_files:
                raise ReleaseCheckError(f"duplicate ZIP entry: {info.filename}")
            with archive.open(info) as handle:
                archive_files[folded] = _sha256_stream(handle)
    _validate_private_names(archive_files)
    if set(archive_files) != set(staging_files):
        missing = sorted(set(staging_files) - set(archive_files))
        extra = sorted(set(archive_files) - set(staging_files))
        raise ReleaseCheckError(f"ZIP/staging file list mismatch; missing={missing[:5]}, extra={extra[:5]}")
    for folded, source in staging_files.items():
        if archive_files[folded] != sha256_file(source):
            raise ReleaseCheckError(f"ZIP content differs from staging: {source.relative_to(staging_root)}")


def verify_binary(path: Path, forbidden_strings: Iterable[str] = ()) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ReleaseCheckError(f"release binary does not exist or is empty: {path}")
    _scan_file_for_needles(path, _encoded_needles(forbidden_strings))


def _distribution_license_files(distribution: importlib.metadata.Distribution) -> list[tuple[Path, Path]]:
    found: list[tuple[Path, Path]] = []
    for entry in distribution.files or ():
        parts = list(entry.parts)
        dist_info_index = next(
            (index for index, part in enumerate(parts) if part.casefold().endswith(".dist-info")),
            None,
        )
        if dist_info_index is None:
            continue
        tail = parts[dist_info_index + 1:]
        if not tail or not _LICENSE_NAMES.fullmatch(tail[-1]):
            continue
        if tail[0].casefold() == "licenses":
            tail = tail[1:]
        if not tail:
            tail = [entry.name]
        source = Path(distribution.locate_file(entry)).resolve()
        if source.is_file():
            found.append((source, Path(*tail)))
    return found


def collect_licenses(
    staging_root: Path,
    python_license: Path,
    distribution_specs: Iterable[str],
) -> None:
    staging_root = staging_root.resolve()
    licenses_root = staging_root / "licenses"
    licenses_root.mkdir(parents=True, exist_ok=True)
    if not python_license.is_file():
        raise ReleaseCheckError(f"Python license file was not found: {python_license}")
    python_target = licenses_root / "Python" / "LICENSE.txt"
    python_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(python_license, python_target)

    inventory = [f"Python {sys.version.split()[0]}"]
    for spec in distribution_specs:
        if "==" not in spec:
            raise ReleaseCheckError(f"distribution must be version-pinned: {spec}")
        name, expected_version = spec.rsplit("==", 1)
        distribution = importlib.metadata.distribution(name)
        if distribution.version != expected_version:
            raise ReleaseCheckError(
                f"license version mismatch for {name}: expected {expected_version}, got {distribution.version}"
            )
        license_files = _distribution_license_files(distribution)
        if not license_files:
            raise ReleaseCheckError(f"installed distribution has no discoverable license files: {spec}")
        package_root = licenses_root / re.sub(r"[^A-Za-z0-9_.-]", "-", name)
        for source, relative in license_files:
            target = package_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        inventory.append(spec)

    tcl_license = staging_root / "_internal" / "_tk_data" / "license.terms"
    if not tcl_license.is_file():
        raise ReleaseCheckError(f"bundled Tcl/Tk license was not found: {tcl_license}")
    tcl_target = licenses_root / "Tcl-Tk" / "license.terms"
    tcl_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tcl_license, tcl_target)
    inventory.append("Tcl/Tk (version bundled with Python)")

    openssl_binaries = [
        *staging_root.glob("_internal/libcrypto-*.dll"),
        *staging_root.glob("_internal/libssl-*.dll"),
    ]
    if not openssl_binaries:
        raise ReleaseCheckError("bundled OpenSSL libraries were not found")
    apache_licenses = list((licenses_root / "tzdata").rglob("LICENSE_APACHE"))
    if len(apache_licenses) != 1:
        raise ReleaseCheckError("the Apache License 2.0 text needed for OpenSSL was not found")
    openssl_target = licenses_root / "OpenSSL" / "LICENSE.txt"
    openssl_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(apache_licenses[0], openssl_target)
    inventory.append("OpenSSL (version bundled with Python)")

    (licenses_root / "README.txt").write_text(
        "Bundled components covered by this license directory:\n\n"
        + "\n".join(f"- {item}" for item in inventory)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_checksums(output: Path, assets: Iterable[Path]) -> None:
    asset_paths = [path.resolve() for path in assets]
    if not asset_paths:
        raise ReleaseCheckError("no release assets were supplied for checksums")
    names: set[str] = set()
    lines: list[str] = []
    for path in sorted(asset_paths, key=lambda item: item.name.casefold()):
        if not path.is_file() or path.stat().st_size == 0:
            raise ReleaseCheckError(f"release asset does not exist or is empty: {path}")
        if any(character in path.name for character in "\r\n\t"):
            raise ReleaseCheckError(f"unsafe release asset name: {path.name!r}")
        folded = path.name.casefold()
        if folded in names:
            raise ReleaseCheckError(f"duplicate release asset name: {path.name}")
        names.add(folded)
        lines.append(f"{sha256_file(path)}  {path.name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def _add_forbidden_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--forbid-string", action="append", default=[])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    staging = subparsers.add_parser("verify-staging")
    staging.add_argument("--root", type=Path, required=True)
    staging.add_argument("--app-name", required=True)
    _add_forbidden_argument(staging)

    archive = subparsers.add_parser("verify-zip")
    archive.add_argument("--zip", type=Path, required=True)
    archive.add_argument("--staging", type=Path, required=True)
    archive.add_argument("--app-name", required=True)

    binary = subparsers.add_parser("verify-binary")
    binary.add_argument("--path", type=Path, required=True)
    _add_forbidden_argument(binary)

    licenses = subparsers.add_parser("collect-licenses")
    licenses.add_argument("--staging", type=Path, required=True)
    licenses.add_argument("--python-license", type=Path, required=True)
    licenses.add_argument("--distribution", action="append", default=[], required=True)

    checksums = subparsers.add_parser("checksums")
    checksums.add_argument("--output", type=Path, required=True)
    checksums.add_argument("--asset", type=Path, action="append", default=[], required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "verify-staging":
            verify_staging(args.root, args.app_name, args.forbid_string)
        elif args.command == "verify-zip":
            verify_zip(args.zip, args.staging, args.app_name)
        elif args.command == "verify-binary":
            verify_binary(args.path, args.forbid_string)
        elif args.command == "collect-licenses":
            collect_licenses(args.staging, args.python_license, args.distribution)
        elif args.command == "checksums":
            write_checksums(args.output, args.asset)
    except (OSError, ReleaseCheckError, importlib.metadata.PackageNotFoundError, zipfile.BadZipFile) as exc:
        print(f"release check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
