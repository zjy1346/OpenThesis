"""Bounded vision fallback for approved failed financial pages.

Configured models stay behind the Rust credential gateway. The separate MinerU
Flash path is token-free and can run only after an explicit per-run page preview
approval; neither path exposes long-term credentials to Python.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from datetime import date, timedelta
from hashlib import sha256
from io import BytesIO
import json
import re
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .domain import Company, EvidenceRef, FilingDocument, FinancialFact
from .providers import ProviderError


VISION_MAX_PAGES = 20
VISION_MAX_BYTES = 10 * 1024 * 1024
VISION_TASK_SCHEMA_VERSION = "vision-task-v1"


def _vision_period_start(fiscal_period: str, period_end: str, statement: str) -> str | None:
    if statement == "balance_sheet":
        return None
    try:
        end = date.fromisoformat(period_end[:10])
    except ValueError:
        return None
    period = (fiscal_period or "FY").upper()
    if period == "FY":
        try:
            return (end.replace(year=end.year - 1) + timedelta(days=1)).isoformat()
        except ValueError:
            return (end - timedelta(days=365) + timedelta(days=1)).isoformat()
    if period in {"H1", "Q1", "Q2", "Q3", "9M"}:
        return end.replace(month=1, day=1).isoformat()
    return end.replace(month=1, day=1).isoformat()


class VisionAdapterError(RuntimeError):
    """Safe, stable failure; never includes tokens, signed URLs, or payloads."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VisionFallbackConfig:
    enabled: bool = False
    consent: bool = False
    provider: str = "configured_model"
    configured_model_id: str = ""
    configuration_version: int = 1
    timeout_seconds: float = 60.0
    max_pages: int = VISION_MAX_PAGES
    max_bytes: int = VISION_MAX_BYTES
    approve_upload: Callable[[Mapping[str, Any]], bool] | None = None
    language: str = "auto"
    require_page_approval: bool = False

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.consent:
            raise VisionAdapterError("VISION_CONSENT_REQUIRED")
        if self.max_pages < 1 or self.max_pages > VISION_MAX_PAGES:
            raise VisionAdapterError("VISION_PAGE_LIMIT")
        if self.max_bytes < 1 or self.max_bytes > VISION_MAX_BYTES:
            raise VisionAdapterError("VISION_SIZE_LIMIT")
        if self.provider not in {"mineru_flash", "configured_model"}:
            raise VisionAdapterError("VISION_PROVIDER_UNSUPPORTED")
        if self.provider == "configured_model" and not self.configured_model_id:
            raise VisionAdapterError("VISION_MODEL_REQUIRED")
        if not self.require_page_approval or self.approve_upload is None:
            raise VisionAdapterError("VISION_UPLOAD_APPROVAL_REQUIRED")


@dataclass(frozen=True, slots=True)
class VisionPageRequest:
    original_page: int
    pdf_bytes: bytes
    source_url: str = ""
    source_document: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        if self.original_page < 1:
            raise ValueError("original_page must be positive")
        digest = sha256(self.pdf_bytes).hexdigest()
        if self.content_hash and self.content_hash != digest:
            raise ValueError("page_hash_mismatch")
        if not self.content_hash:
            object.__setattr__(self, "content_hash", digest)


FinancialPageRequest = VisionPageRequest


@dataclass(frozen=True, slots=True)
class VisionExtractionResult:
    facts: tuple[FinancialFact, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    diagnostics: tuple[str, ...] = ()
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None and bool(self.facts)


VisionResult = VisionExtractionResult


def vision_task_key(
    provider: str,
    filing_hash: str,
    pages: Sequence[VisionPageRequest],
    *,
    schema_version: str = VISION_TASK_SCHEMA_VERSION,
    namespace: str = "batch",
) -> str:
    """Return a deterministic key without retaining page/PDF content."""

    material = "|".join(
        [schema_version, str(namespace), str(provider), str(filing_hash or "")]
        + [f"{page.original_page}:{page.content_hash}" for page in pages]
    )
    return sha256(material.encode("utf-8")).hexdigest()


class VisionTaskJournal(Protocol):
    def save_vision_task(self, task_key: str, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_vision_task(self, task_key: str) -> Mapping[str, Any] | None: ...

    def save_vision_result(self, task_key: str, result: dict[str, Any]) -> None: ...

    def get_vision_result(self, task_key: str) -> Mapping[str, Any] | None: ...


def _provider_identity(config: VisionFallbackConfig) -> str:
    if config.provider == "configured_model":
        return (
            f"configured_model:{config.configured_model_id}:"
            f"configuration-{config.configuration_version}"
        )
    return config.provider


def _serialize_vision_result(result: VisionExtractionResult) -> dict[str, Any]:
    return {
        "facts": [fact.to_dict() for fact in result.facts],
        "evidence": [item.to_dict() for item in result.evidence],
        "diagnostics": list(result.diagnostics),
        "error_code": result.error_code,
    }


def _deserialize_vision_result(payload: Mapping[str, Any]) -> VisionExtractionResult | None:
    try:
        facts = tuple(FinancialFact(**dict(item)) for item in payload.get("facts", ()))
        evidence = tuple(EvidenceRef(**dict(item)) for item in payload.get("evidence", ()))
        diagnostics = tuple(str(item) for item in payload.get("diagnostics", ()))
        error_code = payload.get("error_code")
        if error_code is not None or not facts:
            return None
        return VisionExtractionResult(facts, evidence, diagnostics, None)
    except (TypeError, ValueError):
        return None

@dataclass(frozen=True, slots=True)
class VisionHttpResponse:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VisionAdapterError("VISION_MALFORMED_RESPONSE") from exc


class VisionHttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 60.0,
    ) -> VisionHttpResponse: ...


class UrllibVisionTransport:
    """Small bounded transport used only by the no-token MinerU Flash path."""

    class _HttpsOnlyRedirects(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not str(newurl).startswith("https://"):
                return None
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    def __init__(self) -> None:
        self._opener = build_opener(self._HttpsOnlyRedirects())

    def request(self, method: str, url: str, *, headers=None, body=None, timeout=60.0):
        if not str(url).startswith("https://"):
            raise VisionAdapterError("VISION_INSECURE_URL")
        request = Request(url, data=body, headers=dict(headers or {}), method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                payload = response.read(VISION_MAX_BYTES + 1)
                if len(payload) > VISION_MAX_BYTES:
                    raise VisionAdapterError("VISION_SIZE_LIMIT")
                return VisionHttpResponse(response.status, payload, dict(response.headers.items()))
        except HTTPError as exc:
            payload = exc.read(VISION_MAX_BYTES + 1)
            if len(payload) > VISION_MAX_BYTES:
                raise VisionAdapterError("VISION_SIZE_LIMIT") from exc
            return VisionHttpResponse(exc.code, payload, dict(exc.headers.items()))
        except (URLError, TimeoutError, OSError) as exc:
            raise VisionAdapterError("VISION_NETWORK_ERROR") from exc


class VisionFinancialSourceAdapter(Protocol):
    def extract(
        self,
        company: Company,
        filing: FilingDocument,
        pages: Sequence[VisionPageRequest],
        config: VisionFallbackConfig,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> VisionExtractionResult: ...


def _check_request(config: VisionFallbackConfig, pages: Sequence[VisionPageRequest], filing: FilingDocument | None = None) -> None:
    config.validate()
    if not config.enabled:
        raise VisionAdapterError("VISION_DISABLED")
    if not pages:
        raise VisionAdapterError("VISION_NO_FAILED_PAGES")
    if len(pages) > config.max_pages:
        raise VisionAdapterError("VISION_PAGE_LIMIT")
    if sum(len(page.pdf_bytes) for page in pages) > config.max_bytes:
        raise VisionAdapterError("VISION_SIZE_LIMIT")
    summary = {
        "provider": config.provider if config.provider == "mineru_flash" else config.configured_model_id,
        "pages": tuple(page.original_page for page in pages),
        "total_bytes": sum(len(page.pdf_bytes) for page in pages),
        "document_hashes": tuple(page.content_hash for page in pages),
        "source_document": (filing.primary_document if filing else ""),
        "filing_hash": (filing.content_hash if filing else ""),
    }
    approver = config.approve_upload
    if approver is not None and not approver(summary):
        raise VisionAdapterError("VISION_UPLOAD_NOT_APPROVED")



def _safe_error(response: VisionHttpResponse) -> str:
    if response.status == 401:
        return "VISION_UNAUTHORIZED"
    if response.status == 403:
        return "VISION_FORBIDDEN"
    if response.status == 429:
        return "VISION_RATE_LIMITED"
    if response.status >= 400:
        return "VISION_HTTP_ERROR"
    return "VISION_MALFORMED_RESPONSE"


def _https_url(value: Any) -> str:
    url = str(value or "")
    if not url.startswith("https://"):
        raise VisionAdapterError("VISION_INSECURE_URL")
    return url


def _json_value(payload: Any, *paths: str) -> Any:
    if not isinstance(payload, dict):
        return None
    for path in paths:
        value: Any = payload
        for part in path.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                value = None
                break
        if value is not None:
            return value
    return None


def _ensure_business_success(payload: Any) -> None:
    code = _json_value(payload, "code", "data.code")
    if code not in (None, 0, "0", "OK", "ok", "SUCCESS", "success"):
        raise VisionAdapterError("VISION_REMOTE_FAILED")


class MineruFlashAdapter:
    """No-token MinerU Flash adapter for approved failed PDF pages only."""

    endpoint = "https://mineru.net/api/v1/agent"

    def __init__(
        self,
        transport: VisionHttpTransport | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        journal: VisionTaskJournal | None = None,
    ):
        self.transport = transport or UrllibVisionTransport()
        self.sleep = sleep
        self.clock = clock
        self.journal = journal

    def _request(
        self,
        method: str,
        url: str,
        config: VisionFallbackConfig,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> VisionHttpResponse:
        """Retry only bounded network/server failures, never auth or payload errors."""

        deadline = self.clock() + max(0.01, config.timeout_seconds)
        attempts = 0
        while True:
            if cancel_check and cancel_check():
                raise VisionAdapterError("VISION_CANCELLED")
            try:
                response = self.transport.request(
                    method, _https_url(url), headers=headers, body=body,
                    timeout=max(0.01, deadline - self.clock()),
                )
            except (VisionAdapterError, OSError, TimeoutError) as exc:
                error_code = exc.code if isinstance(exc, VisionAdapterError) else "VISION_NETWORK_ERROR"
                if error_code != "VISION_NETWORK_ERROR" or attempts >= 1:
                    if isinstance(exc, VisionAdapterError):
                        raise
                    raise VisionAdapterError("VISION_NETWORK_ERROR") from exc
                delay = 0.1
                attempts += 1
                if self.clock() + delay >= deadline:
                    raise VisionAdapterError("VISION_TIMEOUT") from exc
                self.sleep(delay)
                continue
            if response.status in {401, 403}:
                raise VisionAdapterError(_safe_error(response))
            retryable = response.status == 429 or 500 <= response.status < 600
            if not retryable:
                if response.status >= 400:
                    raise VisionAdapterError(_safe_error(response))
                return response
            if attempts >= 1:
                raise VisionAdapterError(_safe_error(response))
            retry_after = next(
                (value for key, value in response.headers.items() if str(key).lower() == "retry-after"),
                "",
            )
            try:
                delay = max(0.0, float(retry_after))
            except (TypeError, ValueError):
                # A 429 without a valid server budget is not retried; 5xx gets
                # one short bounded retry.
                if response.status == 429:
                    raise VisionAdapterError("VISION_RATE_LIMITED")
                delay = 0.1
            attempts += 1
            if self.clock() + delay >= deadline:
                raise VisionAdapterError("VISION_TIMEOUT")
            if cancel_check and cancel_check():
                raise VisionAdapterError("VISION_CANCELLED")
            self.sleep(delay)

    def _poll(self, url: str, config: VisionFallbackConfig, cancel_check):
        deadline = self.clock() + max(0.01, config.timeout_seconds)
        while self.clock() < deadline:
            if cancel_check and cancel_check():
                raise VisionAdapterError("VISION_CANCELLED")
            response = self._request("GET", url, config, cancel_check=cancel_check)
            payload = response.json()
            state = str(_json_value(payload, "state", "status", "data.state") or "").lower()
            if state in {"success", "succeeded", "done", "completed"}:
                return payload
            if state in {"failed", "error", "cancelled"}:
                raise VisionAdapterError("VISION_REMOTE_FAILED")
            self.sleep(min(2.0, max(0.01, deadline - self.clock())))
        raise VisionAdapterError("VISION_TIMEOUT")

    def _save_journal(self, task_key: str, filing: FilingDocument, page: VisionPageRequest, *, status: str, remote_task_id: str = "", error_code: str = "") -> None:
        if self.journal is None:
            return
        self.journal.save_vision_task(
            task_key,
            company_cik=filing.company_cik,
            document_id=filing.document_id,
            provider="mineru_flash",
            page_hashes=(page.content_hash,),
            page_numbers=(page.original_page,),
            status=status,
            remote_task_id=remote_task_id,
            error_code=error_code,
        )

    def extract(self, company, filing, pages, config, *, cancel_check=None):
        try:
            _check_request(config, pages, filing)
            if config.provider != "mineru_flash":
                raise VisionAdapterError("VISION_PROVIDER_UNSUPPORTED")
            facts: list[FinancialFact] = []
            refs: list[EvidenceRef] = []
            diagnostics: list[str] = []
            page_errors: list[str] = []
            page_task_key = ""
            current_page: VisionPageRequest | None = None
            task_id = ""
            remote_state = ""
            for page in pages:
                current_page = page
                task_id = ""
                remote_state = ""
                if cancel_check and cancel_check():
                    raise VisionAdapterError("VISION_CANCELLED")
                page_task_key = vision_task_key(
                    "mineru_flash", filing.content_hash, (page,), namespace="page"
                )
                prior = self.journal.get_vision_task(page_task_key) if self.journal is not None else None
                prior_status = str(prior.get("status") or "") if prior else ""
                if prior_status == "created":
                    # The service cannot prove that the prior upload completed.
                    # Polling could incorrectly treat an empty remote task as a
                    # valid parse, while re-uploading could duplicate disclosure.
                    raise VisionAdapterError("VISION_RECOVERY_BLOCKED")
                if prior and prior.get("remote_task_id") and prior_status in {"uploaded", "running", "completed"}:
                    task_id = str(prior["remote_task_id"])
                    remote_state = prior_status
                payload = {
                    "file_name": f"failed-page-{page.original_page}.pdf",
                    "language": config.language if config.language in {"ch", "en"} else ("ch" if company.market == "CN_A" else "en"),
                    "enable_table": True,
                    "is_ocr": False,
                    "enable_formula": False,
                }
                if not task_id:
                    response = self._request(
                        "POST",
                        self.endpoint + "/parse/file",
                        config,
                        headers={"Content-Type": "application/json"},
                        body=json.dumps(payload).encode("utf-8"),
                        cancel_check=cancel_check,
                    )
                    data = response.json()
                    _ensure_business_success(data)
                    task_id = str(_json_value(data, "data.task_id") or "")
                    file_url = _https_url(_json_value(data, "data.file_url"))
                    if not task_id:
                        raise VisionAdapterError("VISION_MALFORMED_RESPONSE")
                    # ``created`` means the upload is not known to have
                    # completed. A restart blocks instead of guessing whether
                    # the signed upload was consumed.
                    self._save_journal(page_task_key, filing, page, status="created", remote_task_id=task_id)
                    remote_state = "created"
                    if cancel_check and cancel_check():
                        raise VisionAdapterError("VISION_CANCELLED")
                    self._request("PUT", file_url, config, body=page.pdf_bytes, cancel_check=cancel_check)
                    self._save_journal(page_task_key, filing, page, status="uploaded", remote_task_id=task_id)
                    remote_state = "uploaded"
                self._save_journal(page_task_key, filing, page, status="running", remote_task_id=task_id)
                remote_state = "running"
                result = self._poll(self.endpoint + "/parse/" + str(task_id), config, cancel_check)
                markdown_url = _https_url(_json_value(result, "data.markdown_url", "data.result.markdown_url"))
                if cancel_check and cancel_check():
                    raise VisionAdapterError("VISION_CANCELLED")
                markdown = self._request("GET", markdown_url, config, cancel_check=cancel_check)
                candidate = parse_vision_markdown(markdown.body.decode("utf-8", errors="strict"), company, filing, (page,))
                facts.extend(candidate.facts)
                refs.extend(candidate.evidence)
                diagnostics.extend(candidate.diagnostics)
                if candidate.error_code:
                    page_errors.append(candidate.error_code)
                self._save_journal(page_task_key, filing, page, status="completed", remote_task_id=task_id)
            if not facts:
                error_code = page_errors[0] if page_errors else "VISION_NO_CANDIDATES"
                return VisionExtractionResult((), (), tuple(diagnostics) + (error_code,), error_code)
            return VisionExtractionResult(
                tuple(facts), tuple(refs), tuple(diagnostics) + ("VISION_MINERU_FLASH_COMPLETED",),
                page_errors[0] if page_errors else None,
            )
        except UnicodeDecodeError:
            return VisionExtractionResult(diagnostics=("VISION_MALFORMED_RESPONSE",), error_code="VISION_MALFORMED_RESPONSE")
        except VisionAdapterError as exc:
            if (
                exc.code != "VISION_RECOVERY_BLOCKED"
                and self.journal is not None
                and current_page is not None
                and page_task_key
            ):
                self._save_journal(
                    page_task_key,
                    filing,
                    current_page,
                    # A failed/aborted PUT has an unknowable remote outcome.
                    # Keep ``created`` so restart blocks instead of creating a
                    # second chargeable task or polling an unproven upload.
                    status="created" if remote_state == "created" else "failed",
                    remote_task_id=task_id,
                    error_code=exc.code,
                )
            return VisionExtractionResult(diagnostics=(exc.code,), error_code=exc.code)
class GatewayVisionAdapter:
    """Render failed pages locally, then call only the Rust credential gateway."""

    def __init__(
        self,
        provider: Any,
        *,
        image_renderer: Callable[[bytes], bytes] | None = None,
    ):
        self.provider = provider
        self.image_renderer = image_renderer or default_pdf_to_png

    def extract(self, company, filing, pages, config, *, cancel_check=None):
        try:
            _check_request(config, pages, filing)
            if not config.configured_model_id:
                raise VisionAdapterError("VISION_PROVIDER_UNSUPPORTED")
            facts: list[FinancialFact] = []
            refs: list[EvidenceRef] = []
            diagnostics: list[str] = []
            page_errors: list[str] = []
            for page in pages:
                if cancel_check and cancel_check():
                    raise VisionAdapterError("VISION_CANCELLED")
                image = self.image_renderer(page.pdf_bytes)
                if not image:
                    raise VisionAdapterError("VISION_IMAGE_RENDER_FAILED")
                prompt = (
                    "Return JSON only with this shape: "
                    "{facts:[{concept,value,currency,unit_scale,statement,scope,"
                    "period_end,original_page,raw_text}]}. "
                    f"Use only original_page {page.original_page}; prefer consolidated "
                    "scope, preserve the stated period and currency, and omit uncertain values."
                )
                try:
                    structured = self.provider.generate_vision(
                        "Extract candidate financial facts from one approved filing page. "
                        "Do not infer missing values or use outside knowledge.",
                        prompt,
                        image,
                    )
                except ProviderError as exc:
                    if exc.code == "MODEL_TIMEOUT":
                        raise VisionAdapterError("VISION_TIMEOUT") from exc
                    if exc.code in {"MODEL_UNAUTHORIZED", "MODEL_CREDENTIAL_MISSING"}:
                        raise VisionAdapterError("VISION_UNAUTHORIZED") from exc
                    if exc.code == "MODEL_RATE_LIMITED":
                        raise VisionAdapterError("VISION_RATE_LIMITED") from exc
                    raise VisionAdapterError("VISION_MODEL_ERROR") from exc
                structured.pop("_response_meta", None)
                candidate = parse_vision_json(structured, company, filing, (page,))
                facts.extend(candidate.facts)
                refs.extend(candidate.evidence)
                diagnostics.extend(candidate.diagnostics)
                if candidate.error_code:
                    page_errors.append(candidate.error_code)
            if not facts:
                return VisionExtractionResult(
                    (),
                    (),
                    tuple(diagnostics) or ("VISION_NO_CANDIDATES",),
                    page_errors[0] if page_errors else "VISION_NO_CANDIDATES",
                )
            return VisionExtractionResult(
                tuple(facts),
                tuple(refs),
                tuple(diagnostics) + ("VISION_MODEL_GATEWAY_COMPLETED",),
                page_errors[0] if page_errors else None,
            )
        except VisionAdapterError as exc:
            return VisionExtractionResult(diagnostics=(exc.code,), error_code=exc.code)


class VisionTaskCoordinator:
    """Bounded, deterministic orchestration for approved failed pages."""

    def __init__(
        self,
        adapter: VisionFinancialSourceAdapter,
        *,
        journal: VisionTaskJournal | None = None,
        max_workers: int = 2,
        progress: Callable[[int, int], None] | None = None,
    ):
        self.adapter = adapter
        self.journal = journal
        self.max_workers = max(1, min(2, int(max_workers)))
        self.progress = progress
        self._completed_results: dict[str, VisionExtractionResult] = {}

    def extract(
        self,
        company: Company,
        filing: FilingDocument,
        pages: Sequence[VisionPageRequest],
        config: VisionFallbackConfig,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> VisionExtractionResult:
        try:
            unique: dict[tuple[int, str], VisionPageRequest] = {}
            for page in sorted(pages, key=lambda item: (item.original_page, item.content_hash)):
                unique.setdefault((page.original_page, page.content_hash), page)
            ordered_pages = tuple(unique.values())
            config.validate()
            if not config.enabled:
                raise VisionAdapterError("VISION_DISABLED")
            if not ordered_pages:
                raise VisionAdapterError("VISION_NO_FAILED_PAGES")
            if len(ordered_pages) > config.max_pages:
                raise VisionAdapterError("VISION_PAGE_LIMIT")
            if sum(len(page.pdf_bytes) for page in ordered_pages) > config.max_bytes:
                raise VisionAdapterError("VISION_SIZE_LIMIT")
            provider_identity = _provider_identity(config)
            task_key = vision_task_key(
                provider_identity,
                filing.content_hash,
                ordered_pages,
                namespace="batch",
            )
            prior = self.journal.get_vision_task(task_key) if self.journal is not None else None
            cached = self._completed_results.get(task_key)
            if cached is not None:
                return cached
            load_result = getattr(self.journal, "get_vision_result", None)
            if callable(load_result):
                persisted = load_result(task_key)
                restored = _deserialize_vision_result(persisted) if persisted else None
                if restored is not None:
                    cached = VisionExtractionResult(
                        restored.facts,
                        restored.evidence,
                        restored.diagnostics + ("VISION_RESULT_CACHE_HIT",),
                        None,
                    )
                    self._completed_results[task_key] = cached
                    return cached
            _check_request(config, ordered_pages, filing)
            if prior and prior.get("status") in {"created", "uploaded", "running"} and config.provider != "mineru_flash":
                return VisionExtractionResult(
                    diagnostics=("VISION_RECOVERY_BLOCKED",),
                    error_code="VISION_RECOVERY_BLOCKED",
                )
            if self.journal is not None:
                self.journal.save_vision_task(
                    task_key,
                    company_cik=filing.company_cik,
                    document_id=filing.document_id,
                    provider=provider_identity,
                    page_hashes=tuple(page.content_hash for page in ordered_pages),
                    page_numbers=tuple(page.original_page for page in ordered_pages),
                    status="running",
                )
            # The coordinator performs one approval for the complete manifest;
            # worker calls use an internal no-op callback and cannot re-prompt.
            worker_config = replace(config, approve_upload=lambda _summary: True)
            results: dict[int, VisionExtractionResult] = {}
            errors: list[str] = []
            internal_cancel = threading.Event()

            def combined_cancel() -> bool:
                return internal_cancel.is_set() or bool(cancel_check and cancel_check())

            executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="vision-page")
            futures = {
                executor.submit(
                    self.adapter.extract,
                    company,
                    filing,
                    (page,),
                    worker_config,
                    cancel_check=combined_cancel,
                ): index
                for index, page in enumerate(ordered_pages)
            }
            try:
                timeout = max(0.01, float(config.timeout_seconds))
                for future in as_completed(futures, timeout=timeout):
                    index = futures[future]
                    if cancel_check and cancel_check():
                        raise VisionAdapterError("VISION_CANCELLED")
                    try:
                        results[index] = future.result()
                    except VisionAdapterError as exc:
                        results[index] = VisionExtractionResult(
                            diagnostics=(exc.code,), error_code=exc.code
                        )
                    except Exception:
                        results[index] = VisionExtractionResult(
                            diagnostics=("VISION_PROVIDER_ERROR",),
                            error_code="VISION_PROVIDER_ERROR",
                        )
                    if self.progress is not None:
                        self.progress(len(results), len(ordered_pages))
            except FuturesTimeoutError:
                errors.append("VISION_TIMEOUT")
                internal_cancel.set()
            except VisionAdapterError as exc:
                errors.append(exc.code)
                internal_cancel.set()
            finally:
                if errors:
                    internal_cancel.set()
                    for future in futures:
                        future.cancel()
                # Transport calls are themselves deadline-bounded. Waiting
                # here guarantees no worker can mutate the journal after this
                # method has returned a timeout/cancellation result.
                executor.shutdown(wait=True, cancel_futures=True)
            facts: list[FinancialFact] = []
            evidence: list[EvidenceRef] = []
            diagnostics: list[str] = []
            for index in range(len(ordered_pages)):
                result = results.get(index)
                if result is None:
                    errors.append("VISION_CANCELLED")
                    continue
                facts.extend(result.facts)
                evidence.extend(result.evidence)
                diagnostics.extend(result.diagnostics)
                if result.error_code:
                    errors.append(result.error_code)
            error_code = errors[0] if errors else None
            if not facts and error_code is None:
                error_code = "VISION_NO_CANDIDATES"
            if self.journal is not None:
                self.journal.save_vision_task(
                    task_key,
                    company_cik=filing.company_cik,
                    document_id=filing.document_id,
                    provider=provider_identity,
                    page_hashes=tuple(page.content_hash for page in ordered_pages),
                    page_numbers=tuple(page.original_page for page in ordered_pages),
                    status="failed" if error_code and not facts else "partial" if error_code else "completed",
                    error_code=error_code or "",
                )
            if error_code is None and facts:
                completed = VisionExtractionResult(
                    tuple(facts), tuple(evidence), tuple(diagnostics), None
                )
                self._completed_results[task_key] = completed
                save_result = getattr(self.journal, "save_vision_result", None)
                if callable(save_result):
                    save_result(task_key, _serialize_vision_result(completed))
                return completed
            if error_code and not facts:
                diagnostics.append(error_code)
            return VisionExtractionResult(tuple(facts), tuple(evidence), tuple(diagnostics), error_code)
        except VisionAdapterError as exc:
            return VisionExtractionResult(diagnostics=(exc.code,), error_code=exc.code)


def default_pdf_to_png(pdf_bytes: bytes) -> bytes:
    """Render the first PDF page in memory with bounded pixels and no temp file."""
    try:
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(pdf_bytes)
        if len(document) < 1:
            raise VisionAdapterError("VISION_IMAGE_RENDER_FAILED")
        page = document[0]
        bitmap = page.render(scale=1.5, rev_byteorder=True)
        image = bitmap.to_pil()
        width, height = image.size
        if width > 4096 or height > 4096 or width * height > 12_000_000:
            image.thumbnail((4096, 4096))
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        data = output.getvalue()
        if len(data) > 8 * 1024 * 1024:
            raise VisionAdapterError("VISION_IMAGE_SIZE_LIMIT")
        try:
            page.close()
            document.close()
        except Exception:
            pass
        return data
    except VisionAdapterError:
        raise
    except Exception as exc:
        raise VisionAdapterError("VISION_IMAGE_RENDER_FAILED") from exc


def parse_vision_markdown(
    markdown: str,
    company: Company,
    filing: FilingDocument,
    pages: Sequence[VisionPageRequest],
) -> VisionExtractionResult:
    """Conservatively turn labelled markdown rows into candidate facts."""
    if not markdown or not pages:
        return VisionExtractionResult(error_code="VISION_EMPTY_MARKDOWN", diagnostics=("VISION_EMPTY_MARKDOWN",))
    scale = 1.0
    compact = markdown.lower()
    if "million" in compact or "百万" in markdown:
        scale = 1_000_000.0
    elif "thousand" in compact or "千元" in markdown:
        scale = 1_000.0
    labels = {
        "revenue": r"(?:revenue|operating revenue|营业收入)",
        "net_income": r"(?:net income|net profit|净利润)",
        "operating_cash_flow": r"(?:operating cash flow|cash flows? .*operating activities|经营活动.*现金流)",
        "assets": r"(?:total assets|资产总计)",
        "liabilities": r"(?:total liabilities|负债合计)",
        "total_equity": r"(?:total equity|total shareholders.? equity|所有者权益合计)",
    }
    facts: list[FinancialFact] = []
    evidence: list[EvidenceRef] = []
    first_page = pages[0]
    for concept, label in labels.items():
        match = re.search(rf"(?im)^{label}[^\n]*?([\(\-]?\d[\d,]*(?:\.\d+)?\)?)(?:\s|$)", markdown)
        if not match:
            continue
        token = match.group(1).replace(",", "")
        negative = token.startswith("(") and token.endswith(")")
        value = float(token.strip("()")) * scale
        if negative:
            value = -value
        statement = "cash_flow" if concept == "operating_cash_flow" else "balance_sheet" if concept in {"assets", "liabilities", "total_equity"} else "income_statement"
        fact = FinancialFact(
            hashlib_id := f"{filing.document_id}:{concept}:{first_page.original_page}:{first_page.content_hash[:16]}:{value}",
            company.security_id, concept, concept,
            value, company.reporting_currency, int(filing.period_end[:4]), filing.fiscal_period,
            filing.form_type, _vision_period_start(filing.fiscal_period, filing.period_end, statement),
            filing.period_end, filing.filed_at, filing.accession_number, filing.source_url,
            scope="consolidated", entity=company.name, market=company.market, statement=statement,
            period_start=_vision_period_start(filing.fiscal_period, filing.period_end, statement),
            consolidated_scope="consolidated", currency=company.reporting_currency,
            unit_scale=scale, source_document=filing.primary_document, source_page=first_page.original_page,
            source_bbox=None, raw_text=match.group(0), parser_version="vision-candidate-v1",
        )
        facts.append(fact)
        evidence.append(EvidenceRef(
            f"fact:{fact.fact_id}", filing.document_id, filing.source_url,
            filing.primary_document, f"page:{first_page.original_page}", match.group(0),
            filing.filed_at, first_page.content_hash,
        ))
    if not facts:
        return VisionExtractionResult(error_code="VISION_NO_CANDIDATES", diagnostics=("VISION_NO_CANDIDATES",))
    return VisionExtractionResult(tuple(facts), tuple(evidence), ("VISION_CANDIDATES_ONLY",))


def parse_vision_json(payload: Any, company: Company, filing: FilingDocument, pages: Sequence[VisionPageRequest]) -> VisionExtractionResult:
    """Parse the strict custom-vision schema into bounded candidate facts."""
    rows = payload.get("facts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return VisionExtractionResult(error_code="VISION_STRUCTURED_JSON_REQUIRED", diagnostics=("VISION_STRUCTURED_JSON_REQUIRED",))
    allowed = {"revenue", "net_income", "operating_cash_flow", "assets", "liabilities", "equity", "total_equity"}
    scales = {1.0, 1_000.0, 10_000.0, 100_000.0, 1_000_000.0}
    facts: list[FinancialFact] = []
    refs: list[EvidenceRef] = []
    pages_by_number = {page.original_page: page for page in pages}
    for row in rows:
        if not isinstance(row, dict) or row.get("concept") not in allowed:
            continue
        try:
            page_no = int(row.get("original_page"))
            page = pages_by_number[page_no]
            value = float(row["value"])
            scale = float(row.get("unit_scale", 1.0))
            end_date = str(row.get("period_end", ""))
            if scale not in scales or str(row.get("scope", "")) != "consolidated" or end_date != filing.period_end:
                continue
            currency = str(row.get("currency", "")).upper()
            if currency != company.reporting_currency.upper():
                continue
            statement = str(row.get("statement", ""))
            if statement not in {"income_statement", "balance_sheet", "cash_flow"}:
                continue
            raw_text = str(row.get("raw_text", "")).strip()
            if not raw_text:
                continue
            value *= scale
        except (KeyError, TypeError, ValueError):
            continue
        fact = FinancialFact(
            f"{filing.document_id}:{row['concept']}:{page_no}:{page.content_hash[:16]}:{value}", company.security_id, row["concept"], row["concept"], value,
            currency, int(filing.period_end[:4]), filing.fiscal_period, filing.form_type,
            _vision_period_start(filing.fiscal_period, filing.period_end, statement), filing.period_end,
            filing.filed_at, filing.accession_number, filing.source_url, scope="consolidated", entity=company.name,
            market=company.market, statement=statement, period_start=_vision_period_start(filing.fiscal_period, filing.period_end, statement),
            consolidated_scope="consolidated", currency=currency, unit_scale=scale, source_document=filing.primary_document,
            source_page=page_no, raw_text=raw_text, parser_version="vision-json-v1",
        )
        facts.append(fact)
        refs.append(EvidenceRef(f"fact:{fact.fact_id}", filing.document_id, filing.source_url, filing.primary_document, f"page:{page_no}", raw_text, filing.filed_at, page.content_hash))
    if not facts:
        return VisionExtractionResult(error_code="VISION_NO_CANDIDATES", diagnostics=("VISION_NO_CANDIDATES",))
    return VisionExtractionResult(tuple(facts), tuple(refs), ("VISION_CANDIDATES_ONLY",))
