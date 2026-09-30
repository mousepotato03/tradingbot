# tradingbot

Evidence-based autonomous investment research system.

이 저장소는 기존 TradingAgents/monitor 구조를 리팩터링하는 프로젝트가 아니라, **웹 ChatGPT 수준의 조사 자유도 + 감사 가능한 증거 원장 + 결정론적 리스크 검증**을 목표로 새로 구축하는 프로젝트다.

## 핵심 흐름

```
Data / Web / Filings / Browser
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

예상 구성:

- research/orchestrator
- structured-data adapters
- isolated browser worker (Chromium + Playwright)
- scheduler
- database
- Discord notifier

v1은 자동 주문이 아니라 **리서치·추천·모니터링·알림**까지를 범위로 한다.

## 실행

Python 3.13 이상과 [uv](https://docs.astral.sh/uv/)를 사용한다.

```powershell
uv sync --frozen
uv run tradingbot research TEST --output .research/demo.md
uv run tradingbot doctor
```

기본 `fixture` 모드는 합성 자료와 scripted model로 전체 파이프라인을 실행한다. 키 없이 동작하며 실제 종목 분석이나 Discord 전송을 하지 않는다. 결과는 `.research/`에 저장한다.

실제 조사에는 `.env.example`을 `.env`로 복사하고 Toss·OpenAI·Brave 키, 모델명, SEC 연락처를 설정한다. Oracle 배포, 요청 API, 리스크 입력, 백업 방법은 [운영 가이드](docs/RUNTIME.md)에 정리했다.

```powershell
uv run playwright install chromium
uv run pytest -q
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
```

## 구현 상태

v0.1은 Toss 읽기 전용 계좌·시세·봉·수수료, SEC 공시·재무, Brave 검색, HTML/PDF reader, 격리된 Chromium worker, OpenAI tool calling, 강세/약세 논쟁·리스크 위원회·PM, 증거/수치 검증, 후보 감시·변화 알림·성과 기록을 구현했다. SQLite fixture 실행과 PostgreSQL/Alembic 기반 Compose 배포 구성을 제공한다.

외부 API는 mock으로 검증하고 실제 Chromium은 로컬 페이지로 검증한다. PostgreSQL 통합 테스트는 별도 테스트 DB에서 실행하며 CI에 포함했다. 실제 API 자격 증명과 Oracle VM을 이용한 운영 검증은 별도로 필요하다. 자동 주문 API는 구현 범위에 없다.
