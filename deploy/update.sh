#!/usr/bin/env bash
# VM에서 실행: 최신 코드를 받아 이미지를 다시 빌드하고 프로세스를 재기동한다.
#   ./deploy/update.sh              기본 서비스 갱신
#   ./deploy/update.sh --browser    Chromium browser worker 프로필 포함
#   ./deploy/update.sh --logs 200   반영 후 워커 로그 줄 수 (기본 60)
set -euo pipefail

profile=()
logs=60
while [ $# -gt 0 ]; do
  case "$1" in
    --browser) profile=(--profile browser) ;;
    --logs) logs="${2:?--logs 뒤에 줄 수가 필요합니다}"; shift ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
  esac
  shift
done

cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain)" ]; then
  echo "VM 작업 트리에 커밋되지 않은 변경이 있습니다. 확인 후 다시 실행하세요." >&2
  git status --short >&2
  exit 1
fi

echo "==> git pull"
git pull --ff-only

echo "==> docker compose up -d --build"
docker compose config --quiet
docker compose "${profile[@]}" up -d --build --remove-orphans

echo "==> 상태"
docker compose ps

echo "==> research-worker 로그 (최근 ${logs}줄)"
docker compose logs --tail "$logs" research-worker
