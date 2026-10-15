"""Explicit, same-original monetary disclosures; no table currency inference.

An audit or note can disclose a fact independently of a formal statement.
Keep its printed precision and require the sentence itself to establish the
concept, period and scope. The validator replays this grammar against bound
original text; the extractor's interpretation is not admission evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import re

from ..domain import Company, EvidenceRef, FilingDocument, FinancialFact
from ..financial_contracts import QuantityKind, SourceAmount, STATEMENT_FOR
from ..financial_unit_declarations import literal_money_unit
from ..text_normalization import canonical_search_text

DISCLOSURE_VERSION = "financial-explicit-disclosure-v1"
_LABELS = {
    "营业收入": "revenue", "營業收入": "revenue",
    "归属于母公司所有者的净利润": "parent_net_income",
    "归属于上市公司股东的净利润": "parent_net_income",
    "净利润": "net_income", "淨利潤": "net_income",
    "经营活动产生的现金流量净额": "operating_cash_flow",
    "资产总计": "assets", "资产总额": "assets",
    "负债合计": "liabilities", "负债总额": "liabilities",
    "所有者权益合计": "total_equity",
}
_LABEL = "|".join(re.escape(label) for label in sorted(_LABELS, key=len, reverse=True))
_AMOUNT = re.compile(
    rf"(?P<label>{_LABEL})\s*(?:为|為|是|达|達|计|計|[:：])\s*"
    r"(?P<currency>人民币|人民幣|美元|港元|港币|港幣|CNY|RMB|USD|HKD)\s*"
    r"(?P<amount>[+-]?(?:\d{1,3}(?:,\s*\d{3})+|\d+)(?:\.\d+)?)\s*"
    r"(?P<unit>十亿元|百万元|亿元|万元|千元|元|十億美元|百万美元|万美元|美元|港元)", re.I,
)
_SENTENCE = re.compile(r"[^。；;!?！？]+(?:[。；;!?！？]|$)")
_NOT_HISTORICAL = re.compile(r"预计|預計|预测|預測|预算|預算|假设|假設|计划|計劃|可能|若|不超过|不少于|约为|約為")
_UNIT_SCALES = {"十亿元": "1000000000", "十億美元": "1000000000", "百万元": "1000000",
    "百万美元": "1000000", "亿元": "100000000", "万元": "10000", "万美元": "10000",
    "千元": "1000", "元": "1", "美元": "1", "港元": "1"}


@dataclass(frozen=True, slots=True)
class MoneyDisclosure:
    concept: str
    label: str
    raw_value: str
    currency: str
    scale: str
    period_end: str
    scope: str
    excerpt: str

    @property
    def display_quantum(self) -> Decimal:
        raw = Decimal(self.raw_value.replace(",", "").replace(" ", ""))
        return Decimal(1).scaleb(raw.as_tuple().exponent) * Decimal(self.scale)


def explicit_money_disclosures(text: str, period_end: str) -> tuple[MoneyDisclosure, ...]:
    """Recognize complete annual sentences, not nearest numbers or defaults.

    Chinese `YYYY年度` proves a calendar FY, not a March-ending HK FY. Other
    periods remain available as raw disclosures until their dates are explicit.
    An unsupported layout is a missing candidate, never a fabricated identity.
    """
    if not re.fullmatch(r"(?:19|20)\d{2}-12-31", period_end or ""):
        return ()
    disclosures = []
    for sentence in _SENTENCE.findall(str(text or "")):
        excerpt = sentence.strip()
        if not excerpt or len(excerpt) > 900 or _NOT_HISTORICAL.search(excerpt):
            continue
        years = re.findall(r"(?<!\d)((?:19|20)\d{2})\s*(?:年度|年)(?!\d)", excerpt)
        scopes = set()
        if re.search(r"合[并併]财[务務][报報]表|合併財務報表", excerpt):
            scopes.add("consolidated")
        if re.search(r"母公司财[务務][报報]表|母公司財務報表", excerpt):
            scopes.add("parent")
        matches = list(_AMOUNT.finditer(excerpt))
        # Multiple years/amounts cannot be assigned by order or proximity.
        if set(years) != {period_end[:4]} or len(scopes) != 1 or len(matches) != 1:
            continue
        match = matches[0]
        scope_match = re.search(r"(?:合[并併]财[务務][报報]表|合併財務報表|母公司财[务務][报報]表|母公司財務報表)", excerpt)
        between = excerpt[scope_match.end():match.start()] if scope_match else ""
        if (scope_match is None or scope_match.end() > match.start()
                or not re.fullmatch(r"\s*(?:中|的)?\s*(?:(?:19|20)\d{2}\s*(?:年度|年)\s*(?:的)?\s*)?", between)):
            # Segment/subsidiary/component qualifiers are not a group total.
            continue
        unit = literal_money_unit(match["currency"] + match["unit"])
        if len(unit.currencies) != 1:
            continue
        currency, scale = next(iter(unit.currencies)), _UNIT_SCALES[match["unit"]]
        raw = re.sub(r"\s+", "", match["amount"])
        try:
            SourceAmount(raw, str(scale), QuantityKind.MONEY, currency, ("source",)).normalize()
        except ValueError:
            continue
        disclosures.append(MoneyDisclosure(
            _LABELS[match["label"]], match["label"], raw, currency, str(scale),
            period_end, next(iter(scopes)), excerpt,
        ))
    return tuple(disclosures)


def disclosure_page_needed(text: str, period_end: str = "") -> bool:
    ends = (period_end,) if period_end else tuple(
        year + "-12-31" for year in set(re.findall(r"(?<!\d)((?:19|20)\d{2})\s*(?:年度|年)", text)))
    return any(explicit_money_disclosures(text, end) for end in ends)


def extract_disclosure_candidates(
    company: Company, filing: FilingDocument, page: int, text: str,
    bbox: tuple[float, float, float, float],
) -> tuple[list[FinancialFact], list[EvidenceRef]]:
    """Positioned page context is retained intact, including issuer heading."""
    if (filing.fiscal_period != "FY" or not filing.content_hash
            or not company.name or canonical_search_text(company.name) not in canonical_search_text(text)):
        return [], []
    facts, refs = [], []
    context_id = "disclosure-context:" + hashlib.sha256(
        f"{filing.document_id}|{page}|{bbox}|{text}".encode("utf-8")
    ).hexdigest()[:24]
    context = EvidenceRef(context_id, filing.document_id, filing.source_url,
        "original monetary disclosure context", f"page:{page}", text,
        filing.filed_at, filing.content_hash, bbox)
    for item in explicit_money_disclosures(text, filing.period_end):
        identity = hashlib.sha256(
            f"{filing.document_id}|{page}|{item.excerpt}".encode("utf-8")
        ).hexdigest()[:24]
        evidence_id = "fact:disclosure:" + identity
        lineage = SourceAmount(item.raw_value, item.scale, QuantityKind.MONEY,
            item.currency, (context_id,)).normalize()
        start = item.period_end[:4] + "-01-01" if STATEMENT_FOR[item.concept] != "balance_sheet" else None
        facts.append(FinancialFact(
            "disclosure:" + identity, company.security_id, item.concept, item.label,
            float(Decimal(lineage.normalized_value)), item.currency,
            int(item.period_end[:4]), "FY", filing.form_type, start, item.period_end,
            filing.filed_at, filing.accession_number, filing.source_url,
            scope=item.scope, entity=company.name, market=company.market,
            statement=STATEMENT_FOR[item.concept], period_start=start,
            consolidated_scope=item.scope, currency=item.currency,
            unit_scale=float(Decimal(item.scale)), unit_provenance="explicit_disclosure",
            revision=filing.revision, source_document=filing.primary_document,
            source_page=page, source_bbox=bbox, raw_text=item.excerpt,
            parser_version=DISCLOSURE_VERSION, normalization_lineage=lineage,
            classification_evidence_ids=(context_id,),
        ))
        refs.append(EvidenceRef(evidence_id, filing.document_id, filing.source_url,
            "explicit monetary disclosure", f"page:{page}", item.excerpt,
            filing.filed_at, filing.content_hash, bbox))
    if facts:
        refs.append(context)
    return facts, refs


def disclosure_source_issue(
    fact: FinancialFact, row: EvidenceRef | None, evidence: dict[str, EvidenceRef],
) -> str | None:
    """Independent source binding, semantic replay and exact normalization."""
    if fact.parser_version != DISCLOSURE_VERSION:
        return None
    context_ids = fact.classification_evidence_ids
    context = evidence.get(context_ids[0]) if len(context_ids) == 1 else None
    lineage = fact.normalization_lineage
    if row is None or context is None or lineage is None:
        return "disclosure_source_proof_missing"
    if (not re.fullmatch(r"[0-9a-fA-F]{64}", row.content_hash or "")
            or row.document_id != context.document_id or row.source_url != context.source_url
            or row.content_hash != context.content_hash or row.locator != context.locator
            or row.locator != f"page:{fact.source_page}" or not row.bbox
            or row.bbox != context.bbox or row.bbox != fact.source_bbox
            or row.excerpt != fact.raw_text or fact.raw_text not in context.excerpt
            or not fact.entity or canonical_search_text(fact.entity) not in canonical_search_text(context.excerpt)):
        return "disclosure_source_binding_mismatch"
    expected_context = "disclosure-context:" + hashlib.sha256(
        f"{row.document_id}|{fact.source_page}|{context.bbox}|{context.excerpt}".encode("utf-8")
    ).hexdigest()[:24]
    if context.evidence_id != expected_context or tuple(lineage.unit_evidence_ids) != (expected_context,):
        return "disclosure_context_identity_mismatch"
    matches = explicit_money_disclosures(context.excerpt, fact.end_date)
    matching = [item for item in matches if item.excerpt == fact.raw_text]
    if len(matching) != 1:
        return "disclosure_semantics_unproven"
    item = matching[0]
    expected = SourceAmount(item.raw_value, item.scale, QuantityKind.MONEY,
        item.currency, (expected_context,)).normalize()
    start = fact.end_date[:4] + "-01-01" if STATEMENT_FOR[item.concept] != "balance_sheet" else None
    if (fact.concept != item.concept or fact.reported_concept != item.label
            or fact.consolidated_scope != item.scope or fact.scope != item.scope
            or fact.currency != item.currency or fact.unit != item.currency
            or fact.unit_scale != float(Decimal(item.scale)) or lineage != expected
            or fact.fiscal_period != "FY" or fact.fiscal_year != int(fact.end_date[:4])
            or fact.start_date != start or fact.period_start != start
            or fact.statement != STATEMENT_FOR[item.concept]
            or fact.unit_provenance != "explicit_disclosure"):
        return "disclosure_identity_or_amount_mismatch"
    return None
