$argumentVariable = 'HARNESS_PYTHON_ARGV_' + [Guid]::NewGuid().ToString('N')
$previousOutputEncoding = [Console]::OutputEncoding
$previousPipelineEncoding = $global:OutputEncoding
$global:OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    [Environment]::SetEnvironmentVariable($argumentVariable, (ConvertTo-Json -InputObject @($args | ForEach-Object { [string]$_ }) -Compress), 'Process')
    [Console]::OutputEncoding = $global:OutputEncoding
    $program = "import json, os, subprocess, sys; sys.exit(subprocess.call([sys.executable] + json.loads(os.environ.pop('$argumentVariable'))))"
    $launcher = Join-Path $PSScriptRoot 'python.cmd'
    if ($MyInvocation.ExpectingInput) { $input | & $launcher -I -S -c $program }
    else { & $launcher -I -S -c $program }
    $code = $LASTEXITCODE
}
finally {
    Remove-Item -LiteralPath "Env:$argumentVariable" -ErrorAction SilentlyContinue
    [Console]::OutputEncoding = $previousOutputEncoding
    $global:OutputEncoding = $previousPipelineEncoding
}
exit $code
