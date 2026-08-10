# OpenThesis Agent Workflow

## Roles

- The primary agent keeps the model selected for the current main task. Do not override or rewrite the primary model configuration.
- `lunahigh` is the only project-configured subagent. Never create or invoke Terra or any other custom or built-in subagent role for this repository.
- The primary agent owns requirement interpretation, complex diagnosis, architecture, implementation planning, tradeoff and security decisions, final diff review, and acceptance.
- `lunahigh` owns bounded execution after the primary agent has made the necessary decisions.

## Delegation policy

After understanding the request and defining a clear plan, the primary agent should delegate to `lunahigh` whenever the remaining work is primarily mechanical or implementation-oriented, including:

- writing or modifying code from clear requirements;
- creating, deleting, moving, or organizing in-scope project files;
- implementing an already-decided feature;
- small or medium refactors;
- code search and ordinary call-path tracing;
- clear, localized bug fixes;
- writing or updating tests;
- running tests, lint, typecheck, builds, commands, and scripts;
- iterating on ordinary test or compiler failures;
- repetitive code changes.

The primary agent should work directly only when the task still requires complex reasoning, architecture, ambiguous product decisions, difficult debugging, security or data-loss judgment, or final review.

## Handoff and edit ownership

1. The primary agent first analyzes the request and establishes the plan and acceptance criteria.
2. Respect the user's approval workflow. Do not delegate implementation before the user has authorized code changes when approval is required.
3. Give `lunahigh` a bounded task with exact scope, constraints, relevant files when known, and required checks.
4. While `lunahigh` is editing, the primary agent must not edit the same files. Prefer no primary-agent writes until the handoff completes.
5. `lunahigh` runs the relevant checks and handles routine repair loops before reporting back.
6. `lunahigh` returns only a compact summary: work completed, files changed, check results, and unresolved issues.
7. The primary agent reviews the resulting diff and validation evidence. If a follow-up is another clear implementation task, send it back to `lunahigh` instead of editing it directly.
8. External publication, Git commits, pushes, and releases still require the user's explicit authorization under the established OpenThesis workflow.

## Concurrency and containment

- Use at most one spawned subagent thread at a time.
- Always select the custom agent named `lunahigh`; do not fall back to `default`, `worker`, `explorer`, Terra, or another agent.
- Do not ask `lunahigh` to spawn further agents.
- Avoid parallel write-heavy work. The primary agent and `lunahigh` must never modify the same file concurrently.

