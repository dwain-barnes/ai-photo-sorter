$ErrorActionPreference = "Stop"

$ReleaseTag = "b9860"
$ModelFile = "gemma-4-E4B-it-Q4_K_S.gguf"
$ProjectorFile = "mmproj-F16.gguf"
$ModelUrl = "https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/resolve/main/$ModelFile"
$ProjectorUrl = "https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/resolve/main/$ProjectorFile"

$AppHome = Join-Path $env:USERPROFILE ".ai_photo_sorter"
$Root = Join-Path $AppHome "llama_cpp"
$Downloads = Join-Path $Root "downloads"
$RuntimeDir = Join-Path $Root "runtime"
$ModelsDir = Join-Path $Root "models"
$LogPath = Join-Path $Root "installer_setup.log"
$RuntimeMarker = Join-Path $RuntimeDir ".runtime_asset"

New-Item -ItemType Directory -Force -Path $Downloads, $RuntimeDir, $ModelsDir | Out-Null

function Write-Log($Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
    Write-Host $Message
}

function Download-File($Url, $Destination, $Label) {
    if (Test-Path -LiteralPath $Destination) {
        $size = (Get-Item -LiteralPath $Destination).Length
        if ($size -gt 10485760) {
            Write-Log "$Label already exists."
            return
        }
    }

    $part = "$Destination.part"
    if (Test-Path -LiteralPath $part) {
        Remove-Item -LiteralPath $part -Force
    }

    Write-Log "Downloading $Label. This can take a while."
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $part
    Move-Item -LiteralPath $part -Destination $Destination -Force

    $downloadedSize = (Get-Item -LiteralPath $Destination).Length
    if ($downloadedSize -lt 10485760) {
        throw "$Label download looks too small."
    }
}

function Find-LlamaServer {
    $server = Get-ChildItem -Path $RuntimeDir -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($server) {
        return $server.FullName
    }
    return $null
}

function Stop-LlamaServer {
    $processes = @(Get-CimInstance Win32_Process -Filter "Name = 'llama-server.exe'" -ErrorAction SilentlyContinue)
    foreach ($process in $processes) {
        $commandLine = [string]$process.CommandLine
        if ($commandLine -like "*$Root*" -or $commandLine -like "*$ModelFile*" -or $commandLine -match "--port\s+8321") {
            Write-Log "Stopping existing llama.cpp server before updating runtime."
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-InstalledRuntimeAsset {
    if (Test-Path -LiteralPath $RuntimeMarker) {
        return (Get-Content -LiteralPath $RuntimeMarker -Raw).Trim()
    }
    if (-not (Find-LlamaServer)) {
        return ""
    }
    $hasCuda = Get-ChildItem -Path $RuntimeDir -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "(?i)cuda|cublas" } |
        Select-Object -First 1
    if ($hasCuda) {
        return "cuda"
    }
    return "cpu"
}

function Runtime-Matches([string]$Asset) {
    if (-not (Find-LlamaServer)) {
        return $false
    }
    if ($Asset -match "(?i)cuda-12.4" -and -not (Cuda-Dlls-Ready)) {
        return $false
    }

    $installed = Get-InstalledRuntimeAsset
    if ($installed -eq $Asset) {
        return $true
    }
    if ($installed -eq "cuda" -and $Asset -match "(?i)cuda") {
        return $true
    }
    if ($installed -eq "cpu" -and $Asset -notmatch "(?i)cuda") {
        return $true
    }
    return $false
}

function Cuda-Dlls-Ready {
    $cudart = Join-Path $RuntimeDir "cudart64_12.dll"
    $cublas = Join-Path $RuntimeDir "cublas64_12.dll"
    return ((Test-Path -LiteralPath $cudart) -and (Test-Path -LiteralPath $cublas))
}

function Select-CudaRuntimeAsset([string]$RuntimeAsset) {
    if ($RuntimeAsset -match "(?i)win-cuda-12.4-x64") {
        return "cudart-llama-bin-win-cuda-12.4-x64.zip"
    }
    return ""
}

function Get-GpuNames {
    try {
        return @(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name } | Where-Object { $_ })
    } catch {
        return @()
    }
}

function Select-RuntimeAsset {
    $gpuNames = Get-GpuNames
    $gpuText = ($gpuNames -join ", ")
    if ($gpuText -match "(?i)nvidia") {
        Write-Log "Detected NVIDIA GPU: $gpuText"
        return "llama-$ReleaseTag-bin-win-cuda-12.4-x64.zip"
    }
    if ($gpuText) {
        Write-Log "Detected GPU without CUDA path: $gpuText"
    } else {
        Write-Log "No dedicated GPU detected."
    }
    return "llama-$ReleaseTag-bin-win-cpu-x64.zip"
}

Write-Log "Preparing llama.cpp local AI for AI Photo Sorter."

$RuntimeAsset = Select-RuntimeAsset
$RuntimeUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$ReleaseTag/$RuntimeAsset"
$runtimeZip = Join-Path $Downloads $RuntimeAsset
if (-not (Runtime-Matches $RuntimeAsset)) {
    Stop-LlamaServer
    Download-File $RuntimeUrl $runtimeZip "llama.cpp runtime"
    $CudaRuntimeAsset = Select-CudaRuntimeAsset $RuntimeAsset
    $CudaRuntimeZip = ""
    if ($CudaRuntimeAsset) {
        $CudaRuntimeUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$ReleaseTag/$CudaRuntimeAsset"
        $CudaRuntimeZip = Join-Path $Downloads $CudaRuntimeAsset
        Download-File $CudaRuntimeUrl $CudaRuntimeZip "llama.cpp CUDA runtime DLLs"
    }
    Write-Log "Extracting llama.cpp runtime."
    if (Test-Path -LiteralPath $RuntimeDir) {
        Remove-Item -LiteralPath $RuntimeDir -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    Expand-Archive -LiteralPath $runtimeZip -DestinationPath $RuntimeDir -Force
    if ($CudaRuntimeZip) {
        Expand-Archive -LiteralPath $CudaRuntimeZip -DestinationPath $RuntimeDir -Force
    }
    Set-Content -LiteralPath $RuntimeMarker -Value $RuntimeAsset -Encoding UTF8
} else {
    Write-Log "llama.cpp runtime already matches this computer."
}

if (-not (Find-LlamaServer)) {
    throw "llama-server.exe was not found after extracting llama.cpp."
}

Download-File $ModelUrl (Join-Path $ModelsDir $ModelFile) $ModelFile
Download-File $ProjectorUrl (Join-Path $ModelsDir $ProjectorFile) $ProjectorFile

Write-Log "Local AI files are ready."
