"""Registered official-source causes shared by execution and safe UI actions.

Only catalog codes are interpreted here, never provider response text. These
are availability/configuration diagnoses, not financial acceptance rules.
"""
TRANSIENT_SOURCE_CODES = frozenset({
    "REMOTE_TRANSPORT_FAILED", "REMOTE_HTTP_408", "REMOTE_HTTP_425", "REMOTE_HTTP_429",
    "REMOTE_HTTP_500", "REMOTE_HTTP_502", "REMOTE_HTTP_503", "REMOTE_HTTP_504",
    "FILING_FETCH_FAILED", "FILING_DISCOVERY_FAILED", "FILING_DOWNLOAD_FAILED",
    "FILING_TIMEOUT", "RATE_LIMITED",
    "SOURCE_NETWORK_UNAVAILABLE", "SOURCE_RATE_LIMITED",
})
LOCAL_PARSER_AVAILABILITY_CODES = frozenset({
    "PDF_WORKER_START_FAILED", "DOCUMENT_CONTEXT_TIMEOUT", "DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE",
    "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION",
    "PDF_BACKEND_IMPORT_FAILED", "PDFIUM_WORKER_START_FAILED", "PDFIUM_WORKER_EXIT",
    "PDFIUM_WORKER_TIMEOUT", "PDFIUM_WORKER_PROTOCOL_ERROR", "PDFIUM_WORKER_ERROR",
    "PDF_SOURCE_NOT_FOUND", "PDF_SOURCE_ACCESS_DENIED", "PDF_SOURCE_INVALID",
    "PDF_TEXT_EXTRACTION_FAILED", "DOCUMENT_CONTEXT_OCR_REQUIRED",
})
PDF_RUNTIME_FAILURE_CODES = frozenset({
    "PDF_BACKEND_IMPORT_FAILED", "PDFIUM_WORKER_START_FAILED", "PDFIUM_WORKER_EXIT",
    "PDFIUM_WORKER_PROTOCOL_ERROR", "PDFIUM_WORKER_ERROR", "PDF_WORKER_START_FAILED",
    "DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE",
})
SOURCE_ERROR_ACTIONS = {
    "SOURCE_NETWORK_UNAVAILABLE": "check_official_network_or_retry",
    "SOURCE_RATE_LIMITED": "wait_for_official_source_cooldown",
    "SOURCE_DNS_POLICY_BLOCKED": "configure_official_network_profile",
    "SOURCE_IDENTITY_INVALID": "verify_company_identity",
    "PERIOD_DECLARATION_NOT_FOUND": "repair_statement_header_or_enable_authorized_cloud",
    "PDF_WORKER_START_FAILED": "restore_local_worker_or_enable_authorized_cloud",
    "DOCUMENT_CONTEXT_TIMEOUT": "restore_local_worker_or_enable_authorized_cloud",
    "DOCUMENT_CONTEXT_LOCAL_UNAVAILABLE": "restore_local_worker_or_enable_authorized_cloud",
    "PDF_WINDOW_NO_PROGRESS_NEEDS_ACTION": "retry_saved_candidate_validation_or_enable_authorized_cloud",
    "FILING_STATUS_UNVERIFIED": "inspect_official_disclosure_status",
    "NO_FILINGS_AVAILABLE": "provide_official_source_or_retry_discovery",
    "FILING_CONTENT_UNSAFE": "repair_or_replace_conflicting_official_evidence",
    "FILING_CONTENT_INTEGRITY_FAILED": "repair_or_replace_conflicting_official_evidence",
    "FILING_CONTENT_HASH_MISMATCH": "repair_or_replace_conflicting_official_evidence",
    "FILING_SOURCE_IDENTITY_MISMATCH": "repair_or_replace_conflicting_official_evidence",
    "FILING_RULE_UNSUPPORTED": "provide_compatible_official_source",
    "FILING_FORMAT_UNSUPPORTED": "provide_compatible_official_source",
    "FILING_ACCESS_DENIED": "configure_official_source",
    "OFFICIAL_SOURCE_CONFIGURATION_REQUIRED": "configure_official_source",
    "SEC_CONTACT_CONFIGURATION_REQUIRED": "configure_sec_contact",
    **{code: "repair_local_pdf_runtime" for code in PDF_RUNTIME_FAILURE_CODES},
    "PDF_SOURCE_NOT_FOUND": "restore_registered_original",
    "PDF_SOURCE_ACCESS_DENIED": "repair_local_file_access",
    "PDF_SOURCE_INVALID": "replace_damaged_official_original",
    "PDF_TEXT_EXTRACTION_FAILED": "retry_alternate_parser_or_authorize_ocr",
    "DOCUMENT_CONTEXT_OCR_REQUIRED": "authorize_scanned_page_ocr",
}
