param()
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $vswhere)) { throw 'Visual Studio C++ Build Tools are required.' }
$vcvars = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find 'VC\Auxiliary\Build\vcvars64.bat'
if (-not $vcvars) { throw 'Visual Studio x64 C++ tools were not found.' }
$build = Join-Path $root 'build'
New-Item -ItemType Directory -Path $build -Force | Out-Null
$source = Join-Path $root 'msrc_scan_native.cpp'
$output = Join-Path $root 'msrc_scan_native.exe'
$object = Join-Path $build 'msrc_scan_native.obj'
$response = Join-Path $build 'native.rsp'
$arguments = @('/nologo /std:c++17 /EHsc /utf-8 /O2', ('"' + $source + '"'),
    ('/Fe:"' + $output + '"'), ('/Fo:"' + $object + '"'), 'bcrypt.lib')
Set-Content -LiteralPath $response -Value $arguments -Encoding ascii
$batch = Join-Path $build 'build-native.cmd'
Set-Content -LiteralPath $batch -Encoding ascii -Value @('@echo off',
    ('call "' + $vcvars + '" >nul'), ('cd /d "' + $build + '"'), ('cl @"' + $response + '"'), 'exit /b %errorlevel%')
& $batch
if ($LASTEXITCODE) { throw "Native parser build failed: $LASTEXITCODE" }
Write-Output "Built $output"
