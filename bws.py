#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilibili BW 预约抢票脚本（优化版） - 精细状态码处理 + 可配置重试延迟
Python ≥3.8 仅依赖 requests
可选依赖：orjson（超快 JSON 解析）、psutil（CPU 亲和/系统信息）
"""
# 安装依赖: pip install requests orjson psutil

import sys, time, json, threading, requests, statistics, re, atexit, os
from datetime import datetime
from requests.adapters import HTTPAdapter
import importlib
import importlib.util

# Optional accel libs
def _fast_json_loads(data):
    return json.loads(data)

_spec = importlib.util.find_spec("orjson")
if _spec is not None:
    orjson = importlib.import_module("orjson")
    _fast_json_loads = orjson.loads

try:
    import psutil
except ImportError:
    psutil = None

# Windows 1 ms 定时器 & 进程优先级
if sys.platform == "win32":
    try:
        import ctypes
        _winmm = ctypes.WinDLL("winmm")
        if _winmm.timeBeginPeriod(1) == 0:
            atexit.register(lambda: _winmm.timeEndPeriod(1))
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000080)  # HIGH_PRIORITY_CLASS
    except Exception:
        pass

if psutil is not None:
    try:
        p = psutil.Process()
        cpus = p.cpu_affinity()
        if cpus and len(cpus) > 1:
            p.cpu_affinity([cpus[0]])
    except Exception:
        pass

_PERF_OFFSET_NS = time.perf_counter_ns() - time.time_ns()

# ── 全局停止标志（原子） ──
STOP_RESERVE = threading.Event()

# ── 0. 账号 Cookie ──
def _load_cookie() -> str:
    env_cookie = os.environ.get("BW_COOKIE", "").strip()
    if env_cookie:
        return env_cookie
    cookie_file = os.environ.get("BW_COOKIE_FILE", "cookie.txt")
    try:
        with open(cookie_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        print(f"❌ 读取 Cookie 文件失败: {exc}")
        return ""

RAW_COOKIE = _load_cookie()
TICKET_DAYS = [3]  # 默认只看12号

COOKIE_DICT = {kv.split("=", 1)[0].strip(): kv.split("=", 1)[1]
               for kv in RAW_COOKIE.split(";") if "=" in kv}
SESSDATA = COOKIE_DICT.get("SESSDATA")
BILI_JCT = COOKIE_DICT.get("bili_jct")
if not (SESSDATA and BILI_JCT):
    print("❌ Cookie 中缺少 SESSDATA / bili_jct，脚本无法工作")
    sys.exit(1)

# ── 1. 可调参数 ──
CFG = {
    "ahead_sec":     0.8,
    "threads":       32,
    "requests_per_thread": 2,
    "reserve_type": 0,
    "time_jitter_ms": 15,
    "preheat_rounds": 8,
    "dry_run":       False,
    "debug":         True,
    # 状态码重试延迟字典（单位：毫秒），可随时通过菜单修改
    "retry_delays": {
        "75637": 500,    # 尚未开放
        "412":   180000, # 风控
        "429":   500,    # 限流
        "76650": 100,    # 操作频繁
        "-702":  100,    # 请求频率太快
        "-1":    200,    # 网络错误
        # 未知状态码默认快速重试，未在此字典中的状态码将使用 200ms 延迟
        "default": 200
    }
}

DAY_MAP = {1: 20260710, 2: 20260711, 3: 20260712}

# ── 2. Session 初始化 ──
HEADERS = {
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/540.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/540.36"),
    "origin":  "https://www.bilibili.com",
    "referer": "https://www.bilibili.com/blackboard/era/bws2026-event.html",
    "accept": "application/json, text/plain, */*",
    "accept-encoding": "gzip, deflate",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8"
}

sess = requests.Session()
sess.headers.update(HEADERS)
pool_size = CFG["threads"] * CFG.get("requests_per_thread", 2) * 2
sess.mount("https://", HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, pool_block=True))
for k, v in COOKIE_DICT.items():
    sess.cookies.set(k, v, domain=".bilibili.com")

_httpx_cli = None
_spec_httpx = importlib.util.find_spec("httpx")
if _spec_httpx is not None:
    httpx = importlib.import_module("httpx")
    _httpx_cli = httpx.Client(http2=True, headers=HEADERS, timeout=2.0)

def _http_post(url: str, data: bytes, headers: dict[str, str]):
    if _httpx_cli is not None:
        return _httpx_cli.post(url, content=data, headers=headers)
    return sess.post(url, data=data, headers=headers, timeout=(1, 2))

def log(*msg):
    print(time.strftime("[%H:%M:%S]"), *msg, flush=True)

def dbg(*msg):
    if CFG.get("debug"):
        log("DEBUG:", *msg)

# ── 3. 服务器时间同步 ──
_TIME_SOURCES = [
    ("https://api.bilibili.com/x/report/click/now", "now"),
    ("https://api.bilibili.com/x/activity/bws/online/park/nav", "server_time")
]
_TIME_OFFSET = 0.0
_OFFSET_TS   = 0.0

def _calibrate_offset(samples: int = 20):
    global _TIME_OFFSET, _OFFSET_TS
    log("🔄 正在与 B 站服务器校时…")
    offsets = []
    last_err = ""
    for _ in range(samples):
        t0 = time.time()
        try:
            server = None
            for url, key in _TIME_SOURCES:
                r = sess.get(url, timeout=2)
                body_preview = r.text[:120].replace("\n", " ") if r.text else ""
                print("SRC", url.split("/x/")[-1][:25], "HTTP", r.status_code,
                      "CT", r.headers.get("content-type"), "BODY", body_preview)
                if not r.headers.get("content-type", "").startswith("application/json"):
                    continue
                try:
                    j = r.json()
                    if key in j:
                        server = j.get(key)
                    else:
                        server = j.get("data", {}).get(key)
                    if isinstance(server, (int, float)) and server > 1e12:
                        server /= 1000.0
                except Exception:
                    server = None
                if server:
                    break
            t1 = time.time()
            if server:
                offsets.append(server - (t0 + t1) / 2)
            else:
                last_err = "no server time"
        except Exception as e:
            last_err = str(e)
        time.sleep(0.3)

    if offsets:
        _TIME_OFFSET = statistics.mean(offsets)
        _OFFSET_TS   = time.time()
        log(f"⏱️  时差校准成功: {_TIME_OFFSET*1000:.1f} ms (样本数={len(offsets)})")
    else:
        log(f"⚠️  时差校准失败，未能获取服务器时间，最后一次错误: {last_err}")

def now_server() -> float:
    if time.time() - _OFFSET_TS > 300:
        threading.Thread(target=_calibrate_offset, daemon=True).start()
    return time.time() + _TIME_OFFSET

# 移除启动时强制校时，改为菜单手动触发

# ── 4. 场次相关 API ──
INFO_URL = "https://api.bilibili.com/x/activity/bws/online/park/reserve/info"
GOODS_URL = "https://api.bilibili.com/x/activity/bws/online/park/goods/list"

def fetch_info(reserve_type=0):
    dates = [str(DAY_MAP[d]) for d in TICKET_DAYS if d in DAY_MAP]
    date_str = ",".join(dates) if dates else "20260712"
    params = {
        "csrf": BILI_JCT,
        "reserve_date": date_str,
        "reserve_type": reserve_type,
        "year": "202601"
    }
    r = sess.get(INFO_URL, params=params, cookies=COOKIE_DICT, timeout=5)
    if CFG.get("debug"):
        dbg("请求URL:", r.url[:300])
        dbg("HTTP", r.status_code, "响应前200:", r.text[:200])
    try:
        resp = r.json()
    except Exception:
        raise RuntimeError(f"非JSON响应 HTTP={r.status_code} body={r.text[:200]}")
    if resp["code"] != 0:
        if resp["code"] == 75638:
            raise RuntimeError(
                "❌ 账号未绑定门票\n"
                "1. 请用浏览器打开 https://www.bilibili.com/blackboard/era/bws2026-event.html 确认已绑定门票\n"
                "2. 重新获取 Cookie 后重试\n"
                "3. 检查 TICKET_DAYS 与门票日期匹配"
            )
        raise RuntimeError(f"接口错误 code={resp['code']} msg={resp.get('message')}")
    if CFG.get("debug"):
        filename = "_bw_goods.json" if reserve_type == 1 else "_bw_info.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(resp, f, ensure_ascii=False, indent=2)
        dbg("JSON saved")
    return resp["data"]

def fetch_goods():
    try:
        return fetch_info(reserve_type=1)
    except Exception as e:
        dbg("fetch_goods error:", e)
        return None

def _norm_status(start_ts: int, remain: int, now: float) -> int:
    if now < start_ts:
        return 0
    return 2 if remain <= 0 else 1

def parse_goods(data) -> list:
    if not data:
        return []
    raw = data.get("reserve_list", {})
    if isinstance(raw, dict):
        items = []
        for date_key, v in raw.items():
            lst_v = v if isinstance(v, list) else [v]
            for itm in lst_v:
                if isinstance(itm, dict):
                    itm = itm.copy()
                    itm["_date"] = str(date_key)
                    items.append(itm)
        raw = items
    elif not isinstance(raw, list):
        raw = []
    ticket_map = {str(k): v.get("ticket", "") for k, v in data.get("user_ticket_info", {}).items()}
    now = int(now_server())
    lst = []
    for itm in raw:
        if not isinstance(itm, dict):
            continue
        start_ts = int(itm.get("reserve_begin_time") or itm.get("reserve_time") or 0)
        title_raw = (itm.get("title") or itm.get("act_title") or itm.get("sku_name") or "")
        loc = itm.get("reserve_location", "")
        title = f"{title_raw}｜{loc}" if loc else title_raw
        remain = int(itm.get("standard_stock", itm.get("surplus", 0)))
        next_open_ts = int(itm.get("next_reserve", {}).get("reserve_begin_time", 0))
        if next_open_ts > start_ts:
            start_ts = next_open_ts
        dt = datetime.fromtimestamp(start_ts) if start_ts else None
        start_str = f"{dt.month}月{dt.day}日 {dt.strftime('%H:%M:%S')}" if dt else "??:??:??"
        date_key = itm.get("_date") or str(itm.get("screen_date", ""))
        ticket_no = ticket_map.get(date_key, "")
        action_url = (itm.get("reserve_action_url") or itm.get("button_link") or
                      itm.get("url") or (DO_URL if ticket_no else RESV_URL))
        lst.append({
            "id": itm.get("reserve_id"),
            "title": f"[商品] {title}",
            "start": start_ts,
            "start_s": start_str,
            "remain": remain,
            "total": int(itm.get("standard_ticket_num", itm.get("total", 0))),
            "status": _norm_status(start_ts, remain, now),
            "next_open": next_open_ts,
            "url": action_url,
            "ticket": ticket_no,
            "is_goods": True
        })
    lst.sort(key=lambda x: x["start"])
    return lst

def parse_sessions(data) -> list:
    raw = data.get("reserve_list", [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    if isinstance(raw, dict):
        tmp = []
        for date_key, v in raw.items():
            lst_v = v if isinstance(v, list) else [v]
            for itm0 in lst_v:
                if isinstance(itm0, dict):
                    itm0 = itm0.copy()
                    itm0["_date"] = str(date_key)
                tmp.append(itm0)
        raw = tmp
    if not isinstance(raw, list):
        raw = []
    ticket_map = {str(k): v.get("ticket", "") for k, v in data.get("user_ticket_info", {}).items()}
    now = int(now_server())
    lst = []
    for itm in raw:
        if not isinstance(itm, dict):
            continue
        if CFG.get("debug"):
            rid_dbg = itm.get("reserve_id")
            url_candidates = {k: v for k, v in itm.items()
                               if isinstance(v, str) and ("reserve" in v and "http" in v)}
            if url_candidates:
                dbg("URL-CANDS", rid_dbg, url_candidates)
        start_ts = int(itm.get("reserve_begin_time") or itm.get("reserve_time") or 0)
        title_raw = (itm.get("title") or itm.get("act_title") or
                     itm.get("sku_name") or "")
        loc   = itm.get("reserve_location", "")
        title = f"{title_raw}｜{loc}" if loc else title_raw
        remain = int(itm.get("standard_stock", itm.get("surplus", 0)))
        next_open_ts = int(itm.get("next_reserve", {}).get("reserve_begin_time", 0))
        if next_open_ts > start_ts:
            start_ts = next_open_ts
        display_ts = start_ts
        dt = datetime.fromtimestamp(display_ts) if display_ts else None
        start_str = f"{dt.month}月{dt.day}日 {dt.strftime('%H:%M:%S')}" if dt else "??:??:??"
        date_key = itm.get("_date") or str(itm.get("screen_date", ""))
        ticket_no = ticket_map.get(date_key, "")
        action_url = (itm.get("reserve_action_url") or itm.get("button_link") or
                       itm.get("url") or (DO_URL if ticket_no else RESV_URL))
        lst.append({
            "id":       itm.get("reserve_id"),
            "title":    title,
            "start":    start_ts,
            "start_s":  start_str,
            "remain":   remain,
            "total":    int(itm.get("standard_ticket_num", itm.get("total", 0))),
            "status":   _norm_status(start_ts, remain, now),
            "next_open": int(itm.get("next_reserve", {}).get("reserve_begin_time", 0)),
            "url":      action_url,
            "ticket":   ticket_no,
            "is_goods": False
        })
    lst.sort(key=lambda x: x["start"])
    return lst

def group_by_start(sessions: list):
    mp = {}
    for s in sessions:
        mp.setdefault(s["start"], []).append(s)
    return [(k, mp[k]) for k in sorted(mp)]

def print_sessions(lst):
    if not lst:
        return
    mark_map = {0: "未开", 1: "未开", 2: "售完"}
    for idx, it in enumerate(lst, 1):
        mark = mark_map.get(it["status"], f"状态{it['status']}")
        tag = "🛒" if it.get("is_goods") else "🎫"
        log(f" {idx:02d}  {tag} id={it['id']}  {it['start_s']}  "
            f"余{it['remain']:>4}/{it['total']:<4}  {mark}  {it['title']}")
    print()

# ── 5. 预约接口 ──
RESV_URL = "https://api.bilibili.com/x/activity/bws/online/park/reserve/add"
DO_URL   = "https://api.bilibili.com/x/activity/bws/online/park/reserve/do"
_CODE_RE = re.compile(rb'"code":\s*(-?\d+)')
_SUCCESS_BYTES = b'"code":0'

def reserve_once(reserve_id: int, url_use: str | None = None, ticket: str = ""):
    if CFG["dry_run"]:
        return {"code": 0, "message": "dry-run"}
    import random
    ts = int(time.time() * 1000)
    nonce = random.randint(10000, 99999)
    if ticket:
        payload = (f"inter_reserve_id={reserve_id}&ticket_no={ticket}&csrf={BILI_JCT}&ts={ts}&_={nonce}").encode()
        if url_use is None:
            url_use = DO_URL
    else:
        payload = (f"csrf={BILI_JCT}&reserve_id={reserve_id}&reserve_type={CFG['reserve_type']}&ts={ts}&_={nonce}").encode()
        if url_use is None:
            url_use = RESV_URL
    try:
        _hdr = HEADERS.copy()
        _hdr["content-type"] = "application/x-www-form-urlencoded"
        _hdr["user-agent"] = _hdr["user-agent"].replace("125.0.0.0", f"125.0.{random.randint(0,9)}.{random.randint(0,99)}")
        dbg("POST", url_use)
        resp = _http_post(url_use, payload, _hdr)
        if resp.status_code == 404 and not (ticket and url_use.endswith("/reserve/do")):
            base = url_use.rsplit("/reserve", 1)[0]
            alt_paths = [
                "/reserve/apply", "/reserve/v2/add", "/reserve/v3/add",
                "/v2/reserve/add", "/v3/reserve/add", "/ticket/apply",
                "/ticket/reserve/add", "/reserve/add"
            ]
            for ap in alt_paths:
                alt_url = base + ap
                try:
                    dbg("probe", alt_url)
                    resp = _http_post(alt_url, payload, _hdr)
                    if resp.status_code != 404:
                        dbg("hit", alt_url, resp.status_code)
                        break
                except Exception as _e:
                    dbg("probe exc", alt_url, _e)
            content = resp.content
        else:
            content = resp.content
        if not resp.headers.get("content-type", "").startswith("application/json"):
            dbg("HTTP", resp.status_code, "NON-JSON", content[:120])
            return {"code": resp.status_code, "message": "non-json"}
        if _SUCCESS_BYTES in content:
            return {"code": 0, "message": ""}
        m = _CODE_RE.search(content)
        if m:
            return {"code": int(m.group(1)), "message": ""}
        return _fast_json_loads(content)
    except Exception as e:
        dbg("EXC", e)
        return {"code": -1, "message": str(e)}

# ── 6. 抢票核心 ──
def wait_until(server_ts: int):
    while True:
        delta = server_ts - now_server()
        if delta <= 0:
            break
        if delta > 60:
            log(f"⌛ 距目标 {int(delta) // 60}m{int(delta) % 60:02d}s")
            time.sleep(60)
        else:
            time.sleep(5 if delta > 10 else max(0.5, delta / 2))

def gun_worker(reserve_id: int, fire_ts_server: float, action_url: str, ticket: str = "", thread_id: int = 0):
    import random
    requests_count = CFG.get("requests_per_thread", 2)
    jitter_ms = CFG.get("time_jitter_ms", 15)
    retry_delays = CFG.get("retry_delays", {})
    default_delay = retry_delays.get("default", 200)

    for req_idx in range(requests_count):
        if STOP_RESERVE.is_set():
            dbg(f"线程{thread_id} 收到全局停止信号，退出")
            return

        # 纳秒忙等
        jitter_sec = random.uniform(-jitter_ms, jitter_ms) / 1000.0
        fire_time = fire_ts_server + jitter_sec + (req_idx * 0.05)
        fire_local = fire_time - _TIME_OFFSET
        early = fire_local - time.time()
        if early > 0.25:
            time.sleep(early - 0.20)
        target_ns = int(fire_time * 1e9 + _PERF_OFFSET_NS)
        while time.perf_counter_ns() < target_ns:
            pass

        ret = reserve_once(reserve_id, action_url, ticket)
        code = ret.get("code")
        msg = ret.get("message", "")

        # 成功
        if code == 0:
            log(f"\033[92m🔫 {reserve_id} 成功 [线程{thread_id} 请求{req_idx}]\033[0m")
            return

        # 根据状态码获取等待时间
        delay_ms = retry_delays.get(str(code), default_delay)
        delay_sec = delay_ms / 1000.0

        # 细分处理
        if code == 75637:
            log(f"[75637] 尚未开放，线程{thread_id} 等待 {delay_ms}ms 后重试")
        elif code == 75574:
            log(f"\033[91m[75574] 预约已被抢空！线程{thread_id} 退出\033[0m")
            STOP_RESERVE.set()
            return
        elif code == 76674:
            log(f"\033[91m[76674] 预约已达上限！线程{thread_id} 退出\033[0m")
            STOP_RESERVE.set()
            return
        elif code == 412:
            log(f"\033[91m[412] 风控！线程{thread_id} 等待 {delay_ms}ms 后重试\033[0m")
        elif code == 429:
            log(f"[429] 限流，线程{thread_id} 等待 {delay_ms}ms")
        elif code == 76650:
            log(f"[76650] 操作频繁，线程{thread_id} 等待 {delay_ms}ms")
        elif code == -702:
            log(f"[-702] 请求频率太快，线程{thread_id} 等待 {delay_ms}ms")
        elif code == -1:
            log(f"[-1] 网络错误，线程{thread_id} 等待 {delay_ms}ms")
        else:
            log(f"\033[91m❌ 未知状态码 {code} msg={msg} 线程{thread_id} 等待 {delay_ms}ms\033[0m")

        time.sleep(delay_sec)
        # 继续循环尝试下一个请求

    log(f"\033[91m❌ {reserve_id} 失败 [线程{thread_id}] 已发送{requests_count}次请求\033[0m")

def _preheat_connection(url: str = RESV_URL, rounds=None):
    try:
        payload = f"csrf={BILI_JCT}&reserve_id=0&reserve_type={CFG['reserve_type']}".encode()
        if rounds is None:
            rounds = min(CFG.get("preheat_rounds", 8), CFG["threads"])
        for _ in range(rounds):
            _http_post(url, payload, {"content-type": "application/x-www-form-urlencoded"})
    except Exception:
        pass

def preheat_ids(id_list, url_map, rounds=None):
    if rounds is None:
        rounds = min(CFG.get("preheat_rounds", 8), CFG["threads"])
    for rid in id_list:
        url = url_map.get(rid, RESV_URL)
        try:
            payload = f"csrf={BILI_JCT}&reserve_id={rid}&reserve_type={CFG['reserve_type']}".encode()
            for _ in range(rounds):
                _http_post(url, payload, {"content-type": "application/x-www-form-urlencoded"})
        except Exception:
            pass

def fire_one(ses: dict):
    if ses["remain"] <= 0 and ses["next_open"] > ses["start"]:
        if ses["next_open"] > now_server():
            fmt = datetime.fromtimestamp(ses["next_open"]).strftime("%H:%M:%S")
            log(f"⏳ 库存未上架，等待 next_open {fmt}")
            wait_until(ses["next_open"])
    fire_at_server = ses["start"] - CFG["ahead_sec"]
    if fire_at_server < now_server():
        fire_at_server = now_server() + 0.05
    last_sec = -1
    while True:
        delta = fire_at_server - now_server()
        if delta <= 8:
            print("\r", end="", flush=True)
            break
        sec = int(delta)
        if sec != last_sec:
            print(f"\r⌛ 距开抢 {sec:>4d}s", end="", flush=True)
            last_sec = sec
        time.sleep(1)
    _preheat_connection(ses["url"])
    log(f"▶️  {ses['id']} {ses['title']}  {ses['start_s']}  即将开枪(提前 {CFG['ahead_sec']}s)  URL={ses['url']}")
    ths = [threading.Thread(target=gun_worker,
                            args=(ses["id"], fire_at_server, ses["url"], ses["ticket"], i))
           for i in range(CFG["threads"])]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

def fire_group(sess_list: list):
    STOP_RESERVE.clear()  # 重置全局停止标志
    for s in sess_list:
        if s["remain"] <= 0 and s["next_open"] > s["start"]:
            if s["next_open"] > now_server():
                fmt = datetime.fromtimestamp(s["next_open"]).strftime("%H:%M:%S")
                log(f"⏳ id={s['id']} 库存未上架，等待 next_open {fmt}")
                wait_until(s["next_open"])
    fire_at_server = sess_list[0]["start"] - CFG["ahead_sec"]
    if fire_at_server < now_server():
        fire_at_server = now_server() + 0.05
    last_sec = None
    while True:
        delta_f = fire_at_server - now_server()
        sec_left = int(delta_f + 0.999)
        if sec_left <= 5:
            sys.stdout.write("\r" + " " * 120 + "\r")
            sys.stdout.flush()
            break
        if sec_left != last_sec:
            line_lst = [f"[{s['id']}] ⌛ {sec_left:>4d}s" for s in sess_list]
            out = "   ".join(line_lst)[:120].ljust(120)
            sys.stdout.write("\r" + out)
            sys.stdout.flush()
            last_sec = sec_left
        time.sleep(0.2)
    _preheat_connection(sess_list[0]["url"])
    for s in sess_list:
        log(f"▶️  {s['id']} {s['title']}  {s['start_s']}  "
            f"即将开枪(提前 {CFG['ahead_sec']}s)  URL={s['url']}")
    ths = []
    thread_id = 0
    for s in sess_list:
        for i in range(CFG["threads"]):
            ths.append(threading.Thread(target=gun_worker,
                                        args=(s["id"], fire_at_server, s["url"], s["ticket"], thread_id)))
            thread_id += 1
    for t in ths:
        t.start()
    for t in ths:
        t.join()

# ── 7. 菜单/业务函数 ──
def check_cookie():
    nav_api = "https://api.bilibili.com/x/web-interface/nav"
    j = sess.get(nav_api, timeout=5).json()
    ok = j.get("code") == 0
    uname = j.get("data", {}).get("uname", "--")
    log(f"Cookie 检测: {'✅有效' if ok else '❌失效'}  uname={uname}")

def show_today():
    day_names = {1: "7月10日", 2: "7月11日", 3: "7月12日"}
    selected_days = [day_names.get(d, f"Day{d}") for d in TICKET_DAYS]
    log(f"📅 查询日期: {', '.join(selected_days)}")
    lst = parse_sessions(fetch_info(reserve_type=0))
    goods_data = fetch_goods()
    goods_lst = parse_goods(goods_data) if goods_data else []
    if lst:
        log("\n📅 活动场次：")
        print_sessions(lst)
    if goods_lst:
        log("\n🛒 商品场次：")
        print_sessions(goods_lst)
    if not lst and not goods_lst:
        log("⚠️  没有找到任何场次或商品")
        log("💡 提示：检查 TICKET_DAYS 配置，确保包含有票的日期")

def grab_flow():
    ids_in = input("输入要抢的 id（逗号分隔）、auto 自动挑选、或关键词（如 5070/RTX）: ").strip()
    sesses = parse_sessions(fetch_info(reserve_type=0))
    goods_data = fetch_goods()
    goods_lst = parse_goods(goods_data) if goods_data else []
    all_items = sesses + goods_lst
    if ids_in.lower() == "auto":
        now = now_server()
        targets = [s for s in all_items
                   if s["status"] == 0 and s["start"] > now]
    elif ids_in.replace(",", "").replace(" ", "").isdigit():
        wanted = {int(x) for x in ids_in.replace(" ", "").split(",") if x.strip().isdigit()}
        targets = [s for s in all_items if s["id"] in wanted]
    else:
        keyword = ids_in.lower()
        targets = [s for s in all_items if keyword in s["title"].lower()]
        if targets:
            log(f"🔍 找到 {len(targets)} 个匹配 '{ids_in}' 的项目：")
            for s in targets:
                tag = "🛒" if s.get("is_goods") else "🎫"
                log(f"   {tag} {s['id']} - {s['title']} ({s['start_s']})")
            confirm = input("确认抢这些项目？(y/n): ").strip().lower()
            if confirm != "y":
                log("已取消")
                return
    if not targets:
        log("⚠️  没有符合条件的项目")
        return
    groups = group_by_start(targets)
    id_to_url = {s["id"]: s["url"] for _ts, g in groups for s in g}
    preheat_ids(list(id_to_url.keys()), id_to_url)
    for start_ts, ses_lst in groups:
        fire_group(ses_lst)
    log("🚩 抢票流程结束")

def set_params():
    try:
        global TICKET_DAYS
        log(f"当前购票日期: {TICKET_DAYS} (1=10号, 2=11号, 3=12号)")
        days_in = input("修改购票日期（如 1,2,3 或直接回车跳过）: ").strip()
        if days_in:
            TICKET_DAYS = [int(x) for x in days_in.split(",") if x.strip().isdigit() and 1 <= int(x) <= 3]
            log(f"购票日期已更新: {TICKET_DAYS}")
        a = float(input(f"提前秒[{CFG['ahead_sec']}]: ") or CFG['ahead_sec'])
        th = int(input(f"并发线程[{CFG['threads']}]: ") or CFG['threads'])
        rpt = int(input(f"每线程请求数[{CFG.get('requests_per_thread', 2)}]: ") or CFG.get('requests_per_thread', 2))
        jit = int(input(f"时间抖动ms[{CFG.get('time_jitter_ms', 15)}]: ") or CFG.get('time_jitter_ms', 15))
        CFG.update(ahead_sec=a, threads=th, requests_per_thread=rpt, time_jitter_ms=jit)
        # 修改重试延迟
        print("\n当前重试延迟(ms):")
        for k, v in CFG.get("retry_delays", {}).items():
            print(f"  {k}: {v}")
        mod = input("是否修改重试延迟？(y/n): ").strip().lower()
        if mod == "y":
            for key in CFG["retry_delays"]:
                if key == "default": continue
                val = input(f"  {key} 延迟(ms)[{CFG['retry_delays'][key]}]: ").strip()
                if val:
                    CFG["retry_delays"][key] = int(val)
            val = input(f"  默认延迟(ms)[{CFG['retry_delays'].get('default', 200)}]: ").strip()
            if val:
                CFG["retry_delays"]["default"] = int(val)
        log("参数已更新:", CFG)
    except Exception as e:
        log("输入有误:", e)

# ── 8. 压测等 ──
def pressure_test():
    levels = [8, 16, 32, 48, 64]
    log("🧪 开始压测… (仅本地统计延迟，不会实际预约)")
    for th in levels:
        lat = []
        http_stats = []
        biz_codes = []
        def _w():
            payload = (f"csrf={BILI_JCT}&reserve_id=0&reserve_type={CFG['reserve_type']}").encode()
            t0 = time.perf_counter()
            try:
                resp = _http_post(RESV_URL, payload, {"content-type": "application/x-www-form-urlencoded"})
                http_stats.append(resp.status_code)
                m = _CODE_RE.search(resp.content)
                if m:
                    biz_codes.append(int(m.group(1)))
            except Exception:
                http_stats.append(-1)
            finally:
                lat.append(time.perf_counter() - t0)
        threads = [threading.Thread(target=_w) for _ in range(th)]
        t_begin = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        cost = time.perf_counter() - t_begin
        mean_ms = statistics.mean(lat) * 1000
        p90_ms  = sorted(lat)[int(0.9 * len(lat) - 1)] * 1000
        from collections import Counter
        http_cnt = Counter(http_stats)
        biz_cnt  = Counter(biz_codes)
        http_str = ", ".join(f"{k}:{v}" for k, v in sorted(http_cnt.items()))
        biz_str  = ", ".join(f"{k}:{v}" for k, v in sorted(biz_cnt.items())) or "--"
        log(f"线程 {th:>2d}  耗时 {cost:.2f}s  平均 {mean_ms:.1f} ms  P90 {p90_ms:.1f} ms  "
            f"HTTP({http_str})  code({biz_str})")
    log("🧪 压测结束")

# 颜色辅助
class _CLR:
    HEADER = '\033[95m' if sys.platform != 'win32' else ''
    BLUE = '\033[94m' if sys.platform != 'win32' else ''
    GREEN = '\033[92m' if sys.platform != 'win32' else ''
    YELLOW = '\033[93m' if sys.platform != 'win32' else ''
    RED = '\033[91m' if sys.platform != 'win32' else ''
    END = '\033[0m' if sys.platform != 'win32' else ''
    BOLD = '\033[1m' if sys.platform != 'win32' else ''

def _print_header(title: str):
    print(f"\n{_CLR.BOLD}{_CLR.BLUE}{'=' * 50}{_CLR.END}")
    print(f"{_CLR.BOLD}{_CLR.BLUE}  {title}{_CLR.END}")
    print(f"{_CLR.BOLD}{_CLR.BLUE}{'=' * 50}{_CLR.END}")

def _print_config():
    day_names = {1: "10", 2: "11", 3: "12"}
    days_str = ",".join(day_names.get(d, str(d)) for d in TICKET_DAYS)
    dry = "ON" if CFG["dry_run"] else "OFF"
    print(f"{_CLR.YELLOW}⚙️  当前配置{_CLR.END}")
    print(f"   日期: 7月{days_str}日 | 提前: {CFG['ahead_sec']}s | 线程: {CFG['threads']} | "
          f"每线程请求: {CFG.get('requests_per_thread',2)} | 抖动: ±{CFG.get('time_jitter_ms',15)}ms")
    print(f"   Dry-Run: {dry}")
    retry = CFG.get('retry_delays', {})
    print(f"   重试延迟(ms): 75637={retry.get('75637',500)}, 412={retry.get('412',180000)}, "
          f"429={retry.get('429',500)}, 76650={retry.get('76650',100)}, 默认={retry.get('default',200)}")

def _manual_calibrate():
    """手动触发校时"""
    log("🔄 手动触发服务器校时...")
    _calibrate_offset()

def _auto_pressure_tune():
    """
    自动压测并推荐最佳线程数。
    指标：平均延迟 < 200ms 且 P90 < 400ms 的前提下，选择最大线程数；
    若都不满足则选择平均延迟最小的配置。
    """
    _print_header("自动压测并推荐参数")
    log("将测试线程数 [8, 16, 24, 32, 40, 48, 56, 64]，每个线程发送 2 次请求...")
    results = []
    levels = [8, 16, 24, 32, 40, 48, 56, 64]

    for th in levels:
        lat = []
        http_stats = []
        biz_codes = []

        def _w():
            payload = (f"csrf={BILI_JCT}&reserve_id=0&reserve_type={CFG['reserve_type']}").encode()
            t0 = time.perf_counter()
            try:
                resp = _http_post(RESV_URL, payload, {"content-type": "application/x-www-form-urlencoded"})
                http_stats.append(resp.status_code)
                m = _CODE_RE.search(resp.content)
                if m:
                    biz_codes.append(int(m.group(1)))
            except Exception:
                http_stats.append(-1)
            finally:
                lat.append(time.perf_counter() - t0)

        threads = [threading.Thread(target=_w) for _ in range(th)]
        t_begin = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        cost = time.perf_counter() - t_begin

        mean_ms = statistics.mean(lat) * 1000
        p90_ms = sorted(lat)[int(0.9 * len(lat)) - 1] * 1000 if len(lat) >= 10 else mean_ms * 1.5
        results.append((th, mean_ms, p90_ms, cost, http_stats, biz_codes))
        log(f"线程 {th:>2d}  总耗时 {cost:.2f}s  平均 {mean_ms:.1f}ms  P90 {p90_ms:.1f}ms")

    # 分析推荐
    best_th = None
    best_mean = float('inf')
    for th, mean, p90, *_ in results:
        if mean < 200 and p90 < 400:
            best_th = th  # 满足条件的最大线程会自然被最后覆盖
    if best_th is None:
        # 选择平均延迟最小的
        best_th = min(results, key=lambda x: x[1])[0]
        log(f"{_CLR.YELLOW}⚠️ 未找到完全满足延迟阈值的配置，推荐平均延迟最小的线程数: {best_th}{_CLR.END}")
    else:
        log(f"{_CLR.GREEN}✅ 推荐最佳线程数: {best_th}（平均延迟<200ms 且 P90<400ms）{_CLR.END}")

    # 询问是否应用
    choice = input(f"是否将线程数设为 {best_th}，每线程请求数设为 2？(y/n): ").strip().lower()
    if choice == 'y':
        CFG['threads'] = best_th
        CFG['requests_per_thread'] = 2
        log(f"{_CLR.GREEN}参数已应用！{_CLR.END}")
    else:
        log("未应用，可手动设置。")

def main():
    # 不再自动校时
    while True:
        try:
            _print_header("BWS")
            _print_config()
            print(f" {_CLR.BOLD}1{_CLR.END}) 检查 Cookie 有效性")
            print(f" {_CLR.BOLD}2{_CLR.END}) 查看全部场次（活动+商品）")
            print(f" {_CLR.BOLD}3{_CLR.END}) 自动抢票（支持 id/auto/关键词）")
            print(f" {_CLR.BOLD}4{_CLR.END}) 手动设置参数")
            print(f" {_CLR.BOLD}5{_CLR.END}) 切换 Dry-Run（当前：{CFG['dry_run']}）")
            print(f" {_CLR.BOLD}6{_CLR.END}) 自动压测并推荐最优线程/并发")
            print(f" {_CLR.BOLD}7{_CLR.END}) 校准服务器时间")
            print(f" {_CLR.BOLD}0{_CLR.END}) 退出")
            print(f"{_CLR.BOLD}{_CLR.BLUE}{'=' * 50}{_CLR.END}")
            choice = input("请选择操作：").strip()

            if choice == "1":
                check_cookie()
            elif choice == "2":
                show_today()
            elif choice == "3":
                grab_flow()
            elif choice == "4":
                set_params()
            elif choice == "5":
                CFG["dry_run"] = not CFG["dry_run"]
                log(f"Dry-Run 已切换为 {CFG['dry_run']}")
            elif choice == "6":
                _auto_pressure_tune()
            elif choice == "7":
                _manual_calibrate()
            elif choice == "0":
                log("👋 退出程序，再见！")
                break
            else:
                print(f"{_CLR.RED}请输入 0-7 之间的选项{_CLR.END}")
        except KeyboardInterrupt:
            print(f"\n{_CLR.YELLOW}^C 中断，程序退出{_CLR.END}")
            break
        except Exception as e:
            log(f"{_CLR.RED}⚠️ 运行时异常: {e}{_CLR.END}")

if __name__ == "__main__":
    main()