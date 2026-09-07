[CmdletBinding()]
param(
    [switch]$ElevatedChild,
    [switch]$NoElevation
)

$ErrorActionPreference = "Stop"

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$desktopRoot = Join-Path $repositoryRoot "apps\desktop"

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host "[cosir] $Message" -ForegroundColor Cyan
}

function Test-RestrictedDesktopUser {
    $identityName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    return $identityName -match "\\CodexSandbox" -or $env:USERNAME -match "^CodexSandbox"
}

function Start-ElevatedLauncher {
    $powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $quotedScriptPath = '"{0}"' -f $PSCommandPath
    $arguments = @(
        "-NoLogo"
        "-NoProfile"
        "-ExecutionPolicy"
        "Bypass"
        "-File"
        $quotedScriptPath
        "-ElevatedChild"
    )

    try {
        $elevatedProcess = Start-Process `
            -FilePath $powershell `
            -ArgumentList $arguments `
            -WorkingDirectory $repositoryRoot `
            -Verb RunAs `
            -PassThru `
            -Wait
    }
    catch {
        throw "Elevation was cancelled or failed: $($_.Exception.Message)"
    }

    if ($elevatedProcess.ExitCode -ne 0) {
        throw "Elevated desktop launcher failed. Exit code: $($elevatedProcess.ExitCode)"
    }
}

function Get-CosirProcess {
    $sessionId = (Get-Process -Id $PID).SessionId
    return Get-Process -Name "cosir-desktop" -ErrorAction SilentlyContinue |
        Where-Object { $_.SessionId -eq $sessionId }
}

function Test-LocalPortInUse {
    param([Parameter(Mandatory = $true)][int]$Port)

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect("127.0.0.1", $Port)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

if (-not (Test-Path (Join-Path $desktopRoot "package.json"))) {
    throw "Desktop package.json was not found: $desktopRoot"
}

if ((Test-RestrictedDesktopUser) -and -not $ElevatedChild -and -not $NoElevation) {
    Write-Step "The current context cannot write the WebView2 data directory; requesting desktop permissions."
    Start-ElevatedLauncher
    exit 0
}

$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -eq $npm) {
    throw "npm.cmd was not found. Install Node.js first."
}

$cargo = Get-Command cargo.exe -ErrorAction SilentlyContinue
if ($null -eq $cargo) {
    throw "cargo.exe was not found. Install Rust and cargo-tauri first."
}

$existingProcess = @(Get-CosirProcess)
if ($existingProcess.Count -gt 0) {
    $windowProcess = $existingProcess | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if ($null -ne $windowProcess) {
        $windowText = if ([string]::IsNullOrWhiteSpace($windowProcess.MainWindowTitle)) {
            ""
        }
        else {
            "($($windowProcess.MainWindowTitle))"
        }
        Write-Step "Cosir is already running$windowText; no duplicate instance will be started."
    }
    else {
        $processIds = ($existingProcess | Select-Object -ExpandProperty Id) -join ", "
        Write-Step "Cosir is already starting (PID $processIds); no duplicate instance will be started."
    }
    exit 0
}

$portIsBusy = Test-LocalPortInUse -Port 3000
if ($portIsBusy) {
    $listeners = @(Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue)
    $ownerIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($ownerIds.Count -eq 0) {
        $netstatMatches = @(netstat -ano -p tcp 2>$null | Select-String "127\.0\.0\.1:3000\s+.*LISTENING\s+(\d+)\s*$")
        $ownerIds = @($netstatMatches | ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -Unique)
    }
    $owners = $ownerIds |
        ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue } |
        Select-Object -ExpandProperty ProcessName -Unique
    $ownerText = if ($owners) { $owners -join ", " } else { "unknown process" }
    throw "Port 3000 is already in use ($ownerText). Stop the process and retry."
}

if (-not (Test-Path (Join-Path $desktopRoot "node_modules"))) {
    throw "apps/desktop/node_modules was not found. Run npm install from the repository root first."
}

Write-Step "Starting Tauri; Vite and the local FastAPI backend are managed by the app lifecycle."
Push-Location $desktopRoot
try {
    & $npm.Source run tauri:dev
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri failed to start (exit code: $LASTEXITCODE). Check whether port 3000 is available."
    }
}
finally {
    Pop-Location
}
