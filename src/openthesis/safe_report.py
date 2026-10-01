from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class SafeReport:
    markdown: str
    html: str


class SafeReportAssembler:
    """Last-resort renderer depending only on durable run/ledger records."""

    def assemble(
        self,
        *,
        run_id: str,
        company_name: str,
        status: str,
        artifacts: Iterable[dict[str, Any]],
        continuity: dict[str, Any],
        language: str,
        renderer_error: str = "",
    ) -> SafeReport:
        is_english = language.lower().startswith("en")
        title = f"{company_name} — Research recovery report" if is_english else f"{company_name} — 研究恢复报告"
        stage_title = "Saved research outputs" if is_english else "已保存的研究成果"
        next_title = "Next action" if is_english else "下一步"
        latest = continuity.get("latest") if isinstance(continuity, dict) else None
        latest = latest if isinstance(latest, dict) else {}
        message = str(latest.get("message") or (
            "The complete report renderer is unavailable; verified stage outputs remain saved."
            if is_english else "完整报告暂时无法生成；已验证的阶段成果仍已保存。"
        ))
        rows = []
        seen: set[tuple[str, str]] = set()
        for artifact in artifacts:
            key = (str(artifact.get("artifact_type", "")), str(artifact.get("title", "")))
            if not key[0] or key in seen:
                continue
            seen.add(key)
            rows.append(f"- {key[1] or key[0]} (`{key[0]}`)")
        if not rows:
            rows.append("- " + ("No completed stage output yet." if is_english else "尚无已完成阶段成果。"))
        action = (
            "Review the reported stage, then resume only the affected step."
            if is_english else "请检查所示阶段，并仅恢复受影响的步骤。"
        )
        diagnostic = str(latest.get("error_code") or renderer_error or "REPORT_RENDER_FALLBACK")
        markdown = "\n\n".join((
            f"# {title}",
            message,
            f"## {stage_title}\n\n" + "\n".join(rows),
            f"## {next_title}\n\n{action}",
            f"`run_id: {run_id}`  \n`status: {status}`  \n`diagnostic: {diagnostic}`",
        ))
        body = "".join(f"<li>{html.escape(row[2:])}</li>" for row in rows)
        rendered = (
            "<article class=\"safe-report\">"
            f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
            f"<h2>{html.escape(stage_title)}</h2><ul>{body}</ul>"
            f"<h2>{html.escape(next_title)}</h2><p>{html.escape(action)}</p>"
            f"<code>run_id: {html.escape(run_id)} · status: {html.escape(status)} · diagnostic: {html.escape(diagnostic)}</code>"
            "</article>"
        )
        return SafeReport(markdown, rendered)
