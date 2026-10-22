"""Small progress projection of the durable model plan, not a billing estimate."""


def status_plan_metadata(db, run_id: str) -> dict:
    reuses = db.execute("SELECT COUNT(*) FROM model_result_reuse WHERE run_id=?", (run_id,)).fetchone()[0]
    proofs = db.execute("SELECT COUNT(*) FROM model_assertion_proofs WHERE run_id=?", (run_id,)).fetchone()[0]
    from .research_execution.call_budget import plan_status
    return {"reuse_summary": {"local_result_reuses": reuses, "saved_judgments": proofs},
            "call_plan": plan_status(db, run_id)}
