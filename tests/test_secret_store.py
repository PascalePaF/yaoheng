import base64
import json
import os
import tempfile
import threading
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from secret_store import (
    DEFAULT_SCRYPT_N,
    SecretAlreadyExistsError,
    SecretStore,
    SecretStoreCorruptError,
    SecretStoreError,
    SecretStorePermissionWarning,
    TOKEN_BYTES,
)


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private" / "local-api-auth.json"

    @staticmethod
    def decoded_token_length(token: str) -> int:
        encoded = token.encode("ascii")
        return len(base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)))

    def test_generate_persists_only_hash_material_and_returns_256_bit_token(self):
        store = SecretStore(self.path)
        token = store.generate()

        self.assertEqual(self.decoded_token_length(token), TOKEN_BYTES)
        payload = json.loads(self.path.read_text(encoding="ascii"))
        self.assertEqual(set(payload), {"version", "salt", "kdf", "hash"})
        self.assertEqual(payload["kdf"]["name"], "scrypt")
        self.assertNotIn("token", payload)
        self.assertFalse(token in self.path.read_text(encoding="ascii"))
        self.assertFalse(token in store.backup_path.read_text(encoding="ascii"))
        self.assertTrue(store.verify(token))

    def test_wrong_and_malformed_tokens_fail_without_changing_record(self):
        store = SecretStore(self.path)
        issued = store.generate()
        before = self.path.read_bytes()

        wrong = SecretStore(self.path.parent / "other.json").generate()
        self.assertFalse(store.verify(wrong))
        self.assertFalse(store.verify("not-valid"))
        self.assertFalse(store.verify(None))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(store.verify(issued))

    def test_generate_is_single_use_until_explicit_delete(self):
        store = SecretStore(self.path)
        token = store.generate()

        with self.assertRaises(SecretAlreadyExistsError) as caught:
            store.generate()
        self.assertFalse(token in str(caught.exception))
        self.assertTrue(store.delete())
        self.assertFalse(store.exists())
        self.assertFalse(self.path.exists())
        self.assertFalse(store.backup_path.exists())
        self.assertFalse(store.delete())

    def test_rotate_invalidates_old_token_and_backup_never_revives_it(self):
        store = SecretStore(self.path)
        old_token = store.generate()
        new_token = store.rotate()

        self.assertFalse(store.verify(old_token))
        self.assertTrue(store.verify(new_token))
        self.path.write_bytes(b"damaged")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.assertTrue(store.verify(new_token))
        self.assertFalse(store.verify(old_token))

    def test_corrupt_primary_recovers_from_private_backup(self):
        store = SecretStore(self.path)
        token = store.generate()
        expected_backup = store.backup_path.read_bytes()
        self.path.write_bytes(b"not-json")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertTrue(store.verify(token))
        self.assertEqual(self.path.read_bytes(), expected_backup)
        self.assertTrue(any("恢复" in str(item.message) for item in caught))

    def test_corrupt_primary_and_backup_raise_redacted_error(self):
        store = SecretStore(self.path)
        token = store.generate()
        self.path.write_bytes(b"broken-primary")
        store.backup_path.write_bytes(b"broken-backup")

        with self.assertRaises(SecretStoreCorruptError) as caught:
            store.verify(token)
        self.assertFalse(token in str(caught.exception))
        self.assertFalse(token in repr(caught.exception))

    def test_duplicate_fields_and_excessive_kdf_parameters_are_rejected(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            '{"version":1,"version":1,"salt":"AA","kdf":{},"hash":"AA"}',
            encoding="ascii",
        )
        store = SecretStore(self.path)
        with self.assertRaises(SecretStoreCorruptError):
            store.verify("")

        with self.assertRaises(SecretStoreCorruptError):
            SecretStore(self.path.parent / "bounded.json", scrypt_n=1 << 18)

    def test_successful_verify_migrates_accepted_older_kdf_parameters(self):
        legacy = SecretStore(self.path, scrypt_n=1 << 12)
        token = legacy.generate()
        before = json.loads(self.path.read_text(encoding="ascii"))
        self.assertLess(before["kdf"]["n"], DEFAULT_SCRYPT_N)

        current = SecretStore(self.path)
        self.assertTrue(current.verify(token))
        after = json.loads(self.path.read_text(encoding="ascii"))
        backup = json.loads(current.backup_path.read_text(encoding="ascii"))
        self.assertEqual(after["kdf"]["n"], DEFAULT_SCRYPT_N)
        self.assertEqual(after, backup)
        self.assertFalse(token in self.path.read_text(encoding="ascii"))

    def test_failed_atomic_replace_preserves_existing_verifier(self):
        store = SecretStore(self.path)
        existing = store.generate()
        real_replace = os.replace

        def fail_primary(source, target):
            if Path(target) == self.path:
                raise OSError("simulated replace failure")
            return real_replace(source, target)

        with mock.patch("secret_store.os.replace", side_effect=fail_primary):
            with self.assertRaises(SecretStoreError) as caught:
                store.rotate()
        self.assertFalse(existing in str(caught.exception))
        self.assertTrue(store.verify(existing))

    def test_permission_failure_is_visible_without_exposing_token(self):
        messages: list[str] = []
        store = SecretStore(self.path, warning_callback=messages.append)
        with mock.patch("secret_store.os.chmod", side_effect=OSError("denied")):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                token = store.generate()

        self.assertTrue(messages)
        self.assertTrue(store.security_warnings)
        self.assertTrue(any(item.category is SecretStorePermissionWarning for item in caught))
        combined = " ".join(messages + list(store.security_warnings))
        self.assertFalse(token in combined)

    def test_concurrent_generate_has_one_winner_across_instances(self):
        stores = (SecretStore(self.path), SecretStore(self.path))
        barrier = threading.Barrier(2)

        def generate(store):
            barrier.wait()
            try:
                return store.generate()
            except SecretAlreadyExistsError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(generate, stores))
        issued = [item for item in results if item is not None]
        self.assertEqual(len(issued), 1)
        self.assertTrue(stores[0].verify(issued[0]))

    def test_repr_contains_configuration_only(self):
        store = SecretStore(self.path)
        token = store.generate()
        rendered = repr(store)
        self.assertIn("SecretStore", rendered)
        self.assertFalse(token in rendered)


if __name__ == "__main__":
    unittest.main()
