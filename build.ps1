<#
.SYNOPSIS
    Builds the Bling Reborn installer: dist\BlingReborn-Setup-<version>.exe

.DESCRIPTION
    1. Finds Python 3.10+ (installs Python 3.12 for the current user via winget if missing)
    2. Creates build\venv and installs blinkpy, pystray, Pillow and PyInstaller
    3. Builds BlingReborn.exe (tray app) and bling-reborn-cli.exe with PyInstaller
    4. Optionally code-signs them (-CertThumbprint)
    5. Packs everything with Inno Setup (installs it via winget if missing)

.EXAMPLE
    .\build.ps1

.EXAMPLE
    .\build.ps1 -CertThumbprint 0123456789ABCDEF0123456789ABCDEF01234567

.NOTES
    If scripts are blocked:  powershell -ExecutionPolicy Bypass -File .\build.ps1
#>
[CmdletBinding()]
param(
    [string]$Version,
    [string]$CertThumbprint,
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe exited with code $LASTEXITCODE" }
}

function Find-Python {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue)     { $candidates += ,@("py", "-3") }
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += ,@("python") }
    $installed = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" `
        -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
    foreach ($exe in $installed) { $candidates += ,@($exe.FullName) }

    foreach ($c in $candidates) {
        $probe = @($c | Select-Object -Skip 1) + @("-c", "import sys;print(sys.executable);print(sys.version_info[0]*100+sys.version_info[1])")
        try { $out = & $c[0] @probe 2>$null } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $out.Count -ge 2 -and [int]$out[1] -ge 310) { return $out[0] }
    }
    return $null
}

function Find-InnoSetup {
    $roots = @(${env:ProgramFiles(x86)}, $env:ProgramFiles, "$env:LOCALAPPDATA\Programs") | Where-Object { $_ }
    Get-ChildItem $roots -Filter "Inno Setup*" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName "ISCC.exe" } | Where-Object { Test-Path $_ } |
        Sort-Object -Descending | Select-Object -First 1
}

function Find-SignTool {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "\\x64\\" } | Sort-Object FullName -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}

function Install-WithWinget($id, $extra) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget isn't available. Install $id manually and run this script again."
    }
    Write-Host "Installing $id with winget..."
    Invoke-Native winget (@("install", "--id", $id, "-e", "--accept-package-agreements", "--accept-source-agreements") + $extra)
}

function Sign-Files([string[]]$Files) {
    $signtool = Find-SignTool
    if (-not $signtool) { throw "signtool.exe not found. Install the Windows SDK signing tools or omit -CertThumbprint." }
    Invoke-Native $signtool (@("sign", "/sha1", $CertThumbprint, "/fd", "SHA256", "/tr", $TimestampUrl, "/td", "SHA256") + $Files)
}

# ------------------------------------------------------------------ version
if (-not $Version) {
    $Version = (Select-String -Path bling_reborn.py -Pattern '^__version__ = "(.+)"').Matches[0].Groups[1].Value
}
Write-Host "Building Bling Reborn $Version"

# ------------------------------------------------------------------ Python
Step "Finding Python"
$python = Find-Python
if (-not $python) {
    Install-WithWinget "Python.Python.3.12" @("--scope", "user")
    $python = Find-Python
    if (-not $python) { throw "Python was installed but can't be found yet. Open a new PowerShell window and run build.ps1 again." }
}
Write-Host "Using $python"

# ------------------------------------------------------------------ virtual environment
Step "Preparing build environment"
$venv = Join-Path $PSScriptRoot "build\venv"
$vpy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $vpy)) { Invoke-Native $python @("-m", "venv", $venv) }
Invoke-Native $vpy @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
Invoke-Native $vpy @("-m", "pip", "install", "--quiet", "--upgrade", "-r", "requirements.txt", "pyinstaller")

# ------------------------------------------------------------------ app
Step "Building BlingReborn.exe"
New-Item -ItemType Directory -Force build | Out-Null
Invoke-Native $vpy @("icons.py", "build\app.ico")
$env:BLINGREBORN_VERSION = $Version
Invoke-Native $vpy @("-m", "PyInstaller", "--noconfirm", "--clean",
    "--distpath", "build\pyinstaller\dist", "--workpath", "build\pyinstaller\work", "BlingReborn.spec")

$appDir = "build\pyinstaller\dist\BlingReborn"
Invoke-Native "$appDir\bling-reborn-cli.exe" @("--version") | Out-Null   # smoke test: the bundle starts

if ($CertThumbprint) {
    Step "Signing executables"
    Sign-Files @("$appDir\BlingReborn.exe", "$appDir\bling-reborn-cli.exe")
}

if ($SkipInstaller) {
    Write-Host "`nApp built in $appDir (installer skipped)." -ForegroundColor Green
    return
}

# ------------------------------------------------------------------ installer
Step "Building installer"
$iscc = Find-InnoSetup
if (-not $iscc) {
    Install-WithWinget "JRSoftware.InnoSetup" @()
    $iscc = Find-InnoSetup
    if (-not $iscc) { throw "Inno Setup was installed but ISCC.exe can't be found." }
}
Invoke-Native $iscc @("/Q", "/DAppVersion=$Version", "installer.iss")

$setup = Resolve-Path "dist\BlingReborn-Setup-$Version.exe"
if ($CertThumbprint) {
    Step "Signing installer"
    Sign-Files @($setup.Path)
}

Write-Host "`nDone: $setup" -ForegroundColor Green
