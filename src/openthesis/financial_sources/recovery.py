"""Durable official GET/read operations, not another financial acceptance gate.

The first complete acquisition is journaled independently of the additional
repair incident. Two extra transient attempts share the existing company/run
repair budget. Captured payloads are immutable artifacts; the bounded recovery
journal contains only a receipt. Numeric facts still cross the pure compiler.
"""
from copy import deepcopy
from dataclasses import replace
import json
import time

from ..domain import FilingDocument, FinancialFact, utc_now_iso
from ..financial_recovery import financial_incident_scope
from ..financial_source_errors import TRANSIENT_SOURCE_CODES as _TRANSIENT_SOURCE_CODES, SOURCE_ERROR_ACTIONS as _SOURCE_ERROR_ACTIONS
from ..resilience.cloud_repository import CloudCandidateRepository
from ..resilience.contracts import (
    AuthorizationScope, Issue, RecoveryAction, RecoveryLimits, RecoveryPlan,
    RecoveryState, Usage, VerificationResult, canonical_json, digest, IncidentScope,
)
from ..resilience.coordinator import RecoveryCoordinator
from ..resilience.remote import remote_request_scope
from ..resilience.strategies import CandidateResult, RecoveryError, Strategy, StrategyRegistry
from .downloads import filing_cache_valid, filing_identity_matches


class OfficialSourceRepository(CloudCandidateRepository):
    head_namespace = "official-source:"
    artifact_namespace = "official-receipt:"
    candidate_schema = "openthesis.official-receipt.v1"
    artifact_type = "official-source-receipt"
    provider_id = "official-disclosure"
    producer_id = "financial-official-recovery"


class DurableOfficialSource:
    def __init__(self, source, store, run_id, company, *, cancel_check=None,
                 limits=None, clock=time.time, sleeper=time.sleep):
        self.source, self.store, self.run_id = source, store, run_id
        self.company = replace(company)
        self.cancelled = cancel_check or (lambda: False)
        self.scope = financial_incident_scope(company)
        self.limits = limits or RecoveryLimits(calls=32, pages=20)
        self.clock, self.sleeper = clock, sleeper

    def list_annual_filings(self, company, limit=5):
        return self._listing(company, limit)

    def list_financial_filings(self, company, limit=5):
        return self._listing(company, limit)

    def _listing(self, company, limit):
        loader = getattr(self.source, "list_financial_filings", None) or self.source.list_annual_filings
        material = {"company": company.to_dict(), "limit": limit}
        def decode(value):
            items = [FilingDocument(**item) for item in value]
            if any(item.company_cik != company.cik or not item.document_id
                   or not item.accession_number or not item.source_url for item in items):
                raise ValueError("FILING_SOURCE_IDENTITY_MISMATCH")
            return items
        return self._execute("discovery", material,
            lambda: [item.to_dict() for item in loader(replace(company), limit=limit)], decode)

    def download_filing(self, filing, target):
        frozen = replace(filing)
        material = frozen.to_dict()
        # Local capture timestamps and paths are not remote source identity.
        material.pop("ingested_at", None)
        material.pop("local_path", None)
        def decode(value):
            item = FilingDocument(**value)
            if (item.company_cik != frozen.company_cik or item.document_id != frozen.document_id
                    or item.accession_number != frozen.accession_number
                    or not filing_identity_matches(frozen, item)):
                raise ValueError("FILING_SOURCE_IDENTITY_MISMATCH")
            if not filing_cache_valid(item):
                raise ValueError("FILING_CONTENT_HASH_MISMATCH")
            return item
        return self._execute("download", material,
            lambda: self.source.download_filing(replace(frozen), target).to_dict(), decode)

    def get_company_facts(self, company):
        def decode(value):
            facts = [FinancialFact(**item) for item in value]
            if any(item.company_cik != company.cik for item in facts):
                raise ValueError("FILING_SOURCE_IDENTITY_MISMATCH")
            return facts
        return self._execute("structured", {"company": company.to_dict()},
            lambda: [fact.to_dict() for fact in self.source.get_company_facts(replace(company))], decode)

    def _execute(self, operation, material, fetch, decode):
        frozen_input = digest({"operation": operation, "input": material, "contract": "official-source-v1"})
        target = "official:" + operation + ":" + frozen_input

        def admit(receipt):
            if set(receipt) != {"artifact_id", "input_digest"} or receipt["input_digest"] != frozen_input:
                raise ValueError("OFFICIAL_RECEIPT_IDENTITY_INVALID")
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM artifacts WHERE artifact_id=? AND run_id=?",
                                 (receipt["artifact_id"], self.run_id)).fetchone()
            if row is None or row["artifact_type"] != "official-source-capture":
                raise ValueError("OFFICIAL_CAPTURE_MISSING")
            value = json.loads(row["payload_json"])
            if (receipt["artifact_id"] != "official-capture:" + digest({"run": self.run_id, **value})
                    or value["input_digest"] != frozen_input or value["operation"] != operation):
                raise ValueError("OFFICIAL_CAPTURE_INTEGRITY_FAILED")
            decode(deepcopy(value["result"]))
            return dict(receipt)

        repository = OfficialSourceRepository(self.store, self.run_id, target, frozen_input,
            cancel_check=self.cancelled, admit_candidate=admit)
        issue = Issue("OFFICIAL_SOURCE_REQUIRED", "source", operation, self.run_id,
                      frozen_input, affected_ids=(target,), retryability="transient")
        configuration = digest({"provider": "official-disclosure", "contract": "official-source-v1"})
        authorization = AuthorizationScope(("official-disclosure",), (), (configuration,))

        def produce(context):
            try:
                with remote_request_scope(context):
                    result = fetch()
            except RecoveryError:
                raise
            except PermissionError as exc:
                raise RecoveryError("OFFICIAL_LOCAL_IO_UNAVAILABLE",
                    needs_action="repair_local_data_directory_access") from exc
            except (OSError, TimeoutError) as exc:
                if getattr(exc, "filename", None):
                    raise RecoveryError("OFFICIAL_LOCAL_IO_UNAVAILABLE",
                        needs_action="repair_local_data_directory_access") from exc
                raise RecoveryError("REMOTE_TRANSPORT_FAILED", transient=True,
                                    needs_action="retry_official_source") from exc
            except Exception as exc:
                # Provider adapters already expose catalog codes. Preserve
                # registered causes, never classify arbitrary provider prose
                # or an unknown implementation exception as a network outage.
                code = getattr(exc, "code", "")
                if code in _TRANSIENT_SOURCE_CODES or code in _SOURCE_ERROR_ACTIONS:
                    raise RecoveryError(code, transient=code in _TRANSIENT_SOURCE_CODES,
                        needs_action=_SOURCE_ERROR_ACTIONS.get(code, "retry_official_source")) from exc
                raise
            value = {"operation": operation, "input_digest": frozen_input, "result": result}
            encoded = canonical_json(value)
            artifact_id = "official-capture:" + digest({"run": self.run_id, **value})
            context.checkpoint()
            with self.store.connect() as db:
                row = db.execute("SELECT payload_json FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
                if row and row["payload_json"] != encoded:
                    raise ValueError("ARTIFACT_IDENTITY_CONFLICT")
                if not row:
                    db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
                        (artifact_id, self.run_id, "official-source-capture", target, encoded,
                         "official-disclosure", "financial-official-recovery", utc_now_iso()))
            return CandidateResult({"artifact_id": artifact_id, "input_digest": frozen_input}, (artifact_id,))

        def verify(candidate, _plan):
            try:
                admit(candidate.payload)
                return VerificationResult(True, (issue.issue_id,))
            except (KeyError, TypeError, ValueError):
                return VerificationResult(False, remaining_issue_ids=(issue.issue_id,),
                    invariant_failures=("OFFICIAL_CAPTURE_INTEGRITY_FAILED",))

        registry = StrategyRegistry()
        for phase in ("initial", "repair"):
            action = RecoveryAction("official-" + phase, "1", (target,), frozen_input,
                "official-source-proof-v1", configuration_fingerprint=configuration,
                provider_id="official-disclosure",
                estimated_usage=Usage(calls=128 if phase == "initial" else 8, seconds=60))
            registry.register(Strategy(action.strategy_id, action.strategy_version, action.verifier_id,
                produce, verify, lambda _plan, _action: True))
            # The initial acquisition is not one of the eight extra repairs.
            # Initial + one repair + one transient retry = at most 3 attempts.
            limits = (RecoveryLimits(automatic_actions=1, transient_retries=0, calls=128,
                      seconds=600, lease_seconds=65) if phase == "initial"
                      else replace(self.limits, transient_retries=1, lease_seconds=65))
            scope = None if phase == "initial" else self.scope
            incident = "incident:" + digest({"run": self.run_id,
                "target": scope.target if scope else target + ":initial",
                "input": scope.input_digest if scope else frozen_input})
            # Baseline target scope is distinct for accounting, while its
            # actual source target/lease is shared with the repair plan.
            budget_scope = scope or IncidentScope(target + ":initial", frozen_input)
            plan = RecoveryPlan(incident, self.run_id, target, repository.generation,
                frozen_input, (issue,), (action,), limits=limits,
                authorization_scope=authorization, budget_scope=budget_scope)
            outcome = RecoveryCoordinator(repository, registry, clock=self.clock, sleeper=self.sleeper).execute(
                plan, expected_generation=repository.generation, plan_hash=plan.plan_hash, authorization=authorization)
            if outcome["state"] == RecoveryState.COMMITTED:
                receipt = repository.committed_result(outcome["committed_revision_id"])
                if receipt is None:
                    raise RecoveryError("OFFICIAL_CAPTURE_INTEGRITY_FAILED")
                admit(receipt)
                with self.store.connect() as db:
                    row = db.execute("SELECT payload_json FROM artifacts WHERE artifact_id=?",
                                     (receipt["artifact_id"],)).fetchone()
                return decode(json.loads(row["payload_json"])["result"])
            code = str(outcome.get("diagnostic_code") or "OFFICIAL_SOURCE_UNAVAILABLE")
            transient = code in _TRANSIENT_SOURCE_CODES
            if phase == "initial" and transient:
                continue
            raise RecoveryError(code, transient=transient,
                waiting_external=outcome["state"] == RecoveryState.WAITING_EXTERNAL,
                needs_action=(outcome.get("next_actions") or ("repair_required_official_evidence",))[0])
