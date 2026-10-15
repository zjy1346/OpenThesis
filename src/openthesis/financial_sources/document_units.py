"""Pure, explicit financial-statement presentation declarations.

This is not a nearest-number heuristic. Only a declaration about the financial
statements as a whole can supply a missing formal table header. Row/table
declarations override it; incompatible broad declarations stay unresolved.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence

from ..text_normalization import canonical_search_text
from ..financial_unit_declarations import literal_money_unit
from ..domain import EvidenceRef


@dataclass(frozen=True, slots=True)
class DocumentUnitDeclaration:
    page: int
    excerpt: str
    currency: str
    scale: str
    scope: str = "all"


@dataclass(frozen=True, slots=True)
class DocumentPresentationCurrency:
    """Verbatim currency-only clause about the financial statements themselves."""
    page: int
    excerpt: str
    currency: str
    scope: str = "all"


@dataclass(frozen=True, slots=True)
class NoteUnitRegion:
    """A note declaration cannot provide units to a formal statement."""
    start_page: int
    end_page: int
    scope: str
    heading: str
    declaration: DocumentUnitDeclaration
    period_page: int
    period_excerpt: str
    end_heading: str = ""


@dataclass(frozen=True, slots=True)
class DocumentUnitResolution:
    declarations: tuple[DocumentUnitDeclaration, ...] = ()
    presentation_currencies: tuple[DocumentPresentationCurrency, ...] = ()
    note_regions: tuple[NoteUnitRegion, ...] = ()
    # Native index text for independently captioned note candidates. The
    # coordinate backend can change extraction order of multi-level tables;
    # that is not a new source or permission to infer missing columns.
    note_page_texts: tuple[tuple[int, str], ...] = ()
    note_continuations: tuple[tuple[int, int, str], ...] = ()
    # Separate host-bound legal inference from original printed declarations.
    statutory_presentation: tuple[EvidenceRef, ...] = ()

    def note_for_page(self, page: int) -> NoteUnitRegion | None:
        regions = tuple(item for item in self.note_regions if item.start_page <= page < item.end_page)
        return regions[0] if len(regions) == 1 else None

    def note_text_for_page(self, page: int) -> str | None:
        matches = tuple(text for number, text in self.note_page_texts if number == page)
        return matches[0] if len(matches) == 1 and self.note_for_page(page) is not None else None

    def note_continuation_for_page(self, page: int) -> tuple[int, str] | None:
        matches = tuple((prior, text) for number, prior, text in self.note_continuations if number == page)
        return matches[0] if len(matches) == 1 and self.note_for_page(page) is not None else None

    def for_scope(self, scope: str) -> DocumentUnitDeclaration | None:
        applicable = tuple(item for item in self.declarations if item.scope in {"all", scope})
        identities = {(item.currency, item.scale) for item in applicable}
        currencies = {item.currency for item in (*applicable, *self.presentation_currencies)
                      if item.scope in {"all", scope}}
        return applicable[0] if len(identities) == 1 and len(currencies) == 1 else None


    def currency_for_scope(self, scope: str) -> DocumentPresentationCurrency | None:
        applicable = tuple(item for item in self.presentation_currencies if item.scope in {"all", scope})
        currencies = {item.currency for item in (*applicable, *self.declarations) if item.scope in {"all", scope}}
        return applicable[0] if applicable and len(currencies) == 1 else None


def discover_presentation_currencies(page_texts: Sequence[str]) -> tuple[DocumentPresentationCurrency, ...]:
    """Separate stated presentation currency from a later default display scale.

    Local 'yuan' tables may override 'thousands unless otherwise indicated',
    not the currency explicitly stated for the financial statements. Capture
    only a complete original clause with no multiplier, never rewrite a basis
    sentence to remove a conflicting unit. Functional currency alone is not
    presentation currency and monetary narrative is not a basis declaration.
    """
    declarations = []
    for page, text in enumerate(page_texts, 1):
        # CAS issuers often state the consolidated reporting currency in the
        # functional-currency policy: subsidiary books may differ, but their
        # financial statements are explicitly translated for presentation.
        # Capture the complete original sentence, not functional currency alone
        # or an exchange-rate narrative. This applies only to consolidation.
        for sentence in re.split(r"[。；;]", str(text or "")):
            joined = canonical_search_text(sentence)
            observed = re.search(r"(?:本公司|本集团)(?:下属|所属)?(?:的)?子公司.{0,120}"
                r"编制(?:合并)?财务报表时(?:统一)?折算为(?:人民币|美元|港元|港币)$", joined)
            if observed is None or re.search(r"可能|拟|不折算|无需|若|假设", observed.group()):
                continue
            # Retain actual extraction whitespace and the full source clause.
            position = re.search(r"本[公集][司团]", sentence)
            excerpt = sentence[position.start():].strip() if position else sentence.strip()
            unit = literal_money_unit(excerpt)
            if len(excerpt) <= 650 and not unit.scale_explicit and len(unit.currencies) == 1:
                item = DocumentPresentationCurrency(page, excerpt, next(iter(unit.currencies)), "consolidated")
                if item not in declarations:
                    declarations.append(item)
        for clause in re.split(r"[,，;；。]|(?<=\.)\s+(?=[A-Z])", str(text or "")):
            excerpt = clause.strip()
            if not excerpt or len(excerpt) > 650:
                continue
            normalized = canonical_search_text(excerpt)
            if re.search(r"本附注|本注释|thesenotes|notestothe", normalized):
                continue
            chinese = bool(re.search(
                r"财务报表.{0,80}(?:所采用的货币|(?:列报|呈报|报告)货币|以.{0,30}(?:列示|呈列|表示))",
                normalized))
            english = bool(re.search(r"financialstatementsare(?:presented|expressed)in", normalized))
            # A source may explicitly name both functional and reporting
            # currency without repeating 'financial statements'. Require the
            # complete declarative clause, not a mention of a subsidiary's
            # currency, a possibility, or functional currency alone.
            reporting_clause = bool(re.fullmatch(
                r"(?:\d+记账本位币)?(?:本公司|本集团|母公司)?"
                r"以(?:人民币|美元|港元|港币)作为(?:记账本位币(?:和|及))?"
                r"(?:报告|呈报|列报)货币", normalized))
            if not (chinese or english or reporting_clause):
                continue
            unit = literal_money_unit(excerpt)
            if unit.scale_explicit or len(unit.currencies) != 1:
                continue
            scope = ("parent" if any(token in normalized for token in
                ("母公司财务报表", "parentcompanyfinancialstatements", "母公司以")) else "all")
            declaration = DocumentPresentationCurrency(page, excerpt, next(iter(unit.currencies)), scope)
            if declaration not in declarations:
                declarations.append(declaration)
    return tuple(declarations)


def discover_document_units(page_texts: Sequence[str]) -> DocumentUnitResolution:
    declarations = []
    for page, text in enumerate(page_texts, 1):
        # Join extraction line breaks but not arbitrary pages. At most the
        # following sentence can complete a presentation declaration.
        sentences = re.split(r"(?<=[。；;])|(?<=\.)\s+(?=[A-Z])", str(text or ""))
        for index, sentence in enumerate(sentences):
            if len(sentence) > 650:
                # Native PDF text is often a whole paragraph; take a bounded
                # window anchored on financial statements, never monetary prose.
                anchor = re.search(r"财[务務][报報]表|financial statements", sentence, re.I)
                if not anchor:
                    continue
                sentence = sentence[max(0, anchor.start() - 40):anchor.start() + 600]
            excerpt = sentence.strip()
            normalized = canonical_search_text(excerpt)
            if re.search(r"本附注|本注释|thesenotes|notestothe", normalized):
                continue
            if "财务报表" not in normalized and "financialstatements" not in normalized:
                continue
            if index + 1 < len(sentences) and len(excerpt) + len(sentences[index + 1]) <= 650:
                # Only append when the first sentence supplies reporting currency
                # and the next explicitly addresses all amounts or exceptions.
                following = canonical_search_text(sentences[index + 1])
                if any(token in following for token in ("allamounts", "allvalues", "所有金额", "除另有", "unless")):
                    excerpt += sentences[index + 1].strip()
                    normalized = canonical_search_text(excerpt)
            chinese_basis = (
                any(token in normalized for token in ("均以", "所有金额", "全部金额"))
                and any(token in normalized for token in ("表示", "列示", "呈列"))
            )
            english_basis = (
                any(token in normalized for token in ("presented", "expressed", "rounded"))
                and any(token in normalized for token in ("allamounts", "allvalues", "unless", "except"))
            )
            if not (chinese_basis or english_basis):
                continue
            currencies = {
                code for code, markers in (
                    ("CNY", ("人民币", "renminbi", "rmb", "cny")),
                    ("USD", ("美元", "usd", "us$", "usdollars", "unitedstatesdollars")),
                    ("HKD", ("港元", "港币", "hkd", "hk$", "hongkongdollars")),
                ) if any(marker in normalized for marker in markers)
            }
            if len(currencies) != 1:
                continue
            scale_tokens = (
                ("1000000000", ("billion", "十亿元")),
                ("100000000", ("亿元",)),
                ("1000000", ("million", "百万元")),
                ("10000", ("万元",)),
                ("1000", ("thousand", "千元", "'000", "’000")),
            )
            # Consume the longest literal first: 百万元 contains 万元, but
            # two separate 'millions or thousands' declarations conflict.
            literal_scales = {token: value for value, tokens in scale_tokens for token in tokens}
            pattern = "|".join(re.escape(token) for token in sorted(literal_scales, key=len, reverse=True))
            scales = {literal_scales[match.group()] for match in re.finditer(pattern, normalized)}
            if not scales:
                if any(token in normalized for token in ("以人民币元", "人民币为单位", "inyuan", "indollars")):
                    scales = {"1"}
                else:
                    continue
            scope = ("parent" if any(token in normalized for token in ("母公司财务报表", "parentcompanyfinancialstatements"))
                     else "all")
            for scale in sorted(scales):
                item = DocumentUnitDeclaration(page, excerpt, next(iter(currencies)), scale, scope)
                if item not in declarations:
                    declarations.append(item)
    regions = discover_note_unit_regions(page_texts)
    from .note_disclosures import note_row_disclosures
    note_texts, continuations = [], []
    for page, text in enumerate(page_texts, 1):
        if sum(region.start_page <= page < region.end_page for region in regions) != 1:
            continue
        years = set(re.findall(r"(?<!\d)((?:19|20)\d{2})\s*年\s*(?:年度报告|度报告)", text))
        if len(years) != 1:
            continue
        year = int(next(iter(years)))
        if note_row_disclosures(text, year):
            note_texts.append((page, text))
        elif page > 1:
            # A neighbour alone is not proof: both pages must remain inside
            # this exact bounded chapter, with observed caption/column order.
            region = next(region for region in regions if region.start_page <= page < region.end_page)
            previous = page_texts[page - 2]
            if region.start_page <= page - 1 and note_row_disclosures(text, year, previous):
                note_texts.append((page, text))
                continuations.append((page, page - 1, previous))
    return DocumentUnitResolution(tuple(declarations), discover_presentation_currencies(page_texts),
                                  regions, tuple(note_texts), tuple(continuations))


def discover_note_unit_regions(page_texts: Sequence[str]) -> tuple[NoteUnitRegion, ...]:
    """Bind explicit note-only units to explicitly named note chapters.

    A calendar-year clause is required for the narrow current/prior annual
    column adapter. Other period shapes remain unresolved, never guessed.
    These records are deliberately unavailable through ``for_scope``.
    """
    units, calendars, headings = [], [], []
    for page, text in enumerate(page_texts, 1):
        original = str(text or "")
        positions = [index for index, char in enumerate(original) if not char.isspace()]
        def verbatim(match):
            return original[positions[match.start()]:positions[match.end() - 1] + 1]
        for line in str(text or "").splitlines():
            compact = canonical_search_text(line)
            if re.fullmatch(r"[一二三四五六七八九十百]+[、.．].+", line.strip()):
                headings.append((page, line.strip(), compact))
        joined = re.sub(r"\s+", "", original)
        for match in re.finditer(r"(?:除特别注明外[,，]?)?本附注(?:金额)?单位(?:均)?(?:为|以)"
                                 r"(?:人民币|人民幣|美元|港元|港币|港幣)(?:百万元|万元|千元|元)", joined):
            excerpt = verbatim(match)
            unit = literal_money_unit(excerpt)
            if len(unit.currencies) == len(unit.scales) == 1 and unit.scale_explicit:
                units.append(DocumentUnitDeclaration(page, excerpt, next(iter(unit.currencies)),
                                                     next(iter(unit.scales)), "notes"))
        for match in re.finditer(r"(?:本公司)?会计年度[^。；]{0,90}(?:公历年度[^。；]{0,50})?"
                                 r"(?:1月1日)[^。；]{0,30}(?:12月31日)", joined):
            calendars.append((page, verbatim(match)))
    regions = []
    for index, (page, heading, compact) in enumerate(headings):
        scope = note_chapter_scope(heading)
        if scope is None:
            continue
        number = note_chapter_number(heading)
        boundary = next((item for item in headings[index + 1:]
                         if note_chapter_number(item[1]) == number + 1), None)
        if boundary is None:
            continue  # No observed chapter boundary, no safely bounded scope.
        end, end_heading, _ = boundary
        applicable = tuple(unit for unit in units if unit.page <= page)
        identities = {(item.currency, item.scale) for item in applicable}
        periods = tuple(item for item in calendars if item[0] <= page)
        if len(identities) != 1 or not applicable or not periods or end <= page:
            continue
        unit, period = applicable[-1], periods[-1]
        regions.append(NoteUnitRegion(page, end, scope,
                                     heading, unit, period[0], period[1], end_heading))
    return tuple(regions)


def note_chapter_scope(heading: str) -> str | None:
    """Read only an explicitly named accounting scope from a note chapter.

    附注 and 注释 are presentation aliases, not authority to infer scope
    from an unqualified chapter or a subsidiary/mixed-scope heading.
    """
    match = re.fullmatch(r"[一二三四五六七八九十百]+(合并|母公司)财务报表"
                         r"(?:主要|重要)?项目(?:注释|附注)", canonical_search_text(heading))
    return ("parent" if match[1] == "母公司" else "consolidated") if match else None


def note_chapter_number(heading: str) -> int:
    match = re.match(r"^\s*([一二三四五六七八九十]+)[、.．]", heading)
    if not match:
        return -1
    values = {char: index for index, char in enumerate("零一二三四五六七八九")}
    token = match[1]
    if "十" in token:
        left, right = token.split("十", 1)
        if len(left) > 1 or len(right) > 1:
            return -1
        return (values.get(left, 1) if left else 1) * 10 + values.get(right, 0)
    return values.get(token, -1)
