[CmdletBinding()]
param([string]$GoldRelease='/demo/airflow/native-inputs/expanded-khanhhoi460-01/gold/releases/5d7915f7313e0da6dc10f1bb')
$ErrorActionPreference='Stop'
$repo=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$output=Join-Path $repo '.local/m3/dashboard'
[IO.Directory]::CreateDirectory($output) | Out-Null
$log=Join-Path $repo ('.local/m3/build-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')+'.log')
$arguments=@('run','--rm','--pull','never','--network','none','--user','50000:0',
    '--entrypoint','/usr/local/bin/python','-e','PYTHONPATH=/app/src:/app',
    '-e','PYTHONDONTWRITEBYTECODE=1','--mount',
    'type=volume,source=fnb-platform_demo_outputs,target=/demo,readonly',
    '--mount',('type=bind,source='+$output+',target=/mvp'))
foreach ($entry in @('src','pipelines','config')) {
    $arguments+=@('--mount',('type=bind,source='+(Join-Path $repo $entry)+',target=/app/'+$entry+',readonly'))
}
$arguments+=@('fnb/analytics:1.0.0','-B','-m','fnb_platform.gold.business_mvp',
    '--gold-release',$GoldRelease,
    '--output','/mvp')
& docker @arguments *> $log
$code=$LASTEXITCODE
Get-Content -LiteralPath $log -Tail 3
Write-Host "Full build log: $log"
if ($code -ne 0) { throw "MVP build failed (exit $code); inspect retained log" }
Write-Host 'Business marts published. Use Superset for charts/dashboard.'
