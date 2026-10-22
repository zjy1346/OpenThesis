"""Host-owned assertion proofs and preparation outcomes; never a network lane.

Keys include the assertion, source bytes, scope and validator protocol. A stage
name is not proof identity. Replaying a report cannot erase a preparation error
or transfer a verdict to rewritten text. Original candidates remain untouched.
"""
from __future__ import annotations

from copy import deepcopy
import json
import time
import uuid

from ..resilience.contracts import candidate_document_json, digest


SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS model_assertion_requests (
        run_id TEXT NOT NULL, assertion_id TEXT NOT NULL, protocol TEXT NOT NULL,
        stage_id TEXT NOT NULL, input_digest TEXT NOT NULL, configuration TEXT NOT NULL,
        PRIMARY KEY(run_id,assertion_id,protocol),
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS model_assertion_legacy_imports (
        run_id TEXT NOT NULL, protocol TEXT NOT NULL, PRIMARY KEY(run_id,protocol),
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS model_assertion_owners (
        run_id TEXT NOT NULL, assertion_id TEXT NOT NULL, protocol TEXT NOT NULL,
        owner_id TEXT NOT NULL, state TEXT NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(run_id,assertion_id,protocol),
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS model_assertion_proofs (
        run_id TEXT NOT NULL, assertion_id TEXT NOT NULL, protocol TEXT NOT NULL,
        proof_json TEXT NOT NULL, PRIMARY KEY(run_id,assertion_id,protocol),
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS model_assertion_flows (
        run_id TEXT NOT NULL, stage_id TEXT NOT NULL, query_digest TEXT NOT NULL,
        flow_json TEXT NOT NULL, PRIMARY KEY(run_id,stage_id,query_digest),
        FOREIGN KEY(run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE)""",
)


def acquire_assertions(journal, queries, protocol):
    """Atomically elect one sender across stage IDs and process restarts.

    No timeout can prove a submitted remote request did not run. Owners are
    released only with a confirmed not-submitted result, never by stealing an
    expired lease. A changed source creates a different assertion identity.
    """
    owner = uuid.uuid4().hex
    store = getattr(journal, "store", None)
    if store is None:
        return owner, list(queries), []
    owned, waiting = [], []
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for query in queries:
            identity = query["assertion_id"]
            db.execute("INSERT OR IGNORE INTO model_assertion_owners VALUES(?,?,?,?,?,?)",
                       (journal.run_id, identity, protocol, owner, "owned", time.time()))
            row = db.execute("SELECT owner_id FROM model_assertion_owners WHERE run_id=? AND assertion_id=? AND protocol=?",
                             (journal.run_id, identity, protocol)).fetchone()
            (owned if row[0] == owner else waiting).append(query)
    return owner, owned, waiting


def assertion_owner_state(journal, queries, protocol, owner, state):
    store = getattr(journal, "store", None)
    if store is None:
        return
    with store.connect() as db:
        for query in queries:
            key = (journal.run_id, query["assertion_id"], protocol, owner)
            if state == "not_submitted":
                db.execute("DELETE FROM model_assertion_owners WHERE run_id=? AND assertion_id=? AND protocol=? AND owner_id=?", key)
            else:
                db.execute("UPDATE model_assertion_owners SET state=?,updated_at=? WHERE run_id=? AND assertion_id=? AND protocol=? AND owner_id=?",
                           (state, time.time(), *key))


def bind_assertion_request(journal, queries, protocol, stage_id, request):
    """Persist only exact request identity; the journal already owns its body."""
    store = getattr(journal, "store", None)
    if store is None:
        return
    configuration = journal.stage_configuration(stage_id)
    with store.connect() as db:
        db.executemany("""INSERT INTO model_assertion_requests VALUES(?,?,?,?,?,?)
            ON CONFLICT(run_id,assertion_id,protocol) DO UPDATE SET stage_id=excluded.stage_id,
            input_digest=excluded.input_digest,configuration=excluded.configuration""",
            [(journal.run_id, query["assertion_id"], protocol, stage_id, request.input_digest, configuration) for query in queries])


def recover_received_proofs(journal, queries, protocol):
    """Revalidate a known received body locally, even after the sender crashed.

    The binding was committed before dispatch. No text search or guessed stage
    can adopt an unrelated response. Unknown outcomes stay locked; changed
    sources/validator identities cannot inherit this request or its proof.
    """
    store = getattr(journal, "store", None)
    if store is None or not queries:
        return {}
    from ..research_assertions import validate_response
    recovered = {}
    with store.connect() as db:
        rows = db.execute("""SELECT b.assertion_id,j.* FROM model_assertion_requests b
            JOIN model_request_journal j ON j.run_id=b.run_id AND j.stage_id=b.stage_id
              AND j.input_digest=b.input_digest AND j.configuration_fingerprint=b.configuration
            JOIN model_stage_request_heads h ON h.run_id=j.run_id AND h.stage_id=j.stage_id
              AND h.generation=j.generation
            WHERE b.run_id=? AND b.protocol=? AND j.purpose='semantic_verification'
              AND j.response_received=1 AND j.state IN ('candidate','late_candidate')""",
            (journal.run_id, protocol)).fetchall()
        expected = {query["assertion_id"]: query for query in queries}
        grouped = {}
        for row in rows:
            if row["assertion_id"] in expected:
                group = grouped.setdefault(row["request_id"], [row, []])
                group[1].append(expected[row["assertion_id"]])
        for row, relevant in grouped.values():
            try:
                # Digest/type validation is identical to exact journal replay.
                # Merely reading a known body does not clear an unknown lease.
                candidate = journal._read_candidate(row)
                recovered.update(validate_response(candidate, relevant, row["configuration_fingerprint"]))
            except (ValueError, TypeError):
                continue
    if recovered:
        save_proofs(journal, recovered, protocol)
    return recovered


def wait_assertions(journal, queries, protocol):
    store = getattr(journal, "store", None)
    if store is None or not queries:
        return {}
    deadline = time.monotonic() + max(.1, float(getattr(journal, "timeout_seconds", 1)))
    pending = {query["assertion_id"] for query in queries}
    while pending:
        if callable(getattr(journal, "cancel_check", None)) and journal.cancel_check():
            break
        with store.connect() as db:
            states = {row[0]: row[1] for row in db.execute(
                "SELECT assertion_id,state FROM model_assertion_owners WHERE run_id=? AND protocol=? AND assertion_id IN (" + ",".join("?" for _ in pending) + ")",
                (journal.run_id, protocol, *sorted(pending)))}
        if not any(state in {"owned", "dispatched"} for state in states.values()) or time.monotonic() >= deadline:
            break
        time.sleep(.05)
    saved = read_proofs(journal, queries, protocol)
    missing = [query for query in queries if query["assertion_id"] not in saved]
    return {**saved, **recover_received_proofs(journal, missing, protocol)}


def _memory(journal):
    if journal is None:
        return {"proofs": {}, "flows": {}}
    if not hasattr(journal, "_assertion_cache"):
        journal._assertion_cache = {"proofs": {}, "flows": {}}
    return journal._assertion_cache


def read_proofs(journal, queries, protocol):
    if not queries:
        return {}
    store = getattr(journal, "store", None)
    if store is None:
        records = list(_memory(journal)["proofs"].values())
    else:
        with store.connect() as db:
            identities = sorted({query["assertion_id"] for query in queries})
            records = []
            for start in range(0, len(identities), 256):
                keys = identities[start:start + 256]
                records.extend(json.loads(row[0]) for row in db.execute(
                    "SELECT proof_json FROM model_assertion_proofs WHERE run_id=? AND protocol=? AND assertion_id IN (" + ",".join("?" for _ in keys) + ")",
                    (journal.run_id, protocol, *keys)))
            imported = db.execute("SELECT 1 FROM model_assertion_legacy_imports WHERE run_id=? AND protocol=?", (journal.run_id, protocol)).fetchone()
        # Upgrade legacy, host-owned stage verification records locally. Never
        # read proof flags out of the model's candidate body.
        artifacts = []
        if not imported and not getattr(journal, "_assertion_legacy_loaded", False):
            from ..resilience.payloads import load_payload
            with store.connect() as db:
                for row in db.execute("SELECT payload_json FROM artifacts WHERE run_id=? AND artifact_type='stage-checkpoint'", (journal.run_id,)):
                    try:
                        artifacts.append({"artifact_type": "stage-checkpoint", "content": load_payload(db, row[0])})
                    except (ValueError, TypeError):
                        continue
        legacy = []
        for artifact in artifacts:
            content = artifact.get("content", {})
            if artifact.get("artifact_type") != "stage-checkpoint" or not isinstance(content, dict):
                continue
            validation = content.get("result", {}).get("validation", {})
            record = validation.get("record", {}) if isinstance(validation, dict) else {}
            if record.get("assertion_protocol") == protocol:
                legacy.extend(record.get("assertion_results", []))
        records.extend(legacy)
        if artifacts:
            save_proofs(journal, {item["assertion_id"]: item for item in legacy
                if isinstance(item, dict) and isinstance(item.get("assertion_id"), str)
                and item.get("protocol") == protocol and item.get("state") in {"supported", "contradicted"}}, protocol)
        journal._assertion_legacy_loaded = True
        if not imported:
            with store.connect() as db:
                db.execute("INSERT OR IGNORE INTO model_assertion_legacy_imports VALUES(?,?)", (journal.run_id, protocol))
    expected = {item["assertion_id"] for item in queries}
    return {item["assertion_id"]: deepcopy(item) for item in records if isinstance(item, dict)
            and item.get("assertion_id") in expected and item.get("protocol") == protocol
            and item.get("state") in {"supported", "contradicted"}}


def save_proofs(journal, proofs, protocol):
    if journal is None or not proofs:
        return
    store = getattr(journal, "store", None)
    if store is None:
        _memory(journal)["proofs"].update(deepcopy(proofs))
        return
    with store.connect() as db:
        db.executemany("""INSERT INTO model_assertion_proofs VALUES(?,?,?,?)
            ON CONFLICT(run_id,assertion_id,protocol) DO UPDATE SET proof_json=excluded.proof_json""",
            [(journal.run_id, key, protocol, candidate_document_json(value))
             for key, value in proofs.items()])


def flow_key(queries):
    return digest(sorted(item["assertion_id"] for item in queries))


def unknown_assertions(journal):
    """An unresolved remote outcome follows the assertion across stages."""
    store = getattr(journal, 'store', None)
    if store is None:
        flows = _memory(journal)['flows'].values()
    else:
        with store.connect() as db:
            flows = [json.loads(row[0]) for row in db.execute(
                'SELECT flow_json FROM model_assertion_flows WHERE run_id=?', (journal.run_id,))]
    return {identity for flow in flows for batch in flow.get('batches', ())
            if batch.get('submission_state') == 'remote_unknown'
            for identity in batch.get('assertion_ids', ()) if isinstance(identity, str)}


def read_flow(journal, stage_id, queries):
    key = flow_key(queries)
    store = getattr(journal, "store", None)
    if store is None:
        return deepcopy(_memory(journal)["flows"].get((stage_id, key), {}))
    with store.connect() as db:
        row = db.execute("SELECT flow_json FROM model_assertion_flows WHERE run_id=? AND stage_id=? AND query_digest=?",
                         (journal.run_id, stage_id, key)).fetchone()
    return json.loads(row[0]) if row else {}


def save_flow(journal, stage_id, queries, flow):
    if journal is None:
        return
    prior = read_flow(journal, stage_id, queries)
    # The original failure remains inspectable after a successful local check.
    history = list(prior.get("diagnostic_history", []))
    for code in (prior.get("diagnostic_code"), flow.get("diagnostic_code")):
        if code and code not in history:
            history.append(code)
    value = {**flow, "diagnostic_history": history}
    store = getattr(journal, "store", None)
    if store is None:
        _memory(journal)["flows"][(stage_id, flow_key(queries))] = deepcopy(value)
    else:
        with store.connect() as db:
            db.execute("""INSERT INTO model_assertion_flows VALUES(?,?,?,?)
                ON CONFLICT(run_id,stage_id,query_digest) DO UPDATE SET flow_json=excluded.flow_json""",
                (journal.run_id, stage_id, flow_key(queries), candidate_document_json(value)))
    return value
