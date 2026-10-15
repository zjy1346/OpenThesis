from __future__ import annotations

import html
from urllib.parse import quote
from dataclasses import dataclass
from typing import Any, Iterable

from .report_content import read_trusted_snapshots, minimum_presentation, unresolved_content_invalidations


@dataclass(frozen=True, slots=True)
class SafeReport:
    markdown: str
    html: str
    nodes: tuple[dict[str, Any], ...] = ()


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
        is_hant = language.lower() in {"zh-tw", "zh-hk", "zh-hant"}
        def localized(english, simplified, traditional):
            return english if is_english else traditional if is_hant else simplified
        title = company_name + " — " + localized("Research recovery report", "研究恢复报告", "研究恢復報告")
        stage_title = localized("Saved research outputs", "已保存的研究成果", "已儲存的研究成果")
        next_title = localized("Next action", "下一步", "下一步")
        latest = continuity.get("latest") if isinstance(continuity, dict) else None
        latest = latest if isinstance(latest, dict) else {}
        message = str(latest.get("message") or (
            localized("The complete report renderer is unavailable; verified stage outputs remain saved.",
                "完整报告暂时无法生成；已验证的阶段成果仍已保存。", "完整報告暫時無法生成；已驗證的階段成果仍已儲存。")
        ))
        rows = []
        artifacts = list(artifacts)
        nodes = read_trusted_snapshots(artifacts, run_id=run_id)
        if unresolved_content_invalidations(artifacts, run_id=run_id):
            message += " " + localized(
                "New source evidence corrected financial facts. Related previous conclusions were withdrawn; "
                "independent verified content remains available.",
                "新来源证据更正了财务事实；相关旧结论已撤回，无关的可信内容仍可阅读。",
                "新來源證據更正了財務事實；相關舊結論已撤回，無關的可信內容仍可閱讀。")
        seen: set[tuple[str, str]] = set()
        for artifact in artifacts:
            key = (str(artifact.get("artifact_type", "")), str(artifact.get("title", "")))
            if not key[0] or key in seen:
                continue
            seen.add(key)
            rows.append(f"- {key[1] or key[0]} (`{key[0]}`)")
        if not rows:
            rows.append("- " + ("No completed stage output yet." if is_english else "尚无已完成阶段成果。"))
        action = localized("Review the reported stage, then resume only the affected step.",
            "请检查所示阶段，并仅恢复受影响的步骤。", "請檢查所示階段，並僅恢復受影響的步驟。")
        diagnostic = str(latest.get("error_code") or renderer_error or "REPORT_RENDER_FALLBACK")
        retained_title = localized("Retained verified content", "保留的可信正文", "保留的可信正文")
        def context_body(node):
            context = " · ".join(str(item["text"]) for item in node.get("context", ()))
            marker = localized("Labelled assumption", "已标注假设", "已標註假設") if node.get("role") == "assumption" else ""
            return "\n\n".join(item for item in (context, marker) if item)

        def minimum_markdown(node):
            minimum = minimum_presentation(node, language)
            if minimum["kind"] == "text":
                return html.escape(minimum["text"])
            def cell(value):
                return html.escape(value).replace("|", "&#124;").replace("\n", "<br>")
            header, *body = minimum["rows"]
            return "\n".join("| " + " | ".join(cell(value) for value in row) + " |"
                for row in (header, ("---", "---"), *body))

        def minimum_html(node):
            minimum = minimum_presentation(node, language)
            if minimum["kind"] == "text":
                return '<p>' + html.escape(minimum["text"]) + '</p>'
            header, *body = minimum["rows"]
            return '<table><thead><tr>' + ''.join('<th>' + html.escape(value) + '</th>' for value in header) + \
                '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + html.escape(value) + '</td>'
                    for value in row) + '</tr>' for row in body) + '</tbody></table>'

        def source_markdown(node):
            linked = {item["evidence_id"]: item for item in node.get("sources", ())}
            values = []
            for reference in node.get("evidence_ids", ()):
                item = linked.get(reference)
                label = html.escape(reference.replace("[", "").replace("]", "").replace("`", ""))
                values.append(f"[{label}](<{quote(item['source_url'], safe=':/?=&%#@+;,') }>)" if item else f"`{label}`")
            return ", ".join(values)

        retained_markdown = "\n\n".join(html.escape(context_body(node)) + "\n\n" + minimum_markdown(node)
            + "\n\n" + source_markdown(node) for node in nodes)
        markdown = "\n\n".join((
            f"# {title}",
            message,
            f"## {retained_title}\n\n{retained_markdown}" if nodes else "",
            f"## {stage_title}\n\n" + "\n".join(rows),
            f"## {next_title}\n\n{action}",
            f"`run_id: {run_id}`  \n`status: {status}`  \n`diagnostic: {diagnostic}`",
        ))
        body = "".join(f"<li>{html.escape(row[2:])}</li>" for row in rows)
        rendered = (
            "<article class=\"safe-report\">"
            f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
            + (f"<h2>{html.escape(retained_title)}</h2>" + "".join(
                f'<section data-node-id="{html.escape(node["node_id"], quote=True)}">'
                f'<p>{html.escape(context_body(node))}</p>' + minimum_html(node) + '<p>' + ", ".join(
                    f'<a href="{html.escape(source["source_url"], quote=True)}">{html.escape(source["evidence_id"])}</a>'
                    for source in node.get("sources", ())) + '</p><p>' +
                html.escape(", ".join(node.get("evidence_ids", ()))) + '</p></section>'
                for node in nodes) if nodes else "") +
            f"<h2>{html.escape(stage_title)}</h2><ul>{body}</ul>"
            f"<h2>{html.escape(next_title)}</h2><p>{html.escape(action)}</p>"
            f"<code>run_id: {html.escape(run_id)} · status: {html.escape(status)} · diagnostic: {html.escape(diagnostic)}</code>"
            "</article>"
        )
        return SafeReport(markdown, rendered, nodes)
