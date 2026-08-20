---
type: session-handover
canonical: true
project: hermes-agent
session_end: 2026-08-20 13:45
git_branch: feat/clara-lead-model-swap-handoff100
git_commit: c7b550aa7a
---

# Session Handover — hermes-agent (2026-08-20 13:45)

## 1. 현재 상태 (다음 세션 시작점)

`/Users/392yes/.local/bin/hermes-loadout` 일반 TUI turn 상태 기능은 commit `42a32c6874`에서 구현·검증·push 완료됐다. 그러나 handoff 작성 뒤 concurrent successor `c7b550aa7ab40cda950816703726787ad218adb9` (`fix(cli): show canonical background loadout status`)가 같은 branch에 생성·push됐고, exact committed tree 검증에서 status-bar 회귀 1건이 확인됐다: 관련 181 tests 중 `180 passed, 1 failed`. 현재 HEAD와 upstream은 모두 `c7b550aa7a`지만 이 successor는 green으로 간주하지 않는다. Hugo/Clara 공통 다음 시작점은 `/session-resume` 후 `tests/cli/test_cli_status_bar.py::TestCLIStatusBar::test_compression_count_in_wide_fragments` 회귀를 먼저 수정·검증하고 Coda review를 받는 것이다. PR/merge/deploy는 요청·실행되지 않았다.

## 2. 가장 최근 작업 (100% 보존)

### 2.1 Handoff 뒤 도착한 concurrent successor와 현재 blocker

Handoff stabilization 직전 branch/remote가 원래 feature SHA `42a32c6874`에서 다음 commit으로 이동한 것을 감지했다.

- commit: `c7b550aa7ab40cda950816703726787ad218adb9`
- message: `fix(cli): show canonical background loadout status`
- tree: `c28309bcf33b8e5c355c00bba8ee22361f3e31b5`
- 현재 local HEAD와 `fork/feat/clara-lead-model-swap-handoff100`가 모두 이 SHA다.
- commit scope: `cli.py`, `hermes_cli/loadout_orchestrator_status.py`, `tests/cli/test_loadout_orchestrator_status.py`, `tests/tools/test_process_registry.py`, `tools/process_registry.py`; `1176 insertions`, 신규 파일 2개.
- 이 process/owner는 현재 Hermes process registry에 남아 있지 않고, handoff 작성자가 실행한 background handle도 아니다. 도착 시각이 아니라 exact commit tree를 기준으로 검증했다.

Exact detached worktree에서 실행한 명령:

`./scripts/run_tests.sh tests/cli/test_loadout_orchestrator_status.py tests/cli/test_loadout_turn_status.py tests/cli/test_cli_status_bar.py tests/cli/test_tool_progress_scrollback.py tests/cli/test_cli_yolo_toggle.py tests/tools/test_process_registry.py`

결과:

- `180 passed, 1 failed`
- 실패 node: `tests/cli/test_cli_status_bar.py::TestCLIStatusBar::test_compression_count_in_wide_fragments`
- 실패 원문: `AssertionError: assert '🗜️ 7' in [' ⚕ claude-sonnet-4-20250514 [█░░░░░░░░░] 1% 4h 47m │ 12.4K/200K │ 🗜️ 7 │ 15m │...']`
- 같은 node를 직전 exact commit `42a32c6874aaa1e158c7bf3fa3a874280da03361`에서 fresh 실행한 결과: `1 passed in 0.38s`.
- 분류: `c7b550aa7a`에서 도입된 deterministic status-bar fragment contract regression. combined-suite order contamination이나 temp-dir 문제로 재분류하지 않는다.
- `c7b550aa7a`에 대한 independent Coda review는 아직 없다. 기존 Coda `APPROVED (0/0/0)`는 `42a32c6874` 범위에만 유효하다.

### 2.2 선택 커밋과 push

- 작업 저장소: `/Users/392yes/.hermes/hermes-agent`
- branch: `feat/clara-lead-model-swap-handoff100`
- upstream: `fork/feat/clara-lead-model-swap-handoff100`
- feature commit:
  - full SHA: `42a32c6874aaa1e158c7bf3fa3a874280da03361`
  - message: `feat(cli): show live status in hermes-loadout`
  - commit tree: `77857a5ed6d9af9892932a26959af207118561e7`
- push 명령/결과:
  - `git push fork "HEAD:refs/heads/feat/clara-lead-model-swap-handoff100"`
  - remote update: `f53b1117b3..42a32c6874`
  - local SHA와 remote SHA 모두 `42a32c6874aaa1e158c7bf3fa3a874280da03361`
- 함께 push된 직전 local commit: `278b7eb4ba fix: pass failing api_key hint to credential pool 401 recovery`
- push 이후 branch divergence 없음. PR 생성, merge, deploy는 하지 않았다.

`cli.py`에는 이번 작업 전부터 reasoning override와 quiet bridge 관련 dirty 변경이 섞여 있었다. raw `git diff -- cli.py`를 hunk 단위로 분석해 `loadout` marker가 있는 17개 hunk만 `/tmp/hermes-loadout-cli.patch`로 추출하고 `git apply --cached --check` 후 index에 적용했다. 신규 파일 두 개만 pathspec으로 stage했다. staged diff는 다음 정확한 3파일이었다.

- `M cli.py`
- `A hermes_cli/loadout_turn_status.py`
- `A tests/cli/test_loadout_turn_status.py`

기계적 scope 검증 결과:

- `staged_cli_hunks=17 all_loadout_scoped=yes`
- `unrelated_staged_tokens=none`
- `unstaged_loadout_hunks=none`
- post-commit: `unstaged_loadout_hunks=none`, `unrelated_cli_work_preserved=yes`

### 2.3 최종 구현 파일과 동작

1. `/Users/392yes/.hermes/hermes-agent/hermes_cli/loadout_turn_status.py`
   - launcher-scoped presentation-only thread-safe 상태 머신.
   - 9개 상태와 emoji-label:
     - `🟢 RUNNING`
     - `🔵 WAITING`
     - `🟡 DELAYED`
     - `🟠 STALLED`
     - `🔴 DISCONNECTED`
     - `🟣 WAITING APPROVAL`
     - `✅ COMPLETED`
     - `❌ FAILED`
     - `⛔ STOPPED`
   - 기본 threshold: 진행 이벤트 60초 부재 시 `DELAYED`, 300초 부재 시 `STALLED`.
   - `DISCONNECTED / COMPLETED / FAILED / STOPPED` terminal 상태는 다음 turn의 `start()` 전까지 latch한다. 늦은 thinking/tool callback이 terminal 상태를 `WAITING/RUNNING`으로 되돌리지 못한다.

2. `/Users/392yes/.hermes/hermes-agent/cli.py`
   - `_loadout_status_enabled()`는 startup-preloaded skill 목록에 정확히 `hermes-loadout`이 있을 때만 활성화한다. 일반 Hermes와 다른 launcher footer는 변하지 않는다.
   - status snapshot, plain-text renderer, 실제 prompt_toolkit fragment renderer의 narrow/medium/wide 경로에 현재 상태 label을 연결했다.
   - lifecycle mapping:
     - `chat()` 시작 → `RUNNING`
     - thinking/model response → `WAITING`
     - tool started → `RUNNING`
     - tool completed → `WAITING`
     - approval modal active → `WAITING APPROVAL`
     - completed/failed/interrupted/missing result → `COMPLETED/FAILED/STOPPED/DISCONNECTED`
   - runtime credential 또는 agent init 실패는 `FAILED`로 종결한다.
   - `@context` hard-limit 차단 조기 return도 `FAILED`로 종결한다.
   - 상태는 conversation history, approval state, 별도 `scripts/orchestrate.py` canonical state를 수정하지 않는다.

3. `/Users/392yes/.hermes/hermes-agent/tests/cli/test_loadout_turn_status.py`
   - 최종 22 tests.
   - 9개 emoji-label, waiting→delayed→stalled, approval 비승격, launcher scope/negative scope, 실제 fragment renderer, 10/32/51/52/75/76/120/200열 overflow, thinking/tool lifecycle, approval override, dead-worker completion race, terminal result, credential failure, context injection blocked, late callback latch를 고정한다.

4. `/Users/392yes/.hermes/skills/devops/hermes-loadout/SKILL.md`
   - git 밖의 active skill 문서에 `hermes-loadout` 터미널 런처와 `/hermes-loadout` slash 오케스트레이터의 계층 차이를 기록했다.
   - 이 문서 변경은 active runtime에는 적용됐지만 Git feature commit에는 포함되지 않는다.

### 2.4 최종 검증 증거

Exact staged snapshot을 검증하기 위해 다음 절차를 사용했다.

- `git write-tree`
- `git commit-tree`로 임시 candidate `44656255105a82f9d03d7e2ac6b43ea033be889b` 생성
- `/tmp/hermes-loadout-stage.*` detached worktree 생성
- 실행 명령:
  - `./scripts/run_tests.sh tests/cli/test_loadout_turn_status.py tests/cli/test_cli_status_bar.py tests/cli/test_tool_progress_scrollback.py tests/cli/test_cli_yolo_toggle.py`
- 결과:
  - `95 tests passed, 0 failed`
  - `tests/cli/test_loadout_turn_status.py`: 22 passed
  - `tests/cli/test_cli_status_bar.py`: 45 passed
  - `tests/cli/test_tool_progress_scrollback.py`: 15 passed
  - `tests/cli/test_cli_yolo_toggle.py`: 13 passed
- `cli.py`, `hermes_cli/loadout_turn_status.py`, `tests/cli/test_loadout_turn_status.py` AST parse 통과.
- `git diff --check` 통과.
- 임시 candidate tree와 실제 feature commit tree가 모두 `77857a5ed6d9af9892932a26959af207118561e7`로 일치했다.

Final live E2E:

- 새 PTY에서 `TERM=xterm-256color /Users/392yes/.local/bin/hermes-loadout` 실행.
- 일반 task가 terminal tool로 `python3 -c 'import time; time.sleep(2); print("ok")'`를 실행하도록 입력.
- 전체 redraw log에서 `RUNNING → WAITING → RUNNING → WAITING → COMPLETED` 확인.
- `DISCONNECTED/FAILED` 오탐 없음.
- 테스트 PTY는 `/exit`로 정상 종료, exit code `0`.
- 최종 `process list`: 빈 목록.

### 2.5 Coda 독립 리뷰와 해결 ledger

Coda/Codex는 실제 dirty worktree `/Users/392yes/.hermes/hermes-agent`, branch `feat/clara-lead-model-swap-handoff100`, 당시 HEAD `278b7eb4...`에서 read-only 2단계 리뷰를 수행했다.

초기 Stage 1은 PASS였고 Stage 2에서 다음을 재현했다.

1. Important: blocked `@context`가 `RUNNING` 상태로 조기 return.
   - 재현: `chat_result='blocked'`, `history_len=0`, `status_after_return=RUNNING`.
   - 수정: `cli.py`의 blocked return 직전에 `_set_loadout_turn_status("FAILED")`.
2. Important: interrupt 후 살아 있는 worker의 늦은 callback이 `STOPPED -> RUNNING`으로 덮어씀.
   - 수정: 상태 머신 terminal latch.
3. Minor: exact emoji, 위 두 회귀, narrow-width coverage 부족.
   - 수정: exact label mapping, 두 regression, 8개 width parameter test 추가.

RED 증거:

- `AssertionError: assert 'RUNNING' == 'STOPPED'`
- `AssertionError: assert 'RUNNING' == 'FAILED'`

수정 후 exact focused nodes `2 passed`, 전체 관련 suite `95 passed`.

Coda 재리뷰는 같은 worktree에서 F1/F2/F3 모두 `FIXED`로 판정했다.

- 남은 `Critical 0 · Important 0 · Minor 0`
- final verdict: `APPROVED`
- Coda read-only sandbox의 첫 pytest 시도는 제품 오류가 아니라 temp 디렉터리 제약으로 setup 실패했다.
  - 원문: `FileNotFoundError: [Errno 2] No usable temporary directory found in [...]`
  - `--confcutdir=tests/cli`로 exact 4 nodes/8 width parameters를 재실행해 `11 passed, 0 failed`.

## 3. 이전 작업 (내림차순 압축)

### 3.1 정상 완료 직전 `DISCONNECTED` flicker 진단·수정

첫 실제 PTY에서 `WAITING → RUNNING → WAITING` 이후 agent thread가 끝났지만 `chat()`이 result를 finalization하기 전 renderer가 dead worker만 보고 `🔴 DISCONNECTED`를 잠시 표시했다. 회귀 테스트 `test_dead_worker_does_not_flicker_disconnected_before_result_finalization`에서 `AssertionError: assert 'DISCONNECTED' == 'RUNNING'` RED를 확인했다. renderer의 worker-liveness 추측을 제거하고, `result is None and not worker_alive`가 finalization에서 확인된 경우만 명시적으로 `DISCONNECTED`를 기록하도록 수정했다. 이후 실제 PTY 전체 redraw에 `DISCONNECTED`가 없음을 확인했다.

### 3.2 근본 원인과 최초 연결

`/Users/392yes/.local/bin/hermes-loadout`은 일반 Hermes TUI를 띄우고 skills만 preload한다. 기존 9상태 renderer는 `/hermes-loadout <task>`가 `scripts/orchestrate.py`를 시작한 별도 canonical score-loop에서만 동작했다. 따라서 사용자가 새 terminal에서 일반 task를 실행했을 때 상태가 보이지 않은 것은 두 계층 사이에 lifecycle/footer 연결이 없었기 때문이다. 일반 turn 상태를 별도 presentation-only 상태 머신으로 만들고 `hermes-loadout` preload 시에만 footer에 연결했다.

최초 lifecycle RED에서는 다음 오류/실패를 확인했다.

- `AttributeError: 'HermesCLI' object has no attribute '_finish_loadout_turn_status'`
- thinking callback 후 기대 `WAITING`, 실제 `RUNNING`
- approval modal 후 기대 `WAITING APPROVAL`, 실제 `RUNNING`

구현 후 11 tests GREEN, 이후 race/Coda findings/width coverage를 추가해 최종 22 tests로 확장했다.

### 3.3 기존 `/hermes-loadout` orchestrator 상태 구현

별도 installed skill의 `scripts/orchestrate.py` canonical 경로는 이미 9상태 분류와 3줄 status renderer를 보유하며, 이전 검증에서 orchestrator `96/96`, installer `7/7`, Coda 잔여 finding `0/0/0`을 통과했다. 이번 commit은 그 canonical run state를 재사용하거나 변경하지 않고 일반 TUI turn의 presentation 상태만 추가했다.

### 3.4 승인 정책 메모

active `/Users/392yes/.hermes/skills/devops/hermes-loadout/SKILL.md`에는 기본 `critical-only`와 routine reversible recovery 자동 처리가 이미 문서화돼 있다. 이번 Git feature commit은 approval policy 자체를 변경하지 않았고 TUI status lifecycle만 다뤘다. 승인 정책을 추가 감사·수정할 경우 별도 task로 현재 `scripts/orchestrate.py` state/CLI behavior를 다시 검증해야 한다.

## 4. 사용자 결정사항·승인 내역 (무압축)

- 2026-08-20: 사용자는 일반 `hermes-loadout` 새 터미널에서 별도 `/hermes-loadout status` 명령 없이 상태가 자동 표시되길 요구했다.
- 2026-08-20: 상태 표시는 startup-preloaded `hermes-loadout` TUI에만 적용하고 일반 Hermes/다른 launcher에는 노출하지 않는 범위로 구현했다.
- 2026-08-20: 일반 turn presentation 상태와 `/hermes-loadout` orchestrator canonical run state를 분리했다. approval/conversation/canonical state는 변경하지 않는다.
- 2026-08-20: 사용자가 명시적으로 `커밋하라`고 승인했다. 기존 unrelated dirty 변경은 제외하고 status feature만 선택 커밋했다.
- 2026-08-20: 사용자가 명시적으로 `푸시하라`고 승인했다. fork의 현재 branch로 non-force push했다.
- 2026-08-20: 사용자가 session handoff 작성 뒤 `커밋하고 푸시하라`고 명시했다. 승인 범위는 canonical `handover.md` 단독 docs-only commit과 현재 fork upstream으로의 non-force push다.
- 2026-08-20: PR 생성, merge, deploy, runtime restart는 승인하거나 요청하지 않았다.
- 기존 사용자 작업을 보존한다. 현재 worktree의 reasoning override, quiet bridge, gateway/tests/docs 등 unrelated tracked/untracked 변경을 stage, reset, stash, clean, 삭제하지 않는다.

## 5. 미완료 작업 / 다음 액션

- [ ] 최우선 blocker: `c7b550aa7a` exact tree에서 실패하는 `tests/cli/test_cli_status_bar.py::TestCLIStatusBar::test_compression_count_in_wide_fragments`를 수정하고, 직전 RED가 GREEN으로 바뀌는지 확인한다.
- [ ] 수정 후 `test_loadout_orchestrator_status.py`, `test_loadout_turn_status.py`, `test_cli_status_bar.py`, `test_tool_progress_scrollback.py`, `test_cli_yolo_toggle.py`, `test_process_registry.py`의 exact candidate suite를 fresh 실행하고 Coda 2단계 review를 받는다.
- Hugo/Clara 공통 다음 액션 1순위는 위 deterministic regression closure다. 그 다음 현재 unrelated dirty 파일들의 owner/scope를 확인하고 status fix와 섞지 않는다.
- 사용자가 요청할 때만 fork branch에서 PR 생성 또는 merge 범위를 별도로 확인한다. 자동 PR/merge/deploy 금지.
- 선택 사항: active skill 문서의 launcher/slash 구분 변경을 versioned canonical skill source에도 반영하려면 해당 source repository lineage를 먼저 확인한다. 현재 active skill 경로 자체는 Git 저장소가 아니다.
- 이 파일을 포함하는 docs-only handoff commit은 frontmatter에 자기 SHA를 재귀 기록하지 않는다. `git log -1 -- handover.md`로 확인한다.

## 6. 주의사항·함정

- slash 없는 `hermes-loadout` 터미널 런처와 `/hermes-loadout <task>` slash 오케스트레이터는 다른 계층이다. 일반 TUI 상태 누락을 `scripts/orchestrate.py status`만 수정해서 해결하려 하지 않는다.
- 이미 떠 있던 Hermes TUI는 Python source를 hot reload하지 않는다. 변경 확인은 기존 사용자 pane을 종료하지 말고 새 terminal/PTTY에서 한다.
- footer에는 9개 legend가 동시에 나타나는 것이 아니라 현재 상태 하나가 자동 표시된다.
- `0% CPU`, sleep, 모델 대기만으로 `STALLED` 또는 `DISCONNECTED`를 판정하지 않는다.
- 정상 agent thread 종료와 result finalization 사이의 짧은 구간을 `DISCONNECTED`로 추측하지 않는다. disconnect는 finalization에서 result 부재를 확인한 뒤 명시적으로 기록한다.
- terminal 상태는 다음 turn `start()` 전까지 latch해야 한다. interrupt worker가 늦게 callback해도 `STOPPED`를 덮으면 안 된다.
- 현재 worktree는 clean하지 않다. 2026-08-20 13:45 기준 task 외 tracked 변경 29개와 여러 untracked backup/handover 파일이 남아 있다. broad `git add -A`, `git reset --hard`, `git clean`, stash, checkout 덮어쓰기를 하지 않는다.
- successor commit `c7b550aa7a`와 remote는 동기화됐지만, dirty worktree는 의도적으로 보존됐다. `git status`의 dirty 목록을 push 실패나 feature 미커밋으로 오해하지 않는다.
- active `hermes-loadout` skill 문서 변경은 `/Users/392yes/.hermes/skills/devops/hermes-loadout/SKILL.md`에 있고 Git commit `42a32c6874`에는 없다.
- 모든 테스트용 PTY/Codex review process는 종료됐다. `42a32c6874` 범위의 authoritative verdict는 Coda `APPROVED (0/0/0)`와 tree `77857a5e...`의 95-test PASS다. 더 최신 `c7b550aa7a`의 authoritative current result는 exact tree `180 passed, 1 failed`이며 independent review는 pending이다. 두 snapshot을 합쳐 green으로 표현하지 않는다.
