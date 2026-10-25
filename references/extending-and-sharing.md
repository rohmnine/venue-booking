# 扩展到别的小程序 · 同时监控多个 · 分享给别人

> **三个问题的短答**
>
> | 问题 | 短答 | 代价 |
> | --- | --- | --- |
> | 能扩展到别的小程序吗？ | **能，而且不用改代码。** 全站差异都收敛在 `venues.json`，加一段配置即可 | 一次抓包（唯一必须人工做的步骤） |
> | 能同时监控多个吗？ | **能。** `venues` 是数组，一次 `check` 遍历全部已启用场馆，各自独立快照、独立推送。已实测 | 每轮请求数 = 场馆数 |
> | 分享给别人怎么用？ | 把 skill 目录整个给对方，**必须替换三样私有信息**：接口凭证、推送 webhook、脚本里的绝对路径 | 对方要自己抓一次包 |

---

## 一、为什么能扩展到别的小程序

不是「碰巧能改」，是设计上就把差异挤到了三个收敛点。

### 1.1 引擎里没有一行场馆业务逻辑

实测核查：在引擎 `venue_monitor.py` 里搜 `groundJson` `timeList` `JAuthorization` `book`
等场馆专属字段名 —— **所有命中全部出现在注释和文档字符串里，没有一处进入代码路径**。

也就是说：任何一个具体场馆对引擎而言，只是配置里的一条数据，
和内置的 `demo-venue` 没有地位差别。

### 1.2 差异只有四类，各对应配置里的一段

| 差异 | 对应配置段 | 引擎怎么处理 |
| --- | --- | --- |
| 请求怎么发 | `request` | 地址/方法/请求头/请求体全部声明式；`${DATE}` `${TIMETABLE_ID}` `${TS}` `${VENUE_ID}` 四个占位符递归替换（url、headers、body 都替换） |
| 响应怎么读 | `response` | `slots_path` 点号路径定位场次数组，支持 `*` 通配一层；`children_field` 处理嵌套型 |
| 盯哪些场次 | `watch` / `dates` | 时长、时段窗口、日期偏移 |
| 推到哪 | `notify` | 6 种 IM 通道，每场馆独立 |

### 1.3 已实测：异构场馆同轮跑通

拿「真实场馆（嵌套型响应、3000+ 条原始场次、`${TIMETABLE_ID}` 寻址）」和「演示场馆（mock 数据源、扁平响应、`${DATE}` 寻址）」放进同一个配置跑一次 `check`：

```
==============================================================
监测：xx市体育馆 (example-venue)
==============================================================
命中号源 17 个（共探测 25），原始场次 3000+ 条，过滤后 48 条，空余 2 条，覆盖日期 1 个（首次运行，建立基线）

==============================================================
监测：演示场馆（异构：接口形态完全不同） (demo-venue)
==============================================================
命中号源 6 个（共探测 6），原始场次 72 条，过滤后 24 条，空余 10 条，覆盖日期 6 个（首次运行，建立基线）
```

两者接口形态、响应形态、寻址方式、数据量全都不同，同一份配置、同一次执行、各自独立落盘。

---

## 二、扩展步骤（5 步）

### 第 1 步：抓包（唯一必须人工做的一步）

按 `recon-playbook.md`（电脑端）或 `phone-capture-guide.md`（手机端）走一遍，目标是**拿到那条业务接口请求**，需要记录：

| 抓包要拿到的东西 | 用来填 |
| --- | --- |
| 完整请求 URL | `request.url` |
| 请求方法 | `request.method` |
| 请求头（尤其凭证） | `request.headers` |
| 请求体（POST 时） | `request.body` |
| 响应 JSON 全文 | 校准 `response.*` 各字段 |

**只抓一次就够。** 拿 `Copy as cURL` 最省事，后面可以直接喂给 `curl2venue.py` 自动生成配置。

### 第 2 步：复制模板

打开 `references/venues.json`，复制 `another-venue` 那个对象（它本身就是为扩展准备的模板），改 `id` / `name` / `miniprogram`。

### 第 3 步：填 `request`

把抓到的 URL、headers、body 原样粘进去。有两点要判断：

- **日期是查询参数还是排期号？**
  - 请求里有明确的日期参数（`date=2026-09-17`）→ 用 `${DATE}`，填 `dates.offsets`
  - 请求里是个跟日期绑定、且只在该日放号后才存在的 id（`timetableId=1048`）→ 用 `${TIMETABLE_ID}`，填 `timetable_ids`（模式 B，见下）
- **凭证字段叫什么？** 原样照抄抓包里的字段名，别改。

### 第 4 步：填 `response`

这是最容易填错的一步，必须拿**真实响应 JSON** 校准。按顺序问自己：

1. 场次数组在响应 JSON 的第几层？→ `slots_path`（如 `data.timeList`、`data.slots`、`data.list`）
2. 数组里每个元素是**一场**，还是**一个时段（里面挂多个场地）**？
   - 一场 → 扁平型，填 `time_field` / `available_field` / `court_field` / `price_field`
   - 时段挂场地 → 嵌套型，额外填 `children_field`（场地数组的字段名）+ `child_index_field`（场地编号字段）
3. 哪个字段表示「能订」？→ `available_field` + `available_values`（**必须实测校准**，别猜）
4. 有没有「占位值」表示不开放？（常见是 `price=9999`）→ `unavailable_price_values`

> **校准技巧**：抓包响应里挑一条**确定能订**的和一条**确定不能订**的，对比哪个字段不同。这个字段就是 `available_field`，两个值分别进 `available_values` 和排除列表。

> **加速器**：`curl2venue.py` 带 `--send` 时会**真的打一次接口**，然后根据真实响应推荐 `slots_path` / `available_values`。比自己猜快得多：
> ```bash
> python scripts/curl2venue.py --curl-file 抓包.txt --id my-venue --send --out 建议配置.json
> ```
> 推荐结果仍需人工核对（引擎的推荐是启发式的，不保证对），但通常能省掉大半试错。

### 第 5 步：验证

```bash
# 1) 配置体检：看新场馆是否被正确解析
python scripts/venue_monitor.py doctor

# 2) 单次试跑，只跑新场馆（-v 是「只处理这个场馆 id」，不是 verbose）
python scripts/venue_monitor.py check -v <新场馆id>

# 3) 确认「空余数」和你在小程序里肉眼看到的数量对得上
```

数量对不上就回第 4 步重新校准 `available_field` / `available_values` / `unavailable_price_values`。

---

## 三、同时监控多个小程序

### 3.1 怎么开

`venues` 是**数组**，把每个场馆的 `enabled` 设为 `true` 即可。什么都不用额外做：

```bash
python scripts/venue_monitor.py check          # 遍历全部已启用场馆
python scripts/venue_monitor.py check -v abc   # 临时只跑一个
python scripts/venue_monitor.py watch          # 调度入口，同样覆盖全部已启用场馆
```

已实测：

- 3 个场馆一次 `check` 全部处理，各自生成独立快照（`demo-venue.json` / `demo-venue-2.json` / `demo-venue-3.json`）
- 真实场馆（嵌套型响应 / 3000+ 条原始场次）与演示场馆（扁平响应）**同一次执行**各自跑通
- `enabled: false` 的场馆被正确跳过；`-v` 显式点名时则无视 `enabled`
- 2 个场馆各写各的 `poll_windows`（`08:50-10:30` + `09:30-11:00`），
  一次 `watch` 合并成 `08:50-11:00`，间隔取两者最小的 60 秒

### 3.2 每个场馆是完全独立的

| 维度 | 是否独立 |
| --- | --- |
| 快照与「新增空余」比对基线 | ✅ 各自一个 `<id>.json` |
| 号源扫描前沿（`frontier`） | ✅ 各自记录，互不干扰 |
| 推送 | ✅ 各自配置；某个场馆没配 `notify` 就不推 |
| 静默期 / 每日推送上限 | ✅ 各自计数 |
| 出错 | ✅ 逐场馆收集 `errors`，一个场馆挂了不阻断其他场馆 |

### 3.3 多场馆要注意的四件事

**① 请求量翻倍。** 每个场馆每轮都要打自己的接口。场馆越多，单轮耗时越长。经验值：

| 场馆数 | 建议 `poll_interval` | 说明 |
| --- | --- | --- |
| 1–2 | 60–90 秒 | 单轮 1–3 秒，间隔基本就是纯等待 |
| 3–5 | 90–120 秒 | 单轮 3–8 秒，仍很轻松 |
| 6+ | 120 秒以上 | 考虑错开 `poll_windows`，或拆分到不同机器 |

> 多场馆共用一份 `watch` 时，`poll_interval` 取**各场馆的最小值**（最勤快）。
> 想让某个场馆跑得勤、其他跑得慢，目前做不到 —— 间隔是全局的。

**② 推送会混进同一个群。** 如果多个场馆共用一个飞书/企业微信机器人，消息会挤在一起。三个办法：
- 给每个场馆配**不同的机器人**（不同群）
- 或者共用一个群，靠消息标题区分 —— 引擎的推送标题里**带场馆名**，扫一眼就知道是哪个
- 或者只给最在意的场馆配 `notify`，其他场馆只落盘不推送

**③ 计划任务只需要一个。** 一次 `watch` 调用会覆盖**全部已启用场馆**，不用给每个场馆建一个任务。加场馆后**不需要**重跑 `setup-scheduler.ps1`。

**④ 各场馆放号时间不同 —— 各写各的。** 每个场馆在自己的 `schedule` 段里写自己的窗口，引擎会自动取并集：

```jsonc
// 场馆 A：上午 09:30 放号
"schedule": { "poll_windows": ["08:50-10:30"] }

// 场馆 B：下午 14:00 放号
"schedule": { "poll_windows": ["13:20-15:00"] }
```

合并结果：`08:50-10:30` 与 `13:20-15:00` 两段，各自加密轮询。

重叠窗口会被**合并**。比如 A 写 `09:00-10:30`、B 写 `09:30-11:00`，合并成 `09:00-11:00` ——
不会出现「盯到 10:30 就停、把 B 的 10:30-11:00 漏掉」。

其余三个字段的合并规则（都取更保守的一侧，宁可多跑也不漏）：

| 字段 | 合并方式 | 理由 |
| --- | --- | --- |
| `poll_interval` | 取**最小** | 最勤快 |
| `lead_minutes` | 取**最大** | 最早开始预跑 |
| `quiet_before` | 取**最小** | 最早解除静默 |
| `keep_output_days` | 取**最小** | 最严格清理磁盘 |

### 3.4 `enabled` 字段才是开关

**`enabled` 为 `false` 的场馆会被 `check` / `watch` 跳过**；字段缺省视为启用（兼容老配置）。

```jsonc
{ "id": "venue-a", "enabled": true  }   // 参与监测
{ "id": "venue-b", "enabled": false }   // 跳过（模板、暂不关心的场馆）
{ "id": "venue-c" }                     // 缺省 = 参与监测
```

`-v <id>` 是**显式点名**，会无视 `enabled` —— 所以临时调试一个已禁用的场馆仍然可以：

```bash
python scripts/venue_monitor.py check -v venue-b
```

用 `doctor` 可以一眼看出哪些被跳过了：

```
[venue-b] 某某体育馆  ->  ⏸️  已禁用（enabled=false，check/watch 会跳过）
就绪场馆：1 / 3
已启用  ：1 / 3（2 个被 enabled=false 跳过）
```

> 如果所有场馆都是 `enabled=false`，`check` / `watch` 会直接报错退出，
> 而不是静默什么都不做 —— 静默失效是最难查的故障。

---

## 四、分享给别人，对方怎么用

### 4.1 必须先替换的三样私有信息

直接把自己这份配置给别人，**对方大概率跑不通，而且有隐私风险**。

| # | 私有信息 | 在哪 | 为什么必须换 |
| --- | --- | --- | --- |
| 1 | **接口凭证** | `venues.json` → `request.headers` 里的 `JAuthorization` / `Authorization` | 那是**你账号**的登录态。给别人 = 把账号借出去；而且对方查的是你的账号数据 |
| 2 | **推送 webhook** | `venues.json` → `notify.webhook_url` + `notify.feishu_secret` | 那是**你的群**。对方一跑，消息全发到你群里；反过来对方也收不到 |
| 3 | **绝对路径与用户名** | `venue-check.bat`（Python 路径、skill 路径、输出目录）、`setup-scheduler.ps1`（用户名） | 里面写死了 `C:\Users\<你的用户名>\...`，对方机器上不存在 |

另外 `venues.json` 里还有你的 `state_dir`、输出目录，以及该场馆的备注信息（含场馆地址、开放规则等），按需保留或删掉。

> **最小化分享的做法**：不要给整份 `venues.json`，只给 `another-venue` 那段**空模板** + 文档。让对方自己抓包填。

### 4.2 对方的完整上手路径

```
① 拿到 skill 目录，放到自己的 skills 目录下
      ↓
② 确认 Python 可用，跑一次离线自检
   python scripts/venue_monitor.py selftest
      ↓
③ 读 references/recon-playbook.md，抓自己想监控的小程序
   （手机端看 phone-capture-guide.md）
      ↓
④ 用 curl2venue.py 把抓包结果转成配置，或手工填 another-venue 模板
   python scripts/curl2venue.py --curl-file <保存了 cURL 的文本文件> --id my-venue --send
      ↓
⑤ python scripts/venue_monitor.py doctor     # 看配置是否被正确解析
   python scripts/venue_monitor.py check -v my-venue       # 单次验证
      ↓
⑥ 配推送：读 references/notify-setup.md，建自己的机器人，
   填 notify.webhook_url，然后
   python scripts/venue_monitor.py notify-test -v my-venue   # 测通道
      ↓
⑦ 部署调度：改 scripts/venue-check.bat 里的绝对路径 + 用户名，
   再跑 scripts/setup-scheduler.ps1
      ↓
⑧ python scripts/venue_monitor.py status     # 确认调度在跑
```

### 4.3 对方需要自备的东西

| 需要什么 | 说明 |
| --- | --- |
| **Python 3.9+** | 无第三方依赖，标准库即可 |
| **Windows**（当前调度脚本） | `venue-check.bat` / `setup-scheduler.ps1` 是 Windows 专用。Linux/macOS 把 `watch` 挂到 cron 或 systemd timer 即可，引擎本身跨平台 |
| **一台常开的机器** | 这是硬约束 —— 发起方必须在线。关机 / 睡眠 = 停止监测（见 `scheduler-setup.md` 二·五） |
| **一个 IM 机器人** | 飞书 / 企业微信 / 钉钉 / Server酱 / PushPlus / Bark 任选，见 `notify-setup.md` |
| **目标小程序的访问能力** | 得能打开那个小程序、能抓包 |

### 4.4 分享时的风险提示（建议一并转达）

- **别把带凭证的配置发到公开仓库 / 群里。** `venues.json` 的 `_readme` 里已经写了这条警告。
- **抓包得到的凭证是账号级的。** 别人拿到能查你的订单和排期。
- **默认只读**：`check` / `watch` 只做查询与提醒。
  自动下单是**可选功能**（`order` 子命令 / `order.auto`），转发时请一并说明。
- **自动下单会排他性占用公共场馆的稀缺资源**，分发前请想清楚 —— 详见下一节。
- **别用来做高频轰炸。** 90 秒一轮已经很密了，再密有被风控的风险。

---

## 五、什么时候扩展不了

不是所有小程序都能接。遇到下面几种，直接放弃：

| 情况 | 为什么不行 |
| --- | --- |
| 接口带**客户端签名**（`sign` 由小程序内部算法生成，且密钥藏在代码里） | 请求没法伪造。除非逆向出签名算法，成本远超收益 |
| 接口走 **WebSocket / 长连接**推送 | 当前引擎是 HTTP 轮询模型 |
| 有**强风控 / 验证码 / 设备指纹** | 轮询会被识别并封禁 |
| 数据**只在客户端渲染**、接口返回加密串 | 拿不到结构化 JSON 就没法定位场次数组 |
| 需要**小程序内登录态**且凭证短时效（几分钟） | 手工抓包跟不上，得自动化登录，复杂度陡增 |

**判断方法**：抓包时如果请求里有看不懂的 `sign` / `nonce` / `encrypt` 参数，且删掉它就报错，那就是第 1 种，收手。

---

## 六·五、要不要开源 / 公开分发

这是个需要认真想的问题，因为**这个工具的两部分风险完全不同**。

### 两部分要分开看

| 部分 | 性质 | 分发风险 |
| --- | --- | --- |
| **监测**（`check` / `watch` / 抓包工具 / 文档） | 只读查询公开可订信息 | **低**。不改变任何资源分配，对他人无影响 |
| **自动下单**（`order` / `order.auto`） | 对稀缺公共资源产生排他性占用 | **高**。一个人用是「自己抢自己的场」；一万个人用就变成「脚本党挤压普通人」 |

**自己用 vs 公开分发，是两件性质不同的事。** 前者完全合理；后者要意识到
「工具会被怎么用」不取决于作者的本意。

### 建议的做法

**开源监测部分，自动下单部分留在本地或做成用户自己实现的扩展。**

具体：

1. **拆成两层**：公共仓库只放引擎 + 抓包工具 + 文档；`order` 相关代码作为可选扩展，
   由使用者自己按 `recon-playbook.md` 第七节抓包实现。
2. **只发 mock 场馆**，不要带任何真实场馆的接口地址与 id。
   让使用者自己抓 —— 既避免暴露别人家的私有接口，也避免「一键就能刷」的滥用门槛。
3. **绝不接受带真实凭证的 PR / issue**。`venues.json` 里的 `JAuthorization` 是账号级的。
4. **加 LICENSE + 免责声明**，明确「按现状提供、不担保、使用者自担风险」。
5. **说明平台规则风险**：自动化操作可能违反场馆服务条款，存在账号被风控的可能。

### 会被问到的几个问题

**「别人抓不到包怎么办？」**
`recon-playbook.md` + `phone-capture-guide.md` 已经把三条路线写全了，
`curl2venue.py` 能直接从 `.chlz` / `.har` / cURL 生成配置。门槛主要在「愿意动手」，不在技术。

**「接口被改了怎么办？」**
每个场馆的接口都是对方私有的，随时可能变。开源意味着你要长期维护一堆场馆配置 ——
这也是建议「只发引擎、场馆由用户自己抓」的原因之一：维护责任分散。

**「会不会导致场馆加风控，把我也封了？」**
有可能。使用面越广，被识别、被加验证码、被改接口的概率越高。
这也是建议**限制分发范围、保持轮询克制**的现实理由（不只是道德理由）。

### 怎么构建「只有监测」的开源版

已经做成一条命令 —— **单一份源码，构建时按标记剥离**，不需要维护两份代码：

```bash
python scripts/make-share-package.py --monitor-only --out share-public --zip
```

它会：

| 处理对象 | 做法 |
| --- | --- |
| `venue_monitor.py` | 剥掉 `ORDER-ONLY` 标记之间的全部下单实现 + watch 里的钩子 + CLI 的 `order` 子命令 |
| `SKILL.md` / `recon-playbook.md` | 剥掉「下单接口怎么抓」「order 子命令」等章节 |
| `venues.json` | 不含任何 `order` 配置；`_readme` 里加一句「本版本不含自动下单」 |

**剥完会自检**：只要还残留 `def cmd_order` / `maybe_auto_order` / `order/create`
任何一个符号，打包直接**报错退出** —— 宁可失败，也不要悄悄发出去一份带下单能力的包。

剥离后实测：`selftest` 仍然通过，`order` 子命令变成 `invalid choice`。

> 源码里的标记：`# >>> ORDER-ONLY-BEGIN` / `# <<< ORDER-ONLY-END`（Python）、
> `<!-- ORDER-ONLY-BEGIN -->` / `<!-- ORDER-ONLY-END -->`（Markdown）、
> `# ORDER-HOOK`（watch 里的单行钩子）。**改代码时别把标记弄丢**，否则剥离会报错。

### 如果决定公开，最低限度的清单

- [ ] 用 `make-share-package.py` 出包（已内置匿名化 + 硬校验，会扫残留的私有信息）
- [ ] 确认 `state/`、`share/`、`*.bak` 没有被提交
- [ ] 确认 `venues.json` 里没有真实 token、真实场馆 id、真实 webhook
- [ ] README 顶部的「合规提醒」保留完整
- [ ] 明确标注：自动下单是可选功能，且使用者需自行评估风险

---

## 七、相关文档

| 文档 | 用途 |
| --- | --- |
| `recon-playbook.md` | 电脑端抓包完整手册 |
| `phone-capture-guide.md` | 手机端抓包（只有手机能打开的小程序） |
| `notify-setup.md` | 6 种推送通道配置 + 两层验证法 |
| `scheduler-setup.md` | 系统级调度部署、日志、资源占用、关机场景 |
| `venues.json` | 配置文件；`another-venue` 是扩展模板 |
