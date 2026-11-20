param(
    [ValidateSet('api', 'web', 'ontology', 'executor')]
    [string[]]$Services = @('api', 'web')
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$env:DOCKER_HOST = 'npipe:////./pipe/dockerDesktopLinuxEngine'

# Project credentials must win over credentials inherited from other projects.
# Never print the configuration or credential values.
if (-not (Test-Path -LiteralPath '.env')) { throw 'Project .env is missing.' }
foreach ($line in Get-Content -LiteralPath '.env') {
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
        Set-Item -Path "Env:$($matches[1])" -Value $matches[2]
    }
}
docker compose -f docker-compose.yml -f deploy/compose.mvp.yml up -d --no-build --no-deps @Services
if ($LASTEXITCODE -ne 0) { throw 'Semora MVP update failed.' }
