#requires -Version 7.2
[CmdletBinding()]
param(
    [ValidatePattern('^[a-z0-9-]{1,30}$')][string]$RunName='final-01',
    [ValidateSet('Demo','Real','Both')][string]$Mode='Demo',
    [string]$InputVolume='fnb-platform_demo_outputs',
    [switch]$WithRuntime,
    [ValidateSet('','dimensional_analytics','business_analytics','gold_staging','area_enrichment')][string]$Docs='',
    [string]$GoldRelease='/demo/airflow/native-inputs/expanded-khanhhoi460-01/gold/releases/5d7915f7313e0da6dc10f1bb',
    [string]$GoldManifestSha='551521862c19be77e887a0ada9521716f95dffc54e3f7221d899eca475a89fe3',
    [string]$AnalyticsRelease='/business-input/releases/a833acfc5db7959f581354c1',
    [string]$AnalyticsManifestSha='c80eb1cfb0a531fad5aeaaff248590ad0af416964e88c82aa484e9f8b844ba59',
    [string]$DemographicsRelease='/area-input/releases/edd1b24eec37b96c9c6fc430',
    [string]$DemographicsManifestSha='8259fb49259ed2d8d1d98bf111822f8912dbf6b49534bda5e066beeca4160a3e',
    [ValidateRange(1024,65535)][int]$Port=18089,
    [ValidateRange(1024,65535)][int]$PostgresPort=15433,
    [ValidateRange(1024,65535)][int]$DocsPort=18081
)
$ErrorActionPreference='Stop'
$repo=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $repo
if($Mode -eq 'Both'){
    if($RunName.Length -gt 25){throw 'Both mode needs a run name of at most 25 characters'}
    & $PSCommandPath -Mode Demo -RunName ($RunName+'-demo') -Port ($Port+1) -PostgresPort ($PostgresPort+2) -DocsPort ($DocsPort+2)
    & $PSCommandPath -Mode Real -RunName $RunName -InputVolume $InputVolume -WithRuntime:$WithRuntime -Port $Port -PostgresPort $PostgresPort -DocsPort $DocsPort -GoldRelease $GoldRelease -GoldManifestSha $GoldManifestSha -AnalyticsRelease $AnalyticsRelease -AnalyticsManifestSha $AnalyticsManifestSha -DemographicsRelease $DemographicsRelease -DemographicsManifestSha $DemographicsManifestSha
    $root=Join-Path $repo '.local/duckdb-final'
    $demo=Get-Content -Raw -LiteralPath (Join-Path $root ($RunName+'-demo/verification.json'))|ConvertFrom-Json
    $real=Get-Content -Raw -LiteralPath (Join-Path $root ($RunName+'/verification.json'))|ConvertFrom-Json
    if($demo.status -ne 'PASS' -or $real.status -ne 'PASS'){throw 'Demo/Real verification is incomplete'}
    @{status='PASS';demo=$demo.status;real=$real.status;runtime=$real.airflow;commit='NOT_RUN';analytics_warehouse='DuckDB'}|ConvertTo-Json|Set-Content -Encoding utf8 -LiteralPath (Join-Path $root ($RunName+'/package-verification.json'))
    return
}
$run=Join-Path $repo ('.local/duckdb-final/'+$RunName)
$output=Join-Path $run 'warehouse'
$secrets=Join-Path $run 'secrets'
foreach($folder in @($run,$output,$secrets)){[IO.Directory]::CreateDirectory($folder)|Out-Null}
$project='fnb-duckdb-'+$RunName
$compose=@('compose','--project-name',$project,'-f','compose.analytics.yaml')
$names=@('FNB_ANALYTICS_OUTPUT','FNB_SECRETS_ROOT','FNB_POSTGRES_PORT','FNB_SUPERSET_PORT','FNB_DBT_DOCS_PORT','FNB_DEMO_VOLUME','FNB_DBT_DOCS_PROJECT','FNB_IMAGE_PREFIX','FNB_AIRFLOW_PORT','FNB_ALLOW_LIVE_CRAWL','FNB_ALLOW_DOWNLOADS')
$prior=@{};foreach($name in $names){$prior[$name]=[Environment]::GetEnvironmentVariable($name)}
$env:FNB_ANALYTICS_OUTPUT=$output;$env:FNB_SECRETS_ROOT=$secrets
$env:FNB_POSTGRES_PORT=[string]$PostgresPort;$env:FNB_SUPERSET_PORT=[string]$Port;$env:FNB_DBT_DOCS_PORT=[string]$DocsPort
$env:FNB_DBT_DOCS_PROJECT='dimensional_analytics'
$env:FNB_DEMO_VOLUME=if($Mode -eq 'Real'){$InputVolume}else{$project+'_demo_outputs'}
$env:FNB_IMAGE_PREFIX=$project
$env:FNB_ALLOW_LIVE_CRAWL='0';$env:FNB_ALLOW_DOWNLOADS='0'
$checkpoint=Join-Path $run 'checkpoint.json'
$state=@{status='RUNNING';mode=$Mode;stages=@{}}
$lock=$null
$checkpointAccepted=$false
function Invoke-DockerCommand([string[]]$Arguments){$executable=(Get-Command docker -CommandType Application -ErrorAction Stop|Select-Object -First 1).Source;& $executable @Arguments;if($LASTEXITCODE -ne 0){throw "Docker failed, exit $LASTEXITCODE; inspect stage log"}}
function Invoke-PythonCommand([string[]]$Arguments){$executable=(Get-Command python -CommandType Application -ErrorAction Stop|Select-Object -First 1).Source;& $executable @Arguments;if($LASTEXITCODE -ne 0){throw "Python failed, exit $LASTEXITCODE; inspect stage log"}}
function Save-State {$state|ConvertTo-Json -Depth 12|Set-Content -Encoding utf8 -LiteralPath ($checkpoint+'.tmp');[IO.File]::Move($checkpoint+'.tmp',$checkpoint,$true)}
function Step([string]$Name,[scriptblock]$Action,[switch]$Always){
    if(-not $Always -and $state.stages[$Name] -eq 'PASS'){Write-Host "$Name : checkpoint reused";return}
    $state.phase=$Name;$state.stages[$Name]='RUNNING';Save-State
    $log=Join-Path $run ($Name+'-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')+'.log')
    Write-Host "$Name : running; full log $log"
    try {& $Action *> $log;$state.stages[$Name]='PASS';Save-State}
    catch {$state.stages[$Name]='FAIL';Add-Content -Encoding utf8 -LiteralPath $log -Value ('ERROR: '+$_.Exception.Message);throw "Stage $Name failed; log/checkpoint preserved: $log"}
}
try {
    $lock=[IO.File]::Open((Join-Path $run '.runner.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    $code=@('compose.analytics.yaml','docker/analytics/Dockerfile','docker/analytics/serving.sql','docker/analytics/configure_superset.py','docker/analytics/market_overview.zip','scripts/Invoke-DuckDbPilot.ps1','scripts/dbt_docs.py','scripts/verify_superset.py','scripts/bootstrap_analytics.py')
    $code+=@(Get-ChildItem src/fnb_platform/analytics,pipelines/dimensional_analytics,pipelines/business_analytics -Recurse -File|Where-Object {$_.Extension -in '.py','.sql','.yml'}|ForEach-Object{$_.FullName})
    if($WithRuntime){
        $code+=@('compose.yaml','compose.host.yaml','docker/runtime/Dockerfile','docker/runtime/airflow_entry.py','docker/runtime/smoke.py','docker/postgres/init.sh','docker/postgres/migrations/001_crawler.sql','requirements/crawl-state.txt')
        $code+=@(Get-ChildItem src/fnb_platform,crawl_experiment,dags -Recurse -File|Where-Object{$_.Extension -eq '.py'}|ForEach-Object{$_.FullName})
    }
    $identity=[ordered]@{mode=$Mode;runtime=[bool]$WithRuntime;volume=$InputVolume;input_root=$env:FNB_INPUT_ROOT;inputs=@($GoldRelease,$GoldManifestSha,$AnalyticsRelease,$AnalyticsManifestSha,$DemographicsRelease,$DemographicsManifestSha);ports=@($Port,$PostgresPort,$DocsPort);hashes=@($code|Sort-Object -Unique|ForEach-Object{(Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash})}|ConvertTo-Json -Depth 5 -Compress
    $signature=[Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($identity)))
    if(Test-Path -LiteralPath $checkpoint){$loaded=Get-Content -Raw -LiteralPath $checkpoint|ConvertFrom-Json -AsHashtable;if(-not $Docs -and $loaded.signature -ne $signature){throw 'Code/options changed; preserve this run and choose a new -RunName'};$state=$loaded}
    if($Docs){
        $env:FNB_DBT_DOCS_PROJECT=$Docs
        $env:FNB_DEMO_VOLUME=if($state.mode -eq 'Real'){$state.input_volume}else{$project+'_demo_outputs'}
        Invoke-DockerCommand ($compose+@('up','-d','--no-deps','--no-build','--force-recreate','--wait','--wait-timeout','180','dbt-docs'))
        Write-Host "dbt Docs: http://127.0.0.1:$DocsPort"
        return
    }
    $checkpointAccepted=$true
    $state.signature=$signature;$state.input_volume=$InputVolume;$state.status='RUNNING';Save-State
    Step 'SECRETS' {Invoke-PythonCommand @('-B','scripts/init_analytics_secrets.py','--directory',$secrets);& ./scripts/Initialize-FnbRuntime.ps1 -SecretsDirectory $secrets} -Always
    Step 'CONFIG' {
        Invoke-DockerCommand ($compose+@('config','--quiet'))
        Invoke-PythonCommand @('-B','-c',"import importlib.util;assert importlib.util.find_spec('playwright');print('Browser prerequisite available')")
        $writers=& docker ps --filter "label=com.docker.compose.project=$project" --filter 'label=com.docker.compose.service=analytics-build' --filter status=running -q
        if($writers){throw 'An owned warehouse writer is still running. Preserve its checkpoint; do not start a second writer.'}
    } -Always
    Step 'BUILD_IMAGES' {Invoke-DockerCommand ($compose+@('build','analytics-build','superset-init'))}
    Step 'FOCUSED_TESTS' {
        Invoke-DockerCommand ($compose+@('run','--rm','--no-deps','--entrypoint','python','analytics-build','-B','-m','pytest','-q','/app/tests/test_dimensional_analytics.py','/app/tests/test_business_mvp.py','/app/tests/test_analytics_demographics.py','/app/tests/test_superset_bootstrap.py','/app/tests/test_superset_browser.py','/app/tests/test_duckdb_serving.py','--basetemp','/analytics-out/verification-fixtures','-o','cache_dir=/analytics-out/pytest-cache'))
    }
    $build=@('--demo','--serve')
    if($Mode -eq 'Real'){
        $build=@('--gold',$GoldRelease,'--gold-sha',$GoldManifestSha,'--analytics',$AnalyticsRelease,'--analytics-sha',$AnalyticsManifestSha,'--demographics',$DemographicsRelease,'--demographics-sha',$DemographicsManifestSha,'--serve')
        Step 'REAL_INPUT_ADMISSION' {Invoke-DockerCommand ($compose+@('run','--rm','--no-deps','analytics-build')+$build[0..($build.Length-2)]+@('--check-only'))} -Always
    }
    Step 'DBT_SNAPSHOT_ONE' {Invoke-DockerCommand ($compose+@('run','--rm','--no-deps','analytics-build')+$build)}
    $first=Get-Content -Raw -LiteralPath (Join-Path $output 'serving-current.json')|ConvertFrom-Json -AsHashtable
    Step 'METADATA_POSTGRES' {Invoke-DockerCommand ($compose+@('up','-d','--no-deps','--no-build','--wait','--wait-timeout','120','postgres'));Invoke-DockerCommand ($compose+@('run','--rm','--no-deps','analytics-init'))} -Always
    Step 'SUPERSET_IMPORT' {Invoke-DockerCommand ($compose+@('run','--rm','--no-deps','superset-init'))}
    Step 'SUPERSET_SERVICE' {Invoke-DockerCommand ($compose+@('up','-d','--no-deps','--no-build','--wait','--wait-timeout','180','superset'))} -Always
    Step 'BROWSER_ONE' {Invoke-PythonCommand @('-B','scripts/verify_superset.py','--url',"http://127.0.0.1:$Port",'--output',(Join-Path $run 'browser-one'))}
    Step 'CONCURRENT_REFRESH' {
        $job=Start-Job -ArgumentList @($compose,$build,(Join-Path $run 'refresh-two.log'),$repo) -ScriptBlock {param($prefix,$buildArgs,$log,$cwd);Set-Location -LiteralPath $cwd;& docker @prefix run --rm --no-deps analytics-build @buildArgs *> $log;if($LASTEXITCODE -ne 0){throw 'Second DuckDB build failed; inspect refresh-two.log'}}
        try {Invoke-PythonCommand @('-B','scripts/verify_superset.py','--url',"http://127.0.0.1:$Port",'--output',(Join-Path $run 'browser-during-refresh'))}
        finally {if(-not (Wait-Job -Job $job -Timeout 900)){throw 'Refresh timeout; preserved job/container and partial artifacts'};Receive-Job -Job $job -ErrorAction Stop;Remove-Job -Job $job}
        $second=Get-Content -Raw -LiteralPath (Join-Path $output 'serving-current.json')|ConvertFrom-Json -AsHashtable
        if(($first.kpis|ConvertTo-Json -Compress) -ne ($second.kpis|ConvertTo-Json -Compress)){throw 'Refresh changed logical KPI accounting'}
        Invoke-DockerCommand ($compose+@('run','--rm','--no-deps','--entrypoint','python','superset-init','-B','/workspace/docker/analytics/configure_superset.py'))
        Invoke-PythonCommand @('-B','scripts/verify_superset.py','--url',"http://127.0.0.1:$Port",'--output',(Join-Path $run 'browser-after-refresh'))
    }
    Step 'DBT_DOCS' {
        Invoke-DockerCommand ($compose+@('up','-d','--no-deps','--no-build','--wait','--wait-timeout','180','dbt-docs'))
        $manifest=Invoke-RestMethod "http://127.0.0.1:$DocsPort/manifest.json"
        $models=@($manifest.nodes.PSObject.Properties|Where-Object{$_.Name -like 'model.*'})
        if($models.Count -ne 7){throw 'Dimensional dbt Docs must retain its seven real models'}
        $null=Invoke-WebRequest "http://127.0.0.1:$DocsPort/"
    }
    if($WithRuntime){
        $env:FNB_POSTGRES_PORT=[string]($PostgresPort+1);$env:FNB_AIRFLOW_PORT='18082'
        $runtime=@('compose','--project-name',($project+'-runtime'),'-f','compose.yaml','-f','compose.host.yaml')
        Step 'RUNTIME_IMAGES' {Invoke-DockerCommand ($runtime+@('build','pipeline','dbt','crawler','airflow-init'))}
        Step 'RUNTIME_SMOKE' {Invoke-DockerCommand ($runtime+@('run','--rm','--no-deps','pipeline','python','docker/runtime/smoke.py','pipeline'));Invoke-DockerCommand ($runtime+@('run','--rm','--no-deps','crawler'))}
        Step 'AIRFLOW_UI' {
            Invoke-DockerCommand ($runtime+@('up','-d','--no-build','--wait','--wait-timeout','240','airflow-webserver','airflow-scheduler'))
            Invoke-DockerCommand ($runtime+@('exec','-T','airflow-scheduler','python','/opt/airflow/dags/verify_orchestration.py'))
            Invoke-DockerCommand ($runtime+@('exec','-T','airflow-scheduler','python','-c',"from airflow.models import DagBag;from airflow.serialization.serialized_objects import SerializedDAG;b=DagBag('/opt/airflow/dags',include_examples=False);assert not b.import_errors,b.import_errors;assert len(b.dags)==3;[SerializedDAG.to_dict(d) for d in b.dags.values()];print('THREE_DAGS_IMPORT_SERIALIZE_PASS')"))
            $null=Invoke-WebRequest 'http://127.0.0.1:18082/health'
        }
        $env:FNB_POSTGRES_PORT=[string]$PostgresPort
    }
    Step 'RESOURCE_SNAPSHOT' {$ids=@(& docker @compose ps -q postgres superset);& docker stats --no-stream --format '{{json .}}' @ids|Set-Content -Encoding utf8 -LiteralPath (Join-Path $run 'resources.jsonl');if($LASTEXITCODE -ne 0){throw 'Container resource sampling failed'}} -Always
    Step 'QUERY_TIMINGS' {Invoke-DockerCommand ($compose+@('run','--rm','--no-deps','--entrypoint','python','analytics-build','-B','-m','fnb_platform.analytics.duckdb_serving','--output','/analytics-out','--probe'))} -Always
    $state.status='PASS';Save-State
    @{status='PASS';mode=$Mode;project=$project;snapshot=$first;duckdb_refresh_twice='PASS';dashboard='20 charts / 3 tabs';browser_during_after_refresh='PASS';dbt_docs="http://127.0.0.1:$DocsPort";superset="http://127.0.0.1:$Port/superset/dashboard/hcmc-fnb-market-overview/";airflow=$(if($WithRuntime){'http://127.0.0.1:18082'}else{'NOT_VERIFIED'});analytics_warehouse='DuckDB';commit='NOT_RUN';resources=(Join-Path $run 'resources.jsonl')}|ConvertTo-Json -Depth 16|Set-Content -Encoding utf8 -LiteralPath (Join-Path $run 'verification.json')
    Write-Host "PASS: isolated verification artifacts: $run"
} catch {
    if($checkpointAccepted){
        $state.status='FAIL';Save-State
        @{status='FAIL';phase=$state.phase;checkpoint=$checkpoint;error=$_.Exception.Message}|ConvertTo-Json|Set-Content -Encoding utf8 -LiteralPath (Join-Path $run 'verification.json')
    }
    Write-Error $_;exit 1
} finally {
    if($lock){$lock.Dispose()}
    foreach($name in $names){[Environment]::SetEnvironmentVariable($name,$prior[$name])}
}
