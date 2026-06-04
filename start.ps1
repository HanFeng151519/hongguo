# Windows 启动脚本（与 start.sh 行为一致）
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerDir = Join-Path $Root "server"
if ((Test-Path "requirements.txt") -and (Test-Path "main.py")) {
    # 已在 server 目录（勿再 cd server）
    $ServerDir = (Get-Location).Path
    $Root = Split-Path -Parent $ServerDir
} elseif (-not (Test-Path $ServerDir)) {
    Write-Error "找不到 server 目录: $ServerDir"
}
Set-Location $ServerDir

if (Test-Path (Join-Path $Root ".env")) {
    Get-Content (Join-Path $Root ".env") | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        if ($line.StartsWith("export ")) { $line = $line.Substring(7).Trim() }
        if ($line -match "^([^=]+)=(.*)$") {
            $k = $matches[1].Trim()
            $v = $matches[2].Trim().Trim('"').Trim("'")
            if ($k -and -not (Get-Item "Env:$k" -ErrorAction SilentlyContinue)) {
                Set-Item -Path "Env:$k" -Value $v
            }
        }
    }
}

if (-not $env:HF_ENDPOINT -and $env:HONGGUO_HF_ENDPOINT) {
    $env:HF_ENDPOINT = $env:HONGGUO_HF_ENDPOINT
} elseif (-not $env:HF_ENDPOINT -and $env:HONGGUO_HF_MIRROR -ne "0") {
    $env:HF_ENDPOINT = "https://hf-mirror.com"
}

$py = "py"
if (-not (Get-Command $py -ErrorAction SilentlyContinue)) { $py = "python" }

& $py -3.12 -m pip install -r requirements.txt -q
if ($env:HONGGUO_ASR_ENABLED -ne "0") {
    & $py -3.12 -m pip install -r requirements-asr.txt -q 2>$null
}
if ($env:HONGGUO_FQ_KOC_AUTO_SYNC -eq "1") {
    & $py -3.12 -m pip install -r requirements-browser.txt -q 2>$null
}

Write-Host ""
Write-Host "服务已绑定 0.0.0.0:8000，请在浏览器打开（不要用 0.0.0.0）：" -ForegroundColor Green
Write-Host "  http://localhost:8000" -ForegroundColor Cyan
Write-Host "  http://127.0.0.1:8000" -ForegroundColor Cyan
Write-Host ""
& $py -3.12 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
