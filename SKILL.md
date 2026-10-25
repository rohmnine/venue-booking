---
name: venue-booking-monitor
description: 监测体育场馆/运动场地预约小程序（微信小程序订场）是否有空余场次，支持在同一份配置里扩展监测多个不同小程序链接；包含抓包侦察、放号时间实测（含用订单号时间戳反推）、新增空余比对与提醒、报告落盘。当用户提到「订场/预约小程序」「场地还有没有空」「抢不到场」「几点放号」「体育场/体育馆 羽毛球 足球 篮球 网球 场地预约」「监测空场」等场景时使用。
agent_created: true
---

# 场馆预约监测（venue-booking-monitor）

## 用途

把「某体育场馆小程序还有没有空余场次」这件事，变成一条可定时、可多场馆复用的只读监测流水线。

核心事实：**微信小程序没有公开 URL**，`#小程序://xxx/yyy` 只能在微信内打开。
因此本 skill 的路线不是去模拟点击 UI（脆弱且需真机），而是：
**抓包拿到小程序背后的 HTTPS 接口 → 写进配置 → 引擎定时请求并比对**。

引擎只做**查询 + 比对 + 提醒**，**不做自动下单/自动占场**。

## 何时使用

- 用户想「自动盯着某场馆有没有空场」
- 用户问「这个小程序几点放号 / 几点开放场次预订」
- 用户要监测多个不同场馆/小程序
- 用户已经有场馆小程序链接（`#小程序://...`）并想自动化

## 目录结构

```
venue-booking-monitor/
├── README.md                     # 操作手册：分享方「该给什么」+ 接收方「从零跑起来」
├── SKILL.md
├── scripts/
│   ├── venue_monitor.py          # 引擎：doctor / check / probe / release / notify-test / set-token / watch / status / selftest
│   ├── curl2venue.py             # 把抓到的 cURL / HAR / .chlz 转成场馆配置 + 推荐响应字段映射
│   ├── make-share-package.py     # 生成「可直接发给别人」的净化副本（剔除凭证/路径 + 硬校验）
│   │                             #   --monitor-only：剥离自动下单，产出可开源的「只监测」版
│   ├── venue-check.bat           # 计划任务入口（自定位，纯 ASCII，不可加 BOM）
│   ├── venue-monitor-toggle.bat  # 一键暂停 / 恢复（双击即可，纯 ASCII）
│   └── setup-scheduler.ps1       # 计划任务管理：-Action install/pause/resume/toggle/status/uninstall
│                                 #   不带 -Action 时默认 install（注册 / 重装）。必须带 UTF-8 BOM
├── references/
│   ├── venues.json               # 场馆配置（多场馆在此扩展）
│   ├── recon-playbook.md         # 抓包侦察手册（总览，必读）
│   ├── phone-capture-guide.md    # 手机抓包实操指南（iOS/Android 逐步）
│   ├── notify-setup.md           # 推送通道配置指南（企业微信/钉钉/飞书/Server酱/PushPlus/Bark）
│   ├── scheduler-setup.md        # 系统级调度部署与运维（不依赖客户端挂机）
│   └── extending-and-sharing.md  # 扩展到别的小程序 / 同时监控多个 / 分享给别人
├── share/                        # make-share-package.py 的产物（可整包发出去）
└── state/                        # 运行时快照、放号观测日志、调度日志（自动生成）
```

> **脚本编码规则**（踩坑换来的，别改）：
> `.ps1` 必须带 UTF-8 BOM（否则中文注释被 PowerShell 按 GBK 误读，报错行号都对不上）；
> `.bat` 必须纯 ASCII 且**不能**有 BOM（cmd 会把 BOM 当命令执行）。
> `make-share-package.py` 打包时会校验这两条。

## 标准工作流

### 第 0 步：先做一次自检（离线，验证引擎可用）

```bash
python scripts/venue_monitor.py selftest
```

内置 mock 场馆会模拟「无空余 → 放出一批空余」的过程。
若输出「✅ 通过（检测到 N 条新增空余）」，说明整条链路正常。

### 第 1 步：配置体检

```bash
python scripts/venue_monitor.py doctor -c references/venues.json
```

会列出每个场馆是「✅ 已就绪 / 🧪 mock 演示 / ⚠️ 待抓包」，并**自动解出 JWT 凭证的到期日**。
新场馆在 `request.url` 为空时会被自动跳过，不会报错。

### 第 2 步：抓包（唯一必须人工做的一步）

**先读 `references/recon-playbook.md`**（路线总览 + PC 微信打不开的排查）。

**首选路线：PC 一站式（Charles + 微信 PC 端）** —— 不用碰手机，且有个额外好处：
Charles 本身就是代理，让微信走 Charles 就绕开了坏掉的系统代理，
所以「PC 微信报后端链接异常」很可能同时被治好。
要点：Charles 勾上 `Proxy → Windows Proxy` 接管**系统**代理 →
证书装进**「受信任的根证书颁发机构」**（不是「个人」）。

> ⚠️ **微信 4.x 已移除「设置 → 通用设置 → 网络代理」这一项**，别去那里找。
> 让微信走 Charles 靠三招（按顺序试）：Charles 的 `Windows Proxy` 接管系统代理 →
> 手工设 Windows 系统代理 → **Proxifier 按进程强制转发**（最可靠，不依赖微信配合）。
> 详见 `references/recon-playbook.md` 路线 A。

只有 PC 路线走不通时，才改走手机：照 `references/phone-capture-guide.md` 走。

抓包只需做一次。拿到结果后交给 `curl2venue.py` 自动转配置，**三种交接口径**：

**方式 1（最省事）**：Charles 里 `File → Save Session` 存成 `.chlz`，直接丢给脚本

```bash
python scripts/curl2venue.py --chlz session.chlz \
    --id example-venue --name "xx市体育馆" \
    --miniprogram "#小程序://xx市体育馆/xxxxxxxxxxxxxxxx"
```

`.chlz` 里包含**请求和响应**，所以无需 `--send`。脚本会给所有条目**按「像不像场次列表接口」打分排序**，
自动挑中最像的那条（挑错了用 `--entry <编号>` 指定），并用真实响应推荐字段映射。

**方式 2**：选中目标请求 → 右键 → `Export Session...` → 格式选 `.har`，用 `--har` 传入。

**方式 3（只抓了一条请求）**：右键请求 → `Copy cURL Request` → 存成文件

```bash
python scripts/curl2venue.py --curl-file my-curl.txt \
    --id example-venue --name "xx市体育馆" --send
```

三种方式都会：解析 URL/方法/请求头/请求体 → **自动把日期参数替换成 `${DATE}`** →
推荐 `slots_path` 与 `available_values` 候选值 → 输出可直接粘进 `venues.json` 的配置块。

#### 抓包排错：SSL Proxying 的三层坑（按出现频率排序）

这三层是实测踩出来的，症状都是「抓到的全是 CONNECT 隧道」，但原因完全不同：

| 层 | 症状 | 定位方法 |
| --- | --- | --- |
| ① 总开关没开 | **连 `baidu.com:443` 这类标准站点也没解密** | 看会话里有没有任何明文请求；一个都没有 → 总开关问题 |
| ② Port 填成默认 443 | 标准 443 站点**能**解密，非标端口的业务接口**不能** | 业务接口端口不是 443（如 `8443`）→ Include 规则要写 `*:*` 而不是 `*:443` |
| ③ **Exclude 里有 `*:*`** | 总开关开了、规则也加了，**依然全隧道** | Exclude 优先级**高于** Include，一条 `*:*` 会抵消全部 Include |

③ 最隐蔽。直接用 `--check-charles-config` 读 Charles 的配置文件，一眼看出：

```bash
python scripts/curl2venue.py --check-charles-config
```

> **通用教训**：排查这类问题时，**能直接读软件配置文件就读文件**，不要靠截图逐张推测。
> 另外，**不同时刻采集的样本不要混用**（配置改过之后，旧会话的结论已失效），
> 否则会得出自相矛盾的结论。

另：`.chlz` 是 zip，内部命名在不同 Charles 版本/配置下会变 —— 见过
`N-req.dat` / `N-res.dat` / `N-meta.json`，也见过只有 `N-res.json` 的。
`curl2venue.py` 两种都兼容。

> 用户问「几点放号」时，先说明：公开渠道（政府公告、百科、新闻）只写「开放时间」，
> **不写放号时间**；放号时间是运营在小程序后台配的，只能实测。
> 不要用「通常是 0 点放号」这类通用经验当作该场馆的结论。

> **凭证安全**：抓到的 cURL 含 `Authorization` / `token` / `Cookie`，等同于登录态。
> 生成的配置只保存在本地，不要提交到公开仓库，也不要转发给他人。

### 第 3 步：单次验证

```bash
python scripts/venue_monitor.py check -c references/venues.json \
    -v example-venue -o ./reports
```

- 若解析出 0 条场次 → `response.slots_path` 或 `available_values` 填错了。
- 首次运行只建立基线，不算「新增」；第二次起才会报告增量。

### 第 4 步：加密实测放号时间（需要分钟级精度时用）

```bash
python scripts/venue_monitor.py probe -c references/venues.json \
    -v example-venue \
    --window-from 09:00 --window-to 11:00 --interval 20 --stop-on-first
```

盯住一个「还没放出来」的未来日期，高频轮询，
捕捉「空余 0 → 空余 N」翻转的时刻 = 放号时间。
连续测 2–3 天落在同一时刻附近才可确认。明细写入 `state/probe-*.json`。

### 第 5 步：读放号日志，确定放号时间（推荐路线，无需熬夜）

`check` 每次运行都会记录**「某个日期第一次被观测到已开放预订」的时刻**，
写入 `state/<venue>-release.json`。因此只要让 `check` 每小时跑着，
跑几天就能自动把放号时间测出来 —— 这是最省事的路线。

```bash
python scripts/venue_monitor.py release -v example-venue
```

输出形如：

```
目标日期           首次观测到「已开放预订」
2026-09-18     2026-09-17 09:31:12
2026-09-19     2026-09-18 09:30:48
推断放号时刻：**09:31**（2/2 个日期命中，置信度较高）
```

**精度 = check 的调度间隔**。若想要更精确（分钟级），配合 `probe` 在候选窗口内加密轮询。

#### 冷启动技巧：用订单号时间戳反推放号时间（不用等几天）

**如果响应里带订单号，且订单号内嵌了下单时刻，就能立刻推断放号时间**，
不用等 `release` 跑几天。识别方法：订单号是纯数字且长度固定，
前 12 位按 `%y%m%d%H%M%S` 解出来是**合理且分散**的时间（不是同一秒的假数据）。

实测案例（xx市体育馆，订单号 `2609161217386575` → 2026-09-16 12:17:38）：

| 目标日期 | 该日最早订单时刻 | 推断放号时刻 |
| --- | --- | --- |
| 09-15 | 09-14 09:32:05 | ≤ 09:32 |
| 09-16 | 09-15 09:41:58 | ≤ 09:41 |
| 09-17 | 09-16 09:42:00 | ≤ 09:42 |

→ 结论：**每天上午 09:30 前后放号**（放号时刻 ≤ 最早订单时刻，多日聚类即可收敛）。

再配合「订单量按距最早订单的小时数分桶」，能看出**放号后多久被抢光**：
xx的数据是「0–1 小时内被抢走 50%+」，说明**提醒必须快**。

> 注意：最早订单只是放号时刻的**上界**。若某位长期订场的用户习惯固定钟点下单，
> 该样本会被污染 —— 所以要用**多天聚类**而非单天取值。

#### 调度设计：警惕「整点触发 + 固定时长」留下盲区

调度粒度通常比放号窗口粗得多（如 `FREQ=HOURLY;INTERVAL=1` 只能整点触发）。
若在提示词里写「窗口内加密轮询，最多跑 40 分钟」，就会留下固定盲区：

| 触发时刻 | 加密轮询区间 | 结果 |
| --- | --- | --- |
| 09:00 | 09:00–09:40 | 09:40–10:00 **空档** |
| 10:00 | 10:00–10:40 | — |

而这个空档往往正好落在「放号后被抢最猛」的时段（xx实测：放号后 0–1 小时被抢走 50%+），
等于把最该盯的 20 分钟拱手让出去了。

**正确写法**：加密轮询应「**持续到离开窗口为止**」，而不是「固定跑 N 分钟」。
即每 90 秒一轮 check，直到当前时间超出窗口上界才停。这样任一次整点触发
都会把窗口剩余部分盯满，多次触发自然衔接，不留缝。

> 另注：RRULE 的 `HOURLY` **不支持** `BYHOUR` 限定时段，所以做不到「只在 06:00–23:00 跑」。
> 替代做法是在提示词里加一条「早于 06:00 直接静默退出，不发请求」，
> 既省掉深夜空跑，又不必改动调度本身。

### 第 6 步：落成定时任务

用 `check` 建自动化（`--out-dir` 指向项目内 reports 目录）。

**调度策略（不要全天等频轮询）**：

| 时段 | 间隔 | 为什么 |
| --- | --- | --- |
| 放号窗口前 5 分钟 ~ 放号后 1 小时 | **分钟级** | 这是「新场次放出」的唯一时刻，且放号后 1 小时内被抢光 50%+ |
| 其余时间 | 小时级 | 捡漏（临时退场）用，够用且省请求 |

两个高价值窗口：
1. **放号窗口**（用第 5 步测出；xx实测 ≈ 每天上午 09:30）
2. **每场开赛前 6 小时附近** —— 退款截止线，
   临时退场释放的空位集中在开赛前 6 小时，这是**全天随机发生**的捡漏机会。

## 两种接口形态与两种响应形态

引擎对这两组形态都做了支持，配置即可，不改代码。

### 接口形态 A：按日期查询（最常见）

`request.url` 里用 `${DATE}` 占位，配 `dates.offsets`：

```json
"request": { "url": "https://x.com/api/slots?date=${DATE}" },
"dates": { "offsets": [0,1,2,3,4,5,6,7], "date_format": "%Y-%m-%d" }
```

### 接口形态 B：按「排期号」查询（本 skill 遇到过的进阶形态）

有些接口**不按日期查**，而是按一个「排期 id」（如 `timetableId=1048`）查，
**id 与日期绑定，且只在该日期放号后才存在**（不存在的 id 返回 `code=500`）。
这种情况下无法直接用日期拼 URL，配 `timetable_ids`：

```json
"request": { "url": "https://x.com/api/timetable/details?timetableId=${TIMETABLE_ID}" },
"timetable_ids": {
  "mode": "scan",          // scan=扫区间自动发现日期；map=手工写死 {"2026-09-17": 1048}
  "from": 1030,            // 硬下界
  "to": 1120,              // 硬上界
  "window_before": 4,      // 已探到的最大有效 id 往前扫几个
  "window_after": 10       // 往后扫几个（覆盖未来几天的新增 id）
}
```

- 引擎把扫描结果**按响应里的真实日期归组**，因此「新放出来的日期」会自动被发现，
  并触发 `📅 有日期刚开放预订` 提醒 —— 这就是放号事件的实时信号。
- 已探到的最大有效 id（frontier）会记进 state，下一轮只扫 `frontier ± (4,10)`，
  实测从 91 个请求降到 15 个请求。
- 首次运行或排查时加 `--full-scan` 扫满 `from~to`。
- ⚠️ 这类 id 空间里**通常还留着历史日期**（已过去的日子），必须配
  `watch.skip_past_dates: true`，否则报告和「新增」比对都会被历史数据污染。
- ⚠️ 别指望能推出 id 的线性公式。实测两个产品交替占用 id 且顺序会翻转，
  **扫描 + 按日期归组**比猜公式可靠。

### 响应形态 A：扁平型

`slots_path` 直接指向场次数组，每项 = 一个场地/时段。见 `venues.json` 里的模板。

### 响应形态 B：嵌套型（时段 → 场地）

外层是「时段」，每个时段里挂一个「场地数组」。用 `children_field` 指到内层：

```json
"response": {
  "code_path": "code", "ok_code_values": [200],
  "slots_path": "data.timeList",        // 外层：时段
  "date_field": "bookDate",
  "time_field": "timeFrom", "time_end_field": "timeEnd",
  "children_field": "groundJson",       // 内层：场地
  "child_index_field": "index", "court_suffix": "号场",
  "available_field": "book", "available_values": [0],
  "price_field": "price",
  "unavailable_price_values": [9999]    // 占位价 = 该场地不开放，须排除
}
```

**两个必须警惕的坑**（都是实测踩出来的）：

1. **`code` 藏在 body 里**：HTTP 200 但 `code=500` 表示「该日期/排期还不存在」。
   配 `code_path` / `ok_code_values` 后，引擎会把这类响应当作「无数据」而不是错误。
2. **占位价**：不开放的场地可能返回 `book=0` 但 `price=9999`。
   若只按 `book==0` 判定，会把「不开放」误报成「可抢」。
   必须用 `unavailable_price_values` 排除。

> 校准 `available_values` 的正确方法：抓一次**有空场**的响应和一次**已约满**的响应，
> 对比同一字段的取值差异。该场馆那次是 `book: 0`（空）/ `book: 1`（满），
> 用 200+ 条样本交叉验证过三种组合：`(0, 7.5, 无订单)` / `(1, 7.5, 有订单)` / `(1, 9999, 无订单)`。

## 扩展监测不同的小程序链接

复制 `venues.json` 里 `another-venue` 那个 venue 对象，改 3 处即可：

1. `id` / `name` / `miniprogram` —— 标识与小程序链接
2. `request` —— 抓到的接口地址、请求头、请求体（日期用 `${DATE}` 或 `${TIMETABLE_ID}` 占位）
3. `response` —— `slots_path` 与 `available_field` / `available_values`

引擎无需改动。一个配置文件可同时容纳任意多个场馆，
`check` 不带 `-v` 时会依次监测**全部已启用**（`enabled` 不为 `false`）的场馆。

> ⚠️ **占位符只有四个**：`${DATE}` `${TIMETABLE_ID}` `${TS}` `${VENUE_ID}`。
> 其他形如 `${TOKEN}` 的写法**不会被替换**，会原样发出去，表现为「接口没返回数据」，很难查。
> 凭证请直接粘整串明文。

**已实测**：真实场馆（嵌套型响应 / `${TIMETABLE_ID}` 寻址）与结构完全不同的演示场馆
（扁平响应 / `${DATE}` 寻址）可在同一次 `check` 中各自独立跑通。

📖 **完整指南见 `references/extending-and-sharing.md`** —— 含扩展 5 步、多场馆注意事项
（请求量 / 推送混群 / 放号时间不同怎么合并）、分享给别人时必须替换的 3 样私有信息，
以及「哪些小程序接不了」（客户端签名 / WebSocket / 强风控）。

## 同时监控多个小程序

`venues` 是数组，把每个场馆的 `enabled` 设为 `true` 即可，无需其他改动。
每个场馆的快照、号源扫描前沿、推送、静默期、错误收集**全部独立**，一个场馆挂了不影响其他。

**`enabled` 是真正的开关**：`false` 会被 `check` / `watch` 跳过，字段缺省视为启用。
`-v <id>` 是显式点名，会无视 `enabled`（方便临时调试已禁用的场馆）。
用 `doctor` 看哪些被跳过了：

```
[venue-b] 某某体育馆  ->  ⏸️  已禁用（enabled=false，check/watch 会跳过）
已启用  ：1 / 3（2 个被 enabled=false 跳过）
```

**一次 `watch` 调用覆盖全部已启用场馆**，所以计划任务只需一个，加场馆后不用重跑 `setup-scheduler.ps1`。
各场馆的 `schedule` 段会被自动合并：

| 字段 | 合并方式 |
| --- | --- |
| `poll_windows` | 取并集，**重叠窗口会合并**（A 的 09:00-10:30 + B 的 09:30-11:00 → 09:00-11:00，不会漏掉 10:30-11:00） |
| `poll_interval` | 取最小（最勤快） |
| `lead_minutes` | 取最大（最早预跑） |
| `quiet_before` | 取最小（最早解除静默） |
| `keep_output_days` | 取最小（最严格清理） |

其他注意点：共用同一个 IM 机器人时消息会混群（推送标题里带场馆名可区分）；
场馆数 6+ 建议把 `poll_interval` 调到 120 秒以上。
详见 `references/extending-and-sharing.md` 第三节。

## 配置字段速查

### request

| 字段 | 说明 |
| --- | --- |
| `enabled` | 场馆级开关。`false` = `check`/`watch` 跳过它；缺省视为 `true`。`-v <id>` 点名时会无视此字段 |
| `request.url` | 接口地址；为空则该场馆被视为「待抓包」自动跳过 |
| `request.method` | GET / POST；GET 时 `body` 会自动拼成 query string |
| `request.headers` | 必要请求头（Authorization / JAuthorization / Referer / Content-Type / UA） |
| `request.body` | 请求体，支持 `${DATE}` `${TIMETABLE_ID}` `${TS}` `${VENUE_ID}` 占位 |
| `request.mock` | `true` 时走内置模拟数据源（离线演示） |
| `request.proxy` | 指定代理；`--no-proxy` 可强制绕过系统代理 |

### timetable_ids（接口形态 B 专用）

| 字段 | 说明 |
| --- | --- |
| `mode` | `scan` 扫区间自动发现日期；`map` 手工写死 `{"2026-09-17": 1048}` |
| `from` / `to` | 扫描硬下界 / 硬上界 |
| `window_before` / `window_after` | 相对已探到前沿的扫描半径（默认 4 / 10） |
| `stale_hours` | 状态超过这么久没更新就判定「过期」，按时间跨度一次性跳过缺口（默认 12 小时） |
| `id_per_day` | id 空间每天大约前进多少个（默认 2），用于估算过期后该跳多远 |

> **为什么需要 `stale_hours`**：id 每天都在往前长，而每轮只前进 `window_after` 个。
> 停用几天后 frontier 就再也追不上 —— 实测停用 24 天（id 前进约 48）而 `window_after=10`，
> 原本要连续跑 5 轮（5 小时）才恢复，而这段空窗可能正好覆盖上午的放号窗口，等于白盯。
> 有了这个保护，恢复后的**第一轮**就能直接跨到当前前沿。

### response

| 字段 | 说明 |
| --- | --- |
| `slots_path` | 场次数组位置，点号分隔，支持 `*` 通配一层 |
| `flatten_dict_lists` | 响应是「日期→数组」映射时置 `true` |
| `code_path` / `ok_code_values` | 业务码位置与合法值（`code=500` 视为无数据而非错误） |
| `date_field` | 日期字段名（号源扫描时**必填**，引擎靠它归组） |
| `time_field` / `time_end_field` | 起止时间字段；两个都给则拼成 `09:00-09:30` |
| `children_field` | **嵌套型必填**：时段里挂场地数组的字段名 |
| `child_index_field` | 内层场地编号字段（配 `court_suffix` 拼成「7号场」） |
| `available_field` / `available_values` | **判定「有空场」的关键**，需用真实响应校准 |
| `unavailable_price_values` | 占位价（如 `9999`）= 不开放，即便 `available` 也排除 |
| `price_field` / `court_field` / `court_prefix` / `court_suffix` | 价格与场地名。`court_prefix`/`court_suffix` 会按需补到场地名前后（如 `12` → `12号场`），**两者各自独立判断**，只配其中一个也生效 |

### dates / watch / report / notify

| 字段 | 说明 |
| --- | --- |
| `dates.offsets` | 相对今天的天数偏移，如 `[0,1,2,...,7]`（接口形态 B 下留空） |
| `dates.date_format` | 日期格式，须与抓到的请求参数格式一致 |
| `watch.skip_past_dates` | **号源扫描必开**：过滤掉 id 空间里残留的历史日期 |
| `watch.slot_minutes` | 只盯特定时长的场次，如 `[30]`；留空 = 全部 |
| `watch.time_windows` | 只关注这些时段，如 `["18:00-22:00"]`；留空=全部。判定是「**场次完整落在窗口内**」，所以 `["20:00-20:30"]` 只命中那一段，不会带上 20:30-21:00 |
| `report.mode` | `flat`=一场一行；`group`=按「时长+时段」聚合成一行（场地多时用） |
| `state_dir` | 状态目录，相对配置文件所在目录解析；默认 `../state` |
| `notify.webhook_url` | 可选：推送地址，仅在有新增空余时推送。可只填密钥（serverchan / pushplus / bark 会自动补全 URL） |
| `notify.type` | `wecom`（企业微信/钉钉）/ `feishu` / `serverchan` / `pushplus` / `bark` / `generic` |
| `notify.daily_limit` | 每日推送上限。`serverchan` 默认 **5**（免费版限额，超出会静默失败）；`0` = 不限 |
| `notify.feishu_secret` | 飞书机器人开了「签名校验」时必填，引擎会自动带 `timestamp` + `sign` |
| `notify.max_slots_in_message` | 单条消息最多列几个**时段行**，默认 12（同一时段的多个场地算一行）。再多就把消息撑爆了 |
| `notify.daily_digest_at` | 如 `"10:35"`。设了就每天在该时刻补发一条「当前空场一览」摘要（当天只发一次）。作用是让你知道**系统还活着** —— 告警只在有新空位时才推，安静时无法区分「没变化」和「程序挂了」。留空 = 不启用 |
| `notify.include_report_path` | 默认 `false`：**不**把本地报告路径塞进消息（手机上点不开、看不懂）。设 `true` 才带 |
| `notify.no_proxy` | 默认 `true` = **直连**。见下方「代理陷阱」，一般不要改 |
| `request.no_proxy` | 同上，作用于业务接口请求。建议设为 `true` |

## 子命令一览

| 命令 | 作用 |
| --- | --- |
| `selftest` | 离线自检，用 mock 场馆跑通全链路（不联网） |
| `doctor` | 配置体检：已就绪 / 待抓包 + **JWT 凭证到期日与剩余天数** |
| `check` | 拉取 + 比对 + 报告；同时记录放号观测 |
| `probe` | 高频轮询，捕捉「空余 0 → 空余 N」翻转时刻 |
| `release` | 打印放号观测日志并推断放号时刻 |
| `notify-test` | 给配置的 webhook 发一条测试消息，验证提醒通道真的通 |
| `set-token` | 换接口凭证：从 cURL / `.chlz` / 直接粘贴取值，写回配置后自动实测验证，失败自动回滚 |
| `watch` | **系统级调度入口**：窗口内加密轮询、窗口外只跑一次。给计划任务 / cron 调，不依赖 WorkBuddy |
| `status` | **调度自检**：最近几次运行的时间与退出码、快照更新时间，用来发现「静默失效」 |


`check` 常用开关：`-v <场馆>` 只测一个；`-o <目录>` 落报告；`--full-scan` 扫满号源区间；
`--no-notify` 只查不发（跳过变更推送与每日摘要，但**照常更新快照**）。

> `--no-notify` 的典型用法：长时间停用后恢复，先用它跑一次**重建基线** ——
> 否则基线里全是过期日期，恢复后的第一轮会把「当前所有空场」都误报成「新增」，
> 推一条误导性的大消息。用它跑过一次之后，下一轮就只推真正的新变化。

**长期挂机请用 `watch` + 系统计划任务，而不是 WorkBuddy 自动化** ——
自动化依赖客户端处于运行状态，客户端一关就不再触发。
`watch` 自己不常驻（一次调用最多盯完一个窗口就退出），外层只要「每小时调一次」：

```bash
python scripts/venue_monitor.py watch -v example-venue -o <报告目录>
```

它会按 `venues.json` 里 `schedule` 段的配置自行决定「静默 / 只跑一次 / 等到窗口再轮询」，
调度逻辑在代码里而不是提示词里。完整部署与运维见 `references/scheduler-setup.md`。

`set-token` 用法（凭证过期或服务端开始鉴权时用）：

```bash
# 最省事：在 Charles 里右键那条业务请求 -> Copy cURL Request，然后：
python scripts/venue_monitor.py set-token -v example-venue --from-curl "curl 'https://...' -H 'JAuthorization: eyJ...'"
# 或从导出的抓包文件里自动提取：
python scripts/venue_monitor.py set-token -v example-venue --from-chlz 会话.chlz
# 或直接粘贴凭证字符串：
python scripts/venue_monitor.py set-token -v example-venue --token "eyJ..."
```

它只替换配置里那一行（换行/BOM/缩进都不动，diff 干净），并且：
先亮出新凭证的到期日 → 写回（留 `.bak` 备份）→ **实测验证** → 失败自动回滚。
加 `--dry-run` 只看不写，`--no-verify` 跳过实测。

`scripts/curl2venue.py`（独立小工具，配合抓包用）：

| 参数 | 作用 |
| --- | --- |
| `--check-charles-config` | **排查抓包问题时用**：直接读 Charles 的 `charles.config`，打印 SSL Proxying 的 Include / Exclude 规则并报警 |
| `--diagnose` | **排查抓包问题时用**：逐主机打印解密状态表，直接告诉你卡在哪一层 |
| `--chlz` | **最省事**：Charles 会话文件（`.chlz` / `.chls`），可重复传多个；自动打分挑目标请求 |
| `--har` | Charles 导出的 `.har`，同样自动打分挑请求 |
| `--entry` | 手工指定条目编号（自动挑错时用） |
| `--curl-file` / `--curl` | 只抓了一条请求时，用 `Copy cURL Request` 的结果 |
| `--send` | 实打一次接口，推荐 `slots_path` / `available_values`（cURL 模式用） |
| `--response-file` | 已有响应 JSON 时，只做路径推荐（不联网） |
| `--no-proxy` | 绕过系统代理（本机有代理时常用） |
| `--out` | 把生成的配置块写入文件 |

> **未解密时会明确报错**：若会话里全是 TLS 隧道，脚本会识别出来并打印
> 「SSL Proxying 没生效」及修复步骤，而不是给你一份错误的配置。

## 「一有空场就立刻提醒」的完整链路与三个硬约束

当用户要求**实时/立即提醒**时，先说清这条链路的四个环节，缺一不可：

```
① 接口（必须抓包）→ ② 高频轮询 → ③ 变更检测 → ④ 即时推送（webhook）
```

**① 接口是前提，无法绕过。** 「有没有空场」这个状态只存在于场馆后端的返回数据里。
即使已经知道放号时间，那也只解决了「什么时候去看」，没解决「怎么看」。
没有接口，程序就没有眼睛，只能靠人手动打开小程序。

> 常见误解：「我知道几点放号了，是不是就不用抓包了？」
> 答：**知道时间 ≠ 能自动监测**。时间决定轮询的触发点，接口决定轮询的内容。
> 唯一不需要抓包的替代方案是「到点提醒你自己去抢」—— 那是定时提醒，不是监测。

**② 轮询间隔 = 提醒延迟，且与风控风险成反比。**

| 间隔 | 最坏延迟 | 说明 |
| --- | --- | --- |
| 60 分钟 | 1 小时 | 几乎无风控风险，但抢不到热门场 |
| 5–10 分钟 | 10 分钟 | 较平衡 |
| 1 分钟 | 1 分钟 | 接近实时，但需注意限流 |

小程序**没有推送能力**，无法让场馆后端主动通知你，本质只能主动去问。
所以「实时」实际是「准实时」，下限由轮询间隔决定。
**结论：不要全天等频轮询，按第 6 步的「窗口化调度」做。**

**③ 变更检测已内置。** `check` 会把当前空余与上次快照比对，只报告**新增**，
不会每次都把全部空余重复推给你。首次运行只建基线，不推送。

> ⚠️ **推送只在「跑 check」那一刻发生 —— 没有调度就没有提醒。**
> 引擎是纯被动的：`notify()` 的**唯一**调用点在 `check` 里（`probe` / `release` /
> `doctor` / `set-token` 都不推）。所以「飞书收到消息」这件事，等价于「有人跑了 check」。
> 由此有两条容易误判的推论：
> - **关掉自动化任务 → 推送立即彻底停止**，因为再没有任何东西去跑 `check`。
> - 自动化由**本地客户端**调度，客户端没运行（退出 / 关机 / 休眠）时同样不会触发 ——
>   也就是说「飞书安静」不等于「没有空位」，可能只是**没跑**。
> - 要「不依赖客户端也持续推送」：用 `watch` 子命令 + 系统级计划任务
>   （Windows 任务计划程序 / macOS launchd / Linux cron），部署见 `references/scheduler-setup.md`。
>   这是长期挂机的**推荐方式**。

**④ 推送通道必须自检。** 检测到了但没人知道，等于没做。
配置 webhook 后用 `notify-test` 验证：

```bash
python scripts/venue_monitor.py notify-test -v example-venue
```

支持的渠道（**各家 webhook 报文格式互不兼容，这是「配了但收不到」的最常见原因**）：

| `notify.type` | 渠道 | 报文格式 | 备注 |
| --- | --- | --- | --- |
| `wecom` | 企业微信群机器人 / 钉钉群机器人 | `{"msgtype":"text","text":{"content":...}}` | 两者格式一致，可共用 |
| `feishu` | 飞书群机器人 | `{"msg_type":"text","content":{"text":...}}` | |
| `serverchan` | Server酱 | `{"title":...,"desp":...}` | **没有企业微信的个人首选**，推到自己的微信；可只填 SendKey。⚠️ 免费版 **5 条/天**（引擎已内置配额保护）。**两个版本 SendKey 不通用**：`SCT…`→`sctapi.ftqq.com`，`sctp{uid}t…`→`{uid}.push.ft07.com`，引擎按前缀自动识别 |
| `pushplus` | PushPlus | `{"token":...,"title":...,"content":...}` | 推到自己的微信；可只填 token |
| `bark` | Bark | `{"title":...,"body":...}` | 推到 iPhone；可只填 key |
| `generic` | 通用 webhook | `{"title":...,"text":...}` | 自建服务用 |

> **飞书有个必做步骤**：自定义机器人要配「安全设置」（自定义关键词 / IP 白名单 / 签名校验），
> 三选一。选「自定义关键词」时，关键词必须**原样出现在消息文本里** ——
> 建议填 `xx` / `空余` / `场次` 这类必然出现的词，填「提醒」可能匹配不上。
> 选「签名校验」则把密钥填进 `notify.feishu_secret`。
> 漏配的表现是「HTTP 200 但收不到」，错误码 `19024 Key Words Not Found`。

> 企业微信群机器人的取地址路径：**群设置 → 群机器人 → 添加 → 复制 Webhook 地址**。
> ⚠️ 必须是「**企业内部群**」—— 含外部联系人的群**没有**机器人入口，
> 这是最常见的卡点。若企业管理员禁用了群机器人，也会看不到该入口。
>
> **各渠道的完整开通步骤、门槛对比、排错表见 `references/notify-setup.md`。**
> 一句话结论：企业微信/钉钉/飞书都要「先有一个群」，个人用户往往没有 ——
> **只想让自己手机收到提醒的话，Server酱（微信扫码即用）比企业微信省事得多。**

#### 代理陷阱（本机实测踩到，值得单独说）

很多开发机设了全局 `http_proxy`（比如指向 Charles / Fiddler 之类的调试代理）。
Python 的 `urllib` **会自动读取这个环境变量**，于是：

- 调试代理一关，**监测和推送就一起静默失败**（请求发不出去）；
- 更糟的是它看起来像「场馆接口挂了」，排查方向会跑偏。

所以本 skill 的配置里默认 **`no_proxy: true`（直连）**：

```json
"request": { "no_proxy": true },
"notify":  { "no_proxy": true }
```

> 只有当你所在的**公司网络必须走代理**才能出网时，才把它改成 `false`，
> 并用 `notify.proxy` / `request.proxy` 指定真实代理地址。
>
> 命令行排查时对应 `--no-proxy` 开关。验证方法：
> 把 `http_proxy` 指向一个死端口再跑 `check`，若仍能取到数据，说明直连生效。

## 已知约束与注意事项

- **Windows 环境无法用浏览器自动化打开小程序**；本 skill 不依赖浏览器，靠接口抓包。
- **PC 微信打不开小程序（报「后端链接异常」）**：说明前端已加载、后端请求失败，
  通常不是「不支持 PC」，而是 PC 走了系统代理被拦。先关代理再试；
  无效则改走手机抓包（iOS 成功率最高）。详见 `references/recon-playbook.md`。
- **凭证可能过期，但先别急着认命**：JWT 带 `exp`，`doctor` 会自动解出到期日与剩余天数
  （xx那次的 token 有效期是 **15 天**）。但**「token 会过期」不等于「监测会断」** ——
  很多场馆的**只读查询接口压根不校验凭证**。xx实测：正常凭证 / 乱填 `garbage` /
  已过期 / **完全不传该 header**，四种请求全部返回 `code=200`。
  所以拿到凭证后，第一件事是**做一次对照实验**，别白折腾抓包：

  ```bash
  # 不带凭证直接打一次，能通就说明这接口不鉴权 —— 此后永远不用管 token 过期
  python -c "…"   # 或直接看 set-token 的输出：它会自动做这个匿名探针
  ```

  `set-token` 内置了这个探针，会把结果分三态告诉你：
  `verified`（接口会鉴权，新凭证确实可用）/ `anonymous`（接口不鉴权，凭证写错也不影响）/
  `failed`（凭证无效，自动回滚）。**注意别只看「能打通」就以为凭证是对的** ——
  在免鉴权接口上，乱填的凭证一样"能打通"。
  真遇到鉴权失效（返回 401/403）时，用 `set-token` 换凭证即可，不必重走抓包流程。
- **只抓自己的请求**：仅在自己的设备与账号上抓自己的预约请求，不采集他人数据。
- **不做自动下单**：自动占场可能触发场馆「单月取消 4 次即终止权限」的风控，
  也可能违反平台规则。本 skill 的边界是「发现空位 → 提醒人」。
- **系统代理会让监测静默失败**：`urllib` 会自动读 `http_proxy` / `https_proxy`，
  若它指向本机的调试代理（Charles 等），代理一关，监测和推送就一起发不出去。
  配置里默认 `no_proxy: true` 直连；排查时对应 `--no-proxy`。详见上方「代理陷阱」。
- **推送渠道的报文格式不通用**：企业微信/钉钉、飞书、Server酱、PushPlus、Bark 各不相同，
  配错 `notify.type` 的表现是「HTTP 200 但收不到消息」。用 `notify-test` 验证，
  它会打印实际发出的渠道、目标地址（密钥已打码）和代理模式。
