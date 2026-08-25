# Windows equivalent of scripts/down_bare.sh: resume (a SIGSTOP'd process cannot be
# cleanly terminated) then kill the whole tree, parent launcher included.
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = ".\.venv\Scripts\python.exe"
Get-ChildItem run/*.pid -ErrorAction SilentlyContinue | ForEach-Object {
    $target = (Get-Content $_ -Raw).Trim()
    if ($target) {
        & $py -c "import os,signal,sys`ntry: os.kill(int(sys.argv[1]), signal.SIGCONT)`nexcept Exception: pass" $target 2>$null
        & taskkill /PID $target /T /F 2>$null | Out-Null
    }
    Remove-Item $_ -Force
}
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'uvicorn (serving\.app|edge\.proxy)' } |
    ForEach-Object { & taskkill /PID $_.ProcessId /T /F 2>$null | Out-Null }
Write-Host "all stopped"
