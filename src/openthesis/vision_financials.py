"""Opt-in cloud vision fallbacks for failed financial-table extraction.

The adapters in this module are deliberately small and side-effect free: all
payloads are held in memory, credentials are session values, and diagnostics
contain only stable error codes.  They return candidate facts; the ingestion
quality gate remains the authority that can accept or quarantine them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from hashlib import sha256
from io import BytesIO
import json
from pathlib import PurePosixPath
import re
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

from .domain import Company, EvidenceRef, FilingDocument, FinancialFact


VISION_MAX_PAGES = 20
VISION_MAX_BYTES = 10 * 1024 * 1024


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
    provider: str = "mineru_lite"
    token: str = ""
    api_key: str = ""
    endpoint: str = ""
    model: str = ""
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
        if self.provider not in {"mineru_lite", "mineru_precision", "custom_vision"}:
            raise VisionAdapterError("VISION_PROVIDER_UNSUPPORTED")


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
    """Minimal GET/POST/PUT transport; replaceable in tests and desktop builds."""

    def request(self, method: str, url: str, *, headers=None, body=None, timeout=60.0):
        request = Request(url, data=body, headers=dict(headers or {}), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                return VisionHttpResponse(response.status, response.read(), dict(response.headers.items()))
        except HTTPError as exc:
            return VisionHttpResponse(exc.code, exc.read(), dict(exc.headers.items()))
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
        "provider": config.provider,
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


class _MineruBase:
    def __init__(self, transport: VisionHttpTransport | None = None, *, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.transport = transport or UrllibVisionTransport()
        self.sleep = sleep
        self.clock = clock

    def _poll(self, url: str, headers: Mapping[str, str], config: VisionFallbackConfig, cancel_check):
        url = _https_url(url)
        deadline = self.clock() + max(0.01, config.timeout_seconds)
        while self.clock() < deadline:
            if cancel_check and cancel_check():
                raise VisionAdapterError("VISION_CANCELLED")
            response = self.transport.request("GET", url, headers=headers, timeout=config.timeout_seconds)
            if response.status >= 400:
                raise VisionAdapterError(_safe_error(response))
            payload = response.json()
            state = str(_json_value(payload, "state", "status", "data.state") or "").lower()
            if state in {"success", "succeeded", "done", "completed"}:
                return payload
            if state in {"failed", "error", "cancelled"}:
                raise VisionAdapterError("VISION_REMOTE_FAILED")
            self.sleep(min(2.0, max(0.01, deadline - self.clock())))
        raise VisionAdapterError("VISION_TIMEOUT")


class MineruLiteAdapter(_MineruBase):
    endpoint = "https://mineru.net/api/v1/agent"

    def extract(self, company, filing, pages, config, *, cancel_check=None):
        try:
            _check_request(config, pages, filing)
            facts: list[FinancialFact] = []
            refs: list[EvidenceRef] = []
            for page in pages:
                if cancel_check and cancel_check():
                    raise VisionAdapterError("VISION_CANCELLED")
                payload = {
                    "file_name": f"failed-page-{page.original_page}.pdf",
                    "language": (config.language if config.language in {"ch", "en"} else ("ch" if company.market == "CN_A" else "en")),
                    "enable_table": True,
                    "is_ocr": False,
                    "enable_formula": False,
                }
                response = self.transport.request("POST", self.endpoint + "/parse/file", headers={"Content-Type": "application/json"}, body=json.dumps(payload).encode(), timeout=config.timeout_seconds)
                if response.status >= 400:
                    raise VisionAdapterError(_safe_error(response))
                data = response.json()
                _ensure_business_success(data)
                task_id = _json_value(data, "data.task_id")
                file_url = _https_url(_json_value(data, "data.file_url"))
                if not task_id:
                    raise VisionAdapterError("VISION_MALFORMED_RESPONSE")
                if cancel_check and cancel_check():
                    raise VisionAdapterError("VISION_CANCELLED")
                put = self.transport.request("PUT", file_url, body=page.pdf_bytes, timeout=config.timeout_seconds)
                if put.status >= 400:
                    raise VisionAdapterError(_safe_error(put))
                result = self._poll(self.endpoint + "/parse/" + str(task_id), {}, config, cancel_check)
                markdown_url = _https_url(_json_value(result, "data.markdown_url", "data.result.markdown_url"))
                if cancel_check and cancel_check():
                    raise VisionAdapterError("VISION_CANCELLED")
                markdown_response = self.transport.request("GET", markdown_url, timeout=config.timeout_seconds)
                if markdown_response.status >= 400:
                    raise VisionAdapterError(_safe_error(markdown_response))
                candidate = parse_vision_markdown(markdown_response.body.decode("utf-8", errors="strict"), company, filing, (page,))
                facts.extend(candidate.facts)
                refs.extend(candidate.evidence)
            if not facts:
                return VisionExtractionResult((), (), ("VISION_NO_CANDIDATES",), "VISION_NO_CANDIDATES")
            return VisionExtractionResult(tuple(facts), tuple(refs), ("VISION_MINERU_LITE_COMPLETED",))
        except VisionAdapterError as exc:
            return VisionExtractionResult(diagnostics=(exc.code,), error_code=exc.code)


class MineruPrecisionAdapter(_MineruBase):
    endpoint = "https://mineru.net/api/v4"

    def extract(self, company, filing, pages, config, *, cancel_check=None):
        try:
            _check_request(config, pages, filing)
            if not config.token:
                raise VisionAdapterError("VISION_TOKEN_REQUIRED")
            headers = {"Authorization": f"Bearer {config.token}", "Content-Type": "application/json"}
            files = [{"name": f"failed-page-{page.original_page}.pdf", "data_id": f"page-{page.original_page}-{page.content_hash[:12]}"} for page in pages]
            response = self.transport.request(
                "POST", self.endpoint + "/file-urls/batch", headers=headers,
                body=json.dumps({"model_version": "vlm", "enable_table": True, "files": files}).encode(),
                timeout=config.timeout_seconds,
            )
            if response.status >= 400:
                raise VisionAdapterError(_safe_error(response))
            payload = response.json()
            _ensure_business_success(payload)
            batch_id = _json_value(payload, "batch_id", "data.batch_id", "data.id")
            urls = _json_value(payload, "data.file_urls")
            if not batch_id or not isinstance(urls, list) or len(urls) != len(pages):
                raise VisionAdapterError("VISION_MALFORMED_RESPONSE")
            for page, item in zip(pages, urls):
                if cancel_check and cancel_check():
                    raise VisionAdapterError("VISION_CANCELLED")
                signed = _https_url(item.get("file_url") if isinstance(item, dict) else item)
                put = self.transport.request("PUT", signed, body=page.pdf_bytes, timeout=config.timeout_seconds)
                if put.status >= 400:
                    raise VisionAdapterError(_safe_error(put))
            result = self._poll(self.endpoint + "/extract-results/batch/" + str(batch_id), headers, config, cancel_check)
            results = _json_value(result, "data.extract_result")
            if not isinstance(results, list):
                raise VisionAdapterError("VISION_MALFORMED_RESPONSE")
            facts: list[FinancialFact] = []
            refs: list[EvidenceRef] = []
            by_id = {f"page-{page.original_page}-{page.content_hash[:12]}": page for page in pages}
            for item in results:
                data_id = str(item.get("data_id", "")) if isinstance(item, dict) else ""
                page = by_id.get(data_id)
                state = str(item.get("state", item.get("status", ""))).lower() if isinstance(item, dict) else ""
                if state not in {"done", "success", "succeeded", "completed"}:
                    raise VisionAdapterError("VISION_REMOTE_FAILED")
                zip_url = _https_url(item.get("full_zip_url") if isinstance(item, dict) else "")
                if page is None:
                    raise VisionAdapterError("VISION_PROVENANCE_MISMATCH")
                if cancel_check and cancel_check():
                    raise VisionAdapterError("VISION_CANCELLED")
                archive = self.transport.request("GET", zip_url, timeout=config.timeout_seconds)
                if archive.status >= 400:
                    raise VisionAdapterError(_safe_error(archive))
                markdown = _safe_zip_markdown(archive.body, config.max_bytes)
                candidate = parse_vision_markdown(markdown, company, filing, (page,))
                facts.extend(candidate.facts)
                refs.extend(candidate.evidence)
            if not facts:
                return VisionExtractionResult((), (), ("VISION_NO_CANDIDATES",), "VISION_NO_CANDIDATES")
            return VisionExtractionResult(tuple(facts), tuple(refs), ("VISION_MINERU_PRECISION_COMPLETED",))
        except VisionAdapterError as exc:
            return VisionExtractionResult(diagnostics=(exc.code,), error_code=exc.code)


class CustomVisionAdapter:
    def __init__(self, transport: VisionHttpTransport | None = None, *, image_renderer: Callable[[bytes], bytes] | None = None):
        self.transport = transport or UrllibVisionTransport()
        self.image_renderer = image_renderer or default_pdf_to_png

    def extract(self, company, filing, pages, config, *, cancel_check=None):
        try:
            _check_request(config, pages, filing)
            if not config.endpoint.startswith("https://"):
                raise VisionAdapterError("VISION_HTTPS_REQUIRED")
            if not config.model or not config.api_key:
                raise VisionAdapterError("VISION_CREDENTIALS_REQUIRED")
            import base64
            facts: list[FinancialFact] = []
            refs: list[EvidenceRef] = []
            diagnostics: list[str] = []
            for page in pages:
                if cancel_check and cancel_check():
                    raise VisionAdapterError("VISION_CANCELLED")
                image = self.image_renderer(page.pdf_bytes)
                if not image:
                    raise VisionAdapterError("VISION_IMAGE_RENDER_FAILED")
                payload = {
                    "model": config.model,
                    "messages": [{"role": "user", "content": [{"type": "text", "text": f"Return JSON only: {{facts:[{{concept,value,currency,unit_scale,statement,scope,period_end,original_page,raw_text}}]}}. Use only original_page {page.original_page}, consolidated scope, and the requested period."}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image).decode("ascii")}}]}],
                    "temperature": 0,
                }
                response = self.transport.request("POST", config.endpoint, headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}, body=json.dumps(payload).encode(), timeout=config.timeout_seconds)
                if cancel_check and cancel_check():
                    raise VisionAdapterError("VISION_CANCELLED")
                if response.status >= 400:
                    raise VisionAdapterError(_safe_error(response))
                parsed = response.json()
                content = _json_value(parsed, "choices.0.message.content", "content")
                if not isinstance(content, str):
                    raise VisionAdapterError("VISION_MALFORMED_RESPONSE")
                try:
                    structured = json.loads(content)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise VisionAdapterError("VISION_STRUCTURED_JSON_REQUIRED") from exc
                candidate = parse_vision_json(structured, company, filing, (page,))
                facts.extend(candidate.facts)
                refs.extend(candidate.evidence)
                diagnostics.extend(candidate.diagnostics)
            if not facts:
                return VisionExtractionResult((), (), tuple(diagnostics) or ("VISION_NO_CANDIDATES",), "VISION_NO_CANDIDATES")
            return VisionExtractionResult(tuple(facts), tuple(refs), tuple(diagnostics) + ("VISION_CUSTOM_COMPLETED",))
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


def _safe_zip_markdown(data: bytes, max_bytes: int) -> str:
    if len(data) > max_bytes:
        raise VisionAdapterError("VISION_SIZE_LIMIT")
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            total_uncompressed = 0
            for info in infos:
                if info.flag_bits & 0x1:
                    raise VisionAdapterError("VISION_ENCRYPTED_ARCHIVE")
                if info.file_size > max_bytes:
                    raise VisionAdapterError("VISION_SIZE_LIMIT")
                total_uncompressed += info.file_size
                if total_uncompressed > max_bytes:
                    raise VisionAdapterError("VISION_SIZE_LIMIT")
                if info.compress_size and info.file_size / max(1, info.compress_size) > 100:
                    raise VisionAdapterError("VISION_COMPRESSION_RATIO")
            names = [PurePosixPath(info.filename) for info in infos]
            if any(path.is_absolute() or ".." in path.parts or "\\" in str(path) for path in names):
                raise VisionAdapterError("VISION_UNSAFE_ARCHIVE")
            markdown_names = [path for path in names if path.name.lower() in {"full.md", "full.markdown"}]
            if not markdown_names:
                raise VisionAdapterError("VISION_MARKDOWN_MISSING")
            info = next(item for item in infos if item.filename == str(markdown_names[0]))
            if info.file_size > max_bytes:
                raise VisionAdapterError("VISION_SIZE_LIMIT")
            with archive.open(info, "r") as stream:
                raw = stream.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise VisionAdapterError("VISION_SIZE_LIMIT")
            return raw.decode("utf-8", errors="strict")
    except VisionAdapterError:
        raise
    except (zipfile.BadZipFile, UnicodeError, KeyError) as exc:
        raise VisionAdapterError("VISION_MALFORMED_ARCHIVE") from exc


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
