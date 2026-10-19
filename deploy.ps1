# 1회 배포 스크립트 — 실행:  powershell -ExecutionPolicy Bypass -File .\deploy.ps1
# 필요: git, gh(GitHub CLI) 로그인 상태(gh auth login)
$repo = "isaccjong-jpg/news-signal-engine"
$utf8 = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = $utf8              # 파이프로 gh에 넘기는 한글(판단원칙·유니버스)이 깨지지 않도록
[Console]::OutputEncoding = $utf8

function Fail($msg) { Write-Host "[중단] $msg" -ForegroundColor Red; exit 1 }

# 0) 사전 점검
foreach ($f in "private\config.local.json","private\prompt.txt","private\universe.json") {
  if (-not (Test-Path $f)) { Fail "$f 없음" }
}
$cfg = Get-Content private\config.local.json -Raw -Encoding UTF8
if ($cfg -match "여기에_") { Fail "private\config.local.json 에 API 키를 먼저 입력하십시오." }

# 1) 워크플로 위치
New-Item -ItemType Directory -Force .github\workflows | Out-Null
foreach ($f in "signal.yml","keepalive.yml") { if (Test-Path $f) { Move-Item $f .github\workflows\ -Force } }

# 2) git → 공개 저장소(이미 생성됨)
if (-not (Test-Path .git)) { git init | Out-Null }
git add -A
$staged = git status --porcelain
if ($staged -match "private/") { Fail "private/ 가 스테이징됨 — .gitignore 확인" }
git commit -m "news-signal-engine init" | Out-Null
git rev-parse --verify HEAD 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "commit 실패 — git config --global user.name / user.email 설정 후 재실행" }
git branch -M main
$remotes = git remote
if ($remotes -notcontains "origin") { git remote add origin "https://github.com/$repo.git" }
git push -u origin main
if ($LASTEXITCODE -ne 0) { Fail "push 실패 — gh auth status 확인" }

# 3) Secrets 3개 (PowerShell엔 '<' 입력 리다이렉션이 없으므로 파이프로 stdin 전달)
Get-Content private\config.local.json -Raw -Encoding UTF8 | gh secret set CONFIG_JSON -R $repo
if ($LASTEXITCODE -ne 0) { Fail "CONFIG_JSON 등록 실패" }
Get-Content private\prompt.txt -Raw -Encoding UTF8 | gh secret set PRIVATE_PROMPT -R $repo
if ($LASTEXITCODE -ne 0) { Fail "PRIVATE_PROMPT 등록 실패" }
Get-Content private\universe.json -Raw -Encoding UTF8 | gh secret set PRIVATE_UNIVERSE -R $repo
if ($LASTEXITCODE -ne 0) { Fail "PRIVATE_UNIVERSE 등록 실패" }
gh secret list -R $repo

# 4) 첫 실행 (push 직후 워크플로 인덱싱에 수 초 걸려 재시도)
$ok = $false
for ($i = 0; $i -lt 8 -and -not $ok; $i++) {
  Start-Sleep -Seconds 5
  gh workflow run signal.yml -R $repo -f mode=collect 2>$null
  if ($LASTEXITCODE -eq 0) { $ok = $true }
}
if ($ok) { Write-Host "실행 요청 완료. 1~2분 뒤 확인:  gh run list -R $repo --limit 3" -ForegroundColor Green }
else { Write-Host "수동 실행 요청 실패 — GitHub Actions 탭에서 Run workflow(collect)를 직접 누르십시오." -ForegroundColor Yellow }
Write-Host "판정 시트: https://docs.google.com/spreadsheets/d/1eauZiROVCoQurzoV9iBppw_VwbySoD9Y8cQj-gN803s"
