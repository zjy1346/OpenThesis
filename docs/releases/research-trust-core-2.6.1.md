# OpenThesis 2.6.1 — Lossless Synthesis Recovery

- Workflow name: `research-trust-core`
- Target version: `2.6.1`
- Previous developer package: `2.6.0`
- Acceptance: `PASS`
- User Test: `PASS（用户已完成实机验证并批准发布，2026-09-11）`
- Upload Ready: `YES`
- Current phase: implementation, developer acceptance, user testing, and unsigned test packaging complete; ready for publication.

## 1. Requested changes

The independent QA run found release-blocking behaviour after the 2.6.0 package was produced. The user requires development to continue as 2.6.1 and specifically requires that comprehensive-report context handling must not reduce research quality.

The user permits reverting the 2.6.0 hierarchical compression path if zero quality loss cannot be guaranteed and asks for a better design if one exists. Research quality, factual integrity, evidence completeness, cross-domain reasoning, and explicit uncertainty remain higher priorities than latency, token cost, or always producing a polished final synthesis.

2.6.1 must address:

1. comprehensive synthesis must never become empty because a provider context budget is exceeded;
2. the application must not present byte-preserving partitioning as proof of unchanged reasoning quality;
3. recursive LLM summaries must not silently replace original verified claims/evidence;
4. section calls must not be given a full-report instruction that asks them to invent unavailable sections;
5. provider limits must be enforced using the exact request envelope rather than payload-only estimates;
6. the Windows portable startup failure reported by QA must be investigated with a repeatable signal and must remain a release regression gate;
7. a forced financial rebuild must reparse a valid, identity-matched local official PDF even when the network refresh is unavailable, while never reusing a revision/hash mismatch;
8. image-only audited statement pages must produce a dedicated, localized and actionable diagnosis instead of collapsing into generic data-quality or fetch failure;
9. scanned-page recovery must preserve the existing consent, page-count, byte-count, provenance and canonical compiler gates and must never treat an image-only page as `NOT_DISCLOSED`.
10. annual and interim growth must be able to use a verified comparative column carried by the current official filing when a separate prior-period filing is absent, without weakening period, scope, currency, unit, consolidation or provenance checks;
11. reverse DCF must enforce an explicit normalized-money contract at the valuation boundary so that table-unit fixture data, currency mismatches and accidental double scaling fail visibly instead of becoming plausible but incorrect valuations;
12. the displayed annual window must explain which fiscal years were actually disclosed and available as of the research date; it must never invent an unavailable later annual report merely to satisfy a calendar label;
13. conclusion and growth-opportunity counts remain evidence-driven, but every eligible verified input must be traceable through synthesis and rendering so that context loss cannot masquerade as "insufficient evidence".

## 2. Evidence and independent reproduction

### 2.1 QA evidence accepted as factual input

`docs/qa/2.6.0qa.md` reports:

- the 2.6.0 transport layer retained tested JSON values and evidence IDs across partitions;
- hierarchical section and merge calls can lose cross-domain attention and repeatedly summarize already summarized prose;
- section calls reuse `prompts/research-synthesizer.md`, whose instruction requires all report sections even when the call receives only one partial section;
- `_bounded_synthesis_merge` compares only serialized payload bytes with the provider limit;
- the later `_run_agent` check includes the real system prompt, role instructions, language contract, research context, and envelope, and can therefore reject a payload already accepted by the merge planner;
- `context_budget_exceeded` is excluded from the staged fallback path, allowing an empty projected report.

### 2.2 Tight red-capable feedback loop

A deterministic in-workspace harness was run against the current 2.6.0 code path. It constructed a one-entry merge payload close to a 36,000-byte provider input limit and compared the merge planner verdict with the exact `_agent_input_size` request size.

Observed output:

```text
{'merge_payload_bytes': 33130, 'actual_agent_input_bytes': 36327, 'limit_bytes': 36000}
AssertionError: BUG-260-AI-02: merge accepted a payload whose real provider request exceeds the context budget
```

This is a seconds-long, deterministic, agent-runnable red signal for the exact budget mismatch. It will be converted into a repository regression test before implementation.

### 2.3 Prompt-role mismatch confirmed

The section path calls `_run_agent("research-synthesizer-section", "prompts/research-synthesizer.md", ...)`. The referenced prompt begins by requiring a complete report including `executive_summary`, `business_model`, `financial_quality`, `balance_sheet`, and other global sections. A partial section therefore receives an instruction that is impossible to satisfy from its available evidence without empty placeholders or unsupported content.

### 2.4 Desktop runtime report is not currently reproduced

The exact 2.6.0 executable named in the QA report was run through `scripts/verify-desktop-runtime.ps1`. It remained alive for the full probe, started exactly one sidecar, completed the protocol checks, and returned:

```json
{"MainProcess":106288,"SidecarProcess":126804,"VisibleConsoleHosts":0,"IsolatedDataDirectory":"D:\\githubmax\\build\\portable-runtime-data\\ba2ddd4e1c4f4a98bf04c199a4e4ffc9"}
```

The QA startup failure is therefore not yet established as a deterministic production-code defect. 2.6.1 will add a repeated cold-start regression gate and capture panic/stderr/exit details before any runtime-code change is considered.

### 2.5 Mature-solution research

The external solution audit is recorded in `docs/research/lossless-context-synthesis-2.6.1.md`. Its central finding is that no existing prompt compressor, retrieval framework, compaction API, or long-context model provides a general guarantee of zero reasoning-quality loss for open-ended, cross-domain financial synthesis.

2.6.1 will therefore reuse mature components only within their proven boundaries:

- provider-native token counters or official model tokenizers for preflight budgeting;
- verified long-context model capabilities for a full-context-first route;
- provider prompt caching to reduce repeated-prefix latency and cost, never as a context-capacity extension;
- RFC 6901 JSON Pointer, JSON Schema, stable evidence IDs, and content hashes for reversible deduplication and auditability;
- RAG/GraphRAG or controlled read-only evidence tools only as retrieval and organization layers, never as the financial source of truth;
- deterministic reducers and a versioned golden corpus to make observable quality constraints release gates.

LLMLingua, LongLLMLingua, RAPTOR-style recursive summaries, and provider compaction are excluded from the authoritative synthesis path. They delete, reorder, or abstract source text and offer benchmark-level empirical results rather than a strict cross-language financial-integrity guarantee. This exclusion is especially important for Simplified Chinese, Traditional Chinese, and English parity.

### 2.6 Forced-rebuild local-cache failure reproduced

A temporary, repository-local harness exercised the real `_retry_market_financials(..., force=True)` call path with an identity-matched, hash-valid local official PDF and an offline download adapter. The required behaviour was that the existing file is reparsed through the current compiler without a network download. The current result was:

```text
FAIL: test_forced_rebuild_reparses_valid_local_pdf_when_network_download_fails
expected ingest_calls: ['acc-2025']
actual ingest_calls:   []
errors: ['acc-2025:download:ConnectionError']
download_calls: ['acc-2025', 'acc-2025']
```

This confirms `BUG-260-DATA-02`. `service.py` first restores the stored local path/hash onto the freshly discovered filing, but later sets `cached = []` whenever `force=True` and discovery is not stale. The valid local file consequently disappears from `parse_targets` after the network failure. The production parser/compiler is never reached.

The fix must redefine force as “reparse/revalidate all selected filings from trusted bytes” rather than “discard all local bytes and redownload everything”. Fresh network discovery metadata may still be requested, but downloading is required only when no identity- and hash-valid local document exists or when the discovered revision no longer matches.

### 2.7 Scanned-filing diagnosis separated from page recognition

The current code already contains a bounded audited-contents mapper and visual page selector. Two existing real-fixture checks were run against the 31.3 MB Zijin Mining annual report:

```text
test_zijin_real_scanned_statements_are_selected_from_audited_contents ... ok
test_zijin_real_scanned_statements_complete_through_same_vision_gate ... ok
```

The second check completed in 52.492 seconds. It proves that the current implementation can safely locate the audited image-only statement pages and can complete them through the same canonical compiler when an authorized vision adapter supplies valid candidates.

The newly reproduced defect is therefore not a need to rewrite the working page mapper. A dedicated `SCANNED_IMAGE_FILING_DETECTED` message was requested through the actual localization seam in Simplified Chinese, Traditional Chinese and English; all three fell back to the generic filing-fetch message. There is no stable scanned-image diagnosis in the service catalog, and the local-ingestion result does not distinguish:

1. ordinary missing/invalid fields;
2. image-only statements whose exact pages are safely located and recoverable by vision;
3. an image-only filing whose statement layout cannot be safely located.

2.6.1 will preserve the working TOC/page selection and add typed terminal diagnostics for the latter two states. `SCANNED_IMAGE_FILING_DETECTED` is actionable and may offer the already configured vision path; `SCANNED_IMAGE_LAYOUT_UNRESOLVED` fails closed and explains that no page range was uploaded because it could not be verified.

### 2.8 Second QA pass: reverse-DCF scale claim separated from a real boundary defect

The later QA update attributes widespread `outside_search_range` results to a universal 1,000,000-times production mismatch and proposes multiplying financial values by `unit_scale` at valuation time. That proposed root cause is **not accepted**.

The production PDF parser already normalizes a parsed table value into base currency units before creating a fact: `value = parsed_table_value * fact_multiplier`. `unit_scale` is then retained as provenance; it is not an instruction to scale the normalized fact a second time. Existing real-PDF tests assert full-unit values while retaining the source scale. Multiplying again in `reverse_dcf_analysis` would therefore corrupt valid production facts.

A seconds-long deterministic check against the current solver used the same CNY 9.5 billion equity value with two FCF inputs:

```text
QA fixture-like input:       FCF 380          -> outside_search_range
normalized production input: FCF 380,000,000  -> ok, implied growth 15.9291%
```

This proves that the QA simulation's naked `380` value is not evidence of a production parser defect. The untracked simulation fixture appears to have supplied a table-unit value while the market value was in base currency units.

There is nevertheless a genuine architectural weakness: the valuation API accepts untyped floats and cannot prove that market value and FCF share currency, scale and as-of semantics. 2.6.1 will close that boundary with normalized monetary inputs and provenance validation. Ambiguous or inconsistent inputs must return a typed `valuation_unit_mismatch`/`valuation_currency_mismatch` diagnosis; the solver must never guess a multiplier or silently double-scale a parsed fact.

### 2.9 Second QA pass: same-filing comparative columns are not canonical facts

`calculate_metrics` and `calculate_interim_metrics` currently compute growth only when separate facts exist for both periods. The filing selector can preserve a hidden prior-year report and the compiler can select a separate prior interim filing, but the PDF extraction path selects only the target-period value from a row and deduplicates to one fact per concept. A verified `2025 Q1` comparative column printed inside a `2026 Q1` report is therefore discarded as a computation input.

This is accepted as `BUG-260-DATA-03`, with a stronger fix than the QA proposal. A same-filing comparative value will become a first-class comparator fact with:

- the current filing identity, revision and content hash;
- source page, bounding box and column header provenance;
- comparison fiscal year/period, start/end dates and comparison basis;
- statement, consolidated scope, currency, unit scale and taxonomy mapping;
- explicit `usage_status=comparator`, so it cannot masquerade as a separately filed current-period fact.

For reported year-over-year analysis, a verified comparative column in the current report is preferred because it can contain the issuer's restatement of the prior period. When a separately filed prior report is also available, both are retained and compared; a difference becomes a visible restatement/conflict rather than being overwritten. Ambiguous headers, non-like-for-like periods, incompatible scope/currency/unit, and balance-sheet opening columns must fail closed.

### 2.10 Calendar-window and conclusion-count observations

The repository no longer contains the fixed `[-5:]` annual-window behavior described by QA. Selection is disclosure-driven: five latest eligible annual reports are displayed and an additional hidden comparator may be retained. If a 2025 annual report was not legally disclosed or locally obtainable as of the research date, 2020-2024 is correct and 2025 must not be fabricated. The UI still needs an explicit requested-versus-available period explanation and a precise missing/disclosure diagnosis.

QA also did not establish a hard-coded two-conclusion cap. The growth pipeline permits up to five evidence-valid opportunities, and QA's own simulated outputs contain more than two conclusions. 2.6.1 will not impose a minimum count or relax the evidence gate. Instead, regression instrumentation will compare eligible verified inputs, model output, post-validation artifacts and rendered items, distinguishing genuine lack of evidence from synthesis-budget loss, malformed output or rendering truncation.

## 3. Ranked root causes

1. **Proven — two budget authorities:** `_bounded_synthesis_merge` accepts payload bytes, while `_run_agent` rejects the larger real request envelope.
2. **Proven — context error bypasses safe fallback:** `context_budget_exceeded` is explicitly excluded from the fallback branch, allowing a structurally empty report.
3. **Confirmed design defect — prompt role mismatch:** partial inputs are asked to generate a full report.
4. **High confidence — recursive prose compression:** section outputs are model-generated prose and are recursively summarized again, so original verified details and cross-domain relationships can be diluted even when serialized input IDs are retained.
5. **Proven — forced rebuild discards valid local parse candidates:** the market-financial path empties `cached` under a non-stale forced rebuild, so a transient download failure produces no parse target even after local path/hash restoration.
6. **Proven — scanned filings collapse into generic diagnosis:** the audited TOC mapper and bounded vision path work on the real Zijin fixture, but no typed scan terminal state reaches localized user guidance.
7. **Unresolved runtime hypothesis:** the `tao` panic may depend on desktop state, injected software, startup timing, or environment rather than a deterministic application change; repeated cold-start evidence is required.
8. **Proven — comparative-column loss:** extraction retains only the target-period value per concept, so an issuer-provided prior-period comparison column cannot currently satisfy annual/interim growth when no separate comparator fact exists.
9. **Confirmed boundary defect, rejected scaling diagnosis:** reverse DCF accepts unitless floats and lacks currency/scale provenance validation, but production financial facts are already normalized. QA's universal million-times mismatch originates in an unnormalized simulation input and must not be "fixed" through double scaling.
10. **Not proven — conclusion-count truncation:** current evidence shows evidence validation and synthesis transport as possible causes, not a hard-coded two-item limit.

## 4. Proposed quality-preserving architecture using mature primitives

### 4.1 Remove lossy recursive report compression

The 2.6.0 `section -> LLM merge -> LLM merge -> final LLM` path will not remain the default overflow strategy. A model-generated summary must never become the sole surviving representation of an original verified claim or source reference.

The application will retain the complete canonical claim/evidence ledger and every verified stage artifact. Intermediate prose may be generated for display, but it cannot replace or delete the source-backed typed records used for later verification and rendering.

### 4.2 Full-context-first strict synthesis

1. Build the exact final provider request using the same prompt builder used for transmission.
2. Count the complete request through a provider/model adapter. Prefer an official Count Tokens endpoint where available; otherwise use the provider's official tokenizer and its documented message/tool envelope rules. Include system prompt, role prompt, language contract, research context, schema, tool definitions, output reserve, and repair reserve.
3. Select a configured model whose verified context window can contain the complete request and reserves. If it fits, perform one full-context synthesis call. This is the reference-quality path.
4. Before declaring overflow, apply deterministic, reversible normalization only: canonical JSON encoding, duplicate-record elimination by stable identity and content hash, and evidence text stored once in an `EvidenceManifest` with RFC 6901 pointers from claims. Unique claims, values, periods, scopes, caveats, counterarguments, scenarios, conflicts, and citations may not be removed.
5. Resolve the exact reference closure needed by the call, rebuild the real provider envelope, and recount it. If it fits, perform the same one-call synthesis.
6. Stable, non-sensitive schema and policy prefixes may use the provider's prompt cache. Cache hits reduce repeated processing cost/latency but do not reduce logical context occupancy, expand the context window, or establish output equivalence.

### 4.3 Strict overflow outcome instead of pretending quality parity

No generic transformation can mathematically guarantee that a finite-context model reasons identically after its input is partitioned. Therefore the default strict mode will not claim “zero quality decline” after partitioning.

If the complete normalized request still exceeds the configured model's verified context capacity:

- do not recursively summarize it;
- do not send an oversized request and wait for the provider to fail;
- preserve and render the complete verified stage report deterministically, including all sections, claims, evidence, risks, scenarios, and unresolved questions;
- mark only the cross-section final synthesis as `not_completed_context_capacity`, never the research data itself as missing;
- show the exact required/available capacity and offer retry with a configured larger-context model without rerunning completed research stages;
- never label the staged result as a completed synthesized report.

This preserves all research content and avoids false claims of equivalent reasoning quality. It is safer than reverting to an unbounded local state, because cloud providers still enforce real hard context limits.

RAG, GraphRAG, and MCP-style evidence reading may help a bounded call locate the complete claim/evidence bundle, but a retrieval miss must be reported as a miss. Unretrieved evidence cannot be treated as absent, and graph/community summaries cannot override `FinancialFactCompiler`, the canonical evidence ledger, or deterministic conflict resolution.

### 4.4 Optional cross-domain assisted synthesis is not the strict default

A future opt-in assisted mode may construct typed cross-domain relation packets from original claims rather than recursive summaries. Such a mode must:

- use section-specific instructions;
- carry the global thesis/risk/financial relationship index into every relevant call;
- emit typed claims linked directly to original evidence IDs;
- verify coverage and contradictions deterministically;
- remain visibly labelled as assisted hierarchical synthesis;
- never be used to satisfy the 2.6.1 zero-quality-loss acceptance gate unless an evaluation corpus demonstrates non-inferiority.

2.6.1 does not need this optional mode to ship safely.

Learned prompt compression and recursive abstractive summarization are not eligible implementations of this mode for 2.6.1. If ever offered later, they require an explicitly labelled, user-selected speed mode and must never feed authoritative facts or replace the strict report path.

### 4.5 Non-empty failure recovery

All synthesis failures, including context-capacity errors, malformed JSON, provider errors, cancellation boundaries, and repair failures, must preserve a canonical structured staged report. The fallback may use only previously verified stage material and deterministic financial metrics. Missing integration must be disclosed; no missing section may be filled with invented prose.

### 4.6 One budget authority

Introduce one request-planning boundary shared by planning and execution:

- exact prompt construction and provider-aware counting happen once through a provider/model adapter;
- every planned call carries its measured input size, provider capacity version, output reserve, and repair reserve;
- `_run_agent` verifies the plan immediately before transport but does not apply a contradictory second formula;
- custom providers without a trustworthy counter/tokenizer use conservative UTF-8 accounting, a configurable context override, and an explicit `estimated` diagnostic rather than being described as exact;
- provider rejection updates diagnostics but never erases completed research.

### 4.7 Reversible evidence transport

Introduce an immutable `EvidenceManifest` whose entries contain stable evidence identity, content hash, source filing/page location, period, scope, currency/unit, parser and taxonomy versions, raw and normalized values, validation state, and `supports`/`contradicts`/`derived_from` relationships. Repeated evidence text is transmitted once and referenced through stable IDs/JSON Pointers. Every partition carries or can deterministically load its complete reference closure; a broken reference fails closed and remains visible as `insufficient_evidence`.

Downloaded filings, parsed document structures, canonical facts, conflicts, and report claims use versioned content-addressed cache keys. A changed filing or parser/taxonomy/policy version creates a new cache object instead of silently reusing or overwriting old evidence.

### 4.8 Runtime cold-start gate

Before modifying Tauri/runtime code, run repeated isolated cold starts against the same package and capture exit code, stderr/panic text, child processes, elapsed lifetime, and relevant Windows state. A runtime code change is allowed only after a repository-owned regression loop reproduces the failure or a deterministic unsafe condition is identified.

### 4.9 Local-first forced rebuild

For every selected filing, discovery identity is reconciled with the stored filing before download planning:

- if accession/source identity, form, fiscal period, period end, revision and content hash are compatible and the local file hash verifies, the file is always a parse target, including `force=True` and offline operation;
- `force=True` invalidates derived parse/fact results, not verified official source bytes;
- a changed revision, source identity or failed content hash cannot reuse the old file and must remain download-required;
- a failed refresh never deletes or overwrites the last good file, facts or validation audit; replacement is per-filing and atomic only after the new parse passes the canonical compiler;
- automatic retry and complete rebuild share the same planner, differing only in selected nodes, so their cache rules cannot diverge again.

### 4.10 Typed scanned-statement recovery

Retain the existing audited-contents mapping and `_vision_failed_pages` safety limits. Add a model-free scan classification before the generic quality error is finalized:

- `SCANNED_IMAGE_FILING_DETECTED`: the audited statement range is independently anchored, relevant pages have no extractable text, and bounded vision recovery is possible;
- `SCANNED_IMAGE_LAYOUT_UNRESOLVED`: image-only content is present but the audited statement range cannot be established safely;
- ordinary structural/consistency failures retain their existing codes and are never mislabeled as scanning.

The typed state flows through recovery storage, report status, retry actions and all three UI languages. If vision is disabled or consent is absent, the user sees the exact next action. No page is uploaded without the existing per-page or current-research authorization; unresolved layouts trigger zero upload and zero model calls.

### 4.11 Canonical same-filing comparator lane

Extend statement extraction from "choose one target cell" to "classify every period-bearing value column". Current-period facts and comparator facts share the same parser and validation stages but have separate typed roles. The compiler may use a comparator only after exact period-duration, fiscal calendar, statement, entity/scope, currency and unit compatibility checks.

The growth engine receives a resolved `ComparisonPair` rather than searching unrelated naked facts. Resolution priority is:

1. verified same-filing comparative column for the report's stated YoY basis;
2. separately filed prior-period fact for independent cross-checking and historical-series continuity;
3. explicit gap when neither source passes validation.

Both sources remain in the ledger. A restated same-filing value never silently rewrites the historical filing; its relationship is recorded as `restates` or `conflicts_with` and rendered when material.

### 4.12 Typed valuation input contract

Introduce a normalized `MonetaryAmount`/valuation-input boundary carrying amount, ISO currency, normalized scale, as-of/filing date, source fact ID and provenance. Parser-produced facts enter with `normalized_scale=1`; raw table-unit fixtures or adapters must normalize before valuation or be rejected.

The reverse-DCF planner verifies:

- market value and FCF are finite and positive where required;
- both are normalized base-currency amounts;
- currencies match or an explicit dated FX conversion with source provenance exists;
- the selected FCF is a complete fiscal-year value available by the market as-of date;
- dimensional sanity checks detect likely 10^3/10^4/10^6/10^8 scale mistakes but never auto-correct them without provenance.

Wire-compatible numeric aliases may remain for rendering, but the authoritative calculation path cannot accept an unvalidated pair of naked floats.

## 5. QA recommendations corrected or rejected

- Merely subtracting one `fixed_overhead` value is insufficient. Each agent role and request envelope must be measured by the unified exact request planner.
- “Byte lossless” does not mean “reasoning quality unchanged”; 2.6.1 will use that phrase only for deterministic data retention.
- Removing all local limits is rejected because it transfers the same failure to the provider and can again produce an empty report.
- Unconditionally asking another LLM to repair a context failure is rejected; the repair call itself may not fit and may further summarize evidence.
- A fallback is acceptable only when it deterministically preserves verified staged research and clearly states that cross-section synthesis was not completed.
- The one-off desktop panic is evidence to investigate, not sufficient grounds for speculative runtime changes after the same binary passed a fresh runtime probe.
- Installing LLMLingua or another local compressor is rejected for the strict path: it introduces a model-dependent, lossy transformation and is not a substitute for exact budgeting or reversible evidence transport.
- Prompt caching is accepted only as a cost/latency optimization; cached tokens still occupy the provider context and cannot make an oversized request fit.
- GraphRAG/RAG is accepted only as a bounded retrieval index with measurable recall and visible misses, not as a replacement for canonical financial data or complete evidence coverage.
- QA's local-cache root cause is accepted, but “force” must not blindly prefer either cache or network: it must reuse only identity/hash-valid official bytes and still reparse/revalidate them under the current parser, taxonomy and compiler versions.
- QA's proposal to add scan guidance is accepted with a stronger typed boundary. The existing real-fixture page mapper is retained; image-only recoverable pages and unresolvable layouts receive different codes and actions.
- Relaxing financial quality gates for scanned reports is rejected. Vision outputs remain candidates until the same deterministic compiler accepts them.
- QA's instruction to multiply reverse-DCF cash flow by retained `unit_scale` is rejected. Parser facts are already normalized, so this would double-scale real filings. The accepted change is a typed valuation boundary that rejects unnormalized or cross-currency inputs.
- QA's comparative-column diagnosis is accepted. Its proposed fallback is strengthened into a canonical comparator lane with column-level provenance, restatement/conflict retention and strict like-for-like validation.
- Forcing calendar year 2025 into a report before a valid 2025 annual filing exists is rejected. The application will explain disclosure availability and keep interim periods separate from full fiscal years.
- A minimum number of conclusions or opportunities is rejected because it incentivizes unsupported content. The acceptance gate is lossless propagation of eligible verified inputs, not a quota.

## 6. Relevant scope

Implemented scope:

- `src/openthesis/research.py`;
- `src/openthesis/financial_ingestion.py` and `financial_compiler.py` for typed scanned-page diagnosis without changing canonical acceptance;
- a new deep request-budget/synthesis-plan module if final design review confirms the seam;
- `src/openthesis/report_projection.py`, `reporting.py`, and `report_html.py` only where the strict staged result and capacity diagnostic must render;
- `src/openthesis/service.py` for retry-final-synthesis-without-rerunning-stages, local-first forced rebuild planning, typed recovery diagnostics, and state exposure;
- desktop protocol/types/UI only if required to expose the larger-context retry and explicit synthesis state;
- research workflow, projection, service, rendering, runtime-probe, and version regression tests;
- 2.6.1 version metadata and packaging files after developer acceptance.
- `src/openthesis/financials.py`, valuation call sites and protocol types for typed monetary inputs and dimensional diagnostics;
- PDF/HTML/XBRL ingestion, canonical compiler and filing selection for same-filing comparator facts and restatement relationships;
- deterministic comparator, valuation-scale, currency, annual-window and artifact-lineage regression tests.

## 7. Acceptance checklist

### A. Exact capacity planning

- [x] The current 33,130-byte payload / 36,327-byte real request / 36,000-byte limit reproduction is a committed regression test and passes.
- [x] Planner and transport use one exact request-envelope byte calculation for every synthesis role.
- [x] Prompt, language, context, schema, output reserve, and repair reserve are included before a call is admitted.
- [x] Provider-declared byte limits are exact; where no official counter is exposed, the conservative UTF-8 upper bound is visibly distinguished from exact token counting.
- [x] No provider call exceeds its declared application budget.

### B. Zero silent quality loss

- [x] A fitting report uses one full-context synthesis call after deterministic lossless normalization.
- [x] Recursive LLM prose compression is not used by the default strict path.
- [x] Every unique verified claim, calculation, evidence ID, period, scope, risk, scenario, caveat, and unresolved question remains in the canonical report source.
- [x] Deduplication removes only records proven identical by stable identity/content, not merely similar prose.
- [x] Every deduplicated claim reference resolves through the immutable evidence manifest, and the original bytes/typed value remain reconstructable.
- [x] No test or UI copy equates byte retention with unchanged reasoning quality.
- [x] Learned prompt compression, recursive abstractive summaries, and provider compaction are absent from the authoritative synthesis path.

### C. Overflow and fallback behaviour

- [x] A truly oversized normalized report produces a complete structured staged report, never an empty report.
- [x] The result explicitly distinguishes `research_complete` from `cross_section_synthesis_not_completed_context_capacity`.
- [x] The UI reports required versus available capacity and permits final-synthesis retry with a larger configured model without rerunning prior research stages.
- [x] The fallback uses only verified stage data and deterministic metrics; it cannot invent missing sections.
- [x] Authentication, quota, malformed output, context, repair, and provider failures all preserve completed research artifacts.

### D. Prompt correctness

- [x] No partial section call uses the complete-report prompt.
- [x] No assisted-partition model route remains in the authoritative production path.
- [x] Cross-domain conflict/opportunity relationships are tested and cannot disappear silently.

### E. Runtime and regression

- [x] Repeated isolated cold-start probes are repository-owned and capture actionable failure diagnostics.
- [x] The packaged 2.6.1 desktop app starts reliably, launches exactly one sidecar, and exposes no console window.
- [x] Existing disclosure identity, financial ingestion, fact validation, research gatekeeping, three-language report, frontend, and Rust suites do not regress.
- [x] Version metadata is consistently `2.6.1`.
- [x] One unsigned test package is produced after developer acceptance without a repeated privacy scan.

### F. Retrieval, caching, and quality evaluation

- [x] Prompt caching is absent from the authoritative 2.6.1 path and therefore cannot change request admission or evidence coverage.
- [x] RAG/GraphRAG is absent from the authoritative 2.6.1 path; missing evidence remains explicit and cannot become an unsupported negative conclusion.
- [x] The versioned repository acceptance corpus covers A-share, Hong Kong, US, Simplified Chinese, Traditional Chinese, and English paths.
- [x] The strict path meets disclosed-required-field, numeric exactness, conflict-retention, section-non-emptiness, and evidence-traceability assertions on the fixed corpus.
- [x] The only overflow path is deterministic and zero-call; tests compare retained claims, values, evidence and section coverage directly rather than hiding regressions in an average score.

### G. Forced rebuild and cache integrity

- [x] The reproduced identity/hash-valid local PDF plus offline network case reparses locally and performs zero duplicate download attempts.
- [x] `force=True` invalidates derived parse/fact caches but does not discard trusted official source bytes.
- [x] A revision, source identity, period identity or content-hash mismatch never reuses the stale local file.
- [x] A failed rebuild preserves the previous good file, facts, validations and report artifact until a per-filing replacement passes.
- [x] Market and US filing paths follow one documented cache/reparse policy, with market-specific discovery adapters tested separately.

### H. Scanned financial statements

- [x] The real Zijin audited fixture continues to map only the consolidated statement pages and completes through the authorized vision/compiler gate.
- [x] Safely located image-only statements emit `SCANNED_IMAGE_FILING_DETECTED` and an actionable next step in Simplified Chinese, Traditional Chinese and English.
- [x] Image-only layouts that cannot be anchored emit `SCANNED_IMAGE_LAYOUT_UNRESOLVED`, upload nothing and call no model.
- [x] Scan detection does not classify blank separators, intentionally blank pages, ordinary malformed tables or genuinely undisclosed fields as recoverable statements.
- [x] Existing 20-page/10 MiB, page fingerprint, consent, cancellation, timeout, provider and canonical validation controls do not regress.

### I. Same-filing comparative values

- [x] A single verified annual filing containing current and prior-year columns produces a current fact plus a provenance-complete comparator fact and computes the stated YoY growth without requiring a second PDF.
- [x] Q1, H1 and Q3 cumulative reports use only an exact prior-year same-period comparator with compatible start/end duration and fiscal calendar.
- [x] Current-filing restated comparatives and separately filed historical values are both retained; differences create a typed restatement/conflict and never silently overwrite history.
- [x] Page, bbox, column header, filing hash/revision, period, scope, currency and unit provenance survive through the report evidence chain.
- [x] Ambiguous columns, prior-quarter rather than prior-year columns, balance-sheet opening values, parent-only/consolidated mismatches and currency/unit mismatches do not produce growth.
- [x] The oldest displayed annual year may show growth from its verified same-filing comparative column while the hidden comparator remains outside the visible five-year table.

### J. Reverse-DCF dimensional integrity

- [x] The deterministic CNY 9.5 billion / CNY 380 million case returns `ok` with an implied growth near 15.9291%.
- [x] The same market value paired with an unnormalized table-unit `380` value fails with a typed unit/normalization diagnosis before solving.
- [x] Real PDF parser fixtures continue to expose full base-currency values while retaining original `unit_scale` as provenance; no production fact is scaled twice.
- [x] Cross-currency valuation is rejected unless a dated, sourced FX conversion explicitly normalizes both values.
- [x] Likely powers-of-ten mismatches are visible and actionable but are never auto-corrected from heuristics alone.
- [x] Complete-FY and market-as-of eligibility rules remain enforced.

### K. Disclosure window and artifact lineage

- [x] The report states the requested annual range, the latest legally disclosed/available fiscal year and every missing or rejected year with a reason.
- [x] Interim data never substitutes for a missing annual report and no unavailable future annual period is fabricated.
- [x] Six valid growth opportunities deterministically cap at the documented maximum of five with an explicit diagnostic; there is no hidden cap of two.
- [x] Five evidence-valid opportunities returned by a deterministic provider survive validation and rendering as five items.
- [x] Eligible-input, model-output, validated-artifact and rendered-item counts are traceable per section, so compression loss and evidence rejection are distinguishable.
- [x] Reference-only or invalid-evidence conclusions remain rejected even when this produces fewer visible items.

## 8. Approved implementation decisions

Already authorized by the user:

- target version is exactly `2.6.1`;
- 2.6.0 QA failures must be handled through the existing development workflow;
- comprehensive-report quality must not be silently reduced;
- returning to the previous no-application-limit behaviour is allowed only if no safer zero-loss design exists;
- application-code modification began only after the user's explicit approval.

## 9. Approved implementation decisions

The user approved the section 4 strict architecture instead of reverting to unbounded provider calls:

- full-context one-call synthesis whenever it fits;
- official provider-aware counting and verified long-context routing;
- deterministic reversible evidence normalization using stable IDs, hashes, JSON Pointer/Schema, and content-addressed storage;
- no recursive LLM summary chain in the default path;
- prompt caching for repeated cost/latency only, never capacity;
- GraphRAG/RAG/controlled evidence tools only as auditable retrieval helpers;
- complete staged report plus larger-context retry when the full request cannot fit;
- honest incomplete-synthesis status rather than an empty or falsely equivalent final report;
- local-first forced rebuild that reparses trusted bytes offline without bypassing revision/hash checks;
- typed scanned-statement recovery that preserves the existing working page mapper and compiler gate while giving users an exact next action.
- first-class same-filing comparator facts with column-level provenance, strict like-for-like pairing and explicit restatement/conflict retention;
- typed normalized monetary inputs at the valuation boundary, rejecting scale/currency ambiguity instead of applying heuristic multipliers;
- disclosure-aware fiscal-year messaging and end-to-end artifact lineage counters, without fabricating periods or enforcing unsupported conclusion quotas.

## 10. Acceptance and publication record

- Completed changes: strict one-call-or-deterministic-fallback synthesis; exact full-envelope byte measurement with visibly conservative fallback counting; complete staged-report preservation and larger-context synthesis retry; local-first forced rebuild; typed scanned-statement diagnosis and three-language guidance; same-filing comparative facts with hidden comparator display isolation; normalized reverse-DCF monetary boundaries; disclosure-window and artifact-lineage reporting; Meituan English statement-title adaptation; repeated desktop cold-start verification.
- Files intended for publication: the current approved 2.6.1 application, desktop, tests, scripts, research note, and this acceptance record. Git selection remains part of the separate upload workflow.
- Release artifact path: `D:\githubmax\installer-output\OpenThesis-2.6.1-windows-x64-portable.zip`.
- Release artifact hash (SHA-256): `1ACDF70CC0E5652A8AFF383762464329E3774DB2446091334522C0F4B31929CE`.
- Release artifact size: `50,630,722` bytes.
- Developer acceptance result: `PASS`.
- Independent user test result: `PASS`.
- GitHub upload is handled by the separate upload workflow.

### 10.1 Developer acceptance results

- Full Python discovery: `600/600` passed (`scripts/test.ps1`).
- Financial-ingestion real-fixture suite: `115/115` passed, including Meituan, Baiyin Nonferrous and the authorized Zijin scanned-statement fixture.
- Strict research synthesis workflow: `43/43` passed, including zero-call context overflow and complete staged fallback.
- Desktop Vitest: `77/77` passed across 13 files; TypeScript build and Vite production build passed.
- Rust: `33/33` passed; main/doc targets with zero tests also passed.
- Portable verification: required entries present, Windows GUI subsystem, unsigned-test signature state confirmed.
- Runtime verification: `3/3` isolated cold starts passed; each retained exactly one sidecar, passed `system.hello` and model-gateway protocol probes, and exposed no visible console host.
- Privacy verification was intentionally not repeated for this unchanged build input, following the approved minimal-call packaging rule.

### 10.2 Acceptance notes

- Provider-exposed explicit byte budgets are treated as exact. When a provider exposes no official tokenizer/count endpoint, OpenThesis uses a conservative UTF-8 byte upper bound and labels it as an estimate in the three-language report; it is never described as exact token counting.
- Prompt cache and RAG/GraphRAG are not used by the authoritative synthesis path in 2.6.1, so they cannot alter request admission or evidence coverage.
- The versioned repository tests are the fixed acceptance corpus for A-share, Hong Kong and US paths and for Simplified Chinese, Traditional Chinese and English rendering. No claim of universal 99.9% recognition is made beyond this measured corpus.
- The failed initial package command was caused by two defects in the new runtime verifier (nullable PowerShell exit code handling and an omitted `--model-gateway` argument). The already-built artifact was not rebuilt; after repairing the verifier, portable verification and three isolated runtime attempts passed independently.
