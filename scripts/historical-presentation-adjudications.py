"""Independent, source-bound adjudication of approved historical display changes.

No production renderer imports this module. Unknown text edits, amount changes
and missing-field-to-zero changes are never accepted as presentation corrections.
"""
from collections import Counter
from decimal import Decimal
import difflib
import hashlib
import json
import re

VERSION = "historical-presentation-errata-v1"
MANIFEST_SHA256 = "08b4119f5c2a2d13a2f930de9a4a78390f7586c55ac9c1e2d19505aeb5d8534e"


def load_manifest(path, baseline):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != MANIFEST_SHA256:
        raise ValueError("PRESENTATION_MANIFEST_NOT_ADJUDICATED")
    value = json.loads(data)
    entries = value.get("entries", [])
    expected_runs = {item["run_id"] for item in baseline["historical_reports"]["runs"]}
    if (value.get("schema_version") != "openthesis.historical-presentation-errata.v1"
            or len(entries) != 11 or len({item["run_id"] for item in entries}) != 11
            or not {item["run_id"] for item in entries}.issubset(expected_runs)
            or value.get("source_database_sha256") != baseline["historical_reports"]["source_sha256"]
            or any(baseline["source_hashes"].get(key) != digest
                   for key, digest in value.get("frozen_renderer_source_hashes", {}).items())):
        raise ValueError("PRESENTATION_MANIFEST_BASELINE_CONFLICT")
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _fields(value, path="content"):
    if isinstance(value, dict):
        for key, nested in value.items():
            child = path + "." + key
            if key in {"currency", "discount_rate", "horizon_years", "equity_market_value"}:
                yield child, key, nested
            elif isinstance(nested, (dict, list)):
                yield from _fields(nested, child)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _fields(nested, f"{path}[{index}]")


def _valuation_sources(artifacts):
    # Use the same persisted lane identity, not any incidental numeric field
    # elsewhere in a dossier. The source bytes are independently hash-bound.
    latest = {}
    for artifact in artifacts:
        if artifact.get("artifact_type") in {"deterministic-valuation", "research-report"}:
            latest[artifact["artifact_type"]] = artifact
    for artifact in latest.values():
        content = artifact.get("content", {})
        if not isinstance(content, dict):
            continue
        if artifact.get("artifact_type") == "deterministic-valuation":
            value = content.get("implied_expectations", content)
            prefix = "content.implied_expectations" if "implied_expectations" in content else "content"
        elif artifact.get("artifact_type") == "research-report":
            report = content.get("report", {})
            if not isinstance(report, dict) or not isinstance(report.get("implied_expectations"), dict):
                continue
            value, prefix = report["implied_expectations"], "content.report.implied_expectations"
        else:
            continue
        for path, key, raw in _fields(value, prefix):
            yield {"artifact_id": artifact["artifact_id"], "path": path, "field": key, "value": raw}


def _comparison_growth_source(artifacts, period):
    for artifact in reversed(artifacts):
        if artifact.get("artifact_type") != "deterministic-financial-summary":
            continue
        content = artifact.get("content", {})
        for index, item in enumerate(content.get("interim_metrics", []) if isinstance(content, dict) else []):
            if (isinstance(item, dict) and f"{item.get('year', '')} {item.get('period', '')}".strip() == period
                    and item.get("revenue_growth") is None and not item.get("revenue_growth_status")):
                return {"artifact_id": artifact["artifact_id"],
                        "path": f"content.interim_metrics[{index}].revenue_growth", "value": None}
        break  # Earlier snapshots cannot prove the active displayed row.
    raise ValueError("PRESENTATION_GROWTH_SOURCE_UNPROVEN")


def _heading_source(artifacts):
    for artifact in reversed(artifacts):
        content = artifact.get("content", {})
        if artifact.get("artifact_type") != "deterministic-financial-summary":
            continue
        if isinstance(content, dict) and content.get("interim_metrics"):
            return {"artifact_id": artifact["artifact_id"], "path": "content.interim_metrics",
                    "rule": "interim-revenue-growth-heading"}
        break
    raise ValueError("PRESENTATION_HEADING_SOURCE_UNPROVEN")


def _percent(raw):
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = Decimal(str(raw)) * 100
    if not value.is_finite():
        return None
    text = format(value, "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text) + "%"


def adjudicate(old_markdown, new_markdown, artifacts):
    if not isinstance(old_markdown, str) or not isinstance(new_markdown, str):
        raise ValueError("PRESENTATION_BODY_INVALID")
    old, new = old_markdown.splitlines(), new_markdown.splitlines()
    sources = tuple(_valuation_sources(artifacts))
    changes = []
    for op, left, right, start, end in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        removed = Counter(line for line in old[left:right] if line.strip())
        added = Counter(line for line in new[start:end] if line.strip())
        for line, count in removed.items():
            proof = None
            replacement = ""
            if line == "| 期间 | 营业收入 | 同期增长 | 净利润 | 经营现金流 |":
                replacement = "| 期间 | 营业收入 | 收入增长 | 净利润 | 经营现金流 |"
                proof = _heading_source(artifacts)
            elif line.startswith("| "):
                cells = line.split("|")
                if len(cells) == 7 and cells[3].strip() == "未提供":
                    proof = _comparison_growth_source(artifacts, cells[1].strip())
                    cells[3] = " — "
                    replacement = "|".join(cells)
            elif line.startswith("- **其他：** "):
                rendered = line[len("- **其他：** "):]
                for source in sources:
                    key, raw = source["field"], source["value"]
                    label = {"currency": "币种", "discount_rate": "折现率", "horizon_years": "预测期限（年）"}.get(key)
                    if label is None or isinstance(raw, bool):
                        continue
                    if key == "currency":
                        expected = raw if isinstance(raw, str) and re.fullmatch(r"[A-Z]{3}", raw) else None
                    elif key == "horizon_years":
                        expected = str(raw) if isinstance(raw, int) and raw > 0 else None
                    else:
                        expected = _percent(raw)
                    candidate = f"- **{label}：** {rendered}"
                    if expected == rendered and added[candidate] >= count:
                        replacement, proof = candidate, source
                        break
            if proof is None or added[replacement] < count:
                raise ValueError("PRESENTATION_UNAPPROVED_REMOVAL_OR_REPLACEMENT")
            added[replacement] -= count
            changes.append({"old_line": line, "new_line": replacement, "count": count, "proof": proof})
        for line, count in added.items():
            if not count:
                continue
            if not line.startswith("- **权益市值：** ") or count != 1:
                raise ValueError("PRESENTATION_UNAPPROVED_ADDITION")
            amount_text = line[len("- **权益市值：** "):]
            if not re.fullmatch(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", amount_text):
                raise ValueError("PRESENTATION_AMOUNT_INVALID")
            amount = Decimal(amount_text.replace(",", ""))
            proof = next((source for source in sources
                if source["field"] == "equity_market_value"
                and not isinstance(source["value"], bool)
                and isinstance(source["value"], (int, float))
                and Decimal(str(source["value"])) == amount), None)
            if proof is None:
                raise ValueError("PRESENTATION_AMOUNT_SOURCE_UNPROVEN")
            changes.append({"old_line": None, "new_line": line, "count": 1, "proof": proof})
    return {"contract": VERSION,
        "old_body_sha256": hashlib.sha256(old_markdown.encode("utf-8")).hexdigest(),
        "new_body_sha256": hashlib.sha256(new_markdown.encode("utf-8")).hexdigest(),
        "changes": changes, "changes_sha256": fingerprint(changes)}


def approved_comparison(strict, expected, entry, *, source_hashes, artifacts):
    """Keep strict failures; accept only the exact separately adjudicated body."""
    if strict.get("state") != "FAIL" or {item.get("code") for item in strict.get("issues", [])} != {"HISTORICAL_BODY_CHANGED"}:
        raise ValueError("PRESENTATION_NOT_BODY_ONLY_FAILURE")
    if (entry.get("contract") != VERSION or entry.get("run_id") != strict.get("run_id")
            or entry.get("old_body_sha256") != expected.get("markdown_sha256")
            or entry.get("new_body_sha256") != strict.get("markdown_sha256")
            or not entry.get("changes")
            or entry.get("changes_sha256") != fingerprint(entry["changes"])
            or entry.get("artifact_input_hashes") != source_hashes):
        raise ValueError("PRESENTATION_ADJUDICATION_IDENTITY_CONFLICT")
    # Re-derive every permitted change from the exact persisted sources. A
    # self-consistent changes digest alone is not source evidence.
    for change in entry["changes"]:
        count = change.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
            raise ValueError("PRESENTATION_CHANGE_COUNT_INVALID")
        old_line, new_line = change.get("old_line"), change.get("new_line")
        if (old_line is not None and not isinstance(old_line, str)) or not isinstance(new_line, str):
            raise ValueError("PRESENTATION_CHANGE_LINE_INVALID")
        regenerated = adjudicate("\n".join([old_line] * count) if old_line is not None else "",
                                  "\n".join([new_line] * count), artifacts)
        if fingerprint(regenerated["changes"]) != fingerprint([change]):
            raise ValueError("PRESENTATION_CHANGE_SOURCE_CONFLICT")
    return {"run_id": strict["run_id"], "state": "PASS_APPROVED_PRESENTATION_ERRATUM",
            "strict_state": strict["state"], "changes_sha256": entry["changes_sha256"]}
