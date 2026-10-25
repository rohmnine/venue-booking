#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
curl2venue.py — 把抓包得到的 cURL 命令转成 venues.json 的场馆配置块

用法
----
# 1) 只做转换（离线）
python curl2venue.py --curl-file my-curl.txt --id example-venue --name "xx市体育馆"

# 2) 转换 + 实打一次接口，自动推荐 slots_path / available_values
python curl2venue.py --curl-file my-curl.txt --id example-venue --send

# 3) 已经存了响应 JSON，只做路径推荐
python curl2venue.py --curl-file my-curl.txt --response-file resp.json

依赖：仅 Python 3 标准库。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

DATE_KEY_HINT = re.compile(r"(date|day|start|end|time|dt|ymd|riqi|rq)", re.I)
TIME_KEY_HINT = re.compile(r"(time|start|end|hour|begin|slot|period|shift)", re.I)
STATUS_KEY_HINT = re.compile(r"(status|state|free|avail|book|remain|left|surplus|enable|used|lock)", re.I)
SENSITIVE_HINT = re.compile(r"(authorization|token|cookie|openid|session|sign|secret|apikey|api-key)", re.I)

DATE_PATTERNS = [
    ("%Y-%m-%d", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("%Y/%m/%d", re.compile(r"^\d{4}/\d{2}/\d{2}$")),
    ("%Y%m%d", re.compile(r"^\d{8}$")),
    ("%Y-%m-%d %H:%M:%S", re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")),
]


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- cURL 解析

def parse_curl(text: str) -> Dict[str, Any]:
    text = text.replace("\\\r\n", " ").replace("\\\n", " ").strip()
    if text.lower().startswith("curl"):
        text = text[4:]
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        tokens = text.split()

    url: Optional[str] = None
    method: Optional[str] = None
    headers: Dict[str, str] = {}
    datas: List[str] = []
    cookies: Optional[str] = None
    urlencoded: List[str] = []

    i = 0
    while i < len(tokens):
        t = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if t in ("-X", "--request") and nxt:
            method = nxt.upper(); i += 2; continue
        if t in ("-H", "--header") and nxt:
            if ":" in nxt:
                k, v = nxt.split(":", 1)
                headers[k.strip()] = v.strip()
            i += 2; continue
        if t in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii") and nxt:
            datas.append(nxt); i += 2; continue
        if t == "--data-urlencode" and nxt:
            urlencoded.append(nxt); i += 2; continue
        if t in ("-b", "--cookie") and nxt:
            cookies = nxt; i += 2; continue
        if t in ("-A", "--user-agent") and nxt:
            headers["User-Agent"] = nxt; i += 2; continue
        if t in ("-e", "--referer") and nxt:
            headers["Referer"] = nxt; i += 2; continue
        if t == "--url" and nxt:
            url = nxt; i += 2; continue
        if t.startswith("-"):
            i += 1; continue
        if url is None:
            url = t
        i += 1

    if cookies:
        headers.setdefault("Cookie", cookies)
    if urlencoded:
        datas.extend(urlencoded)
    body_raw = "&".join(datas) if datas else None
    if body_raw and not method:
        method = "POST"
    method = method or "GET"

    return {"url": url or "", "method": method, "headers": headers, "body_raw": body_raw}


# ---------------------------------------------------------------- 日期占位

def detect_date(value: str, key: str) -> Optional[str]:
    """返回匹配到的日期格式，未匹配返回 None。"""
    for fmt, pat in DATE_PATTERNS:
        if not pat.match(value):
            continue
        if fmt == "%Y%m%d":
            # 8 位数字误判风险高：要求字段名带日期语义，且确实是合理日期
            if not DATE_KEY_HINT.search(key):
                continue
            try:
                d = dt.datetime.strptime(value, fmt).date()
            except ValueError:
                continue
            if not (2020 <= d.year <= 2100):
                continue
            return fmt
        try:
            dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt.endswith("%H:%M:%S") and not DATE_KEY_HINT.search(key):
            continue
        return fmt
    return None


def replace_dates(obj: Any, key: str = "", found: Optional[Dict[str, str]] = None
                  ) -> Any:
    """递归把日期字面量替换为 ${DATE}，并记录格式。"""
    if found is None:
        found = {}
    if isinstance(obj, dict):
        return {k: replace_dates(v, k, found) for k, v in obj.items()}
    if isinstance(obj, list):
        return [replace_dates(v, key, found) for v in obj]
    if isinstance(obj, str):
        fmt = detect_date(obj, key)
        if fmt:
            found["date_format"] = fmt
            return "${DATE}"
        return obj
    return obj


def substitute_in_url(url: str, found: Dict[str, str]) -> str:
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    new = []
    for k, v in pairs:
        fmt = detect_date(v, k)
        if fmt:
            found.setdefault("date_format", fmt)
            new.append((k, "${DATE}"))
        else:
            new.append((k, v))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(new)))


# ---------------------------------------------------------------- 结构推荐

def walk_lists(obj: Any, path: str = "") -> List[Tuple[str, List[Any]]]:
    out: List[Tuple[str, List[Any]]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(walk_lists(v, "%s.%s" % (path, k) if path else k))
    elif isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            out.append((path, obj))
        for idx, v in enumerate(obj[:3]):
            out.extend(walk_lists(v, "%s[%d]" % (path, idx)))
    return out


def suggest_paths(payload: Any, top: int = 5) -> List[Dict[str, Any]]:
    cands = []
    for path, items in walk_lists(payload):
        keys = sorted({k for it in items for k in it.keys()})
        score = 0
        if any(TIME_KEY_HINT.search(k) for k in keys):
            score += 5
        if any(STATUS_KEY_HINT.search(k) for k in keys):
            score += 3
        if len(items) >= 4:
            score += 2
        if re.search(r"(list|slots?|items|rows|records|data)", path, re.I):
            score += 2
        cands.append({"path": path, "count": len(items), "keys": keys, "score": score,
                      "sample": items[0]})
    cands.sort(key=lambda c: -c["score"])
    return cands[:top]


def suggest_available(payload: Any) -> List[Dict[str, Any]]:
    out = []
    for path, items in walk_lists(payload):
        keys = {k for it in items for k in it.keys()}
        for k in keys:
            if not STATUS_KEY_HINT.search(k):
                continue
            vals = {}
            for it in items:
                v = it.get(k)
                if isinstance(v, (str, int, float, bool)) or v is None:
                    vals[str(v)] = vals.get(str(v), 0) + 1
            if 1 < len(vals) <= 6:
                out.append({"field": k, "path": path, "values": vals})
    # 去重
    seen = set()
    uniq = []
    for o in out:
        kk = (o["field"], o["path"])
        if kk in seen:
            continue
        seen.add(kk)
        uniq.append(o)
    return uniq[:6]


# ---------------------------------------------------------------- 发送请求

def send(url: str, method: str, headers: Dict[str, str], body_raw: Optional[str],
         timeout: int, no_proxy: bool) -> Any:
    data = None
    if body_raw is not None and method.upper() not in ("GET", "HEAD"):
        data = body_raw.encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper())
    for k, v in headers.items():
        req.add_header(k, v)
    handlers = [urllib.request.ProxyHandler({})] if no_proxy else []
    opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw_text": raw[:3000]}


# ---------------------------------------------------------------- 嵌套结构识别

def detect_nested(items: List[Any]) -> Optional[Dict[str, Any]]:
    """
    识别「外层时段 → 内层场地」的两层结构。
    判据：这一层每个元素都带时间字段，且其中某个字段是「场地数组」，
    该数组的元素里带状态字段（book / status / free …）。
    """
    if not items or not isinstance(items[0], dict):
        return None
    outer_keys = sorted({k for it in items[:5] if isinstance(it, dict) for k in it.keys()})
    if not any(TIME_KEY_HINT.search(k) for k in outer_keys):
        return None
    for k, v in items[0].items():
        if not isinstance(v, list) or not v or not isinstance(v[0], dict):
            continue
        inner_keys = sorted({kk for it in items[:5] if isinstance(it, dict)
                             for kk in ((it.get(k) or [{}])[0] if it.get(k) else {}).keys()})
        if any(STATUS_KEY_HINT.search(kk) for kk in inner_keys):
            return {"children_field": k, "outer_keys": outer_keys, "inner_keys": inner_keys}
    return None


# ---------------------------------------------------------------- 输出配置块

ID_PARAM_RE = re.compile(
    r"([?&])(timetableId|timetable_id|scheduleId|periodId|arrangeId|sessionId)=(\d+)", re.I)


def rewrite_timetable_id(url: str) -> Tuple[str, Optional[int]]:
    """
    有些接口不按日期查、而是按「排期号」查（如 ?timetableId=1048），
    该 id 与日期绑定、只在该日期放号后才存在。
    这里把数字换成 ${TIMETABLE_ID} 占位，并返回抓包时观测到的 id。
    """
    m = ID_PARAM_RE.search(url or "")
    if not m:
        return url, None
    tid = int(m.group(3))
    new = url[:m.start(3)] + "${TIMETABLE_ID}" + url[m.end(3):]
    return new, tid


def build_venue_block(vid: str, name: str, mini: str, parsed: Dict[str, Any],
                      date_format: str, detected_keys: List[str]) -> Dict[str, Any]:
    headers = dict(parsed["headers"])
    body: Any = None
    if parsed["body_raw"]:
        try:
            body = json.loads(parsed["body_raw"])
        except json.JSONDecodeError:
            body = parsed["body_raw"]

    url, obs_tid = rewrite_timetable_id(parsed["url"])
    note = "由 curl2venue.py 自动生成。请核对 response 映射后再启用。检测到的日期参数：%s" % (
        ", ".join(detected_keys) or "无")

    # 结构推荐：优先按真实响应里最像场次列表的那一层来填
    payload = parsed.get("payload")
    resp: Dict[str, Any] = {
        "slots_path": "data.list",
        "time_field": "startTime",
        "available_field": "status",
        "available_values": ["0", "free", "可预约", "available"],
        "court_field": "courtName",
        "price_field": "price",
    }
    top_path = None
    if payload is not None:
        cands = suggest_paths(payload, top=1)
        if cands:
            top_path = cands[0]["path"]
            resp["slots_path"] = top_path
    nested = None
    nested_path = None
    if payload is not None:
        for p, items in walk_lists(payload):
            if "[" in p:
                continue
            nd = detect_nested(items)
            if nd:
                nested, nested_path = nd, p
                break
    if nested:
        ok = nested["outer_keys"]
        ik = nested["inner_keys"]
        date_key = next((k for k in ok if re.search(r"date|day", k, re.I)), None)
        end_key = next((k for k in ok if re.search(r"end|to$|finish", k, re.I)), None)
        idx_key = next((k for k in ik if re.search(r"^(index|no|num|court|id)", k, re.I)), None)
        resp["slots_path"] = nested_path
        resp["children_field"] = nested["children_field"]
        if date_key:
            resp["date_field"] = date_key
        if end_key:
            resp["time_end_field"] = end_key
        if idx_key:
            resp["child_index_field"] = idx_key
        resp["_note"] = ("检测到【嵌套结构】%s[].%s[]：外层时段、内层场地。"
                         "已自动填好 children_field，请核对 available_values 与"
                         " unavailable_price_values（若有占位价如 9999 务必填上）。" % (
                             nested_path, nested["children_field"]))

    block: Dict[str, Any] = {
        "id": vid,
        "name": name,
        "miniprogram": mini,
        "enabled": True,
        "note": note,
        "request": {
            "method": parsed["method"],
            "url": url,
            "headers": headers,
            "body": body,
            "timeout": 15,
        },
        "response": resp,
        "dates": {"offsets": [0, 1, 2, 3, 4, 5, 6, 7], "date_format": date_format},
        "watch": {"time_windows": []},
    }
    if obs_tid:
        # 排期号型接口：日期无法直接拼进 URL，改用扫描 id 区间来发现日期
        block["timetable_ids"] = {
            "mode": "scan",
            "from": max(1, obs_tid - 24),
            "to": obs_tid + 90,
            "window_before": 4,
            "window_after": 10,
            "_note": "抓包时观测到 timetableId=%d。该 id 与日期绑定、只在该日期放号后才存在；"
                     "对应日期需从响应的日期字段确认。" % obs_tid,
        }
        block["dates"] = {"offsets": [], "date_format": date_format}
        block["watch"]["skip_past_dates"] = True
        block["note"] += ("\n⚠️ 检测到「按排期号查询」的接口（timetableId=%d），"
                          "已自动改用 timetable_ids 扫描模式。" % obs_tid)
    return block


# ---------------------------------------------------------------- 会话文件解析（HAR / CHLZ）

def _decode_body(raw: bytes, content_encoding: Optional[str]) -> Optional[str]:
    """把会话文件里的原始 body 解成文本；仍是 TLS 密文时返回 None。"""
    if not raw:
        return None
    if raw[:1] == b"\x16":
        return None                      # TLS 记录 —— 说明这条没被解密
    enc = (content_encoding or "").lower()
    try:
        if enc == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            import zlib
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        elif enc == "br":
            try:
                import brotli  # type: ignore
                raw = brotli.decompress(raw)
            except Exception:
                return None
    except Exception:
        return None
    return raw.decode("utf-8", "replace")


def _to_json(text: Optional[str]) -> Optional[Any]:
    if not text:
        return None
    t = text.lstrip()
    if not t.startswith(("{", "[")):
        return None
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None


def _clean_headers(headers: Dict[str, str]) -> Dict[str, str]:
    for k in ("Content-Length", "Accept-Encoding", "Host", "Proxy-Connection"):
        headers.pop(k, None)
    return headers


def _norm(index: int, url: str, method: str, headers: Dict[str, str],
          body_raw: Optional[str], payload: Optional[Any],
          encrypted: bool = False, note: str = "") -> Dict[str, Any]:
    return {"index": index, "url": url, "method": (method or "GET").upper(),
            "headers": headers or {}, "body_raw": body_raw, "payload": payload,
            "encrypted": encrypted, "note": note}


def load_har_entries(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        har = json.load(f)
    out: List[Dict[str, Any]] = []
    for i, e in enumerate(((har.get("log") or {}).get("entries")) or []):
        req = e.get("request") or {}
        headers = {}
        for h in req.get("headers") or []:
            if h.get("name"):
                headers[h["name"]] = h.get("value", "")
        body_raw = None
        pd = req.get("postData") or {}
        if pd.get("text"):
            body_raw = pd["text"]
        elif pd.get("params"):
            body_raw = urllib.parse.urlencode(
                [(p.get("name"), p.get("value")) for p in pd["params"]])
        content = (e.get("response") or {}).get("content") or {}
        out.append(_norm(i, req.get("url", ""), req.get("method"),
                         _clean_headers(headers), body_raw,
                         _to_json(content.get("text"))))
    return out


def _read_part(z: Any, names: Any, i: int, kind: str, meta: Dict[str, Any]) -> bytes:
    """
    读 .chlz 里的请求/响应体。不同 Charles 版本归档命名不一样：
      · 旧版：<i>-req.dat / <i>-res.dat
      · 新版：<i>-res.json（meta.response.bodyFile 会直接写明文件名）
    优先信 meta 里的 bodyFile，再依次退回常见扩展名。
    """
    side = "request" if kind == "req" else "response"
    cands: List[str] = []
    bf = (meta.get(side) or {}).get("bodyFile")
    if bf:
        cands.append(str(bf))
    for ext in ("dat", "json", "txt", "bin", "body"):
        cands.append("%d-%s.%s" % (i, kind, ext))
    for name in cands:
        if name in names:
            try:
                return z.read(name)
            except Exception:
                continue
    return b""


def _url_from_meta(meta: Dict[str, Any], rq: Dict[str, Any]) -> str:
    """从 meta 拼出完整 URL；firstLine 缺失时退回 meta.path + meta.query。"""
    host, port = meta.get("host"), meta.get("port")
    first = ((rq.get("header") or {}).get("firstLine") or "")
    parts = first.split(" ")
    path_ = parts[1] if len(parts) > 1 else ""
    if not path_:
        path_ = str(meta.get("path") or "/")
        if meta.get("query"):
            path_ += "?" + str(meta["query"])
    netloc = host if port in (None, 80, 443) else "%s:%s" % (host, port)
    return "%s://%s%s" % (meta.get("scheme") or "https", netloc, path_)


def load_chlz_entries(path: str) -> List[Dict[str, Any]]:
    """Charles 会话（.chlz / .chls）：zip 内含 <i>-meta.json 与请求/响应体"""
    import zipfile
    out: List[Dict[str, Any]] = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        idxs = sorted({int(m.group(1)) for n in names
                       if (m := re.match(r"(\d+)-meta\.json$", n))})
        for i in idxs:
            meta = json.loads(z.read("%d-meta.json" % i).decode("utf-8"))
            req = _read_part(z, names, i, "req", meta)
            res = _read_part(z, names, i, "res", meta)
            rq = meta.get("request") or {}
            rs = meta.get("response") or {}
            note = str(meta.get("notes") or "")
            host, port = meta.get("host"), meta.get("port")

            if meta.get("tunnel") or req[:1] == b"\x16":
                out.append(_norm(i, "https://%s:%s" % (host, port), "CONNECT",
                                 {}, None, None, encrypted=True, note=note))
                continue

            url = _url_from_meta(meta, rq)

            headers = {}
            for h in ((rq.get("header") or {}).get("headers") or []):
                if h.get("name"):
                    headers[h["name"]] = h.get("value", "")
            out.append(_norm(i, url, meta.get("method"), _clean_headers(headers),
                             _decode_body(req, rq.get("contentEncoding")),
                             _to_json(_decode_body(res, rs.get("contentEncoding"))),
                             note=note))
    return out


def norm_score(e: Dict[str, Any]) -> int:
    if e.get("payload") is None:
        return -1
    cands = suggest_paths(e["payload"], top=1)
    if not cands:
        return -1
    s = cands[0]["score"]
    url = e.get("url") or ""
    if re.search(r"(slot|book|order|venue|site|reserve|field|chang|appoint)", url, re.I):
        s += 4
    if re.search(r"(list|query|search|get|detail)", url, re.I):
        s += 1
    return s


def pick_entry(entries: List[Dict[str, Any]], want: Optional[int],
               source: str = "会话") -> Optional[Dict[str, Any]]:
    for e in entries:
        e["score"] = norm_score(e)
    valid = [e for e in entries if e["score"] >= 0]
    valid.sort(key=lambda x: -x["score"])
    encrypted = [e for e in entries if e.get("encrypted")]

    log("%s条目排名（按「像不像场次列表接口」打分）：" % source)
    if not valid:
        log("  （没有任何条目能解析出 JSON 响应）")
    for e in valid[:10]:
        log("  #%-4d 分数 %-3d  %s" % (e["index"], e["score"], e["url"][:110]))

    if not valid:
        log("")
        if encrypted:
            hosts = sorted({e["url"].split("//")[-1].split("/")[0] for e in encrypted})
            log("[ERROR] %d 条记录是未解密的 TLS 隧道，没有任何可解析的明文响应。" % len(encrypted))
            log("")
            log("涉及主机：%s" % ", ".join(hosts[:10]))
            log("")
            std443 = [h for h in hosts if h.endswith(":443")]
            if std443:
                log("→ 连 %s 这类标准 443 站点也没解密。" % std443[0])
                log("  这说明是【SSL Proxying 总开关没开】或【规则列表为空】，不是端口问题。")
                log("  请打开 Proxy → SSL Proxying Settings，确认对话框最上面的")
                log("  [√] Enable SSL Proxying 已经打勾，且下方 Include 列表里有一条 * / *。")
            else:
                log("→ 记录里没有标准 443 站点可对照，按下面步骤逐项检查。")
            log("")
            log("检查清单：")
            log("  1. Proxy → SSL Proxying Settings")
            log("     · 最上面 [√] Enable SSL Proxying 必须打勾（最容易漏的就是这个）")
            log("     · 下方 Include 列表点 Add：Host 填 * ，Port 填 *")
            log("       ⚠️ Port 默认值是 443，不改则规则是 *:443，")
            log("          覆盖不到非标准端口（如 8443、8443 这类）")
            log("  2. 完全退出微信 PC 端再重开（旧 TLS 隧道会被复用）")
            log("  3. 重新操作小程序，重新导出会话")
            log("")
            log("30 秒自检（不用碰微信）：用 PC 浏览器打开任意 https 网站，")
            log("然后在 Charles 里看该记录 —— 若显示为 GET / HTTP/1.1 的明文请求，")
            log("说明 SSL Proxying 已生效；若仍是 CONNECT 隧道，说明开关还没打开。")
            log("")
            log("修好的标志：Charles 里该记录从 CONNECT 变成带路径的请求（如 POST /mini/...）")
        else:
            log("[ERROR] 会话里没有返回 JSON 的请求。确认导出前已选中目标请求。")
        return None

    if want is not None:
        for e in valid:
            if e["index"] == want:
                return e
        log("")
        log("[ERROR] --entry %s 不是有效条目（见上面的编号）。" % want)
        return None

    best = valid[0]
    log("")
    log("自动选中 #%d。如果不是它，用 --entry <编号> 指定。" % best["index"])
    return best


# ---------------------------------------------------------------- 会话诊断

# 小程序基础库 / 常见 CDN 的主机名特征。诊断时要先把它们排除，
# 剩下的才是「业务主机」—— 不写死任何具体域名，换个场馆也能给出正确结论。
SDK_HOST_HINT = re.compile(
    r"(servicewechat|weixin|wechat|qq\.com|qpic|qlogo|baidu|google|gstatic|"
    r"apple|icloud|microsoft|doubleclick|umeng|bugly|gtimg|myqcloud|aliyun|"
    r"amap|bdstatic|sm\.cn|mmstat)", re.I)


def _is_sdk_host(host: str) -> bool:
    return bool(SDK_HOST_HINT.search(host or ""))


def diagnose_chlz(path: str) -> None:
    """逐主机打印解密状态，用于排查抓包配置问题。"""
    import zipfile
    log("=" * 100)
    log("会话诊断：%s" % path)
    log("=" * 100)
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        idxs = sorted({int(m.group(1)) for n in names
                       if (m := re.match(r"(\d+)-meta\.json$", n))})
        rows = []
        for i in idxs:
            meta = json.loads(z.read("%d-meta.json" % i).decode("utf-8"))
            req = _read_part(z, names, i, "req", meta)
            res = _read_part(z, names, i, "res", meta)
            rq = meta.get("request") or {}
            rs = meta.get("response") or {}
            if meta.get("tunnel") or req[:1] == b"\x16":
                state = "未解密(隧道)"
            else:
                state = "已解密" if _decode_body(res, rs.get("contentEncoding")) is not None \
                    else "非隧道但无明文"
            rows.append({"i": i, "host": meta.get("host"), "port": meta.get("port"),
                         "method": meta.get("method"), "state": state,
                         "req": len(req), "res": len(res),
                         "note": str(meta.get("notes") or "")})

        log("%-4s %-34s %-6s %-8s %-14s %9s %9s" %
            ("#", "host", "port", "method", "状态", "req", "res"))
        log("-" * 100)
        for r in rows:
            log("%-4d %-34s %-6s %-8s %-14s %9d %9d" %
                (r["i"], r["host"], r["port"], r["method"], r["state"],
                 r["req"], r["res"]))

        hosts: Dict[str, List[str]] = {}
        for r in rows:
            hosts.setdefault("%s:%s" % (r["host"], r["port"]), []).append(r["state"])
        ok = sorted(h for h, v in hosts.items() if any(s == "已解密" for s in v))
        bad = sorted(h for h, v in hosts.items() if all(s == "未解密(隧道)" for s in v))

        # 业务主机 = 排除小程序基础库 / 常见 CDN 后剩下的那些
        biz = sorted(h for h in hosts if not _is_sdk_host(h.rsplit(":", 1)[0]))
        biz_ok = [h for h in biz if h in ok]
        biz_bad = [h for h in biz if h not in ok]

        log("")
        log("-" * 100)
        log("已解密主机（%d）：%s" % (len(ok), ", ".join(ok[:6]) or "无"))
        log("未解密主机（%d）：%s" % (len(bad), ", ".join(bad[:10]) or "无"))
        if biz:
            log("")
            log("其中「业务主机」（已排除小程序基础库/CDN）%d 个：" % len(biz))
            for h in biz:
                log("   %-42s %s" % (h, "✅ 已解密" if h in ok else "❌ 未解密"))
        log("")
        notes = sorted({r["note"] for r in rows if r["note"]})
        for n in notes:
            log("Notes 汇总: %s" % n[:110])
        log("")

        if not ok:
            log("【结论】所有主机都没解密 → SSL Proxying 总开关没开，或 Include 规则列表为空。")
            log("         → Proxy → SSL Proxying Settings：勾上最上面的 Enable SSL Proxying，")
            log("           并在 Include 里加一条 Host=* / Port=*。")
        elif biz_ok and not biz_bad:
            log("【结论】业务主机全部已解密 → 抓包配置正确，可以正常导出了。")
        elif biz_bad:
            log("【结论】有 %d 个业务主机没解密：%s" % (len(biz_bad), ", ".join(biz_bad[:5])))
            nonstd = [h for h in biz_bad if not h.endswith((":443", ":80"))]
            if nonstd:
                log("         → 这些主机用的不是标准端口：%s" % ", ".join(nonstd[:5]))
                log("           检查 SSL Proxying 的 Include 规则是否覆盖该端口（Port 填 *，")
                log("           默认值 443 会漏掉它们）。")
            log("         → 同时检查 Exclude 列表里有没有 *:* —— Exclude 优先级高于 Include，")
            log("           一条通配就会把全部 Include 抵消掉（症状：开关开了、规则也加了，仍全隧道）。")
        else:
            log("【结论】会话里没有明显的业务主机（可能只抓到了小程序基础库的流量）。")
            log("         → 确认导出会话前，已在小程序里真实操作过目标页面（翻到日期列表那一步）。")


# ---------------------------------------------------------------- Charles 配置检查

def default_charles_config() -> str:
    """在 Windows / macOS 上找 Charles 的 charles.config。"""
    cands: List[str] = []
    for var in ("APPDATA", "USERPROFILE", "HOME"):
        base = os.environ.get(var)
        if base:
            cands.append(os.path.join(base, "AppData", "Roaming", "Charles", "charles.config"))
            cands.append(os.path.join(base, "Charles", "charles.config"))
    home = os.path.expanduser("~")
    cands.append(os.path.join(home, "AppData", "Roaming", "Charles", "charles.config"))
    cands.append(os.path.join(home, "Library", "Application Support", "Charles",
                              "charles.config"))
    seen = []
    for c in cands:
        if c not in seen:
            seen.append(c)
    for c in seen:
        if os.path.exists(c):
            return c
    return seen[0] if seen else "charles.config"


def check_charles_config(path: str) -> int:
    """直接读 Charles 的 charles.config，检查 SSL Proxying 的 Include / Exclude 规则。"""
    import xml.etree.ElementTree as ET
    log("=" * 74)
    log("Charles SSL Proxying 配置检查")
    log("=" * 74)
    if not os.path.exists(path):
        log("找不到配置文件：%s" % path)
        log("（Charles 未运行过，或配置在自定义目录）")
        return 1
    try:
        tree = ET.parse(path)
    except ET.ParseError as e:
        log("配置文件解析失败：%s" % e)
        return 1
    root = tree.getroot()

    def read_locations(tag: str) -> List[str]:
        out = []
        for lm in root.iter("locationMatch"):
            pass
        node = root.find(".//%s" % tag)
        if node is None:
            return out
        for loc in node.iter("location"):
            h = (loc.findtext("host") or "").strip()
            p = (loc.findtext("port") or "").strip()
            out.append("%s:%s" % (h, p))
        return out

    inc = read_locations("sslLocations")
    exc = read_locations("sslExcludeLocations")
    transp = root.findtext(".//transparentProxy")

    log("transparentProxy : %s" % transp)
    log("")
    log("Include（要解密的）:")
    for x in inc or ["（空）"]:
        log("   + %s" % x)
    log("")
    log("Exclude（不解密的，优先级高于 Include）:")
    for x in exc or ["（空）"]:
        log("   - %s" % x)
    log("")

    broad = re.compile(r"^(\*|.*\*)\s*:\s*(\*|\d*)$")
    bad = [x for x in exc if broad.match(x.replace(" ", ""))]
    if bad:
        log("=" * 74)
        log("❌ 发现问题：Exclude 里有通配规则 %s" % ", ".join(bad))
        log("")
        log("   Charles 里 Exclude 的优先级高于 Include，")
        log("   一条 `*:*` 的 Exclude 会把 Include 里所有规则全部抵消 —— 什么都不解密。")
        log("")
        log("   修复：在 SSL Proxying Settings 的 Exclude 列表里，")
        log("         把 `*:*` 这一行的勾去掉（或选中它点 − 删除），然后 Done。")
        log("=" * 74)
        return 2

    if not inc:
        log("=" * 74)
        log("❌ 发现问题：Include 列表是空的 → 不会解密任何主机。")
        log("   修复：Add → Host 填 * ，Port 填 *")
        log("=" * 74)
        return 2

    log("=" * 74)
    log("✅ Include / Exclude 规则看起来正常。")
    log("   若仍抓不到明文，请确认：证书已装进「受信任的根证书颁发机构」，")
    log("   且改完设置后已完全退出并重启微信。")
    log("=" * 74)
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="抓包结果（.chlz / HAR / cURL）-> venues.json 配置块")
    ap.add_argument("--chlz", action="append",
                    help="Charles 会话文件（.chlz / .chls），可重复传多个")
    ap.add_argument("--har", help="Charles 导出的 .har 文件（推荐：一步导出，自动挑请求）")
    ap.add_argument("--check-charles-config", nargs="?", const="__default__",
                    metavar="PATH",
                    help="检查 Charles 的 SSL Proxying 规则（默认读 %%APPDATA%%\\Charles\\charles.config）")
    ap.add_argument("--diagnose", action="store_true",
                    help="只做逐主机解密状态诊断，不生成配置")
    ap.add_argument("--entry", type=int, help="指定条目编号（默认自动挑最像的）")
    ap.add_argument("--curl-file", help="保存了 cURL 命令的文本文件")
    ap.add_argument("--curl", help="直接传 cURL 字符串")
    ap.add_argument("--id", default="new-venue", help="场馆 id")
    ap.add_argument("--name", default="新场馆", help="场馆名称")
    ap.add_argument("--miniprogram", default="#小程序://...", help="小程序链接")
    ap.add_argument("--send", action="store_true", help="实打一次接口，推荐 slots_path / available_values")
    ap.add_argument("--response-file", help="已有的响应 JSON 文件，用于推荐路径")
    ap.add_argument("--no-proxy", action="store_true", help="绕过系统代理")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--out", help="把生成的配置块写入文件")
    args = ap.parse_args()

    parsed: Optional[Dict[str, Any]] = None
    payload: Optional[Any] = None

    if args.check_charles_config is not None:
        p = (default_charles_config()
             if args.check_charles_config == "__default__" else args.check_charles_config)
        return check_charles_config(p)

    if args.diagnose:
        targets = list(args.chlz or []) + ([args.har] if args.har else [])
        if not targets:
            log("请用 --chlz <文件> 指定要诊断的会话文件。")
            return 1
        for t in targets:
            if not os.path.exists(t):
                log("[ERROR] 找不到文件：%s" % t)
                return 1
            if t.lower().endswith((".chlz", ".chls")):
                diagnose_chlz(t)
            else:
                log("[提示] %s 不是 Charles 会话文件，诊断模式仅支持 .chlz/.chls。" % t)
        return 0

    if args.chlz or args.har:
        entries: List[Dict[str, Any]] = []
        for p in (args.chlz or []):
            if not os.path.exists(p):
                log("[ERROR] 找不到会话文件：%s" % p)
                return 1
            entries.extend(load_chlz_entries(p))
        if args.har:
            if not os.path.exists(args.har):
                log("[ERROR] 找不到 HAR 文件：%s" % args.har)
                return 1
            entries.extend(load_har_entries(args.har))
        log("=" * 62)
        log("会话解析：共 %d 条记录" % len(entries))
        log("=" * 62)
        picked = pick_entry(entries, args.entry)
        if picked is None:
            return 1
        parsed = {"url": picked["url"], "method": picked["method"],
                  "headers": picked["headers"], "body_raw": picked["body_raw"]}
        payload = picked["payload"]
        log("")
    else:
        if args.curl_file:
            with open(args.curl_file, "r", encoding="utf-8") as f:
                text = f.read()
        elif args.curl:
            text = args.curl
        else:
            log("请提供抓包结果，三选一：")
            log("  · --chlz session.chlz      Charles 会话文件（.chlz / .chls）")
            log("  · --har  session.har       Charles: 选中请求 → 右键 Export Session → .har")
            log("  · --curl-file my-curl.txt  Charles: 右键请求 → Copy cURL Request")
            return 1
        parsed = parse_curl(text)

    if not parsed["url"]:
        log("[ERROR] 没能解析出 URL，请确认导出/复制内容完整。")
        return 1

    log("=" * 62)
    log("解析结果")
    log("=" * 62)
    log("方法    : %s" % parsed["method"])
    log("地址    : %s" % parsed["url"])
    log("请求头  : %d 个" % len(parsed["headers"]))
    for k, v in parsed["headers"].items():
        show = v if not SENSITIVE_HINT.search(k) else (v[:8] + "…(已截断，勿外传)")
        log("          %-18s %s" % (k + ":", show))
    log("请求体  : %s" % (parsed["body_raw"][:300] if parsed["body_raw"] else "（无）"))

    # 日期占位
    found: Dict[str, str] = {}
    detected_keys: List[str] = []
    new_url = substitute_in_url(parsed["url"], found)
    if new_url != parsed["url"]:
        detected_keys.append("URL 查询参数")
        parsed["url"] = new_url

    if parsed["body_raw"]:
        try:
            body_obj = json.loads(parsed["body_raw"])
            new_body = replace_dates(body_obj, "", found)
            if new_body != body_obj:
                detected_keys.append("请求体字段")
            parsed["body_raw"] = json.dumps(new_body, ensure_ascii=False)
        except json.JSONDecodeError:
            pass

    date_format = found.get("date_format", "%Y-%m-%d")
    log("")
    log("日期占位: %s" % ("已把日期替换为 ${DATE}，格式 %s" % date_format if detected_keys
                        else "⚠️ 未识别到日期参数 —— 请手工把日期字段改成 ${DATE}"))

    # 响应分析（HAR 模式已自带响应，无需再打接口）
    if payload is None and args.response_file:
        with open(args.response_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
    elif payload is None and args.send:
        log("")
        log("正在实打接口…")
        # 发送时把 ${DATE} 换成真实日期（明天）
        real = (dt.date.today() + dt.timedelta(days=1)).strftime(date_format)
        send_url = parsed["url"].replace("${DATE}", real)
        send_body = (parsed["body_raw"] or "").replace("${DATE}", real) or None
        try:
            payload = send(send_url, parsed["method"], parsed["headers"], send_body,
                           args.timeout, args.no_proxy)
            log("✅ 请求成功")
        except urllib.error.HTTPError as e:
            log("⚠️  HTTP %s %s（凭证可能已过期，或日期格式不对）" % (e.code, e.reason))
        except Exception as e:  # noqa: BLE001
            log("⚠️  请求失败：%s" % e)
            log("    若报连接类错误，试试加 --no-proxy")

    if payload is not None:
        log("")
        log("=" * 62)
        log("响应结构推荐")
        log("=" * 62)
        cands = suggest_paths(payload)
        if not cands:
            log("没找到「对象数组」形态的场次列表。可能是：")
            log("  · 该日期还没开放预订（换个日期再试）")
            log("  · 列表在更深的层级，或字段名不规则")
            log("")
            log("响应顶层结构预览：")
            log(json.dumps(payload, ensure_ascii=False, indent=2)[:1500])
        for c in cands:
            log("")
            log("slots_path 候选: %-28s (共 %d 条)" % (c["path"], c["count"]))
            log("  字段: %s" % ", ".join(c["keys"]))
            log("  样例: %s" % json.dumps(c["sample"], ensure_ascii=False)[:220])
        for _p, _items in walk_lists(payload):
            if "[" in _p:
                continue
            _nd = detect_nested(_items)
            if _nd:
                log("")
                log("⚠️  检测到【嵌套结构】：%s[].%s[]" % (_p, _nd["children_field"]))
                log("    外层=时段（%s）" % ", ".join(_nd["outer_keys"][:7]))
                log("    内层=场地（%s）" % ", ".join(_nd["inner_keys"]))
                log("    → slots_path 填 %s，并加 \"children_field\": \"%s\"（下方配置块已自动填好）"
                    % (_p, _nd["children_field"]))
                break

        avail = suggest_available(payload)
        if avail:
            log("")
            log("-" * 62)
            log("available_field / available_values 候选：")
            for a in avail:
                log("  %s  （位于 %s）取值分布：%s" % (a["field"], a["path"], a["values"]))
            log("")
            log("  → 抓一次「有空场」的响应，对照上面取值，把表示「可预约」的值填进 available_values")

    parsed["payload"] = payload          # 供 build_venue_block 做结构推荐
    block = build_venue_block(args.id, args.name, args.miniprogram, parsed,
                              date_format, detected_keys)
    text_out = json.dumps(block, ensure_ascii=False, indent=2)
    log("")
    log("=" * 62)
    log("venue 配置块（粘进 references/venues.json 的 venues 数组）")
    log("=" * 62)
    log(text_out)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text_out)
        log("")
        log("已写入: %s" % args.out)
    log("")
    log("⚠️  该配置含身份凭证，请只保存在本地，不要提交到公开仓库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
