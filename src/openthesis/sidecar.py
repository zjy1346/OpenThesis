from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import threading
import time
import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
import sys
from pathlib import Path
from typing import Any, TextIO

from .paths import default_data_dir
from .service import (
    AppService,
    PreferenceValidationError,
    ServiceConfigurationError,
    _FinancialReportRefreshError,
)
from .application.reports import ReportReadError
from .storage import StorageCorruptionError


DEFERRED_METHODS = frozenset({
    "research.get_report", "research.financial_diagnostics", "research.retry_synthesis",
    "research.retry_model_stages", "research.retry_growth", "research.retry_stage",
    "research.revalidate_saved", "research.refresh_financial_report", "research.retry_financials",
    "research.rebuild_financials", "research.market_snapshot", "company.search", "ot.suggest", "packs.install", "app.bootstrap",
})


class JsonLineServer:
    """Small JSON-RPC 2.0 seam for the Tauri platform adapter."""

    def __init__(self, service: AppService):
        self.service = service
        self._operation_lock = threading.Lock()
        self._operations: dict[str, dict] = {}
        self._operations_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="openthesis-rpc-job")

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
        except ServiceConfigurationError as exc:
            return _error(request_id, -32010, exc.code)
        except ReportReadError as exc:
            if self._storage_recovery_required():
                return _error(request_id, -32040, "STORAGE_HEALTH_RECOVERY_REQUIRED")
            return _error(request_id, -32030, exc.code)
        except StorageCorruptionError as exc:
            # Financial row errors are not whole-database recovery signals.
            # Expose only the allowlisted background health code, never paths
            # or the persisted exception text from an unrelated corrupt row.
            if str(exc) == "STORAGE_HEALTH_RECOVERY_REQUIRED":
                return _error(request_id, -32040, "STORAGE_HEALTH_RECOVERY_REQUIRED")
            return _error(request_id, -32603, "internal error")
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
            if self._storage_recovery_required():
                return _error(request_id, -32040, "STORAGE_HEALTH_RECOVERY_REQUIRED")
            return _error(request_id, -32603, "internal error")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _storage_recovery_required(self) -> bool:
        return getattr(getattr(self.service, "storage", None), "maintenance_status", {}).get("state") == "recovery_required"

    def dispatch(self, request: Any) -> Any:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            raise ValueError("invalid request")
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            raise ValueError("invalid request")

        if (self._storage_recovery_required()
                and method not in {"system.hello", "rpc.result", "research.cancel", "research.cancel_model_call"}):
            # Keep health, existing operation results and cancellation accessible.
            # Fail before any broad report wrapper can hide this recovery code
            # or an attempted paid retry can perform more work on a bad store.
            raise StorageCorruptionError("STORAGE_HEALTH_RECOVERY_REQUIRED")

        if method == "rpc.start":
            target, arguments = params.get("method"), params.get("params")
            if target not in DEFERRED_METHODS or not isinstance(arguments, dict):
                raise ValueError("unsupported background operation")
            identity = hashlib.sha256(json.dumps([target, arguments], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            now = time.monotonic()
            with self._operation_lock:
                for key in list(self._operations):
                    value = self._operations[key]
                    if value["state"] != "pending" and value["expires"] <= now:
                        del self._operations[key]
                for key, value in self._operations.items():
                    if value["identity"] == identity and value["state"] == "pending":
                        return {"job_id": key}
                if len(self._operations) >= 8:
                    completed = next((key for key, value in self._operations.items() if value["state"] != "pending"), None)
                    if completed:
                        del self._operations[completed]
                if len(self._operations) >= 8:
                    raise ServiceConfigurationError("CORE_OPERATION_BUSY")
                job_id = uuid.uuid4().hex
                self._operations[job_id] = {"identity": identity, "state": "pending", "expires": now + 660}
            def execute():
                response = self._handle_line(json.dumps({"jsonrpc": "2.0", "id": job_id, "method": target, "params": arguments}))
                with self._operation_lock:
                    self._operations[job_id].update(state="ready", response=response, expires=time.monotonic() + 60)
            self._operations_executor.submit(execute)
            return {"job_id": job_id}
        if method == "rpc.result":
            job_id = params.get("job_id")
            if not isinstance(job_id, str):
                raise ValueError("job identity is required")
            with self._operation_lock:
                row = self._operations.get(job_id)
                if row is None:
                    raise ServiceConfigurationError("CORE_OPERATION_EXPIRED")
                return {"state": row["state"], **({"response": row["response"]} if row["state"] == "ready" else {})}

        if method == "system.hello":
            return self.service.hello()
        if method == "app.bootstrap":
            return self.service.bootstrap(minimal=params.get("minimal") is True)
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
                request_id=params.get("request_id", ""),
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
            company = params.get("company")
            if company is not None and not isinstance(company, dict):
                raise ValueError("company identity must be an object")
            return self.service.save_thesis_version(
                company_cik,
                content,
                base_thesis_version_id=params.get("base_thesis_version_id"),
                company=company,
            )
        if method == "research.list":
            return self.service.list_research_runs(limit=params.get("limit", 50))
        if method == "research.call_history":
            run_id = params.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run identity is required")
            return self.service.storage.model_call_history(run_id, stage_id=params.get("stage_id"),
                after=params.get("after", ""), limit=params.get("limit", 10))
        if method == "research.delete":
            run_id = params.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run_id is required")
            return self.service.delete_research_run(run_id)
        if method == "research.extend_budget":
            return self.service.extend_model_budget(params.get("run_id"), params.get("plan_digest"), params.get("gap_ids"))
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
        if method == "research.financial_diagnostics":
            run_id = params.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run_id is required")
            return self.service.financial_diagnostics(run_id)
        if method == "research.start":
            return self.service.start_research(params)
        if method == "research.market_snapshot":
            company = params.get("company")
            if not isinstance(company, dict):
                raise ValueError("company is required")
            return self.service.preview_market_snapshot(params)
        if method == "research.retry_synthesis":
            run_id = params.get("run_id")
            model = params.get("model")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run_id is required")
            if model is not None and not isinstance(model, dict):
                raise ValueError("model must be an object")
            return self.service.retry_research_synthesis(run_id, model)
        if method == "research.retry_model_stages":
            run_id = params.get("run_id")
            model = params.get("model")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run_id is required")
            if model is not None and not isinstance(model, dict):
                raise ValueError("model must be an object")
            return self.service.retry_research_model_stages(run_id, model, budget_only=params.get("budget_only", False))
        if method == "research.revalidate_saved":
            run_id = params.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run_id is required")
            return self.service.revalidate_saved_research(run_id)
        if method == "research.retry_growth":
            run_id = params.get("run_id")
            model = params.get("model")
            if not isinstance(run_id, str) or not run_id or not isinstance(model, dict):
                raise ValueError("run_id and model are required")
            return self.service.retry_research_growth(run_id, model)
        if method == "research.retry_stage":
            run_id = params.get("run_id")
            target = params.get("target")
            model = params.get("model")
            plan_hash = params.get("plan_hash")
            confirm_new_attempt = params.get("confirm_new_attempt", False)
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run_id is required")
            if not isinstance(target, str) or not target:
                raise ValueError("target is required")
            if model is not None and not isinstance(model, dict):
                raise ValueError("model must be an object")
            if plan_hash is not None and not isinstance(plan_hash, str):
                raise ValueError("plan_hash must be a string")
            if type(confirm_new_attempt) is not bool:
                raise ValueError("confirm_new_attempt must be a boolean")
            if confirm_new_attempt:
                return self.service.retry_research_stage(run_id, target, model, plan_hash,
                                                        confirm_new_attempt=True)
            return self.service.retry_research_stage(run_id, target, model, plan_hash)
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
        if method == "research.cancel_model_call":
            values = [params.get(key) for key in ("job_id", "request_id", "attempt_id")]
            if any(not isinstance(value, str) or not value or len(value) > 256 for value in values):
                raise ValueError("job_id, request_id and attempt_id are required")
            return self.service.cancel_model_call(*values)
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
    last_progress = None
    def progress(stage, completed=None, total=None):
        nonlocal last_progress
        if os.environ.get("OPENTHESIS_STARTUP_PROGRESS") != "1":
            return
        # Only host-owned stage names/counters leave initialization. Never
        # send a path, exception, database payload or provider configuration.
        if stage not in {"checking_schema", "checking_integrity", "backing_up", "migrating_schema", "backfilling_facts",
                         "building_read_models", "storage_ready", "preparing_services", "ready"}:
            return
        valid = type(completed) is int and type(total) is int and 0 <= completed <= total and total > 0
        signature = (stage, completed * 100 // total if valid else None)
        if signature == last_progress:
            return
        last_progress = signature
        record = {"stage": stage, "completed_units": completed if valid else None, "total_units": total if valid else None}
        try:
            sys.stderr.write("OT_STARTUP " + json.dumps(record, separators=(",", ":")) + "\n")
            sys.stderr.flush()
        except OSError:
            pass
    service = AppService(args.data_dir or default_data_dir(), initialization_progress=progress)
    JsonLineServer(service).serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
