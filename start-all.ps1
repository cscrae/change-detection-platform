param(
    [switch]$CheckOnly,
    [ValidateSet('cuda')][string]$Provider = 'cuda'
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot 'backend-py\.venv-local\Scripts\python.exe'

function Test-Port([int]$Port) {
    foreach ($hostAddress in @('127.0.0.1', '::1')) {
        $address = [System.Net.IPAddress]::Parse($hostAddress)
        $client = New-Object System.Net.Sockets.TcpClient($address.AddressFamily)
        try {
            $pending = $client.BeginConnect($address, $Port, $null, $null)
            if ($pending.AsyncWaitHandle.WaitOne(500)) {
                $client.EndConnect($pending)
                return $true
            }
        } catch { }
        finally { $client.Dispose() }
    }
    return $false
}

function Quote-PS([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

function Resolve-ProjectPath([string]$Path) {
    if (-not [IO.Path]::IsPathRooted($Path)) { $Path = Join-Path $projectRoot $Path }
    return [IO.Path]::GetFullPath($Path)
}

function Find-Tool([string]$Relative, [string]$Name) {
    $local = Join-Path $projectRoot $Relative
    if (Test-Path -LiteralPath $local -PathType Leaf) { return $local }
    $command = Get-Command $Name -ErrorAction Stop
    return $command.Source
}

function Assert-PortIdentity($Service) {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Service.Port -ErrorAction SilentlyContinue)
    foreach ($listener in $listeners) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
        if ($owner -and $owner.Name -notin $Service.ProcessNames) {
            throw "Port $($Service.Port) belongs to $($owner.Name) (PID $($owner.ProcessId)), not $($Service.Name). Stop or reconfigure that application first."
        }
    }
    if ($Service.HealthUri) {
        try {
            $health = Invoke-RestMethod $Service.HealthUri -TimeoutSec 3
        } catch {
            throw "Port $($Service.Port) is occupied but $($Service.Name) did not return its expected health response. Check the existing process before restarting."
        }
        if ($health.($Service.HealthProperty) -ne $Service.HealthValue) {
            throw "Port $($Service.Port) does not contain the expected $($Service.Name). Stop or reconfigure the application using it."
        }
    }
}

function Add-ExistingPath([System.Collections.Generic.List[string]]$Paths, [string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return }
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if (-not $Paths.Contains($resolved)) { $Paths.Add($resolved) }
}

function Add-CudaRuntimePath([System.Collections.Generic.List[string]]$Paths, [string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return }
    $required = @(
        'cublasLt64_12.dll', 'cublas64_12.dll', 'cufft64_11.dll',
        'cudart64_12.dll', 'cudnn64_9.dll'
    )
    foreach ($name in $required) {
        if (Test-Path -LiteralPath (Join-Path $Path $name)) {
            Add-ExistingPath $Paths $Path
            return
        }
    }
}

try {
    $localConfig = Join-Path $projectRoot '.env.local.json'
    if (Test-Path -LiteralPath $localConfig -PathType Leaf) {
        $settings = Get-Content -LiteralPath $localConfig -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($key in @('LOCAL_IMAGERY_DIR', 'CHANGE_MODEL_PATH', 'CHANGE_ONNX_PYTHON', 'CHANGE_CUDA_DLL_DIRS')) {
            $property = $settings.PSObject.Properties[$key]
            if ($property -and [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($key, 'Process'))) {
                if ($property.Value -isnot [string]) { throw "Local configuration $key must be a string." }
                [Environment]::SetEnvironmentVariable($key, $property.Value, 'Process')
            }
        }
    }
    foreach ($relative in @(
        'backend-py\.venv-local\Scripts\python.exe',
        'frontend\node_modules\vite\bin\vite.js'
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $relative))) {
            throw "Missing required file: $relative"
        }
    }
    $server = Join-Path $projectRoot '.tools\bin\cdp-server.exe'
    $go = $null
    if (-not (Test-Path -LiteralPath $server -PathType Leaf)) { $go = Find-Tool '.tools\go\bin\go.exe' 'go.exe' }
    $node = Find-Tool '.tools\node\node.exe' 'node.exe'
    & $python -c 'import fastapi, uvicorn, rasterio, numpy, scipy, skimage, shapely, shapefile, requests, PIL, onnxruntime, onnx'
    if ($LASTEXITCODE -ne 0) { throw 'Python dependencies are incomplete.' }

    # The platform environment and the model runtime have different dependency
    # sets. Keep an explicit model interpreter if supplied, otherwise prefer the
    # documented Conda environment and fall back to the platform interpreter.
    $modelPython = $env:CHANGE_ONNX_PYTHON
    $projectModelPython = Join-Path $projectRoot 'model-runtime\.venv-gpu\Scripts\python.exe'
    if ([string]::IsNullOrWhiteSpace($modelPython) -and (Test-Path -LiteralPath $projectModelPython)) {
        $modelPython = $projectModelPython
    }
    $condaBase = $null
    $conda = Get-Command conda.exe -ErrorAction SilentlyContinue
    if ($Provider -eq 'cuda' -and $conda) {
        try {
            $condaBase = (& $conda.Source info --base 2>$null).Trim()
        } catch {
            $condaBase = $null
        }
    }
    if ($Provider -eq 'cuda' -and [string]::IsNullOrWhiteSpace($modelPython) -and $condaBase) {
        $candidate = Join-Path $condaBase 'envs\change_onnx\python.exe'
        if (Test-Path -LiteralPath $candidate) { $modelPython = $candidate }
    }
    if ([string]::IsNullOrWhiteSpace($modelPython)) { $modelPython = $python }
    $modelPython = Resolve-ProjectPath $modelPython
    if (-not (Test-Path -LiteralPath $modelPython -PathType Leaf)) {
        throw "Model Python interpreter not found: $modelPython"
    }

    # ONNX Runtime 1.23.x requires CUDA 12 / cuDNN 9 DLL names. CUDA 13 on
    # PATH is not compatible, so discover a complete runtime directory from
    # the model env or an installed Conda/PyTorch environment.
    if ($Provider -eq 'cuda') {
    $cudaDllDirs = New-Object 'System.Collections.Generic.List[string]'
    if ($env:CHANGE_CUDA_DLL_DIR) {
        Add-CudaRuntimePath $cudaDllDirs (Resolve-ProjectPath $env:CHANGE_CUDA_DLL_DIR)
    }
    if ($env:CHANGE_CUDA_DLL_DIRS) {
        foreach ($dir in ($env:CHANGE_CUDA_DLL_DIRS -split ';')) {
            if (-not [string]::IsNullOrWhiteSpace($dir)) { Add-ExistingPath $cudaDllDirs (Resolve-ProjectPath $dir.Trim()) }
        }
    }
    $modelEnvRoot = Split-Path $modelPython -Parent
    if ((Split-Path $modelEnvRoot -Leaf) -eq 'Scripts') { $modelEnvRoot = Split-Path $modelEnvRoot -Parent }
    $runtimeRoots = @($modelEnvRoot)
    if ($condaBase) {
        $runtimeRoots += $condaBase
        $envsRoot = Join-Path $condaBase 'envs'
        if (Test-Path -LiteralPath $envsRoot -PathType Container) {
            $runtimeRoots += @(Get-ChildItem -LiteralPath $envsRoot -Directory | Select-Object -ExpandProperty FullName)
        }
    }
    foreach ($root in ($runtimeRoots | Select-Object -Unique)) {
        Add-CudaRuntimePath $cudaDllDirs (Join-Path $root 'Library\bin')
        Add-CudaRuntimePath $cudaDllDirs (Join-Path $root 'Lib\site-packages\torch\lib')
        $nvidiaRoot = Join-Path $root 'Lib\site-packages\nvidia'
        if (Test-Path -LiteralPath $nvidiaRoot -PathType Container) {
            foreach ($bin in @(Get-ChildItem -LiteralPath $nvidiaRoot -Directory -Recurse -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -eq 'bin' } | Select-Object -ExpandProperty FullName)) {
                Add-ExistingPath $cudaDllDirs $bin
            }
        }
    }
    $completeRuntime = $null
    $requiredDlls = @('cublasLt64_12.dll', 'cublas64_12.dll', 'cufft64_11.dll', 'cudart64_12.dll', 'cudnn64_9.dll')
    $foundDlls = @{}
    foreach ($dir in $cudaDllDirs) {
        $hasAll = $true
        foreach ($name in $requiredDlls) {
            if (Test-Path -LiteralPath (Join-Path $dir $name)) { $foundDlls[$name] = $true }
            else { $hasAll = $false }
        }
        if ($hasAll -and -not $completeRuntime) { $completeRuntime = $dir }
    }
    $missingDlls = @($requiredDlls | Where-Object { -not $foundDlls.ContainsKey($_) })
    if ($cudaDllDirs.Count -gt 0) {
        $env:PATH = (($cudaDllDirs -join ';') + ';' + $env:PATH)
    }
    if ($completeRuntime) {
        $env:CHANGE_CUDA_DLL_DIR = $completeRuntime
        Write-Host "CUDA runtime DLLs: $completeRuntime"
    } elseif ($missingDlls.Count -eq 0) {
        # NVIDIA pip extras split CUDA and cuDNN among several package bins.
        # An empty directory lets ORT preload from those NVIDIA packages.
        $env:CHANGE_CUDA_DLL_DIR = ''
        Write-Host "Required CUDA 12/cuDNN 9 DLLs found across $($cudaDllDirs.Count) runtime directories."
    } else {
        Write-Warning "CUDA runtime discovery did not find: $($missingDlls -join ', '). GPU model validation may fail."
    }
    }
    $env:CHANGE_ONNX_PROVIDER = $Provider
    $env:CHANGE_ONNX_PYTHON = $modelPython
    if ([string]::IsNullOrWhiteSpace($env:LOCAL_IMAGERY_DIR)) {
        $env:LOCAL_IMAGERY_DIR = Join-Path $projectRoot 'data\imagery'
    }
    $env:LOCAL_IMAGERY_DIR = Resolve-ProjectPath $env:LOCAL_IMAGERY_DIR
    if (-not (Test-Path -LiteralPath $env:LOCAL_IMAGERY_DIR -PathType Container)) {
        Write-Warning "Initial imagery directory is unavailable. Repair sources in Management: $env:LOCAL_IMAGERY_DIR"
    }
    if ([string]::IsNullOrWhiteSpace($env:CHANGE_MODEL_PATH)) {
        $env:CHANGE_MODEL_PATH = Join-Path $projectRoot 'model-runtime\model\enforcement_2.onnx'
    }
    $env:CHANGE_MODEL_PATH = Resolve-ProjectPath $env:CHANGE_MODEL_PATH
    $gpuReady = $false

$gpuProbe = @'
import os, sys
import onnxruntime as ort
model = sys.argv[1]
directory = os.environ.get('CHANGE_CUDA_DLL_DIR', '')
provider = os.environ['CHANGE_ONNX_PROVIDER']
if provider == 'cuda' and hasattr(ort, 'preload_dlls'):
    ort.preload_dlls(directory=directory)
expected = 'CUDAExecutionProvider' if provider == 'cuda' else 'CPUExecutionProvider'
session = ort.InferenceSession(model, providers=[expected])
active = session.get_providers()
if expected not in active:
    raise RuntimeError(expected + ' did not start; active providers: ' + repr(active))
print(expected + ' OK')
'@
    if (-not (Test-Path -LiteralPath $env:CHANGE_MODEL_PATH -PathType Leaf)) {
        Write-Warning "Model file is unavailable: $env:CHANGE_MODEL_PATH. Import a compatible ONNX model in Management before detection."
    } else {
        $probeInfo = New-Object System.Diagnostics.ProcessStartInfo
        $probeInfo.FileName = $modelPython
        $probeInfo.Arguments = '- "' + $env:CHANGE_MODEL_PATH + '"'
        $probeInfo.UseShellExecute = $false
        $probeInfo.CreateNoWindow = $true
        $probeInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
        $probeInfo.RedirectStandardInput = $true
        $probeInfo.RedirectStandardOutput = $true
        $probeInfo.RedirectStandardError = $true
        $probe = [System.Diagnostics.Process]::Start($probeInfo)
        try {
            $probeOutput = $probe.StandardOutput.ReadToEndAsync()
            $probeError = $probe.StandardError.ReadToEndAsync()
            $probe.StandardInput.Write($gpuProbe)
            $probe.StandardInput.Close()
            if (-not $probe.WaitForExit(45000)) {
                & "$env:SystemRoot\System32\taskkill.exe" /PID $probe.Id /T /F 2>&1 | Out-Null
                Write-Warning 'CUDA model session check exceeded 45 seconds. Services will start for management and diagnostics.'
            } elseif ($probe.ExitCode -eq 0) {
                $gpuReady = $true
                Write-Host 'CUDA model session check passed. Complete model validation in Management before detection.'
            } else {
                $details = ($probeError.Result -split '\r?\n' | Where-Object { $_.Trim() } | Select-Object -Last 1)
                if ($details.Length -gt 500) { $details = $details.Substring(0, 500) }
                Write-Warning "CUDA model session check failed: $details. Services will start for management and diagnostics."
            }
        } finally { $probe.Dispose() }
    }

    $env:PY_ALGO_URL = 'http://127.0.0.1:8090'
    $env:GO_PROXY_TARGET = 'http://127.0.0.1:8080'
    $env:PY_PROXY_TARGET = 'http://127.0.0.1:8090'
    $env:GO_API_PORT = '8080'
    $env:CONFIG_PATH = Join-Path $projectRoot 'backend-go\config.yaml'
    $env:DATA_DIR = Join-Path $projectRoot 'backend-go\data'
    $env:GOCACHE = Join-Path $projectRoot 'backend-go\.cache\go-build'
    $logDir = Join-Path $projectRoot 'data\logs'
    if (-not $CheckOnly) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

    $goCommand = if ($go) { "& $(Quote-PS $go) run ./cmd/server" } else { "& $(Quote-PS $server)" }
    $services = @(
        @{ Name = 'Python'; Port = 8090; Directory = 'backend-py'; Command = "& $(Quote-PS $python) -m uvicorn app.main:app --host 127.0.0.1 --port 8090"; ProcessNames = @('python.exe', 'pythonw.exe'); HealthUri = 'http://127.0.0.1:8090/'; HealthProperty = 'service'; HealthValue = 'change-detection-backend-py' },
        @{ Name = 'Go API'; Port = 8080; Directory = 'backend-go'; Command = $goCommand; ProcessNames = @('cdp-server.exe', 'server.exe', 'server', 'backend-go.exe'); HealthUri = 'http://127.0.0.1:8080/api/health'; HealthProperty = 'status'; HealthValue = 'ok' },
        @{ Name = 'Frontend'; Port = 5173; Directory = 'frontend'; Command = "& $(Quote-PS $node) ./node_modules/vite/bin/vite.js --host localhost --port 5173 --strictPort"; ProcessNames = @('node.exe') }
    )
    foreach ($service in $services) {
        $occupied = Test-Port $service.Port
        if ($occupied) {
            Assert-PortIdentity $service
            Write-Host "Port $($service.Port) already has an expected $($service.Name) process; keeping it running. Existing services may use an earlier configuration."
            continue
        }
        if ($CheckOnly) {
            Write-Host "$($service.Name): prerequisites OK; port $($service.Port) available."
            continue
        }
        $directory = Join-Path $projectRoot $service.Directory
        $shell = (Get-Command powershell.exe -ErrorAction Stop).Source
        $logName = $service.Name.Replace(' ', '-').ToLowerInvariant()
        $command = "Set-Location -LiteralPath $(Quote-PS $directory); " + $service.Command +
            " 1> $(Quote-PS (Join-Path $logDir "$logName.log")) 2> $(Quote-PS (Join-Path $logDir "$logName.error.log"))"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
        # A hidden CreateProcess child still inherits an automation host's Job
        # and can be killed when that host finishes. WMI launches independently;
        # BREAKAWAY also avoids inheriting the WMI provider's Job quotas.
        # Preserve the prepared CUDA/runtime environment without logging values.
        $serviceEnvironment = [string[]]@(
            [Environment]::GetEnvironmentVariables('Process').GetEnumerator() |
                ForEach-Object { '{0}={1}' -f $_.Key, $_.Value }
        )
        $startOptions = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{
            ShowWindow = [uint16]0
            CreateFlags = [uint32]0x01000000
            EnvironmentVariables = $serviceEnvironment
        }
        $process = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
            CommandLine = "`"$shell`" -NoProfile -EncodedCommand $encoded"
            CurrentDirectory = $directory
            ProcessStartupInformation = $startOptions
        }
        if ($process.ReturnValue -ne 0) {
            throw "Independent launch of $($service.Name) failed (Windows code $($process.ReturnValue)). Check the local Windows Management service."
        }
        Write-Host "Started $($service.Name) (independent launcher PID $($process.ProcessId)); logs: $logDir"
    }
    if ($CheckOnly) { exit 0 }

    $deadline = (Get-Date).AddSeconds(120)
    do {
        $pendingServices = @($services | Where-Object { -not (Test-Port $_.Port) })
        if ($pendingServices.Count -eq 0) { break }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    if ($pendingServices.Count -gt 0) {
        throw "Ports not ready: $(($pendingServices.Port) -join ', '). Check logs in $logDir."
    }
    $pyHealth = Invoke-RestMethod 'http://127.0.0.1:8090/' -TimeoutSec 15
    if ($pyHealth.service -ne 'change-detection-backend-py') { throw 'Port 8090 is not the expected Python service.' }
    $apiHealth = Invoke-RestMethod 'http://localhost:5173/api/health' -TimeoutSec 15
    if ($apiHealth.status -ne 'ok') { throw 'Frontend proxy cannot reach the Go API.' }
    $null = Invoke-RestMethod 'http://localhost:5173/api/algorithms' -TimeoutSec 15
    # Catalog discovery is best effort; a large source must not hold startup open.
    try {
        $catalog = Invoke-RestMethod 'http://localhost:5173/py/local/catalog' -TimeoutSec 15
        $imageCount = @($catalog.images | Where-Object { $null -ne $_ }).Count
        if ($imageCount -gt 0) { Write-Host "Imagery catalog returned $imageCount image(s)." }
        else { Write-Warning 'No imagery is indexed yet. Configure accessible sources in Management.' }
    } catch { Write-Warning 'Imagery catalog is unavailable or still indexing. Check sources in Management.' }
    Write-Host 'Services, frontend proxies and algorithms are ready. Open http://localhost:5173/'
    if (-not $gpuReady) { Write-Warning 'CUDA detection is not ready. Supply the model and verify the GPU runtime in Management.' }
    Write-Host "Service logs: $logDir"
} catch {
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
