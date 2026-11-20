param([string]$SecretsDirectory="")
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$target = if($SecretsDirectory){$SecretsDirectory}else{Join-Path $root '.auth/fnb'}
[System.IO.Directory]::CreateDirectory($target) | Out-Null
foreach ($name in @('admin_password','crawler_password','airflow_password','fernet_key','webserver_key','ui_password')) {
    $path = Join-Path $target $name
    if (Test-Path -LiteralPath $path) { continue }
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $value = if ($name -eq 'fernet_key') { [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_') } else { [Convert]::ToHexString($bytes).ToLowerInvariant() }
    $stream = [System.IO.File]::Open($path,[System.IO.FileMode]::CreateNew)
    try { $data = [System.Text.Encoding]::UTF8.GetBytes($value); $stream.Write($data,0,$data.Length) } finally { $stream.Dispose() }
}
Write-Output 'F&B runtime secrets ready; existing files preserved.'
