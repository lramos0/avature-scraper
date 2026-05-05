param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$ParallelizeArgs
)

$ErrorActionPreference = "Stop"

function Stop-ParallelizeTree {
  <#
    Kill only the batch started from this shell: direct child processes whose command line
    mentions parallelize.py, plus their descendants (worker subprocesses).
    Avoids stopping unrelated parallel scrapes or other Python jobs.
  #>
  param([int] $ParentPid = $PID)
  $roots = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
      $_.ParentProcessId -eq $ParentPid -and
      $_.CommandLine -and
      $_.CommandLine -match 'parallelize\.py'
    })
  foreach ($r in $roots) {
    try {
      & taskkill.exe /PID $r.ProcessId /T /F 2>$null | Out-Null
    } catch {
      # Process may already have exited.
    }
  }
}

$script:cancelPressed = $false
$cancelHandler = {
  param($sender, $e)
  if ($script:cancelPressed) { return }
  $script:cancelPressed = $true
  $e.Cancel = $true
  Write-Warning "Ctrl+C: stopping parallelize and worker subprocesses..."
  Stop-ParallelizeTree -ParentPid $PID
}

try {
  Write-Host "Starting parallel scrape. Press Ctrl+C to stop all workers safely..." -ForegroundColor Cyan
  $repoRoot = Split-Path -Parent $PSScriptRoot
  if (-not (Test-Path (Join-Path $repoRoot "parallelize.py"))) {
    Write-Error "Could not find parallelize.py under repo root: $repoRoot"
    exit 2
  }
  Push-Location -LiteralPath $repoRoot
  $exitCode = 1
  try {
    $srcPath = Join-Path $repoRoot "src"
    if (Test-Path $srcPath) {
      if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
        $env:PYTHONPATH = $srcPath
      } else {
        $env:PYTHONPATH = "$srcPath;$env:PYTHONPATH"
      }
    }
    try {
      [Console]::CancelKeyPress += $cancelHandler
    } catch {
      # Non-interactive host (no console); parallelize.py still installs a SIGINT handler.
    }
    try {
      & py .\parallelize.py @ParallelizeArgs
      $exitCode = $LASTEXITCODE
    } finally {
      try {
        [Console]::CancelKeyPress -= $cancelHandler
      } catch {
      }
    }
  } finally {
    Pop-Location
    if ($script:cancelPressed) {
      Stop-ParallelizeTree -ParentPid $PID
      $exitCode = 130
    }
  }
  exit $exitCode
} catch {
  Write-Warning "Interrupted. Stopping parallelize process tree..."
  Stop-ParallelizeTree -ParentPid $PID
  exit 130
}
