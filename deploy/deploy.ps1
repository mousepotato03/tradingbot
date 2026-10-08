<#
.SYNOPSIS
  로컬 테스트 -> push -> Oracle VM에서 pull/빌드/재기동 -> 상태 확인을 한 번에 실행한다.

.EXAMPLE
  .\deploy\deploy.ps1                 # 테스트, push, VM 반영
  .\deploy\deploy.ps1 -SkipTests      # 테스트 생략
  .\deploy\deploy.ps1 -Browser        # Chromium browser worker 프로필 포함
  .\deploy\deploy.ps1 -Logs 200       # 반영 후 로그 200줄

VM 접속 정보는 인자 또는 환경변수로 지정한다 (비밀값은 저장소에 두지 않는다).
  TRADINGBOT_DEPLOY_HOST   예: 140.238.x.x 또는 ~/.ssh/config의 Host 별칭
  TRADINGBOT_DEPLOY_USER   예: opc            (기본 opc)
  TRADINGBOT_DEPLOY_KEY    예: C:\Users\me\.ssh\oracle.key (생략 시 ssh 기본 키)
  TRADINGBOT_DEPLOY_PATH   VM의 저장소 경로 (기본 ~/tradingbot)
#>
[CmdletBinding()]
param(
    [string]$HostName = $env:TRADINGBOT_DEPLOY_HOST,
    [string]$User = $(if ($env:TRADINGBOT_DEPLOY_USER) { $env:TRADINGBOT_DEPLOY_USER } else { "opc" }),
    [string]$KeyPath = $env:TRADINGBOT_DEPLOY_KEY,
    [string]$RemotePath = $(if ($env:TRADINGBOT_DEPLOY_PATH) { $env:TRADINGBOT_DEPLOY_PATH } else { "~/tradingbot" }),
    [string]$Branch = "main",
    [int]$Logs = 60,
    [switch]$SkipTests,
    [switch]$Browser
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Run($exe, $arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "$exe 실패 (exit $LASTEXITCODE)" }
}

if (-not $HostName) { throw "VM 주소가 없습니다. -HostName 또는 TRADINGBOT_DEPLOY_HOST를 지정하세요." }

if (git status --porcelain) { throw "커밋되지 않은 변경이 있습니다. 먼저 커밋하세요." }
if ((git rev-parse --abbrev-ref HEAD) -ne $Branch) { throw "현재 브랜치가 $Branch 가 아닙니다." }

if (-not $SkipTests) {
    Step "단위 테스트 / ruff"
    Run ".\.venv\Scripts\python.exe" @("-m", "pytest", "tests/unit", "-q")
    Run ".\.venv\Scripts\python.exe" @("-m", "ruff", "check", "src", "tests")
}

Step "git push origin $Branch"
Run "git" @("push", "origin", $Branch)

$composeProfile = if ($Browser) { "--profile browser " } else { "" }
$remote = @"
set -euo pipefail
cd $RemotePath
git fetch origin $Branch
git merge --ff-only origin/$Branch
docker compose ${profile}up -d --build --remove-orphans
docker compose ps
docker compose logs --tail $Logs research-worker
"@ -replace "`r", ""

$sshArgs = @("-o", "StrictHostKeyChecking=accept-new")
if ($KeyPath) { $sshArgs += @("-i", $KeyPath) }
$sshArgs += @("$User@$HostName", "bash -s")

Step "VM 반영: $User@$HostName`:$RemotePath"
$remote | & ssh @sshArgs
if ($LASTEXITCODE -ne 0) { throw "VM 배포 실패 (exit $LASTEXITCODE)" }

Step "완료"
