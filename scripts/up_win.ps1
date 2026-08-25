# Windows equivalent of scripts/up_bare.sh.
#
# Two Windows-only details this handles that up_bare.sh cannot:
#  1. Git Bash's `$!` returns an MSYS pid, not the Windows pid, so a pid file written
#     from bash points at nothing chaos/kill_region.py (real os.kill) can act on.
#  2. The venv launcher re-execs into the base interpreter, so the pid Start-Process
#     reports is the PARENT of the process that actually binds the port. Suspending
#     that parent does nothing. So we record the pid that OWNS THE LISTENING SOCKET.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force run, reports | Out-Null

$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
if (-not $env:WARMUP_SECONDS)   { $env:WARMUP_SECONDS = "6" }
if (-not $env:EDGE_TTL_SECONDS) { $env:EDGE_TTL_SECONDS = "5" }

function Start-Svc($name, $module, $port, $extraEnv) {
    foreach ($k in $extraEnv.Keys) { Set-Item -Path "env:$k" -Value $extraEnv[$k] }
    Start-Process -FilePath $py `
        -ArgumentList "-m","uvicorn",$module,"--host","127.0.0.1","--port",$port,"--log-level","warning" `
        -RedirectStandardOutput "run/$name.log" -RedirectStandardError "run/$name.err" `
        -NoNewWindow | Out-Null
}

Start-Svc "region-a" "serving.app:app" 8001 @{ REGION="a"; STATE_DIR="state/region-a" }
Start-Svc "region-b" "serving.app:app" 8002 @{ REGION="b"; STATE_DIR="state/region-b" }
Start-Svc "edge"     "edge.proxy:app"  8080 @{ REGION="a" }

# A live pid does not mean uvicorn bound the port yet -- verify over HTTP, then take
# the pid straight from the listening socket.
Write-Host "cho service len (toi da 15s)..."
$ok = $true
foreach ($np in @(@("region-a",8001,"healthz"), @("region-b",8002,"healthz"), @("edge",8080,"edge/state"))) {
    $name, $port, $path = $np
    $up = $false
    foreach ($i in 1..15) {
        try {
            Invoke-WebRequest -Uri "http://127.0.0.1:$port/$path" -TimeoutSec 2 -UseBasicParsing | Out-Null
            $up = $true; break
        } catch { Start-Sleep -Seconds 1 }
    }
    if ($up) {
        $owner = (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop).OwningProcess | Select-Object -First 1
        Set-Content -Path "run/$name.pid" -Value $owner -Encoding ascii
        Write-Host "  $name (port $port): UP pid=$owner"
    } else {
        Write-Host "  $name (port $port): KHONG PHAN HOI -- xem run/$name.log"; $ok = $false
    }
}
if (-not $ok) { Write-Error "MOT SO SERVICE CHUA LEN" }
(Invoke-WebRequest -Uri "http://127.0.0.1:8080/edge/state" -UseBasicParsing).Content
