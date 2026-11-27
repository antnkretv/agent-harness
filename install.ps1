# Установка agent-harness в репозиторий проекта на Windows: скачивает архив выпуска, сверяет sha256 и
# запускает ah init. Выпуск публикует этот файл с версией вместо __VERSION__:
#   irm https://github.com/antnkretv/agent-harness/releases/latest/download/install.ps1 | iex
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$version = if ($env:AGENT_HARNESS_VERSION) { $env:AGENT_HARNESS_VERSION } else { '__VERSION__' }
$releases = if ($env:AGENT_HARNESS_RELEASES) { $env:AGENT_HARNESS_RELEASES } else { 'https://github.com/antnkretv/agent-harness/releases/download' }
$name = "agent-harness-$version"

$root = git rev-parse --show-toplevel 2>$null
if (-not $root) { throw 'agent-harness: запустите команду в рабочей копии git-репозитория проекта' }

$python = $null
foreach ($candidate in @(@('py', '-3'), @('python'))) {
    if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) { continue }
    $arguments = @($candidate | Select-Object -Skip 1) + @('-c', 'import sys; sys.exit(sys.version_info < (3, 11))')
    & $candidate[0] @arguments 2>$null
    if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
}
if (-not $python) { throw 'agent-harness: нужен Python 3.11 или новее: py -3 и python не найдены или старше' }

$work = Join-Path ([IO.Path]::GetTempPath()) ("agent-harness-" + [Guid]::NewGuid())
New-Item -ItemType Directory -Path $work | Out-Null
try {
    $archive = Join-Path $work "$name.tar.gz"
    Invoke-WebRequest -UseBasicParsing -Uri "$releases/v$version/$name.tar.gz" -OutFile $archive
    $published = (Invoke-WebRequest -UseBasicParsing -Uri "$releases/v$version/$name.tar.gz.sha256").Content
    if ($published -is [byte[]]) { $published = [Text.Encoding]::UTF8.GetString($published) }
    $expected = ($published -split '\s+')[0].ToLowerInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 -Path $archive).Hash.ToLowerInvariant()
    if ($expected -ne $actual) { throw "agent-harness: sha256 архива $actual не совпал с опубликованным $expected" }
    tar -xzf $archive -C $work
    if ($LASTEXITCODE -ne 0) { throw 'agent-harness: не удалось распаковать архив: нужен tar из Windows 10 или новее' }
    $arguments = @($python | Select-Object -Skip 1) + @('-B', (Join-Path $work "$name\ah.py"), 'init', '--target', $root, '--archive', $archive)
    & $python[0] @arguments
    if ($LASTEXITCODE -ne 0) { throw "agent-harness: ah init завершился с кодом $LASTEXITCODE" }
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
