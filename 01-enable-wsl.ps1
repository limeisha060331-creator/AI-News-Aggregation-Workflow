#Requires -RunAsAdministrator
<#
  01-enable-wsl.ps1

  作用：在 Windows 上启用 WSL2。
  Dify 官方部署依赖 Docker Desktop，Docker Desktop 在 Windows 上依赖 WSL2。

  用法：右键“以管理员身份运行 PowerShell”，cd 到本目录后执行
        powershell -ExecutionPolicy Bypass -File .\01-enable-wsl.ps1

  执行完成后必须重启电脑，然后再运行 02-install-docker-and-dify.ps1。
#>

$ErrorActionPreference = 'Stop'

function Assert-Admin {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '需要管理员权限。请以管理员身份重新打开 PowerShell 后再运行。'
    }
}

Assert-Admin

Write-Host ''
Write-Host '=== 01 启用 WSL2 ===' -ForegroundColor Cyan
Write-Host ''

# --- 1. 启用两个必需的可选功能 -------------------------------------------
# VirtualMachinePlatform 提供 WSL2 的虚拟化层；
# Microsoft-Windows-Subsystem-Linux 提供 Linux 子系统本体。
$features = @(
    'Microsoft-Windows-Subsystem-Linux',
    'VirtualMachinePlatform'
)

foreach ($feature in $features) {
    $state = (Get-WindowsOptionalFeature -Online -FeatureName $feature).State
    if ($state -eq 'Enabled') {
        Write-Host "[跳过] $feature 已启用"
        continue
    }
    Write-Host "[启用] $feature ..."
    Enable-WindowsOptionalFeature -Online -FeatureName $feature -All -NoRestart | Out-Null
}

# --- 2. 说明后续步骤 ------------------------------------------------------
# 可选功能刚启用、尚未重启时，wsl.exe 还不能工作，此时调用它必然报"未安装"。
# 所以 WSL2 内核安装与默认版本设置统一放到重启后的 02 脚本里做。
Write-Host ''
Write-Host '[提示] 功能已启用，但需要重启后 WSL 才能工作。'
Write-Host '       WSL2 内核安装与默认版本设置由 02 脚本在重启后完成。'

# --- 3. 结果汇总 ----------------------------------------------------------
Write-Host ''
Write-Host '--- 当前状态 ---'
foreach ($feature in $features) {
    $state = (Get-WindowsOptionalFeature -Online -FeatureName $feature).State
    Write-Host ("  {0,-40} {1}" -f $feature, $state)
}

Write-Host ''
Write-Host '下一步：' -ForegroundColor Green
Write-Host '  1. 重启电脑'
Write-Host '  2. 重启后以管理员身份运行 .\02-install-docker-and-dify.ps1'
Write-Host ''
