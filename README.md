# 场馆预约监测 · 操作手册

监测体育场馆 / 运动场地预约小程序的**空余场次**，一有新场放出就通过飞书 / 企业微信 / 钉钉 /  
Server酱 / PushPlus / Bark 推送提醒。**只读接口，不自动下单** —— 提醒到了需要你自己去小程序里订。

---


## 准备：装什么软件（先看这里）

整套东西只有 **一个运行环境** + **一个抓包工具** 是必须装的，其余看情况。

### 必须装

| 软件 | 干什么用 | 下载 | 怎么确认装好了 |
| --- | --- | --- | --- |
| **Python 3.9+** | 跑监测引擎（**无第三方依赖**，只用标准库） | [python.org/downloads](https://www.python.org/downloads/) 或微软商店搜「Python 3.12」 | 命令行执行 `python --version` 能出版本号 |
| **Charles** | 抓小程序的接口 —— **唯一需要动脑的一步** | [charlesproxy.com/download](https://www.charlesproxy.com/download/) | 能打开，`Help → About` 看到版本号 |

> ⚠️ **Windows 装 Python 时务必勾选 `Add python.exe to PATH`。**
> 不勾的话命令行敲 `python` 会提示「不是内部或外部命令」，`.bat` 也会跟着失败。
>
> **Charles 的替代品**：**Reqable**（[reqable.com](https://reqable.com/)）——
> 国内访问更顺、有中文界面，操作逻辑一致。本文档以 Charles 为例，对应菜单名请自行对照。

### 看情况装

| 软件 | 什么时候需要 |
| --- | --- |
| **微信 PC 版** | 小程序能在电脑上打开时 —— **推荐路线，全程不用碰手机** |
| **手机 + 手机端证书** | 小程序只能在手机上打开时（见 `references/phone-capture-guide.md`） |
| **Fiddler / mitmproxy** | 已有使用习惯，可替代 Charles，操作思路相同 |
| **飞书 / 企业微信 / 钉钉** | 想收推送时。**不配也能跑**，结果照常写进日志和报告文件 |

### 不用装

- 不用数据库、不用 Node、不用 `pip install` 任何东西。
- 不用 Docker。

### 大概要花多久

| 阶段 | 时间 | 难度 |
| --- | --- | --- |
| 装 Python + 跑通离线自检 | 5 分钟 | 简单 |
| 装 Charles + **配好 SSL 解密** | 10–20 分钟 | ⚠️ **最容易卡在这里** |
| 抓一次接口 + 转成配置 | 10 分钟 | 中等 |
| 配推送 + 设定时任务 | 10 分钟 | 简单 |

> **卡住最多的是「SSL Proxying」**：Charles 默认不解密 HTTPS，必须手动加规则，
> 而且 `Port` 的默认值是 `443`，会漏掉 `8443` 这类非标准端口。
> 完整步骤 + 30 秒自检法见 `references/recon-playbook.md` **第一节**。

# 从零跑起来

## 1 你需要准备什么

> 软件下载地址见上面「准备：装什么软件」。这里只列**硬性约束**。

| 需要                 | 说明                                                                               |
| ------------------ | -------------------------------------------------------------------------------- |
| **一台 Windows 电脑**  | 调度脚本（`.bat` / `.ps1`）是 Windows 专用。Linux / macOS 见 [2.6.4](#64-linux--macos-怎么办) |
| **Python 3.9 或更高** | 无第三方依赖，标准库就够。命令行跑 `python --version` 能出版本号即可                                     |
| **这台电脑在放号时段是开机的**  | **这是唯一的硬约束。** 关机 / 睡眠 = 停止监测（见 [2.6.6](#66-关机了会怎样)）                             |
| **一个群机器人**         | 飞书 / 企业微信 / 钉钉 / Server酱 / PushPlus / Bark 任选                                    |
| **能打开并抓包目标小程序**    | 得能进那个小程序，才能抓到它的接口                                                                |

## 2 放在哪里

**放哪儿都行**，脚本会自己算出路径。建议：

```
C:\tools\venue-booking-monitor\
```

或者 `%USERPROFILE%\venue-booking-monitor\`。

⚠️ **路径里不要有中文或空格。** 批处理文件在中文 Windows 下会被控制台代码页搞乱，  
这是这个项目踩过的坑。放在纯英文路径下最省事。

解压后应该是这样：

```
venue-booking-monitor/
├── README.md              <- 你正在看的
├── SKILL.md               <- 完整技术文档
├── references/
│   ├── venues.json        <- 场馆配置（你要改的就是这个）
│   ├── recon-playbook.md  <- 抓包手册（必读）
│   ├── notify-setup.md
│   ├── scheduler-setup.md
│   ├── extending-and-sharing.md
│   └── phone-capture-guide.md
└── scripts/
    ├── venue_monitor.py   <- 引擎
    ├── curl2venue.py      <- 抓包转换工具
    ├── venue-check.bat    <- 定时任务入口
    └── setup-scheduler.ps1
```

## 3 第一次运行：先确认引擎是好的（5 分钟，不联网）

先别碰配置，先确认这套东西在你这台机器上能跑。

```bash
cd C:\tools\venue-booking-monitor

# ① 离线自检：用内置 mock 跑通「拉取 -> 解析 -> 比对 -> 报告」全链路
python scripts\venue_monitor.py selftest
```

看到这样的输出就是好的：

```
--- 第 1 次（建立基线）---
场次 12 条 / 空余 5 条 / 新增 0 条
--- 修改快照，模拟「新放出一批空位」---
场次 12 条 / 空余 5 条 / 新增 3 条
自检结果：✅ 通过（检测到 3 条新增空余）
```

```bash
# ② 跑一次真实的 check（此时配置里只有 mock 场馆，仍然不联网）
python scripts\venue_monitor.py check
```

看到「监测：演示场馆… 空余 N 条」就说明引擎、配置、报告输出全部正常。

> 如果 ①② 有报错，先别往下走，把报错贴给分享给你的人 —— 这一步都过不了，  
> 后面配抓包只会更乱。

## 4 接入你自己的小程序

这是唯一需要动脑的一步。**其余全是填表。**

### 4.1 抓包：把那条接口请求留下来

目标只有一个：**抓到那个「查某天有哪些场次」的接口请求**。
不需要懂它的业务逻辑，只要把请求原样留下来。

#### 先选路线

| 你的情况 | 走哪条 | 要装什么 |
| --- | --- | --- |
| 小程序能在**微信 PC 版**里打开 | **路线 A（推荐）** | Charles，全程不用碰手机 |
| 只能在手机上打开 | 路线 C | Charles + 手机 + 手机证书 |
| 有微信开发者工具权限 | 路线 B | 开发者工具（请求最干净） |

**怎么判断能不能在 PC 上打开**：微信 PC 版搜小程序名字，能点进去就算。
如果提示「仅支持手机」或打不开，走路线 C。

#### 路线 A 速查（Charles + 微信 PC 版）

```text
1. 装 Charles，打开
2. Proxy → 勾选 Windows Proxy            （让它接管系统代理）
3. Proxy → SSL Proxying Settings
     ☑ Enable SSL Proxying
     Add → Host = *   Port = *           ← Port 别留 443！这是最大的坑
4. 微信 PC 版打开目标小程序，点进「场地/时段」页面
5. 回到 Charles，左侧找到那个域名，展开看请求
```

> **30 秒自检**（强烈建议先做，别急着折腾微信）：
> 用 PC 浏览器打开任意 https 网站 → 回 Charles 看那条记录。
> - 显示成 `GET / HTTP/1.1` 这种**明文** → SSL 解密已生效 ✅
> - 仍显示 `CONNECT xxx:443` **隧道** → 解密没配好，先把这步弄对 ❌

#### 怎么确认「抓对了」

要抓的那条请求通常满足：

- **URL 里带** `timetable` / `schedule` / `slot` / `list` / `detail` 之类的词
- **响应是 JSON**，里面能看见日期 + 时段 + 场地 + 某个「是否可订」的字段
- 切换日期再点一次，**URL 或参数会跟着变**（这说明它带日期参数）

> 如果看到的全是 `CONNECT` 隧道、没有明文，说明 SSL 解密没生效 ——
> 回到上一步。详细排查见 `references/recon-playbook.md` 第一节。

#### 导出给我/给工具

三种方式，任选（越靠前越省事）：

| 方式 | 怎么操作 | 拿到什么 |
| --- | --- | --- |
| **Charles 会话文件** | `File → Save Session As...` → 存成 `.chlz` | 请求 + 响应全都有（**推荐**） |
| **HAR** | `File → Export Session` → 选 `.har` | 同上 |
| **cURL** | 右键那条请求 → `Copy cURL Request` | 只有请求，**没有响应** |

```bash
# 让工具直接解析会话文件，并推荐字段映射
python scripts\curl2venue.py --chlz 你的会话.chlz --id my-venue --name "我的场馆" --send --out 建议.json
```

> ⚠️ 会话文件里有你的**登录凭证**，等同于账号密码。自己留着，别发给别人、别传公开仓库。

#### 抓不到怎么办

| 现象 | 原因 | 对策 |
| --- | --- | --- |
| 全是 `CONNECT` 隧道 | SSL 解密规则没加或 Port 填了 443 | 按上面第 3 步重配 |
| 小程序提示「后端链接异常」 | 系统代理被别的工具占着 | 先退出其它代理工具，只留 Charles |
| 小程序能开但 Charles 里没记录 | 微信没走系统代理 | 确认 `Windows Proxy` 已勾选 |
| 请求里有看不懂的 `sign` / `encrypt` | 接口带客户端签名 | **接不了**，换场馆吧（见下） |

### 4.2 转成配置

```bash
# 让工具读 cURL、真的打一次接口、并推荐字段映射
python scripts\curl2venue.py --curl-file 抓包.txt --id my-venue --name "我的场馆" --send --out 建议.json
```

`--send` 会实际请求一次，然后根据真实响应推荐 `slots_path` / `available_values`。  
**推荐结果是启发式的，仍需人工核对**，但能省掉大半试错。


### 4.3 填进 venues.json

打开 `references/venues.json`，把 `another-venue` 那段替换成你的：

```json
{
  "id": "my-venue",
  "name": "我的场馆",
  "miniprogram": "#小程序://你的场馆/xxxx",
  "enabled": true,
  "request": {
    "method": "GET",
    "url": "https://你的接口/api/slots?date=${DATE}",
    "headers": {
      "Referer": "https://servicewechat.com/",
      "Authorization": "抓包得到的凭证整串，原样粘贴"
    },
    "timeout": 15,
    "no_proxy": true
  },
  "response": {
    "slots_path": "data.slots",
    "time_field": "startTime",
    "available_field": "status",
    "available_values": ["1"],
    "court_field": "courtName",
    "price_field": "price"
  },
  "dates": { "offsets": [0,1,2,3,4,5,6], "date_format": "%Y-%m-%d" },
  "watch": { "time_windows": ["18:00-22:00"] },
  "report": { "mode": "flat" },
  "notify": { "type": "feishu", "webhook_url": "你的机器人地址", "no_proxy": true }
}
```

**三个容易踩的坑：**

| 坑                            | 说明                                                                                                            |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------- |
| **占位符只有四个**                  | `${DATE}` `${TIMETABLE_ID}` `${TS}` `${VENUE_ID}`。写别的（比如 `${TOKEN}`）**不会被替换**，会原样发出去，表现为「接口没返回数据」，极难查。凭证直接粘明文 |
| **`available_field` 必须实测校准** | 别猜。挑一条你确定能订的、一条确定不能订的，对比哪个字段不同 —— 那个就是它                                                                       |
| **日期格式要和抓包一致**               | 抓包里是 `2026-09-17` 就填 `%Y-%m-%d`；是 `20260917` 就填 `%Y%m%d`                                                      |

> 详细的字段含义见 `SKILL.md` 的「配置字段速查」，  
> 完整扩展流程见 `references/extending-and-sharing.md`。

### 4.4 验证配置

```bash
# 配置体检：看字段有没有被正确解析
python scripts\venue_monitor.py doctor

# 只跑你这个场馆
python scripts\venue_monitor.py check -v my-venue
```

**关键一步：把「空余 N 条」和你自己在小程序里肉眼看到的数量对一下。**  
对不上就回 2.4.3 重新校准 `available_field` / `available_values`。

确认无误后，把 `demo-venue` 的 `enabled` 改成 `false`，别再让它占位。

## 5 配推送

打开 `references/notify-setup.md`，照着建一个机器人，拿到 webhook 地址填进 `notify`。

真正发现空位时，你收到的消息长这样：

```
【xx市体育馆】有场放出了
📅 9/18（明天） 刚开放预订 · 新增 5 个空场
20:00-20:30 · 12号场
21:00-21:30 · 2号场
21:30-22:00 · 2号场、7号场、9号场

👉 去小程序订：#小程序://xx市体育馆/xxxxxxxxxxxxxxxx
```

**直接告诉你去哪订**：哪天、哪个时段、几号场。设计上有意做了三件事：

- **不放本地文件路径**（手机上点不开、看不懂），末尾放的是小程序链接
- **按时段归并**：同一时段的多个场地合成一行（`21:30-22:00 · 2号场、7号场、9号场`），
  5 个空位从 5 行压到 3 行，扫一眼就够
- **日期带「今天/明天」**，不用心算

单条最多列 12 个时段，可用 `notify.max_slots_in_message` 调。

### ⚠️ 一个必须理解的规则：只在「有新空位」时才推

**飞书安静 ≠ 没有空位。** 引擎只在**出现新空位**时推消息。如果某个空位昨天就记录过了、
今天还在，它不会再推一次——否则你每个整点都会收到一条一模一样、没有新信息的消息。

所以「今天一条消息都没有」的正常含义是：**这段时间没有任何新场放出来**，
而不是「没有空位」。想知道「现在到底有什么」，看每日摘要（见下）。

### 每日摘要：证明系统还活着

「安静」和「挂了」从你的角度看是一样的，所以建议配一条每日摘要：

```json
"notify": {
  "webhook_url": "...",
  "daily_digest_at": "10:35"
}
```

效果是每天 10:35 收到一条**当前全部空场**的汇总（不管有没有变化）：

```
【xx市体育馆】每日摘要 · 当前 5 个空场
9/18（明天）　共 5 个空场
20:00-20:30 · 12号场
21:00-21:30 · 2号场
21:30-22:00 · 2号场、7号场、9号场

👉 去小程序订：#小程序://xx市体育馆/xxxxxxxxxxxxxxxx

（每日 10:35 推一条汇总；一旦出现新空位仍会立即提醒）
```

> **为什么是「过了点就补发」而不是「准点发」？** 计划任务是整点触发的，10:35 那一刻没有
> 进程在跑。引擎的保障是两道：① `watch` 在上午轮询窗口（到 10:30）结束后会**顺延到 10:35**
> 补跑一次，所以正常情况下准时；② 万一那次被中断（关机/强杀），下一个整点（11:00）的运行
> 会**兜底补发**。当天只发一次，不会重复。
>
> 摘要时刻留空 = 不启用这个功能。

填好后**必须做两层验证**，缺一层都不算配好：

```bash
# 第一层：验通道 —— 消息能不能发出去
python scripts\venue_monitor.py notify-test -v my-venue
```

`notify-test` 发的那条消息里会**附上真实提醒的样子**（用 2099 年的示例数据，不会被误认成真消息），
你可以顺便确认格式对不对。

第一层过了只说明「通道通」。还要验**第二层：链路** —— 「真的比对出新空位时会不会推」：

```bash
# 备份快照 -> 删掉一条空位 -> 跑 check -> 看有没有推 -> 恢复快照
# 具体命令见 references/notify-setup.md 的「七、验证」
```

> ⚠️ **HTTP 200 不等于推送成功。** 飞书签名错了会返回 200 但 body 里是  
> `{"code":19021,"msg":"sign match fail"}`。所以引擎会读响应体，你也要看一眼日志。

## 6 设成定时任务

### 6.1 先改一处配置

打开 `references/venues.json`，找到 `example-venue` 之外的、你自己场馆的 `schedule` 段  
（没有就加一段）：

```json
"schedule": {
  "poll_windows": ["08:50-10:30", "13:50-15:40"],
  "poll_interval": 90,
  "lead_minutes": 60,
  "quiet_before": "06:00",
  "keep_output_days": 30
}
```

| 字段                 | 含义                                                         |
| ------------------ | ---------------------------------------------------------- |
| `poll_windows`     | **加密轮询窗口**。窗口内每 `poll_interval` 秒跑一次。**起点 = 开始盯，终点 = 停止盯** |
| `poll_interval`    | 轮询间隔秒数。90 秒已经很密了，再密有被风控的风险                                 |
| `lead_minutes`     | 距窗口开始多少分钟内先跑一次、然后等到窗口开始（避免整点触发在窗口开头留空档）                    |
| `quiet_before`     | 早于此时刻静默退出，省得深夜空跑                                           |
| `keep_output_days` | 报告保留天数，超期自动清理                                              |

> **怎么知道放号时刻？** 用 `release` 子命令观察几天，它会从订单号里反推放号时间：  
> `python scripts\venue_monitor.py release -v my-venue`  
> 拿到时间后，把 `poll_windows` 设成「放号前 10 分钟 ~ 放号后 1 小时」。

### 6.2 改 .bat 里的场馆 id

打开 `scripts\venue-check.bat`，只改这一处：

```bat
set "VENUE=my-venue"
```

留空 `set "VENUE="` 表示监测配置里**全部**已启用场馆。

### 6.3 注册计划任务

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-scheduler.ps1
```

它会注册一个名为 `VenueBookingMonitor` 的任务：**每小时（06:00–23:00）+ 每次登录时**触发。  
`setup-scheduler.ps1` 是自定位的，不用改任何路径。

验证：

```powershell
Start-ScheduledTask -TaskName VenueBookingMonitor
Get-ScheduledTaskInfo -TaskName VenueBookingMonitor | Format-List LastRunTime,LastTaskResult
# LastTaskResult 0 = 成功
```

再跑一次自检命令确认调度真的在跑：

```bash
python scripts\venue_monitor.py status
```

### 6.4 Linux / macOS 怎么办

引擎本身跨平台，只有 `watch` 的调度入口需要换。用 cron：

```cron
# 每小时 06:00-23:00 触发一次 watch
0 6-23 * * * cd /path/to/venue-booking-monitor && python3 scripts/venue_monitor.py watch -v my-venue >> state/scheduler-logs/cron.log 2>&1
```

`watch` 自己会判断「现在该不该轮询、轮询到几点」，所以**不需要**在 cron 里写轮询逻辑。

### 6.5 同时监控多个场馆（可选）

想同时盯几个场馆，**不需要建多个计划任务**。在 `references/venues.json` 里把每个场馆都配好、  
`enabled` 都设为 `true`，然后：

- `venue-check.bat` 里 `set "VENUE="` **留空** → 一次 `watch` 覆盖全部已启用场馆
- 如果填了具体 id → 只跑那一个

每个场馆的快照、推送、错误都是独立的，一个场馆挂了不影响其他。

**各场馆放号时间不同怎么办？** 各写各的 `schedule`，引擎会自动取并集：

```jsonc
// 场馆 A：上午 09:30 放号
"schedule": { "poll_windows": ["08:50-10:30"] }

// 场馆 B：下午 14:00 放号
"schedule": { "poll_windows": ["13:20-15:00"] }
```

重叠的窗口会被合并，不会出现「盯到前一个窗口结束就把后一个漏掉」。

用 `doctor` 可以确认哪些场馆真的在跑：

```
[my-venue]     我的场馆    ->  ✅ 已就绪
[venue-b]      某某体育馆  ->  ⏸️  已禁用（enabled=false，check/watch 会跳过）
已启用  ：1 / 2（1 个被 enabled=false 跳过）
```

> 场馆数 6 个以上时，建议把 `poll_interval` 调到 120 秒以上，  
> 免得单轮还没跑完下一轮又开始了。

### 6.6 关机了会怎样

**发起方（跑脚本的这台机器）必须在线。** 关机 / 睡眠 = 停止监测，没有任何补救。

| 情况          | 结果                             |
| ----------- | ------------------------------ |
| 电脑关机过夜      | 次日的放号**收不到提醒**                 |
| 电脑睡眠        | 同上（任务设置里 `WakeToRun` 默认关闭）     |
| 只是关了 IM 客户端 | ✅ 不影响 —— 消息落在 IM 服务器的群里，手机照样收  |
| 计划任务没跑      | `StartWhenAvailable` 会在开机后补跑一次 |

三条补救路径（按推荐度）：

1. **改电源设置，让电脑在放号时段保持唤醒**（最简单）
2. **BIOS 里设定时开机**，让机器在放号前自动启动
3. **把脚本挪到一台常开的机器上**（NAS / 云服务器 / 树莓派）

## 7 日常运维

```bash
# 最常用的一条：调度还好吗？（有没有静默失效）
python scripts\venue_monitor.py status

# 配置体检
python scripts\venue_monitor.py doctor

# 手动跑一次
python scripts\venue_monitor.py check -v my-venue
```

**日志**在 `state/scheduler-logs/`，一天一个文件 `check-YYYYMMDD.log`。

`status` 会一并报出每日摘要的状态，方便确认「系统还活着」：

```
最后一次运行 : 2026-09-17 17:00:00.74（13 分钟前）
当前快照     : 空余 5 条，更新于 2026-09-17 17:06:41
每日摘要     : 今天已发（2026-09-17 17:05:19，定在 10:35）

判定：✅ 正常（调度在按时运行）
```

若显示「⚠️ 已过 10:35 但今天还没发」，说明连补发那次运行也没跑成，要查调度。

### ⚠️ 为什么脚本跑起来「不退出」

**`watch` 在轮询窗口内会一直跑到窗口结束，这是设计行为，不是卡死。**

| 命令 | 行为 | 什么时候用 |
| --- | --- | --- |
| `check` | **跑一次就退出**（几秒） | 手动验证、调试 |
| `watch` | 窗口内每 90 秒轮询一次，**盯到窗口结束才退出** | 计划任务调用 |
| `venue-check.bat` | 内部调用的是 **`watch`** | 计划任务入口 |

举个例子：窗口是 `13:50-15:40`，你在 **13:53** 运行 `watch`，它会一直跑到 **15:40** ——
将近两小时不退出。

**手动测试请用 `check`：**

```bash
python scripts\venue_monitor.py check -v example-venue
```

另一个容易误判的点：**`.bat` 把 python 的输出全部重定向到日志文件了**
（轮询窗口内每 90 秒一条，不重定向会刷屏），所以直接双击 `.bat` 时**终端上什么都不会显示**。
它现在会在开头打印一段说明（模式、要等多久、日志在哪、怎么单次运行），结尾打印退出码。
想看实时输出，另开一个窗口：

```bash
powershell -c "Get-Content -Wait 'state\scheduler-logs\check-YYYYMMDD.log'"
```

> 如果确实要中断，按 `Ctrl+C` 即可 —— 但请注意：中断后**本次轮询窗口就作废了**。
> 不过每日摘要有兜底：下一个整点的运行会补发（见 2.5）。

### 怎么停下来

**最省事：双击 `scripts\venue-monitor-toggle.bat`。**

它是一个**开关**——跑一次暂停，再跑一次恢复，每次都会把结果打印出来：

```
[ok] 已暂停 —— 不会再自动运行。

  任务名    : VenueBookingMonitor
  当前状态  : Disabled  （已暂停，不会自动运行）
  上次运行  : 09/26/2026 21:00:00
  下次运行  : ——（已暂停，不会触发）

  （本脚本是「切换」：再运行一次就会变回另一种状态）
```

命令行等价写法（想明确指定动作时用）：

```bash
cd <skill目录>\scripts
powershell -ExecutionPolicy Bypass -File setup-scheduler.ps1 -Action pause     # 暂停
powershell -ExecutionPolicy Bypass -File setup-scheduler.ps1 -Action resume    # 恢复
powershell -ExecutionPolicy Bypass -File setup-scheduler.ps1 -Action status    # 只看状态
powershell -ExecutionPolicy Bypass -File setup-scheduler.ps1 -Action uninstall # 彻底删除
```

按可逆性从轻到重，三档：

| 档位 | 做法 | 特点 |
| --- | --- | --- |
| **临时停用**（推荐） | 双击 toggle.bat，或 `-Action pause` | 任务保留，随时恢复；配置、快照、日志都不动 |
| **彻底删除** | `-Action uninstall` | 任务没了；恢复要重跑 `setup-scheduler.ps1` |
| **删除 + 清数据** | 上一条，再删 `state\` 与报告目录 | 回到「从没跑过」的状态 |

图形界面也行：`Win+R` → `taskschd.msc` → 右键 `VenueBookingMonitor` → 停用/删除。

**确认真的停了，看 `State` 而不是 `NextRunTime`：**

```powershell
$t = Get-ScheduledTask -TaskName 'VenueBookingMonitor'
$t.State                # 期望 Disabled
$t.Settings.Enabled     # 期望 False
```

> ⚠️ 停用后 `NextRunTime` **不会清空**，仍会显示一个残留时间 —— 这是 Windows 的行为，
> 任务实际不会跑。别用「NextRunTime 是否为空」来判断。
>
> ⚠️ 停用**约 90 分钟后**，`status` 会开始报 ❌（「活跃时段却没有运行记录」）——
> 这是**预期的**，不是故障。刚停用的那一个多小时里它仍显示 ✅（上次运行还没过期）。
>
> 停用只影响自动调度；手动 `python scripts\venue_monitor.py check` 照常可用。

**凭证过期了怎么办？** 很多小程序的接口其实**不校验凭证**（可以先做个对照实验：  
不带凭证请求一次，如果照样返回正常数据，那凭证填错也无所谓）。  
万一确实需要更新：

```bash
# 从新的抓包里取凭证并写回配置（会留 .bak、实测验证、失败自动回滚）
python scripts\venue_monitor.py set-token -v my-venue --from-curl-file 新抓包.txt
```


## 8 常见问题

| 现象                                        | 原因 / 怎么办                                                                                                                                               |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **一整天都没有运行记录，但任务状态显示 Ready**              | **触发器过期了。** 看 `(Get-ScheduledTaskInfo -TaskName 'VenueBookingMonitor').NextRunTime`，为空就重跑 `setup-scheduler.ps1`。整点触发器必须是 `Daily` 类型，用 `Once` 建的话第二天就失效 |
| 日志末尾停在 `^C`、没有 `end` 行                    | 运行被强制中断（关机 / 休眠 / 注销 / Ctrl+C）。任务「上次运行结果」会是 `3221225786`                                                                                               |
| `check` 报「接口没返回数据」                        | 十有八九是 `request.headers` 里留了没被替换的 `${...}` 占位符。检查一遍                                                                                                     |
| `check` 报「请求头含非 ASCII 字符」                 | 请求头里混了中文/emoji。HTTP 头只允许 latin-1，换成抓包得到的原始值                                                                                                            |
| 空余数和小程序里看到的不一致                            | `available_field` / `available_values` 没校准。见 2.4.3                                                                                                     |
| `notify-test` 成功但实际不推                     | 那是「通道」通了、「链路」没通。见 2.5                                                                                                                                  |
| 飞书返回 200 但没收到消息                           | 读响应体，看是不是 `19021 sign match fail`（签名错）                                                                                                                 |
| 本地服务/接口请求莫名 502                           | 系统代理在拦截。确认配置里 `no_proxy: true`                                                                                                                         |
| 计划任务 `LastTaskResult` 不是 0                | 看 `state/scheduler-logs/` 里最新的日志，报错都在里面                                                                                                                |
| 改了 `setup-scheduler.ps1` 后报语法错、**且行号对不上** | `.ps1` 缺 UTF-8 BOM，中文注释被 PowerShell 按 GBK 误读。给文件加 BOM。`.bat` 则相反：必须纯 ASCII 且不能有 BOM                                                                    |
| 日志里中文乱码                                   | `.bat` 里用了 `%date%` 原样输出。本项目的 `.bat` 已用纯数字切片规避                                                                                                         |
| 报告文件太多占磁盘                                 | 调小 `schedule.keep_output_days`。轮询阶段只在「有变化」时才落盘，正常一天没几个文件                                                                                               |

### 一条命令体检

```bash
python scripts\venue_monitor.py status
```

它会直接告诉你调度是否正常，并标出「退出码非 0」「有 start 无 end（被中断）」  
「活跃时段却很久没运行」这三种异常。

---

# 第三部分 · 参考

| 文档                                    | 讲什么                                |
| ------------------------------------- | ---------------------------------- |
| `SKILL.md`                            | 完整技术文档：全部子命令、配置字段速查、两种接口/响应形态、已知约束 |
| `references/recon-playbook.md`        | **抓包手册**，接入新小程序时必读                 |
| `references/phone-capture-guide.md`   | 手机抓包（只有手机能打开的小程序）                  |
| `references/notify-setup.md`          | 6 种推送通道配置 + 两层验证法                  |
| `references/scheduler-setup.md`       | 调度部署、日志、资源占用、关机场景                  |
| `references/extending-and-sharing.md` | 扩展到别的小程序 / 同时监控多个 / 分享给别人          |

## 这个工具不做什么

- ❌ **不自动下单**。它只监测 + 提醒，抢单要你自己去小程序里点。
- ❌ **不绕过风控**。`poll_interval` 别调太小。
- ❌ **不支持需要客户端签名的接口**。抓包时看到看不懂的 `sign` / `nonce` / `encrypt`  
  参数、且删掉就报错，说明这个接口接不了（详见 `extending-and-sharing.md` 第五节）。
- ❌ **不支持 WebSocket / 长连接推送**的接口。引擎是 HTTP 轮询模型。

## 资源占用

| 项目   | 实测                        |
| ---- | ------------------------- |
| 内存峰值 | 约 44 MB，**且不常驻** —— 跑完就退出 |
| 磁盘   | 约 34 MB / 年（30 天滚动清理）     |

## 合规提醒

- **默认只读**：`check` / `watch` 只查公开可订的场次信息，不碰订单、不碰支付。
- **自动下单是可选功能**（`order` 子命令，或 `order.auto=true`）。它会对**公共场馆的稀缺资源**
  产生排他性占用 —— 请只在「你自己确实要去打」的那一个时段使用，
  不要批量扫场、不要高频重试、不要替他人代抢。
- 轮询频率保持克制（≥ 60 秒），别给对端服务器造成压力。
- 抓包得到的凭证是账号级的，**不要提交到公开仓库**。
- 平台规则以场馆 / 小程序的服务条款为准；自动化操作存在账号被风控的风险，请自行评估。
