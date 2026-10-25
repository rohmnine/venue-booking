# 手机抓包实操指南：拿到接口地址 + 请求头 + 请求体

目标产物只有三样东西：

1. **接口地址**（URL + 请求方法）
2. **请求头**（Headers，尤其是身份凭证）
3. **请求体**（Body / 参数）

拿到后不用手填 —— 直接用 `scripts/curl2venue.py` 转成配置。

---

## 零、选平台（先看这个，能省几小时）

| 平台 | 成功率 | 说明 |
| --- | --- | --- |
| **PC 微信 + Charles** | ✅ 最省事 | 前提是 PC 能打开小程序（先解决代理问题） |
| **iPhone + Charles** | ✅ 推荐 | 证书信任设置里手动开启完全信任后即可 |
| **Android 模拟器** | ✅ 可行 | 模拟器自带 root，可装系统级证书 |
| **Android 真机（未 root）** | ❌ 基本不行 | 微信不信任用户安装的 CA，抓到的 HTTPS 全是乱码 |
| **Android 真机（已 root）** | ✅ 可行 | 需把证书装进系统目录，或配 LSPosed/JustTrustMe |

**能借到 iPhone 就用 iPhone。** 这是投入产出比最高的一条路。

---

## 一、iOS + Charles 完整步骤

### 准备

- PC 装 Charles：https://www.charlesproxy.com/
- **手机和 PC 连同一个 WiFi**（必须同网段）
- 查 PC 的局域网 IP：Windows 上执行 `ipconfig`，找「IPv4 地址」，例如 `192.168.1.100`

### 第 1 步：配置 Charles

1. `Proxy → Proxy Settings` → 端口填 `8888`，勾选 `Enable transparent HTTP proxying`
2. `Proxy → SSL Proxying Settings` → 勾选 `Enable SSL Proxying`
3. 点 `Add` → Host 填 `*`，Port 填 `*`（先全开，确认能抓到后再收窄）
4. `Proxy → Access Control Settings` 可保持默认（或确保允许局域网设备）

### 第 2 步：iPhone 挂代理

1. 设置 → 无线局域网 → 点当前 WiFi 右侧的 **ⓘ**
2. 拉到底 → 配置代理 → **手动**
3. 服务器：PC 的 IP（如 `192.168.1.100`）　端口：`8888`
4. 保存

挂上后 Charles 会弹窗询问是否允许该连接 → 点 **Allow**。

### 第 3 步：装证书（关键，最容易漏）

1. iPhone 用 **Safari** 打开 `chls.pro/ssl` → 允许下载描述文件
2. 设置 → 通用 → **VPN 与设备管理** → 安装该描述文件（输锁屏密码）
3. **最重要的一步**：设置 → 通用 → **关于本机** → 拉到底 → **证书信任设置**
   → 打开 `Charles Proxy CA` 的**完全信任**开关

> 第 3 步不做的话，微信的 HTTPS 请求会全部连接失败或显示乱码 —— 90% 的「抓不到」都卡在这里。

### 第 4 步：抓请求

1. Charles 里先点 **🚫 扫帚图标（Clear）** 清空列表
2. 手机微信打开小程序，**只做一个动作**：翻到「选日期 → 看场次」那一屏
3. 回到 Charles，列表里应该只多了几条 —— 这时最容易找

**缩小范围的三种办法：**

- **Filter 框**输入关键字逐个试：`api`、`slot`、`order`、`book`、`venue`、`site`、`date`、`list`、`tiyu`、`example-venue`
- **Structure 视图**按域名分组，先排除噪音域名：
  `weixin.qq.com`、`mmbiz.qpic.cn`（图片）、`servicewechat.com`（基础库）、`res.wx.qq.com`
- **Focus**：右键可疑域名 → `Focus`，列表就只剩这一个域名的请求

**判定哪条是对的**：点开请求 → `Contents → Response`，找**返回 JSON 里包含场次列表**的那条
（能看到日期、时间段、场地名、状态字段）。看到它就找对了。

### 第 5 步：导出三件套（一步到位）

**最省事的做法**：右键那条请求 → **`Copy cURL Request`**

这一下就把 URL + 方法 + 所有请求头 + 请求体全部复制成一条 cURL 命令。

如果 `Copy cURL Request` 不可用，手工抄这三处：

| 要抄什么 | 在 Charles 哪里看 |
| --- | --- |
| URL + 方法 | `Overview` 里的 `Request Method` 和 `Full URL` |
| 请求头 | `Contents → Headers` 区域（要 `Authorization`、`Cookie`、`Referer`、`Content-Type`、`User-Agent`） |
| 请求体 | `Contents → Request` 标签页，切到 `JSON Text` 或 `Form` 视图 |

### 第 6 步：转成配置

把复制到的 cURL 粘进文件，然后跑：

```bash
python scripts/curl2venue.py --curl-file my-curl.txt --id example-venue --name "xx市体育馆"
```

脚本会自动：
- 解析出 URL / 方法 / 请求头 / 请求体
- 把**日期参数自动替换成 `${DATE}` 占位符**（识别 `2026-09-20`、`20260920` 等格式）
- 输出可直接粘进 `references/venues.json` 的 venue 配置块

剩下你只需人工确认一件事：`response` 里的 `slots_path` 和 `available_values`
（脚本会给出候选值提示，但必须对着真实响应校准）。

---

## 二、Android 模拟器路线（没有 iPhone 时）

1. 装 MuMu / 雷电模拟器（自带 root 权限）
2. 模拟器设置里配置 WiFi 代理指向 PC 的 Charles（`192.168.1.100:8888`）
3. 导出 Charles 证书为 `.pem`，重命名为**证书哈希值**（用 `openssl x509 -inform PEM -subject_hash_old -in charles.pem | head -1` 取哈希），
   后缀改 `.0`
4. `adb root` → `adb remount` → `adb push <hash>.0 /system/etc/security/cacerts/` → `adb shell chmod 644 ...`
5. 重启模拟器，此时证书成为**系统级**证书，微信才会信任
6. 模拟器里登录微信（可能需要手机扫码），打开小程序，按上面第 4–6 步操作

> 微信对模拟器登录有风控，可能要求扫码验证 —— 这是正常的。

---

## 三、抓不到 / 抓到乱码的排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 手机完全上不了网 | 代理没生效或 IP 填错 | 检查手机代理设置与 PC IP；确认 Charles 弹窗点了 Allow |
| Charles 里看不到任何请求 | 手机没走代理 | 关掉手机上的 VPN / 加速器再试 |
| HTTPS 全是乱码或连接失败 | **证书信任未开启** | iOS：设置 → 通用 → 关于本机 → 证书信任设置 → 开完全信任 |
| 只抓到 `weixin.qq.com` | 找错域名了 | 用 Filter 搜关键字，或按 Structure 视图逐个域名点开看响应 |
| 请求列表刷得太快 | 没清空 | 先 Clear，再在手机上只做一个动作 |
| 某条请求显示 `<unknown>` | 该接口用了证书固定 | 换 PC 微信路线；小程序接口一般不做固定，少见 |

---

## 四、注意事项

- **只抓自己的请求**：在自己的设备、自己的账号上操作，不采集、不存储、不转让他人数据。
- **凭证敏感**：cURL 里会带 `Authorization` / `token` / `openid` / `Cookie`。
  这些等于你的登录态，**不要发给别人、不要提交到公开仓库**。
  本 skill 的配置请只保存在本地。
- **凭证会过期**：失效时接口返回 401，重新抓一次更新即可。
- 抓包工具用完记得**关掉手机代理并删除描述文件**，否则手机会一直上不了网。
