#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilibili BW 预约抢票脚本（极致高效版）
"""
import sys, time, json, threading, requests, statistics, re, atexit, os, random
from datetime import datetime
from requests.adapters import HTTPAdapter
import importlib, importlib.util

# ── 可选加速库 ──
_fast_json_loads = json.loads
_spec = importlib.util.find_spec("orjson")
if _spec is not None:
    orjson = importlib.import_module("orjson")
    _fast_json_loads = orjson.loads

try:
    import psutil
except ImportError:
    psutil = None

# ── Windows 性能优化 ──
if sys.platform == "win32":
    try:
        import ctypes
        _winmm = ctypes.WinDLL("winmm")
        if _winmm.timeBeginPeriod(1) == 0:
            atexit.register(lambda: _winmm.timeEndPeriod(1))
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000080)
    except Exception:
        pass

# F1 暂停支持（仅 Windows）
try:
    import msvcrt
except ImportError:
    msvcrt = None

if psutil is not None:
    try:
        p = psutil.Process()
        cpus = p.cpu_affinity()
        if cpus and len(cpus) > 1:
            p.cpu_affinity([cpus[0]])
    except Exception:
        pass

_PERF_OFFSET_NS = time.perf_counter_ns() - time.time_ns()

# ── 全局原子标志 ──
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
TICKET_DAYS = [3]                # 默认 12 号

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
    "debug":         False,
    "retry_delays": {
        "75637": 500,
        "412":   180000,
        "429":   500,
        "76650": 100,
        "-702":  100,
        "-1":    200,
        "default": 200
    }
}

DAY_MAP = {1: 20260710, 2: 20260711, 3: 20260712}

# ── 2. Session 初始化 ──
HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/540.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/540.36",
    "origin":  "https://www.bilibili.com",
    "referer": "https://www.bilibili.com/blackboard/era/bws2026-event.html",
    "accept": "application/json, text/plain, */*",
    "accept-encoding": "gzip, deflate",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8"
}

sess = requests.Session()
sess.headers.update(HEADERS)
pool_size = CFG["threads"] * CFG["requests_per_thread"] * 2
sess.mount("https://", HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, pool_block=True))
for k, v in COOKIE_DICT.items():
    sess.cookies.set(k, v, domain=".bilibili.com")

# 选择底层 HTTP 客户端（一次判定，避免热路径重复检查）
_httpx_cli = None
if importlib.util.find_spec("httpx") is not None:
    import httpx
    _httpx_cli = httpx.Client(http2=True, headers=HEADERS, timeout=2.0)

def _post_func(url, data, hdr):
    """直接调用，避免热路径间接调用开销"""
    if _httpx_cli is not None:
        return _httpx_cli.post(url, content=data, headers=hdr)
    return sess.post(url, data=data, headers=hdr, timeout=(1, 2))

# 日志输出函数
def log(*msg):
    print(time.strftime("[%H:%M:%S]"), *msg, flush=True)

# 调试函数：debug=False 时置为空函数，消除调用开销
if CFG["debug"]:
    def dbg(*msg):
        log("DEBUG:", *msg)
else:
    def dbg(*msg):
        pass

# ── 3. 服务器时间同步 ──
_TIME_SOURCES = [
    ("https://api.bilibili.com/x/report/click/now", "now"),
    ("https://api.bilibili.com/x/activity/bws/online/park/nav", "server_time")
]
_TIME_OFFSET = 0.0
_OFFSET_TS   = 0.0
_CALIBRATED  = False

def _calibrate_offset(samples: int = 20):
    global _TIME_OFFSET, _OFFSET_TS, _CALIBRATED
    log("🔄 正在与 B 站服务器校时…")
    offsets = []
    for _ in range(samples):
        t0 = time.time()
        server = None
        for url, key in _TIME_SOURCES:
            try:
                r = sess.get(url, timeout=2)
                if r.headers.get("content-type", "").startswith("application/json"):
                    j = r.json()
                    s = j.get(key) or j.get("data", {}).get(key)
                    if isinstance(s, (int, float)) and s > 1e12:
                        s /= 1000.0
                    if s:
                        server = s
                        break
            except Exception:
                continue
        t1 = time.time()
        if server:
            offsets.append(server - (t0 + t1) / 2)
        time.sleep(0.3)

    if offsets:
        _TIME_OFFSET = statistics.mean(offsets)
        _OFFSET_TS   = time.time()
        log(f"⏱️  时差校准成功: {_TIME_OFFSET*1000:.1f} ms")
        _CALIBRATED = True
    else:
        log("⚠️  时差校准失败")
        _CALIBRATED = True

def now_server() -> float:
    if _CALIBRATED and time.time() - _OFFSET_TS > 300:
        threading.Thread(target=_calibrate_offset, daemon=True).start()
    return time.time() + _TIME_OFFSET

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
    try:
        resp = r.json()
    except Exception:
        raise RuntimeError(f"非JSON响应 HTTP={r.status_code}")
    if resp["code"] != 0:
        if resp["code"] == 75638:
            raise RuntimeError("❌ 账号未绑定门票，请检查Cookie/门票绑定")
        raise RuntimeError(f"接口错误 code={resp['code']} msg={resp.get('message')}")
    return resp["data"]

def fetch_goods():
    try:
        return fetch_info(reserve_type=1)
    except Exception:
        return None

def _norm_status(start_ts, remain, now):
    if now < start_ts:
        return 0
    return 2 if remain <= 0 else 1

def parse_goods(data):
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
        action_url = itm.get("reserve_action_url") or itm.get("button_link") or itm.get("url") or (DO_URL if ticket_no else RESV_URL)
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

def parse_sessions(data):
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
        start_ts = int(itm.get("reserve_begin_time") or itm.get("reserve_time") or 0)
        title_raw = (itm.get("title") or itm.get("act_title") or itm.get("sku_name") or "")
        loc   = itm.get("reserve_location", "")
        title = f"{title_raw}｜{loc}" if loc else title_raw
        remain = int(itm.get("standard_stock", itm.get("surplus", 0)))
        next_open_ts = int(itm.get("next_reserve", {}).get("reserve_begin_time", 0))
        if next_open_ts > start_ts:
            start_ts = next_open_ts
        dt = datetime.fromtimestamp(start_ts) if start_ts else None
        start_str = f"{dt.month}月{dt.day}日 {dt.strftime('%H:%M:%S')}" if dt else "??:??:??"
        date_key = itm.get("_date") or str(itm.get("screen_date", ""))
        ticket_no = ticket_map.get(date_key, "")
        action_url = itm.get("reserve_action_url") or itm.get("button_link") or itm.get("url") or (DO_URL if ticket_no else RESV_URL)
        lst.append({
            "id":       itm.get("reserve_id"),
            "title":    title,
            "start":    start_ts,
            "start_s":  start_str,
            "remain":   remain,
            "total":    int(itm.get("standard_ticket_num", itm.get("total", 0))),
            "status":   _norm_status(start_ts, remain, now),
            "next_open": next_open_ts,
            "url":      action_url,
            "ticket":   ticket_no,
            "is_goods": False
        })
    lst.sort(key=lambda x: x["start"])
    return lst

def group_by_start(sessions):
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
        log(f" {idx:02d}  {tag} id={it['id']}  {it['start_s']}  余{it['remain']:>4}/{it['total']:<4}  {mark}  {it['title']}")
    print()

# ── 5. 预约接口 ──
RESV_URL = "https://api.bilibili.com/x/activity/bws/online/park/reserve/add"
DO_URL   = "https://api.bilibili.com/x/activity/bws/online/park/reserve/do"
_CODE_RE = re.compile(rb'"code":\s*(-?\d+)')
_SUCCESS_BYTES = b'"code":0'

# 预生成随机 UA 片段，减少热路径中 random.randint 与字符串拼接开销
_UA_POOL = [f"125.0.{random.randint(0,9)}.{random.randint(0,99)}" for _ in range(100)]

def reserve_once(reserve_id: int, url_use: str | None = None, ticket: str = ""):
    if CFG["dry_run"]:
        return {"code": 0, "message": "dry-run"}
    ts = int(time.time() * 1000)
    nonce = random.randint(10000, 99999)
    if ticket:
        payload = f"inter_reserve_id={reserve_id}&ticket_no={ticket}&csrf={BILI_JCT}&ts={ts}&_={nonce}".encode()
        if url_use is None:
            url_use = DO_URL
    else:
        payload = f"csrf={BILI_JCT}&reserve_id={reserve_id}&reserve_type={CFG['reserve_type']}&ts={ts}&_={nonce}".encode()
        if url_use is None:
            url_use = RESV_URL

    _hdr = HEADERS.copy()
    _hdr["content-type"] = "application/x-www-form-urlencoded"
    # 快速替换 UA 尾部
    _hdr["user-agent"] = _hdr["user-agent"].replace("125.0.0.0", random.choice(_UA_POOL))

    try:
        resp = _post_func(url_use, payload, _hdr)
        if resp.status_code == 404 and not (ticket and url_use.endswith("/reserve/do")):
            base = url_use.rsplit("/reserve", 1)[0]
            for ap in ["/reserve/apply", "/reserve/v2/add", "/reserve/v3/add",
                       "/v2/reserve/add", "/v3/reserve/add", "/ticket/apply",
                       "/ticket/reserve/add", "/reserve/add"]:
                alt_url = base + ap
                try:
                    resp = _post_func(alt_url, payload, _hdr)
                    if resp.status_code != 404:
                        break
                except Exception:
                    continue
        content = resp.content
        if not resp.headers.get("content-type", "").startswith("application/json"):
            return {"code": resp.status_code, "message": "non-json"}
        if _SUCCESS_BYTES in content:
            return {"code": 0, "message": ""}
        m = _CODE_RE.search(content)
        if m:
            return {"code": int(m.group(1)), "message": ""}
        return _fast_json_loads(content)
    except Exception as e:
        return {"code": -1, "message": str(e)}

# ── 6. 抢票核心（极致优化版） ──
def wait_until(server_ts):
    while True:
        delta = server_ts - now_server()
        if delta <= 0:
            break
        if delta > 60:
            log(f"⌛ 距目标 {int(delta)//60}m{int(delta)%60:02d}s")
            time.sleep(60)
        else:
            time.sleep(5 if delta > 10 else max(0.5, delta/2))

def gun_worker(reserve_id, fire_ts_server, action_url, ticket, thread_id):
    # 将所有外部变量缓存为局部变量，避免属性查询
    req_cnt = CFG["requests_per_thread"]
    jitter_ms = CFG["time_jitter_ms"]
    retry_delays = CFG["retry_delays"]
    default_delay = retry_delays["default"]
    time_offset = _TIME_OFFSET
    perf_offset = _PERF_OFFSET_NS
    stop_event = STOP_RESERVE

    for req_idx in range(req_cnt):
        if stop_event.is_set():
            return

        # 计算精准发射时间（纳秒忙等）
        jitter_sec = random.uniform(-jitter_ms, jitter_ms) / 1000.0
        fire_time = fire_ts_server + jitter_sec + (req_idx * 0.05)
        fire_local = fire_time - time_offset
        early = fire_local - time.time()
        if early > 0.25:
            time.sleep(early - 0.20)
        target_ns = int(fire_time * 1e9 + perf_offset)
        while time.perf_counter_ns() < target_ns:
            pass

        ret = reserve_once(reserve_id, action_url, ticket)
        code = ret["code"]
        msg = ret.get("message", "")

        if code == 0:
            log(f"\033[92m🔫 {reserve_id} 成功 [线程{thread_id} 请求{req_idx}]\033[0m")
            return

        delay_ms = retry_delays.get(str(code), default_delay)
        delay_sec = delay_ms / 1000.0

        # 细分状态处理
        if code == 75637:
            log(f"[75637] 尚未开放，线程{thread_id} 等待 {delay_ms}ms")
        elif code in (75574, 76674):
            log(f"\033[91m[{code}] 已抢空/达上限，线程{thread_id} 退出\033[0m")
            stop_event.set()
            return
        elif code == 412:
            log(f"\033[91m[412] 风控！线程{thread_id} 等待 {delay_ms}ms\033[0m")
        elif code == 429:
            log(f"[429] 限流，线程{thread_id} 等待 {delay_ms}ms")
        elif code == 76650:
            log(f"[76650] 操作频繁，线程{thread_id} 等待 {delay_ms}ms")
        elif code == -702:
            log(f"[-702] 频率太快，线程{thread_id} 等待 {delay_ms}ms")
        elif code == -1:
            log(f"[-1] 网络错误，线程{thread_id} 等待 {delay_ms}ms")
        else:
            log(f"\033[91m❌ 未知状态码 {code} msg={msg} 线程{thread_id} 等待 {delay_ms}ms\033[0m")

        time.sleep(delay_sec)

    log(f"\033[91m❌ {reserve_id} 失败 [线程{thread_id}] 已发送{req_cnt}次请求\033[0m")

def _preheat_connection(url=RESV_URL, rounds=None):
    payload = f"csrf={BILI_JCT}&reserve_id=0&reserve_type={CFG['reserve_type']}".encode()
    rounds = rounds or min(CFG["preheat_rounds"], CFG["threads"])
    for _ in range(rounds):
        try:
            _post_func(url, payload, {"content-type": "application/x-www-form-urlencoded"})
        except Exception:
            pass

def preheat_ids(id_list, url_map, rounds=None):
    rounds = rounds or min(CFG["preheat_rounds"], CFG["threads"])
    for rid in id_list:
        url = url_map.get(rid, RESV_URL)
        payload = f"csrf={BILI_JCT}&reserve_id={rid}&reserve_type={CFG['reserve_type']}".encode()
        for _ in range(rounds):
            try:
                _post_func(url, payload, {"content-type": "application/x-www-form-urlencoded"})
            except Exception:
                pass

# 暂停检测（轻量级）
def _check_pause():
    if msvcrt is None or not msvcrt.kbhit():
        return False
    ch = msvcrt.getch()
    if ch in (b'\x00', b'\xe0'):
        ch2 = msvcrt.getch()
        if ch2 == b';':          # F1
            log("\n⏸️  暂停：检测到 F1 键")
            while True:
                choice = input("c 继续 / q 退出: ").strip().lower()
                if choice == 'c':
                    log("▶️  继续抢票...")
                    return False
                elif choice == 'q':
                    log("🛑 退出抢票，返回主菜单...")
                    STOP_RESERVE.set()
                    return True
    return False

def fire_one(ses):
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
        if _check_pause():
            return False

    _preheat_connection(ses["url"])
    log(f"▶️  {ses['id']} {ses['title']}  {ses['start_s']}  即将开枪(提前 {CFG['ahead_sec']}s)")
    ths = [threading.Thread(target=gun_worker,
                            args=(ses["id"], fire_at_server, ses["url"], ses["ticket"], i))
           for i in range(CFG["threads"])]
    for t in ths:
        t.start()
    for t in ths:
        while t.is_alive():
            t.join(timeout=0.1)
            if _check_pause():
                # 快速通知所有线程停止
                for t2 in ths:
                    t2.join(timeout=0.5)
                return False
    return True

def fire_group(sess_list):
    STOP_RESERVE.clear()
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
        if _check_pause():
            return False

    _preheat_connection(sess_list[0]["url"])
    for s in sess_list:
        log(f"▶️  {s['id']} {s['title']}  {s['start_s']}  即将开枪(提前 {CFG['ahead_sec']}s)")
    ths = []
    thread_id = 0
    for s in sess_list:
        for i in range(CFG["threads"]):
            ths.append(threading.Thread(target=gun_worker,
                                        args=(s["id"], fire_at_server, s["url"], s["ticket"], thread_id)))
            thread_id += 1
    for t in ths:
        t.start()
    # 等待所有线程结束，同时检测暂停
    for t in ths:
        while t.is_alive():
            t.join(timeout=0.1)
            if _check_pause():
                for t2 in ths:
                    t2.join(timeout=0.5)
                return False
    return True

# ── 7. 已选目标管理 ──
SELECTED_TARGET_IDS = []

def _select_targets_interactive():
    sesses = parse_sessions(fetch_info(reserve_type=0))
    goods_data = fetch_goods()
    goods_lst = parse_goods(goods_data) if goods_data else []
    all_items = sesses + goods_lst
    if not all_items:
        log("⚠️ 没有可用的场次或商品")
        return []

    ids_in = input("输入要抢的 id（逗号分隔）、auto 自动挑选、或关键词: ").strip()
    if ids_in.lower() == "auto":
        now = now_server()
        targets = [s for s in all_items if s["status"] == 0 and s["start"] > now]
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
            if input("确认抢这些项目？(y/n): ").strip().lower() != "y":
                log("已取消")
                return []
    if not targets:
        log("⚠️ 没有符合条件的项目")
        return []
    log("已选择以下目标：")
    for s in targets:
        tag = "🛒" if s.get("is_goods") else "🎫"
        log(f"   {tag} {s['id']} - {s['title']} ({s['start_s']})")
    return targets

def _execute_grab(targets):
    if not targets:
        return
    groups = group_by_start(targets)
    id_to_url = {s["id"]: s["url"] for _ts, g in groups for s in g}
    preheat_ids(list(id_to_url.keys()), id_to_url)
    for start_ts, ses_lst in groups:
        if not fire_group(ses_lst):
            log("抢票已被用户中断")
            break
    log("🚩 抢票流程结束")

# ── 8. 菜单/业务函数 ──
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

def grab_flow():
    global SELECTED_TARGET_IDS, _CALIBRATED

    sesses = parse_sessions(fetch_info(reserve_type=0))
    goods_data = fetch_goods()
    goods_lst = parse_goods(goods_data) if goods_data else []
    all_items = sesses + goods_lst

    targets = []
    if SELECTED_TARGET_IDS:
        id_set = set(SELECTED_TARGET_IDS)
        valid = [s for s in all_items if s["id"] in id_set and s["status"] == 0 and s["start"] > now_server()]
        if valid:
            log(f"📌 已有已选目标 ({len(valid)} 个)：")
            for s in valid:
                tag = "🛒" if s.get("is_goods") else "🎫"
                log(f"   {tag} {s['id']} - {s['title']} ({s['start_s']})")
            choice = input("是否直接抢这些目标？(y=继续 / n=重新选择): ").strip().lower()
            if choice == 'y':
                targets = valid
            else:
                SELECTED_TARGET_IDS.clear()
                log("已清除旧目标，请重新选择：")
        else:
            log("⚠️ 已选目标不再有效，自动清除。")
            SELECTED_TARGET_IDS.clear()

    if not targets:
        targets = _select_targets_interactive()
        if targets:
            SELECTED_TARGET_IDS = [s["id"] for s in targets]
            log(f"✅ 目标已保存，共 {len(targets)} 个")

    if not targets:
        log("未选择目标，返回主菜单")
        return

    if not _CALIBRATED:
        log("⏱️  时间尚未校准，正在自动校准...")
        _calibrate_offset()
        if not _CALIBRATED:
            log("⚠️  自动校准失败，无法抢票。请手动校准后重试（菜单 1）")
            return
        log("✅ 校准完成，立即开始抢票")

    _execute_grab(targets)

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
        rpt = int(input(f"每线程请求数[{CFG['requests_per_thread']}]: ") or CFG['requests_per_thread'])
        jit = int(input(f"时间抖动ms[{CFG['time_jitter_ms']}]: ") or CFG['time_jitter_ms'])
        CFG.update(ahead_sec=a, threads=th, requests_per_thread=rpt, time_jitter_ms=jit)
        print("\n当前重试延迟(ms):")
        for k, v in CFG["retry_delays"].items():
            print(f"  {k}: {v}")
        if input("是否修改重试延迟？(y/n): ").strip().lower() == "y":
            for key in CFG["retry_delays"]:
                if key == "default": continue
                val = input(f"  {key} 延迟(ms)[{CFG['retry_delays'][key]}]: ").strip()
                if val:
                    CFG["retry_delays"][key] = int(val)
            val = input(f"  默认延迟(ms)[{CFG['retry_delays']['default']}]: ").strip()
            if val:
                CFG["retry_delays"]["default"] = int(val)
        log("参数已更新:", CFG)
    except Exception as e:
        log("输入有误:", e)

# ── 9. 压测与推荐 ──
def _auto_pressure_tune():
    thread_levels = [8, 16, 24, 32, 40, 48, 56, 64]
    rpt_levels = [1, 2, 3, 4]
    log(f"将测试线程数 {thread_levels} 和每线程请求数 {rpt_levels} 的组合...")
    results = []

    for rpt in rpt_levels:
        for th in thread_levels:
            lat = []
            http_stats = []
            biz_codes = []

            def _w(rpt=rpt):
                payload = f"csrf={BILI_JCT}&reserve_id=0&reserve_type={CFG['reserve_type']}".encode()
                hdr = {"content-type": "application/x-www-form-urlencoded"}
                for _ in range(rpt):
                    t0 = time.perf_counter()
                    try:
                        resp = _post_func(RESV_URL, payload, hdr)
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
            total_req = th * rpt
            results.append((th, rpt, mean_ms, p90_ms, cost, total_req))
            log(f"线程 {th:>2d} * 请求/线程 {rpt} (总{total_req:>3d})  平均 {mean_ms:.1f}ms  P90 {p90_ms:.1f}ms")

    best = None
    best_score = -1
    for th, rpt, mean, p90, cost, total in results:
        if mean < 200 and p90 < 300:
            if total > best_score or (total == best_score and best and mean < best[2]):
                best_score = total
                best = (th, rpt, mean, p90, cost, total)

    if best is None:
        best = min(results, key=lambda x: x[2])
        log(f"⚠️ 推荐平均延迟最小的组合: 线程 {best[0]}, 每线程请求 {best[1]} (平均 {best[2]:.1f}ms)")
    else:
        log(f"✅ 推荐最佳配置: 线程 {best[0]}, 每线程请求 {best[1]} (总请求 {best[5]}) 平均 {best[2]:.1f}ms")

    if input("是否应用推荐配置？(y/n): ").strip().lower() == 'y':
        CFG['threads'] = best[0]
        CFG['requests_per_thread'] = best[1]
        log(f"参数已应用！线程数={best[0]}, 每线程请求数={best[1]}")
    else:
        log("未应用")

# ── 10. 界面辅助 ──
class _CLR:
    HEADER = '\033[95m' if sys.platform != 'win32' else ''
    BLUE = '\033[94m' if sys.platform != 'win32' else ''
    GREEN = '\033[92m' if sys.platform != 'win32' else ''
    YELLOW = '\033[93m' if sys.platform != 'win32' else ''
    RED = '\033[91m' if sys.platform != 'win32' else ''
    END = '\033[0m' if sys.platform != 'win32' else ''
    BOLD = '\033[1m' if sys.platform != 'win32' else ''

def _print_header(title):
    print(f"\n{_CLR.BOLD}{_CLR.BLUE}{'='*50}{_CLR.END}")
    print(f"{_CLR.BOLD}{_CLR.BLUE}  {title}{_CLR.END}")
    print(f"{_CLR.BOLD}{_CLR.BLUE}{'='*50}{_CLR.END}")

def _print_config():
    days_str = ",".join({1:"10",2:"11",3:"12"}.get(d, str(d)) for d in TICKET_DAYS)
    dry = "ON" if CFG["dry_run"] else "OFF"
    calib = "✅ 已校准" if _CALIBRATED else "❌ 未校准"
    print(f"{_CLR.YELLOW}⚙️  当前配置{_CLR.END}")
    print(f"   日期: 7月{days_str}日 | 提前: {CFG['ahead_sec']}s | 线程: {CFG['threads']} | "
          f"每线程请求: {CFG['requests_per_thread']} | 抖动: ±{CFG['time_jitter_ms']}ms")
    print(f"   校时状态: {calib}  | Dry-Run: {dry}")
    if SELECTED_TARGET_IDS:
        print(f"   已选目标: {len(SELECTED_TARGET_IDS)} 个 (id: {', '.join(map(str, SELECTED_TARGET_IDS))})")

def _manual_calibrate():
    global _CALIBRATED
    _calibrate_offset()
    if SELECTED_TARGET_IDS:
        log(f"📌 当前已选目标: {SELECTED_TARGET_IDS}")
        if input("是否立即使用已选目标开始抢票？(y/n): ").strip().lower() == 'y':
            grab_flow()
            return
    log("返回主菜单。")

def main():
    while True:
        try:
            _print_header("BWS")
            _print_config()
            print(f" {_CLR.BOLD}1{_CLR.END}) 校准服务器时间（{_CLR.GREEN if _CALIBRATED else _CLR.RED}{'已校准' if _CALIBRATED else '未校准'}{_CLR.END}）")
            print(f" {_CLR.BOLD}2{_CLR.END}) 检查 Cookie 有效性")
            print(f" {_CLR.BOLD}3{_CLR.END}) 查看全部场次（活动+商品）")
            print(f" {_CLR.BOLD}4{_CLR.END}) 自动抢票（选择目标 & 执行）")
            print(f" {_CLR.BOLD}5{_CLR.END}) 手动设置参数")
            print(f" {_CLR.BOLD}6{_CLR.END}) 切换 Dry-Run（当前：{CFG['dry_run']}）")
            print(f" {_CLR.BOLD}7{_CLR.END}) 自动压测并推荐最优线程/并发")
            if SELECTED_TARGET_IDS:
                print(f" {_CLR.BOLD}8{_CLR.END}) 清除已选目标 (当前: {len(SELECTED_TARGET_IDS)} 个)")
            else:
                print(f" {_CLR.BOLD}8{_CLR.END}) 清除已选目标 (无)")
            print(f" {_CLR.BOLD}0{_CLR.END}) 退出")
            print(f"{_CLR.BOLD}{_CLR.BLUE}{'='*50}{_CLR.END}")
            choice = input("请选择操作：").strip()

            if choice == "1":
                _manual_calibrate()
            elif choice == "2":
                check_cookie()
            elif choice == "3":
                show_today()
            elif choice == "4":
                grab_flow()
            elif choice == "5":
                set_params()
            elif choice == "6":
                CFG["dry_run"] = not CFG["dry_run"]
                log(f"Dry-Run 已切换为 {CFG['dry_run']}")
            elif choice == "7":
                _auto_pressure_tune()
            elif choice == "8":
                if SELECTED_TARGET_IDS:
                    SELECTED_TARGET_IDS.clear()
                    log("已清除所有已选目标。")
                else:
                    log("当前没有已选目标。")
            elif choice == "0":
                log("👋 退出程序，再见！")
                break
            else:
                print(f"{_CLR.RED}请输入 0-8 之间的选项{_CLR.END}")
        except KeyboardInterrupt:
            print(f"\n{_CLR.YELLOW}^C 中断，程序退出{_CLR.END}")
            break
        except Exception as e:
            log(f"{_CLR.RED}⚠️ 运行时异常: {e}{_CLR.END}")

if __name__ == "__main__":
    main()
