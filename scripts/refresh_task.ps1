# Scheduled refresh of the NCAAF games that are not yet final in SQLite.
#
# Registered in Windows Task Scheduler as "ncaaf-clv refresh" (Monday and Tuesday 07:00 local time);
# see README "Run" step 5. Runs `uv run ncaaf-clv refresh --db data/ncaaf_clv.db` from the repo root
# and appends stdout/stderr to data/logs/refresh-<yyyyMM>.log. Exit code is the CLI's:
# 0 ok, 1 some game summaries failed to fetch, 2 ESPN blocked the run (database unchanged; re-run later).

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("refresh-{0}.log" -f (Get-Date -Format "yyyyMM"))

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    $uv = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe"
}

"===== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz') refresh start ($uv) =====" | Add-Content $log
& $uv run ncaaf-clv refresh --db data/ncaaf_clv.db 2>&1 | ForEach-Object { "$_" } | Add-Content $log
$code = $LASTEXITCODE
"===== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz') refresh end (exit $code) =====" | Add-Content $log
exit $code
