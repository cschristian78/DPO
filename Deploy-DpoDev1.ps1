# Deploy Dynamic Portfolio Optimization to the local preprod site.
# Requires the IIS HttpPlatformHandler module and Python on PATH.

$ErrorActionPreference = 'Stop'
$source = 'E:\Development\DPO'
$target = 'C:\inetpub\wwwroot\DPO-Dev1'

if (-not (Test-Path -LiteralPath $source)) {
    throw "Source folder was not found: $source"
}
New-Item -ItemType Directory -Force -Path $target | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $target 'logs') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $target 'App_Data') | Out-Null

& robocopy $source $target /E /NFL /NDL /NJH /NJS /XD .git __pycache__ .venv venv App_Data logs /XF *.pyc
if ($LastExitCode -ge 8) {
    throw "robocopy failed with exit code $LastExitCode"
}

$python = Join-Path $target '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & py -3 -m venv (Join-Path $target '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
}
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $target 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }

Write-Host "Deployed to $target"
