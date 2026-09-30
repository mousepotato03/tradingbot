# Runtime and operations

## 1. 구현 범위

이 버전은 리서치·추천·후보 관리·감시·Discord 알림을 수행한다. AI가 조사와 투자 판단을 하고, 코드는 출처·수치·통화·시각·리스크를 검증한다. `Buy / Overweight / Hold / Underweight / Sell / 판단 보류` 외의 등급은 스키마에서 거절한다.

조회 어댑터에는 주문 엔드포인트가 없다. 실제 계좌 조회에 실패했다고 빈 보유 목록을 만들지 않는다. 매수 가능 금액을 계좌 순자산으로 사용하지 않는다.

## 2. 로컬 fixture

```powershell
uv sync --frozen
uv run tradingbot research TEST --output .research/demo.md
uv run tradingbot status <run-id>
uv run tradingbot report <run-id>
uv run tradingbot discover --batch 1
uv run tradingbot worker
```

`TRADINGBOT_MODE=fixture`가 기본값이다. 모든 종목의 자료가 명시적 합성 fixture로 생성된다. 실제 투자 결과와 섞지 않으며, fixture outbox 항목은 live notifier에서도 전송하지 않는다. SQLite 개발 DB는 첫 실행에 생성한다. `init-db`의 `create_all`과 운영용 Alembic migration은 다른 초기화 경로이므로 이미 생성한 fixture DB에 초기 migration을 적용하지 않는다. `create_all`은 기존 테이블을 바꾸지 않으므로, 0002 이전에 만든 fixture DB에는 초기화 시 `outcomes.mature` 컬럼만 추가한다. fixture 종목 `FUND`는 합성 ETF 경로를 실행한다.

## 3. Live 설정

`.env.example`을 `.env`로 복사하고 다음을 채운다.

| 설정 | 용도 |
|---|---|
| `TRADINGBOT_MODE=live` | 실제 공급자 사용 |
| `TRADINGBOT_OPENAI_API_KEY` | 모델 호출 |
| `TRADINGBOT_RESEARCH_MODEL`, `TRADINGBOT_PM_MODEL` | Responses API tool calling·strict structured output·이미지 입력을 지원하는 모델명 |
| `TRADINGBOT_TRIAGE_MODEL` | 선택 사항. 발굴 triage용 저비용 모델. 비우면 research 모델 사용 |
| `TRADINGBOT_RESEARCH_REASONING_EFFORT`, `TRADINGBOT_PM_REASONING_EFFORT`, `TRADINGBOT_TRIAGE_REASONING_EFFORT` | 선택 사항. 역할별 `reasoning.effort`. 비우면 provider 기본값. 지원 값은 모델마다 다름 |
| `TRADINGBOT_BROWSER_POST_ALLOWED_HOSTS` | 선택 사항. POST 검색 폼이 꼭 필요한 정확한 hostname 목록(쉼표 구분). 기본은 비어 있어 browser가 읽기 전용 |
| `TRADINGBOT_TOSS_CLIENT_ID`, `TRADINGBOT_TOSS_CLIENT_SECRET` | Toss 공식 Open API 읽기 |
| `TRADINGBOT_TOSS_ACCOUNT_SEQ` | 필요한 경우 계좌 지정 |
| `TRADINGBOT_SEARCH_PROVIDER` | `tavily`(기본) 또는 `brave` |
| `TRADINGBOT_TAVILY_API_KEY` | Tavily Search API. 무료 플랜은 월 1,000 크레딧, 카드 불필요, 한도 초과 시 차단 |
| `TRADINGBOT_BRAVE_API_KEY` | Brave Search API를 선택한 경우만 필요. 카드 등록, 월 크레딧 초과 시 자동 청구 |
| `TRADINGBOT_SEARCH_MONTHLY_LIMIT` | 최근 30일 live 검색 횟수 상한. 기본 900. 비우면 상한 없음 |
| `TRADINGBOT_RESEARCH_SCHEDULE`, `TRADINGBOT_MARKET_OPEN_OFFSET_MINUTES` | 정기 재조사 방식. 기본 `market_open`: 거래일 정규장 개장 10분 후 1회. `interval`: 아래 간격 |
| `TRADINGBOT_RESEARCH_INTERVAL_HOURS` | `interval` 방식의 재조사 간격(기본 24시간). 감시 종목 수와 함께 모델 비용을 좌우 |
| `TRADINGBOT_HOLDINGS_AUTO_WATCH`, `TRADINGBOT_HOLDING_RESEARCH_MODE` | 계좌의 USD 보유 종목 자동 감시 여부와 그 조사 모드. 기본 `true`, `quick` |
| `TRADINGBOT_SEC_USER_AGENT` | 서비스명과 실제 연락처를 포함한 SEC User-Agent |
| `TRADINGBOT_BROWSER_TOKEN` | 내부 browser worker 인증용 별도 난수 |
| `TRADINGBOT_POSTGRES_PASSWORD` | 운영 DB 암호 |
| `TRADINGBOT_DISCORD_WEBHOOK` | 선택 사항. 비워 두면 전송하지 않음 |

`doctor`는 필수 설정의 존재만 검사하며 실제 API 연결이나 권한을 확인하지 않는다. 모델명과 가격을 코드에 고정하지 않으며 실제 token usage를 run trace에 보관한다. 달러 비용을 임의로 추정하지 않는다.

Toss는 동일 client의 새 토큰 발급이 이전 토큰을 무효화하므로, 운영은 **research worker 한 개**가 인증과 account/quote 조회를 소유한다. 같은 credentials로 기존 봇이나 별도 CLI live 프로세스를 동시에 실행하지 않는다. 실행 중 토큰이 다른 곳에서 무효화되어 `HTTP_401`이 나면 한 번 재발급해 재시도하지만, 두 프로세스가 번갈아 토큰을 받으면 계속 끊긴다. API 프로세스는 작업을 큐에 넣으며 Toss·모델 키를 받지 않는다.

## 4. Oracle Compose 배포

Docker Engine과 Compose가 동작하는 Oracle Linux VM에서 실행한다.

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
docker compose logs --tail 100 research-worker browser-worker
```

`postgres → migrate → research-api/research-worker` 순서로 시작한다. 기본 구성은 postgres, migrate(1회 실행), research-api, research-worker, egress-proxy다. Chromium browser worker는 문서를 직접 읽을 수 없을 때만 쓰는 선택 기능이며 메모리를 1.5GB까지 쓴다. 그래서 기본으로 띄우지 않고 모델에게 브라우저 도구도 주지 않는다. 쓰려면 `.env`에 `TRADINGBOT_BROWSER_ENABLED=true`를 넣고 `docker compose --profile browser up -d --build`로 띄운다. PostgreSQL 18의 `/var/lib/postgresql`과 `.research`를 볼륨에 저장한다. API는 호스트 `127.0.0.1:8000`에만 열며 SSH tunnel 등으로 접근한다. 별도 인증 없이 공개 포트로 노출하지 않는다. 모든 서비스는 JSON 로그를 10 MB × 3개로 회전한다.

browser worker는 별도의 non-root 컨테이너다. Toss·OpenAI·검색·DB 자격 증명, 호스트 디렉터리, Docker socket을 전달하지 않는다. read-only filesystem, 제한된 tmpfs, 메모리/CPU/PID 제한, seccomp와 Chromium sandbox를 사용한다. 인터넷은 Squid proxy로 나가며 사설망·loopback·메타데이터 주소와 80/443 외 포트를 막는다. 직접 문서 reader도 이 proxy를 사용한다. Chromium sandbox가 시작되지 않으면 VM의 user namespace/seccomp/AppArmor 설정을 조사하고 sandbox를 끄는 방식으로 우회하지 않는다.

서비스는 heartbeat/HTTP/DB health check를 제공한다. Docker의 `unhealthy` 표시는 자동 복구 자체를 보장하지 않으므로 VM의 서비스 감시로도 상태를 확인한다. 기본 구성은 worker·browser 각각 하나다. 여러 worker로 확장하려면 토큰 소유, browser session 용량, lease 복구 정책을 먼저 변경해야 한다.

## 5. 조사 요청과 결과

```sh
curl -X POST http://127.0.0.1:8000/research-runs \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"AAPL","mode":"deep","question":"공식 실적과 반대 근거를 조사해 신규 진입과 기존 보유자의 행동을 구분해 판단"}'
```

반환된 `run_id`로 다음을 조회한다.

- `GET /research-runs/{run_id}`: 진행 상태·checkpoint·실패 코드
- `GET /research-runs/{run_id}/report`: typed JSON 보고서
- `GET /research-runs/{run_id}/markdown`: 한국어 보고서
- `GET /research-runs/{run_id}/evidence`: 출처·해시·단위·시각·파싱 메타데이터

CLI의 `research TICKER --request request.json --enqueue`도 같은 작업을 생성한다. 수량 제안에는 요청의 `risk` 객체가 필요하다. `portfolio_value`, `currency`, `max_loss_fraction`, `max_position_fraction`, `slippage_fraction`, `tax_fraction`은 사용자가 확인한 입력이다. 실제 NAV나 허용 손실을 추측하지 않는다. 현재 검증기는 같은 통화의 long 거래를 지원한다. 수량 단위는 거래안의 `sizing_unit`으로 정한다. `whole_share`(기본)는 정수 주식이다. `fractional_amount`는 Toss의 금액 기반 미국 시장가 매수로, 0.000001주 단위로 내림하며 체결가가 확정되지 않는다. FX 변환, 누락된 sector/correlation 정보에 의존하는 수량은 거절한다.

최종 결정은 등급·신규 진입 행동·보유자 행동·논지 상태·거래안이 [결정 계약](INVESTMENT_POLICY.md#decision-contract)과 일치해야 한다. 예를 들어 `Sell`과 추가 매수, `Buy`와 무효화된 논지, `Sell`과 long 진입 계획은 숫자가 맞아도 거절한다. `판단 보류`가 아닌 모든 등급은 근거가 있는 논지 claim이 필요하다.

`watch TICKER --request request.json`으로 감시를 등록하면 그 요청의 투자 조건이 이후 재조사에 계승된다.

작업은 `PENDING → RUNNING → COMPLETED/FAILED`로 진행한다. report·후보 상태·outbox는 하나의 트랜잭션으로 확정한다. 완료된 run을 다시 조회해도 모델을 재호출하지 않는다. 재시작은 완료된 단계의 checkpoint와 원래 예산을 이어서 사용한다. 중단된 모델 호출 자체는 재실행될 수 있으며 동일 호출 비용을 정확히 한 번으로 보장하지 않는다. 영구 실패한 run은 새 요청으로 재조사한다.

## 6. 조사 예산과 근거 한계

| 모드 | 모델 요청 도구 호출 | 경과 시간 | 비용 기준 token |
|---|---:|---:|---:|
| quick | 10 | 15분 | 450,000 |
| normal | 30 | 30분 | 900,000 |
| deep | 80 | 60분 | 1,800,000 |
| critical | 150 | 90분 | 3,000,000 |

예산은 목표가 아니라 상한이다. 모든 모드가 11단계(director, 토론 4, research manager, 리스크 3, premortem, PM)를 거치고 reasoning 모델 호출은 한 번에 15~30초가 걸리므로, 시간과 token 상한은 그만큼을 전제로 한다.

- **도구 한도:** 모델이 요청한 호출만 센다. 기본 수집(식별·시세·일봉과 기술지표·계좌·수수료·공시·재무 또는 ETF 보유종목·시가총액 계산)과 PM 직전 시세 재조회는 따로 기록하며 한도에 넣지 않는다. 도구 한도에 닿으면 이후 단계는 도구 없이 이미 모은 근거로 결론을 쓴다.
- **판단 보류로 끝나는 한도:** 경과 시간, 비용 기준 token, model 호출 수(`도구 한도 + 24`)다.
- **비용 기준 token:** 캐시된 입력을 10%로 계산한다. 응답별 출력 token 상한은 기본 16,000이다. 누적 token과 시간은 호출 경계에서 확인하므로 진행 중인 단일 호출만큼 초과할 수 있다.

부족하거나 충돌한 핵심 근거는 `판단 보류`로 끝낸다. 숫자나 잘못된 stop/target을 자동으로 고치지 않는다.

**주장 검증 실패:** 모델에게 구체적인 오류(어떤 인용·fact가 왜 틀렸는지, 그 fact가 실제로 있는 근거 ID)를 알려 주고 한 번 수정할 기회를 준다. 그래도 틀린 주장은 그 단계에서 제외하고, `claim_removal` trace와 비차단 gap으로 남긴다. 주장 하나 때문에 조사 전체를 버리지 않는다.

**근거 ID:** 짧고 종류가 드러나게 만든다. 예) `qt-…` 시세, `ta-…` 기술지표, `fin-…` SEC 재무, `pf-…` 계좌. 모델이 긴 UUID를 잘못 옮기거나 시세 근거에 기술지표 fact를 다는 오류를 줄이기 위해서다.

**구조화 근거의 인용:** 시세·계좌·공시 목록처럼 본문이 없는 근거는 context에 보이는 JSON 형태(`"filed": "2026-08-26"`) 그대로 인용할 수 있다. 검증기는 키 순서나 따옴표가 아니라 기록된 필드 값과 정확히 같은지를 본다. 본문의 숫자·날짜가 모두 검증된 인용 안에 있으면 별도 숫자 참조 없이도 허용한다. 계좌 근거에는 `buying_power`와 종목별 `{ticker}.quantity`/`.average_price`/`.market_value` fact가 있고, 기술지표는 소수 4자리로 기록하며 52주 고가·저가를 포함한다.

**`extract_fact`:** 원문에 적힌 숫자와 단위(`scale`)를 받는다. 저장되는 값은 숫자 × 단위다. 표 행을 인용할 때는 표 머리의 "In millions" 같은 단위 표기를 인용문 앞 6,000자 안에서 찾아 확인하고, 어디서 찾았는지 기록한다. 인용문 비교는 공백을 정규화한다.

모델 context에는 근거 원문 대신 **evidence index**가 들어간다. 각 근거마다 ID·출처·시각·freshness, 최대 40개 fact, 300자 snippet, 요약 payload만 넣는다. PDF 페이지, 봉 데이터, 브라우저 요소 목록은 넣지 않는다. 도구 결과는 index 항목과 4,000자 미리보기로 돌려주고, 한 단계 대화에서 오래된 도구 결과는 evidence ID로 축약하며 screenshot은 최신 1장만 남긴다. 세부 내용은 `evidence_read`로 필요할 때 읽는다. `evidence_read`는 읽기 전용 view이므로 새 근거를 만들지 않는다.

`material_gaps`에는 `blocking`/`non_blocking` 심각도가 있다. 판단을 막는 gap은 research manager(blocking gap과 `evidence_sufficient=false`)와 PM만 확정한다. bull·bear·리스크·premortem 단계의 gap은 PM에게 open gap으로 전달되며, 한 단계가 단독으로 `판단 보류`를 강제하지 않는다. `판단 보류`는 blocking gap을 하나 이상 명시해야 한다.

질적 문장의 의미가 출처와 논리적으로 일치하는지까지 결정론적으로 증명하지는 않는다. FACT의 참조 존재, 검색 snippet 금지, 숫자의 fact/value/unit 일치, 계산·시각·통화를 검증한다. 숫자가 아닌 FACT는 인용한 근거 본문의 정확한 span(공백 정규화, 12자 이상 또는 structured 필드 값 전체)을 `quotes`로 제시해야 한다. 검증된 span은 `quotation` 근거로 저장한다. 문서 숫자 추출은 정확한 인용문과 수치·scale·통화 표기를 확인하며 라벨·회계 기간의 의미는 조사와 검토 단계가 책임진다. 정성 free text는 숫자를 쓰지 않고 수치는 structured reference에 넣는 계약이다.

기술 지표·계산·추출값처럼 다른 근거에서 만든 근거는 입력 중 가장 빨리 만료되는 근거의 freshness를 상속한다. daily OHLCV는 마지막 완료 세션 종가 시각을 기준으로 5일 후 만료된다. 정밀 가격 수준은 그 근거와 기록된 모든 입력이 결정 시점에 fresh해야 한다(`LEVEL_STALE`).

SEC는 US-GAAP과 IFRS(`ifrs-full`, 20-F/40-F/6-K) 표준 tag를 수집하고, IFRS 수치는 보고 통화(TWD 등)를 그대로 유지한다. fact 이름에 taxonomy가 들어간다(`revenue:us-gaap:Revenues:…`, 이전 형식은 `revenue:Revenues:…`). USD·주식 수가 아닌 단위는 이름 끝에 단위를 붙인다. 과거 보고서는 재검증하지 않으므로 영향이 없지만, fact 이름을 외부에서 파싱하는 도구는 새 형식을 따라야 한다. 통화가 다른 수치의 계산은 FX 근거 없이 거절한다. 회사별 custom tag와 이미지 PDF는 지원하지 않는다. 이 경우 자료 부족을 표시하며 핵심 재무 fact가 없는 payload를 충분한 재무 근거로 인정하지 않는다. 예탁증서(DR)는 ADS 비율 확인이 필요하다는 경고를 붙이고, 자동 발굴에서는 기본적으로 제외한다.

ETF(레버리지·인버스 제외)는 별도 경로로 조사한다. SEC fund ticker mapping에서 series를 찾아 최신 N-PORT 보유종목·순자산·자산/국가 비중과 요약 투자설명서(497K) 링크를 수집하며, 발행사 재무제표를 요구하지 않는다. mapping에 없는 SPY 같은 UIT는 아직 자동 보유종목 근거가 없어 `판단 보류`가 된다. 실시간 시세는 timestamp가 필수이고 당일 진행 중인 daily candle은 기술 계산에서 제외한다. 장외의 유효 계획은 WATCH에 남으며 ENTRY는 정규장과 확인된 진입 조건을 요구한다.

## 7. 보존과 replay

근거의 원문 hash, source, published/effective/retrieved time, parser version, 숫자 단위와 기간을 저장한다. 기본적으로 SEC 등 설정된 원천의 문서만 본문을 보존한다. 다른 문서는 조사 중에 읽고 DB에는 metadata·hash·최대 1,200자 snippet·짧은 수치 인용을 저장한다. PDF 페이지 번호를 유지하고 별도 download 원본은 브라우저 sandbox 종료 시 지운다.

`TRADINGBOT_RETAINED_DOCUMENT_HOSTS`는 JSON 배열이다. 계약/약관상 허용된 호스트만 추가한다. screenshot은 모델에 전달하고 hash를 기록하지만 파일 보존은 기본적으로 꺼져 있다. 사용 허용 범위를 확인한 후 `TRADINGBOT_RETAIN_BROWSER_SCREENSHOTS=true`로 켤 수 있다.

이전 버전의 `evidence_read`는 발췌문을 새 근거로 저장해 비승인 호스트 본문이 최대 24,000자까지 보존될 수 있었다. 이제 `evidence_read`는 저장하지 않는 view다. 재시작으로 재개된 run은 비승인 호스트 문서의 snippet만 갖고 있다. 그러나 이미 검증된 인용은 `quotation` 근거로 남아 있으므로 재검증할 수 있다.

`as_of`는 이미 저장된 과거 snapshot만 사용한다. 해당 시점 이후 조회된 근거를 가져오지 않고 네트워크를 호출하지 않으며 live 후보 상태·알림을 변경하지 않는다. 과거 snapshot이 없으면 판단 보류다. 이 기능은 데이터 공급자의 point-in-time backfill을 대체하지 않는다.

## 8. 감시·알림·평가

성공한 live 연구 종목과 Toss 계좌의 USD 보유 종목은 자동으로 감시한다. 새로 발견한 보유 종목은 `기존 보유` 상태로 `TRADINGBOT_HOLDING_RESEARCH_MODE`(기본 quick) 첫 조사를 요청한다. 정규장 중이면 바로, 아니면 다음 정기 조사 시점에 돈다. 감시 종목은 거래일마다 재조사되므로 종목 수와 모드가 모델·검색 비용을 결정한다. 재조사의 투자자 상태는 계좌 기준으로 맞춘다. 보유 중이면 `기존 보유`, 전량 매도했으면 `신규 진입 검토`가 된다. 단, 요청이 `일부 매도 검토`였으면 그대로 둔다. 조건 확인은 60초, 계좌는 300초 간격이다. 정기 재조사는 Toss 미국 장 운영 캘린더 기준으로 **거래일 정규장 개장 10분 후**(`TRADINGBOT_MARKET_OPEN_OFFSET_MINUTES`) 한 번 돌고, 주말과 휴장일은 건너뛴다. 예) 서머타임 중 한국 시간 22:40, 해제 후 23:40. 개장 전으로 당기지 않은 이유가 있다. 정밀 가격 수준은 90초 이내의 새 시세로만 검증하므로, 개장 전 조사는 대부분 `판단 보류`가 되고 손절선도 받지 못한다. 보유 종목이 여러 개면 worker가 순서대로 처리하므로 개장 후 1시간 안팎에 결과가 모인다. `TRADINGBOT_RESEARCH_SCHEDULE=interval`이면 이전처럼 `TRADINGBOT_RESEARCH_INTERVAL_HOURS` 간격으로 돈다. 가격·수량 이벤트에 의한 재조사는 일정과 무관하게 즉시 요청한다. 한 번의 확인에서 모든 대상 시세를 `/prices`로 일괄 조회하며(호출당 200종목, 장 운영 calendar 1회), 계좌는 모든 종목이 공유하는 snapshot 하나를 300초마다 조회한다. 같은 process 안에서는 계좌 조회 결과를 60초 동안 재사용한다. Toss 호출은 rate-limit 그룹(`MARKET_DATA` 15/s, `MARKET_DATA_CHART` 20/s, `ACCOUNT` 1/s 등)별 token bucket으로 조절한다. 초기값은 공식 문서 수치이고 이후 `X-RateLimit-*` 응답 헤더로 갱신한다. 실시간 WebSocket은 사용하지 않는다. REST 일괄 조회로 60초 주기 감시가 충분하기 때문이다.

감시 중 조회한 시세·계좌·발굴 스크리닝은 `monitor_observations`에 저장하며, 완료된 보고서의 근거 원장은 변경하지 않는다(완료된 run에 근거를 추가하면 오류). 거래안이 있는 종목은 60초마다 시세 관측값이 쌓인다(종목당 하루 최대 약 1,440행, 장 운영 calendar 포함). 이전 버전이 보고서 근거 테이블에 쌓던 양과 같다. 아직 자동 정리 작업이 없으므로 DB 크기를 주기적으로 확인한다. 진입 범위·손절·목표 도달은 **정규장 시세로만** 판정한다. 프리·애프터·데이마켓의 얇은 거래 가격으로는 알리지 않고, 장외 급변은 다음 정규장 시작 직후 판정한다. 장이 닫혀 있을 때 시세가 오래된 것은 조회 실패로 보지 않는다. 이미 알린 가격 신호는 장이 닫혔다 열려도 같은 이탈이 계속되는 한 다시 보내지 않는다.

**보유 종목 손절 감시(`position_guard`):** 계좌에 있는 종목이면 PM이 근거 있는 손절선과 (선택) 익절 검토 가격을 낸다. 손절선은 현재가 아래, 익절가는 위여야 한다. 청산 권고(`EXIT`)가 아니면 필수이고, `판단 보류`와 함께 낼 수도 있다. 코드가 근거·신선도·위치를 검증한 뒤, 모니터가 1분마다 정규장 가격으로 확인한다. 손절선 이하면 `HOLDER_STOP` 알림과 critical 재조사, 익절가 이상이면 `HOLDER_TAKE_PROFIT_n` 알림을 보낸다. 손절선이 처음 생기거나 없어지면 보고 알림에 포함되고, 모든 보고 알림에 현재 손절선이 함께 표시된다.

진입 범위·손절·목표 도달, 보유 손절·익절 도달, 해당 종목의 보유 수량 변화, 데이터 조회 실패가 재조사를 요청한다. 계좌 조회 실패는 보유 중(`ACTIVE_POSITION`)인 종목에만 알린다. 재조사는 감시를 만든 요청의 위험 입력·기간·투자자 상태·질문·모드를 계승하고, 무효화 가격 도달 시 `critical`로 올리며, 이전 보고서 이후 무엇이 바뀌었는지를 `trigger`로 전달한다. free-text 조건은 미확인 요건이며 가격만으로 만족한 것으로 간주하지 않는다. 가격 범위에 들어오면 추가 요건이 남아 있어도 `ENTRY_REVIEW_REQUIRED`로 재조사를 요청하고, 모델은 근거로 요건 충족을 확인한 뒤에만 조건을 해제한다.

기본 `report_policy=changes`는 등급·논지·후보·자료 품질·새 근거 변화만 알린다. 같은 근거의 표현 변경은 새 자료 알림에서 제외한다. 주기적으로 동일한 관찰 목록을 보내지 않는다. `always`와 `none`은 report 알림 정책이고 별도 감시 이벤트는 유지한다. Discord에는 mention을 허용하지 않는다. report 알림은 24시간, 즉시 조건 알림은 15분 이후 만료시켜 복구 후 오래된 신호를 보내지 않는다. outbox는 재시도하며 webhook 응답 유실 시 중복 전송 가능성이 있다.

평가기는 benchmark와 공통인 완료·수정 daily session을 맞춰 1/5/20/60일 수익률과 benchmark 상대 수익률, high/low MFE·MAE를 기록한다. 기준은 보고서의 뉴욕 날짜 이후 첫 공통 종가이며 16시 이후 보고서는 다음 날짜부터 시작한다. MFE·MAE는 기준 종가 이후 session만 사용한다. 이는 실제 체결가나 조기 폐장 calendar가 반영된 체결 simulation이 아니다. 실제 체결을 수집하지 않으므로 realized R은 `null`이다. 등급 전환, 무효화, 조건 hit, stale/source failure는 보고서·trace·outbox에 기록한다. alert precision/false alert의 판정과 aggregate 통계는 아직 별도 평가 데이터가 필요하다.

평가기는 60개 session이 모두 관측된 보고서와 `판단 보류` 보고서를 `mature`로 표시하고 다시 조회하지 않는다. 필요한 종목의 OHLCV만 가져온다.

자동 후보 발굴은 `TRADINGBOT_DISCOVERY_ENABLED=false`가 기본이다. 켜면 하루 한 번 별도 maintenance thread에서 세 단계로 실행한다.

1. 결정론적 스크리닝: 1개월 거래대금 순위 상위 종목부터 최대 `discovery_screen_limit`개를 본다. 가격·20일 거래대금·시가총액·자료 충분성은 통과 조건이고, 추세·상대강도는 순위에만 반영한다.
2. 저비용 triage: 모델 호출 1회로 `deep_research`/`watch_later`/`skip`을 정한다.
3. 심층 조사: `deep_research`만 batch 한도까지 deep run으로 넣는다.

현재가가 원하는 진입 범위 밖이라는 이유로 종목을 버리지 않는다. 스크리닝 실패와 비심층 triage 결과는 `discovery_rescreen_days`(30일) 동안 기억한다. 예탁증서와 ETF는 `discovery_security_types` 기본값에서 제외한다. live 비용과 작업량을 확인한 뒤 batch와 한도를 늘린다.

## 9. 백업과 복구

Oracle VM에서 정기적으로 DB dump와 research volume을 별도 저장소에 복사한다. 예시에서는 파일명을 고정했으므로 기존 파일을 덮어쓰지 않도록 날짜별 이름을 사용한다.

```sh
docker compose exec -T postgres pg_dump -U research -d research -Fc -f /tmp/research-backup.dump
docker compose cp postgres:/tmp/research-backup.dump ./research-backup.dump
```

migration `0002`는 `monitor_observations` 테이블과 `outcomes.mature` 컬럼을, `0003`은 검색 사용 기록 `search_usage` 테이블을 추가한다. 기존 보고서 JSON은 다시 쓰지 않는다. 자유 서술 행동은 `DEFER`와 "(이전 형식 자유 서술)" 표시가 붙은 note로 읽고, 옛 PM gap은 blocking, 단계별 gap은 non_blocking으로 읽는다.

`.env`의 비밀값은 Git이나 browser volume에 보관하지 않는다. backup에는 계좌 snapshot과 report가 포함되므로 접근을 제한한다. 복구는 worker와 API를 중지하고 별도 새 DB에 dump를 복원한 후 `alembic upgrade head`를 수행한다. 단일 worker만 시작해 남은 job을 재개한다. 운영 DB에 `init-db`, legacy schema import, Alembic 초기 revision을 강제로 stamp하는 방법을 사용하지 않는다.

## 10. 검증

```powershell
uv run playwright install chromium
uv run pytest -q
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv build
```

단위 테스트는 실제 broker/web/model에 연결하지 않는다. Chromium 통합 테스트도 로컬 HTTP fixture로만 수행한다. PostgreSQL 검증은 **비어 있는 전용 테스트 DB**의 `TEST_DATABASE_URL`을 설정한 후 실행한다. 테스트는 DB에 migration과 합성 report를 남긴다.

CI는 PostgreSQL 18 통합 테스트, Chromium, wheel build, 세 Dockerfile build를 수행하도록 구성했다. 로컬 Docker daemon·실제 API 키·Oracle 환경이 없으면 그 검증을 수행했다고 주장하지 않는다. 배포 후 실제 quote timestamp·완료 봉·계좌 완전성·SEC 수집·모델 계약·브라우저 sandbox·Discord webhook·재시작 복구를 작은 범위에서 확인해야 한다.
