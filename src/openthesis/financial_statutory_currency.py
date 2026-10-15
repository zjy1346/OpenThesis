"""Source-bound CAS presentation basis; never a listing-currency default.

The collector captures an original report identity, domestic legal identity
and its CAS preparation clause. The pure validator replays that evidence and
the pinned law version. This supplies currency only; scale stays in the cell's
literal header. Explicit presentation currencies always take precedence.
"""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import re

from .domain import EvidenceRef
from .financial_unit_declarations import LiteralMoneyUnit, combined_money_unit
from .text_normalization import canonical_search_text


STATUTORY_CURRENCY_VERSION = "cas-statutory-presentation-v2"
_LEGACY_VERSION = "cas-statutory-presentation-v1"
_LAWS = {
    "prc-accounting-law-2017-art12": "https://jdjc.mof.gov.cn/ckjd/202111/t20211112_3765255.htm",
    "prc-accounting-law-2024-art12": "https://jiangsu.chinatax.gov.cn/art/2024/6/28/art_23636_381.html",
}
_FIELDS = {"schema", "law_version", "law_url", "company_id", "company_name", "security_code",
           "document_id", "document_hash", "period_end", "source_ids", "currency"}
_V2_FIELDS = _FIELDS | {"issuer_legal_name", "issuer_credit_code"}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _proof_id(payload):
    return "unit:statutory:" + sha256(_encoded(payload).encode()).hexdigest()


def _identity_clause(text, name, code, year):
    compact = canonical_search_text(text)
    return (len(canonical_search_text(name)) >= 4 and canonical_search_text(name) in compact
            and re.search(r"股票代码[:：]?" + re.escape(code) + r"(?!\d)", compact)
            and re.search(re.escape(year) + r"年(?:年度|度)报告", compact))


def _domestic_cas_clause(text):
    compact = canonical_search_text(text)
    # A legal identity of the reporting company, not a subsidiary's address
    # or a bare country name. No assumption from ticker/amount magnitude.
    legal = re.search(r"(?<!子)(?:本)?公司营业执照统一社会信用代码[:：]?[0-9A-HJ-NPQRTUWXY]{18}(?![A-Z0-9])", compact, re.I)
    basis = re.search(r"(?<!子)(?:本)?公司财务报表(?:及附注)?[^。；]{0,180}财政部[^。；]{0,120}企业会计准则", compact)
    blocked = re.search(r"(?:本公司|公司)(?:系|为|在|注册于|成立于)[^。；]{0,30}(?:境外|香港|澳门|开曼|百慕大)", compact)
    return bool(legal and basis and not blocked)


def _report_legal_name(text, name, code, year):
    """Bind a display name to the issuer's legal name and stock code, not its length."""
    compact = canonical_search_text(text)
    display = canonical_search_text(name)
    if (not display or not re.search(r"(?:股票|证券)代码" + re.escape(code) + r"(?!\d)", compact)
            or not re.search(re.escape(year) + r"年(?:年度|度)报告", compact)):
        return ""
    candidates = re.findall(
        r"(?:公司的中文名称|公司中文名称|公司名称|中文名称)(.{2,80}?(?:股份有限公司|有限责任公司|有限公司))", compact)
    # Some originals state the legal name only in the annual-report title.
    candidates += re.findall(
        r"([\u3400-\u9fffa-z·]{2,80}?(?:股份有限公司|有限责任公司|有限公司))(?=20\d{2}年)", compact)
    aliases = re.search(r"(?:公司的中文简称|公司简称|股票简称|证券简称)" + re.escape(display), compact)
    matched = {legal for legal in candidates if display == legal or legal.startswith(display)
               or (aliases and display in legal)}
    return next(iter(matched)) if len(matched) == 1 else ""


def _foreign_issuer(text):
    compact = canonical_search_text(text)
    return bool(re.search(r"(?:本公司|公司)(?:系|为|在|注册于|成立于).{0,30}(?:境外|香港|澳门|开曼|百慕大)", compact))


def _issuer_credit_code(text, legal_name):
    compact = canonical_search_text(text)
    if _foreign_issuer(text):
        return ""
    # Explicit reporting-company licence, or the issuer's registration table.
    # A subsidiary's licence elsewhere in the book is not an issuer identity.
    direct = []
    for clause in re.split(r"[。；;\r\n]", str(text)):
        statement = canonical_search_text(clause)
        match = re.match(r"(?:本)?公司营业执照统一社会信用代码([0-9a-hj-npqrtuwxy]{18})(?![a-z0-9])", statement)
        if match:
            direct.append(match[1])
    if not direct and legal_name and legal_name in compact and re.search(
            r"(?:公司的中文名称|公司中文名称|公司名称)" + re.escape(legal_name), compact):
        registration = re.search(r"(?:注册变更情况|公司注册情况|公司的中文名称).{0,1200}?统一社会信用代码([0-9a-hj-npqrtuwxy]{18})(?![a-z0-9])", compact)
        if registration and not re.search(r"(?:子公司|下属公司|控股公司).{0,80}统一社会信用代码", registration[0]):
            direct = [registration[1]]
    return direct[0].upper() if len(set(direct)) == 1 else ""


def _issuer_cas_basis(text):
    if _foreign_issuer(text):
        return False
    compact = canonical_search_text(text)
    # Both common word orders are used in formal CAS preparation statements.
    # The reporting company must be the subject; a subsidiary statement cannot qualify.
    return bool(re.search(
        r"(?<!子)(?:本)?公司(?:基于.{0,60}?)?(?:编制的)?财务报表(?:及附注)?"
        r".{0,180}?财政部.{0,120}?企业会计准则", compact))


def _v2_sources_valid(refs, payload):
    if [ref.title for ref in refs] != ["report-identity", "issuer-legal-identity", "domestic-cas-basis"]:
        return False
    legal = _report_legal_name(refs[0].excerpt, payload["company_name"],
                              payload["security_code"], payload["period_end"][:4])
    return bool(legal and legal == payload["issuer_legal_name"]
                and _issuer_credit_code(refs[1].excerpt, legal) == payload["issuer_credit_code"]
                and re.fullmatch(r"[0-9A-HJ-NPQRTUWXY]{18}", payload["issuer_credit_code"])
                and _issuer_cas_basis(refs[2].excerpt))


def bind_statutory_presentation(resolution, page_texts, company, filing):
    """Capture applicable original clauses; no currency/scale rewrite in text."""
    if (company.market != "CN_A" or company.accounting_standard != "CAS"
            or filing.company_cik not in {company.cik, company.security_id}
            or str(filing.fiscal_period).upper() != "FY"
            or filing.form_type not in {"ANNUAL_REPORT", "ANNUAL"}
            or not re.fullmatch(r"[a-fA-F0-9]{64}", filing.content_hash or "")
            or not re.fullmatch(r"\d{4}-12-31", filing.period_end or "")
            or not re.match(r"(?:19|20)\d{2}-\d{2}-\d{2}", filing.filed_at or "")):
        return resolution
    # A direct non-CNY presentation basis is not overridden by legislation.
    if any(item.currency != "CNY" for item in (*resolution.declarations, *resolution.presentation_currencies)):
        return resolution
    code_match = re.match(r"(\d{6})(?:\.|$)", company.ticker)
    if not code_match or str(filing.filed_at)[:10] < "2017-11-05":
        return resolution
    code, year = code_match[1], filing.period_end[:4]
    identity = next(((page, text, _report_legal_name(text, company.name, code, year))
                     for page, text in enumerate(page_texts, 1)
                     if _report_legal_name(text, company.name, code, year)), None)
    if not identity:
        return resolution
    legal = next(((page, text, _issuer_credit_code(text, identity[2]))
                  for page, text in enumerate(page_texts, 1)
                  if _issuer_credit_code(text, identity[2])), None)
    basis = next(((page, text) for page, text in enumerate(page_texts, 1) if _issuer_cas_basis(text)), None)
    if not legal or not basis:
        return resolution
    refs = []
    for role, (page, text) in (("report-identity", identity[:2]), ("issuer-legal-identity", legal[:2]),
                              ("domestic-cas-basis", basis)):
        content = str(text).strip()
        evidence_id = "statutory-source:" + sha256(
            f"{filing.document_id}|{filing.content_hash}|{page}|{role}|{content}".encode()).hexdigest()
        refs.append(EvidenceRef(evidence_id, filing.document_id, filing.source_url, role,
                                f"page:{page}", content, filing.filed_at, filing.content_hash))
    law = "prc-accounting-law-2024-art12" if filing.filed_at[:10] >= "2024-07-01" else "prc-accounting-law-2017-art12"
    payload = {"schema": STATUTORY_CURRENCY_VERSION, "law_version": law, "law_url": _LAWS[law],
               "company_id": company.security_id, "company_name": company.name, "security_code": code,
               "document_id": filing.document_id, "document_hash": filing.content_hash,
               "period_end": filing.period_end, "source_ids": [ref.evidence_id for ref in refs], "currency": "CNY",
               "issuer_legal_name": identity[2], "issuer_credit_code": legal[2]}
    proof = EvidenceRef(_proof_id(payload), filing.document_id, filing.source_url,
                        "statutory presentation currency (not a printed unit)", refs[-1].locator,
                        _encoded(payload), filing.filed_at, filing.content_hash)
    return replace(resolution, statutory_presentation=tuple((*refs, proof)))


def _basis_proves_cny(proof, fact, evidence_by_id):
    try:
        payload = json.loads(proof.excerpt)
        if not isinstance(payload, dict):
            return False
        version = payload.get("schema")
        expected_fields = _FIELDS if version == _LEGACY_VERSION else _V2_FIELDS
        if (set(payload) != expected_fields or version not in {_LEGACY_VERSION, STATUTORY_CURRENCY_VERSION}
                or _proof_id(payload) != proof.evidence_id
                or payload["law_version"] not in _LAWS or payload["law_url"] != _LAWS[payload["law_version"]]
                or payload["currency"] != "CNY" or fact.currency != "CNY" or fact.market != "CN_A"
                or fact.fiscal_period != "FY" or fact.consolidated_scope not in {"consolidated", "parent"}
                or payload["company_id"] != fact.company_cik
                or payload["document_id"] != proof.document_id
                or payload["document_hash"] != proof.content_hash
                or not re.fullmatch(r"[a-fA-F0-9]{64}", proof.content_hash or "")
                or not re.fullmatch(r"\d{4}-12-31", payload["period_end"])
                or (fact.end_date != payload["period_end"] and not (
                    fact.usage_status == "comparator"
                    and fact.end_date == f"{int(payload['period_end'][:4]) - 1}-12-31"))):
            return False
        expected_law = "prc-accounting-law-2024-art12" if proof.published_at[:10] >= "2024-07-01" else "prc-accounting-law-2017-art12"
        if proof.published_at[:10] < "2017-11-05" or payload["law_version"] != expected_law:
            return False
        source_ids = payload["source_ids"]
        count = 2 if version == _LEGACY_VERSION else 3
        if not isinstance(source_ids, list) or len(source_ids) != count or len(set(source_ids)) != count:
            return False
        refs = [evidence_by_id.get(key) for key in source_ids]
        for ref in refs:
            if (ref is None or ref.document_id != proof.document_id or ref.content_hash != proof.content_hash
                    or ref.source_url != proof.source_url or ref.published_at != proof.published_at
                    or not re.fullmatch(r"page:[1-9]\d*", ref.locator or "")):
                return False
            expected = "statutory-source:" + sha256(
                f"{ref.document_id}|{ref.content_hash}|{ref.locator[5:]}|{ref.title}|{ref.excerpt}".encode()).hexdigest()
            if expected != ref.evidence_id:
                return False
        if version == STATUTORY_CURRENCY_VERSION:
            return _v2_sources_valid(refs, payload)
        return bool(refs[0].title == "report-identity" and refs[1].title == "domestic-cas-basis"
                    and _identity_clause(refs[0].excerpt, payload["company_name"], payload["security_code"], payload["period_end"][:4])
                    and _domestic_cas_clause(refs[1].excerpt))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def bound_money_unit(fact, refs, evidence_by_id):
    """Combine literal scale with independently replayed statutory currency."""
    ordinary = tuple(ref for ref in refs if not ref.evidence_id.startswith("unit:statutory:"))
    unit = combined_money_unit(ref.excerpt for ref in ordinary)
    proofs = tuple(ref for ref in refs if ref.evidence_id.startswith("unit:statutory:"))
    if not proofs:
        return unit
    # Legislation never supplies scale or resolves any explicit currency
    # conflict. This approved route is deliberately limited to literal 元.
    if (unit.currencies or not unit.scale_explicit or unit.scales != frozenset({"1"})
            or len(proofs) != 1 or not _basis_proves_cny(proofs[0], fact, evidence_by_id)):
        return unit
    return LiteralMoneyUnit(frozenset({"CNY"}), unit.scales, True)
