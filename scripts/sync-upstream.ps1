<#
.SYNOPSIS
  同步上游 amane: main 前移到上游 tag, dev 合并 main, 重跑生成产物并跑本地检查.

.DESCRIPTION
  流程规定见仓库根目录 FORK.md. main 只做 fast-forward (纯上游对照), 自己的改动都在 dev 上.
  缺省用上游最新的 v* tag; 生成物 (web/openapi.json 与 web/src/client) 的冲突交给生成器重跑;
  然后跑 ruff / format / pyright / ty / pytest / pnpm check. 加 -Push 则推送 main 与 dev,
  并以 dev 为 ref 触发 CI 与镜像构建.

.PARAMETER Ref
  上游 ref (tag 或分支). 缺省取最新 v* tag.

.PARAMETER Push
  检查通过后推送 main 与 dev, 并触发 CI 与 Build my image.

.PARAMETER SkipChecks
  跳过本地检查 (只做同步与生成物重跑).

.PARAMETER NoTag
  不打同步 tag.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sync-upstream.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sync-upstream.ps1 -Ref v0.22.0 -Push
#>
[CmdletBinding()]
param(
    [string]$Ref,
    [switch]$Push,
    [switch]$SkipChecks,
    [switch]$NoTag
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$UpstreamUrl = 'https://github.com/sqzw-x/amane.git'
$MirrorBranch = 'main'
$WorkingBranch = 'dev'
# Windows 上与本 fork 无关的既有失败 (Proactor 事件循环差异); Ubuntu CI 上不失败.
$KnownFailures = @(
    'tests/playback/test_proxy.py::test_upstream_request_errors_do_not_leak_gate_permits'
)
$CheckDirs = @('src/amane', 'tests', 'scripts')

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Warn { param([string]$Message) Write-Host "!!  $Message" -ForegroundColor Yellow }
function Stop-Sync { param([string]$Message) Write-Host "xx  $Message" -ForegroundColor Red; exit 1 }

function Invoke-Native {
    param([string]$Label, [string]$Command, [string[]]$Arguments = @())
    Write-Step $Label
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { Stop-Sync "$Label 失败 (exit $LASTEXITCODE)" }
}

function Get-ForkSlug {
    $url = (& git remote get-url origin).Trim()
    if ($url -match 'github\.com[:/](?<owner>[^/]+)/(?<repo>[^/.]+)') {
        return "$($Matches.owner)/$($Matches.repo)"
    }
    return $null
}

function Get-GitHubToken {
    $payload = "protocol=https`nhost=github.com`n`n"
    $lines = $payload | & git credential fill 2>$null
    foreach ($line in $lines) {
        if ($line -like 'password=*') { return $line.Substring('password='.Length) }
    }
    return $null
}

function Start-RemoteWorkflow {
    param([string]$WorkflowFile)
    $slug = Get-ForkSlug
    if (-not $slug) { Write-Warn '取不到 origin 的 owner/repo; 请到 Actions 页面手动触发'; return }
    $token = Get-GitHubToken
    if (-not $token) { Write-Warn "拿不到 GitHub 凭据; 请到 Actions 页面手动触发 $WorkflowFile"; return }
    $uri = "https://api.github.com/repos/$slug/actions/workflows/$WorkflowFile/dispatches"
    $headers = @{
        Authorization = "Bearer $token"
        Accept        = 'application/vnd.github+json'
        'User-Agent'  = 'amane-sync'
    }
    try {
        Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -Body (@{ ref = $WorkingBranch } | ConvertTo-Json) -ContentType 'application/json' | Out-Null
        Write-Step "已触发 $WorkflowFile (ref=$WorkingBranch)"
    } catch {
        Write-Warn "触发 $WorkflowFile 失败: $($_.Exception.Message); 请到 Actions 页面手动触发"
    }
}

$repoRoot = (& git rev-parse --show-toplevel).Trim()
Set-Location $repoRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $uvFallback = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path $uvFallback) { Set-Alias -Name uv -Value $uvFallback -Scope Script }
    else { Write-Warn '未找到 uv; 生成产物与 Python 检查会失败' }
}

if ((git remote).Trim() -notcontains 'upstream') {
    Invoke-Native '添加 upstream 远端' git @('remote', 'add', 'upstream', $UpstreamUrl)
}

if ((git status --porcelain)) { Stop-Sync '工作树不干净, 先提交或 stash 再同步' }

Invoke-Native '取上游' git @('fetch', 'upstream', '--tags', '--prune')
Invoke-Native '取本 fork' git @('fetch', 'origin', '--prune')

if (-not $Ref) {
    $Ref = (git tag --list 'v[0-9]*' --sort=-v:refname | Select-Object -First 1)
}
if (-not $Ref) { Stop-Sync '没有可用的上游 tag; 用 -Ref 指定要同步的 ref' }

# 1) main 只前移, 保持纯上游.
Invoke-Native "切到 $MirrorBranch" git @('checkout', $MirrorBranch)
Invoke-Native "$MirrorBranch 前移到 $Ref" git @('merge', '--ff-only', $Ref)

# 2) dev 合并 main; 生成物冲突交给生成器.
Invoke-Native "切到 $WorkingBranch" git @('checkout', $WorkingBranch)
Write-Step "$WorkingBranch 合并 $MirrorBranch"
& git merge --no-edit $MirrorBranch
if ($LASTEXITCODE -ne 0) {
    $conflicts = @(git diff --name-only --diff-filter=U | Where-Object { $_ })
    if ($conflicts.Count -eq 0) { Stop-Sync '合并失败且不是文件冲突; 请手动查看' }
    $generated = @($conflicts | Where-Object { $_ -eq 'web/openapi.json' -or $_ -like 'web/src/client/*' })
    $others = @($conflicts | Where-Object { $generated -notcontains $_ })
    if ($others.Count -gt 0) { Stop-Sync ('需要人工处理的冲突: ' + ($others -join ', ')) }
    Write-Warn ('生成物冲突, 取上游版本后重跑生成器: ' + ($generated -join ', '))
    foreach ($path in $generated) {
        & git checkout --theirs -- $path
        & git add -- $path
    }
}

Invoke-Native '同步 Python 依赖' uv @('sync', '--frozen', '--all-extras', '--dev')
Invoke-Native '导出 openapi' uv @('run', 'python', 'scripts/export_openapi.py')
Push-Location (Join-Path $repoRoot 'web')
try {
    Invoke-Native '安装前端依赖' pnpm @('install', '--frozen-lockfile')
    Invoke-Native '生成前端客户端' pnpm @('gen-client')
} finally {
    Pop-Location
}

$mergeHead = (& git rev-parse --git-path MERGE_HEAD).Trim()
& git add -A
if (Test-Path $mergeHead) {
    Invoke-Native '提交合并' git @('commit', '--no-edit')
} elseif ((git status --porcelain)) {
    Invoke-Native '提交生成产物' git @('commit', '-m', "chore(web): $Ref 后重跑生成产物")
}

if (-not $SkipChecks) {
    Invoke-Native 'ruff check' uv (@('run', 'ruff', 'check') + $CheckDirs)
    Invoke-Native 'ruff format --check' uv (@('run', 'ruff', 'format', '--check') + $CheckDirs)
    Invoke-Native 'pyright' uv @('run', 'pyright')
    Invoke-Native 'ty check' uv @('run', 'ty', 'check')

    Write-Step 'pytest (全量)'
    $pytestOutput = (& uv run pytest tests/ -q -n auto --dist worksteal 2>&1 | Out-String)
    ($pytestOutput -split '\r?\n' | Select-String -Pattern 'passed|failed' | Select-Object -Last 2) | ForEach-Object { Write-Host $_.Line }

    $failed = @([regex]::Matches($pytestOutput, '(?m)^FAILED (\S+)') | ForEach-Object { $_.Groups[1].Value })
    $unexpected = @($failed | Where-Object { $KnownFailures -notcontains $_ })
    if ($unexpected.Count -gt 0) { Stop-Sync ('pytest 失败: ' + ($unexpected -join ', ')) }
    if ($failed.Count -gt 0) { Write-Warn ('pytest 只有已知的 Windows 失败, 继续: ' + ($failed -join ', ')) }

    Push-Location (Join-Path $repoRoot 'web')
    try { Invoke-Native '前端检查 (pnpm check)' pnpm @('check') } finally { Pop-Location }
}

$tagName = "sync/$(Get-Date -Format 'yyyy-MM-dd')-$Ref"
if (-not $NoTag) {
    if (-not (git tag --list $tagName)) { Invoke-Native "打同步 tag $tagName" git @('tag', $tagName) }
}

if (-not $Push) {
    Write-Step '未加 -Push: 停在本地'
    Write-Host "    检查通过后: git push origin $MirrorBranch $WorkingBranch, 再到 Actions 手动 Run workflow (CI / Build my image)"
    exit 0
}

Invoke-Native "推送 $MirrorBranch" git @('push', 'origin', $MirrorBranch)
Invoke-Native "推送 $WorkingBranch" git @('push', 'origin', $WorkingBranch)
if (-not $NoTag) { Invoke-Native '推送 tag' git @('push', 'origin', $tagName) }
Start-RemoteWorkflow 'ci.yaml'
Start-RemoteWorkflow 'build-mine.yml'
Write-Step '完成: 镜像构建跑完在 Debian 上 docker compose pull && docker compose up -d'
exit 0
