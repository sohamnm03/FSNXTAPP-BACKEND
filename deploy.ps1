<#
.SYNOPSIS
    Deploys this Flask app (wrapped as an Azure Function) to fsnxt-app-function.

.USAGE
    From the project root, in PowerShell:
        .\deploy.ps1

    First-time setup on a new machine:
        1. Install Azure CLI:            winget install -e --id Microsoft.AzureCLI
        2. Install Functions Core Tools: winget install -e --id Microsoft.Azure.FunctionsCoreTools
        3. Install Python 3.12:          winget install -e --id Python.Python.3.12
        4. Close and reopen the terminal so PATH updates take effect.
        5. Log in once:                  az login
    After that, just run .\deploy.ps1 whenever you want to push updated code.
#>

$ErrorActionPreference = "Stop"

$AppName = "fsnxt-app-function"
$ResourceGroup = "FS_ERP"

Write-Host "== Deploying $AppName (resource group: $ResourceGroup) ==" -ForegroundColor Cyan

# 1. Check az CLI is available and logged in
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Write-Host "Azure CLI not found. Install it with:" -ForegroundColor Red
    Write-Host "  winget install -e --id Microsoft.AzureCLI"
    exit 1
}

$account = az account show 2>$null
if (-not $account) {
    Write-Host "Not logged in to Azure. Running 'az login'..." -ForegroundColor Yellow
    az login
}

# 2. Check Functions Core Tools is available
if (-not (Get-Command func -ErrorAction SilentlyContinue)) {
    Write-Host "Azure Functions Core Tools not found. Install it with:" -ForegroundColor Red
    Write-Host "  winget install -e --id Microsoft.Azure.FunctionsCoreTools"
    exit 1
}

# 3. Build a clean virtual environment so requirements.txt is validated before upload
$venvPath = ".deploy-venv"
if (Test-Path $venvPath) {
    Remove-Item -Recurse -Force $venvPath
}

$pythonCmd = if (Get-Command py -ErrorAction SilentlyContinue) { "py -3.12" } else { "python" }
Write-Host "Creating temporary virtual environment to validate dependencies..." -ForegroundColor Cyan
Invoke-Expression "$pythonCmd -m venv $venvPath"
& "$venvPath\Scripts\python.exe" -m pip install --upgrade pip -q
& "$venvPath\Scripts\python.exe" -m pip install -r requirements.txt -q

Write-Host "Validating that the Functions entry point imports cleanly..." -ForegroundColor Cyan
& "$venvPath\Scripts\python.exe" -c "import azure.functions as func; from app import app as flask_app; func.WsgiFunctionApp(app=flask_app.wsgi_app, http_auth_level=func.AuthLevel.ANONYMOUS); print('OK: function_app.py imports cleanly')"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Import check failed - fix the error above before deploying." -ForegroundColor Red
    Remove-Item -Recurse -Force $venvPath
    exit 1
}

Remove-Item -Recurse -Force $venvPath

# 4. Publish
Write-Host "Publishing to Azure..." -ForegroundColor Cyan
func azure functionapp publish $AppName --python

if ($LASTEXITCODE -ne 0) {
    Write-Host "Deployment failed. See output above." -ForegroundColor Red
    exit 1
}

Write-Host "== Deployment finished ==" -ForegroundColor Green
