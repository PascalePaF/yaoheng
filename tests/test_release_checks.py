import tempfile
import unittest
import zipfile
from pathlib import Path

from app_version import APP_VERSION
from tools.release_checks import (
    ReleaseCheckError,
    validate_release_asset_names,
    validate_release_runtime,
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

    def test_local_api_verifiers_and_settings_migration_files_are_rejected(self):
        private_variants = (
            Path("private/local_api_token.json"),
            Path("local_api_token.json.bak"),
            Path(".local_api_token.json.random.tmp"),
            Path("app_settings.pre-v2.json"),
            Path(".app_settings.json.random.tmp"),
        )
        for relative in private_variants:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                staging = self.make_staging(Path(directory))
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("private fixture", encoding="utf-8")

                with self.assertRaisesRegex(
                    ReleaseCheckError,
                    "private (?:top-level path|runtime file)",
                ):
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

    def test_checksum_manifest_name_must_also_be_stable_ascii(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / f"Yaoheng-{APP_VERSION}-Windows-x64-Setup.exe"
            asset.write_bytes(b"fixture")

            with self.assertRaisesRegex(ReleaseCheckError, "ASCII"):
                write_checksums(root / "校验和.txt", [asset])

    def test_release_asset_names_are_stable_ascii(self):
        expected = [
            f"Yaoheng-{APP_VERSION}-Windows-x64-Setup.exe",
            f"Yaoheng-{APP_VERSION}-Windows-x64-Portable.zip",
            "SHA256SUMS.txt",
        ]
        validate_release_asset_names(expected)

        for unsafe in (
            f"曜衡-{APP_VERSION}-Windows-x64-安装版.exe",
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
        self.assertIn('$StalePublishablePaths = @($LegacyZipPath)', build_script)
        self.assertIn("$ManagedReleaseAssetNamePattern", build_script)
        self.assertIn("Portable\\.zip", build_script)
        self.assertIn("Setup\\.exe", build_script)
        self.assertIn(
            "OutputBaseFilename=Yaoheng-{#AppVersion}-Windows-x64-Setup",
            installer_script,
        )
        self.assertIn(f'$AppVersion = "{APP_VERSION}"', build_script)
        self.assertIn(f'#define AppVersion "{APP_VERSION}"', installer_script)
        # PowerShell's comma operator binds before + here; the parentheses are
        # required or all generated item names collapse into one string and an
        # old _internal directory survives the copy.
        self.assertIn('($AppName + ".exe"), "_internal"', build_script)
        self.assertNotIn('$AppName + ".exe", "_internal"', build_script)
        for source in (
            "app_version.py",
            "conversion_core.py",
            "exchange_page.py",
            "command_service.py",
            "local_api.py",
            "secret_store.py",
            "update_service.py",
            "c2c",
        ):
            with self.subTest(source=source):
                self.assertIn(f'"{source}"', build_script)
        self.assertIn("DelTree(ExpandConstant('{app}\\private')", installer_script)
        self.assertIn("AppId={{49A035BF-7BEC-4FE1-84C4-EEBFD503A917}", installer_script)
        self.assertIn("#ifdef UpgradeSmokeTest", installer_script)
        self.assertIn("AppId={{20F8D67B-55F8-48D7-91E1-04986C8CF8A3}", installer_script)
        self.assertIn("UsePreviousAppDir=yes", installer_script)
        self.assertIn("UsePreviousGroup=yes", installer_script)
        self.assertIn("UsePreviousTasks=yes", installer_script)
        self.assertIn("CloseApplications=yes", installer_script)
        self.assertIn("RestartApplications=yes", installer_script)
        install_delete = installer_script.split("[InstallDelete]", 1)[1].split("[Icons]", 1)[0]
        self.assertIn('Name: "{app}\\_internal"', install_delete)
        self.assertIn('Name: "{app}\\{#AppExeName}"', install_delete)
        self.assertNotIn('Name: "{app}\\private"', install_delete)
        self.assertNotIn('Name: "{app}\\data"', install_delete)
        self.assertNotIn('Name: "{app}\\app_settings', install_delete)

    def test_release_runtime_parses_openssl_patch_field_correctly(self):
        validate_release_runtime(
            (3, 13, 15),
            (3, 0, 0, 21, 0),
            "OpenSSL 3.0.21",
        )
        validate_release_runtime(
            (3, 14, 0),
            (3, 6, 0, 3, 0),
            "OpenSSL 3.6.3",
        )

        with self.assertRaisesRegex(ReleaseCheckError, "Python 3.13.15"):
            validate_release_runtime((3, 13, 14), (3, 0, 0, 21, 0), "OpenSSL 3.0.21")
        with self.assertRaisesRegex(ReleaseCheckError, "not approved"):
            validate_release_runtime((3, 13, 15), (3, 0, 0, 20, 0), "OpenSSL 3.0.20")
        with self.assertRaisesRegex(ReleaseCheckError, "unrecognized"):
            validate_release_runtime((3, 13, 15), (3, 0, 21), "OpenSSL malformed")

        project_root = Path(__file__).resolve().parents[1]
        build_script = (project_root / "build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('$ReleaseChecksScript, "validate-runtime"', build_script)


if __name__ == "__main__":
    unittest.main()
