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

## 상태

현재: architecture/context bootstrap.
