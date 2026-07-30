# Clara → Clive Automatic Parallel Orchestration Implementation Plan

> **For Hermes:** Implement with strict TDD. Preserve all unrelated dirty work and do not commit, push, deploy, or perform business writes.

**Goal:** Let Clara automatically split substantial coding work, run the separate `clive` Hermes profile asynchronously in an isolated worktree, continue Clara-owned work in parallel, gate the result through Coda, and integrate only a conflict-free reviewed patch.

**Architecture:** Reuse Hermes Kanban as the durable queue, worker launcher, task/run log, timeout, retry, crash detection, and named-profile boundary. Add one thin Clara–Clive coordinator that owns worktree creation, exact file ownership, pipeline state, patch capture, Coda review handoff, conflict checks, apply/cancel, and a JSON CLI contract callable from Claude Code. Do not create a second generic scheduler.

**Tech stack:** Python stdlib, SQLite, git worktrees/patches, existing `hermes_cli.kanban_db`, existing `clive` and `coda` profiles.

---

## Acceptance criteria

1. Clara automatically dispatches only when a request has at least one substantial Clive-owned unit and one non-overlapping Clara-owned unit; small/unsafe/dirty-repo work stays direct.
2. Dispatch returns immediately with an orchestration job ID, Kanban task ID, worktree path, and supervisor PID.
3. Clive runs as the separate Hermes profile `clive` in a detached clean git worktree while Clara may edit only declared Clara-owned files in the main checkout.
4. Exact ownership is persisted transactionally; overlapping jobs are rejected.
5. Clive may change only declared paths and may not commit/push. Unowned changes, worktree HEAD drift, or main-checkout changes to Clive-owned paths block integration.
6. Clive completion automatically creates and dispatches a read-only `coda` review task in the same worktree.
7. Coda must explicitly pass; final Git-visible worktree/index divergence or blocking findings prevent integration. The cooperative read-only policy does not claim to monitor ignored, metadata-only, or edit-then-restore activity.
8. Status, wait, apply, timeout, worker crash, block, cancellation, and cleanup are represented by durable states and auditable Kanban task/run IDs.
9. Applying uses a binary git patch, runs `git apply --check` first, and never touches files outside the declared Clive scope.
10. A real E2E demonstrates temporal overlap: Clive is still running when Clara writes a declared Clara-owned file, then Coda passes and the reviewed Clive patch applies.

Boundary note: this is a trusted-agent concurrency and integration guard, not an OS sandbox against malicious same-user code. The coordinator and reviewed patch are scope-restricted; worktree checks, worker tool limits, environment scrubbing, and process quiescence detect/prevent accidental violations.

## Task 1: Coordinator contracts and ownership registry — RED

**Files:**
- Create: `tests/hermes_cli/test_clara_clive_orchestrator.py`
- Create: `hermes_cli/clara_clive_orchestrator.py`

Write failing tests for:
- path normalization/traversal rejection
- exact and prefix ownership conflicts
- transactional acquire/release
- clean-repo requirement
- detached worktree creation
- job state persistence

Run: `scripts/run_tests.sh tests/hermes_cli/test_clara_clive_orchestrator.py`
Expected before implementation: import/behavior failures.

## Task 2: Durable dispatch and Clive worker lifecycle — RED/GREEN

**Files:**
- Modify: `tests/hermes_cli/test_clara_clive_orchestrator.py`
- Modify: `hermes_cli/clara_clive_orchestrator.py`
- Modify: `tests/hermes_cli/test_kanban_worker_spawn_toolsets.py`
- Modify: `hermes_cli/kanban_db.py`

Implement:
- named board initialization
- Clive implementation card with exact files/tests/safety contract
- immediate CAS-safe Kanban dispatch
- detached supervisor process and state transitions
- profile-worker environment scrubbing of orchestrator/provider overrides
- bridge-worker completion instructions using the Kanban CLI when native `kanban_*` tools are unavailable

Verify focused tests fail first, then pass.

## Task 3: Patch capture, Coda gate, and conflict refusal — RED/GREEN

**Files:**
- Modify: `tests/hermes_cli/test_clara_clive_orchestrator.py`
- Modify: `hermes_cli/clara_clive_orchestrator.py`

Implement:
- reject unowned worktree changes and worktree commits
- stage only inside the disposable worktree and save a binary cached diff
- persist patch SHA and Clive structured handoff
- create Coda review task only after implementation validation
- require Coda PASS metadata/summary
- prove Coda did not mutate the patch
- verify main HEAD and owned-file fingerprints before apply
- `git apply --check` then apply; release locks on terminal state

## Task 4: Timeout, failure, cancel, wait, and cleanup — RED/GREEN

**Files:**
- Modify: `tests/hermes_cli/test_clara_clive_orchestrator.py`
- Modify: `hermes_cli/clara_clive_orchestrator.py`

Implement CLI commands:
- `dispatch`
- `status`
- `wait [--apply]`
- `cancel`
- `cleanup`
- internal `supervise`

Cancellation may terminate/reclaim only PIDs attached to this job's Kanban run. Preserve failed worktrees for diagnosis; remove generated worktrees only after successful integration or explicit cleanup.

## Task 5: Clara automatic-use contract

**Files:**
- Modify: `gateway/claude_code_bridge.py`
- Modify: `tests/gateway/test_claude_code_bridge_builder.py`
- Modify: `/Users/392yes/.hermes/profiles/clara/SOUL.md`
- Modify: `/Users/392yes/.claude/agents/clara.md`
- Create: `/Users/392yes/.claude/skills/clara-clive-orchestration/SKILL.md`
- Create: `/Users/392yes/.local/bin/clara-clive`
- Modify: `/Users/392yes/.hermes/profiles/clive/SOUL.md`
- Modify: `/Users/392yes/.hermes/skills/orchestration/hugo-crew-orchestration/SKILL.md`

Lead prompt rules:
- assess parallelizability before editing
- dispatch automatically when the split is safe
- print/store returned IDs
- continue Clara-owned work before waiting
- call `wait --apply` before integration completion
- run final verification after apply
- do not simulate dispatch or claim success from prose

## Task 6: Real E2E and independent review

Create a disposable clean git repository with one Clive-owned file and one Clara-owned file. Dispatch without naming Clive in the user-level task, modify the Clara-owned file while the implementation task is running, verify overlapping timestamps, wait through Coda review, apply, and run tests.

Then run:
- coordinator tests
- Kanban worker spawn tests
- Claude bridge builder/progress tests
- Python compilation and `git diff --check`
- Coda/Codex independent code, regression, security, and race-condition review

No commits are created by this implementation session.
