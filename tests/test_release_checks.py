import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.release_checks import (
    ReleaseCheckError,
    validate_release_asset_names,
    verify_staging,
    verify_zip,
    write_checksums,
)


class ReleaseChecksTests(unittest.TestCase):
    @staticmethod
    def make_staging(root: Path) -> Path:
        staging = root / "曜衡"
        staging.mkdir()
        for name in ("曜衡.exe", "app.ico", "app.png", "使用说明.txt", "THIRD-PARTY-NOTICES.txt"):
            (staging / name).write_bytes(f"fixture:{name}".encode("utf-8"))
        licenses = staging / "licenses"
        licenses.mkdir()
        (licenses / "LICENSE.txt").write_text("fixture license", encoding="utf-8")
        return staging

    @staticmethod
    def make_zip(staging: Path, target: Path) -> None:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(path, (Path(staging.name) / path.relative_to(staging)).as_posix())

    def test_valid_staging_and_matching_zip_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = self.make_staging(root)
            archive = root / "portable.zip"
            self.make_zip(staging, archive)

            verify_staging(staging, "曜衡")
            verify_zip(archive, staging, "曜衡")

    def test_private_runtime_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            staging = self.make_staging(Path(directory))
            (staging / "app_settings.json.bak").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ReleaseCheckError, "private (?:top-level path|runtime file)"):
                verify_staging(staging, "曜衡")

    def test_build_machine_paths_and_secret_tokens_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            staging = self.make_staging(Path(directory))
            guide = staging / "使用说明.txt"
            guide.write_text("built from D:\\private\\worktree", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseCheckError, "forbidden build-machine string"):
                verify_staging(staging, "曜衡", ["D:\\private\\worktree"])

            fake_credential = "api" + '_key = "' + "1234567890abcdef" + '"'
            guide.write_text(fake_credential, encoding="utf-8")
            with self.assertRaisesRegex(ReleaseCheckError, "assigned credential"):
                verify_staging(staging, "曜衡")

    def test_zip_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = self.make_staging(root)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("曜衡/../escaped.txt", "bad")

            with self.assertRaisesRegex(ReleaseCheckError, "unsafe ZIP entry"):
                verify_zip(archive, staging, "曜衡")

    def test_checksum_manifest_is_sorted_and_uses_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            second = root / "b.zip"
            first = root / "a.exe"
            second.write_bytes(b"second")
            first.write_bytes(b"first")
            manifest = root / "SHA256SUMS.txt"

            write_checksums(manifest, [second, first])

            lines = manifest.read_text(encoding="utf-8").splitlines()
            self.assertEqual([line.split("  ", 1)[1] for line in lines], ["a.exe", "b.zip"])
            self.assertTrue(all(len(line.split("  ", 1)[0]) == 64 for line in lines))

    def test_release_asset_names_are_stable_ascii(self):
        expected = [
            "Yaoheng-3.16-Windows-x64-Setup.exe",
            "Yaoheng-3.16-Windows-x64-Portable.zip",
        ]
        validate_release_asset_names(expected)

        for unsafe in (
            "曜衡-3.16-Windows-x64-安装版.exe",
            "nested/Yaoheng.zip",
            "Yaoheng Portable.zip",
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(ReleaseCheckError, "ASCII"):
                    validate_release_asset_names([unsafe])

        with self.assertRaisesRegex(ReleaseCheckError, "duplicate"):
            validate_release_asset_names([expected[0], expected[0].upper()])

    def test_windows_build_configs_use_expected_release_asset_names(self):
        project_root = Path(__file__).resolve().parents[1]
        build_script = (project_root / "build.ps1").read_text(encoding="utf-8-sig")
        installer_script = (project_root / "installer" / "installer.iss").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn('$ReleaseAssetStem = "Yaoheng-{0}-Windows-x64" -f $AppVersion', build_script)
        self.assertIn('$PortableAssetName = $ReleaseAssetStem + "-Portable.zip"', build_script)
        self.assertIn('$InstallerBaseName = $ReleaseAssetStem + "-Setup"', build_script)
        self.assertIn('$LegacyPublishablePaths = @($LegacyZipPath)', build_script)
        self.assertIn(
            "OutputBaseFilename=Yaoheng-{#AppVersion}-Windows-x64-Setup",
            installer_script,
        )


if __name__ == "__main__":
    unittest.main()
