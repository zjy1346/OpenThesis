"""Deep application seams used by :mod:`openthesis.service`.

These components own one concern each while preserving the existing JSON-RPC
surface.  They deliberately accept the already-configured collaborators so
desktop, tests, and headless callers keep the same dependency seams.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Sequence

from .financial_recovery import FinancialRecoveryController
from .financial_recognition import FinancialRecognitionCoordinator
from .financial_ingestion import FinancialIngestionEngine
from .research import ResearchWorkflow


class DisclosureService:
    """Resolve companies and capture market disclosures through one seam."""

    def __init__(self, market_data: Any, market_snapshot: Any):
        self.market_data = market_data
        self.market_snapshot = market_snapshot

    def resolve(self, query: str, market: Any, *, limit: int = 15) -> Sequence[Any]:
        return self.market_data.resolve(query, market, limit=limit)

    def adapter_for(self, company: Any) -> Any:
        return self.market_data.adapter_for(company)

    def capture(self, company: Any, policy: Any, manual: Any = None) -> Any:
        if manual is None:
            return self.market_snapshot.capture(company, policy)
        return self.market_snapshot.capture(company, policy, manual)


class FinancialPipeline:
    """Own the financial engine, recognition, and recovery lifecycle."""

    def __init__(
        self,
        ingestion: FinancialIngestionEngine,
        recognition: FinancialRecognitionCoordinator,
        recovery: FinancialRecoveryController,
    ):
        self.ingestion = ingestion
        self.recognition = recognition
        self.recovery = recovery

    def ingestion_for(self, company: Any, filings: Sequence[Any]) -> Any:
        clone = getattr(self.ingestion, "with_compatibility_rules", None)
        if not callable(clone):
            return self.ingestion
        report_type = "annual" if any(
            str(item.fiscal_period or "FY").upper() == "FY" for item in filings
        ) else "quarterly"
        rules = self.recovery.compatibility_rules(company, report_type)
        return clone(rules)

    def recognition_for(self, company: Any, filings: Sequence[Any]) -> FinancialRecognitionCoordinator:
        engine = self.ingestion_for(company, filings)
        if engine is self.ingestion:
            return self.recognition
        return FinancialRecognitionCoordinator(engine)

    def discover(self, adapter: Any, company: Any, *, annual_limit: int, force: bool = False) -> Any:
        return self.recovery.discover(adapter, company, annual_limit=annual_limit, force=force)

    def compatibility_rules(self, company: Any, report_type: str) -> Any:
        return self.recovery.compatibility_rules(company, report_type)

    def compatibility_summary(self, market: Any, report_type: str) -> Any:
        return self.recovery.compatibility_summary(market, report_type)


class ResearchOrchestrator:
    """Construct research workflows without owning JSON-RPC policy."""

    def __init__(self, storage: Any, provider_factory: Callable[[Any], Any]):
        self.storage = storage
        self.provider_factory = provider_factory

    def create(
        self,
        selected_pack: Any,
        config: Any,
        *,
        cancel_check: Callable[[], bool] | None = None,
        report_language: str = "zh-CN",
        ui_language: str = "zh-CN",
        parallel_agents: bool = False,
        agent_progress: Callable[[str, str], None] | None = None,
    ) -> ResearchWorkflow:
        provider = self.provider_factory(config)
        set_cancel_check = getattr(provider, "set_cancel_check", None)
        if callable(set_cancel_check):
            set_cancel_check(cancel_check)
        return ResearchWorkflow(
            self.storage,
            selected_pack,
            provider,
            config,
            cancel_check=cancel_check,
            report_language=report_language,
            ui_language=ui_language,
            parallel_agents=parallel_agents,
            agent_progress=agent_progress,
        )


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
        return self._markdown_renderer(run_id, artifacts, **kwargs)

    def html(self, run_id: str, artifacts: Sequence[Any], **kwargs: Any) -> str:
        return self._html_renderer(run_id, artifacts, **kwargs)

    def document(self, run_id: str, artifacts: Sequence[Any], **kwargs: Any) -> Any | None:
        """Assemble the canonical immutable document for the standard renderers."""
        if (
            getattr(self._markdown_renderer, "__name__", "") == "render_research_run"
            and getattr(self._html_renderer, "__name__", "") == "render_research_html"
        ):
            from .report_document import assemble_report_document

            return assemble_report_document(
                run_id,
                artifacts,
                continuity=kwargs.get("continuity"),
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
            from .report_document import render_html, render_markdown

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
                "is_substantive": bool(body),
                "diagnostic_code": "LEGACY_RENDERER_UNVERIFIED",
            },) if body else ()
            return ReportReadResult(
                run_id=snapshot.run_id,
                report_contract_version="1",
                report_revision_id=snapshot.revision_id or None,
                report_input_generation=snapshot.input_generation,
                read_state=_report_read_state(
                    has_substantive_content=bool(visible), readiness_complete=False,
                ),
                is_substantive=bool(visible),
                visible_sections=visible,
                diagnostics=({"code": "LEGACY_RENDERER_UNVERIFIED", "location": "report"},),
                markdown=str(markdown),
                html=str(rendered_html),
            )

        from .report_document import render_html, render_markdown

        visible_sections = tuple(
            {
                "section_id": section.section_id,
                "title": section.title,
                "source_artifact_ids": list(section.source_artifact_ids),
                "verification_state": section.verification_state,
                "is_substantive": section.is_substantive,
                "diagnostic_code": section.diagnostic_code,
            }
            for section in document.sections
            if section.is_substantive
        )
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
                has_substantive_content=bool(visible_sections),
                readiness_complete=document.readiness.complete is True,
            ),
            is_substantive=bool(visible_sections),
            visible_sections=visible_sections,
            diagnostics=diagnostics,
            markdown=render_markdown(document),
            html=render_html(document),
            document=document,
        )
