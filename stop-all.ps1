param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot

function Get-ServiceName($Process) {
    $command = [string]$Process.CommandLine
    if ($Process.Name -eq 'powershell.exe' -and
        $command -match '(?i)-EncodedCommand\s+"?([A-Za-z0-9+/=]+)') {
        try {
            $command = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1]))
        } catch { return $null }
        # Match the exact working directory used by start-all.ps1, including
        # the closing quote, so another checkout is never selected.
        foreach ($service in @(
            @{ Name = 'Python'; Directory = 'backend-py'; Pattern = '-m uvicorn app\.main:app' },
            @{ Name = 'Go API'; Directory = 'backend-go'; Pattern = 'run\s+\./cmd/server|\.tools[/\\]bin[/\\]cdp-server\.exe' },
            @{ Name = 'Frontend'; Directory = 'frontend'; Pattern = 'vite[/\\]bin[/\\]vite\.js' }
        )) {
            $directory = (Join-Path $projectRoot $service.Directory).Replace("'", "''")
            $location = "Set-Location -LiteralPath '$directory';"
            if ($command.IndexOf($location, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                $command -match $service.Pattern) { return $service.Name }
        }
    }
    # Also find Python if its launcher has exited, including venv forwarding
    # to a base interpreter, where ExecutablePath is outside the checkout.
    $python = Join-Path $projectRoot 'backend-py\.venv-local\Scripts\python.exe'
    if ($Process.Name -in @('python.exe', 'pythonw.exe') -and
        $command -match ('(?i)(?:^|["\s])' + [regex]::Escape($python) + '(?:"|\s)') -and
        $command -match '-m\s+uvicorn\s+app\.main:app') { return 'Python' }
    $server = Join-Path $projectRoot '.tools\bin\cdp-server.exe'
    if ($Process.Name -eq 'cdp-server.exe' -and
        [string]::Equals([string]$Process.ExecutablePath, $server, [StringComparison]::OrdinalIgnoreCase)) { return 'Go API' }
    return $null
}

try {
    $processes = @(Get-CimInstance Win32_Process)
    $byId = @{}
    $selected = @{}
    foreach ($process in $processes) {
        $byId[[int]$process.ProcessId] = $process
        if ($process.ProcessId -eq $PID) { continue }
        $serviceName = Get-ServiceName $process
        if ($serviceName) { $selected[[int]$process.ProcessId] = $serviceName }
    }

    # Use the outermost matching process; taskkill /T includes Go's temporary
    # server binary, Python workers, and active model subprocesses.
    $roots = @()
    foreach ($processId in $selected.Keys) {
        $parentId = [int]$byId[$processId].ParentProcessId
        $seen = @{}
        $hasSelectedParent = $false
        while ($byId.ContainsKey($parentId) -and -not $seen.ContainsKey($parentId)) {
            $seen[$parentId] = $true
            if ($selected.ContainsKey($parentId)) { $hasSelectedParent = $true; break }
            $parentId = [int]$byId[$parentId].ParentProcessId
        }
        if (-not $hasSelectedParent) { $roots += $processId }
    }

    if ($roots.Count -eq 0) { Write-Host 'No running services belonging to this project were found.' }
    foreach ($processId in $roots) {
        if ($CheckOnly) {
            Write-Host "Would stop $($selected[$processId]) process tree (PID $processId)."
            continue
        }
        # Check creation time before killing to guard against PID reuse.
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
        if (-not $current) { continue }
        if ($current.CreationDate -ne $byId[$processId].CreationDate) {
            throw "PID $processId was reused; run stop-all.cmd again."
        }
        $output = & "$env:SystemRoot\System32\taskkill.exe" /PID $processId /T /F 2>&1
        if ($LASTEXITCODE -ne 0 -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
            throw "Could not stop PID ${processId}: $output"
        }
        Write-Host "Stopped $($selected[$processId]) process tree (PID $processId)."
    }

    if (-not $CheckOnly) {
        $remaining = @(Get-CimInstance Win32_Process | Where-Object {
            $_.ProcessId -ne $PID -and (Get-ServiceName $_)
        })
        if ($remaining.Count -gt 0) { throw "Project processes are still running: $($remaining.ProcessId -join ', ')." }
    }
    if (-not $CheckOnly) {
        $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalPort -in @(5173, 8080, 8090) })
        foreach ($listener in $listeners) {
            Write-Host "Port $($listener.LocalPort) is still in use (PID $($listener.OwningProcess)); ownership was not verified, so it was left running." -ForegroundColor Yellow
        }
        Write-Host 'Project shutdown complete.'
    }
} catch {
    Write-Host "Shutdown failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
