# 抓包侦察手册：如何把「小程序」变成可监测的接口

微信小程序**没有可公开访问的 URL**。`#小程序://xx市体育馆/xxxxxxxxxxxxxxxx` 这种链接只能在微信内打开，
桌面浏览器 / 爬虫工具一律打不开。所以监测的前提是：**先把小程序背后的 HTTPS 接口抓出来**，
再把接口参数填进 `venues.json`。抓包只需做一次，之后引擎就能长期自动跑。

> 为什么必须抓包：小程序的「有没有空场」数据来自它自己的后端接口。
> 只要拿到那个接口，监测就变成了「定时请求 + 比对」——稳定、轻量、可多场馆复用。
> 反之，如果去模拟点击小程序 UI，既脆弱（改版就失效）又需要真机环境。

---

## 一、准备：三条抓包路线（按上手难度排序）

### 路线 A：Charles + 微信 PC 端（**首选，最省事**）

**为什么首选**：不用碰手机、不用配手机代理、不用在手机上装证书。全部在同一台电脑上完成。
而且有个额外好处 —— **Charles 本身就是代理，微信走 Charles 就绕开了坏掉的系统代理**，
所以「后端链接异常」很可能同时被治好。

#### 第 1 步：装 Charles 并接管系统代理

1. 装 Charles：https://www.charlesproxy.com/
   （中文界面可选 Reqable：https://reqable.com/ ，操作同理，对国内用户更友好）
2. **先退出你现有的本地代理工具**（占用系统代理的那个，比如 `127.0.0.1:56656`）
3. Charles 菜单 → `Proxy` → 勾上 **`Windows Proxy`**
   → Charles 会自动把自己设为 Windows 系统代理，不用手工填
4. `Proxy → Proxy Settings` → 端口保持 `8888`，勾选 `Enable transparent HTTP proxying`
5. `Proxy → SSL Proxying Settings` → 勾选 `Enable SSL Proxying`
   → `Add` → Host 填 `*`，Port 填 `*`
6. `Proxy → External Proxy Settings` → **保持空白**（让 Charles 直连，不要再套一层代理）

> ⚠️ **第 5 条是抓包成败的关键，且极易漏。** 只勾了 `Enable SSL Proxying` 还不够，
> 必须再点 `Add` 加一条规则 —— Charles 4.6+ 默认没有任何 SSL 解密规则。
>
> **`Port` 的默认值是 `443`，这是最大的坑。** 若你只改了 Host 填 `*` 而没动 Port，
> 规则实际是 `*:443`，**覆盖不到 `api.example-venue.test:8443` 这种非标准端口**。
> 必须把 Port 也改成 `*`（或单独加一条 `api.example-venue.test` / `8443`）。
>
> **更省事的做法**：在 Charles 左侧 Structure 视图里右键目标主机
> （如 `api.example-venue.test`）→ **`Enable SSL Proxying`**，Charles 会自动加好规则，不用手填。
>
> **怎么判断漏了**：
> - 记录仍显示为 `CONNECT api.example-venue.test:8443`（而不是带路径的 `POST /xxx`）
> - 记录的 `Notes` 写着 `SSL Proxying not enabled for this host`
> - 用 `curl2venue.py` 解析会话文件，它会直接报「SSL Proxying 没生效」，
>   并自动区分是「总开关没开」还是「只漏了某个端口」
>
> **30 秒自检（不用碰微信，强烈推荐先做这一步）**：
> 用 PC 浏览器打开任意 https 网站 → 回到 Charles 看那条记录。
> - 显示为 `GET / HTTP/1.1` 的**明文请求** → SSL Proxying 已生效 ✅
> - 仍显示为 `CONNECT xxx:443` **隧道** → 开关还没打开，先把这一步弄对再折腾微信 ❌
>
> **逐主机诊断**（把会话文件交给脚本，一眼看清卡在哪层）：
> ```bash
> python scripts/curl2venue.py --diagnose --chlz session.chlz
> ```
> 会输出每个主机的「已解密 / 未解密(隧道)」状态表，并给出结论。
>
> **别被主机名里的 `:443` 误导**：Charles 显示主机节点时会带端口
> （`sp1.baidu.com:443`），这只是命名，**不代表你的规则配成了 443**。
> 判断标准永远是「有没有带路径的明文请求」，不是主机名后缀。
>
> **三种失败要分清**（决定你该改哪里）：
> | 现象 | 原因 | 改哪里 |
> | --- | --- | --- |
> | 连 `baidu.com`、`mp.weixin.qq.com` 这类标准 443 站点都是隧道 | 总开关没开 / 规则列表为空 | 勾上 `Enable SSL Proxying` + Add `*`/`*` |
> | 443 站点能解密，只有 `api.example-venue.test:8443` 是隧道 | 规则没覆盖这个端口 | 把 Port 改成 `*`，或单加该 host:8443 |
> | **所有主机都是隧道，但 Include 里明明有 `*:*`** | **Exclude 里有 `*:*`，而 Exclude 优先级高于 Include，全部被抵消** | 取消勾选 / 删除 Exclude 里的 `*:*` |
>
> ⚠️ 第 3 种最隐蔽：Include 看起来完全正确，但被 Exclude 一票否决。
> Exclude 面板下方**也有一个 `+` 按钮**（位置和 Include 的几乎对称），
> 很容易想往 Include 加规则却点到了 Exclude 的 `+`。
>
> **一键检查配置**（不用翻对话框，直接读 Charles 配置文件）：
> ```bash
> python scripts/curl2venue.py --check-charles-config
> ```
> 会打印 Include / Exclude 全部规则，并在 Exclude 含通配规则时直接报警。
>
> **改完规则后必须完全退出微信再重开** —— 微信会复用已建立的 TLS 隧道，
> 不重启的话新规则不会对旧连接生效。
>
> **正确的验证标准**：Charles 里那条记录从 `CONNECT` 变成
> 带路径的普通请求（`POST /mini/... `），这才说明解密成功。

#### 第 2 步：装证书到 Windows 受信任根（必须）

`Help → SSL Proxying → Install Charles Root Certificate`
→ 安装时**必须选「将所有的证书都放入下列存储」→ 浏览 → 「受信任的根证书颁发机构」**

> 如果只装到「个人」存储，微信的 HTTPS 会全部失败 —— 和手机上忘记开「完全信任」是同一个坑。

#### 第 3 步：让微信 PC 端走 Charles

> ⚠️ **微信 4.x（如 4.1.13.65）已没有「设置 → 通用设置 → 网络代理」这一项。**
> 微信 3.x 的路径是「设置 → 通用设置 → 网络代理」，4.x 找不到是正常的，不是你操作错了。
> 按下面顺序试，**推荐直接从 (A) 开始**。

**(A) 让 Charles 接管 Windows 系统代理（推荐，不依赖微信自身的设置）**

1. 先退出你现有的本地代理工具（否则它会抢回系统代理设置）
2. Charles 菜单 → `Proxy` → 勾选 **`Windows Proxy`**（Charles 自动把自己设为系统代理）
3. **完全退出微信**：任务栏右键 → 退出（直接关窗口不算），再重新打开

微信即使没有自己的代理设置项，通常也会跟随 Windows 系统代理。

**(B) 手工设置 Windows 系统代理**

设置 → 网络和 Internet → 代理 → 手动设置代理 → 打开 →
地址 `127.0.0.1`，端口 `8888`

**(C) 到登录界面里找（部分版本）**

有资料显示微信 PC 的代理选项在**扫码登录界面**左下角「更多」里，
而不是登录之后的设置里。在登录界面点「更多」看看有没有「使用系统代理」类选项。

**(D) Proxifier 强制进程走 Charles（最可靠，完全不依赖微信配合）**

如果 (A)(B)(C) 都不生效 —— 说明微信 4.x 不理会系统代理 —— 用 Proxifier 在网络层强制转发：

1. 新增代理服务器 `127.0.0.1:8888`，类型选 HTTPS
2. 规则里把目标程序设为微信主进程：4.x 是 `Weixin.exe`，3.x 是 `WeChat.exe`
3. **再补上小程序宿主进程**：打开小程序时看任务管理器，
   把进程名含 `AppEx` / `App` / `Browser` 且属于微信的都加进规则
   （3.x 是 `WeChatAppEx.exe`；4.x 进程名可能不同，以任务管理器实际看到的为准）
4. 规则动作设为走上面那条代理

这条路不需要微信支持代理设置，成功率最高。

**(E) 以上都无效 → 改走手机抓包**

见 `phone-capture-guide.md`。别在 PC 上无限耗时间。

#### 第 4 步：抓请求

1. 微信 PC 端打开小程序，翻到「选日期 → 看场次」那一屏
   - 如果这时小程序能正常打开 → 顺带证明「后端链接异常」就是代理问题
   - 如果仍报错 → 见本文件前面的排查章节
2. Charles 里先点 🚫 **Clear 清空**，再在微信里**只做一个动作**（切一次日期）
3. 用 Filter 框搜关键字缩小范围：`api`、`slot`、`order`、`book`、`venue`、`site`、`date`、`list`
   先排除噪音域名：`weixin.qq.com`、`mmbiz.qpic.cn`、`servicewechat.com`、`res.wx.qq.com`
4. 点开 `Contents → Response`，**返回 JSON 里含日期/时段/场地名/状态字段**的那条就是目标

#### 第 5 步：导出（三选一）

**方式 1（最省事）：直接保存整个会话**

`File → Save Session` → 存成 `.chlz`

`.chlz` 是 Charles 原生会话格式，**请求和响应都在里面**，一个文件搞定：

```bash
python scripts/curl2venue.py --chlz session.chlz --id example-venue --name "xx市体育馆"
```

**方式 2：导出 HAR**

选中目标请求 → 右键 → **`Export Session...`** → 格式选 **`.har`**

HAR 是标准 JSON，同样包含 URL、方法、请求头、请求体**和完整响应**：

```bash
python scripts/curl2venue.py --har session.har --id example-venue --name "xx市体育馆"
```

**方式 3（只抓了一条请求）**

右键那条请求 → **`Copy cURL Request`** → 存成 `my-curl.txt`

这一下把 URL、方法、所有请求头、请求体复制成一条命令，但**不含响应**，
所以需要加 `--send` 让它实打一次接口来推荐字段映射。

如果 `Copy cURL Request` 不可用，手工抄这三处：

| 要抄什么 | 在 Charles 哪里看 |
| --- | --- |
| URL + 方法 | `Overview` 里的 `Request Method` 和 `Full URL` |
| 请求头 | `Contents → Headers` 区域（要 `Authorization`、`Cookie`、`Referer`、`Content-Type`、`User-Agent`） |
| 请求体 | `Contents → Request` 标签页，切到 `JSON Text` 或 `Form` 视图 |

#### 第 6 步：转成配置

```bash
# 方式 1：.chlz 会话文件
python scripts/curl2venue.py --chlz session.chlz \
    --id example-venue --name "xx市体育馆" \
    --miniprogram "#小程序://xx市体育馆/xxxxxxxxxxxxxxxx"

# 方式 2：HAR
python scripts/curl2venue.py --har session.har \
    --id example-venue --name "xx市体育馆"

# 方式 3：cURL
python scripts/curl2venue.py --curl-file my-curl.txt \
    --id example-venue --name "xx市体育馆" --send
```

脚本会自动：
- 解析出 URL / 方法 / 请求头 / 请求体
- 把**日期参数自动替换成 `${DATE}` 占位符**（识别 `2026-09-20`、`20260920` 等格式）
- 若 URL 里带 `timetableId=` / `scheduleId=` 这类**排期号**参数，会换成 `${TIMETABLE_ID}`
  并自动补一个 `timetable_ids` 扫描块 —— 这类 id 与日期绑定、只在该日期放号后才存在，
  无法直接用日期拼 URL，只能扫区间再按响应里的日期归组
- **推荐 `slots_path`**（遍历响应找「对象数组」，按字段名与路径名打分）
  并列出状态字段的取值分布，供校准 `available_values`
- **识别「时段 → 场地」嵌套结构**（如 `data.timeList[].groundJson[]`），
  自动填好 `children_field` / `date_field` / `time_end_field` / `child_index_field`
- 输出可直接粘进 `references/venues.json` 的 venue 配置块

剩下需人工确认两件事：

1. `response.available_values` 里哪个值代表「可预约」
   （抓一次「有空场」的响应，与「已约满」的响应对比即可确定）。
2. **有没有「占位价」**。有些接口对「不开放售卖」的场地也返回 `available` 值，
   但价格是 `9999` 这类哨兵值 —— 必须填进 `response.unavailable_price_values`，
   否则会把「不开放」误报成「可抢」。

> **`.chlz` 的内部命名在不同 Charles 版本/配置下会变**：见过 `<i>-req.dat` / `<i>-res.dat`，
> 也见过只有 `<i>-res.json` 的（响应体文件名由 `meta.response.bodyFile` 指明）。
> 脚本两种都兼容。若你手工解包，**先看 `meta.json` 里的 `bodyFile` 字段**，别硬猜扩展名。

---

### 路线 B：微信开发者工具（能看到更干净的请求）
1. 用「微信开发者工具」打开该小程序的体验版/预览码（若你有权限）。
2. `Network` 面板直接看 XHR，右键 `Copy as cURL` —— 参数、Header 一次拿全。
3. 这是最省事的路线，前提是你能拿到小程序的开发/体验权限。

### 路线 C：手机抓包（mitmproxy / Charles / Stream）
1. 手机 WiFi 代理指向电脑的 mitmproxy（默认 8080），安装并信任 mitmproxy 证书。
2. `mitmproxy` 启动后，`f` 过滤 `~u api`，在手机上操作小程序。
3. `i` 键进入 `Flow detail`，`r` 重放，`e` 编辑；确认无误后导出。

> 📱 **手机抓包请直接照 `phone-capture-guide.md` 走** —— 那里有 iOS 与 Android 的逐步操作、
> 证书信任设置、找不到请求时的三种缩范围办法，以及「抓到乱码/抓不到」的排查表。
> 这条路只在**路线 A 走不通时**才用（比如微信 PC 端怎么都不认代理）。

> ⚠️ 只在**你自己的设备、你自己的账号**上抓自己的预约请求。不要抓取、存储或转让他人数据。

### ⚠️ 排查：PC 端微信打不开小程序（报「后端链接异常」）

**先看清这个报错的含义**：小程序在手机上正常、PC 微信却报「后端链接异常」，
说明**小程序前端已经加载成功，是它向后端发请求失败了** —— 这不是「小程序没适配 PC」，
而是 PC 这条链路上的网络请求被拦了。按顺序排查：

1. **先查系统代理（最常见，优先查这个）**

   如果本机设置了全局 HTTP 代理（例如 `http_proxy` / `https_proxy` 指向
   `http://127.0.0.1:56656` 这类本地代理工具），微信 PC 端会跟着走这个代理。
   代理规则一旦把微信/小程序域名路由到不可达的节点，就会精确地报出「后端链接异常」。

   - 微信 PC 端 → 左下角菜单 → 设置 → 通用设置 → **网络代理 → 选「不使用代理」**
   - Windows 设置 → 网络和 Internet → 代理 → 关闭「使用代理服务器」
   - 或在代理工具里给 `weixin.qq.com`、`servicewechat.com`、小程序后端域名加**直连规则**
   - 改完**必须重启微信 PC 端**，再重新打开小程序

2. 微信 PC 端 → 设置 → 通用设置 → **清除缓存**，然后重开小程序
3. 把微信 PC 端升级到最新版
4. 若以上都无效 → 该小程序后端可能按 User-Agent 拦截 PC 请求（PC 微信的 UA 与手机不同）。
   这种情况你改不了，**直接走手机抓包**，别在 PC 上继续耗时间。

### 手机抓包：iOS 与 Android 的成功率差异很大

| 平台 | 可行性 | 做法 |
| --- | --- | --- |
| **iOS** | ✅ 最省事 | 装 Charles / mitmproxy 证书后，**还要**到「设置 → 通用 → 关于本机 → 证书信任设置」里手动启用完全信任。启用后微信小程序流量可解 |
| **Android 真机（7+）** | ⚠️ 麻烦 | 系统不信任用户安装的 CA，微信自身也不信任用户证书。需要 root 后把证书装进系统目录，或配 LSPosed / JustTrustMe |
| **Android 模拟器** | ✅ 可行 | 在模拟器里登录微信 + 装系统级证书，再操作小程序，比真机 root 省事 |

> **结论：能借到 iPhone 就用 iPhone 抓** —— 成功率最高、步骤最少。
> 只有 Android 真机的话，优先考虑「模拟器 + 系统级证书」这条路。

---

## 二、从抓到的东西里，只挑 4 类信息

对着那条返回场次的请求，记录：

| 需要的信息 | 在哪看 | 填到配置的哪个字段 |
| --- | --- | --- |
| 接口地址 | General → Request URL | `request.url` |
| 请求方法 | General → Request Method | `request.method` |
| 必要请求头 | Request Headers | `request.headers` |
| 请求参数 | Query String / Payload | `request.body` |

**关键判断：日期参数长什么样。**
把日期参数换成占位符 `${DATE}`，例如 `"date": "${DATE}"`。引擎会按 `dates.offsets`
逐日替换并发请求。注意保持日期**格式一致**（`%Y-%m-%d` 还是 `%Y%m%d`，看抓到的原值）。

**必要请求头通常有这几个：**
- `Authorization` / `token` / `openid` —— 身份凭证（**会过期**，见下文）
- `Referer: https://servicewechat.com/...` —— 有些后端校验
- `Content-Type: application/json` —— POST 时必带

---

## 三、映射响应字段

把返回的 JSON 贴出来，看清结构，然后填 `response`：

```json
{
  "code": 0,
  "data": {
    "list": [
      { "startTime": "19:00", "endTime": "20:00", "courtName": "羽毛球03号场",
        "status": 0, "price": 40 }
    ]
  }
}
```

- `slots_path`：场次数组的位置，点号分隔 → `"data.list"`
  - 如果是 `{"2026-09-20": [...], "2026-09-21": [...]}` 这种「日期 → 数组」的映射，
    填 `"data"` 并加 `"flatten_dict_lists": true`。
  - 支持 `*` 通配一层：`"data.*.slots"`。
- `available_field` + `available_values`：**这是最需要试错的一步**。
  抓一次「明明有空场」的响应，看那个字段的值是什么（`0`? `"free"`? `"可预约"`? `1`?），
  把它填进 `available_values`。再抓一次「约满」的响应，确认值不同。
- `court_field` / `price_field`：没有就留默认，不影响可用性判断。

---

## 四、凭证过期怎么办（重要）

`Authorization` / `token` 一般几天到几十天会失效。三种处理方式：

1. **手填 + 定期更新**：把 token 写在配置里，失效时引擎会报 401，手动换一次。
2. **环境变量注入**：配置里写 `"Authorization": "${TOKEN}"`，
   运行时用环境变量传入（引擎的 `render()` 会把 `${TOKEN}` 替换掉，
   但注意 `build_dates` 只注入 DATE/TS/VENUE_ID —— token 走环境变量请改为直接写值或先做替换）。
3. **无凭证接口**：部分场馆的「可预约时段」接口是匿名的（只要不提交订单），
   优先找这类接口，监测最省心。

---

## 五、验证抓包是否成功

```bash
# 1. 看配置是否被识别
python scripts/venue_monitor.py doctor -c references/venues.json

# 2. 单次拉取，看能否解析出空余场次
python scripts/venue_monitor.py check -c references/venues.json -v example-venue -d 2026-09-20

# 3. 如果解析出 0 条场次，说明 slots_path 填错了 —— 用下面的响应结构推荐重新校准
```

**排查「解析出 0 条」的万能办法**：让脚本实打一次接口，它会自动推荐字段路径。

```bash
# 直接实打并推荐 slots_path / available_values
python scripts/curl2venue.py --curl-file my-curl.txt --send

# 或者你已把响应存成了文件，就离线分析
python scripts/curl2venue.py --curl-file my-curl.txt --response-file resp.json
```

如果想手工看结构：

```bash
curl -s '把抓到的URL粘这里' -H 'Authorization: xxx' > resp.json
python -c "import json;d=json.load(open('resp.json',encoding='utf-8'));print(json.dumps(d,ensure_ascii=False,indent=2)[:3000])"
```

---

## 六、放号时间怎么确定（本项目的核心问题）

公开渠道（政府公告、百科、新闻）只会写「开放时间」，**不会写「几点开始放号」**。
放号时间是运营在小程序后台配的，唯一可靠的办法是**实测**：

```bash
# 盯住一个「明天还没放出来」的日期，从 20:00 守到 23:30，每 20 秒问一次
python scripts/venue_monitor.py probe -c references/venues.json \
    -v example-venue -d 2026-09-23 \
    --window-from 20:00 --window-to 23:30 --interval 20 --stop-on-first
```

观测到「空余 0 → 空余 N」翻转的那个时刻，就是放号时间。
建议**连续测 2–3 天**，都落在同一时刻附近才能确认规律。
结果会写到 `state/probe-<venue>-<时间戳>.json`，含完整时间线，可复查。

### 冷启动技巧：用订单号时间戳反推放号时间（不用等几天）

`probe` 和 `release` 都要连续跑几天才能收敛，但**如果响应里带订单号、且订单号内嵌了下单时刻，
就能在抓包当天立刻推断出放号时间**。

识别方法：订单号是纯数字且长度固定，取前 12 位按 `%y%m%d%H%M%S` 解析，
结果应该是**合理且分散**的时间戳。若全部解成同一秒，说明那不是时间戳，别硬套。

实测案例（xx市体育馆，订单号 `2609161217386575` → 2026-09-16 12:17:38）：

| 目标日期 | 该日最早订单时刻 | 该日下单量 |
| --- | --- | --- |
| 09-15 | 09-14 **09:32:05** | 88 |
| 09-16 | 09-15 **09:41:58** | 32 |
| 09-17 | 09-16 **09:42:00** | 70 |

→ 结论：**每天上午 09:30 前后放号**（且 T 日只放 T+1，可订窗口 = 今天 + 明天）。

两个要点：

1. **最早订单只是放号时刻的上界**（放号必然早于第一笔订单）。
   若某位长期订场的用户习惯固定钟点下单，单天样本会被污染 —— 所以要用**多天聚类**，
   而不是取单天的最早值。
2. 顺手把订单按「距最早订单的小时数」分桶，能看出**放号后多久被抢光**。
   xx的数据是「0–1 小时内被抢走 50%+」（57/195、48/88、58/70），存在明显的**陡崖** ——
   这意味着**提醒必须快**，否则看到时已经没了。

```python
# 从响应里反推放号时刻（把 slots_path / 订单号字段换成你自己抓到的）
import json, datetime
d = json.load(open('resp.json', encoding='utf-8'))
ts = []
for slot in d['data']['list']:              # ← 换成你的 slots_path
    o = slot.get('orderNo') or ''           # ← 换成你的订单号字段
    if len(o) == 16:
        ts.append(datetime.datetime.strptime(o[:12], '%y%m%d%H%M%S'))
ts.sort()
print('最早订单:', ts[0], ' 共', len(ts), '笔')
```

### 常见放号规则（作为搜索时间窗的候选）

- 提前 N 天的 **00:00** 整点放号（最常见）
- 提前 N 天的 **08:30 / 09:00**（场馆开门时刻）
- 提前 N 天的 **12:00 / 18:00**
- 每周固定某天集中放一周的号
- **每天固定上午时段（如 09:30 前后）放次日场次** —— xx实测，见上

先用 `probe` 在 **00:00、08:30、09:30、12:00、20:00** 几个候选点附近各测一轮，命中率最高。
若响应里能读出订单号时间戳，优先走上面的冷启动技巧，省掉等待。

---

