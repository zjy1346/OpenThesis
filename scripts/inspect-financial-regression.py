"""Capture existing real-source regression results, never rewrite expectations."""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import importlib.util
import json
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("financial_regression_fixture", Path(__file__).resolve().parents[1] / "tests/test_financial_ingestion_engine.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    engine = module.FinancialIngestionEngine
    original = engine.ingest
    captures = []

    def capture(self, company, filings, *positional, **keywords):
        dataset = original(self, company, filings, *positional, **keywords)
        captures.append({"company": company.to_dict(), "filings": [item.to_dict() for item in filings],
                         "result": asdict(dataset) if is_dataclass(dataset) else vars(dataset)})
        return dataset

    engine.ingest = capture
    names = args.case or [name for name in dir(module.FinancialIngestionEngineTests) if name.startswith("test_public_engine_parses_real_")]
    try:
        for name in names:
            captures.clear()
            started = time.monotonic()
            error = ""
            try:
                getattr(module.FinancialIngestionEngineTests(name), name)()
            except Exception as exception:
                error = type(exception).__name__ + ": " + str(exception)
            result = {"case": name, "seconds": round(time.monotonic() - started, 3), "test_error": error, "captures": captures}
            target = args.output / (name + ".json")
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=lambda item: getattr(item, "value", str(item))), encoding="utf-8")
            print(json.dumps({"case": name, "seconds": result["seconds"], "captured": len(captures), "test_error": error}, ensure_ascii=False), flush=True)
    finally:
        engine.ingest = original


if __name__ == "__main__":
    main()
