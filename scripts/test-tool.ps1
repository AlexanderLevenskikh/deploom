$ErrorActionPreference = 'Stop'

$toolRoot = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $toolRoot 'run_tool_tests.py'

& python $runner --suite unit
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
