"""Independent original-source checks for explicitly approved gold errata.

This benchmark-only module never imports a production parser, changes gold, or
feeds answers into extraction. It supports exactly the two reviewed FASB
concept corrections, not value-based aliases or source-absence adjudication.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

IX = "{http://www.xbrl.org/2013/inlineXBRL}"
XBRLI = "{http://www.xbrl.org/2003/instance}"
PERIOD_FIELDS = ("period_start", "period_end", "fiscal_period", "fiscal_year")
CORRECTIONS = {
    ("net_income", "NetIncomeLoss"): "parent_net_income",
    ("total_equity", "StockholdersEquity"): "equity",
}
# Independently captured FASB primary document/definition hashes, 2026-10-06.
# These pin the reviewed editions, not live extraction or arbitrary JSON claims.
APPROVED_TAXONOMY_DEFINITIONS = {
    2021: {
        "file_sha256": "b6c606d758b73a7f4b4588879abc47c8f06e44af06c85289017e16ae48823f72",
        "definitions": {
            "NetIncomeLoss": "b074bba89e771c330bbbf0703837bd97f7b6886d6a35ec8b35829dce14ca0c57",
            "StockholdersEquity": "2542fb2f85a44c0ef5470a748bec98615be097dcc402589ad442858c835814b6",
        },
    },
    2022: {
        "file_sha256": "a9d515b582ddab7961cfc5283c4d5799db03b6c4fbc6875bdb0fd31de482f0d2",
        "definitions": {
            "NetIncomeLoss": "b074bba89e771c330bbbf0703837bd97f7b6886d6a35ec8b35829dce14ca0c57",
            "StockholdersEquity": "2542fb2f85a44c0ef5470a748bec98615be097dcc402589ad442858c835814b6",
        },
    },
    2023: {
        "file_sha256": "288ed00809b3f84f62ee82c93db6f063d4db09bdc4ee6d1654207a1865ded5e7",
        "definitions": {
            "NetIncomeLoss": "b074bba89e771c330bbbf0703837bd97f7b6886d6a35ec8b35829dce14ca0c57",
            "StockholdersEquity": "7ad9d5dcb79a149c02f42ab29f245e9cef833ab112016b7800af3290b75a6962",
        },
    },
    2024: {
        "file_sha256": "96b321b294493af54253a45e3694e85d501857cb4029ba2529089ad9d7880872",
        "definitions": {
            "NetIncomeLoss": "b074bba89e771c330bbbf0703837bd97f7b6886d6a35ec8b35829dce14ca0c57",
            "StockholdersEquity": "7ad9d5dcb79a149c02f42ab29f245e9cef833ab112016b7800af3290b75a6962",
        },
    },
    2025: {
        "file_sha256": "27f1d4ba06b0dcb0c91225db2ac746d5adefc167518fa3948abbdfb944dab019",
        "definitions": {
            "NetIncomeLoss": "b074bba89e771c330bbbf0703837bd97f7b6886d6a35ec8b35829dce14ca0c57",
            "StockholdersEquity": "7ad9d5dcb79a149c02f42ab29f245e9cef833ab112016b7800af3290b75a6962",
        },
    },
}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def row_key(ticker: str, period: dict, expected: dict) -> tuple:
    return ticker, expected["source_id"], period["period_end"], expected["concept"]


def row_digest(period: dict, expected: dict) -> str:
    return digest({"period": {key: period.get(key) for key in PERIOD_FIELDS}, "row": expected})


def _qname(value: str, namespaces: dict) -> str:
    prefix, separator, local = value.partition(":")
    if not separator or prefix not in namespaces or not local:
        raise ValueError("ERRATUM_QNAME_UNRESOLVED")
    return "{" + namespaces[prefix] + "}" + local


def original_observations(raw: bytes, expected: dict) -> list[dict]:
    """Re-read precisely the gold tag/context; never search by amount."""
    if (len(raw) > 32 * 1024 * 1024 or b"\0" in raw
            or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.I)):
        raise ValueError("ERRATUM_ORIGINAL_XML_UNSUPPORTED")
    namespace_maps, parents, stack, pending = {}, {}, [], {}
    parser = ET.iterparse(io.BytesIO(raw), events=("start", "end", "start-ns"))
    count = 0
    for event, node in parser:
        if event == "start-ns":
            pending[node[0] or ""] = node[1]
        elif event == "start":
            namespaces = dict(namespace_maps[id(stack[-1])]) if stack else {}
            namespaces.update(pending)
            pending.clear()
            namespace_maps[id(node)] = namespaces
            if stack:
                parents[id(node)] = stack[-1]
            stack.append(node)
            count += 1
            if count > 250_000 or len(stack) > 256:
                raise ValueError("ERRATUM_ORIGINAL_XML_UNSUPPORTED")
        else:
            stack.pop()
    root = parser.root
    ids = {}
    for node in root.iter():
        if node.get("id"):
            ids.setdefault(node.get("id"), []).append(node)
    locator = expected["locator"]
    context_id = locator["xbrl_context"]
    contexts = [node for node in ids.get(context_id, ()) if node.tag == XBRLI + "context"]
    if len(contexts) != 1:
        raise ValueError("ERRATUM_CONTEXT_AMBIGUOUS")
    context = contexts[0]
    if any(node.tag in {XBRLI + "segment", XBRLI + "scenario"} for node in context.iter()):
        raise ValueError("ERRATUM_DIMENSIONAL_CONTEXT_UNSUPPORTED")
    identifiers = context.findall(".//" + XBRLI + "identifier")
    if len(identifiers) != 1:
        raise ValueError("ERRATUM_ENTITY_AMBIGUOUS")
    period = context.find(XBRLI + "period")
    if period is None:
        raise ValueError("ERRATUM_PERIOD_MISSING")
    instant, start, end = (period.find(XBRLI + name) for name in ("instant", "startDate", "endDate"))
    period_values = (None, instant.text) if instant is not None else (
        start.text if start is not None else None, end.text if end is not None else None)
    observations = []
    for node in root.iter(IX + "nonFraction"):
        if (node.get("name") != locator["xbrl_concept"]
                or node.get("contextRef") != context_id):
            continue
        ancestor = node
        row = None
        while id(ancestor) in parents:
            ancestor = parents[id(ancestor)]
            if ancestor.tag == IX + "hidden":
                raise ValueError("ERRATUM_HIDDEN_FACT_UNSUPPORTED")
            if ancestor.tag.endswith("}tr") and row is None:
                row = ancestor
        if row is None or node.get("{http://www.w3.org/2001/XMLSchema-instance}nil"):
            raise ValueError("ERRATUM_VISIBLE_ROW_MISSING")
        units = [item for item in ids.get(node.get("unitRef"), ()) if item.tag == XBRLI + "unit"]
        if len(units) != 1 or len(list(units[0])) != 1 or units[0][0].tag != XBRLI + "measure":
            raise ValueError("ERRATUM_UNIT_AMBIGUOUS")
        measure = _qname(units[0][0].text or "", namespace_maps[id(units[0][0])])
        if not measure.startswith("{http://www.xbrl.org/2003/iso4217}"):
            raise ValueError("ERRATUM_NON_MONETARY_UNIT")
        transform = _qname(node.get("format", ""), namespace_maps[id(node)])
        if (not re.fullmatch(r"\{http://www\.xbrl\.org/inlineXBRL/transformation/"
                r"(?:2011-07-31|2015-02-26|2020-02-12)\}num-?dot-?decimal", transform)
                or node.get("sign", "") not in {"", "-"}):
            raise ValueError("ERRATUM_TRANSFORM_UNSUPPORTED")
        text = "".join(node.itertext()).strip()
        token = re.sub(r"[,\s\u00a0]", "", text)
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", token):
            raise ValueError("ERRATUM_NUMERIC_TOKEN_UNSUPPORTED")
        try:
            exponent = int(node.get("scale", "0"))
            if abs(exponent) > 18:
                raise ValueError("ERRATUM_SCALE_UNSUPPORTED")
            scale = Decimal(10) ** exponent
            amount = Decimal(token) * scale * (-1 if node.get("sign") == "-" else 1)
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("ERRATUM_AMOUNT_INVALID") from exc
        observations.append({
            "reported_concept": _qname(node.get("name"), namespace_maps[id(node)]),
            "context_id": context_id, "entity_identifier": identifiers[0].text,
            "entity_scheme": identifiers[0].get("scheme"),
            "period_start": period_values[0], "period_end": period_values[1],
            "currency": measure.split("}", 1)[1], "scale": str(scale),
            "normalized_value": str(amount), "visible_text": text,
            "numeric_attributes": dict(node.attrib),
            "row_text": " ".join("".join(row.itertext()).split()),
        })
    if not observations:
        raise ValueError("ERRATUM_ORIGINAL_LOCATOR_MISSING")
    return observations


def verify_entry(entry: dict, ticker: str, period: dict, expected: dict,
                 source: dict, packet: dict, frozen: dict, raw: bytes) -> None:
    """Concept-only approval cannot change any numeric or source identity."""
    if (entry.get("key") != list(row_key(ticker, period, expected))
            or entry.get("row_sha256") != row_digest(period, expected)
            or entry.get("frozen_artifact_byte_hashes") != {
                name: frozen["artifact_byte_hashes"][name]
                for name in ("financial_facts.json", "source_packet.json")}
            or entry.get("source_sha256") != source["sha256"]
            or hashlib.sha256(raw).hexdigest() != source["sha256"]):
        raise ValueError("ERRATUM_FROZEN_IDENTITY_CHANGED")
    observations = original_observations(raw, expected)
    if observations != entry.get("source_observations"):
        raise ValueError("ERRATUM_SOURCE_PROOF_CHANGED")
    names = {observation["reported_concept"] for observation in observations}
    if len(names) != 1:
        raise ValueError("ERRATUM_CONCEPT_AMBIGUOUS")
    name = next(iter(names))
    match = re.fullmatch(r"\{http://fasb\.org/us-gaap/(2021)-01-31\}(\w+)", name)
    if match is None:
        match = re.fullmatch(r"\{http://fasb\.org/us-gaap/(202[2-5])\}(\w+)", name)
    if (not match or ticker not in {"NVDA", "META", "GOOGL"}
            or packet["issuer"]["market"] != "US" or expected["status"] != "CONFIRMED"
            or "consolidated" not in expected.get("scope", "").casefold()
            or period["fiscal_period"] != "FY"):
        raise ValueError("ERRATUM_OUTSIDE_APPROVED_SCOPE")
    corrected = CORRECTIONS.get((expected["concept"], match[2]))
    if not corrected or entry.get("corrected_concept") != corrected:
        raise ValueError("ERRATUM_CONCEPT_CORRECTION_UNAPPROVED")
    suffix = "2021-01-31" if match[1] == "2021" else match[1]
    reference = entry.get("taxonomy_reference", {})
    approved_definition = APPROVED_TAXONOMY_DEFINITIONS[int(match[1])]
    if (reference.get("year") != int(match[1]) or reference.get("concept") != match[2]
            or reference.get("url") != f"https://xbrl.fasb.org/us-gaap/{match[1]}/elts/us-gaap-doc-{suffix}.xml"
            or any(not re.fullmatch("[a-f0-9]{64}", reference.get(field, ""))
                for field in ("file_sha256", "definition_sha256"))
            or reference.get("file_sha256") != approved_definition["file_sha256"]
            or reference.get("definition_sha256") != approved_definition["definitions"][match[2]]
            or entry.get("review_decision") != "SOURCE_CONFIRMED_CONCEPT_ONLY"):
        raise ValueError("ERRATUM_PRIMARY_DEFINITION_UNVERIFIED")
    for observation in observations:
        if (observation["entity_identifier"].lstrip("0") != packet["issuer"]["cik"].lstrip("0")
                or observation["entity_scheme"] != "http://www.sec.gov/CIK"
                or observation["period_start"] != (None if corrected == "equity" else period["period_start"])
                or observation["period_end"] != period["period_end"]
                or observation["currency"] != expected["normalized_currency"]
                or Decimal(observation["scale"]) != Decimal(str(expected["unit_scale_to_base"]))
                or Decimal(observation["normalized_value"]) != Decimal(expected["normalized_value"])):
            raise ValueError("ERRATUM_AMOUNT_PERIOD_UNIT_OR_ENTITY_MISMATCH")


def load_adjudications(path: Path, baseline_path: Path) -> dict[tuple, dict]:
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("ERRATUM_DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    if (payload.get("schema") != "openthesis.golden-concept-errata.v1"
            or payload.get("baseline_sha256") != hashlib.sha256(baseline_path.read_bytes()).hexdigest()
            or payload.get("approval") != "2026-10-06:independent-errata-originals-and-gold-unchanged"
            or not isinstance(payload.get("entries"), list) or len(payload["entries"]) != 25):
        raise ValueError("ERRATUM_MANIFEST_UNAPPROVED")
    result = {}
    for entry in payload["entries"]:
        key = tuple(entry["key"])
        if key in result:
            raise ValueError("ERRATUM_DUPLICATE_ROW")
        result[key] = entry
    return result
