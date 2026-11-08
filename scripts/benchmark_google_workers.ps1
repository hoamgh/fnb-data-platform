param(
    [string]$Catalog = 'output/overture-20-20261027T043433580934Z/canonical_places.parquet',
    [int]$Limit = 20,
    [int[]]$WorkerCounts = @(1,2,3,4),
    [int]$PerStoreSeconds = 180,
    [int]$MaxScrolls = 50,
    [int]$TotalSeconds = 5400,
    [string]$Python = 'C:\Users\MSI\anaconda3\python.exe',
    [string]$DependencyRoot = 'D:\Projects\integration-recomentdation\output\ci01-2ac84b90\venv\Lib\site-packages'
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:PYTHONPATH = $DependencyRoot
$env:PYTHONIOENCODING = 'utf-8'
if (-not (Test-Path -LiteralPath $Catalog -PathType Leaf)) { throw 'Catalog missing' }
if ($WorkerCounts.Count -eq 0 -or @($WorkerCounts | Where-Object { $_ -lt 1 -or $_ -gt $Limit }).Count) {
    throw 'Worker counts must be between one and the selected store count'
}
$maximumWorkers = ($WorkerCounts | Measure-Object -Maximum).Maximum
$profiles = @('output/google-crawler-login')
for ($i=2; $i -le $maximumWorkers; $i++) {
    $profiles += ('output/google-crawler-login-{0:D2}' -f $i)
}
foreach ($profile in $profiles) {
    if (-not (Test-Path (Join-Path $profile 'Local State')) -or
        -not (Test-Path (Join-Path $profile 'Default/Preferences'))) {
        throw "Prepare dedicated profile first, then close Chrome: $profile"
    }
}
# Short paths also avoid the Windows path-length limit in Crawlee storage.
$benchRoot = 'output/b-' + [guid]::NewGuid().ToString('N').Substring(0,8)
New-Item -ItemType Directory -Path $benchRoot | Out-Null
Write-Host "Benchmark output: $PWD\$benchRoot"
try {
    foreach ($workers in $WorkerCounts) {
        $runOutput = Join-Path $benchRoot "w$workers"
        if (Test-Path -LiteralPath $runOutput) { throw 'Duplicate trial/output refused' }
        $crawlArgs = @('-B','-u','-m','scripts.google_live_workers','--operator-submit',
            '--review-source','dom','--catalog',$Catalog,'--output',$runOutput,
            '--limit',"$Limit",'--workers',"$workers",'--per-store-seconds',"$PerStoreSeconds",
            '--max-scrolls',"$MaxScrolls",'--total-seconds',"$TotalSeconds")
        for ($i=0; $i -lt $workers; $i++) { $crawlArgs += @('--profile-dir',$profiles[$i]) }
        Write-Host "`n===== DYNAMIC QUEUE: $workers WORKER ====="
        $trialClock = [System.Diagnostics.Stopwatch]::StartNew()
        & $Python @crawlArgs
        $code = $LASTEXITCODE
        $trialClock.Stop()
        $invocationRecord = [pscustomobject]@{
            Workers=$workers; ProcessWallSeconds=$trialClock.Elapsed.TotalSeconds
            ExitCode=$code; Limit=$Limit; PerStoreSeconds=$PerStoreSeconds
            MaxScrolls=$MaxScrolls; TotalSeconds=$TotalSeconds
            Semantics='Whole Python invocation including imports, preflight, collection, cleanup and summary writing'
        }
        if (Test-Path -LiteralPath $runOutput -PathType Container) {
            $invocationRecord | ConvertTo-Json | Set-Content (Join-Path $runOutput 'invocation.json') -Encoding utf8
        }
        $summaryPath = Join-Path $runOutput 'summary.json'
        if (-not (Test-Path -LiteralPath $summaryPath)) { throw "Exit=$code; no fresh summary: $runOutput" }
        $s = Get-Content -LiteralPath $summaryPath -Raw | ConvertFrom-Json
        if ($code -notin @(0,1) -or $s.source_stopped -or -not $s.cleanup_verified -or
            $s.pending -gt 0 -or @($s.reconciliation_errors).Count -gt 0 -or
            -not $s.benchmark.measurement_complete -or $s.parquet_rows -le 0) {
            Write-Warning "Incomplete/unsafe trial; no further trial. Inspect $summaryPath"
            break
        }
    }
}
finally {
    $comparison = @(foreach ($workers in $WorkerCounts) {
        $file = Join-Path $benchRoot "w$workers/summary.json"
        if (Test-Path -LiteralPath $file) {
            $s = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
            $invocationFile = Join-Path $benchRoot "w$workers/invocation.json"
            $invocation = if (Test-Path -LiteralPath $invocationFile) {
                Get-Content -LiteralPath $invocationFile -Raw | ConvertFrom-Json
            } else { $null }
            [pscustomobject]@{
                Workers=$s.workers; CompleteTrial=$s.benchmark.measurement_complete
                ProcessWallSeconds=$invocation.ProcessWallSeconds
                Seconds=[math]::Round($s.elapsed_seconds,1)
                SQLiteRows=$s.sqlite_records; ParquetRows=$s.parquet_rows; Pending=$s.pending
                ReviewsPerMin=[math]::Round($s.benchmark.published_events_per_minute,1)
                StoreP50Sec=$s.benchmark.store_p50_seconds; StoreP95Sec=$s.benchmark.store_p95_seconds
                FullStores=$s.full_store_count; PartialStores=$s.partial_store_count; UnknownStores=$s.unknown_store_count
                PeakRAM_GiB=[math]::Round($s.peak_owned_rss_bytes / 1GB,2)
                CPUCoreLowerBound=$s.benchmark.mean_busy_cores_lower_bound
                CleanupOK=$s.cleanup_verified
            }
        }
    })
    $comparison | Format-Table -AutoSize
    $comparison | Export-Csv (Join-Path $benchRoot 'comparison.csv') -NoTypeInformation -Encoding utf8
    Write-Host "Results: $PWD\$benchRoot"
}
