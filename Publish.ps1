[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Message,
    [string]$Uv = 'uv',
    [switch]$Release
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Push-Location $root
$previousPath = $env:PATH
try {
    $uvCommand = (Get-Command $Uv -ErrorAction Stop).Source
    $env:PATH = (Split-Path $uvCommand -Parent) + [IO.Path]::PathSeparator + $env:PATH
    function Invoke-Checked {
        param([string]$Executable,[Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
        & $Executable @Arguments
        if ($LASTEXITCODE -ne 0) { throw "$Executable failed with exit code $LASTEXITCODE" }
    }
    $branch = (& git branch --show-current).Trim()
    if ($branch -ne 'main') { throw 'Publish from main after reviewing/merging the intended changes.' }
    if (-not (& git remote get-url origin)) { throw 'Configure the intended origin repository first.' }
    Invoke-Checked $uvCommand @('sync','--locked','--python','3.13')
    Invoke-Checked $uvCommand @('run','--locked','python','scripts/check_publication.py')
    Invoke-Checked $uvCommand @('run','--locked','pytest','-q','--junitxml=test-results/publish-tests.xml')
    Invoke-Checked $uvCommand @('build','--wheel','--sdist')
    Invoke-Checked 'git' @('config','core.hooksPath','.githooks')
    # Only project paths. Never stage the surrounding workspace or runtime state.
    $publicPaths = @('README.md','README.NL.md','LICENSE','THIRD_PARTY_NOTICES.md','CHANGELOG.md',
        'CONTRIBUTING.md','SECURITY.md','AGENTS.md','Install.ps1','Publish.ps1','pyproject.toml','uv.lock',
        '.gitignore','.gitattributes','.python-version','src','tests','scripts','skill','docs','.github','.githooks')
    $present = @($publicPaths | Where-Object { Test-Path -LiteralPath (Join-Path $root $_) })
    Invoke-Checked 'git' (@('add','--all','--') + $present)
    Invoke-Checked 'git' @('update-index','--chmod=+x','--','.githooks/pre-commit','.githooks/pre-push')
    Invoke-Checked $uvCommand @('run','--locked','python','scripts/check_publication.py','--staged')
    & git diff --cached --quiet
    if ($LASTEXITCODE -eq 1) { Invoke-Checked 'git' @('commit','-m',$Message) }
    elseif ($LASTEXITCODE -ne 0) { throw 'Could not inspect staged changes.' }
    Invoke-Checked 'git' @('push','--set-upstream','origin','main')
    $tag = $null
    if ($Release) {
        $version = [regex]::Match((Get-Content -LiteralPath pyproject.toml -Raw),'(?m)^version\s*=\s*"([^"]+)"').Groups[1].Value
        if (-not $version) { throw 'Missing package version.' }
        $tag = "v$version"
        & git show-ref --verify --quiet "refs/tags/$tag"
        if ($LASTEXITCODE -eq 0) { throw 'Release tag already exists; do not move published tags.' }
        Invoke-Checked 'git' @('tag','-a',$tag,'-m',"VM Bridge $version")
        Invoke-Checked 'git' @('push','origin',$tag)
    }
    New-Item -ItemType Directory -Path test-results -Force | Out-Null
    @{published=$true;commit=((& git rev-parse HEAD).Trim());tag=$tag;utc=[DateTime]::UtcNow.ToString('o')} |
        ConvertTo-Json | Set-Content -LiteralPath test-results/publication-result.json -Encoding UTF8
    Write-Output 'Source published. GitHub Actions must still complete the remote checks/release.'
} finally {
    $env:PATH = $previousPath
    Pop-Location
}
