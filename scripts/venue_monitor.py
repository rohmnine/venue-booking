#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
venue_monitor.py — 场馆/场地预约小程序「空余场次」监测引擎

设计目标
--------
1. 一套引擎，多场馆：所有站点差异都收敛到 venues.json 的声明式配置里，
   新增一个「小程序」只需要加一段配置（URL / 请求模板 / 响应字段映射），不改代码。
2. 只读监测：只做「查询 + 比对 + 通知」，不做自动下单/自动占场。
3. 可探测放号时间：probe 子命令高频轮询，记录「无场次 -> 有场次」的翻转时刻。

子命令
------
  doctor   体检配置：字段是否齐全、哪些场馆还没抓包、凭证是否快过期
  check    查一次：拉取所有关注日期/号源的场次，与上次快照比对，输出新增空余
  probe    高频轮询，探测放号时间（--from/--to/--interval）
  release  打印「日期首次开放预订」的观测日志，推断放号时刻
  notify-test  给 webhook 发一条测试消息
  set-token  换接口凭证：从 cURL / 抓包文件 / 直接粘贴取值，写回配置并实测验证
  watch    系统级调度入口：窗口内加密轮询、窗口外只跑一次（给计划任务/cron 调）
  status   调度自检：最近几次运行正常吗、快照多久没更新（查「静默失效」）
  selftest 用内置 mock 场馆跑通全链路，不联网

响应形态支持
------------
  A. 扁平型：slots_path 直接指向场次数组，每项 = 一个场地/时段
  B. 嵌套型：slots_path 指向「时段数组」，每个时段里再用 children_field
     挂一个「场地数组」（xx市体育馆就是这种：data.timeList[].groundJson[]）

号源发现（timetable_ids）
-------------------------
  有些接口不按日期查询，而是按一个「排期 id」（如 timetableId=1048）查询，
  id 与日期绑定、且只在该日期放号后才存在。此时配置 timetable_ids.mode="scan"，
  引擎会扫描 id 区间、按响应里的真实日期归组，从而自动发现「新放出来的日期」。

依赖：仅 Python 3 标准库。
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 基础工具

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STATE_DIR = os.path.join(SKILL_ROOT, "state")

try:  # Windows 控制台中文
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


def log(msg: str = "") -> None:
    print(msg, flush=True)


def now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def die(msg: str, code: int = 1) -> "None":
    log("[ERROR] " + msg)
    sys.exit(code)


# ---------------------------------------------------------------- 配置加载

REQUIRED_VENUE_FIELDS = ["id", "name"]


def load_config(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        die("配置文件不存在: %s\n先执行: python venue_monitor.py doctor --config %s" % (path, path), 1)
    with open(path, "r", encoding="utf-8") as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError as e:
            die("配置文件不是合法 JSON: %s (%s)" % (path, e), 1)
    if "venues" not in cfg or not isinstance(cfg["venues"], list):
        die("配置缺少 venues 数组", 1)
    return cfg


def is_enabled(venue: Dict[str, Any]) -> bool:
    """场馆是否启用。

    字段缺省视为启用（向后兼容没有该字段的老配置）；
    只有显式写了 false 才算禁用。用 `is not False` 而不是真值判断，
    是为了让 "enabled": 0 / "" 这类脏值不至于被静默当成禁用。
    """
    return venue.get("enabled", True) is not False


def pick_venues(cfg: Dict[str, Any], only: Optional[str]) -> List[Dict[str, Any]]:
    """挑出本次要处理的场馆。

    - 指定了 only（-v）：以 id 为准，**不管 enabled** —— 显式点名就是要跑它，
      这样 `check -v 某个已禁用的场馆` 仍然可用于调试。
    - 没指定：只返回 enabled 的场馆。一个都没有就报错，
      而不是静默什么都不做（静默失效最难查）。
    """
    venues = cfg["venues"]
    if only:
        venues = [v for v in venues if v.get("id") == only]
        if not venues:
            die("配置里找不到场馆 id=%s" % only, 1)
        return venues

    enabled = [v for v in venues if is_enabled(v)]
    if not enabled:
        die("配置里没有任何已启用的场馆（检查各场馆的 enabled 字段）", 1)
    return enabled


def is_configured(venue: Dict[str, Any]) -> bool:
    """是否已完成抓包配置（能真正发请求）。"""
    req = venue.get("request") or {}
    if req.get("mock"):
        return True
    return bool(req.get("url"))


# 还没填真值的痕迹。模板里故意留这些词，doctor 会据此提醒。
_PLACEHOLDER_RE = re.compile(
    r"(PASTE|REPLACE_ME|YOUR_|XXX|TODO|FIXME|CHANGEME|example\.com|xxxx)",
    re.IGNORECASE,
)


def placeholder_headers(venue: Dict[str, Any]) -> List[str]:
    """返回疑似「还留着占位值」的请求头名字。"""
    out: List[str] = []
    for k, v in ((venue.get("request") or {}).get("headers") or {}).items():
        if isinstance(v, str) and _PLACEHOLDER_RE.search(v):
            out.append(k)
    return out


def resolve_state_dir(cfg: Dict[str, Any], args: argparse.Namespace) -> str:
    state_dir = getattr(args, "state_dir", None) or cfg.get("state_dir") or DEFAULT_STATE_DIR
    if not os.path.isabs(state_dir):
        cfg_path = getattr(args, "config", None) or os.path.join(SKILL_ROOT, "references", "venues.json")
        state_dir = os.path.join(os.path.dirname(os.path.abspath(cfg_path)), state_dir)
    return state_dir


# ---------------------------------------------------------------- 路径取值

def dig(obj: Any, path: str) -> Any:
    """按 'a.b.c' 取值；'*' 表示「把这一层的所有子值铺平」。找不到返回 None。"""
    if not path:
        return obj
    cur = obj
    for seg in path.split("."):
        if seg == "*":
            if isinstance(cur, dict):
                cur = list(cur.values())
            elif isinstance(cur, list):
                pass
            else:
                return None
            continue
        if isinstance(cur, list):
            # 列表上继续取字段 -> 逐元素取，铺平
            out = []
            for item in cur:
                got = dig(item, seg)
                if isinstance(got, list):
                    out.extend(got)
                elif got is not None:
                    out.append(got)
            cur = out
            continue
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def flatten_lists(obj: Any) -> List[Any]:
    """若 obj 是 dict，把它的所有 list 值铺平成一个大 list（应对 date->slots 的映射结构）。"""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        out: List[Any] = []
        for v in obj.values():
            out.extend(flatten_lists(v))
        return out
    return []


# ---------------------------------------------------------------- 小工具

def hhmm(v: Any) -> str:
    """把 '20:30:00' / '20:30' 统一成 '20:30'；非法值原样返回。"""
    s = str(v if v is not None else "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})", s)
    if not m:
        return s
    return "%02d:%s" % (int(m.group(1)), m.group(2))


def slot_minutes(t_from: Any, t_end: Any) -> Optional[int]:
    a, b = hhmm(t_from), hhmm(t_end)
    try:
        ah, am = a.split(":")[:2]
        bh, bm = b.split(":")[:2]
        d = (int(bh) * 60 + int(bm)) - (int(ah) * 60 + int(am))
        return d if d > 0 else None
    except Exception:
        return None


def norm_court(v: Any, prefix: str = "", suffix: str = "") -> str:
    """规范化场地名：按需补上前缀 / 后缀。

    注意：前缀和后缀必须**各自独立**判断。
    早先写成 `if not s.endswith(suffix) and not s.startswith(prefix)`，
    而 `s.startswith("")` 恒为 True —— 于是只配了 `court_suffix`（没配 prefix）时，
    整个条件恒为 False，后缀永远拼不上。同理只配 `court_prefix` 时前缀也失效。
    """
    s = str(v if v is not None else "").strip()
    if not s or s.lower() in ("none", "null"):
        return "-"
    if prefix and not s.startswith(prefix):
        s = "%s%s" % (prefix, s)
    if suffix and not s.endswith(suffix):
        s = "%s%s" % (s, suffix)
    return s


def jwt_exp(token: str) -> Optional[dt.datetime]:
    """从 JWT 的 payload 里解出 exp（本地时间）。解不出返回 None。"""
    try:
        parts = str(token).split(".")
        if len(parts) < 2:
            return None
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad).decode("utf-8"))
        exp = payload.get("exp")
        if not exp:
            return None
        return dt.datetime.fromtimestamp(int(exp))
    except Exception:
        return None


# ---------------------------------------------------------------- 日期

def build_dates(venue: Dict[str, Any], override: Optional[List[str]]) -> List[str]:
    if override:
        return override
    spec = venue.get("dates") or {}
    fmt = spec.get("date_format", "%Y-%m-%d")
    out: List[str] = []
    today = dt.date.today()
    for off in spec.get("offsets", [0]):
        out.append((today + dt.timedelta(days=int(off))).strftime(fmt))
    for s in spec.get("explicit", []) or []:
        out.append(dt.date.fromisoformat(s).strftime(fmt))
    # 去重保序
    seen = set()
    uniq = []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


# ---------------------------------------------------------------- 请求发送

def render(tpl: Any, ctx: Dict[str, str]) -> Any:
    """把 ${VAR} 占位符替换成实际值（递归处理 dict/list/str）。"""
    if isinstance(tpl, str):
        out = tpl
        for k, v in ctx.items():
            out = out.replace("${%s}" % k, str(v))
        return out
    if isinstance(tpl, dict):
        return {k: render(v, ctx) for k, v in tpl.items()}
    if isinstance(tpl, list):
        return [render(v, ctx) for v in tpl]
    return tpl


def http_fetch(url: str, method: str, headers: Dict[str, str], body: Any,
               timeout: int, no_proxy: bool, proxy: Optional[str]) -> Any:
    data = None
    if method.upper() not in ("GET", "HEAD") and body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        else:
            data = str(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper())

    # 请求头必须是 latin-1 可编码的（HTTP/1.1 规范）。中文占位符、emoji 之类
    # 会在这里炸成 "'latin-1' codec can't encode characters..."，
    # 那个报错完全看不出问题出在哪个头，所以提前给出能直接照做的提示。
    bad_headers = []
    for k, v in (headers or {}).items():
        try:
            str(v).encode("latin-1")
        except UnicodeEncodeError:
            bad_headers.append((k, str(v)))
    if bad_headers:
        detail = "; ".join("%s=%r" % (k, v[:40]) for k, v in bad_headers)
        raise RuntimeError(
            "请求头含非 ASCII 字符，无法发送：%s\n"
            "       → HTTP 头只允许 latin-1。多半是把「待填写的占位文字」留在了 "
            "request.headers 里，或者凭证里混进了中文/emoji。\n"
            "       → 把该头的值换成抓包得到的原始 ASCII 字符串即可。" % detail)

    for k, v in (headers or {}).items():
        req.add_header(k, str(v))
    if "User-Agent" not in req.headers and "user-agent" not in [h.lower() for h in req.headers]:
        req.add_header("User-Agent", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                                     "AppleWebKit/605.1.15 MicroMessenger/8.0.49")

    handlers: List[Any] = []
    if no_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    elif proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()

    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"_raw_text": raw[:2000]}
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError("请求失败（已重试3次）: %s" % last_err)


# ---------------------------------------------------------------- mock 场馆

def mock_response(venue: Dict[str, Any], date_str: str) -> Dict[str, Any]:
    """
    内置演示数据源，让整条链路（请求->解析->比对->报告）离线可跑通。
    模拟规则：目标日期在「可预订窗口」外时全部约满；进入窗口后陆续放出空位。
    这样 selftest 能稳定复现「无 -> 有」的翻转。
    """
    window_days = int((venue.get("request") or {}).get("mock_window_days", 7))
    release_hour = int((venue.get("request") or {}).get("mock_release_hour", 21))
    today = dt.date.today()
    target = dt.date.fromisoformat(date_str)
    offset = (target - today).days
    seed = int(("%s%s" % (venue.get("id", "x"), date_str)).encode("utf-8").hex(), 16) % 10**6
    rnd = random.Random(seed)

    released = offset < window_days or dt.datetime.now().hour >= release_hour
    if not released:
        # 真实接口在「该日期尚未开放预订」时返回空数组，而不是返回一堆已满场次
        return {"code": 0, "msg": "ok", "data": {"date": date_str, "list": []}}

    slots = []
    start_hours = [8, 9, 10, 11, 14, 15, 16, 17, 18, 19, 20, 21]
    for i, h in enumerate(start_hours):
        status = "free" if rnd.random() < 0.35 else "full"
        slots.append({
            "startTime": "%02d:00" % h,
            "endTime": "%02d:00" % (h + 1),
            "courtName": "羽毛球%02d号场" % (i % 12 + 1),
            "status": status,
            "price": 30 if h < 18 else 40,
        })
    return {"code": 0, "msg": "ok", "data": {"date": date_str, "list": slots}}


# ---------------------------------------------------------------- 场次解析

def extract_slots(venue: Dict[str, Any], payload: Any, date_str: str) -> List[Dict[str, Any]]:
    """
    从接口响应里抽出「可比较的场次单元」。

    两种响应形态：
      A. 扁平型 —— slots_path 直接指向场次数组
      B. 嵌套型 —— slots_path 指向「时段数组」，每个时段用 children_field
                   挂一个「场地数组」（xx市体育馆：data.timeList[].groundJson[]）
    """
    rspec = venue.get("response") or {}

    # 业务码校验：有些接口 HTTP 200 但 body 里 code=500 表示「该日期/排期还不存在」
    code_path = rspec.get("code_path")
    if code_path:
        code = dig(payload, code_path)
        ok_vals = [str(x) for x in rspec.get("ok_code_values", [200, 0, "200", "0"])]
        if code is not None and str(code) not in ok_vals:
            return []

    node = dig(payload, rspec.get("slots_path", "data.list"))
    if node is None and rspec.get("flatten_dict_lists"):
        node = dig(payload, rspec.get("slots_path", "data"))
    items = flatten_lists(node) if isinstance(node, dict) else (node or [])
    if not isinstance(items, list):
        items = [items]

    f_date = rspec.get("date_field")
    f_time = rspec.get("time_field", "startTime")
    f_end = rspec.get("time_end_field")
    f_avail = rspec.get("available_field", "status")
    free_vals = [str(x).strip().lower() for x in
                 rspec.get("available_values", ["free", "0", "可预约", "available"])]
    f_court = rspec.get("court_field", "courtName")
    f_price = rspec.get("price_field", "price")
    children = rspec.get("children_field")
    child_idx = rspec.get("child_index_field", "index")
    court_prefix = str(rspec.get("court_prefix") or "")
    court_suffix = str(rspec.get("court_suffix") or "")
    bad_prices = [str(x) for x in (rspec.get("unavailable_price_values") or [])]

    def build(container: Dict[str, Any], child: Optional[Dict[str, Any]],
              fallback_date: str) -> Optional[Dict[str, Any]]:
        t_from = dig(container, f_time)
        if t_from is None:
            return None
        t_end = dig(container, f_end) if f_end else None
        label = hhmm(t_from) + (("-" + hhmm(t_end)) if t_end else "")
        d = dig(container, f_date) if f_date else None

        src = child if isinstance(child, dict) else container
        raw_avail = dig(src, f_avail)
        if isinstance(raw_avail, dict):        # 有些接口用 {code:0} 表示可约
            raw_avail = raw_avail.get("code", raw_avail)
        price = dig(src, f_price)
        if price is None:
            price = dig(container, f_price)

        available = str(raw_avail).strip().lower() in free_vals
        # 占位价（如 9999）= 该场地不开放售卖，即便 book=0 也不能抢
        if available and bad_prices and str(price) in bad_prices:
            available = False

        if child is not None:
            raw_court = dig(child, child_idx)
            if raw_court is None:
                raw_court = dig(child, f_court)
        else:
            raw_court = dig(container, f_court)

        return {
            "date": str(d or fallback_date),
            "time": label,
            "court": norm_court(raw_court, court_prefix, court_suffix),
            "available": available,
            "raw_status": raw_avail,
            "price": price,
            "minutes": slot_minutes(t_from, t_end),
        }

    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if children:
            kids = dig(it, children) or []
            if not isinstance(kids, list):
                kids = [kids]
            for k in kids:
                row = build(it, k, date_str)
                if row:
                    out.append(row)
        else:
            row = build(it, None, date_str)
            if row:
                out.append(row)
    return out


def slot_key(s: Dict[str, Any]) -> str:
    return "%s|%s|%s" % (s["date"], s["time"], s["court"])


def in_time_window(slot: Dict[str, Any], windows: List[str]) -> bool:
    """
    windows 形如 ['20:00-20:30']，为空则不过滤。

    判定用「**场次完整落在窗口内**」，而不是「起始时间落在窗口内」——
    后者会让 ['20:00-20:30'] 把 20:30-21:00 这个场次也带进来，不符合直觉。
    场次时间形如 '20:00-20:30'（有结束时间）或 '20:00'（只有开始时间，退化为起点判定）。
    """
    if not windows:
        return True
    m = re.match(r"^(\d{1,2}):(\d{2})(?:\D+(\d{1,2}):(\d{2}))?",
                 str(slot.get("time") or ""))
    if not m:
        return True
    start = int(m.group(1)) * 60 + int(m.group(2))
    end = int(m.group(3)) * 60 + int(m.group(4)) if m.group(3) else start
    for w in windows:
        try:
            a, b = w.split("-")
            ah, am = (a.split(":") + ["0"])[:2]
            bh, bm = (b.split(":") + ["0"])[:2]
            ws, we = int(ah) * 60 + int(am), int(bh) * 60 + int(bm)
            if start >= ws and end <= we:
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------- 快照状态

def state_path(venue: Dict[str, Any], state_dir: str) -> str:
    return os.path.join(state_dir, "%s.json" % venue["id"])


def load_state(venue: Dict[str, Any], state_dir: str) -> Dict[str, Any]:
    p = state_path(venue, state_dir)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"available": [], "known_dates": [], "updated_at": None}


def save_state(venue: Dict[str, Any], state_dir: str, available_keys: List[str],
               known_dates: Optional[List[str]] = None,
               extra: Optional[Dict[str, Any]] = None) -> None:
    os.makedirs(state_dir, exist_ok=True)
    old = load_state(venue, state_dir)
    payload = {"available": sorted(available_keys), "updated_at": now_str()}
    # known_dates 取并集：日期只会向前推进。若某天因为「只剩已过期时段」而暂时查不到，
    # 用并集可避免它从记录里掉出、之后又被误判成「刚开放预订」。
    merged = set(old.get("known_dates") or [])
    if known_dates is not None:
        merged |= set(known_dates)
    payload["known_dates"] = sorted(merged)
    if extra:
        payload.update(extra)
    elif old.get("max_valid_id"):
        payload["max_valid_id"] = old["max_valid_id"]
    with open(state_path(venue, state_dir), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def release_log_path(venue: Dict[str, Any], state_dir: str) -> str:
    return os.path.join(state_dir, "%s-release.json" % venue["id"])


def update_release_log(venue: Dict[str, Any], state_dir: str,
                       dates_with_data: List[str]) -> Dict[str, str]:
    """
    记录「某个日期第一次被观测到已开放预订」的时刻。
    连续跑几天，就能从这份日志里读出放号规律（最早出现时间）。
    返回本次新增的记录。
    """
    p = release_log_path(venue, state_dir)
    data: Dict[str, str] = {}
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    added: Dict[str, str] = {}
    for d in dates_with_data:
        if d not in data:
            data[d] = now_str()
            added[d] = data[d]
    os.makedirs(state_dir, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return added


# ---------------------------------------------------------------- 号源发现

def scan_jobs(venue: Dict[str, Any], args: argparse.Namespace,
              errors: List[str]) -> List[Dict[str, Any]]:
    """
    把 venue.timetable_ids 配置展开成一组「候选号源」。
    返回 [{"ID": 1048, "DATE": None}] 形式；DATE 为 None 表示由响应决定日期。
    """
    spec = venue.get("timetable_ids") or {}
    mode = spec.get("mode", "scan")

    if mode == "map":
        out = [{"ID": int(i), "DATE": d} for d, i in (spec.get("map") or {}).items()]
        if not out:
            errors.append("timetable_ids.mode=map 但 map 为空")
        return out

    lo_cfg = int(spec.get("from", 1))
    hi_cfg = int(spec.get("to", 0))
    back = int(spec.get("window_before", 4))
    fwd = int(spec.get("window_after", 10))

    frontier = 0
    age_h: Optional[float] = None
    state_dir = getattr(args, "state_dir_resolved", None)
    if state_dir and os.path.isdir(state_dir):
        st = load_state(venue, state_dir)
        frontier = int(st.get("max_valid_id") or 0)
        upd = st.get("updated_at")
        if upd:
            try:
                age_h = (dt.datetime.now()
                         - dt.datetime.strptime(str(upd), "%Y-%m-%d %H:%M:%S")
                         ).total_seconds() / 3600.0
            except (ValueError, TypeError):
                age_h = None

    full = bool(getattr(args, "full_scan", False))

    # ---- 状态过期保护 ------------------------------------------------------
    # id 空间每天都在往前长（本项目实测约 2 个/天），而 frontier±N 每轮**最多
    # 只前进 window_after 个**。所以停用几天之后，frontier 就再也追不上了。
    #
    # 实测踩过：停用 24 天（id 前进约 48），而 window_after=10 ——
    # 每轮只挪 10 个，要连续跑 5 轮（5 小时）才能恢复。更糟的是这段空窗
    # 恰好覆盖上午的放号窗口，等于整整一个早上的轮询全部空转。
    #
    # 处理：按「停了多久」估算 id 前进了多少，一次性跨过去。
    # 速率和阈值都可配（timetable_ids.stale_hours / id_per_day）。
    stale_h = float(spec.get("stale_hours", 12))
    per_day = float(spec.get("id_per_day", 2))
    stale = (not full) and frontier > 0 and age_h is not None and age_h > stale_h

    if full:
        lo, hi = lo_cfg, (hi_cfg or lo_cfg + 24)
    elif not frontier:
        # 首次运行：只从下界往上扫一段，找到前沿即可，不必扫满
        lo, hi = lo_cfg, min(hi_cfg, lo_cfg + 24) if hi_cfg else lo_cfg + 24
    elif stale:
        advance = int(age_h / 24.0 * per_day + 0.999) + fwd + 10
        advance = min(advance, 2000)          # 兜底，别一下子扫到天上去
        lo = max(lo_cfg, frontier - back)
        # 这里**故意不受 hi_cfg 限制**：to 也是当初定的，同样会过期。
        hi = frontier + advance
        log("状态已过期 %.1f 小时（frontier=%d），直接跳到 %d 扫描，不再逐轮挪 10 个"
            % (age_h, frontier, hi))
    else:
        lo = max(lo_cfg, frontier - back)
        # 上界**故意不用 hi_cfg 封顶**：`to` 是当初写死的常量，而 id 每天都在往前长，
        # 一旦 frontier 顶到 to 就再也前进不了 —— 实测 to=1120、真实前沿已到 1116，
        # 照原逻辑两天后就会静默卡死（每轮都扫同一段，永远发现不了新日期）。
        # frontier+fwd 本身就是有界的，不存在「扫飞」的风险。
        hi = frontier + fwd
    if hi < lo:
        hi = lo
    return [{"ID": i, "DATE": None} for i in range(lo, hi + 1)]


# ---------------------------------------------------------------- 核心拉取

def fetch_venue(venue: Dict[str, Any], dates: List[str], args: argparse.Namespace
                ) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, Any]]:
    """返回 (所有场次, 错误信息列表, 元信息)"""
    req = venue.get("request") or {}
    all_slots: List[Dict[str, Any]] = []
    errors: List[str] = []
    meta: Dict[str, Any] = {"hit": 0, "miss": 0, "max_valid_id": 0, "jobs": 0}
    headers = dict(req.get("headers") or {})
    if "Cookie" in headers or "cookie" in headers:
        errors.append("注意：配置里含 Cookie/Token，请勿把该配置文件提交到公开仓库")

    jobs: List[Dict[str, Any]]
    if req.get("mock"):
        jobs = [{"ID": None, "DATE": d} for d in dates]
    elif venue.get("timetable_ids"):
        jobs = scan_jobs(venue, args, errors)
    else:
        jobs = [{"ID": None, "DATE": d} for d in dates]
    meta["jobs"] = len(jobs)

    for job in jobs:
        ctx = {"DATE": job.get("DATE") or "",
               "TIMETABLE_ID": "" if job.get("ID") is None else str(job["ID"]),
               "TS": str(int(time.time() * 1000)), "VENUE_ID": venue["id"]}
        if req.get("mock"):
            payload = mock_response(venue, job["DATE"])
        else:
            if not req.get("url"):
                errors.append("%s 未配置 request.url（尚未抓包），跳过" % venue["id"])
                break
            url = render(req["url"], ctx)
            body = render(req.get("body"), ctx)
            if str(req.get("method", "GET")).upper() == "GET" and isinstance(body, dict):
                url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(body)
                body = None
            try:
                payload = http_fetch(url, req.get("method", "GET"), render(headers, ctx), body,
                                     int(req.get("timeout", 15)),
                                     bool(args.no_proxy) or bool(req.get("no_proxy")),
                                     req.get("proxy"))
            except Exception as e:  # noqa: BLE001
                errors.append("%s @%s 请求异常: %s" % (venue["id"], ctx["TIMETABLE_ID"] or ctx["DATE"], e))
                continue
        got = extract_slots(venue, payload, job.get("DATE") or "")
        if got:
            meta["hit"] += 1
            if job.get("ID"):
                meta["max_valid_id"] = max(meta["max_valid_id"], int(job["ID"]))
        else:
            meta["miss"] += 1
        all_slots.extend(got)
    return all_slots, errors, meta


def apply_filters(venue: Dict[str, Any], slots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 watch 配置过滤：时间窗 / 只看今天及以后 / 只看某种时长的场次。"""
    watch = venue.get("watch") or {}
    out = slots
    windows = watch.get("time_windows") or []
    if windows:
        out = [s for s in out if in_time_window(s, windows)]
    if watch.get("skip_past_dates"):
        today = dt.date.today().strftime("%Y-%m-%d")
        out = [s for s in out if str(s.get("date")) >= today]
    mins = watch.get("slot_minutes") or []
    if mins:
        want = {int(x) for x in mins}
        out = [s for s in out if s.get("minutes") in want]
    return out


# ---------------------------------------------------------------- 报告

def series_label(minutes: Any) -> str:
    if minutes:
        return "%s 分钟场" % minutes
    return "未标注时长"


def render_report(venue: Dict[str, Any], all_slots: List[Dict[str, Any]],
                  new_keys: List[str], first_run: bool, errors: List[str],
                  new_dates: Optional[List[str]] = None) -> str:
    lines: List[str] = []
    lines.append("# 场馆场次监测报告")
    lines.append("")
    lines.append("- 场馆：%s (`%s`)" % (venue.get("name"), venue.get("id")))
    lines.append("- 小程序：%s" % venue.get("miniprogram", "-"))
    lines.append("- 监测时间：%s" % now_str())
    lines.append("- 快照状态：%s" % ("首次运行（仅建立基线，不算新增）" if first_run else "与上次比对"))
    lines.append("")

    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for s in all_slots:
        by_date.setdefault(s["date"], []).append(s)

    total_free = sum(1 for s in all_slots if s["available"])
    lines.append("## 汇总")
    lines.append("")
    lines.append("- 抓取场次：%d 条，其中空余：**%d** 条" % (len(all_slots), total_free))
    lines.append("- 覆盖日期：%d 个" % len(by_date))
    lines.append("- 本次新增空余：**%d** 条" % len(new_keys))
    if new_dates:
        lines.append("- 刚开放预订的日期（放号观测）：**%s**" % ", ".join(new_dates))
    if errors:
        lines.append("")
        lines.append("## 异常")
        lines.append("")
        for e in errors:
            lines.append("- %s" % e)
    lines.append("")

    mode = (venue.get("report") or {}).get("mode", "flat")

    for d in sorted(by_date):
        slots = by_date[d]
        free = [s for s in slots if s["available"]]
        lines.append("## %s（空余 %d / %d）" % (d, len(free), len(slots)))
        lines.append("")
        if not slots:
            lines.append("_无数据_")
            lines.append("")
            continue

        if mode == "group":
            # 按「时长 + 时段」聚合成一行，适合场地多（12 块 × 29 段）的场馆
            buckets: Dict[Tuple[Any, str], List[Dict[str, Any]]] = {}
            for s in slots:
                buckets.setdefault((s.get("minutes"), s["time"]), []).append(s)
            by_series: Dict[Any, List[Tuple[str, List[Dict[str, Any]]]]] = {}
            for (mins, t), grp in buckets.items():
                by_series.setdefault(mins, []).append((t, grp))
            for mins in sorted(by_series, key=lambda x: (x is None, x)):
                rows = sorted(by_series[mins], key=lambda x: x[0])
                n_free = sum(1 for _, g in rows for s in g if s["available"])
                n_all = sum(len(g) for _, g in rows)
                lines.append("### %s（空余 %d / %d）" % (series_label(mins), n_free, n_all))
                lines.append("")
                lines.append("| 时段 | 空余 | 可抢场地 | 价格 | 新增 |")
                lines.append("| --- | --- | --- | --- | --- |")
                for t, g in rows:
                    gf = [s for s in g if s["available"]]
                    names = "、".join(sorted({str(s["court"]) for s in gf})) if gf else "—"
                    price = next((s["price"] for s in gf), g[0]["price"] if g else None)
                    mark = "🆕" if any(slot_key(s) in new_keys for s in gf) else ""
                    lines.append("| %s | %d/%d | %s | %s | %s |" % (
                        t, len(gf), len(g), names,
                        price if price is not None else "-", mark))
                lines.append("")
        else:
            lines.append("| 时段 | 场地 | 状态 | 价格 | 新增 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for s in sorted(slots, key=lambda x: (x["time"], x["court"])):
                mark = "🆕" if slot_key(s) in new_keys else ""
                lines.append("| %s | %s | %s | %s | %s |" % (
                    s["time"], s["court"],
                    "✅空余" if s["available"] else "❌已满",
                    s["price"] if s["price"] is not None else "-",
                    mark))
            lines.append("")

    if new_keys:
        lines.append("## 新增空余清单（可抢）")
        lines.append("")
        for k in new_keys:
            lines.append("- %s" % k)
        lines.append("")
    return "\n".join(lines)


def write_outputs(venue: Dict[str, Any], report: str, all_slots: List[Dict[str, Any]],
                  out_dir: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not out_dir:
        return None, None
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    md = os.path.join(out_dir, "venue-%s-%s.md" % (venue["id"], stamp))
    js = os.path.join(out_dir, "venue-%s-%s.json" % (venue["id"], stamp))
    with open(md, "w", encoding="utf-8") as f:
        f.write(report)
    with open(js, "w", encoding="utf-8") as f:
        json.dump({"venue": venue.get("name"), "checked_at": now_str(), "slots": all_slots},
                  f, ensure_ascii=False, indent=2)
    return md, js


def prune_outputs(out_dir: Optional[str], keep_days: int) -> int:
    """
    删掉 out_dir 里超过 keep_days 天的 venue-*.md / .json，返回删除个数。

    长期挂机必须做这件事：即便「无变化不落盘」已经砍掉大部分冗余，
    有变化的日子仍会持续累积。保留窗口按天滚，磁盘占用就有上界。
    """
    if not out_dir or keep_days <= 0 or not os.path.isdir(out_dir):
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for name in os.listdir(out_dir):
        if not name.startswith("venue-") or not (name.endswith(".md") or name.endswith(".json")):
            continue
        p = os.path.join(out_dir, name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
                removed += 1
        except OSError:
            pass
    return removed


NOTIFY_KINDS = [
    ("wecom", "企业微信群机器人 / 钉钉群机器人"),
    ("feishu", "飞书群机器人"),
    ("serverchan", "Server酱（推到自己的微信，个人最省事）"),
    ("pushplus", "PushPlus（推到自己的微信）"),
    ("bark", "Bark（推到 iPhone）"),
    ("generic", "通用 webhook：POST {\"title\":..., \"text\":...}"),
]


def mask_url(u: str) -> str:
    """打印用：把长 token 打码，避免日志里泄漏 webhook 密钥。"""
    return re.sub(r"[A-Za-z0-9_\-]{16,}", "****", u or "")


def serverchan_target(url: str) -> str:
    """
    Server酱有**两个互不兼容的版本**，SendKey 不通用、API 地址也不同：

      · Server酱Turbo —— SendKey 以 `SCT` 开头
        → https://sctapi.ftqq.com/<SendKey>.send
      · Server酱³    —— SendKey 形如 `sctp{uid}t…`
        → https://<uid>.push.ft07.com/send/<SendKey>.send   （uid 是 sctp 与 t 之间的数字）

    也接受用户直接粘贴完整地址。
    """
    s = (url or "").strip()
    if s.startswith("http"):
        return s if s.rstrip("/").endswith(".send") else s.rstrip("/") + ".send"
    s = s.strip("/")
    m = re.match(r"^sctp(\d+)t", s, re.I)
    if m:
        return "https://%s.push.ft07.com/send/%s.send" % (m.group(1), s)
    return "https://sctapi.ftqq.com/%s.send" % s


def notify_quota_path(venue: Dict[str, Any], state_dir: str) -> str:
    return os.path.join(state_dir, "%s-notify.json" % venue["id"])


def notify_quota_ok(venue: Dict[str, Any], state_dir: str, limit: int) -> Tuple[bool, int]:
    """每日推送配额检查。limit<=0 表示不限。"""
    if not limit or limit <= 0:
        return True, 0
    today = dt.date.today().isoformat()
    data: Dict[str, Any] = {}
    p = notify_quota_path(venue, state_dir)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    if data.get("date") != today:
        return True, 0
    used = int(data.get("count") or 0)
    return used < limit, used


def notify_quota_bump(venue: Dict[str, Any], state_dir: str) -> None:
    today = dt.date.today().isoformat()
    p = notify_quota_path(venue, state_dir)
    data: Dict[str, Any] = {}
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    data["count"] = int(data.get("count") or 0) + 1
    os.makedirs(state_dir, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def digest_state_path(venue: Dict[str, Any], state_dir: str) -> str:
    return os.path.join(state_dir, "%s-digest.json" % venue["id"])


def digest_enabled(venue: Dict[str, Any]) -> bool:
    """摘要功能是否启用：**既配了时刻、也配了推送地址**。

    两个都要求，是为了让模板里可以放心留着 `daily_digest_at` 示例值 ——
    接收方还没填 webhook 时不会触发一堆无用计算，也不会被误判成「摘要该发却没发」。
    """
    ncfg = venue.get("notify") or {}
    return bool(str(ncfg.get("daily_digest_at") or "").strip()) and bool(ncfg.get("webhook_url"))


def digest_target_min(venue: Dict[str, Any]) -> Optional[int]:
    """配置的摘要时刻（分钟数）。没配返回 None。"""
    at = str((venue.get("notify") or {}).get("daily_digest_at") or "").strip()
    return hhmm_to_min(at) if at else None


def digest_sent_today(venue: Dict[str, Any], state_dir: str) -> bool:
    try:
        with open(digest_state_path(venue, state_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return data.get("date") == dt.date.today().isoformat()


def digest_due(venue: Dict[str, Any], state_dir: str) -> bool:
    """今天的每日摘要该发了吗？

    语义是「**过了点就补发**」，不是「正好在那个时刻才发」——
    因为计划任务是整点触发的，10:35 那一刻根本没有进程在跑。

    两道保障：
      1. `watch` 在轮询窗口结束后会顺延到摘要时刻补跑一次（见 cmd_watch），
         所以正常情况下摘要在 10:35 准时发出；
      2. 万一那次被中断（关机 / 强杀），下一个整点（11:00）的 check 会兜底补发。
    """
    target = digest_target_min(venue)
    if not digest_enabled(venue) or target is None or digest_sent_today(venue, state_dir):
        return False
    return now_min() >= target


def digest_mark_sent(venue: Dict[str, Any], state_dir: str) -> None:
    os.makedirs(state_dir, exist_ok=True)
    with open(digest_state_path(venue, state_dir), "w", encoding="utf-8") as f:
        json.dump({"date": dt.date.today().isoformat(), "sent_at": now_str()},
                  f, ensure_ascii=False, indent=2)


def build_notify(kind: str, url: str, title: str, text: str,
                 cfg: Optional[Dict[str, Any]] = None
                 ) -> Tuple[str, Dict[str, str], Any]:
    """
    按渠道拼出 (最终URL, 请求头, 请求体)。

    各家 webhook 的报文格式互不兼容，这是「配了但收不到」的最常见原因：
      企业微信/钉钉  {"msgtype":"text","text":{"content":...}}
      飞书          {"msg_type":"text","content":{"text":...}}
      Server酱      {"title":...,"desp":...}
      PushPlus      {"token":...,"title":...,"content":...}
      Bark          {"title":...,"body":...}

    便利处理：Server酱 / PushPlus / Bark 允许只填密钥（不带 URL），会自动补全。
    """
    cfg = cfg or {}
    kind = (kind or "wecom").lower()
    json_hdr = {"Content-Type": "application/json"}

    if kind in ("wecom", "weixin", "qywx", "dingtalk", "dingding"):
        return url, json_hdr, {"msgtype": "text", "text": {"content": text}}

    if kind == "feishu":
        body: Dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
        # 若机器人开了「签名校验」，必须带 timestamp + sign，否则飞书返回
        # {"code":19021,"msg":"sign match fail"}。密钥填进 notify.feishu_secret。
        secret = cfg.get("feishu_secret")
        if secret:
            ts = str(int(time.time()))
            digest = hmac.new(("%s\n%s" % (ts, secret)).encode("utf-8"),
                              digestmod=hashlib.sha256).digest()
            body["timestamp"] = ts
            body["sign"] = base64.b64encode(digest).decode("utf-8")
        return url, json_hdr, body

    if kind == "serverchan":
        return serverchan_target(url), json_hdr, {"title": title, "desp": text}

    if kind == "pushplus":
        token = cfg.get("token", "")
        if not url.startswith("http"):
            token, url = url.strip(), "https://www.pushplus.plus/send"
        return url, json_hdr, {"token": token, "title": title, "content": text,
                               "template": cfg.get("template", "txt")}

    if kind == "bark":
        if not url.startswith("http"):
            url = "https://api.day.app/%s" % url.strip("/")
        return url, json_hdr, {"title": title, "body": text}

    return url, json_hdr, {"title": title, "text": text}


def notify(venue: Dict[str, Any], text: str,
           state_dir: Optional[str] = None) -> bool:
    """可选推送：把消息发到配置的渠道。未配置则静默跳过。

    返回是否**确实发出去了**。调用方据此决定要不要记账
    （比如每日摘要要标记「今天已发」，否则会重复补发）。
    """
    cfg = venue.get("notify") or {}
    url = cfg.get("webhook_url")
    if not url:
        return False
    kind = str(cfg.get("type", "wecom")).lower()

    # 每日配额：Server酱免费版每天只有 5 条，超了会静默失败，不如提前拦住并说明。
    limit = cfg.get("daily_limit")
    if limit is None:
        limit = 5 if kind == "serverchan" else 0
    limit = int(limit)
    if state_dir:
        ok, used = notify_quota_ok(venue, state_dir, limit)
        if not ok:
            log("[notify] ⚠️  今日推送已达上限 %d 条（已用 %d），本条跳过。" % (limit, used))
            log("         报告已落盘，可直接查看；需要更多额度请升级套餐或改用其他渠道")
            log("         （飞书群机器人 100 次/分钟，基本不受限）。")
            return False
    try:
        title = cfg.get("title") or ("【%s】有场次放出" % venue.get("name"))
        final_url, headers, body = build_notify(kind, url, title, text, cfg)
        # 默认绕过系统代理：推送目标是公网 IM，走本机代理（如 Charles）只会让它
        # 在代理关闭时静默失败。确有需要时用 notify.no_proxy=false 改回。
        http_fetch(final_url, "POST", headers, body, 15,
                   bool(cfg.get("no_proxy", True)), cfg.get("proxy"))
        log("[notify] 已推送 -> %s" % mask_url(final_url))
        if state_dir:
            notify_quota_bump(venue, state_dir)
        return True
    except Exception as e:  # noqa: BLE001
        log("[notify] 推送失败: %s" % e)
        return False


# ---------------------------------------------------------------- 子命令

def cmd_doctor(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    log("=" * 62)
    log("配置体检  (%s)" % args.config)
    log("=" * 62)
    ok_cnt = 0
    for v in cfg["venues"]:
        missing = [f for f in REQUIRED_VENUE_FIELDS if not v.get(f)]
        conf = is_configured(v)
        is_mock = bool((v.get("request") or {}).get("mock"))
        if is_mock:
            status = "🧪 mock 演示"
        elif conf and not missing:
            status = "✅ 已就绪"
        else:
            status = "⚠️  待抓包"
        if not is_enabled(v):
            # 禁用状态优先显示 —— 否则用户会以为「已就绪」的场馆真的在跑
            status = "⏸️  已禁用（enabled=false，check/watch 会跳过）"
        log("")
        log("[%s] %s  ->  %s" % (v.get("id", "?"), v.get("name", "?"), status))
        log("     小程序 : %s" % v.get("miniprogram", "-"))
        log("     接口   : %s" % ("（内置 mock 数据源，无需联网）" if is_mock
                                 else ((v.get("request") or {}).get("url") or "（空）")))
        if v.get("timetable_ids"):
            spec = v["timetable_ids"]
            log("     号源   : timetable_ids.mode=%s  from=%s to=%s  frontier±(%s,%s)" % (
                spec.get("mode", "scan"), spec.get("from"), spec.get("to"),
                spec.get("window_before", 4), spec.get("window_after", 10)))
        else:
            log("     关注日期: %s" % ",".join(build_dates(v, None)))
        w = v.get("watch") or {}
        if w.get("slot_minutes"):
            log("     只看   : %s 分钟场" % w["slot_minutes"])
        if w.get("time_windows"):
            log("     时段窗 : %s" % w["time_windows"])

        # 凭证有效期（JWT）
        for hk, hv in ((v.get("request") or {}).get("headers") or {}).items():
            if isinstance(hv, str) and hv.count(".") == 2:
                exp = jwt_exp(hv)
                if exp:
                    left = (exp - dt.datetime.now()).days
                    flag = "✅" if left > 3 else "⚠️ "
                    log("     凭证   : %s 到期 %s（还剩 %d 天）%s" % (
                        hk, exp.strftime("%Y-%m-%d %H:%M"), left,
                        "  ← 快过期，需重新抓包" if left <= 3 else ""))
                    log("              %s" % flag)
        if missing:
            log("     ❌ 缺字段: %s" % ", ".join(missing))
        ph = placeholder_headers(v)
        if ph and not is_mock:
            log("     ⚠️  这些请求头还留着占位值，需要换成抓包得到的真值: %s" % ", ".join(ph))
        if not conf and not is_mock:
            log("     → 抓包后把 request.url / headers / body 填进配置即可启用")
            log("     → 步骤见 references/recon-playbook.md")
        if conf and not missing and is_enabled(v):
            ok_cnt += 1
    enabled_cnt = sum(1 for v in cfg["venues"] if is_enabled(v))
    total_cnt = len(cfg["venues"])
    log("")
    log("=" * 62)
    log("就绪场馆：%d / %d" % (ok_cnt, total_cnt))
    if enabled_cnt != total_cnt:
        log("已启用  ：%d / %d（%d 个被 enabled=false 跳过）"
            % (enabled_cnt, total_cnt, total_cnt - enabled_cnt))
    if enabled_cnt == 0:
        log("⚠️  没有任何已启用的场馆，check / watch 会直接报错。")
    log("=" * 62)
    return 0


def run_venue(venue: Dict[str, Any], args: argparse.Namespace, state_dir: str,
              announce: bool = True) -> Dict[str, Any]:
    """拉取 + 过滤 + 比对 + 落盘 + 通知。check / probe 共用。"""
    dates = build_dates(venue, args.date)
    all_slots, errors, meta = fetch_venue(venue, dates, args)
    raw_n = len(all_slots)
    all_slots = apply_filters(venue, all_slots)

    prev = load_state(venue, state_dir)
    prev_set = set(prev.get("available") or [])
    prev_dates = set(prev.get("known_dates") or [])
    first_run = not prev.get("updated_at")
    cur_free = [slot_key(s) for s in all_slots if s["available"]]
    cur_set = set(cur_free)
    new_keys = sorted(cur_set - prev_set) if not first_run else []
    gone_keys = sorted(prev_set - cur_set) if not first_run else []

    cur_dates = sorted({s["date"] for s in all_slots})
    new_dates = [d for d in cur_dates if d not in prev_dates] if not first_run else []
    release_added = update_release_log(venue, state_dir, cur_dates)

    if announce:
        log("命中号源 %d 个（共探测 %d），原始场次 %d 条，过滤后 %d 条，空余 %d 条，覆盖日期 %d 个%s" % (
            meta.get("hit", 0), meta.get("jobs", 0), raw_n, len(all_slots), len(cur_free),
            len(cur_dates), "（首次运行，建立基线）" if first_run else ""))
        for e in errors:
            log("[warn] %s" % e)
        if new_dates:
            log("📅 有日期刚开放预订（放号观测）：%s" % ", ".join(new_dates))
            for d in new_dates:
                log("   - %s  首次观测到：%s" % (d, release_added.get(d, "?")))
        if new_keys:
            log("🆕 新增空余 %d 条：" % len(new_keys))
            for k in new_keys[:40]:
                log("   - %s" % k)
            if len(new_keys) > 40:
                log("   ... 其余 %d 条见报告" % (len(new_keys) - 40))
        if gone_keys:
            log("➖ 已消失 %d 条" % len(gone_keys))
        if not new_keys and not new_dates and not first_run:
            log("无新增空余。")

    report = render_report(venue, all_slots, new_keys, first_run, errors, new_dates)
    # 高频轮询时跳过「无变化」的落盘：窗口内每 90 秒一份报告，其中 99% 是冗余，
    # 一年能堆出十万个文件、上 GB 空间。只在真的有变化时才留档。
    skip_save = (getattr(args, "output_only_on_change", False)
                 and not first_run and not new_keys and not new_dates)
    if skip_save:
        md = js = None
    else:
        md, js = write_outputs(venue, report, all_slots, getattr(args, "out_dir", None))
        if md and announce:
            log("报告已写入: %s" % md)

    extra = {"max_valid_id": meta.get("max_valid_id") or prev.get("max_valid_id") or 0}
    save_state(venue, state_dir, cur_free, cur_dates, extra)

    return {"slots": all_slots, "new_keys": new_keys, "new_dates": new_dates,
            "first_run": first_run, "errors": errors, "meta": meta, "report": report,
            "md": md, "available_keys": cur_free, "gone_keys": gone_keys}


# ---------------------------------------------------------------- 推送文案

def human_date(d: Any) -> str:
    """'2026-09-18' -> '9/18'（当天/明天再加个前缀，见下）。"""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(d or "").strip())
    if not m:
        return str(d)
    return "%d/%d" % (int(m.group(2)), int(m.group(3)))


def human_date_with_hint(d: Any) -> str:
    """'2026-09-18' -> '9/18（明天）'，让人一眼知道是哪天。"""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(d or "").strip())
    if not m:
        return str(d)
    try:
        target = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return str(d)
    delta = (target - dt.date.today()).days
    hint = {0: "今天", 1: "明天", 2: "后天"}.get(delta)
    if hint is None:
        hint = "今天" if delta < 0 else None
    base = human_date(d)
    return "%s（%s）" % (base, hint) if hint else base


def group_slot_keys(keys: List[str], court_suffix: str = ""
                    ) -> Dict[str, Dict[str, List[str]]]:
    """把 slot key 列表按「日期 -> 时段 -> [场地]」分组。

    key 形如 '2026-09-18|20:00-20:30|12'（内部用 '|' 拼接便于快照比对）。
    展示时把场地名补上后缀（如 '12' -> '12号场'），**只在展示层做，不动 key**。
    """
    grouped: Dict[str, Dict[str, List[str]]] = {}
    for k in keys:
        parts = str(k).split("|")
        if len(parts) < 3:
            continue
        d, t, c = parts[0], parts[1], parts[2]
        if court_suffix and c and not c.endswith(court_suffix):
            c = "%s%s" % (c, court_suffix)
        grouped.setdefault(d, {}).setdefault(t, []).append(c)
    return grouped


def build_digest_text(venue: Dict[str, Any], keys: List[str]) -> str:
    """每日摘要正文：**当前全部**可抢场次一览。

    和告警的区别：告警只说「新增了什么」，摘要说「现在有什么」。
    存在的意义是让用户知道**系统还活着** —— 否则「飞书安静」和「程序挂了」
    从用户角度看完全一样，只能自己手动跑命令确认。
    """
    rspec = venue.get("response") or {}
    court_suffix = str(rspec.get("court_suffix") or "")
    ncfg = venue.get("notify") or {}
    try:
        max_lines = int(ncfg.get("max_slots_in_message", 12))
    except (TypeError, ValueError):
        max_lines = 12

    grouped = group_slot_keys(keys, court_suffix)
    lines: List[str] = []

    if not grouped:
        lines.append("当前没有可抢的空场。")
    else:
        shown = 0
        skipped = 0
        for d in sorted(grouped.keys()):
            slots = grouped[d]
            total = sum(len(v) for v in slots.values())
            lines.append("%s　共 %d 个空场" % (human_date_with_hint(d), total))
            for t, courts in slots.items():
                if shown >= max_lines:
                    skipped += 1
                    continue
                lines.append("%s · %s" % (t, "、".join(courts)))
                shown += 1
            lines.append("")
        while lines and not lines[-1]:
            lines.pop()
        if skipped:
            lines.append("（另有 %d 个时段未列出）" % skipped)

    link = str(venue.get("miniprogram") or "")
    if link.startswith("#小程序"):
        lines += ["", "👉 去小程序订：%s" % link]

    at = str(ncfg.get("daily_digest_at") or "")
    if at:
        lines += ["", "（每日 %s 推一条汇总；一旦出现新空位仍会立即提醒）" % at]

    return "\n".join(lines)


def build_alert_text(venue: Dict[str, Any], new_keys: List[str],
                     new_dates: List[str], report_path: Optional[str] = None) -> str:
    """拼「发现新空位」的推送正文。

    目标是**扫一眼就知道去哪订**，所以：

      * **不放本地文件路径**。早先文案末尾跟一行 `详情：D:\\reports\\...\\xxx.md`，
        在手机上完全没用 —— 点不开、看不懂，还暴露本机目录结构。
        改成放**小程序链接**，收到就能直接去订。
      * **按时段归并**。同一时段有多个场地时合成一行（`21:30-22:00 · 2号场、7号场、9号场`），
        而不是一个场地一行 —— 5 个空位从 5 行压到 3 行，更好扫。
      * **直接写「几号场 + 什么时段」**，不暴露内部的 `日期|时段|场地` key。
      * 日期带「今天/明天」提示，省得心算。
    """
    rspec = venue.get("response") or {}
    court_suffix = str(rspec.get("court_suffix") or "")
    ncfg = venue.get("notify") or {}
    try:
        max_lines = int(ncfg.get("max_slots_in_message", 12))
    except (TypeError, ValueError):
        max_lines = 12

    # 分组：日期 -> 时段 -> [场地]（复用 group_slot_keys，它负责补场地后缀）
    grouped = group_slot_keys(new_keys, court_suffix)

    new_dates = list(new_dates or [])
    order: List[str] = []
    for d in new_dates + list(grouped.keys()):
        if d not in order:
            order.append(d)
    order.sort()          # 按日期先后排，扫起来才顺（别把 9/19 排在 9/18 前面）

    blocks: List[str] = []
    shown = 0
    skipped = 0
    for d in order:
        slots = grouped.get(d) or {}
        total = sum(len(v) for v in slots.values())
        is_new = d in new_dates
        if is_new and total:
            head = "📅 %s 刚开放预订 · 新增 %d 个空场" % (human_date_with_hint(d), total)
        elif is_new:
            head = "📅 %s 刚开放预订（暂时没有空场，先盯着）" % human_date_with_hint(d)
        else:
            head = "🆕 %s · 新增 %d 个空场" % (human_date_with_hint(d), total)

        body: List[str] = []
        for t, courts in slots.items():
            if shown >= max_lines:
                skipped += 1
                continue
            body.append("%s · %s" % (t, "、".join(courts)))
            shown += 1

        blocks.append(head if not body else head + "\n" + "\n".join(body))

    text = "\n\n".join(blocks)
    if skipped:
        text += "\n\n（另有 %d 个时段未列出）" % skipped

    link = str(venue.get("miniprogram") or "")
    if link.startswith("#小程序"):
        text += "\n\n👉 去小程序订：%s" % link

    # 本地路径默认不推 —— 手机上看不了。确有需要（比如推给自己电脑上的脚本）
    # 可以设 notify.include_report_path = true。
    if report_path and ncfg.get("include_report_path"):
        text += "\n\n详情：%s" % report_path

    return text


def cmd_check(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    state_dir = args.state_dir_resolved
    exit_code = 0
    for venue in pick_venues(cfg, args.venue):
        log("")
        log("=" * 62)
        log("监测：%s (%s)" % (venue.get("name"), venue.get("id")))
        log("=" * 62)
        r = run_venue(venue, args, state_dir)
        if not r["slots"] and r["errors"]:
            exit_code = max(exit_code, 2)
            continue
        # --no-notify：只查不发。快照照常更新（这一点很重要 —— 用它重建基线
        # 之后，下一轮就只会推真正的新变化，而不会把「基线过期」误报成新增）。

        no_notify = bool(getattr(args, "no_notify", False))
        if no_notify:
            if r["new_keys"] or r["new_dates"]:
                log("[notify] --no-notify 已开启，跳过变更推送"
                    "（本次新增 %d 条 / 新日期 %d 个，已写入快照）"
                    % (len(r["new_keys"]), len(r["new_dates"])))
            if digest_due(venue, state_dir):
                log("[notify] --no-notify 已开启，跳过每日摘要（不会标记为已发）")
            continue

        if (r["new_keys"] or r["new_dates"]) and not r["first_run"]:
            body = build_alert_text(venue, r["new_keys"], r["new_dates"], r["md"])
            notify(venue, "【%s】有场放出了\n%s" % (venue.get("name"), body), state_dir)

        # 每日摘要：过了配置时刻后的第一次运行发一条，当天不重复。
        # 与告警的分工 —— 告警只说「新增了什么」，摘要说「现在有什么」。
        # 摘要存在的意义是让用户知道系统还活着：否则「飞书安静」和「程序挂了」
        # 从用户角度看完全一样（实测用户就是这么困惑的）。
        if digest_due(venue, state_dir):
            keys = r.get("available_keys") or []
            body = build_digest_text(venue, keys)
            head = "【%s】每日摘要 · 当前 %d 个空场" % (venue.get("name"), len(keys))
            if notify(venue, "%s\n%s" % (head, body), state_dir):
                digest_mark_sent(venue, state_dir)
                log("[notify] 每日摘要已发送（%d 个空场）。" % len(keys))
    return exit_code


# ---------------------------------------------------------------- 系统级调度

def hhmm_to_min(v: Any) -> Optional[int]:
    """'08:50' -> 530。解析不出返回 None。"""
    m = re.match(r"^\s*(\d{1,2})\s*:\s*(\d{2})\s*$", str(v or ""))
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def min_to_hhmm(v: int) -> str:
    return "%02d:%02d" % (v // 60, v % 60)


def now_min() -> int:
    n = dt.datetime.now()
    return n.hour * 60 + n.minute


def parse_windows(windows: List[Any]) -> List[Tuple[int, int]]:
    """['08:50-10:30', '13:50~15:40'] -> [(530, 630), (830, 940)]，按开始时间排序。"""
    out: List[Tuple[int, int]] = []
    for w in windows or []:
        parts = re.split(r"\s*[-~到至]\s*", str(w).strip())
        if len(parts) < 2:
            continue
        s, e = hhmm_to_min(parts[0]), hhmm_to_min(parts[1])
        if s is not None and e is not None and e > s:
            out.append((s, e))
    return sorted(out)


def merge_windows(windows: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """合并重叠或首尾相接的窗口。

    多场馆共用一份 watch 时，各场馆的 poll_windows 会被取并集，
    很容易出现重叠（A 的 09:00-10:30 和 B 的 09:30-11:00）。
    不合并的话 decide_watch_action 会命中第一个匹配的窗口，
    导致「盯到 10:30 就停」而不是盯到 11:00。
    """
    if not windows:
        return []
    out: List[List[int]] = []
    for s, e in sorted(windows):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def decide_watch_action(now: int, windows: List[Tuple[int, int]], lead: int,
                        quiet_before: Optional[int] = None) -> Tuple[str, Any]:
    """
    决定这次 watch 该做什么（纯函数，便于测试）。返回 (action, payload)：

      quiet  早于静默线，什么都不做
      poll   已在窗口内，应轮询到 payload=(start, end)
      wait   距下一窗口 ≤ lead 分钟，应先跑一次、等到 payload=start、再轮询
      once   窗口外，只跑一次就走
    """
    if quiet_before is not None and now < quiet_before:
        return "quiet", None
    for s, e in windows:
        if s <= now <= e:
            return "poll", (s, e)
    nxt: Optional[int] = None
    for s, _e in windows:
        if s > now:
            nxt = s if nxt is None else min(nxt, s)
    if nxt is not None and (nxt - now) <= lead:
        return "wait", nxt
    return "once", None


def _sleep_until_min(target: int, chunk: int = 30) -> None:
    """睡到当天的某个分钟数。分片睡，方便 Ctrl+C 中断。"""
    while True:
        n = dt.datetime.now()
        cur = n.hour * 60 + n.minute
        if cur >= target:
            return
        remain = (target - cur) * 60 - n.second
        time.sleep(max(1, min(remain, chunk)))


def merge_schedules(venues: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把多个场馆的 schedule 段合并成一份（纯函数，便于测试）。

    合并规则（都取「更保守」的一侧，宁可多跑几次也不漏掉窗口）：
      windows   各场馆 poll_windows 的并集，并合并重叠
      interval  取最小值 = 最勤快
      lead      取最大值 = 最早开始预跑
      quiet     取最小值 = 最早解除静默
      keep_days 取最小值 = 最严格清理
    """
    all_windows: List[Tuple[int, int]] = []
    intervals: List[int] = []
    leads: List[int] = []
    quiets: List[int] = []
    keeps: List[int] = []
    for v in venues:
        sched = v.get("schedule") or {}
        all_windows += parse_windows(sched.get("poll_windows"))
        intervals.append(int(sched.get("poll_interval", 90)))
        leads.append(int(sched.get("lead_minutes", 60)))
        q = hhmm_to_min(sched.get("quiet_before"))
        if q is not None:
            quiets.append(q)
        keeps.append(int(sched.get("keep_output_days", 30)))

    return {
        "windows": merge_windows(all_windows),
        "interval": min(intervals) if intervals else 90,
        "lead": max(leads) if leads else 60,
        "quiet": min(quiets) if quiets else None,
        "keep_days": min(keeps) if keeps else 30,
    }


def next_digest_to_wait_for(venues: List[Dict[str, Any]], state_dir: str,
                            window_end: int, grace: int = 45
                            ) -> Optional[int]:
    """轮询窗口结束后，是否该顺延等一会儿再补发摘要？返回要等到的分钟数，或 None。

    纯函数，便于测试。判定条件：今天还有摘要没发，且它的时刻落在
    `(window_end, window_end + grace]` 区间内 —— 只顺延「就差一会儿」的情况，
    避免为了等一个很晚的摘要而让进程空转几小时。

    摘要定在窗口**之内**的情况不需要走这里：轮询循环每 90 秒跑一次 check，
    到点自然就发出去了。这里专门解决「摘要定在窗口结束后不久」的场景
    （例如窗口到 10:30、摘要定在 10:35 —— 那一刻本来没有任何任务在跑）。
    """
    pending = []
    for v in venues:
        t = digest_target_min(v)
        if t is not None and digest_enabled(v) and not digest_sent_today(v, state_dir):
            pending.append(t)
    if not pending:
        return None
    nxt = min(pending)
    return nxt if window_end < nxt <= window_end + grace else None


# ---------------------------------------------------------------- 自动下单


def cmd_watch(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    """
    系统级调度入口 —— 给 Windows 任务计划程序 / launchd / cron 调用。

    与 check 的分工：check 是「跑一次」，watch 是「按时间窗口决定跑几次」。
    设计成给外部调度器用，所以自己不常驻：一次调用最多盯完一个窗口就退出，
    外层只要「每小时调一次」即可，窗口内自然会加密轮询。

    **一次调用覆盖全部已启用场馆** —— 所以计划任务只需要注册一个，
    以后新增场馆不必改动任务。各场馆的 schedule 段会被取并集：
      poll_windows  窗口取并集并合并重叠（interval/lead/quiet 取各自最保守的值）
      poll_interval 窗口内轮询间隔秒（默认 90），多场馆时取最小值 = 最勤快
      lead_minutes  距窗口开始多少分钟内就先跑一次并等待（默认 60），取最大值 = 最早开始
      quiet_before  早于此时刻静默退出，如 "06:00"，取最小值 = 最早解除静默
      keep_output_days 报告保留天数，取最小值 = 最严格清理
    """
    venues = pick_venues(cfg, args.venue)

    # 合并所有被选中场馆的调度参数 —— watch 一次调用覆盖全部场馆，
    # 所以计划任务只需要注册一个，以后加场馆不必改任务。
    sched = merge_schedules(venues)
    windows = sched["windows"]
    interval = sched["interval"]
    lead = sched["lead"]
    quiet = sched["quiet"]
    keep_days = sched["keep_days"]

    cur = now_min()
    action, payload = decide_watch_action(cur, windows, lead, quiet)
    wdesc = ", ".join("%s-%s" % (min_to_hhmm(s), min_to_hhmm(e)) for s, e in windows) or "无"

    log("")
    log("=" * 62)
    log("watch：%d 个场馆   当前 %s" % (len(venues),
                                        dt.datetime.now().strftime("%H:%M:%S")))
    for v in venues:
        log("   · %s (%s)" % (v.get("name"), v.get("id")))
    log("   轮询窗口：%s   间隔 %ds" % (wdesc, interval))
    log("=" * 62)

    # 顺手清理过期报告 —— 长期挂机时保证磁盘占用有上界
    removed = prune_outputs(getattr(args, "out_dir", None), keep_days)
    if removed:
        log("已清理 %d 个超过 %d 天的旧报告。" % (removed, keep_days))

    if action == "quiet":
        log("早于 %s 的静默时段，不发请求，直接退出。" % min_to_hhmm(quiet))
        return 0

    if action == "once":
        log("不在轮询窗口内（窗口：%s），只跑一次。" % wdesc)
        return cmd_check(cfg, args)

    if action == "wait":
        log("距轮询窗口 %s 还有 %d 分钟（≤ %d 分钟），先跑一次，再等到窗口开始。"
            % (min_to_hhmm(payload), payload - cur, lead))
        cmd_check(cfg, args)
        _sleep_until_min(payload)
        start = payload
        end = next((e for s, e in windows if s == payload), payload)
    else:
        start, end = payload
        log("已在轮询窗口 %s-%s 内，每 %d 秒一轮，盯到窗口结束。"
            % (min_to_hhmm(start), min_to_hhmm(end), interval))

    # 轮询阶段只在有变化时落盘：窗口内每 90 秒一份报告，
    # 其中 99% 是「无变化」的冗余，全留档一年会堆出十万个文件。
    args.output_only_on_change = True

    rounds = 0
    while True:
        rounds += 1
        rc = cmd_check(cfg, args)
        if now_min() >= end:
            break
        time.sleep(interval)
    log("")
    log("watch：窗口 %s-%s 已结束，本次共执行 %d 轮 check。"
        % (min_to_hhmm(start), min_to_hhmm(end), rounds))

    # 每日摘要通常定在整点之间（如 10:35），那一刻没有任务在跑。
    # 如果它就在本次轮询结束后不久，就等到那个点补跑一次，让摘要准时发出，
    # 而不是拖到下一个整点（10:35 → 11:00，晚了 25 分钟）。
    # 万一这里被中断（关机 / 强杀），下一个整点的 check 会兜底补发。
    wait_to = next_digest_to_wait_for(venues, args.state_dir_resolved, end)
    if wait_to is not None:
        log("每日摘要定在 %s，等到那时补跑一次再退出。" % min_to_hhmm(wait_to))
        _sleep_until_min(wait_to)
        rc = cmd_check(cfg, args)

    return rc


def cmd_status(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    """
    调度自检：最近几次运行正常吗？快照多久没更新了？

    回答的是「它现在还活着吗」。长期无人值守最容易出的问题不是报错，
    而是**静默失效** —— 路径变了、任务被删、python 升级了，
    表面一切正常，实际早就不跑了，而用户要等到某天才发现。
    """
    venue = pick_venues(cfg, args.venue)[0]
    sched = venue.get("schedule") or {}
    log_dir = os.path.join(args.state_dir_resolved, "scheduler-logs")

    log("")
    log("=" * 62)
    log("调度自检：%s (%s)" % (venue.get("name"), venue.get("id")))
    log("=" * 62)

    # 1) 收集所有运行记录
    #    按出现顺序扫描 start / end 来配对，**不要按下标配对** ——
    #    一次被中断的运行（有 start 没 end，例如机器被关机、任务被强杀 / Ctrl+C）
    #    会让后面所有记录错位，把「退出码」张冠李戴到别的运行上。
    runs: List[Dict[str, Any]] = []
    if os.path.isdir(log_dir):
        for name in sorted(os.listdir(log_dir)):
            if not name.endswith(".log"):
                continue
            try:
                with open(os.path.join(log_dir, name), "r", encoding="utf-8",
                          errors="replace") as f:
                    txt = f.read()
            except OSError:
                continue
            for m in re.finditer(
                    r"={10} start (\S+ \S+) ={10}"
                    r"|-{10} end (\S+ \S+) \(exit=(\d+)\) -{10}", txt):
                if m.group(1):
                    runs.append({"start": m.group(1), "end": None, "exit": None})
                elif runs and runs[-1]["end"] is None:
                    # end 收束「最近一个还没闭合的 start」
                    runs[-1]["end"] = m.group(2)
                    runs[-1]["exit"] = m.group(3)

    if not runs:
        log("⚠️  还没有任何调度运行记录。")
        log("    日志目录：%s" % log_dir)
        log("")
        log("    若计划任务刚装好，等下一个整点再看；若已过整点仍没有，检查任务是否还在：")
        log("      Get-ScheduledTask -TaskName 'VenueBookingMonitor'")
        return 2

    runs.sort(key=lambda r: r["start"])
    log("最近 %d 次运行：" % min(6, len(runs)))
    for r in runs[-6:]:
        dur = ""
        if r["end"]:
            try:
                a = dt.datetime.strptime(r["start"].split(".")[0], "%Y-%m-%d %H:%M:%S")
                b = dt.datetime.strptime(r["end"].split(".")[0], "%Y-%m-%d %H:%M:%S")
                dur = "用时 %.1f 秒" % (b - a).total_seconds()
            except ValueError:
                pass
        else:
            dur = "⚠️ 未正常结束（有 start 无 end）"
        log("  %-21s 退出码 %-3s %s" % (r["start"], r["exit"] or "?", dur))

    # 2) 判定是否正常
    last = runs[-1]
    try:
        last_dt = dt.datetime.strptime(last["start"].split(".")[0], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        last_dt = dt.datetime.now()
    age_min = (dt.datetime.now() - last_dt).total_seconds() / 60.0

    quiet = hhmm_to_min(sched.get("quiet_before"))
    nm = now_min()
    in_active_hours = (quiet is None or nm >= quiet) and nm <= 23 * 60

    problems: List[str] = []
    if last["end"] is None:
        problems.append(
            "最后一次运行没有正常结束（有 start 无 end）。常见原因：机器被关机/休眠，"
            "或任务被强制结束（Ctrl+C / 注销登录）。这一次的轮询窗口被中断了。")
    if last["exit"] not in (None, "0"):
        problems.append("最后一次退出码为 %s（非 0），说明脚本执行时报错了" % last["exit"])
    if in_active_hours and age_min > 90:
        problems.append("现在属于活跃时段（%s–23:00），却已有 %.0f 分钟没有任何运行记录"
                        % (min_to_hhmm(quiet) if quiet is not None else "?", age_min))

    # 超过 24 小时没运行 —— 不管现在是不是活跃时段都值得报。
    # 实测踩过：任务被停用后，在**非活跃时段**跑 status 会显示「✅ 正常」
    # （因为上面那条只在活跃时段才判），而实际上它已经 25 天没跑了。
    # 这个假阴性很危险：用户以为在监控，其实早就停了。
    if age_min > 24 * 60:
        problems.append(
            "最后一次运行已是 %.1f 天前（%s）。如果这不是你有意暂停的，"
            "先确认计划任务有没有被停用：\n"
            "      powershell -ExecutionPolicy Bypass -File scripts\\setup-scheduler.ps1 -Action status"
            % (age_min / 1440.0, last["start"]))

    # 摘要该发却没发：说明补发机制没兜住（比如整天都没运行）。
    # 这是「用户以为系统活着、其实早就没动静」的典型信号。
    _dt = digest_target_min(venue)
    if (digest_enabled(venue) and _dt is not None and nm > _dt + 120
            and not digest_sent_today(venue, args.state_dir_resolved)):
        problems.append("每日摘要定在 %s，现在已过 2 小时多却还没发 —— 说明补发那次运行也没跑成"
                        % min_to_hhmm(_dt))

    # 顺带看一眼计划任务本身还活着吗 —— 触发器过期是「静默失效」的头号原因：
    # 用 -Once 建的一次性触发器，重复窗口一过就再也不触发了。
    log("")
    log("最后一次运行 : %s（%.0f 分钟前）" % (last["start"], age_min))
    st = load_state(venue, args.state_dir_resolved)
    log("当前快照     : 空余 %d 条，更新于 %s"
        % (len(st.get("available") or []), st.get("updated_at") or "（无）"))

    # 每日摘要状态 —— 顺手一起报，省得用户再去翻 state 目录
    d_target = digest_target_min(venue)
    if digest_enabled(venue) and d_target is not None:
        at = min_to_hhmm(d_target)
        if digest_sent_today(venue, args.state_dir_resolved):
            try:
                with open(digest_state_path(venue, args.state_dir_resolved),
                          "r", encoding="utf-8") as f:
                    sent_at = json.load(f).get("sent_at") or "?"
            except (OSError, ValueError):
                sent_at = "?"
            log("每日摘要     : 今天已发（%s，定在 %s）" % (sent_at, at))
        elif nm < d_target:
            log("每日摘要     : 今天还没到点（定在 %s）" % at)
        else:
            log("每日摘要     : ⚠️ 已过 %s 但今天还没发 —— 下次运行会补发" % at)
    log("")
    if problems:
        log("判定：❌ 可能有问题")
        for p in problems:
            log("   - %s" % p)
        log("")
        log("排查顺序：")
        log("   1. 看计划任务的下次运行时间是否为空 —— 空 = 触发器已过期，需要重新注册：")
        log("      (Get-ScheduledTaskInfo -TaskName 'VenueBookingMonitor').NextRunTime")
        log("   2. 看日志目录里最新那个 .log 的末尾，python 报错会有 traceback；")
        log("      若末尾停在 '^C' 或没有 end 行，就是被强制中断了")
        log("   3. 手动跑一次确认脚本本身还能用：")
        log("      python scripts/venue_monitor.py watch -v %s" % venue.get("id"))
        return 1
    log("判定：✅ 正常（调度在按时运行）")
    if not in_active_hours:
        log("   注：当前不在活跃时段（%s–23:00），没有新记录是正常的。"
            % (min_to_hhmm(quiet) if quiet is not None else "?"))
    return 0


def cmd_probe(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    """高频轮询，捕捉「无空余 -> 有空余」的翻转时刻，用来推断放号时间。"""
    state_dir = args.state_dir_resolved
    venue = pick_venues(cfg, args.venue)[0]
    dates = build_dates(venue, args.date)
    log("=" * 62)
    log("放号时间探测：%s" % venue.get("name"))
    log("目标：%s   间隔：%ds   时间窗：%s ~ %s" % (
        ", ".join(dates) if not venue.get("timetable_ids") else "扫描号源区间",
        args.interval, args.window_from, args.window_to))
    log("=" * 62)

    started = dt.datetime.now()
    first_seen: Dict[str, str] = {}
    timeline: List[Dict[str, Any]] = []
    rounds = 0
    try:
        while True:
            clock = dt.datetime.now()
            if args.window_from and clock.strftime("%H:%M") < args.window_from:
                time.sleep(min(30, args.interval))
                continue
            if args.window_to and clock.strftime("%H:%M") > args.window_to:
                log("已超出探测时间窗，结束。")
                break
            if args.max_rounds and rounds >= args.max_rounds:
                log("达到最大轮询次数 %d，结束。" % args.max_rounds)
                break
            rounds += 1
            slots, errors, meta = fetch_venue(venue, dates, args)
            slots = apply_filters(venue, slots)
            free = [s for s in slots if s["available"]]
            for s in free:
                k = slot_key(s)
                if k not in first_seen:
                    first_seen[k] = clock.strftime("%Y-%m-%d %H:%M:%S")
                    log("[%s] ⚡ 首次出现空余: %s" % (first_seen[k], k))
            timeline.append({"at": clock.strftime("%Y-%m-%d %H:%M:%S"),
                             "free_count": len(free), "total": len(slots),
                             "hit": meta.get("hit"), "errors": errors})
            log("[%s] 第%02d轮 空余 %d/%d" % (clock.strftime("%H:%M:%S"), rounds, len(free), len(slots)))
            if first_seen and args.stop_on_first:
                log("已捕获首个空余，按 --stop-on-first 结束。")
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("手动中断。")

    out = {
        "venue": venue.get("id"),
        "dates": dates,
        "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "rounds": rounds,
        "first_seen": first_seen,
        "timeline": timeline,
    }
    os.makedirs(state_dir, exist_ok=True)
    p = os.path.join(state_dir, "probe-%s-%s.json" % (venue["id"], started.strftime("%Y%m%d-%H%M%S")))
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    log("")
    log("=" * 62)
    if first_seen:
        earliest = min(first_seen.values())
        log("推断放号时间：**%s**（该时刻首次观测到空余）" % earliest)
        log("样本提示：若多日探测都落在同一时刻附近，即可确认放号规则。")
    else:
        log("本次未观测到空余。可能原因：① 该日仍不在放号窗口内；② 确已约满；")
        log("          ③ 时间窗选错。建议扩大 --window-from/--window-to 或换个目标日期再试。")
    log("探测明细：%s" % p)
    log("=" * 62)
    return 0


def cmd_selftest(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    log("=" * 62)
    log("自检：用内置 mock 场馆跑通「拉取 -> 解析 -> 比对 -> 报告」")
    log("=" * 62)
    mock = {"id": "selftest", "name": "自检-演示场馆", "miniprogram": "(mock)",
            "request": {"mock": True, "mock_window_days": 7, "mock_release_hour": 0},
            "response": {"slots_path": "data.list", "time_field": "startTime",
                         "available_field": "status", "available_values": ["free"],
                         "court_field": "courtName", "price_field": "price"},
            "dates": {"offsets": [1, 3, 30], "date_format": "%Y-%m-%d"},
            "watch": {"time_windows": ["18:00-22:00"]}}
    state_dir = os.path.join(SKILL_ROOT, "state", "_selftest")
    ns = argparse.Namespace(no_proxy=True, date=None, out_dir=args.out_dir,
                            state_dir=state_dir, state_dir_resolved=state_dir,
                            venue=None, full_scan=False)
    if os.path.isdir(state_dir):
        for fn in os.listdir(state_dir):
            os.remove(os.path.join(state_dir, fn))

    log("\n--- 第 1 次（建立基线）---")
    fetch_and_report(mock, ns)
    log("\n--- 第 2 次（与基线比对，应报告「无新增」）---")
    fetch_and_report(mock, ns)
    log("\n--- 修改快照，模拟「新放出一批空位」---")
    sp = state_path(mock, ns.state_dir)
    with open(sp, "r", encoding="utf-8") as f:
        st = json.load(f)
    st["available"] = st["available"][:2]
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    added = fetch_and_report(mock, ns)
    log("")
    log("=" * 62)
    log("自检结果：%s（检测到 %d 条新增空余）" % ("✅ 通过" if added > 0 else "❌ 失败", added))
    log("=" * 62)
    return 0 if added > 0 else 1


def fetch_and_report(venue: Dict[str, Any], ns: argparse.Namespace) -> int:
    r = run_venue(venue, ns, ns.state_dir, announce=False)
    log("场次 %d 条 / 空余 %d 条 / 新增 %d 条" % (
        len(r["slots"]), sum(1 for s in r["slots"] if s["available"]), len(r["new_keys"])))
    if r["md"]:
        log("报告: %s" % r["md"])
    return len(r["new_keys"])


def cmd_release(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    """打印放号观测日志，并推断放号时刻。"""
    state_dir = args.state_dir_resolved
    for venue in pick_venues(cfg, args.venue):
        log("")
        log("=" * 62)
        log("放号观测日志：%s (%s)" % (venue.get("name"), venue.get("id")))
        log("=" * 62)
        p = release_log_path(venue, state_dir)
        if not os.path.exists(p):
            log("暂无记录。先跑 check 建立观测基线。")
            continue
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data:
            log("暂无记录。")
            continue
        log("%-14s %s" % ("目标日期", "首次观测到「已开放预订」"))
        log("-" * 62)
        for d in sorted(data):
            log("%-14s %s" % (d, data[d]))

        buckets: Dict[str, List[str]] = {}
        for d, ts in data.items():
            buckets.setdefault(ts.split(" ")[-1][:5], []).append(d)
        log("")
        log("-" * 62)
        log("时刻分布：")
        for hm in sorted(buckets):
            log("  %s  ←  %s" % (hm, ", ".join(sorted(buckets[hm]))))
        best_hm, best_days = max(buckets.items(), key=lambda kv: len(kv[1]))
        log("")
        if len(data) >= 3 and len(best_days) >= max(2, int(len(data) * 0.6)):
            log("推断放号时刻：**%s**（%d/%d 个日期命中，置信度较高）" % (
                best_hm, len(best_days), len(data)))
        else:
            log("推断放号时刻：**%s**（仅 %d/%d 命中，样本不足，继续观测）" % (
                best_hm, len(best_days), len(data)))
        log("")
        log("注意：精度受 check 调度间隔限制；若观测到多个不同时刻，")
        log("说明放号不按固定钟点，可能是「提前 N 天滚动放号」，需按差值规律判断。")
    return 0


def cmd_notify_test(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    """给配置里的 webhook 发一条测试消息，验证提醒通道是否真的通。"""
    venue = pick_venues(cfg, args.venue)[0]
    ncfg = venue.get("notify") or {}
    url = ncfg.get("webhook_url")
    log("=" * 62)
    log("推送通道测试：%s" % venue.get("name"))
    log("=" * 62)
    if not url:
        log("⚠️  场馆 %s 未配置 notify.webhook_url，无法测试。" % venue["id"])
        log("")
        log("把 notify.webhook_url 填进 references/venues.json，notify.type 选一种：")
        for k, desc in NOTIFY_KINDS:
            log("   %-12s %s" % (k, desc))
        log("")
        log("企业微信群机器人取地址：群设置 → 群机器人 → 添加 → 复制 Webhook 地址")
        log("（注意：必须是「企业内部群」，含外部联系人的群没有机器人入口）")
        log("Server酱 / PushPlus / Bark 可以只填密钥，引擎会自动补全 URL。")
        return 1

    # 顺带把「真实提醒长什么样」预告一下，用 2099 年的示例数据，
    # 一眼能看出是样例、不会被误当成真消息。文案与真实告警共用同一个构造器，
    # 以后改格式这里会自动跟着变。
    preview = build_alert_text(
        venue,
        ["2099-01-01|20:00-20:30|12", "2099-01-01|21:30-22:00|7"],
        ["2099-01-01"], None)

    text = ("【场馆预约监测】推送通道测试\n"
            "时间：%s\n"
            "\n"
            "✅ 收到这条 = 「发现空场 → 立刻提醒你」的通道已打通。\n"
            "\n"
            "———— 以下是真实提醒的样子（示例数据）————\n"
            "%s" % (now_str(), preview))
    kind = ncfg.get("type", "wecom")
    title = ncfg.get("title") or ("【%s】推送测试" % venue.get("name"))
    use_no_proxy = bool(ncfg.get("no_proxy", True)) or bool(args.no_proxy)
    try:
        final_url, headers, body = build_notify(kind, url, title, text, ncfg)
        log("渠道：%s（%s）" % (kind, dict(NOTIFY_KINDS).get(kind, "未知类型")))
        log("目标：%s" % mask_url(final_url))
        log("代理：%s" % ("绕过系统代理（直连）" if use_no_proxy else "走系统代理"))
        log("")
        http_fetch(final_url, "POST", headers, body, 15, use_no_proxy, ncfg.get("proxy"))
        log("✅ 测试消息已发送 —— 请检查对应 App / 群是否收到。")
        log("   没收到？① webhook 地址或密钥是否过期；② 机器人是否被移出群；")
        log("           ③ 若填了 notify.type=wecom 但用的是飞书地址，报文格式对不上（换 type）。")
        return 0
    except Exception as e:  # noqa: BLE001
        log("❌ 推送失败：%s" % e)
        log("   常见原因：webhook 地址/密钥过期；机器人已被移出群；")
        log("             公司网络必须走代理时，把 notify.no_proxy 设为 false。")
        return 1


# ---------------------------------------------------------------- 凭证更新

TOKEN_HEADER_HINTS = ("authorization", "auth", "token", "jwt")


def _clean_token_value(raw: Any) -> str:
    """把粘贴/抓包得到的凭证洗干净：去空白引号，剥掉 Bearer 之类前缀。"""
    v = str(raw or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    for pre in ("bearer ", "jwt ", "token "):
        if v.lower().startswith(pre):
            v = v[len(pre):].strip()
    return v


def _token_header_name(headers: Dict[str, Any],
                       prefer: Optional[str] = None) -> Optional[str]:
    """在 header 集合里找凭证字段名。优先配置里已在用的那个。"""
    if not headers:
        return None
    if prefer:
        for k in headers:
            if str(k).lower() == str(prefer).lower():
                return str(k)
    for k in headers:
        if any(s in str(k).lower() for s in TOKEN_HEADER_HINTS):
            return str(k)
    return None


def _token_from_headers(headers: Dict[str, Any],
                        prefer: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    name = _token_header_name(headers, prefer)
    if not name:
        return None, None
    return name, _clean_token_value(headers.get(name))


def _import_curl2venue():
    """延迟导入同目录的抓包解析工具（只有换凭证时才需要）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import curl2venue  # noqa: PLC0415
    return curl2venue


def _token_from_curl_text(text: str, prefer: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """从一段 cURL 命令里取凭证，返回 (header 名, 值, url)。"""
    c2v = _import_curl2venue()
    try:
        parsed = c2v.parse_curl(text)
    except Exception as e:  # noqa: BLE001
        die("解析 cURL 失败：%s" % e, 1)
    name, val = _token_from_headers(parsed.get("headers") or {}, prefer)
    return name, val, parsed.get("url")


def _token_from_chlz(path: str, prefer: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """从 .chlz / .chls 抓包文件里取凭证（遍历条目，取第一个带凭证的）。"""
    if not os.path.exists(path):
        die("抓包文件不存在：%s" % path, 1)
    c2v = _import_curl2venue()
    entries = c2v.load_chlz_entries(path)
    if not entries:
        die("从 %s 里没解析出任何请求条目。\n"
            "确认导出的是完整 .chlz（不是只导了响应）。" % path, 1)
    for e in entries:
        name, val = _token_from_headers(e.get("headers") or {}, prefer)
        if val:
            return name, val, e.get("url")
    die("抓包里没找到凭证字段（%s）。\n"
        "确认抓到了那个业务接口的请求 —— 就是带 JAuthorization 的那条。"
        % " / ".join(TOKEN_HEADER_HINTS), 1)


def _patch_config_token(config_path: str, header_name: str,
                        new_token: str) -> Tuple[Optional[str], Optional[str], bool]:
    """
    只替换目标 header 的值，其余字节原样保留。

    用文本级精确替换而不是 json.dump 重写：换行符、BOM、缩进、键顺序都不动，
    这样 diff 里只会看到凭证那一行变化，评审时一眼能看懂。
    """
    with open(config_path, "rb") as f:
        raw = f.read()
    bom = raw[:3] == b"\xef\xbb\xbf"
    text = raw.decode("utf-8-sig")
    pat = re.compile(r'("%s"\s*:\s*")([^"]*)(")' % re.escape(header_name))
    m = pat.search(text)
    if not m:
        return None, None, bom
    old = m.group(2)
    safe = new_token.replace("\\", "\\\\").replace('"', '\\"')
    new_text = pat.sub(lambda mm: mm.group(1) + safe + mm.group(3), text, count=1)
    return new_text, old, bom


def _verify_token(venue: Dict[str, Any], header_name: str, token: str,
                  args: argparse.Namespace) -> Tuple[str, str]:
    """
    用新凭证真打一次接口，返回 (status, 说明)。
    status 三态：
      verified   接口会拦匿名请求，且新凭证能过 —— 凭证确实有效
      anonymous  不带凭证也能成功 —— 接口不校验凭证，无法验证凭证本身
      failed     带新凭证仍失败 —— 凭证无效，调用方应回滚

    坑：有些接口压根不校验凭证（实测xx市体育馆的查询接口就是 —— 乱填、过期、
    甚至完全不传，都返回 code=200）。这时「能打通」证明不了凭证本身有效，
    必须靠「匿名探针」把这种情况识别出来，否则会给出虚假的"验证通过"。
    """
    req = venue.get("request") or {}
    url = req.get("url")
    if not url:
        return "failed", "配置里没有 request.url，无法验证"
    if "${TIMETABLE_ID}" in url:
        st = load_state(venue, args.state_dir_resolved)
        tid = st.get("max_valid_id") or (venue.get("timetable_ids") or {}).get("from")
        if not tid:
            return "failed", "找不到可用的 timetableId（先跑一次 check 建立号源基线）"
        url = url.replace("${TIMETABLE_ID}", str(tid))

    rcfg = venue.get("response") or {}
    code_path = rcfg.get("code_path", "code")
    ok_vals = rcfg.get("ok_code_values") or [200]

    def _call(with_token: bool) -> Any:
        headers = dict(req.get("headers") or {})
        if with_token:
            headers[header_name] = token
        else:
            headers.pop(header_name, None)
        return http_fetch(url, req.get("method", "GET"), headers, None,
                          int(req.get("timeout", 15)),
                          bool(req.get("no_proxy", True)), req.get("proxy"))

    # 探针：不带凭证能不能成功？
    try:
        anon_code = dig(_call(False), code_path)
        anonymous_ok = anon_code in ok_vals
    except Exception:  # noqa: BLE001
        anonymous_ok = False
        anon_code = None

    if anonymous_ok:
        return "anonymous", ("不带凭证请求该接口，返回 code=%s（正常值）—— "
                             "接口不校验凭证，因此无法验证这个凭证是否有效。" % anon_code)

    # 接口确实会拦匿名请求，那这次验证才有意义
    try:
        payload = _call(True)
    except Exception as e:  # noqa: BLE001
        return "failed", "请求异常：%s" % e
    code = dig(payload, code_path)
    if code in ok_vals:
        return "verified", "接口会拦匿名请求，而新凭证返回 code=%s —— 凭证确实可用" % code
    return "failed", "接口返回 code=%s（期望 %s）—— 凭证无效或已过期" % (code, ok_vals)


def cmd_set_token(cfg: Dict[str, Any], args: argparse.Namespace) -> int:
    """换接口凭证：取值 -> 写回配置 -> 实测验证 -> 失败自动回滚。"""
    config_path = args.config
    venue = pick_venues(cfg, args.venue)[0]
    req = venue.get("request") or {}
    prefer = _token_header_name(req.get("headers") or {})

    log("=" * 62)
    log("更新接口凭证：%s (%s)" % (venue.get("name"), venue.get("id")))
    log("=" * 62)

    # ---- 1) 取值
    name: Optional[str] = None
    val: Optional[str] = None
    curl_url: Optional[str] = None
    src = ""

    if args.token:
        name = prefer or "JAuthorization"
        val = _clean_token_value(args.token)
        src = "命令行 --token"
    elif args.from_curl or args.from_curl_file:
        text = args.from_curl or ""
        if args.from_curl_file:
            if not os.path.exists(args.from_curl_file):
                die("文件不存在：%s" % args.from_curl_file, 1)
            with open(args.from_curl_file, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        name, val, curl_url = _token_from_curl_text(text, prefer)
        src = "cURL 命令"
    elif args.from_chlz:
        name, val, curl_url = _token_from_chlz(args.from_chlz, prefer)
        src = "抓包文件 %s" % os.path.basename(args.from_chlz)
    else:
        die("要指定凭证来源，三选一：\n"
            "  --from-curl \"curl ...\"    从 Charles 右键 Copy cURL Request 粘过来（最省事）\n"
            "  --from-chlz 会话文件.chlz   从导出的抓包文件里自动提取\n"
            "  --token <值>                直接把凭证字符串粘进来", 1)

    if not val:
        die("没取到凭证值。\n"
            "若是从 cURL / 抓包取的，确认复制的是那个业务接口请求（带 JAuthorization 的那条），\n"
            "而不是小程序启动时的 SDK 上报请求。", 1)

    # 字段名一律以配置里在用的为准 —— 我们要更新的是配置文件里的那个字段。
    # 抓包里的字段名可能与配置不同（实测遇到过 Authorization vs JAuthorization），
    # 若照抓包的名字去改，会改到一个配置里根本不存在的字段上。
    if prefer and name and name != prefer:
        log("注：抓包里的字段名是 %s，配置里用的是 %s，按配置更新。" % (name, prefer))
    name = prefer or name

    # ---- 2) 先亮出新凭证的底细，让人看清换了什么
    log("来源   : %s" % src)
    log("字段   : %s" % name)
    log("新凭证 : %s…（%d 字符）" % (val[:24], len(val)))
    exp = jwt_exp(val)
    if exp:
        left = (exp - dt.datetime.now()).total_seconds() / 86400.0
        log("到期   : %s（还剩 %.1f 天）" % (exp.strftime("%Y-%m-%d %H:%M"), left))
        if left <= 0:
            log("         ⚠️  这个凭证已经过期了，换上去也用不了。")
        elif left < 3:
            log("         ⚠️  只剩不到 3 天，确认不是又粘了个旧的。")
    else:
        log("到期   : 解不出 exp（不是标准 JWT），只能靠下面的实测判断")
    if curl_url and req.get("url") and curl_url != req.get("url"):
        log("")
        log("⚠️  cURL 里的 URL 与配置不一致：")
        log("      配置: %s" % req.get("url"))
        log("      cURL: %s" % curl_url)
        log("    （本次只换凭证不改 URL；若接口真的变了，请手工改 request.url）")
    log("")

    if args.dry_run:
        log("--dry-run：只看不写，配置未改动。")
        return 0

    # ---- 3) 写回（精确替换，保留原格式）
    new_text, old, bom = _patch_config_token(config_path, name, val)
    if new_text is None:
        die("配置里找不到字段 %r，无法自动替换。\n"
            "请手工把凭证填进 request.headers 后重跑。" % name, 1)
    backup = config_path + ".bak"
    shutil.copy2(config_path, backup)
    with open(config_path, "wb") as f:
        f.write((b"\xef\xbb\xbf" if bom else b"") + new_text.encode("utf-8"))
    log("已写回配置：%s" % config_path)
    log("  旧值 %s…" % ((old or "")[:24]))
    log("  新值 %s…" % val[:24])
    log("  备份 %s" % backup)
    log("")

    # ---- 4) 实测验证（换凭证最常见的错就是「粘错了但以为对了」）
    if args.no_verify:
        log("--no-verify：跳过实测验证。建议随后手工跑一次 check 确认。")
        return 0
    log("正在用新凭证实测一次接口…")
    status, why = _verify_token(venue, name, val, args)
    if status == "verified":
        log("✅ 验证通过：%s" % why)
        log("   凭证已生效，监测可以继续跑了。")
        log("   建议再跑一次：venue_monitor.py check -v %s" % venue.get("id"))
        return 0
    if status == "anonymous":
        log("⚠️  无法验证这个凭证：%s" % why)
        log("   好消息是 —— 既然该接口不校验凭证，凭证写错也不会影响监测。")
        log("   已按你的要求写回配置（备份在 %s）。" % backup)
        return 0
    log("❌ 验证失败：%s" % why)
    log("   正在回滚配置…")
    shutil.copy2(backup, config_path)
    log("   已回滚到原配置（备份留在 %s，可人工核对）。" % backup)
    return 1


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description="场馆/场地预约小程序 空余场次监测引擎（只读，不自动下单）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["doctor", "check", "probe", "release", "notify-test",
                                    "set-token", "watch", "status", "selftest"],
                    help="子命令")
    ap.add_argument("--config", "-c",
                    default=os.path.join(SKILL_ROOT, "references", "venues.json"),
                    help="场馆配置文件 (JSON)")
    ap.add_argument("--venue", "-v", help="只处理某个场馆 id")
    ap.add_argument("--date", "-d", action="append", help="覆盖监测日期，可重复，如 -d 2026-09-20")
    ap.add_argument("--out-dir", "-o", default=None, help="报告输出目录")
    ap.add_argument("--state-dir", default=None, help="快照目录")
    ap.add_argument("--no-proxy", action="store_true", help="绕过系统代理（排查网络问题时用）")
    ap.add_argument("--no-notify", action="store_true",
                    help="只查不发：跳过所有推送（含每日摘要），但照常更新快照。"
                         "用于手动验证、或长时间停用后重建基线，避免推一条误导性的大消息")
    ap.add_argument("--full-scan", action="store_true",
                    help="号源扫描时忽略已记录的前沿，扫满 timetable_ids.from~to（首次或排查用）")
    # probe
    ap.add_argument("--interval", type=int, default=20, help="probe 轮询间隔秒")
    ap.add_argument("--window-from", default=None, help="probe 起始时间 HH:MM")
    ap.add_argument("--window-to", default=None, help="probe 结束时间 HH:MM")
    ap.add_argument("--max-rounds", type=int, default=0, help="probe 最大轮询次数（0=不限）")
    ap.add_argument("--stop-on-first", action="store_true", help="probe 捕获首个空余即退出")
    # set-token
    ap.add_argument("--token", default=None, help="set-token：直接给新凭证字符串")
    ap.add_argument("--from-curl", default=None, help="set-token：从一段 cURL 命令里取凭证")
    ap.add_argument("--from-curl-file", default=None, help="set-token：从存了 cURL 的文本文件里取")
    ap.add_argument("--from-chlz", default=None, help="set-token：从 .chlz 抓包文件里取凭证")
    ap.add_argument("--dry-run", action="store_true", help="set-token：只看不写盘")
    ap.add_argument("--no-verify", action="store_true", help="set-token：跳过实测验证")
    args = ap.parse_args()

    if args.cmd == "selftest":
        return cmd_selftest({}, args)

    cfg = load_config(args.config)
    args.state_dir_resolved = resolve_state_dir(cfg, args)
    if args.cmd == "doctor":
        return cmd_doctor(cfg, args)
    if args.cmd == "check":
        return cmd_check(cfg, args)
    if args.cmd == "probe":
        return cmd_probe(cfg, args)
    if args.cmd == "release":
        return cmd_release(cfg, args)
    if args.cmd == "notify-test":
        return cmd_notify_test(cfg, args)
    if args.cmd == "set-token":
        return cmd_set_token(cfg, args)
    if args.cmd == "watch":
        return cmd_watch(cfg, args)
    if args.cmd == "status":
        return cmd_status(cfg, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
