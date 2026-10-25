# 系统级调度（不依赖 WorkBuddy 客户端）

## 一、为什么需要它

引擎本身是**纯被动**的：`notify()` 的唯一调用点在 `check` 里，它没有任何自主调度能力。
**谁来「按时跑 check」，谁就决定了提醒的可靠性。**

| | WorkBuddy 自动化 | 系统级调度（本文方案） |
| --- | --- | --- |
| 依赖 | 客户端必须处于运行状态 | 不依赖任何客户端 |
| 触发方式 | 客户端定时唤起一个 agent 会话 | Windows 任务计划程序 |
| 单次开销 | 起一个完整 agent 会话 | 只起一个 python 进程 |
| 适合场景 | 临时 / 调试 / 需要 agent 判断 | 长期挂着无人值守 |

2026-09-16 已从前者切到后者。**两者不要同时开** —— 会重复发请求，
虽然增量比对能防止重复推送，但纯属浪费，且日志会互相干扰。

## 二、装了哪些东西

| 文件 | 作用 |
| --- | --- |
| `scripts/venue-check.bat` | 任务计划程序实际调用的入口（内含路径与日志重定向） |
| `scripts/setup-scheduler.ps1` | 一键注册 / 重装计划任务 |
| `venue_monitor.py watch` | **调度决策逻辑**：窗口内加密轮询，窗口外只跑一次 |

已注册的计划任务：

```
名称   VenueBookingMonitor
触发1  每天 06:00 起，每小时一次，共 18 次（06:00–23:00）   ← 必须是 Daily，不能是 Once
触发2  登录后延迟 3 分钟触发
动作   scripts\venue-check.bat
设置   MultipleInstances=IgnoreNew   上次还在轮询就跳过本次触发
       StartWhenAvailable=True       关机/休眠错过的触发，开机后补跑
       ExecutionTimeLimit=4h         防止卡死
       WakeToRun=False               不唤醒睡眠中的电脑（见下节）
       登录类型=Interactive           用当前登录会话跑，无需存密码
```

### ⚠️ 两个必须知道的坑（都实际踩过）

**坑 1：整点触发器用 `-Once` 建，第二天就失效。**

```powershell
# ✗ 错的：这是一次性触发器，重复窗口只覆盖「当天 06:00–24:00」
New-ScheduledTaskTrigger -Once -At 06:00 `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Hours 18)

# ✓ 对的：Daily 触发器每天重新武装 + 每天内部的整点重复
$t = New-ScheduledTaskTrigger -Daily -At 06:00
$t.Repetition = (New-ScheduledTaskTrigger -Once -At 06:00 `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Hours 18)).Repetition
```

症状极隐蔽：任务**状态仍是 Ready、看不出任何异常**，但 `NextRunTime` 变成空，
于是从第二天起再也不触发。2026-09-17 实测踩到：09-16 装的任务当天一切正常，
09-17 一整天只在登录时跑了一次。

`setup-scheduler.ps1` 现在会在注册后**当场检查 `NextRunTime`**，为空就直接报错退出，
不会再让你带着一个死任务离开。

**坑 2：`.ps1` 里写中文注释，必须给文件加 UTF-8 BOM。**

Windows PowerShell 5.1 读**无 BOM** 的文件时按系统 ANSI 代码页解码（中文机器上是 GBK）。
UTF-8 的中文注释被当成 GBK 读，产生的悬空前导字节会**吞掉后面的引号甚至换行符**，
结果是字符串被当成代码解析。报错长这样：

```
所在位置 setup-scheduler.ps1:96 字符: 14
+ Write-Host "[ok] task registered."
+              ~
数组索引表达式无效。
```

注意**行号完全对不上**（PowerShell 数出 118 行，实际 130 行）—— 这就是被吞掉换行符的证据。
排查这种错先怀疑编码，别去改代码。

对照规则：

| 文件 | 编码要求 | 原因 |
| --- | --- | --- |
| `*.ps1` | **UTF-8 with BOM** | 无 BOM 会被按 GBK 误读 |
| `*.bat` | **纯 ASCII，且不能有 BOM** | cmd 用 OEM 代码页，BOM 会被当命令执行 |
| `*.py` | UTF-8（无需 BOM） | Python 3 默认 UTF-8 |

`make-share-package.py` 会在打包时校验这两条，不符合就拒绝出包。

## 二·五、电脑关机 / 睡眠会怎样

计划任务有一条硬约束：**电脑得在运行状态**。这是硬件层面的限制，软件解决不了。

| 电脑状态 | 到点能跑吗 |
| --- | --- |
| 开机（含锁屏） | ✅ 能 |
| 睡眠 / 休眠 | ⚠️ 只有当 `WakeToRun=True` 时才能被唤醒；当前是 `False` |
| 完全关机 | ❌ 不能，且无法唤醒 |

**当前部署的实际行为**：如果晚上关机，第二天开机后会跑一次 `check`。
能不能给出提醒，取决于那时空位还在不在 —— 按实测，放号后 1 小时内会被抢走 50%+，
所以开机太晚基本就错过了。

**「登录时触发」这条就是为了减少这种损失**：它让开机登录后**立刻**跑，而不是等系统
「尽快」补跑（后者可能漂移到下一个整点）。而且如果开机时刻恰好落在轮询窗口内
（例如 09:15），`watch` 会直接接管并盯到 10:30，覆盖窗口的剩余部分，
而不是只拍一张快照。

**如果想让关机也不漏**，只有三条路：

1. **改成睡眠**，并把 `WakeToRun` 打开 —— 到点自动唤醒执行。这是最省事的。
2. **BIOS 定时开机（RTC Alarm）** —— 在主板里设每天固定时间自动开机，需进 BIOS，
   且不是所有主板都支持。
3. **放到常开的机器上**（NAS / 云服务器 / 树莓派），把 `watch` 挂到 cron。
   注意云服务器跑的话，抓包得到的请求头（含 `Referer`、UA）要一起带过去。

## 三、调度逻辑在 `watch` 里，不在提示词里

这是本次迁移的关键：原来「窗口内加密轮询」是写在自动化提示词里、由 agent 执行的；
搬到系统层后必须落到代码里，否则计划任务每次只会跑一次 check，丢掉加密轮询能力。

`watch` 的决策表（配置见 `venues.json` 的 `schedule` 段）：

| 当前时刻 | 动作 | 说明 |
| --- | --- | --- |
| 早于 `quiet_before`（06:00） | `quiet` | 不发任何请求，直接退出 |
| 落在 `poll_windows` 内 | `poll` | 每 `poll_interval` 秒跑一次，**盯到窗口结束** |
| 距下一窗口 ≤ `lead_minutes` | `wait` | 先跑一次 → 等到窗口开始 → 再轮询 |
| 其余时间 | `once` | 只跑一次就退出 |

为什么需要 `wait`：计划任务只能整点触发，而窗口是 08:50 开始。
若没有 `wait`，08:00 那次跑完就退出，**08:00–08:50 会出现 50 分钟空档** ——
而放号后正是被抢最猛的时候。`lead_minutes=60` 让 08:00 那次直接等到 08:50 再开始轮询。

`poll_windows` 的两个窗口及其依据：

| 窗口 | 为什么 |
| --- | --- |
| 08:50–10:30 | **放号窗口**。实测每天 09:30 前后放出次日场次，且放号后 1 小时内被抢走 50%+ |
| 13:50–15:40 | **退票窗口**。开赛前 6 小时截止退款，20:00–22:00 场次的临时退场空位集中在此释放 |

## 四、运维命令

**最常用的一条 —— 确认它现在还活着：**

```bash
python scripts/venue_monitor.py status -v example-venue
```

它会列出最近几次运行的时间与退出码，并给出判定。输出形如：

```
最近 4 次运行：
  2026-09-16 22:59:53.66 退出码 0   用时 2.0 秒
  2026-09-16 23:00:02.13 退出码 0   用时 2.0 秒

最后一次运行 : 2026-09-16 23:00:02.13（44 分钟前）
当前快照     : 空余 2 条，更新于 2026-09-16 23:17:55

判定：✅ 正常（调度在按时运行）
```

**为什么需要它**：长期无人值守最怕的不是报错，而是**静默失效** ——
路径变了、任务被删、python 升级了，表面一切正常，实际早就不跑了，
而你要等到某天才发现。`status` 就是用来定期戳一下的。
若当前处于活跃时段（06:00–23:00）却超过 90 分钟没有运行记录，它会直接报 ❌ 并给出排查顺序。

计划任务本身的操作：

```powershell
# 看状态
Get-ScheduledTask -TaskName 'VenueBookingMonitor' | Format-List TaskName,State
Get-ScheduledTaskInfo -TaskName 'VenueBookingMonitor'      # 上次/下次运行时间、结果码

# 立刻手动跑一次
Start-ScheduledTask -TaskName 'VenueBookingMonitor'
```

**但更推荐用封装好的 `-Action` 参数** —— 它会顺手把状态、上次/下次运行时间一起报出来，
不用自己去记 `Get-ScheduledTaskInfo`：

```bash
cd <skill目录>\scripts

# 一键开关（跑一次暂停，再跑一次恢复）
#   或者直接双击 venue-monitor-toggle.bat
powershell -ExecutionPolicy Bypass -File setup-scheduler.ps1 -Action toggle

powershell -ExecutionPolicy Bypass -File setup-scheduler.ps1 -Action pause      # 暂停
powershell -ExecutionPolicy Bypass -File setup-scheduler.ps1 -Action resume     # 恢复
powershell -ExecutionPolicy Bypass -File setup-scheduler.ps1 -Action status     # 只看状态
powershell -ExecutionPolicy Bypass -File setup-scheduler.ps1 -Action uninstall  # 彻底删除

# 不带 -Action 时行为不变，仍是「注册 / 重装」
powershell -ExecutionPolicy Bypass -File setup-scheduler.ps1
```

`-Action` 的输出形如：

```
[ok] 已暂停 —— 不会再自动运行。

  任务名    : VenueBookingMonitor
  当前状态  : Disabled  （已暂停，不会自动运行）
  上次运行  : 09/26/2026 21:00:00
  下次运行  : ——（已暂停，不会触发）
```

不想用命令行：`Win+R` → `taskschd.msc`，在任务计划程序库里找到 `VenueBookingMonitor`，
右键即可运行 / 停用 / 删除。

### 怎么确认「真的停了」

按可逆性从轻到重有三种做法：

| 做法 | 怎么做 | 特点 |
| --- | --- | --- |
| **临时停用** | 双击 `venue-monitor-toggle.bat`，或 `-Action pause` | 任务定义保留，随时 `-Action resume` 恢复；配置、快照、日志都不动 |
| **彻底删除** | `-Action uninstall` | 任务没了；恢复要重跑 `setup-scheduler.ps1` |
| **删除 + 清数据** | 上一条，再删 `state\` 与报告目录 | 回到「从没跑过」的状态 |

#### ⚠️ 两个容易误判的点

**① `NextRunTime` 停用后不会清空，会显示一个残留值。**

实测：
```
State       : Disabled          ← 这个才是权威判据
NextRunTime : 09/26/2026 22:00:00   ← 残留值，任务实际不会跑
```

所以**不要用 `NextRunTime` 是否为空来判断有没有停用**，要看 `State`。
想三层确认（推荐，尤其是远程指导别人时）：

```powershell
$t = Get-ScheduledTask -TaskName 'VenueBookingMonitor'
$t.State                    # 期望 Disabled
$t.Settings.Enabled         # 期望 False
```

**② 停用后 `venue_monitor.py status` 会报 ❌。**

它会看到「活跃时段却没有运行记录」并判定异常 —— 这是**预期的**，因为调度确实停了。
注意有个**延迟**：停用后约 90 分钟内仍显示 ✅（上次运行还没超过 90 分钟的阈值），
之后才开始报 ❌。别把刚停用时的 ✅ 当成「没停成功」。

> 停用不影响手动运行：`python scripts\venue_monitor.py check` 照常可用。

## 五、日志

每次运行都会追加到：

```
state\scheduler-logs\check-YYYYMMDD.log
```

一天一个文件，**纯 UTF-8**。格式：

```
========== start 2026-09-16 22:45:21.73 ==========
watch：xx市体育馆 (example-venue)   当前 22:45:22
不在轮询窗口内（窗口：08:50-10:30, 13:50-15:40），只跑一次。
...
---------- end 2026-09-16 22:45:23.74 (exit=0) ----------
```

排错时先看这个文件，再看任务计划程序的「上次运行结果」码。

> ⚠️ 写这个日志时踩过一个坑：`bat` 的 `echo` 默认按控制台代码页（GBK）输出，
> 而 python 输出 UTF-8，同一个文件里会混进两种编码，中文全乱。
> 解决办法是让 `bat` 的 `echo` 行只用 ASCII —— 日期用 `%date:~0,4%-%date:~5,2%-%date:~8,2%`
> 这种纯数字切片，**不要用 `%date%` 原样输出**（中文 Windows 下它带「周三」两个字）。
> ASCII 是 UTF-8 的子集，这样整个文件就是干净的 UTF-8。

## 五·五、资源占用（长期挂机必看）

**内存 —— 完全不是问题。** 实测一次完整 `check` 的内存峰值：

| 阶段 | 峰值 |
| --- | --- |
| python 刚启动 | 24.4 MB |
| 加载引擎后 | 27.4 MB |
| 跑完一次完整 check（拉 15 个号源、解析 374 条场次） | **43.5 MB** |

而且脚本**跑完就退出，不常驻** —— 两次运行之间占用为 0。
窗口内轮询是同一个进程循环，不会反复启停，内存也不会持续增长。

**磁盘 —— 这才是真正要盯的。** 每次 `check` 产出 2 个文件（`.md` 约 1.3 KB +
`.json` 约 25 KB）。若不加约束，按「窗口内每 90 秒一轮」算：

| | 改进前 | 改进后 |
| --- | --- | --- |
| 落盘次数/天 | 约 149 次（每次必写） | 约 21 次（**仅在有变化时**写） |
| 文件数/天 | 约 298 个 | 约 42 个 |
| 文件数/年 | 约 10.8 万个 | 约 1.5 万个 |
| 长期稳定占用 | 无上界，持续增长 | **约 1260 个 / 34 MB**（30 天滚动） |

两项措施（都已实现，无需配置）：

1. **轮询阶段仅在「有变化」时落盘** —— 窗口内每 90 秒一份报告，其中 99% 是
   「无新增空余」的冗余。现在只有真出现新场次时才留档。
   （由 `watch` 在轮询阶段自动开启，`check` / `probe` 不受影响。）
2. **过期报告自动清理** —— `watch` 每次启动时删除超过 `schedule.keep_output_days`
   天（默认 30）的 `venue-*.md` / `.json`。只匹配本工具的产物命名，
   不会误删目录里的其他文件。

想调整保留期就改 `venues.json`：

```json
"schedule": {
  "keep_output_days": 30
}
```

想立刻手动清理（或临时看有多少）：

```bash
python scripts/venue_monitor.py watch -v example-venue   # 启动时自动清理并打印数量
```

## 六、排错

**先跑这一条**，它能同时覆盖下面大半情况：

```bash
python scripts/venue_monitor.py status
```

`status` 会读日志里的 start/end 配对和快照时间，直接给判定。它会标出三种异常：

- `退出码 N（非 0）` → python 报错了，去日志末尾找 traceback
- `⚠️ 未正常结束（有 start 无 end）` → 运行被强制中断（关机 / 休眠 / Ctrl+C / 注销）
- `现在属于活跃时段，却已有 N 分钟没有任何运行记录` → 调度停了

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| **运行 `watch` / 双击 `.bat` 后终端一直不退出** | **设计行为**：`watch` 在轮询窗口内会每 90 秒轮询一次，盯到窗口结束才退出（例：窗口 13:50-15:40，13:53 运行则跑到 15:40）。且 `.bat` 把输出全部重定向到日志，终端看不到进度，更像卡死 | 手动测试改用 `check`（跑一次就退出）；`.bat` 现在会在终端打印模式说明与日志路径 |
| **一整天都没有运行记录，但任务状态是 Ready** | **触发器用 `-Once` 建的，已过期** | 看 `NextRunTime` 是否为空；是就重跑 `setup-scheduler.ps1`（见「二」坑 1） |
| 日志末尾停在 `^C`、没有 end 行 | 运行被强制中断（关机 / 休眠 / 注销 / Ctrl+C） | 看系统事件日志的关机时间；确认登录触发器有 `delay=PT3M` |
| 飞书一直没动静 | 任务没在跑 / 时间没到窗口 | 看 `LastRunTime`；看日志文件是否在增长 |
| 任务「上次运行结果」非 0 | bat 或 python 报错 | 日志文件末尾会有 python 的 traceback |
| 结果码 `3221225786`（0xC000013A） | 被 Ctrl+C / 关机强制结束 | 见上一条 |
| 日志里中文乱码 | bat 的 echo 用了 `%date%` 原样输出 | 改成纯数字切片（见「五」） |
| `setup-scheduler.ps1` 报语法错、且行号对不上 | `.ps1` 缺 UTF-8 BOM，中文注释被按 GBK 误读 | 给文件加 BOM（见「二」坑 2） |
| 09:00 那次没跑 | 08:00 那次还在轮询，被 `IgnoreNew` 跳过 | **这是正常行为**，08:00 那次会一直盯到 10:30 |
| 关机一夜后没收到提醒 | 关机期间任务不会触发，空位可能已被抢光 | 见「二·五」；**开机越早越好**，09:00 前开机可赶上放号窗口 |
| 开机后补跑但没推消息 | `StartWhenAvailable` 补跑时已过窗口 | 正常。补跑只跑一次 check，不会重放整个窗口 |
| 想确认提醒通道还通 | — | 手动跑 `venue_monitor.py notify-test -v example-venue` |

### 快速体检三条

```powershell
# 1) 任务还在吗？下次什么时候跑？NextRunTime 为空 = 触发器过期
Get-ScheduledTaskInfo -TaskName 'VenueBookingMonitor' |
    Format-List LastRunTime, LastTaskResult, NextRunTime

# 2) 触发器类型对不对？必须是 Daily
(Get-ScheduledTask -TaskName 'VenueBookingMonitor').Triggers |
    Format-List CimClass, Delay, Repetition
```

```bash
# 3) 引擎侧自检
python scripts/venue_monitor.py status
```

## 六·五、自主调试：验证与调整运行时间

### 先分清「两层时间」

| 层 | 配在哪 | 决定什么 |
| --- | --- | --- |
| **触发层** | Windows 计划任务 | 任务多久被**唤起**一次（当前：每小时整点） |
| **轮询层** | `references/venues.json` 的 `schedule` | 被唤起后**实际查多少次**（当前：窗口内每 90 秒） |

两层完全独立：整点唤起 18 次，但真正发请求的是窗口内的加密轮询。
**改调度先想清楚你要动哪一层。**

### 一、看当前配置

```powershell
$t = Get-ScheduledTask -TaskName 'VenueBookingMonitor'
$t.State                       # Ready = 启用
$t.Triggers | Format-List CimClass, StartBoundary, Delay, Repetition
Get-ScheduledTaskInfo -TaskName 'VenueBookingMonitor' |
    Format-List LastRunTime, NextRunTime, LastTaskResult
```

轮询层直接看配置文件里的 `schedule` 段，或用：

```bash
python scripts/venue_monitor.py doctor
```

**判据**：`NextRunTime` 非空 = 触发器还活着；**空 = 触发器已过期**。

### 二、手动触发一次（验证「动作」链路）

```powershell
Start-ScheduledTask -TaskName 'VenueBookingMonitor'
Start-Sleep -Seconds 5
Get-ScheduledTaskInfo -TaskName 'VenueBookingMonitor' | Format-List LastRunTime, LastTaskResult
```

然后看日志确认结果：

```bash
tail -40 state/scheduler-logs/check-$(date +%Y%m%d).log
```

> ⚠️ **`Start-ScheduledTask` 只验证「动作能跑」，不验证「触发器会按时响」。**
> 触发器只能靠 `NextRunTime` 加上「到点后日志里真的出现 start 行」来验证。

### 三、改「多久唤起一次」

```bash
# 例：07:00 开始、连续 16 小时（07:00-23:00）
powershell -ExecutionPolicy Bypass -File scripts\setup-scheduler.ps1 -StartAt 07:00 -RepeatHours 16
```

⚠️ 重跑是**先删旧任务再重建**（脚本会备份旧定义，注册失败自动还原）。

### 四、改「窗口内查多密」

编辑 `venues.json` 的 `schedule`：

| 字段 | 作用 | 当前值 |
| --- | --- | --- |
| `poll_windows` | 加密轮询窗口 | `["08:50-10:30","13:50-15:40"]` |
| `poll_interval` | 窗口内间隔（秒） | `90` |
| `lead_minutes` | 距窗口开始多少分钟内先跑一次 | `60` |
| `quiet_before` | 早于此时刻静默退出 | `06:00` |

改完**不用重注册任务**，下一次唤起就生效。

### 五、临时测试轮询（不用等到点）

把窗口改到当前时间附近，再手动跑 `watch`：

```bash
# 1) 先备份
cp references/venues.json references/venues.json.bak

# 2) 改 poll_windows 到当前时间附近（如现在 16:30 -> "16:28-16:35"），
#    并把 poll_interval 调成 10，方便在几秒内看到多轮
# 3) 手动跑（窗口结束时会自己退出）
python scripts/venue_monitor.py watch -v example-venue --no-notify

# 4) 看日志里出现了多少轮 check
# 5) 测完改回去，删掉 .bak
```

> ⚠️ 测试时**务必加 `--no-notify`**，否则会往飞书刷消息。
> ⚠️ 如果开了自动锁单（`order.auto=true`），测试期间**临时设为 `false`** —— 否则可能真的下单。

### 六、「时间不对」的排查顺序

1. `NextRunTime` 为空 → 触发器过期，重跑 `setup-scheduler.ps1`
2. `NextRunTime` 正常但没日志 → 看 `LastTaskResult`：
   - `0` = 成功
   - `3221225786`（`0xC000013A`）= 被 Ctrl+C / 关机强制中断
   - 其他 → 手动跑一次 `venue-check.bat` 看报错
3. 有日志但没查询 → 检查 `quiet_before`（早于它直接退出）和 `poll_windows`
4. 日志写着「不在轮询窗口内，只跑一次」→ 正常，只是当时不在窗口
5. 日志写着「状态已过期 N 小时…直接跳到 X 扫描」→ 正常，是长期停用后的自愈逻辑

## 七、注意

- **凭证**：查询接口实测不校验凭证，token 过期照常监测；
  但**下单接口会校验**（`code=99 请先登录`），自动锁单前需要用 `set-token` 更新。
- **默认只读**：`check` / `watch` 只做查询与提醒；
  只有配了 `order.auto=true` 或显式执行 `order --confirm` 才会真的下单。
- **改路径后要重注册**：若挪动了 skill 目录，改完重跑 `setup-scheduler.ps1`。
  现在的脚本是自定位的（`.bat` 从自身位置推导路径），所以正常挪动目录也能直接跑。
- **改脚本后注意编码**：给 `.ps1` 加中文内容后必须确认文件带 UTF-8 BOM；
  `.bat` 必须保持纯 ASCII。见「二」坑 2。
