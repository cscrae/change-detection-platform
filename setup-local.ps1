# Reproducible Windows x64 setup with project-local Node/Go and application environments.
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$toolsRoot = Join-Path $projectRoot '.tools'
$cacheRoot = Join-Path $toolsRoot 'cache'
$originalPath = $env:PATH
$originalGoCache = $env:GOCACHE
$originalGoToolchain = $env:GOTOOLCHAIN
$originalCudaDllDir = $env:CHANGE_CUDA_DLL_DIR
$originalTls = [Net.ServicePointManager]::SecurityProtocol

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments = @())
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit $LASTEXITCODE): $Executable $($Arguments -join ' ')"
    }
}

function Get-VerifiedArchive {
    param([string]$Name, [string]$Url, [string]$Sha256)
    $archive = Join-Path $cacheRoot $Name
    $valid = (Test-Path -LiteralPath $archive -PathType Leaf) -and
        ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -eq $Sha256)
    if (-not $valid) {
        Write-Host "Downloading $Name ..."
        Invoke-WebRequest -Uri $Url -OutFile $archive -UseBasicParsing
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $Sha256) {
        throw "SHA256 verification failed: $archive"
    }
    return $archive
}

function Initialize-PythonEnvironment {
    param([string]$RelativePath, [string]$Requirements, [string[]]$AdditionalPackages = @())
    $environmentRoot = Join-Path $projectRoot $RelativePath
    $python = Join-Path $environmentRoot 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $environmentRoot)) {
        Invoke-Checked $uv @('venv', '--python', '3.13.14', '--seed', $environmentRoot)
    }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Existing environment is incomplete: $environmentRoot. Repair it before running setup again."
    }
    Invoke-Checked $python @('-c', "import sys; print(sys.version); sys.exit(0 if sys.version_info[:3] == (3, 13, 14) else 'This environment requires Python 3.13.14')")
    $pipArguments = @('-m', 'pip', 'install', '--timeout', '30', '--retries', '1', '--disable-pip-version-check', '--index-url', 'https://pypi.org/simple', '-r', (Join-Path $projectRoot $Requirements)) + $AdditionalPackages
    $wheelCache = Join-Path $toolsRoot 'wheels'
    if (Test-Path -LiteralPath $wheelCache -PathType Container) {
        $pipArguments += @('--find-links', $wheelCache)
    }
    Invoke-Checked $python $pipArguments
    Invoke-Checked $python @('-m', 'pip', 'check')
}

function Find-ConfiguredCudaRuntime {
    $configured = $env:CHANGE_CUDA_DLL_DIR
    if ([string]::IsNullOrWhiteSpace($configured)) { $configured = $env:CHANGE_CUDA_DLL_DIRS }
    $localConfig = Join-Path $projectRoot '.env.local.json'
    if ([string]::IsNullOrWhiteSpace($configured) -and (Test-Path -LiteralPath $localConfig -PathType Leaf)) {
        $settings = Get-Content -LiteralPath $localConfig -Raw -Encoding UTF8 | ConvertFrom-Json
        $property = $settings.PSObject.Properties['CHANGE_CUDA_DLL_DIRS']
        if ($property) {
            if ($property.Value -isnot [string]) { throw 'Local configuration CHANGE_CUDA_DLL_DIRS must be a string.' }
            $configured = $property.Value
        }
    }
    $requiredDlls = @('cublasLt64_12.dll', 'cublas64_12.dll', 'cufft64_11.dll', 'cudart64_12.dll', 'cudnn64_9.dll')
    foreach ($directory in ($configured -split ';')) {
        if ([string]::IsNullOrWhiteSpace($directory)) { continue }
        $directory = $directory.Trim()
        if (-not [IO.Path]::IsPathRooted($directory)) { $directory = Join-Path $projectRoot $directory }
        $directory = [IO.Path]::GetFullPath($directory)
        $complete = $true
        foreach ($dll in $requiredDlls) {
            if (-not (Test-Path -LiteralPath (Join-Path $directory $dll) -PathType Leaf)) {
                $complete = $false
                break
            }
        }
        if ($complete) { return $directory }
    }
    return $null
}

try {
    if (-not [Environment]::Is64BitOperatingSystem -or
        ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and $env:PROCESSOR_ARCHITEW6432 -ne 'AMD64')) {
        throw 'This setup supports Windows x64 only.'
    }
    $uvCommand = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue
    if (-not $uvCommand) {
        throw 'uv.exe is required. Install it with: winget install --id astral-sh.uv --exact ; then open a new terminal and run setup-local.cmd again. See https://docs.astral.sh/uv/getting-started/installation/'
    }
    $uv = $uvCommand.Source
    New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = $originalTls -bor [Net.SecurityProtocolType]::Tls12

    $nodeRoot = Join-Path $toolsRoot 'node'
    $node = Join-Path $nodeRoot 'node.exe'
    $npm = Join-Path $nodeRoot 'npm.cmd'
    if (-not (Test-Path -LiteralPath $node -PathType Leaf)) {
        $archive = Get-VerifiedArchive 'node-v24.22.1-win-x64.zip' 'https://nodejs.org/dist/v24.22.1/node-v24.22.1-win-x64.zip' 'd2046d28c2c3dcd211ac5145c0bf30e2047ef9dfb306693c8c6e2eccd8b95fa5'
        $unpack = Join-Path $cacheRoot 'node-expanded'
        Expand-Archive -LiteralPath $archive -DestinationPath $unpack -Force
        New-Item -ItemType Directory -Path $nodeRoot -Force | Out-Null
        Get-ChildItem -LiteralPath (Join-Path $unpack 'node-v24.22.1-win-x64') -Force |
            Copy-Item -Destination $nodeRoot -Recurse -Force
    }
    $nodeVersion = (Invoke-Checked $node @('--version') | Out-String).Trim()
    if ($nodeVersion -ne 'v24.22.1') { throw "Expected Node v24.22.1 at $node; found $nodeVersion. Use a separate project-local runtime before retrying." }
    if (-not (Test-Path -LiteralPath $npm -PathType Leaf)) { throw "npm is missing from $nodeRoot. Repair the project-local Node installation." }
    $env:PATH = "$nodeRoot;$env:PATH"
    $npmVersion = (Invoke-Checked $npm @('--version') | Out-String).Trim()
    if ($npmVersion -ne '11.19.1') { throw "Expected npm 11.19.1 at $npm; found $npmVersion." }
    Write-Host "Node $nodeVersion / npm $npmVersion"

    $goRoot = Join-Path $toolsRoot 'go'
    $go = Join-Path $goRoot 'bin\go.exe'
    if (-not (Test-Path -LiteralPath $go -PathType Leaf)) {
        $archive = Get-VerifiedArchive 'go1.27.3.windows-amd64.zip' 'https://go.dev/dl/go1.27.3.windows-amd64.zip' 'f26081f438319757f8a1b46d7d0b2c5ec7583d4038e485e0aa725cfde7885fff'
        Expand-Archive -LiteralPath $archive -DestinationPath $toolsRoot -Force
    }
    $env:GOTOOLCHAIN = 'local'
    $goVersion = (Invoke-Checked $go @('version') | Out-String).Trim()
    if ($goVersion -ne 'go version go1.27.3 windows/amd64') { throw "Expected Go 1.27.3 windows/amd64 at $go; found $goVersion." }
    Write-Host $goVersion
    $env:PATH = "$(Join-Path $goRoot 'bin');$env:PATH"
    $env:GOCACHE = Join-Path $projectRoot 'backend-go\.cache\go-build'
    New-Item -ItemType Directory -Path $env:GOCACHE -Force | Out-Null

    Invoke-Checked $uv @('python', 'install', '3.13.14')
    Initialize-PythonEnvironment 'backend-py\.venv-local' 'backend-py\requirements.lock.txt'
    $gpuPackages = @()
    $cudaRuntime = Find-ConfiguredCudaRuntime
    if ($cudaRuntime) {
        $env:CHANGE_CUDA_DLL_DIR = $cudaRuntime
        $env:PATH = "$cudaRuntime;$env:PATH"
        Write-Host "Reusing configured CUDA 12 / cuDNN 9 runtime: $cudaRuntime"
    } else {
        $env:CHANGE_CUDA_DLL_DIR = $null
        $gpuPackages = @('onnxruntime-gpu[cuda,cudnn]==1.23.2')
        Write-Host 'Installing CUDA 12 / cuDNN 9 runtime wheels in the GPU environment.'
    }
    Initialize-PythonEnvironment 'model-runtime\.venv-gpu' 'model-runtime\requirements.windows-gpu.txt' -AdditionalPackages $gpuPackages

    Push-Location (Join-Path $projectRoot 'frontend')
    try {
        Invoke-Checked $npm @('ci', '--registry=https://registry.npmjs.org')
        Invoke-Checked $npm @('run', 'build')
    } finally { Pop-Location }

    $binRoot = Join-Path $toolsRoot 'bin'
    New-Item -ItemType Directory -Path $binRoot -Force | Out-Null
    Push-Location (Join-Path $projectRoot 'backend-go')
    try {
        Invoke-Checked $go @('mod', 'download')
        Invoke-Checked $go @('build', '-o', (Join-Path $binRoot 'cdp-server.exe'), './cmd/server')
    } finally { Pop-Location }

    Invoke-Checked (Join-Path $projectRoot 'backend-py\.venv-local\Scripts\python.exe') @((Join-Path $projectRoot 'scripts\check_gpu_runtime.py'))

    foreach ($relativePath in @('data\imagery', 'model-runtime\model')) {
        New-Item -ItemType Directory -Path (Join-Path $projectRoot $relativePath) -Force | Out-Null
    }
    $configPath = Join-Path $projectRoot '.env.local.json'
    if (-not (Test-Path -LiteralPath $configPath)) {
        [ordered]@{
            LOCAL_IMAGERY_DIR = 'data/imagery'
            CHANGE_ONNX_PYTHON = 'model-runtime/.venv-gpu/Scripts/python.exe'
            CHANGE_MODEL_PATH = 'model-runtime/model/enforcement_2.onnx'
        } | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8
    }
    Write-Host ''
    Write-Host 'Setup complete. Existing .env.local.json settings were preserved.'
    Write-Host 'Place imagery in data/imagery and the ONNX model at model-runtime/model/enforcement_2.onnx (or edit .env.local.json).'
    Write-Host 'A compatible NVIDIA driver is required for GPU detection.'
    Write-Host 'Check:  .\start-all.cmd -CheckOnly'
    Write-Host 'Start:  .\start-all.cmd'
    Write-Host 'Open:   http://localhost:5173'
    Write-Host 'Stop:   .\stop-all.cmd'
} catch {
    [Console]::Error.WriteLine("Setup failed: $($_.Exception.Message)")
    exit 1
} finally {
    $env:PATH = $originalPath
    $env:GOCACHE = $originalGoCache
    $env:GOTOOLCHAIN = $originalGoToolchain
    $env:CHANGE_CUDA_DLL_DIR = $originalCudaDllDir
    [Net.ServicePointManager]::SecurityProtocol = $originalTls
}
