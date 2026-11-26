#Requires -Version 7.0
$ErrorActionPreference = 'SilentlyContinue'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $root) { exit 0 }

$messages = @{}
foreach ($registry in @((Join-Path $root '.harness/messages.env'), (Join-Path $root '.harness/project/messages.env'))) {
  if (-not (Test-Path $registry)) { continue }
  foreach ($match in [regex]::Matches((Get-Content -Raw -LiteralPath $registry), "(?m)^MSG_([a-z][a-z0-9_]*)='([^']*)'")) {
    $messages[$match.Groups[1].Value] = $match.Groups[2].Value
  }
}

function Get-PlatformMessage {
  param([Parameter(Mandatory = $true, Position = 0)][string]$Key, [Parameter(ValueFromRemainingArguments = $true)][object[]]$Values = @())
  $template = $messages[$Key]
  if ($null -eq $template) { return ((@("@$Key") + $Values) -join ' ') }
  $state = @{ Index = 0 }
  return [regex]::Replace($template, '%[s%]', {
    param($spec)
    if ($spec.Value -eq '%%') { return '%' }
    $value = if ($state.Index -lt $Values.Count) { [string]$Values[$state.Index] } else { '' }
    $state.Index++
    return $value
  })
}

$solution = ''
$manifest = Join-Path $root '.harness/project/manifest.json'
if (Test-Path $manifest) {
  try {
    $solution = (Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json).modules.backend.extensions.dotnet.solution
  } catch {
    [Console]::Error.WriteLine((Get-PlatformMessage cleanup_manifest_unparsed))
    exit 0
  }
}

if (-not $solution) {
  [Console]::Error.WriteLine((Get-PlatformMessage cleanup_no_solution))
  exit 0
}

$sln = Join-Path $root $solution
if (-not (Test-Path $sln)) {
  [Console]::Error.WriteLine((Get-PlatformMessage cleanup_solution_missing $solution))
  exit 0
}

$staged = @(git -C $root diff --cached --name-only --diff-filter=ACM) | Where-Object { $_ }

$formattable = $staged | Where-Object { $_ -match '\.(cs|csproj|props|targets)$' -or $_ -eq '.editorconfig' }
if (-not $formattable) { exit 0 }

dotnet format style $sln --verbosity quiet | Out-Null
dotnet format analyzers $sln --no-restore --verbosity quiet | Out-Null

Push-Location $root
dotnet tool restore | Out-Null
dotnet tool run csharpier format . | Out-Null
if ($LASTEXITCODE -ne 0) { [Console]::Error.WriteLine((Get-PlatformMessage cleanup_csharpier_failed)) }
Pop-Location

if ($staged) { git -C $root add -- $staged | Out-Null }
exit 0
