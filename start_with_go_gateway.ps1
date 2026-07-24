param(
    [int]$BackendPort = 8000,
    [int]$GatewayPort = 8787,
    [int]$GatewayAttempts = 4,
    [int]$GatewayTimeoutSeconds = 30,
    [int]$GatewayRaceParallel = 3,
    [int]$GatewayProxyRotations = 6,
    [string]$GatewayDir = "gopay-auto-protocol\20260609\gpt-pp-main",
    [string]$GoExe = "go",
    [switch]$SkipFrontendBuild,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$GatewayPath = Join-Path $Root $GatewayDir
$GatewayUrl = "http://127.0.0.1:$GatewayPort"
$BackendUrl = "http://127.0.0.1:$BackendPort"
$PythonExe = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExe)) {
    $PythonExe = "python"
}

$FrontendDir = Join-Path $Root "frontend"
$FrontendDepsStamp = Join-Path $FrontendDir ".frontend-deps.stamp"
$FrontendStamp = Join-Path $FrontendDir ".frontend-build.stamp"
$StaticIndex = Join-Path $Root "static\index.html"

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

function Get-NpmCommand {
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($npmCommand) {
        return $npmCommand.Source
    }

    $npmCommand = Get-Command npm -ErrorAction SilentlyContinue
    if ($npmCommand) {
        return $npmCommand.Source
    }

    throw "npm not found. Please install Node.js or add npm to PATH."
}

function Get-FileSha256 {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            $hashBytes = $sha256.ComputeHash($stream)
            return ([System.BitConverter]::ToString($hashBytes)).Replace("-", "")
        } finally {
            $sha256.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

function Get-FrontendBuildHash {
    if (-not (Test-Path -LiteralPath $FrontendDir)) {
        return ""
    }

    $hashInput = New-Object System.Text.StringBuilder
    $paths = @(
        (Join-Path $FrontendDir "package.json"),
        (Join-Path $FrontendDir "package-lock.json"),
        (Join-Path $FrontendDir "vite.config.ts"),
        (Join-Path $FrontendDir "tsconfig.json"),
        (Join-Path $FrontendDir "tsconfig.app.json"),
        (Join-Path $FrontendDir "tsconfig.node.json"),
        (Join-Path $FrontendDir "index.html")
    )

    $srcDir = Join-Path $FrontendDir "src"
    if (Test-Path -LiteralPath $srcDir) {
        $paths += Get-ChildItem -LiteralPath $srcDir -Recurse -File |
            Sort-Object FullName |
            ForEach-Object { $_.FullName }
    }

    foreach ($path in $paths) {
        if (Test-Path -LiteralPath $path) {
            [void]$hashInput.AppendLine($path)
            [void]$hashInput.AppendLine((Get-FileSha256 -Path $path))
        }
    }

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($hashInput.ToString())
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha256.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hashBytes)).Replace("-", "")
    } finally {
        $sha256.Dispose()
    }
}

function Get-FrontendDependencyHash {
    if (-not (Test-Path -LiteralPath $FrontendDir)) {
        return ""
    }

    $hashInput = New-Object System.Text.StringBuilder
    foreach ($name in @("package.json", "package-lock.json")) {
        $path = Join-Path $FrontendDir $name
        if (Test-Path -LiteralPath $path) {
            [void]$hashInput.AppendLine($path)
            [void]$hashInput.AppendLine((Get-FileSha256 -Path $path))
        }
    }

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($hashInput.ToString())
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha256.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hashBytes)).Replace("-", "")
    } finally {
        $sha256.Dispose()
    }
}

function Ensure-FrontendBuild {
    if ($SkipFrontendBuild) {
        Write-Host "[frontend] SkipFrontendBuild enabled; skip frontend check." -ForegroundColor Yellow
        return
    }

    if (-not (Test-Path -LiteralPath $FrontendDir)) {
        Write-Host "[frontend] frontend directory not found; skip frontend build." -ForegroundColor Yellow
        return
    }

    $npm = Get-NpmCommand
    $nodeModules = Join-Path $FrontendDir "node_modules"
    $currentDepsHash = Get-FrontendDependencyHash
    $installedDepsHash = ""
    if (Test-Path -LiteralPath $FrontendDepsStamp) {
        $installedDepsHash = (Get-Content -LiteralPath $FrontendDepsStamp -Raw).Trim()
    }

    if ((-not (Test-Path -LiteralPath $nodeModules)) -or ($currentDepsHash -ne $installedDepsHash)) {
        Write-Host "[frontend] Installing frontend dependencies with npm install..." -ForegroundColor Yellow
        Invoke-Native -FilePath $npm -Arguments @("install", "--prefix", $FrontendDir)
        Set-Content -LiteralPath $FrontendDepsStamp -Value $currentDepsHash -Encoding UTF8
    }

    $currentHash = Get-FrontendBuildHash
    $builtHash = ""
    if (Test-Path -LiteralPath $FrontendStamp) {
        $builtHash = (Get-Content -LiteralPath $FrontendStamp -Raw).Trim()
    }

    if (($currentHash -ne $builtHash) -or (-not (Test-Path -LiteralPath $StaticIndex))) {
        Write-Host "[frontend] Source changed or static missing; building frontend..." -ForegroundColor Yellow
        Invoke-Native -FilePath $npm -Arguments @("run", "build", "--prefix", $FrontendDir)
        Set-Content -LiteralPath $FrontendStamp -Value $currentHash -Encoding UTF8
        Write-Host "[frontend] Frontend static files ready." -ForegroundColor Green
    } else {
        Write-Host "[frontend] Up to date; skip rebuild." -ForegroundColor Green
    }
}

function Test-TcpPort {
    param(
        [string]$HostName,
        [int]$Port
    )
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(500)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Wait-TcpPort {
    param(
        [string]$HostName,
        [int]$Port,
        [int]$TimeoutSeconds = 25
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-TcpPort -HostName $HostName -Port $Port) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Get-ListeningProcessOnPort {
    param([int]$TargetPort)
    try {
        $connection = Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction Stop | Select-Object -First 1
        if (-not $connection) {
            return $null
        }
        return Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)"
    } catch {
        return $null
    }
}

function Stop-ProcessTree {
    param([int]$ProcessId)
    if ($ProcessId -le 0) {
        return
    }
    # /T 一并清子进程，避免 uvicorn/go 残留
    & taskkill.exe /PID $ProcessId /T /F 2>$null | Out-Null
    Start-Sleep -Milliseconds 300
    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Stop-PortService {
    param(
        [int]$Port,
        [string]$Label
    )
    $process = Get-ListeningProcessOnPort -TargetPort $Port
    if (-not $process) {
        Write-Host "[$Label] Port $Port is free."
        return
    }

    $cmd = [string]($process.CommandLine)
    Write-Host "[$Label] Stopping PID $($process.ProcessId) on port $Port ..." -ForegroundColor Yellow
    if ($cmd) {
        Write-Host "[$Label] $cmd"
    }
    Stop-ProcessTree -ProcessId ([int]$process.ProcessId)

    $deadline = (Get-Date).AddSeconds(8)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-TcpPort -HostName "127.0.0.1" -Port $Port)) {
            Write-Host "[$Label] Port $Port released." -ForegroundColor Green
            return
        }
        Start-Sleep -Milliseconds 400
    }

    throw "[$Label] Failed to free port $Port (still listening)."
}

function Stop-CurrentServices {
    Write-Host "[preflight] Stopping current services..." -ForegroundColor Cyan
    Stop-PortService -Port $BackendPort -Label "backend"
    Stop-PortService -Port $GatewayPort -Label "go"
}

function Show-LogTail {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        Write-Host ""
        Write-Host "---- $Path ----"
        Get-Content -LiteralPath $Path -Tail 40
        Write-Host "----------------"
    }
}

Write-Host "Root:        $Root"
Write-Host "Go gateway:  $GatewayUrl"
Write-Host "Backend:     $BackendUrl"
Write-Host ""

if (-not (Test-Path -LiteralPath $GatewayPath)) {
    throw "Go gateway directory not found: $GatewayPath"
}

$env:PAYPAL_PROTOCOL_GATEWAY_URL = $GatewayUrl
$env:PYTHONUTF8 = "1"

if ($DryRun) {
    Write-Host "[dry-run] PAYPAL_PROTOCOL_GATEWAY_URL=$env:PAYPAL_PROTOCOL_GATEWAY_URL"
    Write-Host "[dry-run] Stop services on ports: backend=$BackendPort gateway=$GatewayPort"
    Write-Host "[dry-run] Ensure frontend build (unless -SkipFrontendBuild)"
    Write-Host "[dry-run] Start Go: $GoExe run .\cmd\ppgateway -addr 127.0.0.1:$GatewayPort -attempts $GatewayAttempts -timeout ${GatewayTimeoutSeconds}s -race-parallel $GatewayRaceParallel -proxy-rotations $GatewayProxyRotations"
    Write-Host "[dry-run] Start backend: $PythonExe -m uvicorn main:app --host 0.0.0.0 --port $BackendPort"
    exit 0
}

# 1) 先停当前服务，避免端口占用和旧页面进程
Stop-CurrentServices

# 2) 检测并按需打包前端
Ensure-FrontendBuild

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$GatewayOut = Join-Path $LogDir "ppgateway.out.log"
$GatewayErr = Join-Path $LogDir "ppgateway.err.log"

$startedGateway = $false
$gatewayProcess = $null

try {
    $goCmd = Get-Command $GoExe -ErrorAction SilentlyContinue
    if (-not $goCmd) {
        throw "Go runtime not found: $GoExe. Install Go, add go.exe to PATH, or pass -GoExe C:\Go\bin\go.exe."
    }

    Write-Host "[go] Starting gateway..."
    $gatewayProcess = Start-Process `
        -FilePath $GoExe `
        -ArgumentList @(
            "run", ".\cmd\ppgateway",
            "-addr", "127.0.0.1:$GatewayPort",
            "-attempts", "$GatewayAttempts",
            "-timeout", "${GatewayTimeoutSeconds}s",
            "-race-parallel", "$GatewayRaceParallel",
            "-proxy-rotations", "$GatewayProxyRotations"
        ) `
        -WorkingDirectory $GatewayPath `
        -RedirectStandardOutput $GatewayOut `
        -RedirectStandardError $GatewayErr `
        -WindowStyle Hidden `
        -PassThru
    $startedGateway = $true

    if (-not (Wait-TcpPort -HostName "127.0.0.1" -Port $GatewayPort -TimeoutSeconds 30)) {
        Show-LogTail -Path $GatewayOut
        Show-LogTail -Path $GatewayErr
        throw "Go gateway did not become ready on $GatewayUrl"
    }
    Write-Host "[go] Gateway ready: $GatewayUrl"

    Write-Host "[env] PAYPAL_PROTOCOL_GATEWAY_URL=$env:PAYPAL_PROTOCOL_GATEWAY_URL"
    Write-Host "[backend] Starting main app on $BackendUrl"
    Write-Host "[backend] Press Ctrl+C to stop. If this script started Go gateway, it will be stopped on exit."
    Write-Host ""

    Set-Location -LiteralPath $Root
    & $PythonExe -m uvicorn main:app --host 0.0.0.0 --port $BackendPort
} finally {
    if ($startedGateway -and $gatewayProcess -and -not $gatewayProcess.HasExited) {
        Write-Host ""
        Write-Host "[go] Stopping gateway process $($gatewayProcess.Id)"
        Stop-ProcessTree -ProcessId ([int]$gatewayProcess.Id)
    }
}
