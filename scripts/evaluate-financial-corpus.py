"""Evaluate the official financial golden corpus through the production compiler.

The command is deliberately read-only: it never downloads, writes a database,
or invokes a model.  Local PDF paths are discovered from an explicit override
or the normal application cache and are never printed in the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import queue
import time
from typing import Any

from openthesis.domain import Company, FilingDocument
from openthesis.financial_compiler import CompilerPolicy, FinancialFactCompiler


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "official_financial_sources.json"


def _safe_key(ticker: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in ticker.upper())


def _candidate_paths(issuer: dict[str, Any], period: dict[str, Any]) -> list[Path]:
    ticker = issuer["ticker"]
    accession = str(period["accession"])
    paths: list[Path] = []
    override = os.environ.get(f"OPENTHESIS_GOLDEN_{_safe_key(ticker)}_{period['fiscal_year']}_PDF")
    if override:
        paths.append(Path(override))
    root = os.environ.get("OPENTHESIS_GOLDEN_ROOT")
    roots = [Path(root)] if root else []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        roots.extend([
            Path(local_appdata) / "OpenThesis" / "filings",
            Path(local_appdata) / "org.openthesis.desktop" / "filings",
        ])
    roots.append(ROOT / "tmp" / "financial-corpus")
    for base in roots:
        if not base.exists():
            continue
        direct = list(base.glob(f"**/*{accession}*.pdf"))
        paths.extend(direct)
    return list(dict.fromkeys(paths))


def _company(issuer: dict[str, Any]) -> Company:
    return Company(
        issuer["issuer_id"], issuer["ticker"], issuer["issuer"],
        market=issuer["market"], reporting_currency=issuer["currency"],
        listing_currency=issuer["currency"],
        accounting_standard=issuer["accounting_standard"],
    )


def _safe_actual_facts(facts: Any) -> list[dict[str, Any]]:
    """Project only auditable fact metadata; never expose paths or raw text."""
    projected = []
    for fact in facts:
        raw = str(getattr(fact, "raw_text", "") or "")
        projected.append({
            "concept": str(fact.concept),
            "value": str(fact.value),
            "currency": str(getattr(fact, "currency", "") or ""),
            "unit_scale": float(getattr(fact, "unit_scale", 1.0) or 1.0),
            "source_page": getattr(fact, "source_page", None),
            "scope": str(getattr(fact, "consolidated_scope", "") or getattr(fact, "scope", "")),
            "period_end": str(fact.end_date),
            "raw_excerpt_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else "",
        })
    return projected


def _evaluate_period(issuer: dict[str, Any], period: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    expected = period.get("expected_facts") or {
        concept: None for concept in (
            "revenue", "net_income", "operating_cash_flow",
            "assets", "liabilities", "equity",
        )
    }
    paths = _candidate_paths(issuer, period)
    path = next((item for item in paths if item.is_file()), None)
    base = {
        "ticker": issuer["ticker"], "issuer": issuer["issuer"],
        "fiscal_year": period["fiscal_year"], "period_end": period["period_end"],
        "accession": period["accession"], "expected_status": period["resolution_status"],
        "required_concepts": sorted(expected), "source_url": period["source_url"],
        "mineru_triggered": False,
        "actual_facts": [],
    }
    if path is None:
        base.update({
            "status": "SOURCE_UNAVAILABLE",
            "required_recognized": 0,
            "required_total": len(expected),
            "required_completeness_rate": 0.0,
            "numeric_expected": sum(isinstance(value, (int, float)) for value in expected.values()),
            "numeric_exact_matches": 0,
            "numeric_misidentifications": 0,
            "numeric_misidentification_rate": 0.0,
            "conflict_count": 0,
            "conflict_rate": 0.0,
            "mineru_trigger_count": 0,
            "mineru_trigger_rate": 0.0,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        })
        return base
    try:
        company = _company(issuer)
        filing = FilingDocument(
            f"golden:{issuer['ticker']}:{period['fiscal_year']}", company.security_id,
            str(period["accession"]), "ANNUAL_REPORT", "FY", period["period_end"],
            f"{period['fiscal_year'] + 1}-04-01", period["document"], period["source_url"],
            local_path=str(path), content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        dataset = FinancialFactCompiler().compile(
            company, (filing.period_end, filing.period_end),
            CompilerPolicy(filings=(filing,), reporting_currency=issuer["currency"]),
        )
        actual = {fact.concept: fact.value for fact in dataset.research_facts}
        known = {key: value for key, value in expected.items() if isinstance(value, (int, float))}
        recognized = len(set(expected) & set(actual))
        matched = sum(
            1 for key, value in known.items()
            if key in actual and abs(float(actual[key]) - float(value)) <= max(1e-6, abs(float(value)) * 1e-9)
        )
        unresolved_expectation = any(value is None for value in expected.values())
        base.update({
            "status": (
                "EXPECTATION_UNRESOLVED" if unresolved_expectation
                else "VERIFIED" if dataset.allow_ai and len(known) == matched
                else "MISMATCH_OR_INCOMPLETE"
            ),
            "compiler_status": dataset.status,
            "allow_ai": bool(dataset.allow_ai),
            "actual_facts": _safe_actual_facts(dataset.research_facts),
            "resolved_concepts": sorted(actual),
            "required_recognized": recognized,
            "required_total": len(expected),
            "required_completeness_rate": round(recognized / len(expected), 6) if expected else 1.0,
            "numeric_expected": len(known), "numeric_exact_matches": matched,
            "numeric_misidentifications": len(known) - matched,
            "numeric_misidentification_rate": round((len(known) - matched) / len(known), 6) if known else 0.0,
            "conflict_count": len(dataset.conflicts),
            "conflict_rate": round(len(dataset.conflicts) / len(expected), 6) if expected else 0.0,
            "mineru_trigger_count": 0,
            "mineru_trigger_rate": 0.0,
            "validation_statuses": [item.status for item in dataset.validations],
            "diagnostics": list(dataset.diagnostics),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        })
    except Exception as exc:
        base.update({
            "status": "PARSER_ERROR", "error_type": type(exc).__name__,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        })
    return base


def _period_worker(issuer: dict[str, Any], period: dict[str, Any], result_queue: Any) -> None:
    """Isolated worker so a timed-out PDF parse can be terminated on Windows."""
    try:
        result_queue.put(_evaluate_period(issuer, period))
    except BaseException as exc:  # communicate only a safe class name
        result_queue.put({
            "ticker": issuer["ticker"], "fiscal_year": period["fiscal_year"],
            "period_end": period["period_end"], "accession": period["accession"],
            "status": "PARSER_ERROR", "error_type": type(exc).__name__,
        })


def _evaluate_bounded(issuer: dict[str, Any], period: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(target=_period_worker, args=(issuer, period, result_queue))
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(5)
        result_queue.close()
        return {
            "ticker": issuer["ticker"], "fiscal_year": period["fiscal_year"],
            "period_end": period["period_end"], "accession": period["accession"],
            "status": "TIMEOUT", "elapsed_seconds": timeout,
        }
    try:
        return result_queue.get_nowait()
    except queue.Empty:
        return {
            "ticker": issuer["ticker"], "fiscal_year": period["fiscal_year"],
            "period_end": period["period_end"], "accession": period["accession"],
            "status": "PARSER_ERROR", "error_type": "NoWorkerResult",
        }
    finally:
        result_queue.close()


def evaluate(*, ticker: str | None = None, year: int | None = None, smoke: bool = False) -> dict[str, Any]:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    jobs = []
    for issuer in corpus["issuers"]:
        if ticker and issuer["ticker"].upper() != ticker.upper():
            continue
        periods = [item for item in issuer["periods"] if year is None or item["fiscal_year"] == year]
        periods = periods[:1] if smoke else periods
        jobs.extend((issuer, period) for period in periods)
    rows: list[dict[str, Any]] = []
    # Each parse is a child process.  A timeout therefore cannot leave a PDF
    # parser thread running after the evaluator reports the row.
    for issuer, period in jobs:
        rows.append(_evaluate_bounded(issuer, period))
    return {"schema_version": corpus["schema_version"], "read_only": True, "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="evaluate one issuer")
    parser.add_argument("--year", type=int, help="evaluate one fiscal year")
    parser.add_argument("--smoke", action="store_true", help="evaluate one FY per issuer")
    args = parser.parse_args()
    print(json.dumps(evaluate(ticker=args.ticker, year=args.year, smoke=args.smoke), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
