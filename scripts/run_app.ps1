param(
    [int]$Port = 8000,
    [string]$HostName = "127.0.0.1",
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$PythonExe = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExe)) {
    $PythonExe = "python"
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

Set-Location -LiteralPath $Root
Write-Host "Starting backend. Close this window to stop the service." -ForegroundColor Cyan
& $PythonExe -m uvicorn main:app --host $HostName --port $Port
