$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$env:DOCKER_HOST = 'npipe:////./pipe/dockerDesktopLinuxEngine'
docker compose -f docker-compose.yml -f deploy/compose.mvp.yml stop
if ($LASTEXITCODE -ne 0) { throw 'Stopping Semora failed.' }
# Preserve images, volumes, configuration and query history.
