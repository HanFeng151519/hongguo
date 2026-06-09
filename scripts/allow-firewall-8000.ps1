# Run as Administrator: right-click -> Run with PowerShell
$ruleName = "Hongguo Uvicorn 8000"
$null = netsh advfirewall firewall show rule name="$ruleName" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Rule already exists: $ruleName"
} else {
    netsh advfirewall firewall add rule name="$ruleName" dir=in action=allow protocol=TCP localport=8000 profile=private,public,domain
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed. Run PowerShell as Administrator."
        exit 1
    }
    Write-Host "Allowed inbound TCP port 8000."
}

$ip = $null
try {
    $ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
        $_.IPAddress -notmatch '^127\.' -and $_.PrefixOrigin -ne 'WellKnown'
    } | Select-Object -First 1).IPAddress
} catch {
    $ip = $null
}
if ($ip) {
    Write-Host "Phone browser URL: http://${ip}:8000"
} else {
    Write-Host "Phone browser URL: http://YOUR_PC_IP:8000"
    Write-Host "Run ipconfig to find IPv4 address."
}
Read-Host "Press Enter to close"
