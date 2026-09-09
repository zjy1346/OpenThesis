"""Local, declarative compatibility packs for financial document ingestion.

Compatibility packs are intentionally boring data.  They can provide aliases
for a parser, but cannot contain code, expressions, paths, or network
locations.  The registry only reads JSON files directly below the configured
``financial-compatibility-packs`` directory and verifies each file's canonical
payload before it becomes eligible for use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import base64
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


_TOP_LEVEL_KEYS = frozenset(
    {
        "pack_id",
        "version",
        "schema_version",
        "app_min",
        "app_max",
        "markets",
        "report_types",
        "rules",
        "payload_sha256",
        "key_id",
        "signature_algorithm",
        "signature",
    }
)
_RULE_KEYS = frozenset(
    {"title_aliases", "scope_aliases", "unit_aliases", "taxonomy_aliases"}
)
_DANGEROUS_KEYS = frozenset(
    {
        "__class__",
        "__dict__",
        "__import__",
        "__proto__",
        "class",
        "command",
        "code",
        "eval",
        "exec",
        "expression",
        "import",
        "path",
        "script",
        "url",
    }
)
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_MAX_PACK_BYTES = 1_048_576


class CompatibilityPackError(ValueError):
    """Raised when a local pack is malformed or fails integrity checks."""


@dataclass(frozen=True, slots=True)
class FinancialRulesSnapshot:
    """Pickle-safe, immutable parser rules captured from one verified pack.

    The parser receives this value at the process boundary instead of a live
    registry or a mapping proxy.  Empty snapshots intentionally carry no
    aliases and therefore preserve the pre-pack parser behaviour exactly.
    """

    pack_id: str = ""
    version: str = ""
    payload_sha256: str = ""
    trust_status: str = "none"
    title_aliases: tuple[tuple[str, tuple[str, ...]], ...] = ()
    scope_aliases: tuple[tuple[str, tuple[str, ...]], ...] = ()
    unit_aliases: tuple[tuple[str, tuple[str, ...]], ...] = ()
    taxonomy_aliases: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @classmethod
    def empty(cls) -> "FinancialRulesSnapshot":
        return cls()

    @classmethod
    def from_pack(cls, pack: "CompatibilityPack") -> "FinancialRulesSnapshot":
        def freeze(rule_name: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
            values = pack.rules.get(rule_name, {})
            return tuple(
                (str(key), (str(value),) if isinstance(value, str) else tuple(str(item) for item in value))
                for key, value in sorted(values.items(), key=lambda item: str(item[0]))
            )

        return cls(
            pack.pack_id,
            pack.version,
            pack.payload_sha256,
            pack.trust_status,
            freeze("title_aliases"),
            freeze("scope_aliases"),
            freeze("unit_aliases"),
            freeze("taxonomy_aliases"),
        )

    def aliases(self, rule_name: str, canonical: str) -> tuple[str, ...]:
        values = getattr(self, rule_name, ())
        for key, aliases in values:
            if key.casefold() == str(canonical).casefold():
                return aliases
        return ()

    @property
    def active(self) -> bool:
        return bool(self.payload_sha256)

    def semantic_fingerprint(self) -> str:
        """Stable cache input; includes rules even when pack metadata is same."""
        payload = {
            "pack_id": self.pack_id,
            "version": self.version,
            "payload_sha256": self.payload_sha256,
            "title_aliases": self.title_aliases,
            "scope_aliases": self.scope_aliases,
            "unit_aliases": self.unit_aliases,
            "taxonomy_aliases": self.taxonomy_aliases,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        def expand(values: tuple[tuple[str, tuple[str, ...]], ...]) -> dict[str, list[str]]:
            return {key: list(aliases) for key, aliases in values}

        return {
            "pack_id": self.pack_id,
            "version": self.version,
            "payload_sha256": self.payload_sha256,
            "trust_status": self.trust_status,
            "title_aliases": expand(self.title_aliases),
            "scope_aliases": expand(self.scope_aliases),
            "unit_aliases": expand(self.unit_aliases),
            "taxonomy_aliases": expand(self.taxonomy_aliases),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FinancialRulesSnapshot":
        def freeze(value: Any) -> tuple[tuple[str, tuple[str, ...]], ...]:
            if not isinstance(value, Mapping):
                return ()
            result: list[tuple[str, tuple[str, ...]]] = []
            for key, aliases in sorted(value.items(), key=lambda item: str(item[0])):
                if isinstance(aliases, str):
                    aliases = (aliases,)
                if not isinstance(aliases, (list, tuple)):
                    continue
                result.append((str(key), tuple(str(item) for item in aliases)))
            return tuple(result)

        return cls(
            str(raw.get("pack_id", "")), str(raw.get("version", "")),
            str(raw.get("payload_sha256", "")), str(raw.get("trust_status", "none")),
            freeze(raw.get("title_aliases")), freeze(raw.get("scope_aliases")),
            freeze(raw.get("unit_aliases")), freeze(raw.get("taxonomy_aliases")),
        )


def _semver(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if not match:
        raise CompatibilityPackError("version must be semantic x.y.z")
    return tuple(int(item) for item in match.groups())  # type: ignore[return-value]


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompatibilityPackError(f"{field} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CompatibilityPackError(f"{field} must be a non-empty string array")
    result = tuple(_string(item, f"{field}[{index}]") for index, item in enumerate(value))
    if len(set(result)) != len(result):
        raise CompatibilityPackError(f"{field} contains duplicate values")
    return result


def _scan_dangerous(value: Any, location: str = "payload") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CompatibilityPackError(f"{location} contains a non-string key")
            normalized = key.strip().lower()
            if normalized in _DANGEROUS_KEYS or normalized.startswith("__"):
                raise CompatibilityPackError(f"{location}.{key} is not allowed")
            _scan_dangerous(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_dangerous(child, f"{location}[{index}]")
    elif not isinstance(value, (str, int, float, bool)) and value is not None:
        raise CompatibilityPackError(f"{location} contains an unsupported value")


def _canonical_payload(payload: Mapping[str, Any]) -> bytes:
    # The payload digest is over the unsigned content.  Excluding the
    # signature avoids a circular hash/signature dependency while key_id and
    # signature_algorithm remain bound by the signed message below.
    unsigned = {
        key: value for key, value in payload.items()
        if key not in {"payload_sha256", "signature"}
    }
    return json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonical_signature_message(payload: Mapping[str, Any]) -> bytes:
    """Canonical Ed25519 message: all pack content plus payload hash."""
    signed = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(
        signed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _freeze_rules(rules: Mapping[str, Any]) -> Mapping[str, Mapping[str, tuple[str, ...] | str]]:
    frozen: dict[str, Mapping[str, tuple[str, ...] | str]] = {}
    for rule_name, rule_value in rules.items():
        if rule_name not in _RULE_KEYS:
            raise CompatibilityPackError(f"rules.{rule_name} is not allowed")
        if not isinstance(rule_value, dict) or not rule_value:
            raise CompatibilityPackError(f"rules.{rule_name} must be a non-empty mapping")
        mapped: dict[str, tuple[str, ...] | str] = {}
        for canonical, aliases in rule_value.items():
            key = _string(canonical, f"rules.{rule_name} key")
            if isinstance(aliases, str):
                mapped[key] = _string(aliases, f"rules.{rule_name}.{key}")
            elif isinstance(aliases, list):
                mapped[key] = _string_list(aliases, f"rules.{rule_name}.{key}")
            else:
                raise CompatibilityPackError(
                    f"rules.{rule_name}.{key} must be a string or string array"
                )
        frozen[rule_name] = MappingProxyType(mapped)
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class CompatibilityPack:
    pack_id: str
    version: str
    schema_version: int
    app_min: str
    app_max: str
    markets: tuple[str, ...]
    report_types: tuple[str, ...]
    rules: Mapping[str, Mapping[str, tuple[str, ...] | str]]
    payload_sha256: str
    key_id: str
    signature_algorithm: str
    signature: str
    trust_status: str
    source_path: str

    @property
    def version_key(self) -> tuple[int, int, int]:
        return _semver(self.version)

    def supports_app(self, app_version: str) -> bool:
        try:
            current = _semver(_string(app_version, "app_version"))
            return _semver(self.app_min) <= current <= _semver(self.app_max)
        except CompatibilityPackError:
            return False

    def supports(self, market: str, report_type: str, app_version: str) -> bool:
        return (
            self.supports_app(app_version)
            and market.upper() in {item.upper() for item in self.markets}
            and report_type.upper() in {item.upper() for item in self.report_types}
        )

    def summary(self) -> dict[str, str]:
        return {
            "pack_id": self.pack_id,
            "version": self.version,
            "payload_sha256": self.payload_sha256,
            "key_id": self.key_id,
            "signature_algorithm": self.signature_algorithm,
            "signature": self.signature,
            "trust_status": self.trust_status,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "version": self.version,
            "schema_version": self.schema_version,
            "app_min": self.app_min,
            "app_max": self.app_max,
            "markets": list(self.markets),
            "report_types": list(self.report_types),
            "rules": {
                rule: dict(values) for rule, values in self.rules.items()
            },
            "payload_sha256": self.payload_sha256,
            "trust_status": self.trust_status,
            "source_path": self.source_path,
        }


def _parse_pack(
    raw: Any,
    source_path: Path,
    trusted_public_keys: Mapping[str, bytes | Ed25519PublicKey] | None = None,
) -> CompatibilityPack:
    if not isinstance(raw, dict):
        raise CompatibilityPackError("pack must be a JSON object")
    _scan_dangerous(raw)
    unknown = set(raw) - _TOP_LEVEL_KEYS
    missing = _TOP_LEVEL_KEYS - set(raw)
    if unknown:
        raise CompatibilityPackError(f"unknown top-level fields: {sorted(unknown)}")
    if missing:
        raise CompatibilityPackError(f"missing top-level fields: {sorted(missing)}")
    pack_id = _string(raw["pack_id"], "pack_id")
    version = _string(raw["version"], "version")
    _semver(version)
    schema_version = raw["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        raise CompatibilityPackError("schema_version must be 1")
    app_min = _string(raw["app_min"], "app_min")
    app_max = _string(raw["app_max"], "app_max")
    if _semver(app_min) > _semver(app_max):
        raise CompatibilityPackError("app_min must not exceed app_max")
    markets = _string_list(raw["markets"], "markets")
    report_types = _string_list(raw["report_types"], "report_types")
    rules = raw["rules"]
    if not isinstance(rules, dict):
        raise CompatibilityPackError("rules must be an object")
    frozen_rules = _freeze_rules(rules)
    supplied_hash = _string(raw["payload_sha256"], "payload_sha256").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", supplied_hash):
        raise CompatibilityPackError("payload_sha256 must be a SHA-256 hex digest")
    actual_hash = hashlib.sha256(_canonical_payload(raw)).hexdigest()
    if supplied_hash != actual_hash:
        raise CompatibilityPackError("payload_sha256 does not match canonical payload")
    key_id = _string(raw["key_id"], "key_id")
    signature_algorithm = _string(raw["signature_algorithm"], "signature_algorithm")
    if signature_algorithm.casefold() != "ed25519":
        raise CompatibilityPackError("signature_algorithm must be Ed25519")
    signature_text = _string(raw["signature"], "signature")
    if trusted_public_keys is None or key_id not in trusted_public_keys:
        raise CompatibilityPackError("signature key_id is not trusted")
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except (ValueError, TypeError):
        raise CompatibilityPackError("signature must be base64") from None
    if len(signature) != 64:
        raise CompatibilityPackError("signature must be a 64-byte Ed25519 signature")
    try:
        public_key = trusted_public_keys[key_id]
        if isinstance(public_key, bytes):
            public_key = Ed25519PublicKey.from_public_bytes(public_key)
        if not isinstance(public_key, Ed25519PublicKey):
            raise CompatibilityPackError("trusted key must be an Ed25519 public key")
        public_key.verify(signature, _canonical_signature_message(raw))
    except InvalidSignature:
        raise CompatibilityPackError("signature verification failed") from None
    except (TypeError, ValueError):
        raise CompatibilityPackError("trusted key is not a valid Ed25519 public key") from None
    return CompatibilityPack(
        pack_id,
        version,
        schema_version,
        app_min,
        app_max,
        markets,
        report_types,
        frozen_rules,
        supplied_hash,
        key_id,
        signature_algorithm,
        signature_text,
        "local_trusted/ed25519+hash_verified",
        str(source_path),
    )


class CompatibilityPackRegistry:
    """Discover and atomically activate verified local compatibility packs."""

    def __init__(
        self,
        data_dir: Path,
        app_version: str,
        *,
        trusted_public_keys: Mapping[str, bytes | Ed25519PublicKey] | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.app_version = _string(app_version, "app_version")
        # Copy the caller's mapping once; packs cannot mutate the trust
        # anchor and keys are never discovered from the pack directory.
        self._trusted_public_keys = dict(trusted_public_keys or {})
        self.root = self.data_dir / "financial-compatibility-packs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.active_path = self.root / "active.json"
        self.load_errors: tuple[str, ...] = ()

    def _load_packs(self) -> list[CompatibilityPack]:
        packs: list[CompatibilityPack] = []
        errors: list[str] = []
        root = self.root.resolve()
        try:
            entries = sorted(self.root.iterdir(), key=lambda item: item.name)
        except OSError:
            self.load_errors = ("compatibility_pack_directory_unreadable",)
            return []
        for path in entries:
            if path.name == self.active_path.name or path.suffix.lower() != ".json":
                continue
            try:
                if path.is_symlink() or path.resolve().parent != root or not path.is_file():
                    raise CompatibilityPackError("pack path is outside the trusted directory")
                if path.stat().st_size > _MAX_PACK_BYTES:
                    raise CompatibilityPackError("pack exceeds size limit")
                with path.open("r", encoding="utf-8") as stream:
                    raw = json.load(stream)
                packs.append(_parse_pack(raw, path, self._trusted_public_keys))
            except (OSError, UnicodeError, json.JSONDecodeError, CompatibilityPackError) as exc:
                errors.append(f"{path.name}:{type(exc).__name__}")
        self.load_errors = tuple(errors)
        return packs

    def _read_active(self) -> dict[str, Any]:
        try:
            if not self.active_path.is_file() or self.active_path.is_symlink():
                return {}
            with self.active_path.open("r", encoding="utf-8") as stream:
                raw = json.load(stream)
            if not isinstance(raw, dict) or not isinstance(raw.get("packs"), dict):
                return {}
            return raw
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def _write_active(self, state: Mapping[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix="active-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.active_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _record(pack: CompatibilityPack) -> dict[str, str]:
        return {"version": pack.version, "payload_sha256": pack.payload_sha256}

    def _reconcile(self, packs: list[CompatibilityPack]) -> tuple[list[CompatibilityPack], dict[str, Any]]:
        state = self._read_active()
        by_key = {(pack.pack_id, pack.version): pack for pack in packs}
        selected: list[CompatibilityPack] = []
        changed = not state
        state_packs = state.get("packs", {}) if isinstance(state.get("packs"), dict) else {}
        new_state: dict[str, Any] = {"schema_version": 1, "packs": {}}
        for pack_id in sorted({pack.pack_id for pack in packs}):
            candidates = [pack for pack in packs if pack.pack_id == pack_id and pack.supports_app(self.app_version)]
            candidates.sort(key=lambda pack: pack.version_key, reverse=True)
            if not candidates:
                continue
            prior = state_packs.get(pack_id, {}) if isinstance(state_packs.get(pack_id), dict) else {}
            # The pointer stores the active record under ``current``.  Keep
            # the nested shape explicit so a valid rollback is not silently
            # replaced by the newest pack on every registry read.
            current_record = prior.get("current") if isinstance(prior.get("current"), dict) else prior
            current = by_key.get((pack_id, str(current_record.get("version", ""))))
            if current is None or current.payload_sha256 != str(current_record.get("payload_sha256", "")) or not current.supports_app(self.app_version):
                current = candidates[0]
                changed = True
            previous = prior.get("previous", []) if isinstance(prior.get("previous"), list) else []
            safe_previous = [item for item in previous if isinstance(item, dict)]
            new_state["packs"][pack_id] = {"current": self._record(current), "previous": safe_previous}
            selected.append(current)
        if changed:
            try:
                self._write_active(new_state)
            except OSError:
                pass
        return selected, new_state

    def active_for(self, market: str, report_type: str) -> CompatibilityPack | None:
        packs, _ = self._reconcile(self._load_packs())
        matches = [pack for pack in packs if pack.supports(market, report_type, self.app_version)]
        return max(matches, key=lambda pack: pack.version_key, default=None)

    def rules_snapshot(self, market: str, report_type: str) -> FinancialRulesSnapshot:
        """Return the active pack's immutable parser rules, or an empty snapshot."""
        pack = self.active_for(market, report_type)
        return FinancialRulesSnapshot.from_pack(pack) if pack is not None else FinancialRulesSnapshot.empty()

    def activate(self, pack_id: str, version: str) -> CompatibilityPack:
        pack_id = _string(pack_id, "pack_id")
        version = _string(version, "version")
        packs = self._load_packs()
        chosen = next((item for item in packs if item.pack_id == pack_id and item.version == version), None)
        if chosen is None:
            raise CompatibilityPackError("requested pack is not a valid local pack")
        if not chosen.supports_app(self.app_version):
            raise CompatibilityPackError("requested pack is incompatible with this app")
        _, state = self._reconcile(packs)
        entry = state.setdefault("packs", {}).setdefault(pack_id, {})
        current = entry.get("current") if isinstance(entry, dict) else None
        previous = entry.get("previous", []) if isinstance(entry, dict) else []
        previous = [item for item in previous if isinstance(item, dict)]
        if isinstance(current, dict) and current.get("version") != chosen.version:
            previous.insert(0, current)
        dedup: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in previous:
            key = (str(item.get("version", "")), str(item.get("payload_sha256", "")))
            if key not in seen and key[0] and key[1] != chosen.payload_sha256:
                seen.add(key)
                dedup.append({"version": key[0], "payload_sha256": key[1]})
        state["packs"][pack_id] = {"current": self._record(chosen), "previous": dedup}
        self._write_active(state)
        return chosen

    def rollback(self, pack_id: str) -> CompatibilityPack:
        pack_id = _string(pack_id, "pack_id")
        packs = self._load_packs()
        _, state = self._reconcile(packs)
        entry = state.get("packs", {}).get(pack_id)
        if not isinstance(entry, dict):
            raise CompatibilityPackError("pack has no active version")
        previous = entry.get("previous", [])
        if not isinstance(previous, list):
            previous = []
        by_key = {(item.pack_id, item.version, item.payload_sha256): item for item in packs}
        chosen: CompatibilityPack | None = None
        chosen_index = -1
        for index, item in enumerate(previous):
            if not isinstance(item, dict):
                continue
            key = (pack_id, str(item.get("version", "")), str(item.get("payload_sha256", "")))
            candidate = by_key.get(key)
            if candidate and candidate.supports_app(self.app_version):
                chosen, chosen_index = candidate, index
                break
        if chosen is None:
            raise CompatibilityPackError("no valid previous pack is available")
        current = entry.get("current")
        remaining = [item for index, item in enumerate(previous) if index != chosen_index]
        if isinstance(current, dict):
            remaining.insert(0, current)
        state["packs"][pack_id] = {"current": self._record(chosen), "previous": remaining}
        self._write_active(state)
        return chosen

    def active_summaries(self) -> tuple[dict[str, str], ...]:
        packs, _ = self._reconcile(self._load_packs())
        return tuple(pack.summary() for pack in packs)
