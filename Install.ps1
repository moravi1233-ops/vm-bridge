[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:USERPROFILE 'AppData\Local\VMBridge'),
    [string]$Uv = 'uv',
    [switch]$Update,
    [switch]$InstallSkill,
    [string]$SkillDirectory
)
$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$version = [regex]::Match((Get-Content -LiteralPath (Join-Path $source 'pyproject.toml') -Raw), '(?m)^version\s*=\s*"([^"]+)"').Groups[1].Value
if (-not $version) { throw 'Could not read the package version.' }
$resolvedRoot = [IO.Path]::GetFullPath($InstallRoot)
$destination = Join-Path $resolvedRoot "versions\$version"
$codexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$skill = if ($SkillDirectory) { [IO.Path]::GetFullPath($SkillDirectory) } else { Join-Path $codexRoot 'skills\vm-control' }
$doSkill = $InstallSkill -or $SkillDirectory -or (Test-Path -LiteralPath $skill)
$metadataPath = Join-Path $resolvedRoot 'installation.json'
$prior = if (Test-Path -LiteralPath $metadataPath) { Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json } else { $null }
if ($prior -and -not $Update) { throw 'An installation exists. Use -Update to keep it as the previous version.' }
if ($doSkill -and (Test-Path -LiteralPath $skill) -and -not $Update) { throw 'vm-control already exists; use -Update for this installation.' }
if ($doSkill -and $Update -and (Test-Path -LiteralPath $skill)) {
    $skillInfo = Get-Content -LiteralPath (Join-Path $skill 'references\installation.json') -Raw | ConvertFrom-Json
    if (-not $prior -or [IO.Path]::GetFullPath($skillInfo.root) -ne $resolvedRoot) {
        throw 'The existing skill belongs to another installation; refusing replacement.'
    }
}
if (Test-Path -LiteralPath $destination) {
    throw "Version $version already exists at $destination. Close its sessions and use a new version for an update."
}
$uvCommand = Get-Command $Uv -ErrorAction Stop
New-Item -ItemType Directory -Path $destination -Force | Out-Null
foreach ($file in @('pyproject.toml','uv.lock','LICENSE','THIRD_PARTY_NOTICES.md','README.md')) {
    Copy-Item -LiteralPath (Join-Path $source $file) -Destination $destination
}
Copy-Item -LiteralPath (Join-Path $source 'src') -Destination $destination -Recurse
$oldEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $destination '.venv'
    & $uvCommand.Source sync --project $destination --locked --no-dev --python 3.13
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; inspect the new version folder.' }
} finally {
    if ($null -eq $oldEnvironment) { Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue }
    else { $env:UV_PROJECT_ENVIRONMENT = $oldEnvironment }
}
$python = Join-Path $destination '.venv\Scripts\python.exe'
$state = Join-Path $resolvedRoot 'state'
$metadata = @{version=$version;root=$resolvedRoot;python=$python;state=$state;source_checkout=$source;module='vm_bridge.cli';mcp_module='vm_bridge.mcp_server'}
$launcher = @'
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$BridgeArguments)
$info = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'installation.json') -Raw | ConvertFrom-Json
$previousHome = $env:VMBRIDGE_HOME
try {
    $env:VMBRIDGE_HOME = $info.state
    & $info.python -m vm_bridge.cli @BridgeArguments
    $bridgeExit = $LASTEXITCODE
} finally {
    if ($null -eq $previousHome) { Remove-Item Env:VMBRIDGE_HOME -ErrorAction SilentlyContinue }
    else { $env:VMBRIDGE_HOME = $previousHome }
}
exit $bridgeExit
'@
$previousHome = $env:VMBRIDGE_HOME
try {
    $env:VMBRIDGE_HOME = $state
    & $python -c 'from vm_bridge.storage import home; home()'
    if ($LASTEXITCODE -ne 0) { throw 'Private state initialization failed.' }
    & $python -m vm_bridge.cli discover
    if ($LASTEXITCODE -ne 0) { throw 'Installed engine verification failed.' }
    & $python -m vm_bridge.cli memory-metrics
    if ($LASTEXITCODE -ne 0) { throw 'Installed operational memory verification failed.' }
} finally {
    if ($null -eq $previousHome) { Remove-Item Env:VMBRIDGE_HOME -ErrorAction SilentlyContinue }
    else { $env:VMBRIDGE_HOME = $previousHome }
}
$releaseId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N')
$backup = Join-Path $state "release-backups\$releaseId"
New-Item -ItemType Directory -Path $backup -Force | Out-Null
if ($prior) { Copy-Item -LiteralPath $metadataPath -Destination (Join-Path $backup 'installation.json') }
$launcherPath = Join-Path $resolvedRoot 'vmbridge.ps1'
if (Test-Path -LiteralPath $launcherPath) { Copy-Item -LiteralPath $launcherPath -Destination (Join-Path $backup 'vmbridge.ps1') }
$stagedSkill = $null
if ($doSkill) {
    $stagedSkill = Join-Path (Split-Path $skill -Parent) ".vm-control-$releaseId"
    New-Item -ItemType Directory -Path (Split-Path $skill -Parent) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $source 'skill') -Destination $stagedSkill -Recurse
    $metadata | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stagedSkill 'references\installation.json') -Encoding UTF8
    if (Test-Path -LiteralPath $skill) { Copy-Item -LiteralPath $skill -Destination (Join-Path $backup 'skill') -Recurse }
}
# Backups and the old version remain intact. Replace only this known skill's files.
try {
    if ($doSkill) {
        New-Item -ItemType Directory -Path $skill -Force | Out-Null
        Copy-Item -Path (Join-Path $stagedSkill '*') -Destination $skill -Recurse -Force
    }
    $launcher | Set-Content -LiteralPath $launcherPath -Encoding UTF8
    $temporaryMetadata = Join-Path $resolvedRoot ".installation-$releaseId.json"
    $metadata | ConvertTo-Json | Set-Content -LiteralPath $temporaryMetadata -Encoding UTF8
    [IO.File]::Move($temporaryMetadata, $metadataPath, $true)
} catch {
    if ($prior) { Copy-Item -LiteralPath (Join-Path $backup 'installation.json') -Destination $metadataPath -Force }
    if (Test-Path -LiteralPath (Join-Path $backup 'skill')) {
        Copy-Item -Path (Join-Path $backup 'skill\*') -Destination $skill -Recurse -Force
    }
    if (Test-Path -LiteralPath (Join-Path $backup 'vmbridge.ps1')) {
        Copy-Item -LiteralPath (Join-Path $backup 'vmbridge.ps1') -Destination $launcherPath -Force
    }
    throw
} finally {
    if ($stagedSkill) {
        $resolvedStage = [IO.Path]::GetFullPath($stagedSkill)
        $expectedParent = [IO.Path]::GetFullPath((Split-Path $skill -Parent)) + [IO.Path]::DirectorySeparatorChar
        if (-not $resolvedStage.StartsWith($expectedParent, [StringComparison]::OrdinalIgnoreCase) -or
            (Split-Path $resolvedStage -Leaf) -ne ".vm-control-$releaseId") { throw 'Unexpected staging cleanup path' }
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
$mcpSnippet = @"
[mcp_servers.vm_bridge]
command = '$python'
args = ['-m', 'vm_bridge.mcp_server']
enabled = false
startup_timeout_sec = 15
tool_timeout_sec = 35

[mcp_servers.vm_bridge.env]
VMBRIDGE_HOME = '$state'
"@
$mcpSnippet | Set-Content -LiteralPath (Join-Path $resolvedRoot 'mcp-config.toml') -Encoding UTF8
$mcpJson = @{mcpServers=@{vm_bridge=@{command=$python;args=@('-m','vm_bridge.mcp_server');env=@{VMBRIDGE_HOME=$state}}}}
$mcpJson | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $resolvedRoot 'mcp-config.json') -Encoding UTF8
Write-Output "Installed VM Bridge $version in $resolvedRoot. Previous metadata/skill: $backup. No service or startup entry was created. Existing MCP configuration was not rewritten. Optional disabled snippet: $(Join-Path $resolvedRoot 'mcp-config.toml')"
