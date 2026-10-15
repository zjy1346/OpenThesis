"""Read-only report projection from an isolated input snapshot.

No source extraction, model provider, retry or database write belongs here.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Sequence


def artifacts_for_report_projection(artifacts: Sequence[dict[str, Any]], latest_payload: Any) -> list[dict[str, Any]]:
    """Project the CAS-selected report last without rewriting audit history."""
    projected = list(artifacts)
    if isinstance(latest_payload, dict):
        for index in range(len(projected) - 1, -1, -1):
            item = projected[index]
            if item.get("artifact_type") == "research-report" and item.get("content") == latest_payload:
                projected.append(projected.pop(index))
                break
    return projected


@dataclass(frozen=True, slots=True)
class ReportReadSnapshot:
    run_id: str
    company_name: str
    run_status: str
    artifacts: Sequence[Any]
    continuity: dict[str, Any] | None
    language: str
    include_technical: bool = False
    revision_id: str = ""
    revision_generation: int = 0

    def __post_init__(self) -> None:
        # Storage/cache owners may mutate their decoded payloads after this
        # read begins. Borrowed objects must not change input identity or let
        # a custom renderer edit the caller's persisted view.
        object.__setattr__(self, "artifacts", tuple(deepcopy(tuple(self.artifacts))))
        object.__setattr__(self, "continuity", deepcopy(self.continuity))

    @property
    def input_generation(self) -> str:
        if self.revision_id:
            return f"revision:{self.revision_generation}:{self.revision_id}"
        encoded = json.dumps(
            self.artifacts, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), default=str,
        ).encode("utf-8")
        return f"legacy:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ReportReadResult:
    run_id: str
    report_contract_version: str
    report_revision_id: str | None
    report_input_generation: str
    read_state: str
    is_substantive: bool
    visible_sections: tuple[dict[str, Any], ...]
    diagnostics: tuple[dict[str, str], ...]
    markdown: str
    html: str
    document: Any | None = None


class ReportReadError(RuntimeError):
    """Safe, machine-readable failure from a required historical read step."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _report_read_state(*, has_substantive_content: bool, readiness_complete: bool) -> str:
    if not has_substantive_content:
        return "diagnostic_only"
    return "ready" if readiness_complete else "partial"


class ReportService:
    """Render both report projections from the same canonical artifacts."""

    def __init__(self, markdown_renderer: Callable[..., str], html_renderer: Callable[..., str]):
        self._markdown_renderer = markdown_renderer
        self._html_renderer = html_renderer

    def markdown(self, run_id: str, artifacts: Sequence[Any], **kwargs: Any) -> str:
        return self._markdown_renderer(run_id, deepcopy(tuple(artifacts)), **deepcopy(kwargs))

    def html(self, run_id: str, artifacts: Sequence[Any], **kwargs: Any) -> str:
        return self._html_renderer(run_id, deepcopy(tuple(artifacts)), **deepcopy(kwargs))

    def document(self, run_id: str, artifacts: Sequence[Any], **kwargs: Any) -> Any | None:
        """Assemble the canonical immutable document for the standard renderers."""
        if (
            getattr(self._markdown_renderer, "__name__", "") == "render_research_run"
            and getattr(self._html_renderer, "__name__", "") == "render_research_html"
        ):
            from ..report_document import assemble_report_document

            return assemble_report_document(
                run_id,
                deepcopy(tuple(artifacts)),
                continuity=deepcopy(kwargs.get("continuity")),
                language=kwargs.get("language", "zh-CN"),
                company_name=kwargs.get("company_name", ""),
                include_technical=kwargs.get("include_technical", False),
                run_status=kwargs.get("run_status", ""),
            )
        return None

    def render(self, run_id: str, artifacts: Sequence[Any], **kwargs: Any) -> tuple[str, str]:
        # The standard renderers are compatibility wrappers over the canonical
        # document.  Assemble once here so both formats share precisely the
        # same readiness, claims, sources, and delivery diagnostics.
        document = self.document(run_id, artifacts, **kwargs)
        if document is not None:
            from ..report_document import render_html, render_markdown

            return render_markdown(document), render_html(document)
        return self.markdown(run_id, artifacts, **kwargs), self.html(run_id, artifacts, **kwargs)

    def read(self, snapshot: ReportReadSnapshot) -> ReportReadResult:
        """Project one immutable report snapshot into every read representation."""
        document = self.document(
            snapshot.run_id,
            snapshot.artifacts,
            continuity=snapshot.continuity,
            language=snapshot.language,
            company_name=snapshot.company_name,
            include_technical=snapshot.include_technical,
            run_status=snapshot.run_status,
        )
        if document is None:
            markdown, rendered_html = self.render(
                snapshot.run_id,
                snapshot.artifacts,
                continuity=snapshot.continuity,
                language=snapshot.language,
                company_name=snapshot.company_name,
                include_technical=snapshot.include_technical,
                run_status=snapshot.run_status,
            )
            # Custom renderer adapters are retained for tests and integrations,
            # but cannot claim verification metadata they do not provide.
            body = "\n".join(
                line for line in str(markdown).splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ).strip()
            visible = ({
                "section_id": "legacy-renderer-content",
                "title": "Report content",
                "source_artifact_ids": [],
                "verification_state": "unverified",
                "is_visible": True,
                "is_substantive": False,
                "diagnostic_code": "LEGACY_RENDERER_UNVERIFIED",
            },) if body else ()
            return ReportReadResult(
                run_id=snapshot.run_id,
                report_contract_version="1",
                report_revision_id=snapshot.revision_id or None,
                report_input_generation=snapshot.input_generation,
                read_state=_report_read_state(
                    has_substantive_content=False, readiness_complete=False,
                ),
                is_substantive=False,
                visible_sections=visible,
                diagnostics=({"code": "LEGACY_RENDERER_UNVERIFIED", "location": "report"},),
                markdown=str(markdown),
                html=str(rendered_html),
            )

        from ..report_document import render_html, render_markdown

        visible_sections = tuple(
            {
                "section_id": section.section_id,
                "title": section.title,
                "source_artifact_ids": list(section.source_artifact_ids),
                "verification_state": section.verification_state,
                "is_substantive": section.is_substantive,
                "is_visible": section.is_visible,
                "completeness": section.completeness,
                "content_roles": sorted({node.role for node in section.nodes
                                         if node.kind not in {"heading", "confidence_heading"}}),
                "content_semantics": [
                    {"kind": node.kind, "role": node.role,
                     "source_ids": list(node.source_ids), "materiality": node.materiality,
                     "validation_state": node.validation_record.state,
                     "validation_source_artifact_id": node.validation_record.source_artifact_id}
                    for node in section.nodes if node.kind not in {"heading", "confidence_heading"}
                ],
                "diagnostic_code": section.diagnostic_code,
            }
            for section in document.sections
            if section.is_visible
        )
        has_substantive_content = any(section.is_substantive for section in document.sections)
        diagnostics = tuple(
            {"code": item.code, "location": item.location}
            for item in document.delivery_diagnostics
        )
        return ReportReadResult(
            run_id=snapshot.run_id,
            report_contract_version="1",
            report_revision_id=snapshot.revision_id or None,
            report_input_generation=snapshot.input_generation,
            read_state=_report_read_state(
                has_substantive_content=has_substantive_content,
                readiness_complete=document.readiness.complete is True,
            ),
            is_substantive=has_substantive_content,
            visible_sections=visible_sections,
            diagnostics=diagnostics,
            markdown=render_markdown(document),
            html=render_html(document),
            document=document,
        )
