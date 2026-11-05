"""Read-only A/B profile of the official planner on frozen QA inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import patch

from openthesis.domain import Company
from openthesis.packs import builtin_pack
from openthesis.providers import ModelConfig
from openthesis import research
from openthesis.research import ResearchContext, ResearchWorkflow
from openthesis.research_execution import production


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("facts", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--only-stage", action="append", default=[])
    args = parser.parse_args()
    started = time.perf_counter()
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        rows = db.execute("SELECT run_id,payload_json FROM artifacts WHERE artifact_type=?",
                          ("deterministic-financial-summary",)).fetchall()
        if len(rows) != 1:
            raise ValueError("Expected one frozen research input")
        run_id, raw = rows[0]
        run = json.loads(db.execute("SELECT payload_json FROM research_runs WHERE run_id=?", (run_id,)).fetchone()[0])
        value = db.execute("SELECT payload_json FROM artifacts WHERE run_id=? AND artifact_type=?",
                           (run_id, "deterministic-valuation")).fetchone()
        valuation = json.loads(value[0]) if value else None
    fact_bytes = args.facts.read_bytes()
    read_seconds = time.perf_counter() - started
    started = time.perf_counter()
    summary, facts = json.loads(raw), json.loads(fact_bytes)
    company = Company(**run["company"])
    context = ResearchContext(company, facts, summary["metrics"], summary["interim_metrics"], summary["evidence"],
        valuation, summary.get("market_snapshot"), summary["financial_quality"].get("period_coverage", {}),
        summary["repair_observations"], research_as_of=run.get("data_as_of"))
    parse_seconds = time.perf_counter() - started
    workflow = ResearchWorkflow(None, builtin_pack(), object(), ModelConfig("offline-qa-planner"))
    workflow._save = lambda *a, **k: None
    workflow._new_request_journal = lambda *a, **k: SimpleNamespace(
        prepare=lambda *a, **k: None, saved_generation_candidates=lambda: {}, candidate_read_issues=[])
    planner = production.plan_role_requests
    compile_prompt = research._agent_prompt_bundle
    result = {}
    wires = []

    class PlanningComplete(Exception):
        pass

    def capture(*args, **kwargs):
        start = time.perf_counter()
        if args_cli_stages:
            kwargs["reused_stage_ids"] = tuple(spec.stage_id for spec in args[1].stages
                                               if spec.stage_id not in args_cli_stages)
        rows = planner(*args, **kwargs)
        result.update(plan_seconds=time.perf_counter() - start, plan=rows)
        raise PlanningComplete

    def capture_wire(agent, *args, **kwargs):
        system, user = compile_prompt(agent, *args, **kwargs)
        tree = json.loads(user)
        prefix = {}
        for key, value in tree.items():
            if key == "agent":
                break
            prefix[key] = value
        wires.append({"agent": agent, "system_sha256": hashlib.sha256(system.encode()).hexdigest(),
                      "user_sha256": hashlib.sha256(user.encode()).hexdigest(),
                      "tree_sha256": hashlib.sha256(json.dumps(tree, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":")).encode()).hexdigest(),
                      "prefix_sha256": hashlib.sha256(json.dumps(prefix, ensure_ascii=False,
                          separators=(",", ":")).encode()).hexdigest()})
        return system, user

    start, cpu = time.perf_counter(), time.process_time()
    args_cli_stages = set(args.only_stage)
    with patch.object(production, "plan_role_requests", capture), patch.object(research, "_agent_prompt_bundle", capture_wire):
        try:
            workflow._execute_official_model_graph(context, None, [], publication=None)
        except PlanningComplete:
            pass
    import ctypes
    from ctypes import wintypes

    class ProcessMemory(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD),
                    ("peak_working_set", ctypes.c_size_t), ("working_set", ctypes.c_size_t),
                    ("peak_paged_pool", ctypes.c_size_t), ("paged_pool", ctypes.c_size_t),
                    ("peak_nonpaged_pool", ctypes.c_size_t), ("nonpaged_pool", ctypes.c_size_t),
                    ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t)]
    memory = ProcessMemory()
    memory.size = ctypes.sizeof(memory)
    ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    memory_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemory), wintypes.DWORD]
    memory_info(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(memory), memory.size)
    evidence = {**result, "wires": wires, "read_seconds": read_seconds, "parse_seconds": parse_seconds,
        "total_planning_seconds": time.perf_counter() - start, "planning_cpu_seconds": time.process_time() - cpu,
        "peak_working_set_bytes": memory.peak_working_set,
        "summary_sha256": hashlib.sha256(raw.encode()).hexdigest(), "facts_sha256": hashlib.sha256(fact_bytes).hexdigest(),
        "evidence": len(summary["evidence"]), "repair_observations": len(summary["repair_observations"]),
        "facts": len(facts), "model_calls": 0}
    if args.output:
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(evidence, output, indent=2)
    print(json.dumps({key: value for key, value in evidence.items() if key != "plan"}, indent=2))


if __name__ == "__main__":
    main()
