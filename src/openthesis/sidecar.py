from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from .paths import default_data_dir
from .service import AppService, PreferenceValidationError


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
        if method == "research.list":
            return self.service.list_research_runs(limit=params.get("limit", 50))
        if method == "research.get_report":
            run_id = params.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("run_id is required")
            language = params.get("language")
            if language is not None and not isinstance(language, str):
                raise ValueError("language must be a string")
            return self.service.get_report(run_id, language=language)
        if method == "research.start":
            return self.service.start_research(params)
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
