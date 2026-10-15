"""Explicit issuer presentation qualifies standard taxonomy roles.

No issuer-name rules, materiality rounding, or absence-as-zero inference. This
bounded lane handles a direct disclosure that NCI is included in equity/APIC;
unsupported presentation differences remain available for separate review.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET


PRESENTATION_VERSION = "inline-presentation-v1"
_BLOCKS = frozenset({"div", "p"})
_DIRECT_NCI = r"non[-\s]?controlling interests?"
_DIRECT_INCLUSION = re.compile(
    rf"\b(?:{_DIRECT_NCI}|NCI(?: and RNCI)?)\s+(?:are|is)\s+included\s+"
    r"(?:within|in)\s+(?:additional paid[-\s]in capital|"
    r"(?:total )?(?:stockholders|shareholders)[’']?\s+equity)\b", re.I,
)
_NCI_DEFINITION = re.compile(rf"\b{_DIRECT_NCI}\s*\(NCI\)", re.I)
_TOTAL_EQUITY_ROW = re.compile(r"^total (?:stockholders|shareholders)[’']?\s+equity\b", re.I)
_BALANCE_ROW = re.compile(r"^balance (?:as of|at)\b", re.I)


def explicit_nci_in_equity(text: str) -> bool:
    text = " ".join(text.split())
    for match in _DIRECT_INCLUSION.finditer(text):
        # An undefined acronym is not an accounting definition. Likewise,
        # quoted examples and conditional prose are not current disclosures.
        if match.group().upper().startswith("NCI") and not _NCI_DEFINITION.search(text):
            continue
        sentence_start = max(text.rfind(".", 0, match.start()), text.rfind(";", 0, match.start())) + 1
        prefix = text[sentence_start:match.start()]
        if re.search(r"\b(?:if|might|may|could|would|example|hypothetical|not)\b", prefix, re.I):
            continue
        return True
    return False


def presentation_disclosures(root: ET.Element) -> tuple[ET.Element, ...]:
    """Select minimal source blocks in one bounded postorder traversal."""
    has_block_descendant: dict[int, bool] = {}
    found = []
    for element in reversed(tuple(root.iter())):
        descendants = any(child.tag.split("}")[-1] in _BLOCKS
                          or has_block_descendant.get(id(child), False) for child in element)
        has_block_descendant[id(element)] = descendants
        if element.tag.split("}")[-1] not in _BLOCKS or descendants:
            continue
        chunks, size = [], 0
        for chunk in element.itertext():
            size += len(chunk)
            if size > 4096:
                break
            chunks.append(chunk)
        else:
            if explicit_nci_in_equity("".join(chunks)):
                found.append(element)
                if len(found) >= 8:
                    break
    return tuple(reversed(found))


def presentation_role(
    standard: tuple[str, str, str] | None, row: ET.Element | None,
    disclosures: tuple[ET.Element, ...], *, consolidated: bool = False,
) -> tuple[str, str, str] | None:
    """A labeled total plus explicit inclusion proves total, never parent."""
    if standard is None or standard[0] != "equity" or row is None:
        return standard
    label = " ".join(" ".join(row.itertext()).split())
    if (consolidated and row.tag.split("}")[-1] == "tr"
            and (_TOTAL_EQUITY_ROW.match(label) or _BALANCE_ROW.match(label))
            and any(explicit_nci_in_equity("".join(item.itertext())) for item in disclosures)):
        return ("total_equity", standard[1], standard[2])
    return standard
