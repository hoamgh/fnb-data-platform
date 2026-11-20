#requires -Version 7.2
[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9_-]{1,60}$')][string]$RunName='analytics-01',
    [string]$GoldRelease='/demo/airflow/native-inputs/expanded-khanhhoi460-01/gold/releases/5d7915f7313e0da6dc10f1bb',
    [string]$GoldManifestSha='551521862c19be77e887a0ada9521716f95dffc54e3f7221d899eca475a89fe3',
    [string]$AnalyticsRelease='/business-input/releases/a833acfc5db7959f581354c1',
    [string]$AnalyticsManifestSha='c80eb1cfb0a531fad5aeaaff248590ad0af416964e88c82aa484e9f8b844ba59',
    [string]$DemographicsRelease='/area-input/releases/edd1b24eec37b96c9c6fc430',
    [string]$DemographicsManifestSha='8259fb49259ed2d8d1d98bf111822f8912dbf6b49534bda5e066beeca4160a3e',
    [ValidateRange(1024,65535)][int]$Port=18088
)
$ErrorActionPreference='Stop'
$repo=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $repo
$output=Join-Path $repo '.local/analytics'
$run=Join-Path $output ('run-'+$RunName)
New-Item -ItemType Directory -Force -Path $run | Out-Null
$checkpointPath=Join-Path $run 'checkpoint.json'
$compose=@('compose','-f','compose.analytics.yaml')
$oldPort=$env:FNB_SUPERSET_PORT
$env:FNB_SUPERSET_PORT=[string]$Port
$lock=$null
$state=$null
function Invoke-DockerCommand([string[]]$Arguments) {
    & docker @Arguments
    if($LASTEXITCODE -ne 0){throw "Docker command failed (exit $LASTEXITCODE); inspect this stage log"}
}
function Invoke-PythonCommand([string[]]$Arguments) {
    & python @Arguments
    if($LASTEXITCODE -ne 0){throw "Python command failed (exit $LASTEXITCODE); inspect this stage log"}
}
function Save-State {
    $temporary=$checkpointPath+'.tmp'
    $state | ConvertTo-Json -Depth 20 | Set-Content -Encoding utf8 -LiteralPath $temporary
    [IO.File]::Move($temporary,$checkpointPath,$true)
}
function Step([string]$Name,[scriptblock]$Action,[switch]$Always) {
    if(-not $Always -and $state.stages[$Name] -eq 'PASS'){Write-Host "$Name : checkpoint reused";return}
    $state.phase=$Name;$state.stages[$Name]='RUNNING';Save-State
    $log=Join-Path $run ($Name+'-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')+'.log')
    Write-Host "$Name : running; full log $log"
    try { & $Action *> $log; $state.stages[$Name]='PASS';Save-State }
    catch { $state.stages[$Name]='FAIL';$state.status='FAIL';Save-State;throw "Stage $Name failed; preserved checkpoint and full log: $log" }
}
try {
    $lock=[IO.File]::Open((Join-Path $output '.runner.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    $sources=@('src/fnb_platform/analytics/warehouse.py','scripts/load_analytics.py','scripts/bootstrap_analytics.py',
        'scripts/verify_superset.py','scripts/Invoke-AnalyticsWarehouse.ps1','compose.analytics.yaml')
    $sources+=@(Get-ChildItem -LiteralPath pipelines/dimensional_analytics,docker/analytics -Recurse -File | Where-Object { $_.Extension -in '.py','.sql','.yml','.zip' -or $_.Name -eq 'Dockerfile' } | ForEach-Object { $_.FullName })
    $hashes=@($sources | ForEach-Object { (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash })
    $identity=@{gold=$GoldRelease;gold_sha=$GoldManifestSha;analytics=$AnalyticsRelease;analytics_sha=$AnalyticsManifestSha;
        demographics=$DemographicsRelease;demographics_sha=$DemographicsManifestSha;port=$Port;code=$hashes}
    $bytes=[Text.Encoding]::UTF8.GetBytes(($identity | ConvertTo-Json -Depth 5 -Compress))
    $signature=[Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes))
    if(Test-Path -LiteralPath $checkpointPath){
        $loadedState=Get-Content -Raw -LiteralPath $checkpointPath | ConvertFrom-Json -AsHashtable
        if($loadedState.signature -ne $signature){throw 'Input/code changed. Preserve this checkpoint and use a new -RunName.'}
        $state=$loadedState
    } else {$state=@{signature=$signature;status='RUNNING';phase='PREFLIGHT';stages=@{}}}
    $state.status='RUNNING';Save-State
    $verificationPath=Join-Path $output 'verification.json'
    if(Test-Path -LiteralPath $verificationPath){
        Copy-Item -LiteralPath $verificationPath -Destination (Join-Path $run ('prior-verification-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')+'.json'))
    }
    @{status='RUNNING';run=$RunName;checkpoint=$checkpointPath} | ConvertTo-Json | Set-Content -Encoding utf8 -LiteralPath $verificationPath
    $buildArgs=@('--gold',$GoldRelease,'--analytics',$AnalyticsRelease,'--gold-sha',$GoldManifestSha,'--analytics-sha',$AnalyticsManifestSha,
        '--demographics',$DemographicsRelease,'--demographics-sha',$DemographicsManifestSha)
    Step 'LOCAL_PREFLIGHT' {
        Invoke-PythonCommand -Arguments @('-B','-c',"import importlib.util,pathlib; assert importlib.util.find_spec('playwright'); assert pathlib.Path('C:/Program Files/Google/Chrome/Application/chrome.exe').is_file(); print('Browser verification prerequisites available')")
        Invoke-DockerCommand -Arguments ($compose+@('ps','--status','running','-q','postgres'))
        $postgresId=(& docker @compose ps --status running -q postgres)
        if(-not $postgresId){throw 'Existing PostgreSQL must be running; no volume reset or fallback server is allowed'}
    } -Always
    Step 'SECRETS' { Invoke-PythonCommand -Arguments @('-B','scripts/init_analytics_secrets.py') } -Always
    $warehouseAvailable=& docker image ls --format '{{.Repository}}:{{.Tag}}' 'fnb/analytics:1.0.0'
    if(-not $warehouseAvailable){Step 'WAREHOUSE_IMAGE' { Invoke-DockerCommand -Arguments ($compose+@('build','analytics-build')) }}
    Step 'INPUT_ADMISSION' { Invoke-DockerCommand -Arguments ($compose+@('run','--rm','--no-deps','--pull','never','analytics-build')+$buildArgs+@('--check-only')) } -Always
    Step 'BOOTSTRAP_DATABASES' { Invoke-DockerCommand -Arguments ($compose+@('run','--rm','--no-deps','--pull','never','analytics-init')) }
    Step 'DIMENSIONAL_DBT' { Invoke-DockerCommand -Arguments ($compose+@('run','--rm','--no-deps','--pull','never','analytics-build')+$buildArgs) } -Always
    Step 'POSTGRES_REFRESH_ONE' { Invoke-DockerCommand -Arguments ($compose+@('run','--rm','--no-deps','--pull','never','analytics-load','--expected-gold',(Split-Path -Leaf $GoldRelease),'--result',('/analytics-out/run-'+$RunName+'/refresh-one.json'))) } -Always
    Step 'POSTGRES_REFRESH_TWO' { Invoke-DockerCommand -Arguments ($compose+@('run','--rm','--no-deps','--pull','never','analytics-load','--expected-gold',(Split-Path -Leaf $GoldRelease),'--result',('/analytics-out/run-'+$RunName+'/refresh-two.json'))) } -Always
    $one=Get-Content -Raw (Join-Path $run 'refresh-one.json') | ConvertFrom-Json -AsHashtable
    $two=Get-Content -Raw (Join-Path $run 'refresh-two.json') | ConvertFrom-Json -AsHashtable
    if($one.bundle_id -ne $two.bundle_id -or ($one.kpis | ConvertTo-Json -Compress) -ne ($two.kpis | ConvertTo-Json -Compress) -or
        ($one.market_reference | ConvertTo-Json -Compress) -ne ($two.market_reference | ConvertTo-Json -Compress)){throw 'Idempotency mismatch; do not publish BI'}
    $imageAvailable=& docker image ls --format '{{.Repository}}:{{.Tag}}' 'fnb/superset:6.0.0'
    if(-not $imageAvailable){$state.stages['SUPERSET_IMAGE']='MISSING'}
    if($imageAvailable){Step 'SUPERSET_IMAGE' { Write-Output 'Reusing installed fnb/superset:6.0.0; no image rebuild required' }}
    else {Step 'SUPERSET_IMAGE' { Invoke-DockerCommand -Arguments ($compose+@('build','superset')) }}
    Step 'SUPERSET_METADATA' { Invoke-DockerCommand -Arguments ($compose+@('run','--rm','--no-deps','superset-init')) } -Always
    Step 'SUPERSET_SERVICE' { Invoke-DockerCommand -Arguments ($compose+@('up','-d','--no-deps','--no-build','--wait','--wait-timeout','240','superset')) } -Always
    Step 'BI_BROWSER' { Invoke-PythonCommand -Arguments @('-B','scripts/verify_superset.py','--url',"http://127.0.0.1:$Port",'--output',$run) } -Always
    Step 'INPUTS_UNCHANGED' { Invoke-DockerCommand -Arguments ($compose+@('run','--rm','--no-deps','--pull','never','analytics-build')+$buildArgs+@('--check-only')) } -Always
    Step 'RESOURCE_SNAPSHOT' {
        $containerIds=@(& docker @compose ps -q postgres superset)
        & docker stats --no-stream --format '{{json .}}' @containerIds | Set-Content -Encoding utf8 (Join-Path $run 'container-resources.jsonl')
        if($LASTEXITCODE -ne 0){throw 'Container RAM sampling failed'}
    } -Always
    $pointer=Get-Content -Raw (Join-Path $output 'current.json') | ConvertFrom-Json
    $manifest=Get-Content -Raw (Join-Path $output ('releases/'+$pointer.bundle_id+'/manifest.json')) | ConvertFrom-Json -AsHashtable
    $bi=Get-Content -Raw (Join-Path $run 'superset-verification.json') | ConvertFrom-Json -AsHashtable
    if($bi.status -ne 'PASS'){throw 'Actual Superset/browser verification is not PASS'}
    $summary=@{status='PASS';gold_release_id=$manifest.gold_release_id;warehouse_bundle_id=$pointer.bundle_id;
        dbt=$manifest.dbt;postgres_kpis=$two.kpis;market_reference=$two.market_reference;refresh_twice='PASS';readonly_serving=$two.readonly_serving;
        query_timings=$two.query_timings;build_driver_peak_mib=$manifest.build_driver_peak_mib;dbt_child_peak_mib=$manifest.dbt_child_peak_mib;
        superset=$bi;resources=(Join-Path $run 'container-resources.jsonl');resource_scope='Sampled serving container RAM, shared PostgreSQL includes other platform workloads; not an isolated peak';
        input_integrity='PASS';native_analytics_run_is_not_airflow_dagrun=$true}
    $summary | ConvertTo-Json -Depth 20 | Set-Content -Encoding utf8 (Join-Path $output 'verification.json')
    $state.status='PASS';$state.phase='COMPLETE';Save-State
    Write-Host "PASS: $($bi.dashboard_url)"
    Write-Host "Evidence: $(Join-Path $output 'verification.json')"
} catch {
    if($state){
        $state.status='FAIL';Save-State
        @{status='FAIL';phase=$state.phase;run=$RunName;checkpoint=$checkpointPath} | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $output 'verification.json')
    }
    Write-Error $_
    exit 1
} finally {
    if($lock){$lock.Dispose()}
    $env:FNB_SUPERSET_PORT=$oldPort
}
