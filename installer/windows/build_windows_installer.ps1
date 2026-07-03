$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Root = Split-Path -Parent $Root
Set-Location $Root

function Find-InnoCompiler {
    $command = Get-Command iscc -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }

    return $null
}

if (-not (Test-Path ".build-venv")) {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        & $pyLauncher.Source -3 -m venv .build-venv
    } else {
        & python -m venv .build-venv
    }
}

$Python = Join-Path $Root ".build-venv\Scripts\python.exe"
& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt pyinstaller
& $Python -m PyInstaller --clean --noconfirm "$Root\installer\pyinstaller\AIPhotoSorter.spec"

$iscc = Find-InnoCompiler
if (-not $iscc) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        winget install --id JRSoftware.InnoSetup -e --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
        $iscc = Find-InnoCompiler
    }
}

if (-not $iscc) {
    throw "Inno Setup compiler was not found. Install Inno Setup, then rerun this script."
}

New-Item -ItemType Directory -Force -Path "$Root\release" | Out-Null
Get-ChildItem -Path "$Root\release" -Filter "AIPhotoSorterSetup*.exe" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -ne "AIPhotoSorterSetup-0.2.2.exe" } |
    ForEach-Object {
        try {
            Remove-Item -LiteralPath $_.FullName -Force
        } catch {
            Write-Warning "Could not remove old installer $($_.FullName): $($_.Exception.Message)"
        }
    }
& $iscc "$Root\installer\windows\AIPhotoSorter.iss"
Write-Host "Created $Root\release\AIPhotoSorterSetup-0.2.2.exe"
