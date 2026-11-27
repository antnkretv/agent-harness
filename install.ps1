# Установка agent-harness в репозиторий проекта на Windows: скачивает архив выпуска, сверяет sha256 и
# запускает ah init. Выпуск публикует этот файл с именем, версией и адресом комплекта вместо __NAME__,
# __VERSION__ и __REPOSITORY__:
#   irm https://github.com/antnkretv/agent-harness/releases/latest/download/install.ps1 | iex
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$name = if ($env:AGENT_HARNESS_NAME) { $env:AGENT_HARNESS_NAME } else { '__NAME__' }
$version = if ($env:AGENT_HARNESS_VERSION) { $env:AGENT_HARNESS_VERSION } else { '__VERSION__' }
$repository = if ($env:AGENT_HARNESS_REPOSITORY) { $env:AGENT_HARNESS_REPOSITORY } else { '__REPOSITORY__' }
$url = "$repository/releases/download/v$version/$name-$version.tar.gz"

$root = git rev-parse --show-toplevel 2>$null
if (-not $root) { throw 'agent-harness: запустите команду в рабочей копии git-репозитория проекта' }

$work = Join-Path ([IO.Path]::GetTempPath()) ("agent-harness-" + [Guid]::NewGuid())
New-Item -ItemType Directory -Path $work | Out-Null
try {
    $archive = Join-Path $work "$name-$version.tar.gz"
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 -Uri $url -OutFile $archive
    $published = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 60 -Uri "$url.sha256").Content
    if ($published -is [byte[]]) { $published = [Text.Encoding]::UTF8.GetString($published) }
    $expected = ($published -split '\s+')[0].ToLowerInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 -Path $archive).Hash.ToLowerInvariant()
    if ($expected -ne $actual) { throw "agent-harness: sha256 архива $actual не совпал с опубликованным $expected" }
    $unpacked = Join-Path $work 'kit'
    New-Item -ItemType Directory -Path $unpacked | Out-Null
    tar -xzf $archive -C $unpacked
    if ($LASTEXITCODE -ne 0) { throw 'agent-harness: не удалось распаковать архив: нужен tar из Windows 10 или новее' }
    $kit = Get-ChildItem -Directory -Path $unpacked | Select-Object -First 1
    if (-not $kit -or -not (Test-Path (Join-Path $kit.FullName 'ah.py'))) { throw 'agent-harness: в архиве нет каталога комплекта с ah.py' }
    $python = Join-Path $kit.FullName 'payload\.harness\hooks\python.cmd'
    & $python -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'agent-harness: нужен Python 3.11 или новее: интерпретатор, который выбирает .harness\hooks\python.cmd, не найден или старше' }
    & $python -B (Join-Path $kit.FullName 'ah.py') init --target $root --archive $archive
    if ($LASTEXITCODE -ne 0) { throw "agent-harness: ah init завершился с кодом $LASTEXITCODE" }
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
