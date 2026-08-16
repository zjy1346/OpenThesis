# Research report resilience v2

This proposal makes the research result a typed, failure-aware product rather
than a direct rendering of model JSON. It covers the Meituan-shaped failure in
which the final synthesis is malformed or only partially structured.

## Four layers and boundaries

1. **Evidence and deterministic layer**
   `FinancialIngestionEngine` remains the source of accepted facts and evidence.
   `ResearchContext` and `calculate_metrics` produce deterministic metrics. No
   model or renderer may add a missing fact or turn a missing value into zero.

2. **Analysis orchestration layer**
   `ResearchWorkflow.run` executes the fixed stage graph. Each provider result
   is normalized at the stage boundary and saved with a small response
   diagnostic (`finish_reason`, `content_length`, parse-error class). A final
   synthesis failure may make one bounded repair/re-synthesis call using only
   saved stage artifacts; earlier agents are never rerun. Authentication,
   authorization, rate-limit, and quota failures are terminal and are not
   retried. A second invalid synthesis leaves the run `PARTIAL` with a human
   readable reason.

3. **Typed report projection layer**
   `project_report_value` is the only path from stored protocol data to a user
   report. The non-technical schema is a whitelist of typed sections:
   executive summary, business model, main conclusions, counterarguments,
   invalidation conditions, leading indicators, and unresolved questions.
   Unknown keys and values are dropped and recorded as diagnostics. Evidence
   IDs, parser keys, severity/protocol enums, and raw response metadata remain
   technical-only. Conclusions are grouped into high/medium/low/unlabelled
   confidence buckets before Markdown or HTML rendering.

4. **Presentation and UI state layer**
   `render_research_run` and `render_research_html` consume only the projection;
   they do not inspect arbitrary model dictionaries. `AppService.get_report`
   exposes technical diagnostics only when requested. `ReportWorkspace` shows
   explicit partial/failure text, a clear retry action, and an unambiguous
   technical-details toggle. A failed new run never displays a previous
   company's report under the error banner.

## Public seams under test

- `ResearchWorkflow.run(...) -> ResearchRun`
- `ResearchWorkflow.retry_synthesis(...) -> ResearchRun`
- `AppService.get_report(run_id, include_technical=...)`
- `render_research_run(...)` and `render_research_html(...)`
- `ReportWorkspace` with a `partial` report and retry callback

Tests use fake providers at these seams. They do not inspect private parser
helpers, prompt contents, API keys, or the storage implementation to infer
user-visible behavior.

## Acceptance matrix

| Area | Acceptance condition |
| --- | --- |
| Malformed final JSON | One bounded repair call; no earlier agent reruns; second failure is `PARTIAL`. |
| Provider failures | Auth/rate-limit/quota errors do not retry; human-readable error is persisted. |
| Diagnostics | Finish reason, response length, and parse-error class are technical-only and contain no prompt/key. |
| Projection | Unknown keys/values and evidence IDs are absent from non-technical Markdown and HTML. |
| Conclusions | Main conclusions, counterarguments, and invalidation conditions are separate sections grouped by confidence. |
| Localization | Chinese has localized labels and no raw protocol keys; English has natural English labels and no Chinese UI labels. |
| UI | Partial status and retry/loading failure text are visible; technical toggle state is explicit. |
| Persistence | Retry updates only the final synthesis and thesis snapshot; previous stage artifacts remain intact. |

## Financial coverage and continuity

The financial projection uses an explicit concept allowlist and preserves
unit, source, and period evidence. In addition to revenue, net income,
operating cash flow, assets, liabilities, and equity, the first acceptance
slice covers operating income, capital expenditure, cash and equivalents,
reported ROE, gross margin, and operating margin. Missing concepts stay
missing; they are never converted to zero. Every annual group is validated
independently by filing/accession, period end, fiscal period, scope, and
reporting currency.

Continuity checks record both accepted and rejected years. A rejected year
cannot silently fall back to an older year: the user receives a readable
reason such as period mismatch, unit mismatch, balance-check failure, or
insufficient evidence, while technical mode may include the diagnostic path.
Listing currency and disclosed reporting currency remain separate fields. A
reporting currency is promoted only when exactly one complete consolidated
group is verified; an exchange default must not overwrite CNY, USD, or any
other disclosed currency.

## End-to-end and release boundary

The 15-company acceptance matrix covers A-share, Hong Kong, and US issuers.
Each row runs official cached evidence through ingestion, the quality gate,
deterministic metrics, and report rendering. It checks the latest annual
period, six core facts, unit/scope/provenance, no NaN or absurd ratios, and
the non-technical Chinese/English allowlist. Model paths separately cover a
fake deterministic provider, a configured real provider, timeout/cancellation,
single-stage retry, and the GUI technical-details toggle; temporary failures
must not overwrite a completed report.

GUI acceptance covers partial-report text, synthesis retry, loading errors,
technical-toggle state, history fallback, and Markdown/HTML parity. Portable
acceptance checks the unpacked executable and sidecar, no console launcher,
no user database/history/settings/credentials, and no personal absolute
paths. This phase produces only a local test build plus SHA-256 evidence: it
does not upload to GitHub or create a release/tag.

## Implemented vision fallback boundary (user controlled)

The default path remains local structured facts and native PDF text/table
parsing. `VisionFinancialSourceAdapter` is implemented but opt-in and enabled
only by an explicit session request plus consent. It accepts a session-scoped
API key and a cloud or custom OpenAI-compatible endpoint that supports image
input; no key is persisted, logged, or included in artifacts, and the product
makes no promise of a free quota. Only failed financial-table pages are
selected (at most 20 pages and 10 MB), with a final `approve_upload` seam that
receives provider/page/hash/size metadata but never page contents or signed
URLs. The adapter returns candidate facts, never verified facts: every
candidate must pass the same unit, currency, period, consolidated-scope,
balance-equation, magnitude, and provenance quality gates as other sources.
Failed or ambiguous candidates are quarantined. Diagnostics may retain
provider/model/page/hash and a safe error class, but never the key, raw prompt,
or unrestricted image payload.

## MinerU adapter policy (implemented, optional, never default)

MinerU has two explicit cloud adapters: the official lightweight Agent API
(`https://mineru.net/api/v1/agent`, no user token) and the precise token-backed
VLM API (`/api/v4/file-urls/batch`, table extraction enabled). Neither adapter
trains, downloads, or bundles a local model. Upload is opt-in and limited to
failed financial-table page batches of at most 10 MB and 20 pages; the caller
must show consent and may use the final upload-approval seam. Temporary
payloads are kept in memory and deleted with request scope. MinerU output is
candidate evidence and must pass the same unit, currency, period, scope,
balance-equation, magnitude, and provenance quality gate before acceptance.
Failed candidates are quarantined. Custom cloud vision remains supported under
the preceding session-key and no-prompt retention rules. Providers must treat
429 responses and polling timeouts as terminal safe errors and respect service
IP/rate limits; no retry loop uploads the same page indefinitely.
