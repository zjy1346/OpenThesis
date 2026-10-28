"""Bounded vision fallback for approved failed financial pages.

Configured models stay behind the Rust credential gateway. The separate MinerU
Flash path is token-free and can run only after an explicit per-run page preview
approval; neither path exposes long-term credentials to Python.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import date, timedelta
from hashlib import sha256
from io import BytesIO
import json
import math
import multiprocessing as mp
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from .financial_cloud_proof import CloudUnitContext
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .domain import Company, EvidenceRef, FilingDocument, FinancialFact
from .financial_contracts import QuantityKind, SourceAmount
from .financial_gateway_proof import GATEWAY_FACT_VERSION, gateway_source_id
from .financial_cloud_proof import canonical_cloud_source, cloud_source_id
from .financial_table_text import CLOUD_TABLE_VERSION
from .model_scheduler import shared_model_scheduler
from .providers import ProviderError
from .resilience.contracts import Usage


VISION_MAX_PAGES = 20
VISION_MAX_BYTES = 10 * 1024 * 1024
VISION_TASK_SCHEMA_VERSION = "vision-task-v1"
MINERU_UPLOAD_PROTOCOL_VERSION = "oss-put-ocr-table-v4"

_VISION_CONCEPT_STATEMENTS = {
    "revenue": "income_statement",
    "net_income": "income_statement",
    "profit_before_tax": "income_statement",
    "profit_after_tax": "income_statement",
    "operating_income": "income_statement",
    "gross_profit": "income_statement",
    "cost_of_revenue": "income_statement",
    "total_operating_cost": "income_statement",
    "operating_cash_flow": "cash_flow",
    "capital_expenditure": "cash_flow",
    "assets": "balance_sheet",
    "liabilities": "balance_sheet",
    "equity": "balance_sheet",
    "total_equity": "balance_sheet",
}

_VISION_UNIT_SCALES = {
    1.0, 1_000.0, 10_000.0, 100_000.0, 1_000_000.0, 100_000_000.0, 1_000_000_000.0,
}


def _compact_source_text(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.casefold())


def _vision_source_phrase_present(raw_text: str, phrase: str) -> bool:
    compact_phrase = _compact_source_text(phrase)
    return bool(compact_phrase and compact_phrase in _compact_source_text(raw_text))


def _vision_unit_scale_is_explicit(unit_text: str, scale: float) -> bool:
    text = _compact_source_text(unit_text)
    if not text:
        return False
    terms = {
        1.0: ("yuan", "元", "dollar", "港元", "港幣", "港币", "人民币", "人民幣", "cny", "rmb", "hkd", "usd"),
        1_000.0: ("thousand", "千元", "千人民币", "千人民幣", "千港元", "千美元", "000s"),
        10_000.0: ("ten thousand", "万元", "万人民币", "万人民幣", "万港元", "万美元"),
        100_000.0: ("hundred thousand", "十万元", "十万港元", "十万美元"),
        1_000_000.0: ("million", "百万", "百万元", "百万港元", "百万美元", "000000s"),
        100_000_000.0: ("hundred million", "亿元", "亿港元", "亿美元"),
        1_000_000_000.0: ("billion", "十亿元", "十亿港元", "十亿美元"),
    }
    if scale == 1.0 and any(
        marker in text
        for marker in ("thousand", "千", "tenmillion", "万", "million", "百万", "hundredmillion", "亿", "billion", "十亿", "000s")
    ):
        return False
    return any(_compact_source_text(term) in text for term in terms.get(scale, ()))


def _vision_currency_is_explicit(unit_text: str, currency: str) -> bool:
    text = _compact_source_text(unit_text)
    aliases = {
        "CNY": ("cny", "rmb", "人民币", "人民幣", "yuan", "元", "¥", "￥"),
        "HKD": ("hkd", "hk$", "港元", "港币", "港幣"),
        "USD": ("usd", "us$", "美元", "dollar", "$"),
    }
    return any(_compact_source_text(term) in text for term in aliases.get(currency.upper(), ()))


def _parse_vision_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().replace(",", "")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return -abs(parsed) if negative else parsed


def _vision_decimal_source_text(value: Any) -> str:
    """Return an exact JSON numeric token, rejecting already-rounded floats."""
    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise ValueError("VISION_SOURCE_DECIMAL_REQUIRED")
    if isinstance(value, (int, Decimal, str)):
        token = str(value).strip()
        # SourceAmount performs the authoritative syntax and precision checks.
        if token:
            return token
    raise ValueError("VISION_SOURCE_DECIMAL_REQUIRED")


def _vision_unit_evidence_id(
    document_id: str, page_no: int, content_hash: str, unit_text: str,
) -> str:
    digest = sha256(
        f"{document_id}|{page_no}|{content_hash}|unit|{unit_text}".encode("utf-8")
    ).hexdigest()[:32]
    return f"unit:{digest}"


def _unverified_vision_observation(
    row: Mapping[str, Any],
    filing: FilingDocument,
    pages_by_number: Mapping[int, VisionPageRequest],
) -> EvidenceRef | None:
    """Retain a bounded transcription without making it a financial fact."""
    try:
        page_no = int(row.get("original_page"))
        page = pages_by_number[page_no]
    except (TypeError, ValueError, KeyError):
        return None
    label = str(row.get("label") or row.get("concept") or "").strip()[:120]
    raw_text = str(row.get("raw_text", "")).strip()[:2000]
    value = row.get("value_text", row.get("reported_value", row.get("value")))
    value_text = str(value).strip()[:160] if value is not None else ""
    if not label or not raw_text or not value_text or not re.search(r"\d", value_text):
        return None
    digest = sha256(
        f"{filing.document_id}|{page_no}|{page.content_hash}|{label}|{value_text}|{raw_text}".encode("utf-8")
    ).hexdigest()[:32]
    return EvidenceRef(
        f"observation:{digest}", filing.document_id, filing.source_url,
        f"Unverified visual observation (not used in calculations): {label}",
        f"page:{page_no}",
        f"Displayed value: {value_text}\nSource transcription: {raw_text}",
        filing.filed_at, page.content_hash,
    )


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

    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        retry_after_seconds: float = 0.0,
    ):
        self.code = code
        self.http_status = http_status if isinstance(http_status, int) and not isinstance(http_status, bool) else None
        try:
            retry_after = float(retry_after_seconds)
        except (TypeError, ValueError, OverflowError):
            retry_after = 0.0
        self.retry_after_seconds = min(300.0, retry_after) if math.isfinite(retry_after) and retry_after >= 0 else 0.0
        super().__init__(code)


def _run_pdfium_isolated(
    worker: Callable[..., Any],
    *args: Any,
    timeout_seconds: float = 45.0,
    cancel_check: Callable[[], bool] | None = None,
    diagnostic_callback=None,
    operation="",
    backend="pdfium",
    page_range=None,
) -> tuple[Any | None, str | None]:
    """Compatibility seam; both PDF backends share structured, killable IPC."""
    from .pdf_worker import run_pdf_worker
    return run_pdf_worker(worker, *args, timeout_seconds=timeout_seconds, cancel_check=cancel_check,
        diagnostic_callback=diagnostic_callback, operation=operation, backend=backend, page_range=page_range)


@dataclass(frozen=True, slots=True)
class VisionFallbackConfig:
    enabled: bool = False
    consent: bool = False
    provider: str = "configured_model"
    configured_model_id: str = ""
    connection_id: str = ""
    configuration_version: int = 1
    timeout_seconds: float = 60.0
    max_pages: int = VISION_MAX_PAGES
    max_bytes: int = VISION_MAX_BYTES
    approve_upload: Callable[[Mapping[str, Any]], bool] | None = None
    language: str = "auto"
    require_page_approval: bool = False
    approval_mode: str = "review_each_plan"
    # Host-only original policy value. Queue/task remainders are execution
    # allowances, not configuration changes that invalidate saved candidates.
    configured_timeout_seconds: float | None = None

    def validate(self) -> None:
        if (isinstance(self.configuration_version, bool)
                or not isinstance(self.configuration_version, int) or self.configuration_version < 1):
            raise VisionAdapterError("VISION_CONFIGURATION_VERSION_INVALID")
        timeout = self.timeout_seconds
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError, OverflowError):
            timeout_value = math.nan
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float, Decimal))
            or not math.isfinite(timeout_value)
            or timeout_value <= 0
        ):
            raise VisionAdapterError("VISION_TIMEOUT_INVALID")
        if self.configured_timeout_seconds is not None and (
                isinstance(self.configured_timeout_seconds, bool)
                or not isinstance(self.configured_timeout_seconds, (int, float, Decimal))
                or not math.isfinite(float(self.configured_timeout_seconds))
                or float(self.configured_timeout_seconds) <= 0):
            raise VisionAdapterError("VISION_TIMEOUT_INVALID")
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
        if self.approval_mode not in {"review_each_plan", "approve_current_research"}:
            raise VisionAdapterError("VISION_APPROVAL_MODE_UNSUPPORTED")
        if not self.require_page_approval or self.approve_upload is None:
            raise VisionAdapterError("VISION_UPLOAD_APPROVAL_REQUIRED")


@dataclass(frozen=True, slots=True)
class VisionPageRequest:
    original_page: int
    pdf_bytes: bytes
    source_url: str = ""
    source_document: str = ""
    content_hash: str = ""
    # Locally observed document-wide presentation declarations. They are not
    # uploaded to the provider and cannot stand in for a table's own unit.
    unit_contexts: tuple[CloudUnitContext, ...] = ()

    def __post_init__(self) -> None:
        if self.original_page < 1:
            raise ValueError("original_page must be positive")
        digest = sha256(self.pdf_bytes).hexdigest()
        if self.content_hash and self.content_hash != digest:
            raise ValueError("page_hash_mismatch")
        if not self.content_hash:
            object.__setattr__(self, "content_hash", digest)
        contexts = tuple(self.unit_contexts)
        if any(not isinstance(item, CloudUnitContext) for item in contexts):
            raise ValueError("VISION_UNIT_CONTEXT_INVALID")
        object.__setattr__(self, "unit_contexts", contexts)


FinancialPageRequest = VisionPageRequest


@dataclass(frozen=True, slots=True)
class VisionUploadPlan:
    """Immutable, in-memory manifest for one approved failed-page upload.

    The plan is the authorization boundary: its identity includes the provider
    configuration and the exact original-page/hash pairs.  PDF bytes are kept
    only by the page requests during the active call; ``safe_summary`` never
    exposes them or any source URL/path.
    """

    provider: str
    filing_hash: str
    source_document: str
    pages: tuple[VisionPageRequest, ...] = ()
    max_pages: int = VISION_MAX_PAGES
    max_bytes: int = VISION_MAX_BYTES
    configured_model_id: str = ""
    configuration_version: int = 1
    page_hashes: tuple[str, ...] | None = None
    plan_id: str = field(init=False)
    total_bytes: int = field(init=False)

    def __post_init__(self) -> None:
        if not str(self.filing_hash).strip():
            raise ValueError("VISION_FILING_HASH_REQUIRED")
        if self.max_pages < 1 or self.max_pages > VISION_MAX_PAGES:
            raise ValueError("VISION_PAGE_LIMIT")
        if self.max_bytes < 1 or self.max_bytes > VISION_MAX_BYTES:
            raise ValueError("VISION_SIZE_LIMIT")
        unique: dict[tuple[int, str], VisionPageRequest] = {}
        page_hashes_by_number: dict[int, str] = {}
        for page in sorted(tuple(self.pages), key=lambda item: (item.original_page, item.content_hash)):
            previous_hash = page_hashes_by_number.get(page.original_page)
            if previous_hash is not None and previous_hash != page.content_hash:
                raise ValueError("VISION_PAGE_AMBIGUOUS")
            page_hashes_by_number[page.original_page] = page.content_hash
            unique.setdefault((page.original_page, page.content_hash), page)
        ordered = tuple(unique.values())
        hashes = tuple(page.content_hash for page in ordered)
        if self.page_hashes is not None and tuple(self.page_hashes) != hashes:
            raise ValueError("VISION_PAGE_HASH_MISMATCH")
        if not ordered:
            raise ValueError("VISION_NO_FAILED_PAGES")
        total_bytes = sum(len(page.pdf_bytes) for page in ordered)
        if len(ordered) > self.max_pages:
            raise ValueError("VISION_PAGE_LIMIT")
        if total_bytes > self.max_bytes:
            raise ValueError("VISION_SIZE_LIMIT")
        object.__setattr__(self, "pages", ordered)
        object.__setattr__(self, "page_hashes", hashes)
        object.__setattr__(self, "total_bytes", total_bytes)
        provider_identity = self.provider
        if self.configured_model_id:
            provider_identity = (
                f"{self.provider}:{self.configured_model_id}:"
                f"configuration-{self.configuration_version}"
            )
        object.__setattr__(
            self,
            "plan_id",
            vision_task_key(
                provider_identity,
                self.filing_hash,
                ordered,
                namespace="plan",
            ),
        )

    @property
    def page_numbers(self) -> tuple[int, ...]:
        return tuple(page.original_page for page in self.pages)

    def safe_summary(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "provider": self.provider,
            "configured_model_id": self.configured_model_id,
            "configuration_version": self.configuration_version,
            "filing_hash": self.filing_hash,
            "source_document": re.split(r"[\\/]", str(self.source_document or ""))[-1],
            "pages": self.page_numbers,
            "document_hashes": self.page_hashes,
            "total_bytes": self.total_bytes,
            "max_pages": self.max_pages,
            "max_bytes": self.max_bytes,
        }

    def validate(self) -> None:
        """Revalidate the immutable manifest immediately before upload.

        ``frozen=True`` protects ordinary callers, but authorization must also
        defend against reflective mutation or a stale plan object crossing a
        configuration boundary.  Recomputing the identity makes that check
        explicit at the network seam.
        """

        if not self.pages:
            raise ValueError("VISION_NO_FAILED_PAGES")
        expected_pages = tuple(
            sorted(self.pages, key=lambda item: (item.original_page, item.content_hash))
        )
        page_hashes_by_number: dict[int, str] = {}
        for page in expected_pages:
            previous_hash = page_hashes_by_number.get(page.original_page)
            if previous_hash is not None and previous_hash != page.content_hash:
                raise ValueError("VISION_PAGE_AMBIGUOUS")
            page_hashes_by_number[page.original_page] = page.content_hash
        expected_unique = tuple({(page.original_page, page.content_hash): page for page in expected_pages}.values())
        if expected_unique != self.pages:
            raise ValueError("VISION_UPLOAD_PLAN_CHANGED")
        if not str(self.filing_hash).strip():
            raise ValueError("VISION_FILING_HASH_REQUIRED")
        expected_hashes = tuple(page.content_hash for page in expected_pages)
        if expected_pages != self.pages or expected_hashes != self.page_hashes:
            raise ValueError("VISION_UPLOAD_PLAN_CHANGED")
        total_bytes = sum(len(page.pdf_bytes) for page in self.pages)
        if len(self.pages) > self.max_pages:
            raise ValueError("VISION_PAGE_LIMIT")
        if total_bytes > self.max_bytes:
            raise ValueError("VISION_SIZE_LIMIT")
        provider_identity = self.provider
        if self.configured_model_id:
            provider_identity = (
                f"{self.provider}:{self.configured_model_id}:"
                f"configuration-{self.configuration_version}"
            )
        expected_plan_id = vision_task_key(
            provider_identity,
            self.filing_hash,
            self.pages,
            namespace="plan",
        )
        if self.plan_id != expected_plan_id or self.total_bytes != total_bytes:
            raise ValueError("VISION_UPLOAD_PLAN_CHANGED")


@dataclass(frozen=True, slots=True)
class VisionExtractionResult:
    facts: tuple[FinancialFact, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    diagnostics: tuple[str, ...] = ()
    error_code: str | None = None
    retry_after_seconds: float = 0.0

    def __post_init__(self) -> None:
        try:
            value = float(self.retry_after_seconds)
        except (TypeError, ValueError, OverflowError):
            value = 0.0
        if isinstance(self.retry_after_seconds, bool) or not math.isfinite(value) or value < 0:
            value = 0.0
        object.__setattr__(self, "retry_after_seconds", min(300.0, value))

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
    if config.provider == "mineru_flash":
        return f"mineru_flash:{MINERU_UPLOAD_PROTOCOL_VERSION}"
    return config.provider


def _serialize_vision_result(result: VisionExtractionResult) -> dict[str, Any]:
    return {
        "facts": [fact.to_dict() for fact in result.facts],
        "evidence": [item.to_dict() for item in result.evidence],
        "diagnostics": list(result.diagnostics),
        "error_code": result.error_code,
        "retry_after_seconds": result.retry_after_seconds,
    }


def _deserialize_vision_result(payload: Mapping[str, Any]) -> VisionExtractionResult | None:
    try:
        def bbox_fields(item, key):
            value = dict(item)
            if value.get(key) is not None:
                box = value[key]
                if (not isinstance(box, (list, tuple)) or len(box) != 4
                        or any(isinstance(point, bool) or not isinstance(point, (int, float))
                               or not math.isfinite(point) for point in box)):
                    raise ValueError("VISION_GEOMETRY_INVALID")
                value[key] = tuple(box)
            return value
        facts = tuple(FinancialFact(**bbox_fields(item, "source_bbox")) for item in payload.get("facts", ()))
        evidence = tuple(EvidenceRef(**bbox_fields(item, "bbox")) for item in payload.get("evidence", ()))
        diagnostics = tuple(str(item) for item in payload.get("diagnostics", ()))
        error_code = payload.get("error_code")
        retry_after = payload.get("retry_after_seconds", 0.0)
        if error_code is not None or (not facts and not any(ref.evidence_id.startswith("cell-map:") for ref in evidence)):
            return None
        return VisionExtractionResult(facts, evidence, diagnostics, None, retry_after)
    except (TypeError, ValueError):
        return None

@dataclass(frozen=True, slots=True)
class VisionHttpResponse:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON object key")
                result[key] = value
            return result

        def reject_nonfinite(token: str) -> Any:
            raise ValueError(f"nonfinite JSON constant: {token}")

        try:
            # Keep source number tokens as Decimal; a binary float here would
            # make the later source-to-canonical lineage unverifiable.
            return json.loads(
                self.body.decode("utf-8"),
                parse_float=Decimal,
                parse_constant=reject_nonfinite,
                object_pairs_hook=reject_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
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
        request_headers = dict(headers or {})
        # urllib treats every body without an explicit content type as a form
        # submission.  That implicit header changes the canonical request used
        # by OSS/S3 pre-signed URLs and produces SignatureDoesNotMatch.  An
        # empty Content-Type is intentional: it suppresses urllib's default
        # without inventing a MIME value that was not part of the signature.
        if (
            method.upper() == "PUT"
            and body is not None
            and not any(str(key).casefold() == "content-type" for key in request_headers)
        ):
            request_headers["Content-Type"] = ""
        request = Request(url, data=body, headers=request_headers, method=method)
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


def _check_request(
    config: VisionFallbackConfig,
    pages: Sequence[VisionPageRequest],
    filing: FilingDocument | None = None,
    *,
    plan: VisionUploadPlan | None = None,
) -> VisionUploadPlan:
    config.validate()
    if not config.enabled:
        raise VisionAdapterError("VISION_DISABLED")
    if plan is None:
        try:
            plan = VisionUploadPlan(
                provider=config.provider,
                configured_model_id=config.configured_model_id,
                configuration_version=config.configuration_version,
                filing_hash=filing.content_hash if filing else "",
                source_document=filing.primary_document if filing else "",
                pages=tuple(pages),
                max_pages=config.max_pages,
                max_bytes=config.max_bytes,
            )
        except ValueError as exc:
            raise VisionAdapterError(str(exc)) from exc
    else:
        if tuple(pages) != plan.pages:
            raise VisionAdapterError("VISION_UPLOAD_PLAN_CHANGED")
        if plan.provider != config.provider:
            raise VisionAdapterError("VISION_UPLOAD_PLAN_CHANGED")
        if plan.configured_model_id != config.configured_model_id or plan.configuration_version != config.configuration_version:
            raise VisionAdapterError("VISION_UPLOAD_PLAN_CHANGED")
        if plan.max_pages != config.max_pages or plan.max_bytes != config.max_bytes:
            raise VisionAdapterError("VISION_UPLOAD_PLAN_CHANGED")
        if filing is not None and plan.filing_hash != filing.content_hash:
            raise VisionAdapterError("VISION_UPLOAD_PLAN_CHANGED")
    try:
        plan.validate()
    except ValueError as exc:
        code = str(exc)
        if code not in {"VISION_NO_FAILED_PAGES", "VISION_PAGE_LIMIT", "VISION_SIZE_LIMIT", "VISION_FILING_HASH_REQUIRED", "VISION_PAGE_AMBIGUOUS"}:
            code = "VISION_UPLOAD_PLAN_CHANGED"
        raise VisionAdapterError(code) from exc
    summary = plan.safe_summary()
    # Keep the historical friendly provider field for UI compatibility while
    # the immutable plan id binds the full provider identity.
    summary["provider"] = (
        config.provider if config.provider == "mineru_flash" else config.configured_model_id
    )
    approver = config.approve_upload
    if approver is not None and not approver(summary):
        raise VisionAdapterError("VISION_UPLOAD_NOT_APPROVED")
    return plan



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

    @staticmethod
    def _retry_after(response: VisionHttpResponse) -> float | None:
        value = next(
            (item for key, item in response.headers.items() if str(key).casefold() == "retry-after"),
            None,
        )
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return min(300.0, seconds)

    def _remaining(self, deadline: float, cancel_check: Callable[[], bool] | None) -> float:
        if cancel_check and cancel_check():
            raise VisionAdapterError("VISION_CANCELLED")
        remaining = deadline - self.clock()
        if not math.isfinite(remaining) or remaining <= 0:
            raise VisionAdapterError("VISION_TIMEOUT")
        return remaining

    def _usage(self, recovery_context: Any, *, calls: int = 0, pages: int = 0) -> None:
        if recovery_context is None:
            return
        checkpoint = getattr(recovery_context, "checkpoint", None)
        if callable(checkpoint):
            checkpoint()
        consume = getattr(recovery_context, "consume", None)
        if callable(consume):
            consume(Usage(calls=calls, pages=pages))

    def _request(
        self,
        method: str,
        url: str,
        *,
        deadline: float,
        recovery_context: Any = None,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> VisionHttpResponse:
        """Issue a deadline-bound request; only an unjournaled GET may retry once."""

        method = method.upper()
        safe_url = _https_url(url)
        attempts = 0
        while True:
            self._remaining(deadline, cancel_check)
            self._usage(recovery_context, calls=1)
            timeout = self._remaining(deadline, cancel_check)
            try:
                response = self.transport.request(
                    method, safe_url, headers=headers, body=body, timeout=timeout,
                )
            except (VisionAdapterError, OSError, TimeoutError) as exc:
                error_code = exc.code if isinstance(exc, VisionAdapterError) else "VISION_NETWORK_ERROR"
                can_retry = (
                    method == "GET"
                    and recovery_context is None
                    and attempts == 0
                    and error_code == "VISION_NETWORK_ERROR"
                )
                if not can_retry:
                    if isinstance(exc, VisionAdapterError):
                        raise
                    raise VisionAdapterError("VISION_NETWORK_ERROR") from exc
                delay = 0.1
                if self._remaining(deadline, cancel_check) <= delay:
                    raise VisionAdapterError("VISION_TIMEOUT") from exc
                attempts += 1
                self.sleep(delay)
                continue

            if response.status >= 400:
                retry_after = self._retry_after(response) if response.status == 429 else None
                retry_delay = retry_after if retry_after is not None else 0.1
                retryable_get = (
                    method == "GET"
                    and recovery_context is None
                    and attempts == 0
                    and (500 <= response.status < 600 or (response.status == 429 and retry_after is not None))
                )
                if retryable_get:
                    if self._remaining(deadline, cancel_check) <= retry_delay:
                        raise VisionAdapterError("VISION_TIMEOUT")
                    attempts += 1
                    self.sleep(retry_delay)
                    continue
                raise VisionAdapterError(
                    _safe_error(response),
                    http_status=response.status,
                    retry_after_seconds=retry_after or 0.0,
                )

            # A late GET result must not be interpreted or published. POST is
            # different: its task id still has to be journaled by the caller.
            if method == "GET":
                self._remaining(deadline, cancel_check)
            return response

    def _save_journal(
        self,
        task_key: str,
        filing: FilingDocument,
        page: VisionPageRequest,
        *,
        status: str,
        remote_task_id: str = "",
        error_code: str = "",
    ) -> None:
        if self.journal is None:
            return
        try:
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
        except ValueError as exc:
            if str(exc) == "VISION_TASK_IDENTITY_CONFLICT":
                raise VisionAdapterError("VISION_RECOVERY_BLOCKED") from exc
            raise

    @staticmethod
    def _identity_matches(prior: Mapping[str, Any], filing: FilingDocument, page: VisionPageRequest) -> bool:
        expected = {
            "company_cik": filing.company_cik,
            "document_id": filing.document_id,
            "provider": "mineru_flash",
        }
        for key, value in expected.items():
            stored = prior.get(key)
            if stored not in (None, "") and stored != value:
                return False
        for key, value in (("page_hashes", (page.content_hash,)), ("page_numbers", (page.original_page,))):
            stored = prior.get(key)
            if stored not in (None, "", (), []):
                try:
                    if tuple(stored) != value:
                        return False
                except TypeError:
                    return False
        return True

    @staticmethod
    def _page_cache_valid(
        payload: Mapping[str, Any], company: Company, filing: FilingDocument, page: VisionPageRequest,
    ) -> VisionExtractionResult | None:
        if (
            payload.get("cache_schema") != f"{VISION_TASK_SCHEMA_VERSION}:{MINERU_UPLOAD_PROTOCOL_VERSION}:page-candidate-v1"
            or payload.get("filing_hash") != filing.content_hash
            or payload.get("page_hash") != page.content_hash
            or payload.get("original_page") != page.original_page
        ):
            return None
        restored = _deserialize_vision_result(payload)
        if restored is None:
            return None
        evidence_by_id = {evidence.evidence_id: evidence for evidence in restored.evidence}
        is_table = all(fact.parser_version == "vision-markdown-v4" for fact in restored.facts)
        if not restored.evidence or any(evidence.document_id != filing.document_id
                or evidence.content_hash != (filing.content_hash if is_table else page.content_hash)
                or (not is_table and evidence.locator != f"page:{page.original_page}")
                for evidence in restored.evidence):
            return None
        for fact in restored.facts:
            lineage = fact.normalization_lineage
            if (
                fact.company_cik != company.security_id
                or fact.source_document != filing.primary_document
                or fact.source_page != page.original_page
                or (not is_table and fact.end_date != filing.period_end)
                or fact.currency != company.reporting_currency.upper()
                or lineage is None
                or not lineage.unit_evidence_ids
                or f"fact:{fact.fact_id}" not in evidence_by_id
                or any(item not in evidence_by_id for item in lineage.unit_evidence_ids)
            ):
                return None
            try:
                normalized = SourceAmount(
                    lineage.raw_value,
                    lineage.scale,
                    QuantityKind(lineage.quantity_kind),
                    lineage.currency,
                    lineage.unit_evidence_ids,
                    value_transform=lineage.value_transform,
                ).normalize()
                if (
                    normalized.normalized_value != lineage.normalized_value
                    or lineage.currency != fact.currency
                    or float(Decimal(normalized.normalized_value)) != fact.value
                ):
                    return None
            except (InvalidOperation, TypeError, ValueError, OverflowError):
                return None
            if is_table:
                from .financial_cloud_proof import cloud_table_source_issue
                if cloud_table_source_issue(fact, evidence_by_id[f"fact:{fact.fact_id}"], evidence_by_id):
                    return None
                proof = evidence_by_id[fact.classification_evidence_ids[0]]
                source = json.loads(proof.excerpt)
                from dataclasses import asdict
                if (source.get("unit_contexts") != [asdict(item) for item in page.unit_contexts]
                        or source.get("page_hash") != page.content_hash
                        or source.get("period_end") != filing.period_end):
                    return None
        return restored

    def _cached_page_result(
        self, key: str, company: Company, filing: FilingDocument, page: VisionPageRequest,
    ) -> VisionExtractionResult | None:
        load = getattr(self.journal, "get_vision_result", None) if self.journal is not None else None
        if not callable(load):
            return None
        payload = load(key)
        if not isinstance(payload, Mapping):
            return None
        return self._page_cache_valid(payload, company, filing, page)

    def _page_markdown(self, key, filing, page, markdown=None):
        """Retain a bound raw OCR receipt even when financial parsing fails.

        Source retention is not admission. A resumed parse still uses current
        unit contexts and crosses the independent compiler. Signed download
        URLs and credentials are deliberately absent from this receipt.
        """
        load = getattr(self.journal, "get_vision_result", None)
        save = getattr(self.journal, "save_vision_result", None)
        identity = {"schema": "mineru-source-markdown-v1", "provider": "mineru_flash",
            "protocol": MINERU_UPLOAD_PROTOCOL_VERSION, "company_cik": filing.company_cik,
            "document_id": filing.document_id, "filing_hash": filing.content_hash,
            "original_page": page.original_page, "page_hash": page.content_hash}
        source_key = key + ":source-markdown"
        if markdown is None:
            payload = load(source_key) if callable(load) else None
            if not isinstance(payload, Mapping) or any(payload.get(k) != v for k, v in identity.items()):
                return None
            text = payload.get("markdown")
            if not isinstance(text, str) or len(text.encode("utf-8")) > 4 * 1024 * 1024:
                return None
            if sha256(text.encode("utf-8")).hexdigest() != payload.get("markdown_sha256"):
                return None
            return text
        if len(markdown.encode("utf-8")) > 4 * 1024 * 1024:
            raise VisionAdapterError("VISION_MARKDOWN_SIZE_LIMIT")
        if callable(save):
            save(source_key, {**identity, "markdown": markdown,
                "markdown_sha256": sha256(markdown.encode("utf-8")).hexdigest()})
        return markdown

    def _save_page_result(
        self, key: str, result: VisionExtractionResult, filing: FilingDocument, page: VisionPageRequest,
    ) -> None:
        save = getattr(self.journal, "save_vision_result", None) if self.journal is not None else None
        if not callable(save) or result.error_code is not None or not result.facts:
            return
        payload = _serialize_vision_result(result)
        payload.update({
            "cache_schema": f"{VISION_TASK_SCHEMA_VERSION}:{MINERU_UPLOAD_PROTOCOL_VERSION}:page-candidate-v1",
            "filing_hash": filing.content_hash,
            "page_hash": page.content_hash,
            "original_page": page.original_page,
        })
        save(key, payload)

    def _poll(
        self,
        task_id: str,
        *,
        deadline: float,
        recovery_context: Any = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> tuple[Mapping[str, Any], bool]:
        encoded_id = quote(task_id, safe="").replace(".", "%2E")
        url = self.endpoint + "/parse/" + encoded_id
        while True:
            response = self._request(
                "GET", url, deadline=deadline, recovery_context=recovery_context,
                cancel_check=cancel_check,
            )
            payload = response.json()
            state = str(_json_value(payload, "state", "status", "data.state") or "").casefold()
            if state in {"success", "succeeded", "done", "completed"}:
                return payload, False
            if state in {"failed", "error", "cancelled"}:
                raise VisionAdapterError("VISION_REMOTE_FAILED")
            if recovery_context is not None:
                return payload, True
            remaining = self._remaining(deadline, cancel_check)
            self.sleep(min(2.0, remaining))

    def extract(
        self,
        company,
        filing,
        pages,
        config,
        *,
        cancel_check=None,
        recovery_context=None,
        remote_task_id="",
    ):
        current_page: VisionPageRequest | None = None
        page_task_key = ""
        task_id = ""
        remote_state = ""
        submission_started = False
        explicit_rejection = False
        deadline = 0.0
        facts: list[FinancialFact] = []
        refs: list[EvidenceRef] = []
        diagnostics: list[str] = []
        page_errors: list[str] = []
        try:
            config.validate()
            deadline = self.clock() + float(config.timeout_seconds)
            _check_request(config, pages, filing)
            if config.provider != "mineru_flash":
                raise VisionAdapterError("VISION_PROVIDER_UNSUPPORTED")
            if remote_task_id and len(pages) != 1:
                raise VisionAdapterError("VISION_RECOVERY_BLOCKED")

            for page in pages:
                current_page = page
                task_id = ""
                remote_state = ""
                submission_started = False
                explicit_rejection = False
                self._remaining(deadline, cancel_check)
                page_task_key = vision_task_key(
                    f"mineru_flash:{MINERU_UPLOAD_PROTOCOL_VERSION}",
                    filing.content_hash,
                    (page,),
                    namespace="page",
                )
                prior = self.journal.get_vision_task(page_task_key) if self.journal is not None else None
                if prior and not self._identity_matches(prior, filing, page):
                    raise VisionAdapterError("VISION_RECOVERY_BLOCKED")
                prior_status = str(prior.get("status") or "") if prior else ""
                stored_task_id = str(prior.get("remote_task_id") or "") if prior else ""
                if remote_task_id and stored_task_id and remote_task_id != stored_task_id:
                    raise VisionAdapterError("VISION_RECOVERY_BLOCKED")
                task_id = stored_task_id or (str(remote_task_id) if remote_task_id else "")

                source_markdown = self._page_markdown(page_task_key, filing, page)
                if source_markdown is not None:
                    candidate = parse_vision_markdown(source_markdown, company, filing, (page,))
                    facts.extend(candidate.facts)
                    refs.extend(candidate.evidence)
                    diagnostics.extend((*candidate.diagnostics, "VISION_SOURCE_MARKDOWN_CACHE_HIT"))
                    if candidate.error_code:
                        page_errors.append(candidate.error_code)
                    else:
                        self._save_page_result(page_task_key, candidate, filing, page)
                    continue

                cached = self._cached_page_result(page_task_key, company, filing, page)
                if cached is not None:
                    # Older candidate caches already carry the complete bound
                    # table proof. Migrate its raw text locally, without a new
                    # upload or download; invalid proofs were rejected above.
                    proof = next((ref for ref in cached.evidence if ref.evidence_id.startswith("vision-table:")), None)
                    if proof is not None:
                        self._page_markdown(page_task_key, filing, page, json.loads(proof.excerpt)["markdown"])
                    facts.extend(cached.facts)
                    refs.extend(cached.evidence)
                    diagnostics.extend(cached.diagnostics)
                    diagnostics.append("VISION_PAGE_RESULT_CACHE_HIT")
                    continue

                if task_id:
                    if prior_status not in {"created", "uploaded", "running", "failed", "completed"} and not remote_task_id:
                        raise VisionAdapterError("VISION_RECOVERY_BLOCKED")
                    remote_state = prior_status or "running"
                elif prior:
                    if prior_status in {"submitting", "submission_unknown"}:
                        raise VisionAdapterError("VISION_RECOVERY_BLOCKED")
                    if prior_status != "rejected":
                        # A journal row with no handle and no explicit provider
                        # rejection is never evidence that another POST is safe.
                        raise VisionAdapterError("VISION_RECOVERY_BLOCKED")

                if remote_task_id and not task_id:
                    task_id = str(remote_task_id)
                    remote_state = "running"
                    self._save_journal(page_task_key, filing, page, status="running", remote_task_id=task_id)

                payload = {
                    "file_name": f"failed-page-{page.original_page}.pdf",
                    "language": config.language if config.language in {"ch", "en"} else ("ch" if company.market == "CN_A" else "en"),
                    "enable_table": True,
                    "is_ocr": True,
                    "enable_formula": False,
                }

                if not task_id:
                    reserve_run = getattr(recovery_context, "reserve_model_call", None)
                    if callable(reserve_run):
                        # A free parser has no LLM token contract, but its
                        # recognition submission still consumes a physical call.
                        reserve_run()
                    claim = getattr(self.journal, "claim_vision_task", None) if self.journal is not None else None
                    if callable(claim):
                        try:
                            claimed = claim(
                                page_task_key,
                                company_cik=filing.company_cik,
                                document_id=filing.document_id,
                                provider="mineru_flash",
                                page_hashes=(page.content_hash,),
                                page_numbers=(page.original_page,),
                            )
                        except ValueError as exc:
                            if str(exc) == "VISION_TASK_IDENTITY_CONFLICT":
                                raise VisionAdapterError("VISION_RECOVERY_BLOCKED") from exc
                            raise
                        if not claimed:
                            raise VisionAdapterError("VISION_RECOVERY_BLOCKED")
                    else:
                        self._save_journal(page_task_key, filing, page, status="submitting")
                    remote_state = "submitting"
                    self._remaining(deadline, cancel_check)
                    self._usage(recovery_context, pages=1)
                    self._remaining(deadline, cancel_check)
                    record_intent = getattr(recovery_context, "record_external_intent", None)
                    if callable(record_intent):
                        record_intent()
                    self._remaining(deadline, cancel_check)
                    submission_started = True
                    response = self._request(
                        "POST", self.endpoint + "/parse/file", deadline=deadline,
                        recovery_context=recovery_context,
                        headers={"Content-Type": "application/json"},
                        body=json.dumps(payload).encode("utf-8"), cancel_check=cancel_check,
                    )
                    settle_run = getattr(recovery_context, "settle_model_call", None)
                    if callable(settle_run):
                        settle_run(response=response)
                    data = response.json()
                    raw_task_id = _json_value(data, "data.task_id")
                    if isinstance(raw_task_id, (str, int)) and not isinstance(raw_task_id, bool):
                        candidate_task_id = str(raw_task_id)
                        task_id = candidate_task_id if candidate_task_id and len(candidate_task_id) <= 256 else ""
                    if task_id:
                        remote_state = "created"
                        self._save_journal(page_task_key, filing, page, status="created", remote_task_id=task_id)
                        record_task = getattr(recovery_context, "record_external_task", None)
                        if callable(record_task):
                            record_task(task_id)
                    _ensure_business_success(data)
                    if not task_id or len(task_id) > 256:
                        raise VisionAdapterError("VISION_MALFORMED_RESPONSE")
                    file_url = _https_url(_json_value(data, "data.file_url"))
                    # Keep the remote handle even if the POST completed after
                    # the deadline; the next turn can query, never resubmit.
                    self._remaining(deadline, cancel_check)
                    self._request(
                        "PUT", file_url, deadline=deadline,
                        recovery_context=recovery_context, body=page.pdf_bytes,
                        cancel_check=cancel_check,
                    )
                    self._save_journal(page_task_key, filing, page, status="uploaded", remote_task_id=task_id)
                    remote_state = "uploaded"

                self._remaining(deadline, cancel_check)
                if remote_state != "running":
                    self._save_journal(page_task_key, filing, page, status="running", remote_task_id=task_id)
                    remote_state = "running"
                result, pending = self._poll(
                    task_id, deadline=deadline, recovery_context=recovery_context,
                    cancel_check=cancel_check,
                )
                if pending:
                    self._save_journal(page_task_key, filing, page, status="running", remote_task_id=task_id)
                    return VisionExtractionResult(
                        tuple(facts), tuple(refs), tuple(diagnostics) + ("VISION_PENDING",), "VISION_PENDING",
                    )
                markdown_url = _https_url(_json_value(result, "data.markdown_url", "data.result.markdown_url"))
                markdown = self._request(
                    "GET", markdown_url, deadline=deadline,
                    recovery_context=recovery_context, cancel_check=cancel_check,
                )
                source_markdown = self._page_markdown(page_task_key, filing, page,
                    markdown.body.decode("utf-8", errors="strict"))
                candidate = parse_vision_markdown(source_markdown, company, filing, (page,))
                facts.extend(candidate.facts)
                refs.extend(candidate.evidence)
                diagnostics.extend(candidate.diagnostics)
                if candidate.error_code:
                    page_errors.append(candidate.error_code)
                if candidate.error_code is None and candidate.facts:
                    self._save_page_result(page_task_key, candidate, filing, page)
                self._save_journal(page_task_key, filing, page, status="completed", remote_task_id=task_id)
                remote_state = "completed"

            if not facts:
                error_code = page_errors[0] if page_errors else "VISION_NO_CANDIDATES"
                return VisionExtractionResult((), (), tuple(diagnostics) + (error_code,), error_code)
            return VisionExtractionResult(
                tuple(facts), tuple(refs), tuple(diagnostics) + ("VISION_MINERU_FLASH_COMPLETED",),
                page_errors[0] if page_errors else None,
            )
        except UnicodeDecodeError:
            caught = VisionAdapterError("VISION_MALFORMED_RESPONSE")
        except VisionAdapterError as vision_exc:
            caught = vision_exc

        if (
            self.journal is not None
            and current_page is not None
            and page_task_key
            and caught.code != "VISION_RECOVERY_BLOCKED"
        ):
            if not task_id and caught.http_status in {401, 403, 429} and submission_started:
                reject = getattr(recovery_context, "record_external_rejection", None)
                if callable(reject):
                    reject(caught.http_status)
                explicit_rejection = True
            status = (
                "rejected" if explicit_rejection
                else "submission_unknown" if submission_started and not task_id
                else "created" if task_id and remote_state in {"created", "submitting"}
                else "failed" if task_id
                else remote_state or "failed"
            )
            try:
                self._save_journal(
                    page_task_key, filing, current_page, status=status,
                    remote_task_id=task_id, error_code=caught.code,
                )
            except VisionAdapterError as save_exc:
                caught = save_exc
        return VisionExtractionResult(
            diagnostics=(caught.code,), error_code=caught.code,
            retry_after_seconds=caught.retry_after_seconds,
        )
class GatewayVisionAdapter:
    """Render failed pages locally, then call only the Rust credential gateway."""

    def __init__(
        self,
        provider: Any,
        *,
        image_renderer: Callable[[bytes], bytes] | None = None,
    ):
        self.provider = provider
        self._uses_default_renderer = image_renderer is None
        self.image_renderer = image_renderer or default_pdf_to_png

    def extract(self, company, filing, pages, config, *, cancel_check=None, recovery_context=None):
        deadline = time.monotonic() + float(config.timeout_seconds)

        def budget_cancel():
            return bool(cancel_check and cancel_check()) or time.monotonic() >= deadline

        try:
            _check_request(config, pages, filing)
            if not config.configured_model_id:
                raise VisionAdapterError("VISION_PROVIDER_UNSUPPORTED")
            facts: list[FinancialFact] = []
            refs: list[EvidenceRef] = []
            diagnostics: list[str] = []
            page_errors: list[str] = []
            scope = getattr(self.provider, "for_request_budget", None)
            provider = scope(deadline, cancel_check) if callable(scope) else self.provider
            output_scope = getattr(provider, "for_output_budget", None)
            provider = output_scope(8192) if callable(output_scope) else provider
            for page in pages:
                if cancel_check and cancel_check():
                    raise VisionAdapterError("VISION_CANCELLED")
                if self._uses_default_renderer:
                    image = default_pdf_to_png(page.pdf_bytes, cancel_check=budget_cancel)
                else:
                    image = self.image_renderer(page.pdf_bytes)
                if not image:
                    raise VisionAdapterError("VISION_IMAGE_RENDER_FAILED")
                if time.monotonic() >= deadline:
                    raise VisionAdapterError("VISION_TIMEOUT")
                prompt = (
                    "Return JSON only. Prefer source-faithful structured tables with shape "
                    "{schema:'financial-structure-v1',original_page:N,blocks:["
                    "{type:'title',text:'exact printed statement heading'},"
                    "{type:'text',text:'exact printed units/period declarations'},"
                    "{type:'table',bbox:[left,top,right,bottom],rows:[["
                    "{text:'exact cell text',rowspan:1,colspan:1}]]}]}. "
                    "Preserve ALL columns, merged header spans, unknown labels, amounts and "
                    "consolidated/parent headings. Do not invent geometry, currency, dates or "
                    "missing cells; omit optional bbox when unavailable. Keep each printed "
                    "currency attached to its own header column. "
                    "Alternatively the supported flat candidate shape is: "
                    "{facts:[{concept,label,value,currency,unit_scale,unit_text,statement,"
                    "scope,scope_text,period_end,period_text,original_page,raw_text}],"
                    "observations:[{label,value_text,original_page,raw_text}],"
                    "cell_mappings:[{concept,label,row_quote,original_page}]}. "
                    f"Use only original_page {page.original_page}; prefer consolidated "
                    "scope, preserve the stated period and currency, and omit uncertain facts. "
                    "For every fact quote unit_text, scope_text, and period_text verbatim "
                    "from the page and include all three in raw_text with the numeric row. "
                    "For each fact include label as the exact displayed row label, without "
                    "period/unit/table headings. "
                    "Only emit supported standard concepts; put other visible numeric metrics "
                    "in observations as text, never as facts. Observations are unverified "
                    "and must not be used for calculations. If an unfamiliar row label needs "
                    "semantic interpretation, optionally emit cell_mappings with its exact "
                    "label and complete row_quote (including every displayed column), never "
                    "a value, scale, currency or date override. The host rereads that original "
                    "row and the statement context; a mapping is not an admitted fact."
                )
                scheduler = shared_model_scheduler(
                    config.connection_id or config.configured_model_id,
                    limit=2,
                )
                try:
                    with scheduler.slot(budget_cancel):
                        once = getattr(provider, "generate_vision_once", None)
                        system = (
                            "Extract candidate financial facts from one approved filing page. "
                            "Do not infer missing values or use outside knowledge. "
                            "Never promote segment, custom, or ambiguous metrics to financial facts."
                        )
                        for json_mode in (True, False):
                            if cancel_check and cancel_check():
                                raise VisionAdapterError("VISION_CANCELLED")
                            if time.monotonic() >= deadline:
                                raise VisionAdapterError("VISION_TIMEOUT")
                            if recovery_context is not None:
                                recovery_context.checkpoint()
                                recovery_context.consume(Usage(calls=1, pages=int(json_mode)))
                                reserve_run = getattr(recovery_context, "reserve_model_call", None)
                                if callable(reserve_run):
                                    reserve_run(input_estimate=len(image) + len(system.encode()) + len(prompt.encode()), output_limit=8192)
                                # Each wire intent is durable. A known rejected
                                # format may be retried, an unknown outcome may not.
                                recovery_context.record_external_intent()
                            try:
                                structured = (
                                    once(system, prompt, image, json_mode=json_mode)
                                    if callable(once) else provider.generate_vision(system, prompt, image)
                                )
                                settle_run = getattr(recovery_context, "settle_model_call", None)
                                if callable(settle_run):
                                    settle_run(response=structured)
                                break
                            except ProviderError as exc:
                                settle_run = getattr(recovery_context, "settle_model_call", None)
                                if callable(settle_run):
                                    settle_run(error=exc)
                                format_rejected = (
                                    exc.code == "MODEL_RESPONSE_FORMAT_UNSUPPORTED"
                                    and type(exc.http_status) is int and exc.http_status in {400, 422}
                                )
                                if format_rejected and recovery_context is not None:
                                    recovery_context.record_external_rejection(
                                        exc.http_status, reason="response_format_unsupported")
                                if format_rejected and json_mode and callable(once):
                                    continue
                                raise
                    scheduler.mark_success()
                except InterruptedError as exc:
                    raise VisionAdapterError("VISION_TIMEOUT" if time.monotonic() >= deadline
                                             and not (cancel_check and cancel_check()) else "VISION_CANCELLED") from exc
                except ProviderError as exc:
                    if exc.code == "MODEL_CANCELLED":
                        raise VisionAdapterError("VISION_CANCELLED") from exc
                    rejected_status = {"MODEL_UNAUTHORIZED": 401, "MODEL_RATE_LIMITED": 429}.get(exc.code)
                    if recovery_context is not None and rejected_status is not None:
                        recovery_context.record_external_rejection(rejected_status)
                    if exc.code == "MODEL_RATE_LIMITED":
                        scheduler.mark_rate_limited(exc.retry_after_seconds)
                    if exc.code == "MODEL_TIMEOUT":
                        raise VisionAdapterError("VISION_TIMEOUT") from exc
                    if exc.code in {"MODEL_UNAUTHORIZED", "MODEL_CREDENTIAL_MISSING"}:
                        raise VisionAdapterError("VISION_UNAUTHORIZED") from exc
                    if exc.code == "MODEL_RATE_LIMITED":
                        raise VisionAdapterError("VISION_RATE_LIMITED", retry_after_seconds=exc.retry_after_seconds or 0.0) from exc
                    if exc.code == "MODEL_RESPONSE_FORMAT_UNSUPPORTED":
                        raise VisionAdapterError("VISION_MODEL_FORMAT_UNSUPPORTED") from exc
                    raise VisionAdapterError("VISION_MODEL_ERROR") from exc
                structured.pop("_response_meta", None)
                candidate = parse_vision_json(structured, company, filing, (page,))
                facts.extend(candidate.facts)
                refs.extend(candidate.evidence)
                diagnostics.extend(candidate.diagnostics)
                if candidate.error_code:
                    page_errors.append(candidate.error_code)
            if not facts and not any(ref.evidence_id.startswith("cell-map:") for ref in refs):
                return VisionExtractionResult(
                    (),
                    tuple(refs),
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
            code = ("VISION_TIMEOUT" if exc.code == "VISION_CANCELLED" and time.monotonic() >= deadline
                    and not (cancel_check and cancel_check()) else exc.code)
            return VisionExtractionResult(diagnostics=(code,), error_code=code,
                                          retry_after_seconds=exc.retry_after_seconds)


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
        self._completed_bindings: dict[str, str] = {}

    def extract(
        self,
        company: Company,
        filing: FilingDocument,
        pages: Sequence[VisionPageRequest],
        config: VisionFallbackConfig,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> VisionExtractionResult:
        started = time.monotonic()
        try:
            unique: dict[tuple[int, str], VisionPageRequest] = {}
            for page in sorted(pages, key=lambda item: (item.original_page, item.content_hash)):
                unique.setdefault((page.original_page, page.content_hash), page)
            ordered_pages = tuple(unique.values())
            config.validate()
            deadline = started + float(config.timeout_seconds)
            if cancel_check and cancel_check():
                raise VisionAdapterError("VISION_CANCELLED")
            if time.monotonic() >= deadline:
                raise VisionAdapterError("VISION_TIMEOUT")
            if not config.enabled:
                raise VisionAdapterError("VISION_DISABLED")
            if not ordered_pages:
                raise VisionAdapterError("VISION_NO_FAILED_PAGES")
            if len(ordered_pages) > config.max_pages:
                raise VisionAdapterError("VISION_PAGE_LIMIT")
            if sum(len(page.pdf_bytes) for page in ordered_pages) > config.max_bytes:
                raise VisionAdapterError("VISION_SIZE_LIMIT")
            plan = VisionUploadPlan(
                provider=config.provider,
                configured_model_id=config.configured_model_id,
                configuration_version=config.configuration_version,
                filing_hash=filing.content_hash,
                source_document=filing.primary_document,
                pages=ordered_pages,
                max_pages=config.max_pages,
                max_bytes=config.max_bytes,
            )
            provider_identity = _provider_identity(config)
            task_key = vision_task_key(
                provider_identity,
                filing.content_hash,
                ordered_pages,
                namespace="batch",
            )
            from dataclasses import asdict
            binding = sha256(json.dumps({
                "schema": "vision-batch-candidate-v2", "company": company.to_dict(),
                "filing": {key: value for key, value in filing.to_dict().items() if key not in {"local_path", "ingested_at"}},
                "configuration_version": config.configuration_version,
                "pages": [{"page": page.original_page, "hash": page.content_hash,
                    "unit_contexts": [asdict(item) for item in page.unit_contexts]} for page in ordered_pages],
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            prior = self.journal.get_vision_task(task_key) if self.journal is not None else None
            cached = self._completed_results.get(task_key)
            if cached is not None and self._completed_bindings.get(task_key) == binding:
                replayed = replay_saved_table_sources(cached, company, filing, ordered_pages)
                self._completed_results[task_key] = replayed
                return replayed
            load_result = getattr(self.journal, "get_vision_result", None)
            if callable(load_result):
                persisted = load_result(task_key)
                if isinstance(persisted, dict) and persisted.get("candidate_binding") == binding:
                    restored = _deserialize_vision_result(persisted)
                    if restored is None:
                        return _saved_source_invalid_result()
                    cached = VisionExtractionResult(
                        restored.facts,
                        restored.evidence,
                        restored.diagnostics + ("VISION_RESULT_CACHE_HIT",),
                        None,
                    )
                    cached = replay_saved_table_sources(cached, company, filing, ordered_pages)
                    self._completed_results[task_key] = cached
                    self._completed_bindings[task_key] = binding
                    return cached
            _check_request(config, ordered_pages, filing, plan=plan)
            if cancel_check and cancel_check():
                raise VisionAdapterError("VISION_CANCELLED")
            if time.monotonic() >= deadline:
                raise VisionAdapterError("VISION_TIMEOUT")
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
            worker_config = replace(config, approve_upload=lambda _summary: True,
                configured_timeout_seconds=config.configured_timeout_seconds or float(config.timeout_seconds))
            results: dict[int, VisionExtractionResult] = {}
            errors: list[str] = []
            internal_cancel = threading.Event()

            def combined_cancel() -> bool:
                return internal_cancel.is_set() or bool(cancel_check and cancel_check()) or time.monotonic() >= deadline

            def run_page(page):
                remaining = deadline - time.monotonic()
                if combined_cancel():
                    return VisionExtractionResult(error_code="VISION_CANCELLED" if cancel_check and cancel_check() else "VISION_TIMEOUT")
                scoped_config = replace(worker_config, timeout_seconds=remaining)
                return self.adapter.extract(company, filing, (page,), scoped_config, cancel_check=combined_cancel)

            executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="vision-page")
            futures = {
                executor.submit(run_page, page): index
                for index, page in enumerate(ordered_pages)
            }
            try:
                while futures:
                    if cancel_check and cancel_check():
                        raise VisionAdapterError("VISION_CANCELLED")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        errors.append("VISION_TIMEOUT")
                        break
                    done, _ = wait(tuple(futures), timeout=min(.05, remaining), return_when=FIRST_COMPLETED)
                    for future in done:
                        index = futures.pop(future)
                        if cancel_check and cancel_check():
                            raise VisionAdapterError("VISION_CANCELLED")
                        try:
                            results[index] = future.result()
                        except VisionAdapterError as exc:
                            results[index] = VisionExtractionResult(diagnostics=(exc.code,), error_code=exc.code)
                        except Exception:
                            results[index] = VisionExtractionResult(diagnostics=("VISION_PROVIDER_ERROR",), error_code="VISION_PROVIDER_ERROR")
                        if time.monotonic() >= deadline:
                            # The underlying durable Adapter may retain a
                            # received body/remote handle for local recovery;
                            # this expired owner cannot cache/adopt its return.
                            results.pop(index, None)
                            errors.append("VISION_TIMEOUT")
                            break
                        if self.progress is not None:
                            self.progress(len(results), len(ordered_pages))
                    if errors:
                        break
            except VisionAdapterError as exc:
                errors.append(exc.code)
                internal_cancel.set()
            finally:
                if errors:
                    internal_cancel.set()
                    for future in futures:
                        future.cancel()
                # Give cooperative transports one bounded cancellation drain.
                # No unbounded shutdown: queued pages cannot start; late
                # returns have no path to this coordinator's result/cache.
                if futures:
                    wait(tuple(futures), timeout=.1)
                executor.shutdown(wait=all(future.done() for future in futures), cancel_futures=True)
            facts: list[FinancialFact] = []
            evidence: list[EvidenceRef] = []
            diagnostics: list[str] = []
            for index in range(len(ordered_pages)):
                result = results.get(index)
                if result is None:
                    if not errors:
                        errors.append("VISION_CANCELLED")
                    continue
                facts.extend(result.facts)
                evidence.extend(result.evidence)
                diagnostics.extend(result.diagnostics)
                if result.error_code:
                    errors.append(result.error_code)
            error_code = errors[0] if errors else None
            mapping_body = any(ref.evidence_id.startswith("cell-map:") for ref in evidence)
            if not facts and not mapping_body and error_code is None:
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
            if error_code is None and (facts or mapping_body):
                completed = VisionExtractionResult(
                    tuple(facts), tuple(evidence), tuple(diagnostics), None
                )
                self._completed_results[task_key] = completed
                self._completed_bindings[task_key] = binding
                save_result = getattr(self.journal, "save_vision_result", None)
                if callable(save_result):
                    save_result(task_key, {**_serialize_vision_result(completed), "candidate_binding": binding})
                return completed
            if error_code and not facts:
                diagnostics.append(error_code)
            return VisionExtractionResult(tuple(facts), tuple(evidence), tuple(diagnostics), error_code)
        except VisionAdapterError as exc:
            return VisionExtractionResult(diagnostics=(exc.code,), error_code=exc.code)


def _render_pdfium_page(pdf_bytes: bytes) -> bytes:
    """Native PDFium renderer; called only behind a process boundary."""

    from .pdf_worker import pdf_phase
    pdf_phase('import', backend='pdfium')
    import pypdfium2 as pdfium

    pdf_phase('open', backend='pdfium', version=str(pdfium.PYPDFIUM_INFO))
    document = pdfium.PdfDocument(pdf_bytes)
    page = bitmap = image = None
    try:
        if len(document) < 1:
            raise VisionAdapterError("VISION_IMAGE_RENDER_FAILED")
        page = document[0]
        # Reject an excessive surface before native allocation. Silently
        # thumbnailing it can remove small digits from a financial statement.
        width, height = math.ceil(page.get_width() * 1.5), math.ceil(page.get_height() * 1.5)
        if width > 4096 or height > 4096 or width * height > 12_000_000:
            raise VisionAdapterError("VISION_IMAGE_SIZE_LIMIT")
        pdf_phase('render')
        bitmap = page.render(scale=1.5, rev_byteorder=True)
        image = bitmap.to_pil()
        width, height = image.size
        if width > 4096 or height > 4096 or width * height > 12_000_000:
            raise VisionAdapterError("VISION_IMAGE_SIZE_LIMIT")
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        data = output.getvalue()
        if len(data) > 8 * 1024 * 1024:
            raise VisionAdapterError("VISION_IMAGE_SIZE_LIMIT")
        return data
    finally:
        for resource in (image, bitmap, page, document):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def default_pdf_to_png(
    pdf_bytes: bytes,
    *,
    cancel_check: Callable[[], bool] | None = None,
    timeout_seconds: float = 35.0,
) -> bytes:
    """Render page one in memory inside a killable native-worker process."""
    try:
        data, error = _run_pdfium_isolated(
            _render_pdfium_page,
            bytes(pdf_bytes),
            timeout_seconds=timeout_seconds,
            cancel_check=cancel_check,
        )
        if error == "VISION_IMAGE_SIZE_LIMIT":
            raise VisionAdapterError(error)
        if error == "PDFIUM_WORKER_CANCELLED":
            raise VisionAdapterError("VISION_CANCELLED")
        if error == "PDFIUM_WORKER_TIMEOUT":
            raise VisionAdapterError("VISION_IMAGE_RENDER_TIMEOUT")
        if error is not None or not isinstance(data, bytes):
            raise VisionAdapterError("VISION_IMAGE_RENDER_FAILED")
        return data
    except VisionAdapterError:
        raise
    except Exception:
        # Exception strings and worker payloads are intentionally discarded;
        # they may contain local paths or document-derived content.
        raise VisionAdapterError("VISION_IMAGE_RENDER_FAILED") from None


def default_pdf_to_image_pdf(
    pdf_bytes: bytes, *, cancel_check: Callable[[], bool] | None = None,
    timeout_seconds: float = 35.0,
) -> bytes:
    """Render the complete page, retaining every rendered pixel losslessly.

    The original remains authoritative and untouched. Only this explicitly
    hashed upload representation loses its broken native PDF reading order;
    local unit declarations continue to reference the original page.
    """
    from .financial_sources.image_pdf import image_pdf_from_png

    png = default_pdf_to_png(pdf_bytes, cancel_check=cancel_check,
                             timeout_seconds=timeout_seconds)
    if cancel_check is not None and cancel_check():
        raise VisionAdapterError("VISION_CANCELLED")
    try:
        return image_pdf_from_png(png)
    except (TypeError, ValueError):
        raise VisionAdapterError("VISION_IMAGE_REPRESENTATION_FAILED") from None


def parse_vision_markdown(
    markdown: str,
    company: Company,
    filing: FilingDocument,
    pages: Sequence[VisionPageRequest],
) -> VisionExtractionResult:
    """Collect cells; the compiler separately verifies their source proof."""
    from .financial_sources.markdown import collect_markdown_cells
    if len(pages) != 1:
        return VisionExtractionResult(error_code="VISION_PAGE_MAPPING_REQUIRED",
            diagnostics=("VISION_PAGE_MAPPING_REQUIRED",))
    facts, evidence, error = collect_markdown_cells(markdown, company, filing, pages[0])
    return VisionExtractionResult(facts, evidence,
        (error,) if error else ("VISION_CANDIDATES_ONLY",), error)


def parse_vision_json(payload: Any, company: Company, filing: FilingDocument, pages: Sequence[VisionPageRequest]) -> VisionExtractionResult:
    """Parse explicitly evidenced standard facts and separate unverified observations."""
    if isinstance(payload, dict) and payload.get('schema') == 'financial-structure-v1':
        from .financial_sources.structure import parse_structure
        return parse_structure(payload, company, filing, pages)
    if not isinstance(payload, dict) or not isinstance(payload.get("facts", []), list):
        return VisionExtractionResult(error_code="VISION_STRUCTURED_JSON_REQUIRED", diagnostics=("VISION_STRUCTURED_JSON_REQUIRED",))
    rows = payload.get("facts", [])[:200]
    observation_rows = payload.get("observations", [])
    if not isinstance(observation_rows, list):
        observation_rows = []
    observation_rows = observation_rows[:20]
    facts: list[FinancialFact] = []
    refs: list[EvidenceRef] = []
    pages_by_number = {page.original_page: page for page in pages}
    from .financial_cell_repair import mapping_reference
    from .financial_taxonomy import (
        cost_row_concept, cost_row_is_non_amount, cost_row_mentions,
        cost_row_source_is_non_amount,
    )
    mappings = payload.get("cell_mappings", [])
    if isinstance(mappings, list) and len(mappings) <= 200:
        refs.extend(ref for item in mappings if (ref := mapping_reference(item, filing, set(pages_by_number))) is not None)
    consolidated_markers = ("consolidated", "合并", "合併", "綜合")
    parent_markers = ("parent company", "parent only", "company only", "母公司", "單體", "单体", "个别报表", "個別報表")
    for row in rows:
        if not isinstance(row, dict):
            continue
        concept = str(row.get("concept", ""))
        expected_statement = _VISION_CONCEPT_STATEMENTS.get(concept)
        if expected_statement is None:
            observation = _unverified_vision_observation(row, filing, pages_by_number)
            if observation is not None:
                refs.append(observation)
            continue
        try:
            page_no = int(row.get("original_page"))
            page = pages_by_number[page_no]
            raw_value = _vision_decimal_source_text(row.get("value"))
            reported_value = _parse_vision_number(raw_value)
            scale = float(row.get("unit_scale"))
            end_date = str(row.get("period_end", "")).strip()[:10]
            currency = str(row.get("currency", "")).strip().upper()
            statement = str(row.get("statement", ""))
            scope = str(row.get("scope", "")).strip().casefold()
            unit_text = str(row.get("unit_text", "")).strip()[:160]
            scope_text = str(row.get("scope_text", "")).strip()[:160]
            period_text = str(row.get("period_text", "")).strip()[:160]
            raw_text = str(row.get("raw_text", "")).strip()[:2000]
            source_label = str(row.get("label", "")).strip()[:300]
            if (
                reported_value is None
                or scale not in _VISION_UNIT_SCALES
                or end_date != str(filing.period_end)[:10]
                or currency != company.reporting_currency.upper()
                or statement != expected_statement
                or scope != "consolidated"
                or not raw_text
                or str(filing.period_end)[:4] not in period_text
                or not any(marker in scope_text.casefold() for marker in consolidated_markers)
                or any(marker in scope_text.casefold() for marker in parent_markers)
                or not _vision_source_phrase_present(raw_text, unit_text)
                or not _vision_source_phrase_present(raw_text, scope_text)
                or not _vision_source_phrase_present(raw_text, period_text)
                or not _vision_unit_scale_is_explicit(unit_text, scale)
                or not _vision_currency_is_explicit(unit_text, currency)
                or (source_label and not _vision_source_phrase_present(raw_text, source_label))
            ):
                observation = _unverified_vision_observation(row, filing, pages_by_number)
                if observation is not None:
                    refs.append(observation)
                continue
        except (KeyError, TypeError, ValueError):
            observation = _unverified_vision_observation(row, filing, pages_by_number)
            if observation is not None:
                refs.append(observation)
            continue
        # The model's claim is only a candidate. Prefer the explicit row label
        # over wider raw_text, which may also contain unrelated table headings.
        label_mentions_cost = bool(cost_row_mentions(source_label))
        source_cost_label = source_label or raw_text
        source_cost_concept = cost_row_concept(source_cost_label)
        if (
            (label_mentions_cost and cost_row_is_non_amount(source_label))
            or cost_row_source_is_non_amount(source_cost_label)
            or (source_cost_concept is not None and source_cost_concept != concept)
            or (concept in {"cost_of_revenue", "total_operating_cost"}
                and source_cost_concept != concept)
        ):
            observation = _unverified_vision_observation(row, filing, pages_by_number)
            if observation is not None:
                refs.append(observation)
            continue
        unit_evidence_id = _vision_unit_evidence_id(
            filing.document_id, page_no, page.content_hash, unit_text,
        )
        try:
            lineage = SourceAmount(
                raw_value, str(int(scale)), QuantityKind.MONEY, currency,
                (unit_evidence_id,),
            ).normalize()
            value = float(Decimal(lineage.normalized_value))
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(value):
            continue
        proof_payload = {
            "schema": GATEWAY_FACT_VERSION, "document_hash": filing.content_hash,
            "page_hash": page.content_hash, "original_page": page_no,
            "observation": {"concept": concept, "raw_value": raw_value,
                "currency": currency, "scale": str(int(scale)), "unit_text": unit_text,
                "statement": statement, "scope": scope, "scope_text": scope_text,
                "period_end": end_date, "period_text": period_text, "raw_text": raw_text},
        }
        proof_id = gateway_source_id(filing.document_id, proof_payload)
        period_start = _vision_period_start(filing.fiscal_period, filing.period_end, statement)
        fact_id = f"{filing.document_id}:{concept}:{page_no}:{page.content_hash[:16]}:{value}"
        fact = FinancialFact(
            fact_id, company.security_id, concept, source_label or concept, value,
            currency, int(filing.period_end[:4]), filing.fiscal_period, filing.form_type,
            period_start, filing.period_end, filing.filed_at, filing.accession_number,
            filing.source_url, scope="consolidated", entity=company.name,
            market=company.market, statement=statement, period_start=period_start,
            consolidated_scope="consolidated", currency=currency, unit_scale=scale,
            unit_provenance="vision_explicit", source_document=filing.primary_document,
            source_page=page_no, raw_text=raw_text, parser_version=GATEWAY_FACT_VERSION,
            normalization_lineage=lineage, classification_evidence_ids=(proof_id,),
            revision=filing.revision,
        )
        facts.append(fact)
        refs.append(EvidenceRef(
            f"fact:{fact_id}", filing.document_id, filing.source_url,
            filing.primary_document, f"page:{page_no}", raw_text, filing.filed_at,
            filing.content_hash,
        ))
        refs.append(EvidenceRef(
            unit_evidence_id, filing.document_id, filing.source_url,
            f"{filing.form_type} {filing.period_end} / explicit displayed unit",
            f"page:{page_no}", unit_text, filing.filed_at, filing.content_hash,
        ))
        refs.append(EvidenceRef(
            proof_id, filing.document_id, filing.source_url,
            "Captured visual observation / candidate only", f"page:{page_no}",
            canonical_cloud_source(proof_payload), filing.filed_at, filing.content_hash,
        ))

    for row in observation_rows:
        if not isinstance(row, dict):
            continue
        observation = _unverified_vision_observation(row, filing, pages_by_number)
        if observation is not None:
            refs.append(observation)
    unique_refs: dict[str, EvidenceRef] = {}
    for ref in refs:
        unique_refs.setdefault(ref.evidence_id, ref)
    refs = list(unique_refs.values())

    if not facts and any(ref.evidence_id.startswith("cell-map:") for ref in refs):
        return VisionExtractionResult((), tuple(refs), ("VISION_MAPPING_CANDIDATES_ONLY",))
    if not facts:
        return VisionExtractionResult(
            (), tuple(refs), ("VISION_NO_CANDIDATES",), "VISION_NO_CANDIDATES"
        )
    return VisionExtractionResult(tuple(facts), tuple(refs), ("VISION_CANDIDATES_ONLY",))


def _saved_source_invalid_result(*, candidate_integrity_failed: bool = False) -> VisionExtractionResult:
    diagnostics = ("VISION_CACHED_SOURCE_INVALID",)
    if candidate_integrity_failed:
        diagnostics += ("VISION_CANDIDATE_INTEGRITY_FAILED",)
    return VisionExtractionResult(
        diagnostics=diagnostics,
        error_code="VISION_CACHED_SOURCE_INVALID",
    )


def _saved_table_fact_is_intact(
    fact: FinancialFact,
    evidence_by_id: Mapping[str, EvidenceRef],
    filing: FilingDocument,
    page_by_identity: Mapping[tuple[int, str], VisionPageRequest],
) -> bool:
    """Reject altered normalized table facts before trusting their raw proof."""
    lineage = fact.normalization_lineage
    if lineage is None or lineage.inputs:
        return False
    try:
        amount = SourceAmount(
            lineage.raw_value,
            lineage.scale,
            QuantityKind(lineage.quantity_kind),
            lineage.currency,
            lineage.unit_evidence_ids,
            lineage.value_transform,
        ).normalize()
        if amount.normalized_value != lineage.normalized_value:
            return False
        normalized_value = Decimal(amount.normalized_value)
        if not math.isfinite(fact.value) or float(normalized_value) != fact.value:
            return False
        scale = Decimal(lineage.scale)
        if (Decimal(str(fact.unit_scale)) != scale
                or fact.currency.strip().upper() != amount.currency
                or fact.unit.strip().upper() != amount.currency):
            return False

        unit_ids = tuple(lineage.unit_evidence_ids)
        if not unit_ids or len(set(unit_ids)) != len(unit_ids):
            return False
        allowed_unit_pages = set()
        for page in page_by_identity.values():
            allowed_unit_pages.add(page.original_page)
            allowed_unit_pages.update(
                context.original_page for context in page.unit_contexts
                if context.document_hash == filing.content_hash
            )
        for evidence_id in unit_ids:
            ref = evidence_by_id.get(evidence_id)
            if (ref is None or ref.evidence_id != evidence_id
                    or not evidence_id.startswith("vision-unit:")
                    or ref.document_id != filing.document_id
                    or ref.source_url != filing.source_url
                    or ref.content_hash != filing.content_hash
                    or ref.published_at != filing.filed_at
                    or ref.title != "Explicit financial-statement unit"):
                return False
            match = re.fullmatch(r"page:([1-9]\d*)", ref.locator)
            if match is None:
                return False
            unit_page = int(match.group(1))
            if unit_page not in allowed_unit_pages:
                return False
            expected_id = "vision-unit:" + sha256(
                f"{filing.document_id}|{unit_page}|{ref.excerpt}".encode()
            ).hexdigest()
            # The unit excerpt may intentionally compose a public multiplier
            # with a column-local currency (for example, public USD millions
            # and a local RMB header). Its meaning is re-established from the
            # hash-bound raw table below and checked by the normal compiler;
            # do not re-interpret this flattened excerpt as one declaration.
            if expected_id != evidence_id:
                return False
        return True
    except (InvalidOperation, TypeError, ValueError, OverflowError, AttributeError):
        return False


def replay_saved_table_sources(
    result: VisionExtractionResult,
    company: Company,
    filing: FilingDocument,
    pages: Sequence[VisionPageRequest],
) -> VisionExtractionResult:
    """Rebuild cached table candidates only from their bound raw source proof.

    Flat legacy candidates without table-source proofs keep their historical
    behavior. Table-derived normalized facts are never parser input.
    """
    if not isinstance(result, VisionExtractionResult):
        return _saved_source_invalid_result()

    page_by_identity: dict[tuple[int, str], VisionPageRequest] = {}
    for page in pages:
        identity = (page.original_page, page.content_hash)
        if identity in page_by_identity and page_by_identity[identity] != page:
            return _saved_source_invalid_result()
        page_by_identity[identity] = page

    proof_refs: dict[str, EvidenceRef] = {}
    evidence_by_id: dict[str, EvidenceRef] = {}
    for ref in result.evidence:
        previous_ref = evidence_by_id.get(ref.evidence_id)
        if previous_ref is not None and previous_ref.to_dict() != ref.to_dict():
            return _saved_source_invalid_result()
        evidence_by_id[ref.evidence_id] = ref
        if not ref.evidence_id.startswith("vision-table:"):
            continue
        previous = proof_refs.get(ref.evidence_id)
        if previous is not None and previous.to_dict() != ref.to_dict():
            return _saved_source_invalid_result()
        proof_refs[ref.evidence_id] = ref

    table_facts = []
    preserved_facts = []
    proof_ids = set(proof_refs)
    for fact in result.facts:
        fact_proofs = {
            item for item in fact.classification_evidence_ids
            if item.startswith("vision-table:")
        }
        is_table_fact = fact.parser_version == CLOUD_TABLE_VERSION or bool(fact_proofs)
        if is_table_fact:
            if not fact_proofs or not fact_proofs.issubset(proof_ids):
                return _saved_source_invalid_result()
            if not _saved_table_fact_is_intact(fact, evidence_by_id, filing, page_by_identity):
                return _saved_source_invalid_result(candidate_integrity_failed=True)
            table_facts.append(fact)
        else:
            preserved_facts.append(fact)

    if not proof_refs:
        if table_facts:
            return _saved_source_invalid_result()
        return result

    from dataclasses import asdict
    from .financial_sources.structure import parse_structure, structure_to_markdown

    replayed: list[VisionExtractionResult] = []
    for evidence_id, ref in sorted(proof_refs.items()):
        try:
            if (not isinstance(ref.excerpt, str)
                    or len(ref.excerpt.encode("utf-8")) > 5 * 1024 * 1024):
                return _saved_source_invalid_result()
            payload = json.loads(ref.excerpt)
            if not isinstance(payload, dict):
                return _saved_source_invalid_result()
            if (canonical_cloud_source(payload) != ref.excerpt
                    or payload.get("schema") != CLOUD_TABLE_VERSION
                    or ref.evidence_id != cloud_source_id(ref.document_id, payload)):
                return _saved_source_invalid_result()
            if (ref.document_id != filing.document_id
                    or ref.source_url != filing.source_url
                    or ref.content_hash != filing.content_hash
                    or payload.get("document_hash") != filing.content_hash
                    or payload.get("period_end") != filing.period_end
                    or payload.get("fiscal_period") != filing.fiscal_period):
                return _saved_source_invalid_result()
            page_number = payload.get("original_page")
            page_hash = payload.get("page_hash")
            if type(page_number) is not int or not isinstance(page_hash, str):
                return _saved_source_invalid_result()
            page = page_by_identity.get((page_number, page_hash))
            if page is None or ref.locator != f"page:{page_number}":
                return _saved_source_invalid_result()
            if payload.get("unit_contexts") != [asdict(item) for item in page.unit_contexts]:
                return _saved_source_invalid_result()

            markdown = payload.get("markdown")
            if not isinstance(markdown, str) or len(markdown.encode("utf-8")) > 4 * 1024 * 1024:
                return _saved_source_invalid_result()
            source_structure = payload.get("source_structure")
            if source_structure is not None:
                if structure_to_markdown(source_structure) != markdown:
                    return _saved_source_invalid_result()
                current = parse_structure(source_structure, company, filing, (page,))
            else:
                current = parse_vision_markdown(markdown, company, filing, (page,))
            if current.error_code in {
                "VISION_PAGE_MAPPING_REQUIRED", "VISION_STRUCTURE_INVALID",
                "VISION_TABLE_SOURCE_INVALID", "VISION_EMPTY_MARKDOWN",
                "VISION_DOCUMENT_HASH_REQUIRED",
            }:
                return _saved_source_invalid_result()
            if not any(ref.evidence_id == evidence_id for ref in current.evidence):
                return _saved_source_invalid_result()
            replayed.append(current)
        except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
            return _saved_source_invalid_result()

    facts_by_id: dict[str, FinancialFact] = {}
    for fact in (*preserved_facts, *(fact for item in replayed for fact in item.facts)):
        previous = facts_by_id.get(fact.fact_id)
        if previous is not None and previous.to_dict() != fact.to_dict():
            return _saved_source_invalid_result()
        facts_by_id[fact.fact_id] = fact

    evidence_by_id: dict[str, EvidenceRef] = {}
    for ref in (*result.evidence, *(ref for item in replayed for ref in item.evidence)):
        previous = evidence_by_id.get(ref.evidence_id)
        if previous is not None and previous.to_dict() != ref.to_dict():
            return _saved_source_invalid_result()
        evidence_by_id[ref.evidence_id] = ref

    diagnostics = list(result.diagnostics)
    replay_errors = []
    for item in replayed:
        diagnostics.extend(item.diagnostics)
        if item.error_code:
            replay_errors.append(item.error_code)
    if "VISION_SAVED_SOURCE_REVALIDATED" not in diagnostics:
        diagnostics.append("VISION_SAVED_SOURCE_REVALIDATED")
    return VisionExtractionResult(
        tuple(facts_by_id.values()),
        tuple(evidence_by_id.values()),
        tuple(diagnostics),
        replay_errors[0] if replay_errors else None,
        result.retry_after_seconds,
    )
