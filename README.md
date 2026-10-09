# tradingbot

Evidence-based autonomous investment research system.

이 저장소는 기존 TradingAgents/monitor 구조를 리팩터링하는 프로젝트가 아니라, **웹 ChatGPT 수준의 조사 자유도 + 감사 가능한 증거 원장 + 결정론적 리스크 검증**을 목표로 새로 구축하는 프로젝트다.

## 핵심 흐름

```
  Data / Web / Filings
            ↓
      Evidence Ledger
            ↓
      Research Analyst
            ↓
        Bull ↔ Bear
            ↓
       Risk Committee
            ↓
     Portfolio Manager
            ↓
Buy / Overweight / Hold / Underweight / Sell / 판단 보류
            ↓
 Deterministic Risk Validator
            ↓
     State + Discord
```

AI는 투자 논지와 판단을 담당하고, 코드는 사실 검증·계산·리스크 제약을 담당한다.

## 먼저 읽을 문서

- [AGENTS.md](AGENTS.md)
- [Project Context](docs/PROJECT_CONTEXT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Research System](docs/RESEARCH_SYSTEM.md)
- [Investment Policy](docs/INVESTMENT_POLICY.md)
- [Legacy Reuse](docs/LEGACY_REUSE.md)
- [Implementation Plan](docs/IMPLEMENTATION_PLAN.md)
- [Stock Research Standard](docs/reference/stock_research_standard_ko.md)

## 배포 목표

Oracle Cloud 상시 실행 환경을 기본으로 한다.

VM에서 `.venv`의 worker 하나가 리서치·감시·알림을 모두 처리하고, DB는 SQLite다. `main`에 push하면 GitHub Actions가 검사 후 VM에서 `update.sh`를 실행해 배포한다.

v1은 자동 주문이 아니라 **리서치·추천·모니터링·알림**까지를 범위로 한다.

## 실행

Python 3.13 이상과 [uv](https://docs.astral.sh/uv/)를 사용한다.

```powershell
uv sync --frozen
uv run tradingbot research TEST --output .research/demo.md
uv run tradingbot doctor
```

기본 `fixture` 모드는 합성 자료와 scripted model로 전체 파이프라인을 실행한다. 키 없이 동작하며 실제 종목 분석이나 Discord 전송을 하지 않는다. 결과는 `.research/`에 저장한다.

실제 조사에는 `.env.example`을 `.env`로 복사하고 Toss·OpenAI·Tavily(기본 검색) 키, 모델명, SEC 연락처를 설정한다. Oracle 배포, 조사 요청, 리스크 입력, 백업 방법은 [운영 가이드](docs/RUNTIME.md)에 정리했다.

```powershell
uv run pytest -q
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
```

## 구현 상태

v0.1은 Toss 읽기 전용 계좌·시세·봉·수수료, SEC 공시·재무, Brave 검색, HTML/PDF reader, OpenAI tool calling, 강세/약세 논쟁·리스크 위원회·PM, 증거/수치 검증, 후보 감시·변화 알림·성과 기록을 구현했다. SQLite + Alembic으로 동작하며 GitHub Actions로 VM에 배포한다. 실제로 쓰지 않던 Docker/Compose, PostgreSQL, HTTP API, Chromium worker는 제거했다.

현재 흐름은 research manager 뒤에 구체적인 거래 초안과 수치 검증 결과를 만든 후 리스크 위원회·최종 PM을 거친다. 최종 위험 조건이 바뀌면 위원회가 다시 검토한다. 다른 종목의 파생 가격 근거, 인용문 뒤의 무관한 단위, 참조로 뒷받침되지 않은 추가 숫자, 과거 분석에 미래 보고서가 섞이는 경로도 검증한다. 변경 내용과 회귀 테스트는 [구현 계획](docs/IMPLEMENTATION_PLAN.md)에 정리했다.

v0.2에서는 코드 리뷰 결과를 반영했다(상세: [Implementation Plan](docs/IMPLEMENTATION_PLAN.md#v02-review-fixes)).
- 판단 구조: 등급·행동·논지·거래안 일관성 계약, gap 심각도, 인용 span 검증
- 감사 가능성: 감시 관측값 분리, 파생 근거의 freshness 계보
- 안전·규모: 시세 일괄 조회와 그룹별 rate limit
- 범위 확장: IFRS 재무, ETF(N-PORT) 조사 경로, 스크리닝→triage→심층 조사 발굴
- 운영: Tavily 기본 검색과 30일 검색 상한, Toss 보유 종목 자동 감시

외부 API는 mock으로 검증한다. 실제 API 자격 증명과 Oracle VM을 이용한 운영 검증은 별도로 필요하다. 자동 주문 API는 구현 범위에 없다.
