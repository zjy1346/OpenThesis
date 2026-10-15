"""Explicitly authorized public-page acceptance, not general upload tooling.

Only the frozen BYD FY2025 page 126 may be submitted, once as native PDF and
once as an image-only PDF. The unit declaration on page 140 stays local.
Neither credentials nor application user data are opened. Logs omit signed
upload/download URLs and arbitrary transport exception text.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

from pypdf import PdfReader, PdfWriter
from PIL import Image

from openthesis.domain import Company, FilingDocument, ResearchRun, RunStatus
from openthesis.financial_cloud_proof import CloudUnitContext
from openthesis.financial_compiler import FinancialFactCompiler
from openthesis.financial_cloud_recovery import DurableCloudVisionAdapter
from openthesis.financial_sources.document_units import discover_document_units
from openthesis.resilience.contracts import RecoveryLimits
from openthesis.storage import Storage
from openthesis.vision_financials import (
    MineruFlashAdapter, UrllibVisionTransport, VisionAdapterError,
    VisionFallbackConfig, VisionPageRequest, VisionExtractionResult, default_pdf_to_png,
    MINERU_UPLOAD_PROTOCOL_VERSION, vision_task_key,
)

SOURCE_HASH = "710d8b2e08e37ba521f1c4071d2c3e2369d3cadabc21a45073215d9f72138b0b"
SOURCE_URL = "https://static.cninfo.com.cn/finalpage/2026-03-28/1225045351.PDF"
EXPECTED_CURRENT = {
    "revenue": 803_964_958_000, "operating_income": 40_184_709_000,
    "profit_before_tax": 39_753_049_000, "net_income": 33_760_758_000,
    "parent_net_income": 32_619_022_000, "minority_interest_profit": 1_141_736_000,
}
EXPECTED_COMPARATIVE = {
    "revenue": 777_102_455_000, "operating_income": 50_486_047_000,
    "profit_before_tax": 49_680_677_000, "net_income": 41_587_940_000,
    "parent_net_income": 40_254_346_000, "minority_interest_profit": 1_333_594_000,
}


class BoundedAcceptanceTransport:
    def __init__(self, previous_calls=(), *, query_only=False, offline=False):
        self.transport = UrllibVisionTransport()
        self.calls = list(previous_calls)
        self.submissions = {call["variant"] for call in self.calls if call["method"] == "POST"}
        self.query_only = query_only
        self.offline = offline
        self.variant = ""

    def request(self, method, url, **kwargs):
        if self.offline:
            raise VisionAdapterError("ACCEPTANCE_OFFLINE_REPARSE_NO_HTTP")
        if self.query_only and method != "GET":
            raise VisionAdapterError("ACCEPTANCE_QUERY_ONLY")
        if len(self.calls) >= 16:
            raise VisionAdapterError("ACCEPTANCE_HTTP_LIMIT")
        if method == "POST":
            if self.variant in self.submissions:
                raise VisionAdapterError("ACCEPTANCE_SUBMISSION_LIMIT")
            self.submissions.add(self.variant)
        record = {"variant": self.variant, "method": method, "host": urlsplit(url).hostname}
        self.calls.append(record)
        started = time.monotonic()
        try:
            response = self.transport.request(method, url, **kwargs)
            record["status"] = response.status
            return response
        except VisionAdapterError as error:
            record["error_code"] = error.code
            raise
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--authorize-two-public-page-submissions", action="store_true")
    parser.add_argument("--resume-existing-tasks", action="store_true",
        help="Query only the two saved tasks, sharing the original 16-request limit")
    parser.add_argument("--retain-missing-source", action="store_true",
        help="Read back a missing raw source once, without a new submission")
    parser.add_argument("--reparse-retained-source", action="store_true",
        help="Reparse both saved raw sources locally; all HTTP is forbidden")
    args = parser.parse_args()
    if args.reparse_retained_source and not args.resume_existing_tasks:
        raise SystemExit("ACCEPTANCE_OFFLINE_REPARSE_REQUIRES_EXISTING_TASKS")
    if not args.authorize_two_public_page_submissions:
        raise SystemExit("Explicit two-page submission authorization is required")
    raw = args.source.read_bytes()
    if sha256(raw).hexdigest() != SOURCE_HASH:
        raise SystemExit("PUBLIC_SOURCE_HASH_MISMATCH")
    reader = PdfReader(BytesIO(raw))
    writer = PdfWriter()
    writer.add_page(reader.pages[125])
    output = BytesIO()
    writer.write(output)
    native = output.getvalue()
    png = default_pdf_to_png(native)
    image_pdf = BytesIO()
    with Image.open(BytesIO(png)) as image:
        image.convert("RGB").save(image_pdf, format="PDF", resolution=144)
    scanned = image_pdf.getvalue()
    if PdfReader(BytesIO(scanned)).pages[0].extract_text():
        raise SystemExit("SCAN_STILL_HAS_TEXT_LAYER")
    declaration = discover_document_units((reader.pages[139].extract_text() or "",)).for_scope("consolidated")
    if declaration is None or declaration.currency != "CNY" or declaration.scale != "1000":
        raise SystemExit("PUBLIC_UNIT_DECLARATION_NOT_PROVEN")
    contexts = (CloudUnitContext(140, declaration.excerpt, SOURCE_HASH),)
    company = Company("CN_A:SZSE:002594.SZ", "002594.SZ", "比亚迪", "SZSE", market="CN_A", reporting_currency="CNY")
    filing = FilingDocument("cninfo:1225045351", company.security_id, "1225045351", "ANNUAL_REPORT", "FY",
        "2025-12-31", "2026-03-27", "2025年年度报告", SOURCE_URL, content_hash=SOURCE_HASH)
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"schema": "openthesis.free-cloud-acceptance.v1", "public_document_hash": SOURCE_HASH,
        "uploaded_original_page": 126, "local_only_unit_page": 140,
        "maximum_submissions": 2, "maximum_http_calls": 16, "results": []}
    previous = None
    if args.resume_existing_tasks:
        initial = json.loads((args.output / "result.json").read_text(encoding="utf-8"))
        resume_path = args.output / "query-only-result.json"
        previous = json.loads(resume_path.read_text(encoding="utf-8")) if resume_path.exists() else initial
        original_results = previous.get("initial_attempt_results", previous["results"])
        if (previous.get("public_document_hash") != SOURCE_HASH
                or previous.get("uploaded_original_page") != 126
                or {item["variant"] for item in original_results} != {"native", "image-only"}):
            raise SystemExit("ACCEPTANCE_PRIOR_IDENTITY_MISMATCH")
        report["initial_attempt_results"] = original_results
        report["mode"] = "query-only-readback-and-independent-financial-validation"
        report["results"] = list(previous["results"]) if previous.get("mode") else []
        if args.reparse_retained_source:
            report["mode"] = "offline-retained-source-reparse"
            report["readback_results"] = list(report["results"])
            report["results"] = []
        expected_scan = next(item["page_hash"] for item in original_results if item["variant"] == "image-only")
        if sha256(scanned).hexdigest() != expected_scan:
            # Pillow embeds wall-clock PDF creation/modification dates. Recover
            # only identical bytes from the recorded creation-time window; a
            # similar rendering never substitutes for the uploaded hash.
            stamp = int(args.output.stat().st_ctime)
            recovered = None
            with Image.open(BytesIO(png)) as image:
                rgb = image.convert("RGB")
                for second in range(stamp - 10, stamp + 3):
                    for modification in (second, second + 1):
                        candidate = BytesIO()
                        rgb.save(candidate, format="PDF", resolution=144,
                            creationDate=time.gmtime(second), modDate=time.gmtime(modification))
                        if sha256(candidate.getvalue()).hexdigest() == expected_scan:
                            recovered = candidate.getvalue()
                            break
                    if recovered is not None:
                        break
            if recovered is None:
                raise SystemExit("ACCEPTANCE_UPLOADED_SCAN_BYTES_NOT_RECOVERABLE")
            scanned = recovered
    transport = BoundedAcceptanceTransport(
        previous["http_calls"] if previous else (), query_only=args.resume_existing_tasks,
        offline=args.reparse_retained_source)
    # A fresh test-only database for each variant; never the desktop database.
    for variant, data in (("native", native), ("image-only", scanned)):
        transport.variant = variant
        page = VisionPageRequest(126, data, SOURCE_URL, filing.primary_document, unit_contexts=contexts)
        config = VisionFallbackConfig(enabled=True, consent=True, provider="mineru_flash",
            configuration_version=1, require_page_approval=True, approval_mode="approve_current_research",
            max_pages=1, timeout_seconds=60,
            approve_upload=lambda plan, expected=page: plan.get("filing_hash") == SOURCE_HASH
                and tuple(plan.get("pages", ())) == (126,)
                and tuple(plan.get("document_hashes", ())) == (expected.content_hash,))
        store = Storage(args.output / variant)
        source_key = vision_task_key(f"mineru_flash:{MINERU_UPLOAD_PROTOCOL_VERSION}",
            SOURCE_HASH, (page,), namespace="page") + ":source-markdown"
        if args.reparse_retained_source and store.get_vision_result(source_key) is None:
            raise SystemExit("ACCEPTANCE_RETAINED_RAW_SOURCE_REQUIRED")
        if args.resume_existing_tasks and any(item["variant"] == variant for item in report["results"]):
            if not args.retain_missing_source or store.get_vision_result(source_key) is not None:
                continue
            # One explicitly requested GET-only readback can retain a source
            # discarded by older code, while preserving all prior FAILs.
            report.setdefault("readback_history", []).extend(
                item for item in report["results"] if item["variant"] == variant)
            report["results"] = [item for item in report["results"] if item["variant"] != variant]
        store.save_company(company)
        run_id = f"free-cloud-byd-{variant}"
        existing = store.get_run(run_id)
        if existing is not None and not args.resume_existing_tasks:
            raise SystemExit("ACCEPTANCE_RUN_ALREADY_EXISTS_NO_RESUBMISSION")
        if args.resume_existing_tasks and existing is None:
            raise SystemExit("ACCEPTANCE_EXISTING_RUN_REQUIRED")
        if existing is None:
            store.save_run(ResearchRun(run_id, company, "acceptance-only", "official.long-term-fundamentals", "3.0.0",
                "mineru_flash", "no-key", "2026-10-04", status=RunStatus.RUNNING))
        adapter = DurableCloudVisionAdapter(MineruFlashAdapter(transport, journal=store), store, run_id,
            limits=RecoveryLimits(calls=16, pages=1, seconds=180))
        started = time.monotonic()
        if args.resume_existing_tasks:
            prior_item = next(item for item in report["initial_attempt_results"] if item["variant"] == variant)
            if prior_item["page_hash"] != page.content_hash:
                raise SystemExit("ACCEPTANCE_UPLOADED_PAGE_HASH_MISMATCH")
            task = store.get_vision_task(vision_task_key(
                f"mineru_flash:{MINERU_UPLOAD_PROTOCOL_VERSION}", SOURCE_HASH, (page,), namespace="page"))
            if not task or not task.get("remote_task_id"):
                raise SystemExit("ACCEPTANCE_KNOWN_TASK_REQUIRED_NO_RESUBMISSION")
            # The initial coordinator's premature exhaustion remains a FAIL.
            # This explicitly labelled readback assesses the two already-paid
            # public submissions, never resets their incident budget or POSTS.
            result = MineruFlashAdapter(transport, journal=store).extract(
                company, filing, (page,), config, remote_task_id=task["remote_task_id"])
            if result.error_code is None:
                compiled = FinancialFactCompiler().compile_facts(company, (filing,), result.facts,
                    required_concepts=frozenset(fact.concept for fact in result.facts), evidence=result.evidence)
                if compiled.quarantined_facts or not compiled.accepted_facts:
                    result = VisionExtractionResult(diagnostics=("CLOUD_CANDIDATE_FINANCIAL_INVARIANT_FAILED",),
                        error_code="CLOUD_CANDIDATE_FINANCIAL_INVARIANT_FAILED")
                else:
                    result = VisionExtractionResult(compiled.accepted_facts, compiled.evidence, result.diagnostics)
        else:
            result = adapter.extract(company, filing, (page,), config)
        actual = {(fact.concept, fact.fiscal_year): fact.value for fact in result.facts}
        expected = {(concept, 2025): amount for concept, amount in EXPECTED_CURRENT.items()}
        expected.update({(concept, 2024): amount for concept, amount in EXPECTED_COMPARATIVE.items()})
        missing = [f"{concept}:{year}" for concept, year in expected if (concept, year) not in actual]
        wrong = [f"{concept}:{year}" for (concept, year), value in expected.items()
            if (concept, year) in actual and actual[concept, year] != value]
        item = {"variant": variant, "page_hash": page.content_hash, "uploaded_bytes": len(data),
            "seconds": round(time.monotonic() - started, 3), "error_code": result.error_code,
            "diagnostics": result.diagnostics, "accepted_candidates": len(result.facts),
            "missing_expected_cells": missing, "mismatched_expected_cells": wrong,
            "pass": result.error_code is None and not missing and not wrong,
            "facts": [{"concept": fact.concept, "fiscal_year": fact.fiscal_year, "value": fact.value,
                "source_column": fact.source_column, "revision": fact.revision} for fact in result.facts]}
        report["results"].append(item)
        print(json.dumps({key: value for key, value in item.items() if key != "facts"}, ensure_ascii=False), flush=True)
        report["http_calls"] = transport.calls
        result_name = ("offline-reparse-result.json" if args.reparse_retained_source else
            "query-only-result.json" if args.resume_existing_tasks else "result.json")
        (args.output / result_name).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["pass"] = all(item["pass"] for item in report["results"])
    report["http_calls"] = transport.calls
    (args.output / result_name).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"HTTP_CALLS={len(transport.calls)} SUBMISSIONS={len(transport.submissions)} PASS={report['pass']}", flush=True)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
