# Phase 1 — Shared Active Ledger + Claude Partial Streaming

## 진행 현황 (2026-06-14 업데이트)
| Step | 내용 | 상태 | 커밋 |
|---|---|---|---|
| A | ledger 모듈 (JSONL + 파일락 + trim) | ✅ 완료 | `cc3bf2870` |
| B | write 배선 (native `turn_finalizer` / bridge `_write_bridge_ledger`) | ✅ 완료 | `be4f3b9e4` |
| C | read/주입 (native `plugin_user_context` / bridge `build_continuity_context`) | ✅ 완료 | `cd7dc3cfc` |
| D | resident partial streaming (`--include-partial-messages` + `stream_callback`) | ↩️ 되돌림 — 사용자 판단상 체감지연완화 무의미 | `98abe992d` → revert `7401dad40` |
| E | sync path + gateway/Slack partial streaming | ❌ 취소 (D 폐기로 불필요) | — |

- **단위검증**: ledger/resident/bridge/turn_context 57 테스트 통과. stream-json 이벤트 구조는 실 CLI(2.1.177)로 확인.
- **라이브 게이트 (미완)**: 실 gateway/CLU 재기동 후 hermes-codex 1턴→hermes-claude 1턴으로 실파일 ledger 기록/주입 + partial 중간 노출 + Opus 4.8 유지 확인 필요. 재기동은 사용자 승인 후.
- **이월 사유 (E)**: sync는 `communicate()` 버퍼링 → 라인스트리밍 전환 회귀 위험. gateway는 `_stream_consumer`가 별도 스코프라 클로저 배선 복잡. 둘 다 `stream_callback=None` 기본값이라 현행 무회귀.

---

- 상태: ~~설계 (구현 전)~~ → **A~D 구현 완료, E 이월**
- 작성: 2026-06-14, Clara (clara-lead)
- 베이스라인 커밋: `f060cac6a` (clara_cli.max_turns 반영). `strict_mcp` 변경은 워킹트리에 무수정 이월.
- 제약: 모델 변경 금지(Opus 4.8 유지). push/PR/deploy 금지. 기존 열린 pane kill 금지. gateway restart는 사전 통보.

> 본 문서의 모든 `file:line`은 Explore 조사 시점 기준 후보 지점이다. **각 구현 스텝 착수 직전 해당 라인을 다시 확인**하고 어긋나면 보정한다(코드가 움직였을 수 있음).

---

## 0. 목표 (사용자 요구 그대로)

1. **Shared active ledger** — hermes-codex와 hermes-claude가 같은 "현재 작업 회의록"을 공유한다.
   - turn 종료 시 그 turn의 요약을 ledger에 기록(writer)
   - turn 시작 시 *상대 runtime*의 최근 요약을 주입(reader)
2. **Claude partial streaming** — Claude Opus가 느려도 partial text가 중간에 보여서 체감 지연을 줄인다.
   - Claude Code `stream-json` partial text → Hermes CLI `stream_callback` 연결

두 기능은 독립적이라 **별도 스텝으로 구현·검증**한다. 한 번에 merge하지 않는다.

---

## 1. 현황 요약 (조사 결과)

### 1-A. Ledger 측
| 구성요소 | 상태 | 위치(후보) |
|---|---|---|
| turn 종료 훅 | 재활용 | `agent/turn_finalizer.py` `finalize_turn()` — `_persist_session` 직후 |
| turn 시작 주입 훅 | 재활용 | `agent/turn_context.py` 시스템프롬프트 빌드 직전 |
| 공유 상태 저장소 | 재활용 | `hermes_state.py` `SessionDB` + `~/.hermes/state.db` (WAL, 멀티프로세스 동시접근 설계됨) |
| ephemeral 주입 메커니즘 | 재활용 | `agent.ephemeral_system_prompt` (캐시 시스템프롬프트를 깨지 않음 — **중요**) |
| 요약 생성기 | 부분 재활용 | `agent/context_compressor.py:_generate_summary()` (전체 turn 리스트 기준; turn 단건용 래퍼 필요) |
| ledger writer/reader | **신규** | `agent/team_active_ledger.py` |

핵심: 시스템프롬프트는 세션당 캐시되어 prefix-cache 안정성을 위해 mid-session 재빌드하지 않는다. 따라서 상대 요약 주입은 **반드시 `ephemeral_system_prompt`** 경로로 한다(캐시 시스템프롬프트에 넣으면 매 turn 캐시가 깨져 오히려 느려진다).

### 1-B. Partial streaming 측
| 경로 | 상태 | 병목 |
|---|---|---|
| Hermes 내부 콜백 (`on_delta(text)`, `stream_consumer/dispatch/events`) | **이미 완성** | 일반 LLM은 이미 partial 노출 |
| resident 경로 (`claude_resident.py`) | partial 없음 | `_collect_until_result()`가 `result` 타입만 대기, 중간 `content_block_delta` 무시 |
| sync 경로 (`claude_code_bridge.py:_run_claude_subprocess`) | partial 없음 | `proc.communicate()`로 stdout 일괄 버퍼링 |
| bridge 함수 시그니처 | 없음 | `run_claude_code_bridge_sync/resident`에 `stream_callback` 파라미터 부재 |
| 호출부 (`cli.py`, `gateway/run.py`) | 없음 | bridge 호출 시 콜백 미전달 (일반 agent 경로는 이미 전달 중) |

핵심: **수신부(Hermes on_delta)는 준비 완료**. 손볼 곳은 Claude bridge의 두 경로(resident 우선)와 호출부 배선뿐. resident가 기본(`resident_enabled: true`)이므로 **resident 경로를 먼저** 한다. sync 경로의 `communicate()→라인스트리밍` 전환은 회귀 위험이 커서 후순위 분리.

---

## 2. 설계 결정 (확인 필요 2건은 §6)

### 2-1. Ledger 저장소 — **신규 경량 JSONL 파일** 권장 (1안)
- 경로: `~/.hermes/runtime/active_ledger.jsonl` (append-only, 1줄=1 turn 요약)
- 레코드: `{ts, runtime, session_id, task_id, turn_id, summary, end_reason}`
- reader: 파일 tail에서 *상대 runtime*의 최근 N(기본 1~3)건만 읽음
- 동시성: append 단건 write(O_APPEND, 짧은 락 또는 fcntl), reader는 읽기 전용 → 충돌 거의 없음
- 장점: 사람이 직접 `tail -f`로 회의록을 관찰 가능, 구현 단순, rollback = 파일 삭제, state.db 스키마(현 v16) 마이그레이션 불필요
- 단점: state.db만큼의 트랜잭션 보장은 없음(요약 1줄이라 사실상 무관)

**대안(2안) — state.db 신규 테이블** `active_ledger(session_id, turn_id, runtime, summary, created_at)`
- 장점: 기존 WAL 동시성·컨벤션 일치
- 단점: `SCHEMA_VERSION` 마이그레이션 필요(현 16), rollback 복잡, 관찰성 낮음

> 권장: **1안(JSONL)**. "현재 작업 회의록"이라는 성격·관찰성·rollback 단순성이 ledger 용도에 더 맞음. → §6에서 확인.

### 2-2. turn 요약 방식 — **경량 휴리스틱** 우선 권장
- 매 turn LLM 호출 요약은 추가 지연·토큰 비용 → Phase 1 목표(지연 감소)와 상충
- 1차: 휴리스틱 = `마지막 assistant 응답 앞부분 + 호출한 주요 tool 목록 + 변경 파일(있으면)`, 길이 캡(예: 600자)
- 2차(옵션): 필요 시 `context_compressor._generate_summary()` 단건 래퍼로 LLM 요약 승격
> 권장: **휴리스틱 우선**. → §6에서 확인.

### 2-3. 모듈 경계
- `agent/team_active_ledger.py` 신규: `write_turn(...)`, `read_peer_recent(runtime, limit)` 두 함수 + 레코드 dataclass. conversation 레이어와 동일 위치(`agent/`).
- runtime 식별자: bridge/agent가 자신이 codex인지 claude인지 아는 기존 값 재사용(구현 시 확인 — 예: config의 provider 또는 clara/bridge 플래그). 식별 불가 시 명시적 설정값 추가.

---

## 3. 구현 스텝 (작은 단위, 각 스텝 후 검증)

### Step A — Ledger 모듈 (순수 신규, 위험 최저)
1. `agent/team_active_ledger.py` 작성: writer/reader + 레코드. 파일 경로·포맷 §2-1.
2. 단위 테스트: write→read 라운드트립, 상대 runtime 필터링, 동시 append 안전성(스레드 2개).
- 검증: `pytest tests/...active_ledger...` 신규 테스트 통과.

### Step B — Writer 배선 (turn 종료)
1. `turn_finalizer.py` `_persist_session` 직후 `team_active_ledger.write_turn(...)` 호출. 예외는 삼켜서(try/except) 본 흐름에 영향 없게.
2. 요약은 §2-2 휴리스틱.
- 검증: hermes-codex 한 턴 → ledger 파일에 codex 레코드 1줄. hermes-claude 한 턴 → claude 레코드 1줄.

### Step C — Reader/주입 배선 (turn 시작)
1. `turn_context.py` 시스템프롬프트 빌드 직전 `read_peer_recent(peer_runtime)` 호출 → 결과를 `agent.ephemeral_system_prompt` 앞에 `[상대 작업대 최근 요약] ...` 형태로 prepend.
2. 빈 ledger/예외 시 무주입(no-op).
- 검증: codex 턴 후 → claude 턴 시작 시 시스템 컨텍스트에 codex 요약 주입 확인(로그/디버그). 반대 방향도 확인. **Opus 4.8 유지 확인**(`--model` 미주입).

### Step D — Partial streaming: resident 경로 (우선)
1. `claude_code_bridge.py`: `run_claude_code_bridge_resident(...)`에 `stream_callback: Optional[Callable[[str],None]]=None` 추가, `pool.run_turn(...)`에 전달.
2. `claude_resident.py`: `run_turn`/`_collect_until_result`에 콜백 전달. **먼저 스파이크**: 실제 `stream-json` 이벤트를 캡처해 partial text delta의 정확한 타입/구조 확정(추정: `content_block_delta`/`text_delta`). 확정된 타입에서만 text 추출→`stream_callback(text)`. `result` 최종 처리 로직은 그대로 유지.
3. 호출부: `cli.py` bridge 호출과 `gateway/run.py` bridge 호출에 `stream_callback` 전달(일반 agent 경로의 `_stream_delta_cb` 재사용).
- 검증: hermes-claude 한 턴에서 partial text가 **final-only가 아니라 중간에** 노출되는지 실 CLI에서 확인. 최종 결과 정합성·tool 동작 회귀 없음 확인.

### Step E — Partial streaming: sync 경로 (후순위, 분리 커밋)
- `_run_claude_subprocess`의 `communicate()`를 라인 단위 reader로 전환. 회귀 위험 높음 → resident 검증 안정 후 별도 착수. resident가 기본이라 미적용해도 주 경로엔 영향 없음.

---

## 4. 검증 매트릭스 (Phase 1 종료 게이트)
| 항목 | 증거 |
|---|---|
| ledger write (codex) | 파일에 codex 레코드 |
| ledger write (claude) | 파일에 claude 레코드 |
| ledger 주입 (codex→claude) | claude 턴 컨텍스트에 codex 요약 |
| ledger 주입 (claude→codex) | codex 턴 컨텍스트에 claude 요약 |
| partial 노출 (claude) | 중간 텍스트 실시간 노출(final-only 아님) |
| Opus 4.8 유지 | `--model` 미주입 / 응답 모델 확인 |
| 회귀 없음 | 기존 turn/세션/tool 정상, 단위테스트 green |
| rollback 가능 | JSONL 삭제 + Step별 커밋 revert |

## 5. Rollback
- Step별 독립 커밋. ledger는 파일 삭제로 무력화. partial은 `stream_callback=None`이면 기존 동작.
- gateway restart 필요 시 사전 통보.

## 6. 사용자 확인 필요 (2건)
1. **Ledger 저장소**: 1안 JSONL 파일(`~/.hermes/runtime/active_ledger.jsonl`, 권장) vs 2안 state.db 신규 테이블?
2. **요약 방식**: 휴리스틱(권장, 추가 지연 0) vs LLM 요약(`_generate_summary` 래퍼)?

확인되면 Step A부터 작은 단위로 구현→검증→보고로 진행한다.
