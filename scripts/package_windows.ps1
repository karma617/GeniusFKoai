param(
    [switch]$SkipElectronInstaller
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$FrontendDir = Join-Path $Root "frontend"
$ElectronDir = Join-Path $Root "electron"
$PythonExe = Join-Path $Root ".venv\Scripts\python.exe"
$NpmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($NpmCommand) {
    $NpmCmd = $NpmCommand.Source
} else {
    $NpmCmd = (Get-Command npm -ErrorAction Stop).Source
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python venv not found: $PythonExe. Create .venv and install requirements.txt first."
}

function Invoke-Step {
    param(
        [string]$Title,
        [scriptblock]$Action
    )
    Write-Host ""
    Write-Host "==== $Title ====" -ForegroundColor Cyan
    & $Action
}

function Remove-PathIfExists {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Invoke-Native {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

Set-Location -LiteralPath $Root
$env:PYTHONUTF8 = "1"

Invoke-Step "1/5 Install/check frontend dependencies" {
    if (-not (Test-Path -LiteralPath (Join-Path $FrontendDir "node_modules"))) {
        Invoke-Native -FilePath $NpmCmd -Arguments @("ci", "--prefix", $FrontendDir)
    }
}

Invoke-Step "2/5 Build frontend static files" {
    Invoke-Native -FilePath $NpmCmd -Arguments @("run", "build", "--prefix", $FrontendDir)
}

Invoke-Step "3/5 Package Python backend" {
    $driverDir = & $PythonExe -c "import pathlib, patchright; print(pathlib.Path(patchright.__file__).parent / 'driver')"
    $driverDir = [string]$driverDir
    $nodeExe = Join-Path $driverDir "node.exe"
    $driverPackage = Join-Path $driverDir "package"
    if (-not (Test-Path -LiteralPath $nodeExe)) {
        throw "patchright driver node.exe not found: $nodeExe"
    }
    if (-not (Test-Path -LiteralPath $driverPackage)) {
        throw "patchright driver package not found: $driverPackage"
    }

    Remove-PathIfExists (Join-Path $Root "dist")
    Remove-PathIfExists (Join-Path $Root "build")
    Remove-PathIfExists (Join-Path $ElectronDir "backend")
    $specPath = Join-Path $Root "backend.spec"
    if (Test-Path -LiteralPath $specPath) {
        Remove-Item -LiteralPath $specPath -Force
    }

    $pyInstallerArgs = @(
        "-m", "PyInstaller",
        "--onefile",
        "--name", "backend",
        "--add-data", "$Root\static;static",
        "--add-binary", "$nodeExe;playwright\driver",
        "--add-data", "$driverPackage;playwright\driver\package",
        "--collect-submodules", "api",
        "--collect-submodules", "application",
        "--collect-submodules", "core",
        "--collect-submodules", "domain",
        "--collect-submodules", "infrastructure",
        "--collect-submodules", "platforms",
        "--collect-submodules", "providers",
        "--collect-submodules", "services",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols",
        "--hidden-import", "uvicorn.protocols.http",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "uvicorn.lifespan.off",
        "--hidden-import", "services.turnstile_solver.api_solver",
        "--hidden-import", "services.turnstile_solver.db_results",
        "--hidden-import", "services.turnstile_solver.browser_configs",
        "--hidden-import", "services.turnstile_solver.start",
        "--collect-all", "quart",
        "--collect-all", "patchright",
        "--collect-all", "rich",
        "--collect-all", "browserforge",
        "--collect-all", "apify_fingerprint_datapoints",
        "--collect-all", "camoufox",
        "--collect-all", "language_tags",
        "--collect-all", "hypercorn",
        "$Root\main.py"
    )
    Invoke-Native -FilePath $PythonExe -Arguments $pyInstallerArgs

    $backendOut = Join-Path $ElectronDir "backend\backend"
    New-Item -ItemType Directory -Force -Path $backendOut | Out-Null
    Copy-Item -LiteralPath (Join-Path $Root "dist\backend.exe") -Destination (Join-Path $backendOut "backend.exe") -Force
}

Invoke-Step "4/5 Install/check Electron dependencies" {
    if (-not (Test-Path -LiteralPath (Join-Path $ElectronDir "node_modules"))) {
        & $NpmCmd ci --prefix $ElectronDir
        if ($LASTEXITCODE -ne 0) {
            Write-Host "npm ci failed; running npm install to refresh Electron lock file." -ForegroundColor Yellow
            Invoke-Native -FilePath $NpmCmd -Arguments @("install", "--prefix", $ElectronDir)
        }
    }
}

if (-not $SkipElectronInstaller) {
    Invoke-Step "5/5 Build Windows installer" {
        Invoke-Native -FilePath $NpmCmd -Arguments @("run", "build:win", "--prefix", $ElectronDir)
    }
} else {
    Write-Host ""
    Write-Host "Skip Electron installer. Backend output: electron\backend\backend\backend.exe" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Packaging completed." -ForegroundColor Green
Write-Host "Backend executable: $ElectronDir\backend\backend\backend.exe"
if (-not $SkipElectronInstaller) {
    Write-Host "Installer directory: $ElectronDir\dist"
}
