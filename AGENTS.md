# OpenThesis Agent Workflow

## Roles

- The primary agent keeps the model selected for the current main task. Do not override or rewrite the primary model configuration.
- All spawned subagents for this repository must use the project-configured `lunahigh` agent. Multiple `lunahigh` subagents may be used when parallel work is genuinely useful or necessary.
- The primary agent owns requirement interpretation, complex diagnosis, architecture, implementation planning, tradeoff and security decisions, final diff review, and acceptance.
- `lunahigh` owns bounded execution after the primary agent has made the necessary decisions.


## User approval and release workflow

Every user-requested software modification must follow this workflow:

1. On the first conversational round for a new modification request, do not modify application code or implement the requested feature. Creating or updating the change-record Markdown file described below is allowed.
2. During that first round, locate the source of the problem or the relevant implementation area, explain the cause or current behavior when it can be determined, and report a proposed solution to the user. The agent may ask focused questions or present implementation choices when needed.
3. In that same first round, create a Markdown change record named after the planned target version, such as `1.4.1.md`. The file must record every requested change, the proposed solution or decisions still requiring user choice, relevant scope or files when known, and a checklist that can later be used for acceptance.
4. Do not modify application code, add features, or begin implementation until the user explicitly approves the proposed modification.
5. After the user approves implementation, perform the authorized code and feature changes. Delegation to `lunahigh` must still follow the role, ownership, and concurrency rules in this document.
6. After implementation, perform acceptance against the corresponding `<version>.md` change record. Verify every recorded user requirement individually. Do not claim the work is complete while any recorded requirement is missing, only partially implemented, or unverified.
7. Once all recorded requirements pass acceptance, build and package a release artifact for the user to test.
8. Wait for the user to test the release artifact. Only after the user explicitly states that everything is working correctly and explicitly authorizes uploading or publishing to GitHub may the project create the final Git commit, push changes, and publish the new GitHub Release.

## Minimal release-call principle

- If the application code, dependencies, packaging-affecting configuration, and existing artifact hash are unchanged, do not rebuild or package again.
- If the same code and the same artifact already have a trustworthy privacy-scan record, do not repeat that identical scan.
- Any change that can affect the shipped artifact invalidates the previous build and scan evidence; rerun the necessary validation for the changed artifact.
- Prefer delegating mechanical staging, committing, uploading, and remote verification to `lunahigh`; the primary agent retains scope, security, and final-review ownership.
- The minimal-call principle must never be used to skip the first necessary validation.

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

- The primary agent may spawn multiple subagent threads when parallel work is genuinely useful or necessary. Do not create extra subagents when the work can be handled clearly and efficiently by fewer agents.
- Every spawned subagent must use the custom agent named `lunahigh`, which means Luna with reasoning effort set to `high`. Do not use `default`, `worker`, `explorer`, Terra, or any other custom or built-in subagent role.
- The primary agent is responsible for dividing parallel work into clearly separated scopes before spawning multiple `lunahigh` subagents.
- Do not ask any `lunahigh` subagent to spawn further agents. All subagents must be created and coordinated by the primary agent.
- Avoid overlapping write ownership. The primary agent and any `lunahigh` subagent, and multiple `lunahigh` subagents, must never modify the same file concurrently.
