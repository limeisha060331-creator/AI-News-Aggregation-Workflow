<#
  install-task.ps1

  注册一个 Windows 计划任务，每天固定时间运行 run_daily.py。
  需要管理员 PowerShell。参数可调：
      -At 08:00        每天触发时间
      -PythonExe       解释器路径，默认 D:\anaconda\python.exe

  卸载：powershell -File .\install-task.ps1 -Remove
#>

param(
    [string]$At = '08:00',
    [string]$PythonExe = 'D:\anaconda\python.exe',
    [string]$TaskName = 'Dify-AI-News-Daily',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已删除计划任务 $TaskName"
    exit 0
}

if (-not (Test-Path $PythonExe)) {
    throw "找不到 Python 解释器：$PythonExe"
}

$scriptPath = Join-Path $PSScriptRoot 'run_daily.py'
if (-not (Test-Path $scriptPath)) {
    throw "找不到脚本：$scriptPath"
}

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument ('"{0}"' -f $scriptPath) `
    -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $At

# StartWhenAvailable：开机晚于触发时间时会尽快补跑，对应 PRD 里"机器关机就漏推"的风险
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'AI 资讯聚合日报：触发 Dify workflow 并维护跨天去重历史' `
    -Force | Out-Null

Write-Host "已注册计划任务：$TaskName"
Write-Host "  触发时间：每天 $At"
Write-Host "  执行命令：$PythonExe `"$scriptPath`""
Write-Host ''
Write-Host '手动试跑：'
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host '查看上次运行结果：'
Write-Host "  Get-ScheduledTaskInfo -TaskName $TaskName"
