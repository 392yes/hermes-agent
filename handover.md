---
type: session-handover
canonical: true
project: hermes-agent
session_end: 2026-06-17 16:19
git_branch: remove-t2-role-board-cleanup
git_commit: d55b0c606
---

# Session Handover — hermes-agent (2026-06-17 16:19)

## 1. 현재 상태 (다음 세션 시작점)
Hermes CLI 하단 status bar에 token-tracker 기반 daily usage 표시를 붙이는 작업을 완료했다. 현재 구현은 `hermes-codex` 실행 시 모델명 바로 옆에 Codex daily 사용률 바를, `hermes-claude` 실행 시 Claude Code daily 사용률 바를 표시한다. 다음 세션은 실행 중인 `hermes-codex` / `hermes-claude` 세션을 재시작해 실제 TUI에서 표시가 보이는지 육안 확인하면 된다.

## 2. 가장 최근 작업 (100% 보존)
요청: “대화창의 매번 마지막에 오는 gpt-5.5 옆에다가 가로바를 하나 만들고, 그 가로바에 daily limit 사용 퍼센트를 표시. 사용량이 늘수록 바가 채워지고, 바 옆에는 숫자+% 표시. hermes-codex는 Codex 사용량, hermes-claude는 Claude 사용량.”

수정 파일:
- `/Users/392yes/.hermes/hermes-agent/cli.py`
- `/Users/392yes/.hermes/hermes-agent/tests/cli/test_cli_status_bar.py`

구현 세부사항:
- `token-tracker`가 uv tool로 설치된 Python 경로를 사용한다:
  - `/Users/392yes/.local/share/uv/tools/token-tracker/bin/python`
- Hermes status bar 렌더가 자주 호출되므로 token-tracker 스캔은 직접 렌더 경로에서 블로킹하지 않고, 백그라운드 refresh + 60초 캐시로 처리했다.
- daily limit 기준은 token-tracker 내부의 `aggregate_daily()` + `calculate_p90()` 결과를 사용한다.
- `HERMES_LEAD_MODE`로 표시 대상을 구분한다:
  - `HERMES_LEAD_MODE=hugo-lead` → Codex daily usage
  - `HERMES_LEAD_MODE=clara-lead` → Claude Code daily usage
- 기존 “Codex day 2%” / “CC day 64%” 텍스트형 표시를 제거하고, 모델명 바로 뒤에 바 형태로 표시하도록 바꿨다.
- 바 렌더링:
  - 10칸: `[██████░░░░] 64%`
  - 0%보다 크지만 10칸 rounding 상 0칸이 되는 낮은 사용률도 최소 1칸 표시하도록 처리했다.
  - 예: `2%` → `[█░░░░░░░░░] 2%`
- wide/medium/narrow text fallback와 prompt_toolkit fragment 경로 모두 모델명 옆에 같은 daily usage 바가 붙도록 처리했다.

실제 검증 명령과 결과:

```bash
./venv/bin/python -m py_compile cli.py tests/cli/test_cli_status_bar.py
```
- 결과: 통과, 출력 없음

```bash
./venv/bin/python -m pytest tests/cli/test_cli_status_bar.py -q -o 'addopts='
```
- 결과:
```text
..............................................                           [100%]
46 passed in 0.96s
```

실제 렌더 문자열 확인:

```bash
HERMES_LEAD_MODE=hugo-lead ./venv/bin/python - <<'PY'
from datetime import datetime
from types import SimpleNamespace
import cli
cli._refresh_agent_daily_usage_status('codex')
c=cli.HermesCLI.__new__(cli.HermesCLI)
c.model='gpt-5.5'; c.session_start=datetime.now(); c.conversation_history=[]; c.agent=SimpleNamespace(model='gpt-5.5',session_input_tokens=0,session_output_tokens=0,session_cache_read_tokens=0,session_cache_write_tokens=0,session_prompt_tokens=0,session_completion_tokens=0,session_total_tokens=0,session_api_calls=0,context_compressor=SimpleNamespace(last_prompt_tokens=0,context_length=200000,compression_count=0)); c._prompt_start_time=None; c._prompt_duration=0; c._last_turn_finished_at=None; c._background_tasks={};
print(c._build_status_bar_text(width=160))
PY
```
- 결과:
```text
⚕ gpt-5.5 [█░░░░░░░░░] 2% │ 0/200K │ 0% │ 0s │ ⏲ 0s
```

```bash
HERMES_LEAD_MODE=clara-lead ./venv/bin/python - <<'PY'
from datetime import datetime
from types import SimpleNamespace
import cli
cli._refresh_agent_daily_usage_status('claude-code')
c=cli.HermesCLI.__new__(cli.HermesCLI)
c.model='gpt-5.5'; c.session_start=datetime.now(); c.conversation_history=[]; c.config={}; c.agent=SimpleNamespace(model='gpt-5.5',session_input_tokens=0,session_output_tokens=0,session_cache_read_tokens=0,session_cache_write_tokens=0,session_prompt_tokens=0,session_completion_tokens=0,session_total_tokens=0,session_api_calls=0,context_compressor=SimpleNamespace(last_prompt_tokens=0,context_length=200000,compression_count=0)); c._prompt_start_time=None; c._prompt_duration=0; c._last_turn_finished_at=None; c._background_tasks={};
print(c._build_status_bar_text(width=160))
PY
```
- 결과:
```text
⚕ opus-4.8 [██████░░░░] 64% │ 0/200K │ 0% │ 0s │ ⏲ 0s
```

현재 관련 diff stat:
```text
 cli.py                           | 283 ++++++++++++++++++++++++++-------------
 tests/cli/test_cli_status_bar.py |  49 +++++++
 2 files changed, 241 insertions(+), 91 deletions(-)
```

## 3. 이전 작업 (내림차순 압축)

### token-tracker 설치 및 초기 Hermes status bar 연동 검토
- 사용자가 “token-tracker가 괜찮을거 같네”라고 판단해 설치/검증을 진행했다.
- 설치 명령:
```bash
~/.local/bin/uv tool install token-tracker --python 3.11 --force && ~/.local/bin/tt --version && ~/.local/bin/tt setup
```
- 결과:
```text
tt 0.3.8
✓ Claude Code statusLine configured
Restart Claude Code to take effect
Codex status_line already configured, skipping
```
- 실제 usage 확인:
```bash
~/.local/bin/tt daily
~/.local/bin/tt codex
~/.local/bin/tt claude
```
- 당시 집계 예시:
  - 전체: Token 약 699.5M, Cost 약 $881, Sessions 387, Messages 4952
  - Claude Code: Token 약 687.3M, Cost 약 $866, Sessions 368, Messages 4689
  - Codex: Token 약 12.2M, Cost 약 $14.90, Sessions 19, Messages 263
- 주의: 처음 `uvx --from token-tracker tt --help`가 첫 실행 setup을 자동 수행해 Claude/Codex 설정을 건드렸다. 이후 안정적인 uv tool 경로로 재설치하고 `tt setup`을 다시 실행해 Claude statusLine 경로를 고정했다.

### GitHub usage tracker 후보 조사
- 사용자가 GitHub에서 Codex/Claude Code usage 체크 프로그램을 찾아달라고 요청했다.
- web_search backend가 `Firecrawl search failed: 'NoneType' object has no attribute 'status_code'`로 반복 실패해서 GitHub API/NPM metadata 조회로 대체했다.
- 확인한 주요 후보:
  - `ccusage/ccusage` — 약 16k stars, Claude Code/Codex/Hermes 등 지원
  - `tddworks/ClaudeBar` — macOS 메뉴바 quota app
  - `stormzhang/token-tracker` — Claude Code + Codex statusLine/dashboard
  - `juliantanx/aiusage` — local-first dashboard
  - `Nihondo/AgentLimits` — macOS menu bar/widgets
- 결론: 사용자는 token-tracker를 선택했다.

## 4. 사용자 결정사항·승인 내역 (무압축)
- 2026-06-17: 사용자는 Codex/Claude Code usage tracking 후보 중 `token-tracker`가 괜찮다고 결정했다.
- 2026-06-17: 사용자는 Hermes 내부 status bar에 daily 사용량을 표시하는 방향을 요청했다.
- 2026-06-17: 사용자는 최종적으로 “모델명(gpt-5.5/opus-4.8) 옆에 가로바 + 숫자 퍼센트” 형태를 요구했다.
- 2026-06-17: 삭제/배포/커밋/푸시 승인 없음. 이번 세션에서는 로컬 파일 수정과 테스트만 수행했다.

## 5. 미완료 작업 / 다음 액션
- [ ] 실행 중인 `hermes-codex` / `hermes-claude` CLI 세션을 재시작해 실제 TUI 하단 status bar에서 가로바가 보이는지 확인한다.
- [ ] 필요하면 bar width(현재 10칸), 색상 threshold, 낮은 퍼센트 최소 1칸 표시 정책을 사용자 취향에 맞게 조정한다.
- [ ] 이번 변경을 커밋할지 여부는 사용자 승인 후 결정한다. 현재 커밋/푸시 없음.
- [ ] 기존에 남아 있던 다른 uncommitted 변경(`gateway/claude_code_bridge.py`, `tests/cli/test_cli_terminal_response_sanitizer.py`, 백업 파일 등)과 이번 status bar 변경을 섞어 커밋하지 않도록 분리 검토한다.

## 6. 주의사항·함정
- 현재 브랜치: `remove-t2-role-board-cleanup`, HEAD: `d55b0c606`.
- 현재 repo 전체 `git status --short`:
```text
 M cli.py
 M gateway/claude_code_bridge.py
 M handover.md
 M tests/cli/test_cli_status_bar.py
 M tests/cli/test_cli_terminal_response_sanitizer.py
?? gateway/claude_code_bridge.py.bak-20260614
```
- 이번 요청 범위로 직접 수정한 핵심 파일은 `cli.py`, `tests/cli/test_cli_status_bar.py`, 그리고 이 handoff를 위해 덮어쓴 `handover.md`다. 다른 변경들은 이전 세션/다른 작업의 잔여 변경이므로 무심코 섞어 커밋하면 안 된다.
- `handover.md`는 이 스킬 규칙에 따라 canonical으로 덮어쓴다. 덮어쓰기 자체가 의도된 동작이다.
- token-tracker daily percentage는 실제 provider quota API가 아니라 로컬 로그 기반 P90 daily token 기준이다. 사용자가 “daily limit”이라고 부르는 값은 현재 구현상 token-tracker의 P90 daily token limit이다.
- 첫 렌더 직후에는 백그라운드 refresh가 아직 끝나지 않아 usage bar가 잠깐 비어 있을 수 있다. 60초 캐시/비동기 갱신 구조다.
- Hugo/Clara 공통 다음 시작점은 이 `handover.md`와 Obsidian 사본이다. Claude Code native `--resume` 또는 Hermes pane-local resume은 보조 수단이다. 다음 세션에서는 repo root에서 `/session-resume`을 실행해 이 파일을 읽는 것이 기준이다.
