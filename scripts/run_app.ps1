param(
    [int]$Port = 8000,
    [string]$HostName = "127.0.0.1",
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$VenvDir = Join-Path $Root ".venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"
$RequirementsFile = Join-Path $Root "requirements.txt"
$RequirementsStamp = Join-Path $VenvDir ".requirements.stamp"

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

function Get-BootstrapPython {
    $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pythonLauncher) {
        return @($pythonLauncher.Source, "-3")
    }

    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        return @($pythonCommand.Source)
    }

    throw "Python 3.12+ not found. Please install Python or add python/py to PATH."
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

function Ensure-PythonEnvironment {
    # 首次运行自动创建虚拟环境；后续只在 requirements.txt 变化时重装依赖。
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        $bootstrap = @(Get-BootstrapPython)
        $bootstrapPython = $bootstrap[0]
        $bootstrapArgs = @()
        if ($bootstrap.Count -gt 1) {
            $bootstrapArgs = $bootstrap[1..($bootstrap.Count - 1)]
        }

        Write-Host "Python venv not found. Creating: $VenvDir" -ForegroundColor Yellow
        Invoke-Native -FilePath $bootstrapPython -Arguments ($bootstrapArgs + @("-m", "venv", $VenvDir))
    }

    if (-not (Test-Path -LiteralPath $PythonExe)) {
        throw "Python venv create failed: $PythonExe"
    }

    if (-not (Test-Path -LiteralPath $RequirementsFile)) {
        Write-Host "requirements.txt not found; skip dependency install." -ForegroundColor Yellow
        return
    }

    $currentHash = Get-FileSha256 -Path $RequirementsFile
    $installedHash = ""
    if (Test-Path -LiteralPath $RequirementsStamp) {
        $installedHash = (Get-Content -LiteralPath $RequirementsStamp -Raw).Trim()
    }

    if ($currentHash -ne $installedHash) {
        Write-Host "Installing Python dependencies from requirements.txt..." -ForegroundColor Yellow
        Invoke-Native -FilePath $PythonExe -Arguments @("-m", "pip", "install", "--upgrade", "pip")
        Invoke-Native -FilePath $PythonExe -Arguments @("-m", "pip", "install", "-r", $RequirementsFile)
        Set-Content -LiteralPath $RequirementsStamp -Value $currentHash -Encoding UTF8
        Write-Host "Python dependencies ready." -ForegroundColor Green
    }
}

function Test-TcpPort {
    param(
        [string]$TargetHost,
        [int]$TargetPort
    )
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($TargetHost, $TargetPort, $null, $null)
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

function Stop-ExistingProjectServer {
    param([int]$TargetPort)
    $process = Get-ListeningProcessOnPort -TargetPort $TargetPort
    if (-not $process) {
        return $true
    }

    $cmd = [string]($process.CommandLine)
    $isProjectServer = $cmd -match "uvicorn\s+main:app"
    if (-not $isProjectServer -and $cmd -match "uvicorn\s+main:app") {
        # Windows 上 pyenv shim 可能再派生一个真正的 python.exe，子进程命令行不含项目路径；
        # 向上追父进程，只要父链含当前项目目录，即视为本项目旧后端。
        $parentId = [int]($process.ParentProcessId)
        for ($i = 0; $i -lt 5 -and $parentId -gt 0; $i++) {
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $parentId" -ErrorAction SilentlyContinue
            if (-not $parent) {
                break
            }
            $parentCmd = [string]($parent.CommandLine)
            if ($parentCmd -match [regex]::Escape($Root)) {
                $isProjectServer = $true
                break
            }
            $parentId = [int]($parent.ParentProcessId)
        }
    }
    if (-not $isProjectServer) {
        Write-Host "Port $TargetPort is occupied by another process (PID $($process.ProcessId))." -ForegroundColor Yellow
        Write-Host $cmd
        return $false
    }

    Write-Host "Stopping existing backend on port $TargetPort (PID $($process.ProcessId))..." -ForegroundColor Yellow
    Stop-Process -Id $process.ProcessId -Force
    Start-Sleep -Seconds 2
    return -not (Test-TcpPort -TargetHost $HostName -TargetPort $TargetPort)
}

$Url = "http://${HostName}:$Port"
$env:PYTHONUTF8 = "1"

Set-Location -LiteralPath $Root
Ensure-PythonEnvironment

Write-Host "Project root: $Root"
Write-Host "URL: $Url"
Write-Host "Python: $PythonExe"
Write-Host ""

if (Test-TcpPort -TargetHost $HostName -TargetPort $Port) {
    if (-not (Stop-ExistingProjectServer -TargetPort $Port)) {
        Write-Host "Port $Port is already listening. Opening page directly." -ForegroundColor Yellow
        if (-not $NoBrowser) {
            Start-Process $Url
        }
        exit 0
    }
}

if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($TargetUrl)
        Start-Sleep -Seconds 3
        Start-Process $TargetUrl
    } -ArgumentList $Url | Out-Null
}

Write-Host "Starting backend. Close this window to stop the service." -ForegroundColor Cyan
& $PythonExe -m uvicorn main:app --host $HostName --port $Port
