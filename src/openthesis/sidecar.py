from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
from pathlib import Path
from typing import Any, TextIO

from .paths import default_data_dir
from .service import AppService, PreferenceValidationError, _FinancialReportRefreshError


class JsonLineServer:
    """Small JSON-RPC 2.0 seam for the Tauri platform adapter."""

    def __init__(self, service: AppService):
        self.service = service

    def serve(self, input_stream: TextIO, output_stream: TextIO) -> None:
        for line in input_stream:
            if not line.strip():
                continue
            response = self._handle_line(line)
            output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            output_stream.flush()

    def _handle_line(self, line: str) -> dict[str, Any]:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            return _error(None, -32700, "invalid JSON")

        request_id = request.get("id") if isinstance(request, dict) else None
        try:
            result = self.dispatch(request)
        except PreferenceValidationError:
            return _error(request_id, -32602, "invalid preferences")
        except (TypeError, ValueError):
            return _error(request_id, -32602, "invalid parameters")
        except KeyError as exc:
            message = exc.args[0] if exc.args else "resource not found"
            if message not in {"research run not found", "research job not found"}:
                message = "resource not found"
            return _error(request_id, -32004, message)
        except MethodNotFoundError:
            return _error(request_id, -32601, "method not found")
        except _FinancialReportRefreshError as exc:
            return _error(request_id, -32020, exc.code)
        except Exception:
            return _error(request_id, -32603, "internal error")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def dispatch(self, request: Any) -> Any:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            raise ValueError("invalid request")
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            raise ValueError("invalid request")

        if method == "system.hello":
            return self.service.hello()
        if method == "app.bootstrap":
            return self.service.bootstrap()
        if method == "settings.update":
            preferences = params.get("preferences")
            if not isinstance(preferences, dict):
                raise ValueError("preferences are required")
            return self.service.update_preferences(preferences)
        if method == "company.search":
            query = params.get("query")
            if not isinstance(query, str):
                raise ValueError("query is required")
            market = params.get("market", "US")
            if not isinstance(market, str):
                raise ValueError("market must be a string")
            return self.service.search_companies(
                query,
                market=market,
                limit=params.get("limit", 15),
            )
        if method == "ot.validate":
            return self.service.validate_ot_draft(params.get("draft"))
        if method == "ot.compile":
            return self.service.compile_ot_draft(params.get("draft"))
        if method == "ot.suggest":
            return self.service.suggest_ot_patch(
                params.get("draft"),
                params.get("selected_path"),
                params.get("instruction", ""),
                params.get("model"),
            )
        if method == "packs.install":
            filename = params.get("filename")
            encoded_archive = params.get("data_base64")
            if not isinstance(filename, str) or not isinstance(encoded_archive, str):
                raise ValueError("research pack payload is required")
            return self.service.install_research_pack(filename, encoded_archive)
        if method == "thesis.list":
            return self.service.list_theses(limit=params.get("limit", 100))
        if method == "thesis.get":
            thesis_version_id = params.get("thesis_version_id")
            if not isinstance(thesis_version_id, str) or not thesis_version_id:
                raise ValueError("thesis_version_id is required")
            return self.service.get_thesis(thesis_version_id)
        if method == "thesis.save":
            company_cik = params.get("company_cik")
            content = params.get("content")
            if not isinstance(company_cik, str) or not isinstance(content, dict):
                raise ValueError("thesis content is required")
            return self.service.save_thesis_version(company_cik, content)
        if method == "research.list":
            return self.service.list_research_runs(limit=params.get("limit", 50))
        if method == "research.delete":
            run_id = params.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run_id is required")
            return self.service.delete_research_run(run_id)
        if method == "research.get_report":
            run_id = params.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run_id is required")
            language = params.get("language")
            if language is not None and not isinstance(language, str):
                raise ValueError("language must be a string")
            include_technical = params.get("include_technical", False)
            if not isinstance(include_technical, bool):
                raise ValueError("include_technical must be a boolean")
            return self.service.get_report(
                run_id,
                language=language,
                include_technical=include_technical,
            )
        if method == "research.start":
            return self.service.start_research(params)
        if method == "research.retry_synthesis":
            run_id = params.get("run_id")
            model = params.get("model")
            if not isinstance(run_id, str) or not run_id or not isinstance(model, dict):
                raise ValueError("run_id and model are required")
            return self.service.retry_research_synthesis(run_id, model)
        if method == "research.retry_growth":
            run_id = params.get("run_id")
            model = params.get("model")
            if not isinstance(run_id, str) or not run_id or not isinstance(model, dict):
                raise ValueError("run_id and model are required")
            return self.service.retry_research_growth(run_id, model)
        if method == "research.retry_financials":
            run_id = params.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run_id is required")
            return self.service.retry_financials(run_id)
        if method == "research.refresh_financial_report":
            run_id = params.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run_id is required")
            language = params.get("language")
            if language is not None and not isinstance(language, str):
                raise ValueError("language must be a string")
            return self.service.refresh_financial_report(run_id, language=language)
        if method == "research.start_financial_retry":
            run_id = params.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run_id is required")
            return self.service.start_financial_retry(run_id)
        if method == "research.rebuild_financials":
            run_id = params.get("run_id")
            confirmed = params.get("confirmed")
            if not isinstance(run_id, str) or not run_id or not isinstance(confirmed, bool):
                raise ValueError("run_id and confirmed are required")
            return self.service.rebuild_financials(run_id, confirmed=confirmed)
        if method == "research.start_financial_rebuild":
            run_id = params.get("run_id")
            confirmed = params.get("confirmed")
            if not isinstance(run_id, str) or not run_id or not isinstance(confirmed, bool):
                raise ValueError("run_id and confirmed are required")
            return self.service.start_financial_retry(
                run_id, force=True, confirmed=confirmed
            )
        if method == "research.vision_decision":
            job_id = params.get("job_id")
            approved = params.get("approved")
            if not isinstance(job_id, str) or not job_id or not isinstance(approved, bool):
                raise ValueError("job_id and approved are required")
            return self.service.vision_decision(job_id, approved)
        if method in {"research.status", "research.cancel"}:
            job_id = params.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise ValueError("job_id is required")
            if method == "research.status":
                return self.service.get_research_status(job_id)
            return self.service.cancel_research(job_id)
        raise MethodNotFoundError(method)


class MethodNotFoundError(LookupError):
    pass


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="OpenThesis desktop sidecar")
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    service = AppService(args.data_dir or default_data_dir())
    JsonLineServer(service).serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
