# A loopback origin permits clipboard APIs without a public domain/certificate.
param(
    [ValidateRange(1024, 65535)][int]$LocalPort = 4402,
    [ValidateSet(4002, 4003)][int]$ServerPort = 4002
)
$ErrorActionPreference = 'Stop'
$keyPath = Join-Path $env:USERPROFILE '.ssh\openclaw_codex_rsa'
if (-not (Test-Path -LiteralPath $keyPath)) { throw "Missing dedicated SSH key: $keyPath" }
$forward = "127.0.0.1:${LocalPort}:127.0.0.1:${ServerPort}"
$listener = Get-NetTCPConnection -State Listen -LocalPort $LocalPort -ErrorAction SilentlyContinue
if ($listener) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener[0].OwningProcess)"
    if ($owner.Name -ne 'ssh.exe' -or $owner.CommandLine -notlike "*$forward*" -or $owner.CommandLine -notlike '*openclaw@192.168.1.254*') {
        throw "Local port $LocalPort is already occupied by another application."
    }
} else {
    $logBase = Join-Path $env:TEMP "codex-remote-browser-$LocalPort"
    $tunnel = Start-Process -FilePath "$env:WINDIR\System32\OpenSSH\ssh.exe" -WindowStyle Hidden -PassThru `
        -ArgumentList @('-i', ('"{0}"' -f $keyPath), '-N', '-T', '-o', 'BatchMode=yes',
            '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=12', '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=3', '-L', $forward, 'openclaw@192.168.1.254') `
        -RedirectStandardError "$logBase.stderr.log" -RedirectStandardOutput "$logBase.stdout.log"
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if ($tunnel.HasExited) { throw "SSH tunnel failed. See $logBase.stderr.log" }
        if (Get-NetTCPConnection -State Listen -LocalPort $LocalPort -ErrorAction SilentlyContinue) {
            $ready = $true
            break
        }
        Start-Sleep -Milliseconds 200
    }
    if (-not $ready) {
        Stop-Process -Id $tunnel.Id
        throw "SSH tunnel did not start. See $logBase.stderr.log"
    }
}
Write-Output "Remote browser: http://127.0.0.1:$LocalPort/remote-browser"
