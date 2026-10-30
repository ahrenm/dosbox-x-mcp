[CmdletBinding()]
param([string]$WorkDirectory)
$ErrorActionPreference = 'Stop'
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$executable = Join-Path $repositoryRoot 'bin\x64\Agent Debug SDL2\dosbox-x.exe'
if (!(Test-Path -LiteralPath $executable)) { throw 'Build Agent Debug SDL2|x64 first.' }
if ($WorkDirectory) {
    $workdir = (Resolve-Path -LiteralPath $WorkDirectory).Path
    if (!(Test-Path -LiteralPath $workdir -PathType Container)) { throw 'WorkDirectory must be a directory.' }
    $endpoint = '\\.\pipe\dosbox-mcp-local'
    $config = Join-Path $repositoryRoot 'tests\agent\mcp-local.env'
} else {
    $workdir = Join-Path $repositoryRoot 'tests\agent\runtime-mcp'
    & (Join-Path $repositoryRoot 'tests\agent\build_fixture.ps1') -RuntimeDirectory $workdir
    $endpoint = '\\.\pipe\dosbox-mcp-fixtures'
    $config = Join-Path $repositoryRoot 'tests\agent\mcp-test.env'
}
@"
transport=named_pipe
endpoint=$endpoint
dosbox_executable=$executable
dosbox_workdir=$workdir
profile=production
request_timeout_ms=5000
max_message_bytes=1048576
max_memory_read_bytes=65536
max_trace_events=10000
"@ | Set-Content -LiteralPath $config -Encoding utf8
Write-Output "Prepared $config"
