from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import base64
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openthesis.financial_compatibility import (
    CompatibilityPackError,
    CompatibilityPackRegistry,
    _canonical_payload,
    _canonical_signature_message,
)


_TEST_PRIVATE_KEY = Ed25519PrivateKey.generate()
_TEST_PUBLIC_KEY = _TEST_PRIVATE_KEY.public_key().public_bytes_raw()
_TRUSTED_KEYS = {"test-key": _TEST_PUBLIC_KEY}


def make_pack(*, version: str = "1.0.0", pack_id: str = "cn-reports", **overrides):
    payload = {
        "pack_id": pack_id,
        "version": version,
        "schema_version": 1,
        "app_min": "2.0.0",
        "app_max": "2.9.9",
        "markets": ["SSE", "SZSE"],
        "report_types": ["annual", "quarterly"],
        "rules": {
            "title_aliases": {"consolidated_income": ["合并利润表", "利润表"]},
            "unit_aliases": {"CNY": ["人民币", "元"]},
        },
        "payload_sha256": "",
        "key_id": "test-key",
        "signature_algorithm": "Ed25519",
        "signature": "",
    }
    payload.update(overrides)
    payload["payload_sha256"] = hashlib.sha256(_canonical_payload(payload)).hexdigest()
    payload["signature"] = base64.b64encode(
        _TEST_PRIVATE_KEY.sign(_canonical_signature_message(payload))
    ).decode("ascii")
    return payload


class CompatibilityPackRegistryTests(unittest.TestCase):
    def write(self, root: Path, payload: dict, name: str) -> None:
        (root / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_valid_pack_loads_and_reports_safe_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "financial-compatibility-packs"
            root.mkdir()
            self.write(root, make_pack(), "cn-reports-1.0.0.json")
            pack = CompatibilityPackRegistry(Path(directory), "2.4.0", trusted_public_keys=_TRUSTED_KEYS).active_for("SSE", "annual")
            self.assertIsNotNone(pack)
            assert pack is not None
            self.assertEqual(pack.pack_id, "cn-reports")
            self.assertEqual(pack.trust_status, "local_trusted/ed25519+hash_verified")
            self.assertEqual(pack.summary()["payload_sha256"], pack.payload_sha256)

    def test_unknown_fields_dangerous_keys_and_hash_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "financial-compatibility-packs"
            root.mkdir()
            unknown = make_pack(extra="no")
            dangerous = make_pack()
            dangerous["rules"] = {"title_aliases": {"__import__": ["x"]}}
            bad_hash = make_pack()
            bad_hash["payload_sha256"] = "0" * 64
            for payload, name in ((unknown, "unknown.json"), (dangerous, "danger.json"), (bad_hash, "hash.json")):
                self.write(root, payload, name)
            registry = CompatibilityPackRegistry(Path(directory), "2.4.0", trusted_public_keys=_TRUSTED_KEYS)
            self.assertIsNone(registry.active_for("SSE", "annual"))
            self.assertEqual(len(registry.load_errors), 3)

    def test_app_range_and_highest_version_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "financial-compatibility-packs"
            root.mkdir()
            self.write(root, make_pack(version="1.1.0"), "one.json")
            self.write(root, make_pack(version="1.3.0"), "three.json")
            self.write(root, make_pack(version="2.0.0", app_min="3.0.0"), "incompatible.json")
            pack = CompatibilityPackRegistry(Path(directory), "2.4.0", trusted_public_keys=_TRUSTED_KEYS).active_for("SSE", "annual")
            self.assertIsNotNone(pack)
            self.assertEqual(pack.version, "1.3.0")

    def test_activation_and_rollback_are_atomic_and_keep_previous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "financial-compatibility-packs"
            root.mkdir()
            self.write(root, make_pack(version="1.0.0"), "one.json")
            self.write(root, make_pack(version="1.1.0"), "two.json")
            registry = CompatibilityPackRegistry(Path(directory), "2.4.0", trusted_public_keys=_TRUSTED_KEYS)
            registry.activate("cn-reports", "1.0.0")
            registry.activate("cn-reports", "1.1.0")
            self.assertEqual(registry.active_for("SSE", "annual").version, "1.1.0")
            self.assertEqual(registry.rollback("cn-reports").version, "1.0.0")
            self.assertEqual(registry.active_for("SSE", "annual").version, "1.0.0")
            state = json.loads((root / "active.json").read_text(encoding="utf-8"))
            self.assertEqual(state["packs"]["cn-reports"]["current"]["version"], "1.0.0")

    def test_corrupt_active_pointer_recovers_to_highest_valid_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "financial-compatibility-packs"
            root.mkdir()
            self.write(root, make_pack(version="1.0.0"), "one.json")
            self.write(root, make_pack(version="1.2.0"), "two.json")
            (root / "active.json").write_text("not json", encoding="utf-8")
            registry = CompatibilityPackRegistry(Path(directory), "2.4.0", trusted_public_keys=_TRUSTED_KEYS)
            self.assertEqual(registry.active_for("SSE", "annual").version, "1.2.0")

    def test_only_direct_json_files_in_trusted_directory_are_read(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "financial-compatibility-packs"
            root.mkdir()
            outside = base / "outside.json"
            self.write(outside.parent, make_pack(version="9.9.9"), outside.name)
            self.write(root, make_pack(version="1.0.0"), "one.json")
            nested = root / "nested"
            nested.mkdir()
            self.write(nested, make_pack(version="9.9.9"), "nested.json")
            registry = CompatibilityPackRegistry(base, "2.4.0", trusted_public_keys=_TRUSTED_KEYS)
            pack = registry.active_for("SSE", "annual")
            self.assertEqual(pack.version, "1.0.0")

    def test_signature_tamper_unknown_key_missing_signature_and_wrong_algorithm_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "financial-compatibility-packs"
            root.mkdir()
            valid = make_pack()
            tampered = make_pack(pack_id="tampered")
            tampered["rules"]["unit_aliases"]["CNY"] = ["改写"]
            unknown_key = make_pack(pack_id="unknown", key_id="other-key")
            missing = make_pack(pack_id="missing")
            missing["signature"] = ""
            wrong_algorithm = make_pack(pack_id="algorithm", signature_algorithm="RSA")
            for payload, name in ((valid, "valid.json"), (tampered, "tampered.json"),
                                  (unknown_key, "unknown-key.json"), (missing, "missing.json"),
                                  (wrong_algorithm, "wrong-algorithm.json")):
                self.write(root, payload, name)
            registry = CompatibilityPackRegistry(Path(directory), "2.4.0", trusted_public_keys=_TRUSTED_KEYS)
            self.assertIsNotNone(registry.active_for("SSE", "annual"))
            self.assertEqual(len(registry.load_errors), 4)

    def test_trust_anchor_is_explicit_and_rules_snapshot_is_pickle_safe(self):
        import pickle
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "financial-compatibility-packs"
            root.mkdir()
            self.write(root, make_pack(), "one.json")
            self.assertIsNone(CompatibilityPackRegistry(Path(directory), "2.4.0").active_for("SSE", "annual"))
            registry = CompatibilityPackRegistry(Path(directory), "2.4.0", trusted_public_keys=_TRUSTED_KEYS)
            snapshot = registry.rules_snapshot("SSE", "annual")
            restored = pickle.loads(pickle.dumps(snapshot))
            self.assertEqual(restored.title_aliases, snapshot.title_aliases)
            self.assertEqual(restored.semantic_fingerprint(), snapshot.semantic_fingerprint())


if __name__ == "__main__":
    unittest.main()
