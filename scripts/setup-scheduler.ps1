# setup-scheduler.ps1 -- register the hourly venue-availability monitor
#
# Usage (a normal user shell is usually enough):
#   powershell -ExecutionPolicy Bypass -File setup-scheduler.ps1
# If it complains about permissions, re-run it from an Administrator shell.
#
# SELF-LOCATING: it looks for venue-check.bat next to itself, so the whole
# folder can live anywhere -- no path editing required.
#
# What it registers:
#   Task name : VenueBookingMonitor   (override with -TaskName)
#   Trigger   : every hour, 06:00 through 23:00  +  at logon
#   Action    : venue-check.bat  (which calls venue_monitor.py watch)
#
# Key settings:
#   -MultipleInstances IgnoreNew
#       If a run is still polling inside a watch window, the next hourly
#       trigger is skipped instead of stacking up.
#   -StartWhenAvailable
#       If the machine was off/asleep at the trigger time, run as soon as
#       it comes back -- so a missed 09:00 window is not silently lost.
#   -ExecutionTimeLimit 4h
#       Safety net so a stuck run cannot linger forever.
#   -LogonType Interactive
#       Runs under your logged-in session. No stored password needed.

[CmdletBinding()]
param(
    [ValidateSet('install', 'pause', 'resume', 'toggle', 'status', 'uninstall')]
    [string] $Action = 'install',
    [string] $TaskName = 'VenueBookingMonitor',
    [string] $Description = 'Monitor venue booking availability; push a notification when new slots appear.',
    [string] $StartAt = '06:00',
    [int]    $RepeatHours = 18
)

$ErrorActionPreference = 'Stop'

# ===========================================================================
#  非安装类操作：暂停 / 恢复 / 切换 / 查看 / 删除
#
#  放在最前面，因为这些操作不需要 venue-check.bat、也不需要读配置 ——
#  即使 skill 目录被挪动过，也应该还能把任务停下来。
#
#  默认 $Action 是 'install'，所以老用法
#      powershell -ExecutionPolicy Bypass -File scripts\setup-scheduler.ps1
#  行为完全不变（向后兼容）。
# ===========================================================================
if ($Action -ne 'install') {

    function Show-TaskSummary {
        param($Task, $Info)
        $on = [bool]$Task.Settings.Enabled
        Write-Host ""
        Write-Host ("  任务名    : " + $Task.TaskName)
        Write-Host ("  当前状态  : " + $Task.State + $(if ($on) { "  （启用中，会按时自动运行）" } else { "  （已暂停，不会自动运行）" }))
        Write-Host ("  上次运行  : " + $(if ($Info.LastRunTime) { $Info.LastRunTime } else { "（从未运行）" }))
        if ($on) {
            Write-Host ("  下次运行  : " + $(if ($Info.NextRunTime) { $Info.NextRunTime } else { "（无 —— 触发器可能已过期，建议重新注册）" }))
        } else {
            # 暂停后 NextRunTime 会残留一个旧值，这里明确说清楚，避免误判
            Write-Host  "  下次运行  : ——（已暂停，不会触发）"
        }
        Write-Host ""
    }

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host ""
        Write-Host "[!] 计划任务 '$TaskName' 不存在。" -ForegroundColor Yellow
        Write-Host "    需要先注册（注册后才会自动运行）：" -ForegroundColor Yellow
        Write-Host "      powershell -ExecutionPolicy Bypass -File scripts\setup-scheduler.ps1"
        Write-Host ""
        exit 4
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName

    switch ($Action) {

        'status' {
            Write-Host "计划任务状态"
            Show-TaskSummary $task $info
            exit 0
        }

        'pause' {
            if (-not $task.Settings.Enabled) {
                Write-Host ""
                Write-Host "[=] 已经是暂停状态，无需操作。" -ForegroundColor DarkGray
                Show-TaskSummary $task $info
                exit 0
            }
            Disable-ScheduledTask -TaskName $TaskName | Out-Null
            Write-Host ""
            Write-Host "[ok] 已暂停 —— 不会再自动运行。" -ForegroundColor Green
            Show-TaskSummary (Get-ScheduledTask -TaskName $TaskName) (Get-ScheduledTaskInfo -TaskName $TaskName)
            Write-Host "  恢复运行：再跑一次本脚本（不带参数），或用 -Action resume" -ForegroundColor Cyan
            Write-Host ""
            exit 0
        }

        'resume' {
            if ($task.Settings.Enabled) {
                Write-Host ""
                Write-Host "[=] 已经是启用状态，无需操作。" -ForegroundColor DarkGray
                Show-TaskSummary $task $info
                exit 0
            }
            Enable-ScheduledTask -TaskName $TaskName | Out-Null
            Write-Host ""
            Write-Host "[ok] 已恢复 —— 会按时自动运行。" -ForegroundColor Green
            Show-TaskSummary (Get-ScheduledTask -TaskName $TaskName) (Get-ScheduledTaskInfo -TaskName $TaskName)
            exit 0
        }

        'toggle' {
            # 一键切换：跑一次暂停，再跑一次恢复。
            $wasOn = [bool]$task.Settings.Enabled
            if ($wasOn) {
                Disable-ScheduledTask -TaskName $TaskName | Out-Null
            } else {
                Enable-ScheduledTask -TaskName $TaskName | Out-Null
            }
            Write-Host ""
            if ($wasOn) {
                Write-Host "[ok] 已暂停 —— 不会再自动运行。" -ForegroundColor Green
            } else {
                Write-Host "[ok] 已恢复 —— 会按时自动运行。" -ForegroundColor Green
            }
            Show-TaskSummary (Get-ScheduledTask -TaskName $TaskName) (Get-ScheduledTaskInfo -TaskName $TaskName)
            Write-Host "  （本脚本是「切换」：再运行一次就会变回另一种状态）" -ForegroundColor DarkGray
            Write-Host ""
            exit 0
        }

        'uninstall' {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host ""
            Write-Host "[ok] 已彻底删除计划任务 '$TaskName'。" -ForegroundColor Green
            Write-Host "     快照、日志、报告都还在；想恢复自动运行需重新注册：" -ForegroundColor Cyan
            Write-Host "       powershell -ExecutionPolicy Bypass -File scripts\setup-scheduler.ps1"
            Write-Host ""
            exit 0
        }
    }
}

# ===========================================================================
#  下面是原来的「注册 / 重装」流程（$Action = 'install'）
# ===========================================================================

# --- locate venue-check.bat next to this script (or one level up) ----------
$candidates = @(
    (Join-Path $PSScriptRoot 'venue-check.bat'),
    (Join-Path (Split-Path $PSScriptRoot -Parent) 'venue-check.bat')
)
$Bat = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $Bat) {
    Write-Host "[FATAL] venue-check.bat not found. Looked in:" -ForegroundColor Red
    $candidates | ForEach-Object { Write-Host "        $_" }
    Write-Host "        Keep venue-check.bat in the same folder as this script." -ForegroundColor Red
    exit 1
}
Write-Host "[info] action   : $Bat"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$backupXml = $null
if ($existing) {
    Write-Host "[info] removing existing task '$TaskName' ..."
    # 先把旧定义导出备份 —— 否则一旦新任务注册失败，
    # 就会出现「旧的删了、新的没建成」的空窗，监测彻底断掉。
    try {
        $backupXml = Export-ScheduledTask -TaskName $TaskName
        Write-Host "[info] 已备份现有任务定义（注册失败会自动还原）"
    } catch {
        Write-Host "[warn] 备份失败，继续：$($_.Exception.Message)" -ForegroundColor Yellow
        $backupXml = $null
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# ⚠️ 变量名不要用 $action —— 它和上面的 -Action 参数是同一个变量
#    （PowerShell 变量名不区分大小写），而 ValidateSet 会校验对该变量的**每一次赋值**：
#    `$action = New-ScheduledTaskAction ...` 赋进去的是个 CIM 对象，不匹配取值集合，
#    于是直接抛错、脚本静默终止。实测踩过：注册流程只输出到 [info] action 就没了。
$taskAction = New-ScheduledTaskAction -Execute $Bat -WorkingDirectory (Split-Path $Bat)

# --- 整点触发器 -----------------------------------------------------------
# ⚠️ 必须用 -Daily，**不能**用 -Once。
#    `-Once -At 06:00 -RepetitionDuration 18h` 建出来的是一次性触发器：
#    它的重复窗口只覆盖「当天 06:00–24:00」，第二天就再也不会触发，
#    NextRunTime 变成空 —— 表面上任务还在、状态是 Ready，实际已经死了。
#    这是个很难发现的静默失效，2026-09-17 就踩过一次。
#    正确做法：Daily 触发器（每天都会重新武装）+ 每天内部的整点重复。
$triggerHourly = New-ScheduledTaskTrigger -Daily -At $StartAt
$repetition = (New-ScheduledTaskTrigger -Once -At $StartAt `
        -RepetitionInterval (New-TimeSpan -Hours 1) `
        -RepetitionDuration (New-TimeSpan -Hours $RepeatHours)).Repetition
$triggerHourly.Repetition = $repetition

# --- 登录触发器 -----------------------------------------------------------
# 机器在放号窗口中途开机时，立刻接管，不用等下一个整点。
#
# Delay 是必须的：登录那一刻（尤其用 Windows Hello / PIN 登录）会话还在初始化，
# 此时起的交互式任务可能被会话切换连带杀掉 —— 日志里表现为 start 之后
# 紧跟一个 '^C'、没有 end 行。等 3 分钟让会话稳定下来。
#
# ⚠️ Delay 必须是 ISO 8601 时长字符串（'PT3M'）。
#    写成 New-TimeSpan -Minutes 3 会被序列化成 '00:03:00'，
#    任务 XML 校验直接报「格式不正确或超出范围的值」而注册失败。
$triggerLogon = $null
try {
    $triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $triggerLogon.Delay = 'PT3M'
    Write-Host "[info] logon trigger: 登录后延迟 3 分钟触发"
} catch {
    Write-Host "[warn] 建不出登录触发器，跳过：$($_.Exception.Message)" -ForegroundColor Yellow
    $triggerLogon = $null
}

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

# --- 注册（带降级兜底）----------------------------------------------------
# 先试「整点 + 登录」两个触发器；失败就退化为只有整点触发器。
# 整点触发器是命脉，绝不能因为登录触发器的小毛病而注册不上。
$attempts = @()
if ($triggerLogon) { $attempts += ,@($triggerHourly, $triggerLogon) }
$attempts += ,@($triggerHourly)

$registered = $false
foreach ($tl in $attempts) {
    try {
        Register-ScheduledTask -TaskName $TaskName `
            -Action $taskAction -Trigger $tl `
            -Settings $settings -Principal $principal `
            -Description $Description -ErrorAction Stop | Out-Null
        $registered = $true
        Write-Host "[ok] 已注册，触发器数量：$($tl.Count)"
        break
    } catch {
        Write-Host "[warn] 用 $($tl.Count) 个触发器注册失败：$($_.Exception.Message)" -ForegroundColor Yellow
    }
}

if (-not $registered) {
    Write-Host "[FATAL] 计划任务注册失败。" -ForegroundColor Red
    if ($backupXml) {
        try {
            Register-ScheduledTask -TaskName $TaskName -Xml $backupXml -Force | Out-Null
            Write-Host "[warn] 已还原注册前的任务定义，监测不会中断。" -ForegroundColor Yellow
        } catch {
            Write-Host "[FATAL] 还原也失败了：$($_.Exception.Message)" -ForegroundColor Red
        }
    }
    exit 1
}

$t = Get-ScheduledTask -TaskName $TaskName
$i = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "     name     : $($t.TaskName)"
Write-Host "     state    : $($t.State)"
Write-Host "     triggers : $($t.Triggers.Count)"
foreach ($tr in $t.Triggers) {
    Write-Host "       - $($tr.CimClass.CimClassName)  delay=$($tr.Delay)  interval=$($tr.Repetition.Interval)  duration=$($tr.Repetition.Duration)"
}

# NextRunTime 为空 = 触发器已经过期，任务再也不会自动跑。
# 这是最容易被忽略的静默失效，所以注册完必须当场检查。
Write-Host ""
if (-not $i.NextRunTime) {
    Write-Host "[FATAL] NextRunTime 是空的 —— 触发器不会再次触发！" -ForegroundColor Red
    Write-Host "        检查上面的 trigger 类型：整点触发器必须是 Daily，不能是 Once。" -ForegroundColor Red
    exit 1
}
Write-Host "next run : $($i.NextRunTime)" -ForegroundColor Green
Write-Host ""
Write-Host "Verify it works:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime,LastTaskResult,NextRunTime"
Write-Host "  (LastTaskResult 0 = success; 3221225786 = 0xC000013A = 被 Ctrl+C / 关机强制中断)"
Write-Host "  python scripts/venue_monitor.py status     # 引擎侧自检，能看出静默失效"
