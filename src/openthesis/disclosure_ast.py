from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable


class BlockRole(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    NAVIGATION = "navigation"
    HEADER_FOOTER = "header_footer"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DisclosureBlock:
    role: BlockRole
    text: str
    locator: str
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    cells: tuple[tuple[str, ...], ...] = ()
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DisclosureDocumentAST:
    source_hash: str
    source_type: str
    blocks: tuple[DisclosureBlock, ...]

    def content_blocks(self) -> tuple[DisclosureBlock, ...]:
        """Navigation remains in the AST but is excluded from topic input."""
        return tuple(
            block for block in self.blocks
            if block.role not in {BlockRole.NAVIGATION, BlockRole.HEADER_FOOTER}
        )

    @classmethod
    def from_blocks(
        cls, source_hash: str, source_type: str, blocks: Iterable[DisclosureBlock]
    ) -> "DisclosureDocumentAST":
        return cls(source_hash, source_type, tuple(blocks))
