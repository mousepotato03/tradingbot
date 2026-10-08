#!/usr/bin/env bash
# VM에서 실행: 최신 코드를 받아 의존성·DB를 갱신하고 워커를 재시작한다.
#   ./update.sh              pull, 의존성 동기화, DB migration, 워커 재시작
#   ./update.sh --logs 100   재시작 후 보여줄 로그 줄 수 (기본 30)
set -euo pipefail

logs=30
while [ $# -gt 0 ]; do
  case "$1" in
    --logs) logs="${2:?--logs 뒤에 줄 수가 필요합니다}"; shift ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
  esac
  shift
done

cd "$(dirname "$0")"
log=.research/worker.log
mkdir -p .research

if [ -n "$(git status --porcelain)" ]; then
  echo "작업 트리에 커밋되지 않은 변경이 있습니다. 확인 후 다시 실행하세요." >&2
  git status --short >&2
  exit 1
fi

echo "==> git pull"
git pull --ff-only

echo "==> 의존성 동기화"
if command -v uv >/dev/null 2>&1; then
  uv sync --frozen
else
  echo "uv가 없어 건너뜁니다 (의존성이 바뀌었다면 수동으로 설치하세요)" >&2
fi

echo "==> DB migration"
.venv/bin/alembic upgrade head

echo "==> 기존 워커 종료"
if pgrep -f '/\.venv/bin/tradingbot worker' >/dev/null; then
  pkill -TERM -f '/\.venv/bin/tradingbot worker' || true
  for _ in $(seq 1 30); do
    pgrep -f '/\.venv/bin/tradingbot worker' >/dev/null || break
    sleep 1
  done
  if pgrep -f '/\.venv/bin/tradingbot worker' >/dev/null; then
    echo "30초 안에 종료되지 않아 강제 종료합니다" >&2
    pkill -KILL -f '/\.venv/bin/tradingbot worker' || true
    sleep 1
  fi
else
  echo "실행 중인 워커 없음"
fi

echo "==> 워커 시작"
nohup .venv/bin/tradingbot worker >>"$log" 2>&1 &
sleep 3
if ! pgrep -f '/\.venv/bin/tradingbot worker' >/dev/null; then
  echo "워커가 바로 종료됐습니다. 로그:" >&2
  tail -n "$logs" "$log" >&2
  exit 1
fi

echo "==> 실행 중: $(pgrep -f '/\.venv/bin/tradingbot worker' | tr '\n' ' ')"
echo "==> 로그 (${log} 마지막 ${logs}줄)"
tail -n "$logs" "$log"
