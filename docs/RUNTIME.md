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

`TRADINGBOT_MODE=fixture`가 기본값이다. 모든 종목의 자료가 명시적 합성 fixture로 생성된다. 실제 투자 결과와 섞지 않으며, fixture outbox 항목은 live notifier에서도 전송하지 않는다. SQLite 개발 DB는 첫 실행에 생성한다. `init-db`의 `create_all`과 운영용 Alembic migration은 다른 초기화 경로이므로 이미 생성한 fixture DB에 초기 migration을 적용하지 않는다.

## 3. Live 설정

`.env.example`을 `.env`로 복사하고 다음을 채운다.

| 설정 | 용도 |
|---|---|
| `TRADINGBOT_MODE=live` | 실제 공급자 사용 |
| `TRADINGBOT_OPENAI_API_KEY` | 모델 호출 |
| `TRADINGBOT_RESEARCH_MODEL`, `TRADINGBOT_PM_MODEL` | Responses API tool calling·strict structured output·이미지 입력을 지원하는 모델명 |
| `TRADINGBOT_TOSS_CLIENT_ID`, `TRADINGBOT_TOSS_CLIENT_SECRET` | Toss 공식 Open API 읽기 |
| `TRADINGBOT_TOSS_ACCOUNT_SEQ` | 필요한 경우 계좌 지정 |
| `TRADINGBOT_BRAVE_API_KEY` | Brave Search API |
| `TRADINGBOT_SEC_USER_AGENT` | 서비스명과 실제 연락처를 포함한 SEC User-Agent |
| `TRADINGBOT_BROWSER_TOKEN` | 내부 browser worker 인증용 별도 난수 |
| `TRADINGBOT_POSTGRES_PASSWORD` | 운영 DB 암호 |
| `TRADINGBOT_DISCORD_WEBHOOK` | 선택 사항. 비워 두면 전송하지 않음 |

`doctor`는 필수 설정의 존재만 검사하며 실제 API 연결이나 권한을 확인하지 않는다. 모델명과 가격을 코드에 고정하지 않으며 실제 token usage를 run trace에 보관한다. 달러 비용을 임의로 추정하지 않는다.

Toss는 동일 client의 새 토큰 발급이 이전 토큰을 무효화하므로, 운영은 **research worker 한 개**가 인증과 account/quote 조회를 소유한다. 같은 credentials로 기존 봇이나 별도 CLI live 프로세스를 동시에 실행하지 않는다. API 프로세스는 작업을 큐에 넣으며 Toss·모델 키를 받지 않는다.

## 4. Oracle Compose 배포

Docker Engine과 Compose가 동작하는 Oracle Linux VM에서 실행한다.

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
docker compose logs --tail 100 research-worker browser-worker
```

`postgres → migrate → research-api/research-worker` 순서로 시작한다. PostgreSQL 18의 `/var/lib/postgresql`과 `.research`를 볼륨에 저장한다. API는 호스트 `127.0.0.1:8000`에만 열며 SSH tunnel 등으로 접근한다. 별도 인증 없이 공개 포트로 노출하지 않는다. 모든 서비스는 JSON 로그를 10 MB × 3개로 회전한다.

browser worker는 별도의 non-root 컨테이너다. Toss·OpenAI·Brave·DB 자격 증명, 호스트 디렉터리, Docker socket을 전달하지 않는다. read-only filesystem, 제한된 tmpfs, 메모리/CPU/PID 제한, seccomp와 Chromium sandbox를 사용한다. 인터넷은 Squid proxy로 나가며 사설망·loopback·메타데이터 주소와 80/443 외 포트를 막는다. 직접 문서 reader도 이 proxy를 사용한다. Chromium sandbox가 시작되지 않으면 VM의 user namespace/seccomp/AppArmor 설정을 조사하고 sandbox를 끄는 방식으로 우회하지 않는다.

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

CLI의 `research TICKER --request request.json --enqueue`도 같은 작업을 생성한다. 수량 제안에는 요청의 `risk` 객체가 필요하다. `portfolio_value`, `currency`, `max_loss_fraction`, `max_position_fraction`, `slippage_fraction`, `tax_fraction`은 사용자가 확인한 입력이다. 실제 NAV나 허용 손실을 추측하지 않는다. 현재 검증기는 같은 통화의 long 거래와 정수 주식 수량을 지원한다. FX 변환, 누락된 sector/correlation 정보에 의존하는 수량은 거절한다.

작업은 `PENDING → RUNNING → COMPLETED/FAILED`로 진행한다. report·후보 상태·outbox는 하나의 트랜잭션으로 확정한다. 완료된 run을 다시 조회해도 모델을 재호출하지 않는다. 재시작은 완료된 단계의 checkpoint와 원래 예산을 이어서 사용한다. 중단된 모델 호출 자체는 재실행될 수 있으며 동일 호출 비용을 정확히 한 번으로 보장하지 않는다. 영구 실패한 run은 새 요청으로 재조사한다.

## 6. 조사 예산과 근거 한계

| 모드 | 도구 호출 | 경과 시간 | 누적 사용 token |
|---|---:|---:|---:|
| quick | 10 | 5분 | 120,000 |
| normal | 30 | 15분 | 300,000 |
| deep | 80 | 45분 | 800,000 |
| critical | 150 | 90분 | 1,200,000 |

예산은 목표가 아니라 상한이다. model 호출도 `도구 상한 + 24`회로 제한하고 응답별 출력 token 상한은 기본 16,000이다. 누적 token과 시간은 호출 경계에서 확인하므로 진행 중인 단일 호출만큼 초과할 수 있다. 부족하거나 충돌한 핵심 근거와 예산 소진은 `판단 보류`로 끝낸다. 숫자나 잘못된 stop/target을 자동으로 고치지 않는다.

질적 문장의 의미가 출처와 논리적으로 일치하는지까지 결정론적으로 증명하지는 않는다. FACT의 참조 존재, 검색 snippet 금지, 숫자의 fact/value/unit 일치, 계산·시각·통화를 검증한다. 문서 숫자 추출은 정확한 인용문과 수치·scale·통화 표기를 확인하며 라벨·회계 기간의 의미는 조사와 검토 단계가 책임진다. 정성 free text는 숫자를 쓰지 않고 수치는 structured reference에 넣는 계약이다.

SEC는 초기 표준 US-GAAP tag 집합을 수집한다. IFRS/custom tag·누락된 지표·이미지 PDF는 완전하게 지원하지 않는다. 이 경우 자료 부족을 표시하며 핵심 재무 fact가 없는 payload를 충분한 재무 근거로 인정하지 않는다. 실시간 시세는 timestamp가 필수이고 당일 진행 중인 daily candle은 기술 계산에서 제외한다. 장외의 유효 계획은 WATCH에 남으며 ENTRY는 정규장과 확인된 진입 조건을 요구한다.

## 7. 보존과 replay

근거의 원문 hash, source, published/effective/retrieved time, parser version, 숫자 단위와 기간을 저장한다. 기본적으로 SEC 등 설정된 원천의 문서만 본문을 보존한다. 다른 문서는 조사 중에 읽고 DB에는 metadata·hash·최대 1,200자 snippet·짧은 수치 인용을 저장한다. PDF 페이지 번호를 유지하고 별도 download 원본은 브라우저 sandbox 종료 시 지운다.

`TRADINGBOT_RETAINED_DOCUMENT_HOSTS`는 JSON 배열이다. 계약/약관상 허용된 호스트만 추가한다. screenshot은 모델에 전달하고 hash를 기록하지만 파일 보존은 기본적으로 꺼져 있다. 사용 허용 범위를 확인한 후 `TRADINGBOT_RETAIN_BROWSER_SCREENSHOTS=true`로 켤 수 있다.

`as_of`는 이미 저장된 과거 snapshot만 사용한다. 해당 시점 이후 조회된 근거를 가져오지 않고 네트워크를 호출하지 않으며 live 후보 상태·알림을 변경하지 않는다. 과거 snapshot이 없으면 판단 보류다. 이 기능은 데이터 공급자의 point-in-time backfill을 대체하지 않는다.

## 8. 감시·알림·평가

성공한 live 연구 종목은 자동으로 감시한다. 조건 확인은 60초, 계좌는 300초, 정기 재조사는 24시간 간격이다. 신규 entry/stop/target 도달, 보유 수량 변화, 데이터 조회 실패가 재조사를 요청한다. free-text 조건은 미확인 요건이며 가격만으로 만족한 것으로 간주하지 않는다. 가격 범위에 들어오면 추가 요건이 남아 있어도 `ENTRY_REVIEW_REQUIRED`로 재조사를 요청하고, 모델은 근거로 요건 충족을 확인한 뒤에만 조건을 해제한다.

기본 `report_policy=changes`는 등급·논지·후보·자료 품질·새 근거 변화만 알린다. 같은 근거의 표현 변경은 새 자료 알림에서 제외한다. 주기적으로 동일한 관찰 목록을 보내지 않는다. `always`와 `none`은 report 알림 정책이고 별도 감시 이벤트는 유지한다. Discord에는 mention을 허용하지 않는다. report 알림은 24시간, 즉시 조건 알림은 15분 이후 만료시켜 복구 후 오래된 신호를 보내지 않는다. outbox는 재시도하며 webhook 응답 유실 시 중복 전송 가능성이 있다.

평가기는 benchmark와 공통인 완료·수정 daily session을 맞춰 1/5/20/60일 수익률과 benchmark 상대 수익률, high/low MFE·MAE를 기록한다. 기준은 보고서의 뉴욕 날짜 이후 첫 공통 종가이며 16시 이후 보고서는 다음 날짜부터 시작한다. MFE·MAE는 기준 종가 이후 session만 사용한다. 이는 실제 체결가나 조기 폐장 calendar가 반영된 체결 simulation이 아니다. 실제 체결을 수집하지 않으므로 realized R은 `null`이다. 등급 전환, 무효화, 조건 hit, stale/source failure는 보고서·trace·outbox에 기록한다. alert precision/false alert의 판정과 aggregate 통계는 아직 별도 평가 데이터가 필요하다.

자동 후보 발굴은 `TRADINGBOT_DISCOVERY_ENABLED=false`가 기본이다. 켜면 공식 종목 universe를 하루 한 번 batch만큼 순환한다. 현재가가 원하는 진입 범위 밖이라는 이유로 종목을 버리지 않는다. live 비용과 작업량을 확인한 뒤 batch를 늘린다.

## 9. 백업과 복구

Oracle VM에서 정기적으로 DB dump와 research volume을 별도 저장소에 복사한다. 예시에서는 파일명을 고정했으므로 기존 파일을 덮어쓰지 않도록 날짜별 이름을 사용한다.

```sh
docker compose exec -T postgres pg_dump -U research -d research -Fc -f /tmp/research-backup.dump
docker compose cp postgres:/tmp/research-backup.dump ./research-backup.dump
```

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
