$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$env:DOCKER_HOST = 'npipe:////./pipe/dockerDesktopLinuxEngine'

# Load the project's configuration without printing credentials. Parent-shell
# variables otherwise override Compose's .env file.
if (Test-Path -LiteralPath '.env') {
    foreach ($line in Get-Content -LiteralPath '.env') {
        if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
            Set-Item -Path "Env:$($matches[1])" -Value $matches[2]
        }
    }
}

if (-not (Test-Path '\\.\pipe\dockerDesktopLinuxEngine')) {
    $desktopPath = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path -LiteralPath $desktopPath)) {
        throw 'Docker Desktop was not found. Start your installed Docker Desktop first.'
    }
    if (-not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
        Start-Process -FilePath $desktopPath -WindowStyle Hidden
    }
    Write-Host 'Waiting for Docker Desktop...'
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (Test-Path '\\.\pipe\dockerDesktopLinuxEngine') { break }
        Start-Sleep -Seconds 2
    }
}
$ready = $false
Write-Host 'Waiting for Docker Engine...'
$engineReadyDeadline = (Get-Date).AddMinutes(10)
while ((Get-Date) -lt $engineReadyDeadline) {
    $probeInfo = New-Object System.Diagnostics.ProcessStartInfo
    $probeInfo.FileName = (Get-Command docker).Source
    $probeInfo.Arguments = 'version --format "{{.Server.Version}}"'
    $probeInfo.UseShellExecute = $false
    $probeInfo.CreateNoWindow = $true
    $probeInfo.RedirectStandardOutput = $true
    $probeInfo.RedirectStandardError = $true
    $probe = [System.Diagnostics.Process]::Start($probeInfo)
    try {
        if ($probe.WaitForExit(3000)) {
            if ($probe.ExitCode -eq 0) {
                $engineVersion = $probe.StandardOutput.ReadToEnd().Trim()
                $ready = $true
                break
            }
        } else {
            $probe.Kill()
        }
    } finally {
        $probe.Dispose()
    }
    Start-Sleep -Seconds 2
}
if (-not $ready) { throw 'Docker Engine is unavailable; no project containers were started.' }
Write-Host "Docker Engine: $engineVersion"

# Remove retired containers of this Compose project; keep all data volumes.
docker compose -f docker-compose.yml -f deploy/compose.mvp.yml up -d --no-build --remove-orphans web
if ($LASTEXITCODE -ne 0) { throw 'Semora MVP startup failed.' }
Write-Host 'Semora: http://127.0.0.1:5173'
