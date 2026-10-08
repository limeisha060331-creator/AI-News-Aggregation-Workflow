#Requires -RunAsAdministrator
<#
  02-install-docker-and-dify.ps1

  前置条件：
    - 已运行 01-enable-wsl.ps1 并**重启过电脑**
    - 本机可访问外网（要下载 Docker Desktop 与 Dify 镜像）

  作用：
    1. 校验并补装 WSL2（含内核；不安装 Linux 发行版）
    2. 安装 Docker Desktop（若未安装）
    3. 启动 Docker Desktop 并等待引擎就绪
    4. 克隆 Dify 到 D:\dify 并生成 docker/.env

  脚本不会自动 `docker compose up`——需要你先在 Docker Desktop 里把磁盘镜像
  位置改到 D 盘，否则镜像和容器数据会写进 C 盘。

  用法（管理员 PowerShell，cd 到本目录）：
        powershell -ExecutionPolicy Bypass -File .\02-install-docker-and-dify.ps1

  可选参数：
    -DifyDir  <路径>   Dify 克隆目标目录，默认 D:\dify
    -SkipInstall       已装好 Docker Desktop 时跳过安装步骤
#>

param(
    [string]$DifyDir = 'D:\dify',
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'

function Assert-Admin {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '需要管理员权限。请以管理员身份重新打开 PowerShell 后再运行。'
    }
}

function Test-DockerDesktopInstalled {
    $exe = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    return (Test-Path $exe)
}

function Wait-DockerEngine {
    param([int]$TimeoutSeconds = 300, [int]$IntervalSeconds = 5)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return $true }
        } catch { }
        Start-Sleep -Seconds $IntervalSeconds
    }
    return $false
}

Assert-Admin

Write-Host ''
Write-Host '=== 02 安装 Docker Desktop 并准备 Dify ===' -ForegroundColor Cyan
Write-Host ''

# --- 1. 校验 WSL ----------------------------------------------------------
Write-Host '[1/4] 校验 WSL2 ...'

# 用可选功能状态判断，比解析 wsl --status 的输出更可靠（不受系统语言影响）
$requiredFeatures = @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')
$missingFeatures  = @()
foreach ($feature in $requiredFeatures) {
    $state = (Get-WindowsOptionalFeature -Online -FeatureName $feature).State
    if ($state -ne 'Enabled') { $missingFeatures += $feature }
}
if ($missingFeatures.Count -gt 0) {
    throw ("以下功能尚未启用：{0}。请先运行 01-enable-wsl.ps1 并重启电脑。" -f ($missingFeatures -join ', '))
}

# wsl.exe 在未安装完成时会把提示写到 stderr，PowerShell 会把它当成错误记录，
# 配合 $ErrorActionPreference = 'Stop' 会直接中断脚本，所以这一段单独降级处理。
$previousEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'

Write-Host '      检查 WSL2 是否已就绪 ...'
$wslOutput = (& wsl.exe --status 2>&1 | Out-String)
$wslOk = ($LASTEXITCODE -eq 0)

if (-not $wslOk) {
    Write-Host '      尚未就绪，执行 wsl --install --no-distribution（只装内核，不装发行版）...'
    (& wsl.exe --install --no-distribution 2>&1 | Out-String).Trim() | Write-Host
    Start-Sleep -Seconds 5
    $wslOutput = (& wsl.exe --status 2>&1 | Out-String)
    $wslOk = ($LASTEXITCODE -eq 0)
}

if (-not $wslOk) {
    Write-Host '      仍未就绪，尝试 wsl --update ...'
    (& wsl.exe --update 2>&1 | Out-String).Trim() | Write-Host
    Start-Sleep -Seconds 5
    $wslOutput = (& wsl.exe --status 2>&1 | Out-String)
    $wslOk = ($LASTEXITCODE -eq 0)
}

if (-not $wslOk) {
    $ErrorActionPreference = $previousEap
    Write-Host $wslOutput
    throw 'WSL2 未就绪。请重启电脑后重跑本脚本；若重启后仍失败，请在管理员终端手动执行 wsl --install --no-distribution。'
}

(& wsl.exe --set-default-version 2 2>&1 | Out-Null)
$ErrorActionPreference = $previousEap
Write-Host '      WSL2 就绪'

# --- 2. 安装 Docker Desktop ----------------------------------------------
Write-Host ''
if (Test-DockerDesktopInstalled) {
    Write-Host '[2/4] Docker Desktop 已安装，跳过'
} elseif ($SkipInstall) {
    throw '指定了 -SkipInstall，但未找到 Docker Desktop。'
} else {
    Write-Host '[2/4] 安装 Docker Desktop ...'
    $installed = $false

    # 优先走 winget；不在 PATH 时回退到官方安装包
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host '      通过 winget 安装 ...'
        winget install --exact --id Docker.DockerDesktop `
            --accept-package-agreements --accept-source-agreements --silent
        if ($LASTEXITCODE -eq 0 -or (Test-DockerDesktopInstalled)) { $installed = $true }
    }

    if (-not $installed) {
        Write-Host '      回退到官方安装包 ...'
        $installer = Join-Path $env:TEMP 'DockerDesktopInstaller.exe'
        $url = 'https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe'
        Write-Host "      下载：$url"
        Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
        Write-Host '      静默安装（WSL2 后端，需几分钟）...'
        Start-Process -FilePath $installer `
            -ArgumentList 'install', '--quiet', '--accept-license', '--backend=wsl-2' `
            -Wait -WindowStyle Hidden
        $installed = Test-DockerDesktopInstalled
    }

    if (-not $installed) {
        throw 'Docker Desktop 安装未成功，请手动安装后重跑本脚本（可加 -SkipInstall）。'
    }
    Write-Host '      Docker Desktop 安装完成'
}

# --- 3. 启动 Docker 引擎 -------------------------------------------------
Write-Host ''
Write-Host '[3/4] 启动 Docker Desktop 并等待引擎就绪 ...'
$dockerExe = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
if (-not (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $dockerExe | Out-Null
}

if (Wait-DockerEngine -TimeoutSeconds 300) {
    $serverVersion = (docker info --format '{{.ServerVersion}}')
    Write-Host "      引擎就绪，ServerVersion = $serverVersion"

    # 用 hello-world 做一次端到端验证（需要拉取一个小镜像）
    Write-Host '      运行 hello-world 验证 ...'
    try {
        docker run --rm hello-world | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Host '      验证通过' -ForegroundColor Green }
        else { Write-Host '      hello-world 未通过，请检查 Docker Desktop 状态' -ForegroundColor Yellow }
    } catch {
        Write-Host '      hello-world 未通过（可能是网络问题）' -ForegroundColor Yellow
    }
} else {
    Write-Host '      等待超时。请手动打开 Docker Desktop，确认其状态为 Engine running 后重跑本脚本。' -ForegroundColor Yellow
}

# --- 4. 克隆 Dify --------------------------------------------------------
Write-Host ''
Write-Host '[4/4] 准备 Dify 代码 ...'

if (Test-Path (Join-Path $DifyDir '.git')) {
    Write-Host "      $DifyDir 已存在，执行 git pull ..."
    Push-Location $DifyDir
    try { git pull --ff-only } finally { Pop-Location }
} else {
    Write-Host "      clone 到 $DifyDir ..."
    git clone --depth 1 https://github.com/langgenius/dify.git $DifyDir
}

$dockerDir = Join-Path $DifyDir 'docker'
$envFile   = Join-Path $dockerDir '.env'
$sampleEnv = Join-Path $dockerDir '.env.example'

if (-not (Test-Path $envFile)) {
    Copy-Item $sampleEnv $envFile
    Write-Host '      已由 .env.example 生成 docker\.env'
} else {
    Write-Host '      docker\.env 已存在，保持不动'
}

# --- 收尾提示 -------------------------------------------------------------
Write-Host ''
Write-Host '=== 准备完成，接下来手动做三件事 ===' -ForegroundColor Green
Write-Host ''
Write-Host '1) 改 Docker 磁盘位置（重要，否则镜像会写进 C 盘）'
Write-Host '   Docker Desktop -> Settings -> Resources -> Advanced'
Write-Host '   -> Disk image location 改为  D:\DockerData'
Write-Host '   Apply & restart 后继续下一步'
Write-Host ''
Write-Host '2) 启动 Dify（普通 PowerShell 即可）'
Write-Host "   cd $dockerDir"
Write-Host '   docker compose up -d'
Write-Host '   首次启动要拉取约 7-8 GB 镜像，视网络需要 5-20 分钟'
Write-Host ''
Write-Host '3) 打开初始化页面并创建管理员账号'
Write-Host '   http://localhost/install'
Write-Host '   （若 80 端口被占用，改 docker\.env 里的 EXPOSE_NGINX_PORT）'
Write-Host ''
Write-Host '查看状态：docker compose ps'
Write-Host '查看日志：docker compose logs -f api'
Write-Host ''
