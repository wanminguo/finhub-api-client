#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FinHub 信号客户端 —— 连接 signal API、按信号下单、并把回执回传（计次凭据）
================================================================================
一段话说明它干什么：
    本机跑一个小程序 → 长轮询订阅你的 BTC 信号 → 到点按信号方向挂**限价单**
    （信号价 + 0.05，硬上限 0.85，FAK：吃到多少算多少）→ 把**成交回执**回传给平台。
    平台上按**成交股数**计费（1 股 = 1 额度，633 股 = 633 额度），所以没成交不扣你的额度。

对外依赖：
    · 信号订阅 / 回执上报 / 次数查询：urllib（标准库）
    · 本地面板：http.server（标准库），浏览器打开 http://127.0.0.1:8787
    · 出网隧道：本地 CONNECT 代理（标准库 socket），**任何**库都能通过它出去
      （国内直连 Polymarket 不通，所以走隧道；代理只监听 127.0.0.1 + 只放白名单域名）
    · **实盘下单**：内置 vendor/py_clob_client（SDK 已随客户端打包，
      零 pip 安装开箱即用；若本机 Python 与打包版本不符会自动 pip install 一次）
      —— 纸面模式（默认）不需要它，零依赖就能跑通全链路。

三种运行模式：
    paper   纸面：照信号"假设成交"，只记日志与面板（默认；用来验证链路与体验）
    live    实盘：真的下单（需要 py-clob-client + 你的钱包私钥，私钥只在本机）
    dry     只看信号、什么都不做（排查用）

安全底线（代码里就是这么写的）：
    1. 私钥/API 凭证**只在你的机器上**，绝不发往平台；隧道是**不解密转发**（TLS 端到端）
    2. 本地代理只绑 127.0.0.1，且**只放白名单域名**，不是通用翻墙出口
    3. 实盘必须显式 `--live`；首次会打印风险确认
    4. 启动先查额度余额（股）；余额为 0 会提示充值而不是空跑

没有自动"本金上限"这种功能：每收到一个信号就下**一单**，单笔金额 =
基础份数 × 倍数 × 限价，所以**控制投入靠的是 --base-shares / --multiplier
与你自己往 Polymarket 里放多少钱**，而不是靠软件里的开关。

用法（完整参数见 --help）：
    python -m finhub --key <你的APIKEY> --paper
    python -m finhub --key <你的APIKEY> --live --private-key <0x...> --multiplier 1
    python -m finhub --key <你的APIKEY> --status        # 只看额度/订阅，不下单
"""

import argparse
import html
import json
import os
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import ctypes
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# ★★ 2026-10-02 内置 py-clob-client（vendor 目录，开箱即用，用户无需 pip 安装）
#   · vendor/ 已打包 py_clob_client 及其全部依赖（eth_account / httpx / pydantic /
#     ckzg / bitarray / regex / parsimonious / requests… 含 dist-info 元数据）。
#   · 加载顺序：vendor 优先；没有 vendor 时回退到系统已安装的 py-clob-client。
#   · 若都没有：自动 pip install py-clob-client 一次（有网即可），再加载；
#     纸面/只看信号模式不需要它也能跑（延迟到 live 才真正 import）。
# ---------------------------------------------------------------------------
_VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)


def _ensure_clob_deps():
    """确保 py_clob_client 可用。返回 (ok, msg)。"""
    try:
        import py_clob_client  # noqa: F401
        return True, "py-clob-client 已就绪（内置 vendor）"
    except ImportError:
        pass
    try:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet",
                               "--disable-pip-version-check", "py-clob-client"],
                              timeout=300)
        import py_clob_client  # noqa: F401
        return True, "已自动安装 py-clob-client"
    except Exception as e:                                    # noqa: BLE001
        return False, "缺少 py-clob-client（%s）—— 纸面/只看信号可用；实盘需联网自动安装" % e


VERSION = "0.2.0"
DEFAULT_BASE = "https://api.wanminguo.top/quant/polymarket"
# ★★ 2026-09-28 重要变更：隧道改用「外层 TLS + CONNECT」。
#   原因（实测）：旧版把客户端的 ClientHello 原样转发，里面的 SNI
#   `clob.polymarket.com` 是**明文**的，国内链路的 DPI 看见就注入 RST ——
#   对照实验里 SNI=example.com 能握手、SNI=clob.polymarket.com 0.0 秒被掐。
#   所以现在客户端先与**平台自己的域名**建立一层 TLS（这个域名国内可直连、
#   SNI 无害），在这层加密通道里发 `CONNECT clob.polymarket.com:443`；
#   内层（与 Polymarket 的）TLS 藏在外层里，DPI 看不到。
#
#   因此默认隧道地址改成**带有效证书的主域名**（tun.api.wanminguo.top 没有 DNS 记录、
#   也没有证书，做不了外层 TLS；主域名两者都有）。
DEFAULT_TUNNEL = "api.wanminguo.top:8443"
DEFAULT_TUNNEL_SNI = "api.wanminguo.top"
# 备用 IP：域名解析失败时回退（外层 TLS 仍然用上面的 SNI 名校验证书）
TUNNEL_FALLBACK_IPS = ("43.161.239.203",)


def _writable_dir(preferred):
    """挑一个**真的能写**的状态目录。

    ★ 2026-09-28（联调踩到）：有些环境（受限的 Windows 账号、只读 HOME、
      容器里的非 root 用户）根本建不了 ~/.finhub —— 而游标/配置都指望它。
      挑不到就直接退到系统临时目录，**绝不因为状态目录不可写就每轮刷屏报错**
      （第一次联调日志里刷了几十条 WinError 5，把真正的信息全埋了）。
    ★ 第二轮审查指出：README 写了"会在日志里说明"，但这里其实是静默回退 ——
      现在真打一行到 stderr（此时 log() 还不能用，它自己依赖这个目录）。
    """
    for d in (preferred, os.path.join(tempfile.gettempdir(), "finhub")):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".write_test")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("1")
            os.remove(probe)
            if d != preferred:
                sys.stderr.write("[finhub] 注意：%s 不可写，状态目录改用 %s\n"
                                 % (preferred, d))
            return d
        except OSError:
            continue
    sys.stderr.write("[finhub] 警告：找不到可写的状态目录，游标/配置将无法保存\n")
    return preferred


CONFIG_DIR = _writable_dir(os.path.join(os.path.expanduser("~"), ".finhub"))
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "client.log")
LEDGER_PATH = os.path.join(CONFIG_DIR, "orders.jsonl")   # 本地下单台账（持久化）
COOKIE_FILE = os.path.join(CONFIG_DIR, "cookies.txt")    # 平台登录会话（持久化）

# ---------------------------------------------------------------------------
# 本地下单台账（orders.jsonl）—— 每笔订单一行；结算后补写 win/pnl。
# 面板的「最近信号·回执·本地下单台账」与「连接池表格」都从这里聚合。
# ---------------------------------------------------------------------------
_LEDGER_LOCK = threading.Lock()


def ledger_append(rec):
    """追加一笔订单（信号→回执结果）。rec 为 dict。"""
    try:
        os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
        with _LEDGER_LOCK:
            with open(LEDGER_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def ledger_load():
    rows = []
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def ledger_purge(cid=None, market=None):
    """清空本地下单台账：cid=删该配置的所有单；market=删该市场的所有单；
    两者都不传 = 全部清空（重置重来）。返回剩余条数。"""
    recs = ledger_load()
    if cid:
        recs = [r for r in recs if (r.get("cid") or "") != cid]
    elif market:
        recs = [r for r in recs if (r.get("market") or "") != market]
    else:
        recs = []
    try:
        with _LEDGER_LOCK:
            with open(LEDGER_PATH, "w", encoding="utf-8") as fh:
                for r in recs:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return len(recs)


def ledger_update_settle(market, ws, outcome, avg_side=None):
    """窗口结算后，把该市场该窗口还没写盈亏的订单补上 win/pnl。
    outcome: 'UP'/'DOWN'（平台结果）。返回更新的笔数。"""
    rows = ledger_load()
    changed = 0
    for r in rows:
        if (r.get("market") != market or int(r.get("ws") or 0) != int(ws)
                or r.get("win") is not None):
            continue
        side = str(r.get("side") or "").upper()
        if not side:
            continue
        win = (side == str(outcome).upper())
        avg = float(r.get("avg") or 0)
        filled = int(r.get("filled") or 0)
        if filled > 0 and avg > 0:
            pnl = round(filled * (1.0 - avg), 4) if win else round(-filled * avg, 4)
        else:
            pnl = 0.0
        r["win"] = 1 if win else 0
        r["pnl"] = pnl
        changed += 1
    if changed:
        try:
            with _LEDGER_LOCK:
                with open(LEDGER_PATH, "w", encoding="utf-8") as fh:
                    for r in rows:
                        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        except OSError:
            pass
    return changed


def ledger_stats(rows, group_key):
    """按 group_key（配置名 cid 或 market）聚合：
    orders / filled / win / lose / pnl / winrate。"""
    agg = {}
    for r in rows:
        k = r.get(group_key) or "?"
        a = agg.setdefault(k, {"orders": 0, "filled": 0, "win": 0, "lose": 0,
                               "pnl": 0.0})
        a["orders"] += 1
        a["filled"] += int(r.get("filled") or 0)
        if r.get("win") is not None:
            if r["win"]:
                a["win"] += 1
            else:
                a["lose"] += 1
        a["pnl"] += float(r.get("pnl") or 0)
    for a in agg.values():
        done = a["win"] + a["lose"]
        a["winrate"] = round(a["win"] * 100.0 / done, 1) if done else None
        a["pnl"] = round(a["pnl"], 2)
    return agg

# ---------------------------------------------------------------------------
# 本地分组/档口（马丁阶梯）—— 纯本地计算，绝不读服务器的档位/份数
# ---------------------------------------------------------------------------
# ★ 2026-10-01（用户口径修正）：本地档口加载**策略的 15 级股数表**，
#   1/2/5/10 是**信号端倍数**（不是档位表）。
#   · 份数 = 档口股数（15 级）× 倍数，例如第 1 档 ×1 = 1 份、第 5 档 ×10 = 150 份。
#   · 档口股数表与服务器策略 low_rebound rounds.shares 一致（引擎自己也是这么加码的）。
#   · 倍数越大越难在盘口吃满，成交不了就按实际成交量计额度（用户口径：多了买不进出）。
#   · 组步长 12：赢回第 1 档后组 +1，超过 12 组回第 1 组（每 12 单一轮回）。
#   · 最大连亏 15 档：连亏升档；15 档仍未赢 → 本轮爆仓，重置回第 1 组第 1 档。
#   · 结算事实来自平台（v1/settle 只返回市场结果 UP/DOWN），
#     档位推进规则在本机 ladder 语义里，与服务器策略引擎无关。
LADDER_PATH = os.path.join(CONFIG_DIR, "ladder.json")
LADDER_SHARES_15 = [1, 3, 6, 10, 15, 21, 33, 52, 83, 131,
                    207, 327, 516, 816, 1289]       # 策略 15 级档口股数（第 1~15 档）
# ★ 2026-10-02（用户拍板方案B）：旧表 [1,3,6,10,15,23,34,50,72,105,151,216,310,443,633]
#   后档只有 ~1.45 倍速，在 p=0.32~0.35 下无法覆盖前档累计亏损（第11档赢+99.3
#   追不平前10档亏 111.9，净亏 12.6U）。新表前 5 档不变，第 6 档起按
#   Sₙ ≥ 0.58×ΣS₍<n₎ + 0.5/(1−p) 递推，每档赢都覆盖前面亏损并留目标利润。
LADDER_GROUP_STEP = 12
LADDER_MAX_ROUNDS = 15
LADDER_MAX_GROUP_STEP = 50          # 界面可配上限
LADDER_MAX_ROUNDS_LIMIT = 25        # 界面可配档口上限
LADDER_MULT_OPTIONS = [1, 2, 5, 10]             # 信号端倍数（界面下拉，4 档）
LADDER_MAX_MULT = 10

# 隧道白名单（客户端这一侧也挡一遍；服务端还有一道）
TUNNEL_ALLOW = (
    "clob.polymarket.com",
    "gamma-api.polymarket.com",
    "data-api.polymarket.com",
    "ws-subscriptions-clob.polymarket.com",
    "polygon-rpc.com",
    "rpc.ankr.com",
    "polygon.llamarpc.com",
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def log(msg, path=None):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        path = path or LOG_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_config(path=CONFIG_PATH):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg, path=CONFIG_PATH):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        # ★ 2026-09-28（第二轮审查 P1）：配置里有 API key，权限必须收紧。
        #   POSIX 默认受 umask 影响（常见 0644 = 同机其他用户可读）。
        try:
            os.chmod(path, 0o600)
            os.chmod(os.path.dirname(path) or ".", 0o700)
        except OSError:
            pass                    # Windows 上 chmod 基本无效，README 里已说明
    except OSError as e:
        log("配置写盘失败：%s" % e)


def market_from_key(key=""):
    """★ 2026-10-04（用户口径）：市场由 KEY 决定，不靠配置名称/字段。
    信号 KEY 格式：pm_live_<策略ID>_<市场ID>_<32位hex>
    —— 最后一段是 32 位 hex，倒数第二段就是市场 ID（如 btc-5m）。
    转成界面市场名：btc-5m → BTC-5m。解析不到返回空串（由调用方兜底）。"""
    if not key:
        return ""
    parts = str(key).strip().split("_")
    if len(parts) >= 3 and len(parts[-1]) == 32:
        mid = parts[-2]
        if "-" in mid:
            a, b = mid.split("-", 1)
            if a and b:
                return "%s-%s" % (a.upper(), b)
        return mid.upper()
    return ""


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 本地档口状态（ladder.json）
# ★ 2026-10-04（用户口径）：多引擎并行 —— ladder 按 KEY（api_key）隔离，
#   同一 KEY 的马丁档位状态延续（换配置不丢档位），不同 KEY 互不干扰。
#   文件结构：{ "<api_key>": { "<market>": {...entry...} }, ... }
#   旧平铺格式 { "<market>": {...} } 自动兼容（视为未分 KEY 的旧档位）。
# ---------------------------------------------------------------------------
_LADDER_LOCK = threading.Lock()


def load_ladder(path=LADDER_PATH):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_ladder(lad, path=LADDER_PATH):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(lad, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        log("档口状态写盘失败：%s" % e)


def ladder_scope(key):
    """取某 KEY 的档口子树（market → entry）。旧平铺格式自动兼容：
       顶层键不是任何已存 KEY 时按旧格式整体返回（引擎启动后会自动迁移保存）。"""
    lad = load_ladder()
    if not lad:
        return {}
    if key in lad and isinstance(lad.get(key), dict):
        return lad.get(key)
    return dict(lad)


def save_ladder_scope(key, scope):
    """写某 KEY 的档口子树（带锁，多引擎并发安全）。"""
    with _LADDER_LOCK:
        lad = load_ladder()
        lad = {k: v for k, v in lad.items() if not (k != key and isinstance(v, dict) and "round" in v)}
        # 迁移旧平铺：旧格式 {market: entry} 并入该 KEY（首次启动时）
        for k, v in list(lad.items()):
            if k != key and isinstance(v, dict) and "round" in v and k not in scope:
                scope.setdefault(k, v)
                lad.pop(k)
        lad[key] = scope
        save_ladder(lad)


def reset_ladder(key=None):
    """删除本地组/档口，重新分组档口：不传 = 全部重置；传 KEY = 只重置该 KEY。"""
    with _LADDER_LOCK:
        lad = load_ladder()
        if key:
            lad.pop(key, None)
        else:
            lad.clear()
        save_ladder(lad)
    return lad


def ladder_entry(lad, market):
    return lad.setdefault(str(market), {"group": 1, "round": 1, "losses": 0,
                                        "wins": 0, "last_ws": 0, "last_side": ""})


def shares_for_round(round_, multiplier=1, table=None, max_rounds=None):
    """档口 → 份数：档口股数表[当前档] × 信号端倍数（1/2/5/10）。
    档口股数表 / 最多档口可由每个「连接配置」自定义（cc.shares / cc.max_rounds），
    不填则用默认 15 级表。无硬性份数封顶 —— 倍数越大盘口越难吃满，
    按实际成交量计额度。"""
    table = table or LADDER_SHARES_15
    max_rounds = max_rounds or LADDER_MAX_ROUNDS
    idx = min(max(int(round_), 1), int(max_rounds), len(table)) - 1
    base = table[idx]
    mult = min(max(int(multiplier), 1), LADDER_MAX_MULT)
    return max(1, int(base * mult))


def parse_shares_text(text):
    """解析界面填写的档口股数：支持「1,3,6,10」或 JSON 数组。失败返回 None。
    ★ 旧版配置里 "shares" 是基础份数（int，如 1）—— int/float 一律视为遗留字段，返回 None。"""
    if text is None or isinstance(text, (int, float)):
        return None
    t = str(text).strip()
    if not t:
        return None
    try:
        t = t.strip()
        if t.startswith("["):
            vals = json.loads(t)
        else:
            vals = [v for v in t.replace("，", ",").replace(";", ",").replace(" ", ",").split(",") if v != ""]
        out = []
        for v in vals:
            iv = int(v)
            if iv < 1:
                return None
            out.append(iv)
        if not out or len(out) > LADDER_MAX_ROUNDS_LIMIT:
            return None
        return out
    except Exception:                                      # noqa: BLE001
        return None


def ladder_update_result(lad, market, win, group_step=None, max_rounds=None, key=None):
    """按已结算窗口的结果推进本地档口（只认市场事实，不读服务器档位）：
       赢 → 回第 1 档、组 +1（超过组步长回第 1 组）；亏 → 升一档；
       满档仍未赢 → 本轮爆仓，重置回第 1 组第 1 档。
       组步长 / 最多档口按配置传入（cc.group_step / cc.max_rounds）。
       ★ 2026-10-04：key 非空时落盘写该 KEY 的档口子树（多引擎隔离）。"""
    group_step = int(group_step or LADDER_GROUP_STEP)
    max_rounds = int(max_rounds or LADDER_MAX_ROUNDS)
    e = ladder_entry(lad, market)
    if win:
        e["round"] = 1
        e["losses"] = 0
        e["wins"] += 1
        e["group"] += 1
        if e["group"] > group_step:
            e["group"] = 1
    else:
        e["losses"] += 1
        if e["round"] < max_rounds:
            e["round"] += 1
        else:
            e["round"] = 1
            e["group"] = 1
            e["losses"] = 0
    if key:
        save_ladder_scope(key, lad)
    return e


# ---------------------------------------------------------------------------
# 本地游标（防重启重放，审查 P2-9）
# ---------------------------------------------------------------------------
# ★ 为什么必须落盘：服务的投递窗口是 600 秒，而客户端的 since 默认退回到
#   "1 小时前"、seen 只在内存里。进程一崩/一重启，同一批信号会被**再下一次真单**
#   （服务端的回执幂等只保证不重复扣次，**不保证不重复下单**）。
#   所以游标 + 已处理 signal_id 必须写到磁盘。
CURSOR_PATH = os.path.join(CONFIG_DIR, "cursor.json")
# ★ 第二轮审查 P2：原来只留 200 条已处理 signal_id，而日志声称"重启不会重复下单"。
#   正常 68 条/天够用，但一次性补发几百条后再重启就会重放（服务端幂等只保证
#   不重复扣次，**不保证不重复下真单**）。留 2000 条，代价只有几十 KB。
CURSOR_KEEP = 2000
_cursor_saved = [0.0]        # 上次落盘的 since（节流：没变化就不写盘）
_cursor_err_at = [0.0]       # 上次报错的时间（同一个错误最多每分钟报一次）
_CURSOR_LOCK = threading.Lock()


def load_cursor(key=None, path=CURSOR_PATH):
    """取某 KEY 的游标（since + 已处理 signal_id）。
    ★ 2026-10-04（多引擎并行）：文件结构 { "keys": { "<api_key>": {"since":..,"seen":[..]}, ... } }；
      旧平铺 { "since":.., "seen":[..] } 自动兼容（迁移给调用方传入的 key）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and "keys" in d:
            k = (d.get("keys") or {}).get(str(key) or "__legacy__")
            if k:
                return float(k.get("since") or 0.0), [str(x) for x in (k.get("seen") or [])]
            return 0.0, []
        # 旧平铺格式
        return float(d.get("since") or 0.0), [str(x) for x in (d.get("seen") or [])]
    except Exception:                                            # noqa: BLE001
        return 0.0, []


def save_cursor(since, seen, key=None, path=CURSOR_PATH, force=False):
    """把游标/已处理 signal_id 落盘（按 KEY 隔离 + 节流 + 失败不刷屏）。

    ★ 为什么节流：原来每轮长轮询（10 秒一次）都写一次盘，日志里还会因为
      目录不可写刷一条错 —— 既没必要又淹没信息。只有游标真的前进了才写。
    """
    since = float(since)
    if not force and _cursor_saved[0] > 0 and abs(since - _cursor_saved[0]) < 0.5:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with _CURSOR_LOCK:
            old = {}
            try:
                with open(path, encoding="utf-8") as fh:
                    old = json.load(fh)
            except Exception:                                    # noqa: BLE001
                old = {}
            keys = old.get("keys") if isinstance(old, dict) and "keys" in old else {}
            if not isinstance(keys, dict):
                keys = {}
                # 旧平铺 → 迁移给该 KEY
                if isinstance(old, dict) and ("since" in old or "seen" in old):
                    keys[str(key) or "__legacy__"] = {
                        "since": float(old.get("since") or 0.0),
                        "seen": [str(x) for x in (old.get("seen") or [])],
                    }
            keys[str(key) or "__legacy__"] = {"since": since,
                                              "seen": list(seen)[-CURSOR_KEEP:]}
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"keys": keys}, fh)
        os.replace(tmp, path)
        _cursor_saved[0] = since
    except Exception as e:                                       # noqa: BLE001
        now = time.time()
        if now - _cursor_err_at[0] > 60:
            _cursor_err_at[0] = now
            log("游标落盘失败（不影响下单，但重启后可能重放最近 10 分钟的信号）：%s" % e)


# ---------------------------------------------------------------------------
# 未送达回执的本地暂存（第二轮审查 P2：扣次不许丢）
# ---------------------------------------------------------------------------
# ★ 场景：客户端发出回执时网络断了 / 平台 5xx / 被限速重试三次仍失败。
#   如果就这么算了，这笔成交永远扣不到次（平台少收钱、客户台账缺一笔）。
#   所以把"含真实成交"的回执写进 unsent.jsonl，之后每次连上就补交。
#   服务端 (signal_id,key_id) 幂等，重复补交只会被识别为重复，不会重复扣次。
UNSENT_PATH = os.path.join(CONFIG_DIR, "unsent.jsonl")
UNSENT_MAX = 500


def spool_receipt(rec):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        row = {"at": time.time(), "payload": rec}
        with open(UNSENT_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        log("回执已暂存到本地（%s），下次连上会自动补交" % UNSENT_PATH)
        return True
    except Exception as e:                                       # noqa: BLE001
        log("回执暂存失败（这笔成交会漏扣）：%s" % e)
        return False


def replay_unsent(api, limit=50):
    """补交之前失败的回执。返回成功补交的条数。"""
    if not os.path.exists(UNSENT_PATH):
        return 0
    try:
        with open(UNSENT_PATH, encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
    except Exception as e:                                       # noqa: BLE001
        log("读暂存回执失败：%s" % e)
        return 0
    if not rows:
        return 0
    keep, done = [], 0
    for r in rows[:limit]:
        p = (r or {}).get("payload") or {}
        if not p.get("signal_id"):
            continue
        d, code = api.receipt(p, retries=1)
        # 成功、或服务端明确拒绝（重发也没意义）→ 都从暂存里去掉
        if d.get("ok") or code in (400, 401, 403, 404, 405):
            done += 1
            if d.get("ok"):
                log("补交回执成功：%s（扣次 %s）"
                    % (p.get("signal_id"), (d.get("data") or {}).get("charged")))
        else:
            keep.append(r)
    keep += rows[limit:]
    try:
        with open(UNSENT_PATH, "w", encoding="utf-8") as fh:
            for r in keep[-UNSENT_MAX:]:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:                                            # noqa: BLE001
        pass
    return done


# ---------------------------------------------------------------------------
# 平台 API（信号 / 回执 / 额度）
# ---------------------------------------------------------------------------
class Api:
    """平台 API 客户端。

    ★★ 2026-09-28（第二轮审查 P0）：平台请求**绝不允许**走本地出网隧道。
      上一版在实盘初始化里写了 `os.environ.setdefault("HTTP_PROXY", ...)` ——
      那是**进程全局**的，urllib 也会读它，于是平台请求被塞进本地那个
      **只支持 CONNECT** 的代理，代理对普通 GET/POST 一律回 405 →
      `--live`（默认带隧道）时信号/额度/回执三个接口**全线 405**，
      而且日志只写"拉信号失败：405"，看起来像平台挂了。
      现在：API 用**自带 opener + 空 ProxyHandler**，无论环境变量怎么设都不走代理；
      隧道只给 SDK 用（见 Trader._make_client）。
    """

    def __init__(self, key=None, base=DEFAULT_BASE, timeout=35):
        self.key = key
        self.base = base.rstrip("/")
        self.timeout = timeout
        # 会话 CookieJar（登录用，持久化到 ~/.finhub/cookies.txt，重启客户端保持登录）
        import http.cookiejar as _cjmod
        self._cj = _cjmod.MozillaCookieJar(COOKIE_FILE)
        try:
            self._cj.load(ignore_discard=True, ignore_expires=True)
        except Exception:                                            # noqa: BLE001
            pass
        # 显式"不走任何代理"（含环境变量 HTTP_PROXY/HTTPS_PROXY）
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self._cj))

    def _req(self, path, method="GET", body=None, query=None, form=None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Accept": "application/json",
                   "User-Agent": "finhub-client/%s" % VERSION}
        if self.key:
            headers["X-Api-Key"] = self.key
        if form is not None:
            # 表单编码：登录/登出等浏览器风格接口（auth.php 读 $_POST）
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        # ★ 2026-10-03（卡死修复）：Windows 上 urllib 的 timeout 对 TCP connect
        #   阶段不生效 —— 服务器黑洞（SYN 不响应）时会**无限卡住**，曾导致引擎
        #   线程 8 小时无轮询、面板却显示"运行中"。这里用 socket 级默认超时兜底，
        #   每次请求前设置、结束后恢复，保证 connect+read 都有硬上限。
        old_sock_to = None
        try:
            old_sock_to = socket.getdefaulttimeout()
        except Exception:                                            # noqa: BLE001
            pass
        try:
            socket.setdefaulttimeout(self.timeout)
            with self._opener.open(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace")), r.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw), e.code
            except ValueError:
                return {"ok": False, "error": "http_%d" % e.code, "raw": raw[:400]}, e.code
        except socket.timeout as e:
            return {"ok": False, "error": "network", "message": "请求超时(%ss): %s"
                    % (self.timeout, e)}, 0
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": "network", "message": str(e)}, 0
        finally:
            if old_sock_to is not None:
                try:
                    socket.setdefaulttimeout(old_sock_to)
                except Exception:                                  # noqa: BLE001
                    pass

    def signals(self, since, wait=10, limit=20, market=None):
        q = {"since": "%.3f" % since, "wait": wait, "limit": limit}
        if market:
            q["market"] = market
        return self._req("/v1/signals.php", query=q)

    def receipt(self, payload, retries=3):
        """上报回执。

        ★ 2026-09-28（第二轮审查 P2）：**必须重试**。
          服务端为了"扣次不许丢"专门在死锁/唯一键上做了退避重试，而客户端这边
          原来只发一次 —— 遇到 429（信号端点限速下限 5qps）或 500
          （receipt_failed）就直接记一行日志过去了，游标也已经推进，
          这笔扣次就**永久丢失**（平台少收钱、客户台账缺失）。
          这里对"可能是暂时性"的失败（网络错误 / 429 / 5xx）退避重发；
          对明确的业务拒绝（400/401/403）不重试（重试也不会成功）。
        """
        delay = 1.0
        d, code = {"ok": False, "error": "not_sent"}, 0
        for i in range(retries + 1):
            d, code = self._req("/v1/receipt.php", method="POST", body=payload)
            if d.get("ok") or code in (400, 401, 403, 404, 405):
                return d, code
            if i < retries:
                log("回执上报失败（HTTP %s %s），%.0f 秒后重试第 %d 次"
                    % (code, d.get("error") or d.get("message") or "网络", delay, i + 1))
                time.sleep(delay)
                delay *= 2
        return d, code

    def credits(self, ledger=20):
        return self._req("/v1/credits.php", query={"ledger": ledger})

    def settle(self, market=None, last=40):
        """最近已结算窗口（只返回市场事实：outcome=UP/DOWN，不含任何档位）。
        客户端用它把本地档口往前推 —— 服务器不参与分组/档口计算。"""
        q = {"last": last}
        if market:
            q["market"] = market
        return self._req("/v1/settle.php", query=q)

    # ------------------------------------------------------------------
    # 平台用户登录（★ 2026-10-03 新增）：会话由本 opener 的 CookieJar 维护。
    # 与网页 /me/login.php 共用同一张 users 表与同一 session（pmu_uid）。
    # 请求不带 X-Api-Key —— 登录态由 session cookie 标识。
    # ------------------------------------------------------------------
    def auth(self, action="me", username=None, password=None):
        """action: login / logout / me
        返回 (json, http_code)。登录成功后 CookieJar 已保存到磁盘。"""
        if action == "login":
            return self._req("/v1/auth.php", method="POST",
                             form={"action": "login",
                                   "username": username or "",
                                   "password": password or ""})
        if action == "logout":
            return self._req("/v1/auth.php", method="POST",
                             form={"action": "logout"})
        return self._req("/v1/auth.php")

    def usage_daily(self):
        """每日额度用量（需已登录 session）。返回 (json, code)。"""
        return self._req("/v1/usage.php")

    # ------------------------------------------------------------------
    # 公网远程查看隧道（★ 2026-10-05 新增）：每用户一条 + 平台余额付费订阅。
    #   GET  /v1/tunnel.php         查订阅状态/价格/到期/端口/公网 URL/平台余额
    #   POST /v1/tunnel.php  action=subscribe  扣平台余额订阅（返回 url + private_key）
    #   需已登录（session cookie）。模拟盘不影响隧道（隧道只透传本地面板）。
    # ------------------------------------------------------------------
    def tunnel(self, action="status"):
        if action == "subscribe":
            return self._req("/v1/tunnel.php", method="POST",
                             form={"action": "subscribe"})
        return self._req("/v1/tunnel.php")


# ---------------------------------------------------------------------------
# 全局登录状态（面板顶部「用户登录」区块的数据源）
# ---------------------------------------------------------------------------
_AUTH_LOCK = threading.Lock()
_AUTH = {"user": None, "keys": [], "pool": None, "usage": None, "error": "",
         "last_check": 0.0, "busy": False}


def auth_refresh(force=False, username=None, password=None):
    """面板侧登录/查态/登出统一入口。

    username+password 同时给出 → 登录；否则只查当前会话。
    action='logout' 时走登出。返回 True/False（结果写进 _AUTH）。
    """
    import http.cookiejar as _cjmod
    with _AUTH_LOCK:
        if _AUTH["busy"] and not force:
            return True
        _AUTH["busy"] = True
    try:
        api = Api(None)                       # 不带 X-Api-Key，只走 session
        if username is not None and password is not None:
            d, code = api.auth("login", username, password)
            if not d.get("ok"):
                with _AUTH_LOCK:
                    _AUTH["error"] = (d.get("message") or d.get("error")
                                      or "登录失败（HTTP %s）" % code)
                    _AUTH["busy"] = False
                return False
        elif username == "__LOGOUT__":
            d, code = api.auth("logout")
            with _AUTH_LOCK:
                _AUTH.update(user=None, keys=[], pool=None, usage=None, error="")
                _AUTH["busy"] = False
            try:
                api._cj.save(ignore_discard=True, ignore_expires=True)
            except Exception:                                       # noqa: BLE001
                pass
            return True
        # 查当前登录态
        d, code = api.auth("me")
        if not d.get("ok"):
            with _AUTH_LOCK:
                _AUTH.update(user=None, keys=[], pool=None, usage=None,
                             error=d.get("message") or d.get("error") or "")
                _AUTH["last_check"] = time.time()
                _AUTH["busy"] = False
            return False
        # 登录态有效 → 一并拉每日用量
        # ★ 服务器 pma_ok() 包一层 data：用户/KEY 在 d["data"]，这里统一解包
        _dd = d.get("data") if isinstance(d.get("data"), dict) else {}
        u, uc = api.usage_daily()
        _ud = u.get("data") if (u.get("ok") and isinstance(u.get("data"), dict)) else None
        with _AUTH_LOCK:
            _AUTH.update(user=_dd.get("user"), keys=_dd.get("keys") or [],
                         pool=_dd.get("pool"), usage=_ud, error="", last_check=time.time())
            _AUTH["busy"] = False
        try:
            api._cj.save(ignore_discard=True, ignore_expires=True)
        except Exception:                                            # noqa: BLE001
            pass
        return True
    except Exception as e:                                           # noqa: BLE001
        with _AUTH_LOCK:
            _AUTH["error"] = str(e)
            _AUTH["busy"] = False
        return False


# ---------------------------------------------------------------------------
# 公网远程查看隧道（★ 2026-10-05 新增）：每用户一条 SSH 反向隧道 + 订阅权限
#   · 订阅 = 用户用平台 USDT 余额付费（POST v1/tunnel.php action=subscribe）
#   · 私钥落本机 ~/.finhub/tunnels/tun_<uid>.pem（0600），用 ssh -R 建反向隧道
#   · 公网访问 https://api.wanminguo.top/tunnel/<用户名>/ 直达本地面板
#   · 模拟盘同样可开隧道查看面板；面板本身不暴露平台密钥（实盘才查 PM 余额）
# ---------------------------------------------------------------------------
_TUNNEL_DIR = os.path.join(CONFIG_DIR, "tunnels")
_TUNNEL_LOCK = threading.Lock()
_TUNNEL = {"info": None, "error": "", "busy": False, "auto": False,
           "uid": None, "last_check": 0.0}
_TUNNEL_PROC = {"proc": None, "pid": 0, "port": 0, "started": 0.0}


def tunnel_refresh(force=False, max_age=60.0):
    """查订阅状态（60 秒缓存；force 强制刷新）。登录态由本地 session cookie 带。"""
    with _TUNNEL_LOCK:
        if _TUNNEL["busy"] and not force:
            return dict(_TUNNEL.get("info") or {})
        if (not force and _TUNNEL.get("info")
                and time.time() - _TUNNEL.get("last_check", 0) < max_age):
            return dict(_TUNNEL.get("info") or {})
        _TUNNEL["busy"] = True
    try:
        api = Api(None)
        d, code = api.tunnel("status")
        with _TUNNEL_LOCK:
            if d.get("ok"):
                info = d.get("data") if isinstance(d.get("data"), dict) else {}
                if not info and isinstance(d, dict):
                    info = d                      # 服务端也可能直接平铺
                _TUNNEL["info"] = info
                _TUNNEL["error"] = ""
                # uid 不在隧道接口返回里，从登录用户信息取
                with _AUTH_LOCK:
                    _u = _AUTH.get("user") or {}
                _TUNNEL["uid"] = (_u.get("id") or info.get("uid")
                                  or info.get("user_id") or None)
            else:
                _TUNNEL["error"] = (d.get("message") or d.get("error") or "")
            _TUNNEL["last_check"] = time.time()
            _TUNNEL["busy"] = False
    except Exception as e:                                          # noqa: BLE001
        with _TUNNEL_LOCK:
            _TUNNEL["error"] = str(e)
            _TUNNEL["busy"] = False
    with _TUNNEL_LOCK:
        return dict(_TUNNEL.get("info") or {})


def _tunnel_key_path(uid):
    try:
        os.makedirs(_TUNNEL_DIR, exist_ok=True)
    except Exception:                                                # noqa: BLE001
        pass
    return os.path.join(_TUNNEL_DIR, "tun_%s.pem" % uid)


def _tunnel_save_key(uid, privkey):
    """私钥落本机（0600）。返回是否已就绪。"""
    if not privkey or not uid:
        return False
    p = _tunnel_key_path(uid)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(str(privkey).strip() + "\n")
        try:
            os.chmod(p, 0o600)
        except Exception:                                            # noqa: BLE001
            pass
        return True
    except Exception:                                                # noqa: BLE001
        return False


def _tunnel_is_alive():
    """隧道子进程是否在运行（结束过则清理句柄）。"""
    proc = _TUNNEL_PROC.get("proc")
    if proc is None:
        return False
    if proc.poll() is not None:
        _TUNNEL_PROC["proc"] = None
        _TUNNEL_PROC["pid"] = 0
        return False
    return True


def _tunnel_info_snapshot():
    with _TUNNEL_LOCK:
        return dict(_TUNNEL.get("info") or {})


def tunnel_start(panel_port=8787, force=False):
    """开启公网隧道：ssh -N -R <tunnel_port>:127.0.0.1:<panel_port> tun_<uid>@api.wanminguo.top
    需要：已订阅（服务端 active）+ 本机已有私钥（首次订阅自动保存）。"""
    info = _tunnel_info_snapshot()
    if not info.get("subscribed") and not info.get("status") == "active":
        return {"ok": False, "error": "not_subscribed", "message": "还没有订阅公网远程查看服务"}
    if _tunnel_is_alive() and not force:
        return {"ok": True, "running": True}
    with _TUNNEL_LOCK:
        uid = _TUNNEL.get("uid")
    if not uid:
        uid = info.get("uid") or info.get("user_id")
    if not uid:
        return {"ok": False, "error": "no_uid", "message": "订阅信息缺少用户编号"}
    tport = int(info.get("port") or info.get("tunnel_port") or 0)
    if not tport:
        return {"ok": False, "error": "no_port", "message": "订阅信息缺少隧道端口"}
    # 私钥：服务端返回了就用它落盘（首次订阅/取回私钥时），否则用本地已有
    priv = str(info.get("private_key") or "")
    kp = _tunnel_key_path(uid)
    if priv and not os.path.exists(kp):
        _tunnel_save_key(uid, priv)
    if not os.path.exists(kp):
        return {"ok": False, "error": "no_key_local",
                "message": "本机还没有隧道私钥，请先执行「订阅」把私钥保存到本机"}
    kh = os.path.join(_TUNNEL_DIR, "known_hosts")
    cmd = ["ssh", "-N",
           "-R", "%d:127.0.0.1:%d" % (tport, int(panel_port)),
           "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=%s" % kh,
           "-o", "ExitOnForwardFailure=yes",
           "-o", "ServerAliveInterval=30",
           "-o", "ServerAliveCountMax=3",
           "-i", kp,
           "tun_%s@api.wanminguo.top" % uid]
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        if p.poll() is not None:
            return {"ok": False, "error": "tunnel_exit",
                    "message": "隧道进程启动后立即退出（端口被占用或账号认证失败），请重试"}
        _TUNNEL_PROC.update(proc=p, pid=p.pid, port=tport, started=time.time())
        with _TUNNEL_LOCK:
            _TUNNEL["auto"] = True
        return {"ok": True, "running": True, "pid": p.pid}
    except Exception as e:                                           # noqa: BLE001
        return {"ok": False, "error": "start_fail", "message": str(e)}


def tunnel_stop():
    """关闭公网隧道（只停隧道进程，面板与引擎不动）。"""
    proc = _TUNNEL_PROC.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:                                        # noqa: BLE001
                proc.kill()
        except Exception:                                            # noqa: BLE001
            pass
    _TUNNEL_PROC.update(proc=None, pid=0, port=0, started=0.0)
    with _TUNNEL_LOCK:
        _TUNNEL["auto"] = False
    return {"ok": True, "running": False}


def _tunnel_keepalive(panel_port=8787):
    """长驻保活：隧道掉线且用户开过（auto=True）→ 自动重连；关闭后不再拉起。"""
    while True:
        time.sleep(45)
        try:
            with _TUNNEL_LOCK:
                auto = bool(_TUNNEL.get("auto"))
            if not auto or _tunnel_is_alive():
                continue
            info = _tunnel_info_snapshot()
            if not (info.get("subscribed") or info.get("status") == "active"):
                continue
            r = tunnel_start(panel_port=panel_port)
            if not r.get("ok"):
                log("隧道重连失败：%s" % (r.get("message") or r.get("error") or ""))
        except Exception:                                            # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 开机自启动（★ 2026-10-05 用户口径：信号要长驻 + 开机自启动）
#   用 HKCU\...\Run 注册表项，指向当前 exe + --autostart（不开浏览器）。
#   只在 Windows 生效；卸载/关闭开关即删键。不依赖任务计划程序权限。
# ---------------------------------------------------------------------------
AUTOSTART_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "FinHubClient"


def _current_exe_path():
    """当前程序路径：打包后是 exe，源码跑是 python.exe + 脚本。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    return '"%s" "%s"' % (sys.executable, os.path.abspath(__file__))


def autostart_status():
    """是否已注册开机自启动。非 Windows 一律 False。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_RUN_KEY) as k:
            winreg.QueryValueEx(k, AUTOSTART_NAME)
        return True
    except Exception:                                              # noqa: BLE001
        return False


def autostart_set(on):
    """开启/关闭开机自启动。返回 (ok, msg)。"""
    if sys.platform != "win32":
        return False, "当前系统不支持开机自启动（仅 Windows）"
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_RUN_KEY,
                            0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  _current_exe_path() + " --autostart")
            else:
                try:
                    winreg.DeleteValue(k, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return True, ("已开启开机自启动" if on else "已关闭开机自启动")
    except Exception as e:                                         # noqa: BLE001
        return False, "设置开机自启动失败：%s" % e


# ---------------------------------------------------------------------------
# 出网隧道：本地 CONNECT 代理（只监听 127.0.0.1，只放白名单）
# ---------------------------------------------------------------------------
def _is_ip(host: str) -> bool:
    """是不是 IP 字面量（决定外层 TLS 用哪个名字做 SNI）。"""
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return ":" in host                      # 粗略认 IPv6


class _ProxyHandler(socketserver.StreamRequestHandler):
    timeout = 30

    def _read_request_head(self):
        """从**原始 socket** 逐字节读 CONNECT 头，返回 (首行, 头之后多出来的字节)。

        ★★ 2026-09-28 实测踩到的真 bug：原来用 `self.rfile.readline()` 读 CONNECT，
          而 `rfile` 是**带缓冲**的 —— 它会一次从 socket 多读一段，客户端若把
          "CONNECT 头 + 后续数据" 放在同一个 TCP 段里（流水线发送），
          后面的数据就被吞进 rfile 的缓冲区、**永远不会转发给上游**。
          自测（`.deploy/_test_proxy_framing.py` 的 B 场景）复现：
          假隧道一个字节都收不到，SDK 只看到 200 然后一直等。
          现在逐字节读原始 socket，并把多出来的字节原样转给上游，一个都不丢。

        逐字节读的开销可以忽略（CONNECT 头只有几十字节）。
        """
        buf = b""
        try:
            self.connection.settimeout(20)
            while b"\r\n\r\n" not in buf and len(buf) <= 8192:
                chunk = self.connection.recv(1)
                if not chunk:
                    break
                buf += chunk
        except OSError:
            pass
        head, _, rest = buf.partition(b"\r\n\r\n")
        first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
        return first, rest

    def handle(self):
        line, extra = self._read_request_head()
        if not line:
            return
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            self.wfile.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            self.wfile.flush()
            return
        hostport = parts[1]
        host = hostport.split(":")[0].lower()
        # ★ 第二轮审查 P2：只放 443（真正的目标是 TLS 端口；服务端隧道也是硬编码 443）。
        try:
            cport = int(hostport.rsplit(":", 1)[1]) if ":" in hostport else 443
        except ValueError:
            cport = 0
        if cport != 443:
            self.server.note("拒绝（只允许 443）: %s" % hostport)
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            self.wfile.flush()
            return
        allowed = any(host == d or host.endswith("." + d) for d in TUNNEL_ALLOW)
        if not allowed:
            self.server.note("拒绝（不在白名单）: %s" % hostport)
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            self.wfile.flush()
            return
        # ★ 第二轮审查 P1：并发上限（服务端隧道有 400 上限，客户端原来没有）
        srv = self.server
        with srv.conn_lock:
            if srv.conn_n >= srv.max_conn:
                self.server.note("拒绝（本地代理并发已达上限 %d）" % srv.max_conn)
                self.wfile.write(b"HTTP/1.1 503 Too Busy\r\n\r\n")
                self.wfile.flush()
                return
            srv.conn_n += 1
        try:
            self._relay(hostport, extra)
        finally:
            with srv.conn_lock:
                srv.conn_n -= 1

    def _open_tunnel(self, hostport):
        """建立"外层 TLS + CONNECT"这一段，返回可用的上游 socket。

        ★ 2026-09-28 自查加了重试（见 _relay）：实测发现外网链路上**偶发**会把这条
          到 8443 的连接掐掉（隧道侧连日志都没留下 —— 说明连接没到服务器），
          对下单来说就是"偶发失败"。所以把"建链"这一步做成可重试的独立步骤。
        """
        raw = socket.create_connection(self.server.upstream, timeout=15)
        try:
            up = self.server.ssl_ctx.wrap_socket(raw, server_hostname=self.server.tls_name)
        except (ssl.SSLError, OSError):
            try:
                raw.close()
            except OSError:
                pass
            raise
        try:
            up.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n"
                        % (hostport, hostport)).encode("ascii", "ignore"))
            head = b""
            up.settimeout(20)
            while b"\r\n\r\n" not in head:
                if len(head) > 8192:
                    raise OSError("隧道应答头过长")
                chunk = up.recv(1)      # 逐字节：后面紧接着就是内层 TLS 字节，不能多读
                if not chunk:
                    raise OSError("隧道提前关闭了连接")
                head += chunk
        except OSError:
            try:
                up.close()
            except OSError:
                pass
            raise
        status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 200" not in status:
            try:
                up.close()
            except OSError:
                pass
            raise PermissionError(status)     # 业务拒绝（白名单/端口）—— 不重试
        return up

    def _relay(self, hostport, extra=b""):
        # 把 CONNECT 转给平台隧道：**先建外层 TLS**（SNI=平台域名），
        # 再在这层加密通道里发 CONNECT 给真正的目标 —— 这样目标的 SNI 不会被 DPI 看到。
        # ★ 建链失败会重试（最多 3 次）：实测外网链路上偶发会被掐一下，
        #   而"偶发失败"在下单路径上就是真金白银的损失。
        up = None
        for attempt in (1, 2, 3):
            try:
                up = self._open_tunnel(hostport)
                break
            except PermissionError as e:
                self.server.note("隧道拒绝 %s：%s" % (hostport, e))
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                self.wfile.flush()
                return
            except OSError as e:
                self.server.note("建隧道失败（第 %d/3 次，%s）: %s: %s"
                                 % (attempt, self.server.upstream, type(e).__name__, e))
                if attempt < 3:
                    time.sleep(0.4 * attempt)
        if up is None:
            self.server.note("隧道三次都没建起来 —— 给 SDK 回 502")
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            self.wfile.flush()
            return

        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.wfile.flush()
        self.server.note("转发 %s（经外层 TLS 隧道 %s）" % (hostport, self.server.tls_name))
        # ★ 把"跟 CONNECT 一起发过来的"那批字节原样转给上游（流水线客户端不丢数据）
        if extra:
            try:
                up.sendall(extra)
            except OSError as e:
                self.server.note("转发流水线数据失败：%s" % e)
                try:
                    up.close()
                except OSError:
                    pass
                return
        # ★ 通道建好后两端都**取消短超时**（否则 keep-alive/WebSocket 空闲会被掐断）
        try:
            self.connection.settimeout(900)
            up.settimeout(900)
        except OSError:
            pass

        def pipe(a, b):
            try:
                while True:
                    data = a.recv(65536)
                    if not data:
                        break
                    b.sendall(data)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        t = threading.Thread(target=pipe, args=(self.connection, up), daemon=True)
        t.start()
        pipe(up, self.connection)


class TunnelProxy:
    """本地 CONNECT 代理：**只给 Polymarket SDK 用**（HTTPS_PROXY=http://127.0.0.1:<port>）。

    ⚠️ 平台 API（Api 类）**不走这里** —— 它有自己的 opener 显式禁用代理。
       否则平台请求会被塞进这个只支持 CONNECT 的代理，全部 405。
    """

    def __init__(self, upstream, port=8788, max_conn=64,
                 fallback_ips=TUNNEL_FALLBACK_IPS, tls_name=None):
        self.upstream = tuple(upstream)
        self.port = port
        self.srv = None
        self.events = []
        self.max_conn = max_conn
        self.conn_n = 0
        self.conn_lock = threading.Lock()
        self.fallback_ips = tuple(fallback_ips or ())
        self.resolved = None          # 真正拿去连的上游 (ip, port)
        # ★ 外层 TLS 用哪个名字做 SNI + 校验证书：域名就用它自己；传 IP 时用平台域名。
        host = str(self.upstream[0])
        self.tls_name = tls_name or (DEFAULT_TUNNEL_SNI if _is_ip(host) else host)
        self.ssl_ctx = ssl.create_default_context()   # 默认会校验证书，不能关

    def note(self, msg):
        self.events.append((time.time(), msg))
        del self.events[:-200]
        log("[隧道] " + msg)

    def resolve_upstream(self):
        """把上游解析成 IP；**域名解析失败时回退到备用 IP**。

        ★ 为什么需要：隧道域名可能还没配 DNS 记录（实测就是这样），
          而服务器上的隧道其实在跑。没有兜底的话，客户跑 `--live`
          会直接报 "Name or service not known"，看起来像客户端坏了。
        """
        host, port = self.upstream
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            if infos:
                ip = infos[0][4][0]
                self.resolved = (ip, port)
                self.note("上游 %s:%d 解析为 %s" % (host, port, ip))
                return self.resolved
        except OSError as e:
            self.note("上游 %s:%d 域名解析失败（%s）" % (host, port, e))
        for ip in self.fallback_ips:
            self.resolved = (ip, port)
            self.note("★ 回退到备用 IP %s:%d（域名没配 DNS；"
                      "等 DNS 记录补上后会自动用回域名）" % (ip, port))
            return self.resolved
        self.resolved = (host, port)      # 没有备用就原样试，让错误自然暴露
        return self.resolved

    def start(self):
        self.resolve_upstream()
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", self.port), _ProxyHandler)
        srv.daemon_threads = True
        srv.allow_reuse_address = True
        srv.upstream = self.resolved      # ★ 用解析后的地址，不再每次重新解析
        srv.tls_name = self.tls_name      # ★ 外层 TLS 的 SNI / 证书名
        srv.ssl_ctx = self.ssl_ctx
        srv.note = self.note
        srv.max_conn = self.max_conn          # 并发上限（第二轮审查 P1）
        srv.conn_n = 0
        srv.conn_lock = self.conn_lock
        self.srv = srv
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log("本地隧道代理已启动：http://127.0.0.1:%d → %s:%d（只放白名单域名，"
            "并发上限 %d；**平台 API 不走这里**）"
            % (self.port, self.upstream[0], self.upstream[1], self.max_conn))

    def stop(self):
        if self.srv:
            # ★ 2026-09-28 实测修：只 shutdown() 只停线程，**监听套接字不关**，
            #   于是同进程内再起一个代理会报 WinError 10048（地址占用）。
            #   生产上只起一次所以没暴露，但停止/重启路径必须是干净的。
            try:
                self.srv.shutdown()
            except Exception:                                    # noqa: BLE001
                pass
            try:
                self.srv.server_close()
            except Exception:                                    # noqa: BLE001
                pass
            self.srv = None


# ---------------------------------------------------------------------------
# 下单：纸面 / 实盘
# ---------------------------------------------------------------------------
class Trader:
    """把"信号"变成"订单"，并产出**回执**（平台据此按成交股数计费）。

    纸面：按自己的限价模拟成交（数量取基础份数 × 倍数），**但回执里成交量填 0**
          —— 平台只认真实成交（见下面 place() 的注释）。
    实盘：交给官方 SDK（py-clob-client）下 FAK 限价单，读真实回执。
    """

    def __init__(self, mode, base_shares, multiplier, cap, private_key=None,
                 funder=None, signature_type=1, proxy=None, token_id=None,
                 token_secret=None, shares_table=None, max_rounds=None,
                 direction="follow"):
        self.mode = mode
        self.base_shares = base_shares
        self.multiplier = multiplier
        self.cap = cap
        self.private_key = private_key
        self.funder = funder
        self.signature_type = signature_type
        self.proxy = proxy
        self.token_id = token_id
        self.token_secret = token_secret
        self.shares_table = list(shares_table) if shares_table else list(LADDER_SHARES_15)
        self.max_rounds = int(max_rounds or LADDER_MAX_ROUNDS)
        # ★ 2026-10-04（用户口径）：本地可调方向 —— follow=顺向跟单（信号 UP 买 UP），
        #   reverse=反向（信号 UP 买 DOWN，与服务器策略 low_rebound 同口径）
        self.direction = "reverse" if direction == "reverse" else "follow"
        self.client = None
        if mode == "live":
            self.client = self._make_client()

    def shares(self, round_=1):
        """本地档口份数：档口股数表[当前档] × 信号端倍数。
        档位表 / 最多档口来自本配置（cc.shares / cc.max_rounds），
        只由本地 ladder 状态决定，与服务器档位无关。"""
        return shares_for_round(round_, self.multiplier,
                                self.shares_table, self.max_rounds)

    def _make_client(self):
        ok, msg = _ensure_clob_deps()
        if not ok:
            raise SystemExit(
                "实盘模式需要 py-clob-client：%s\n"
                "（纸面模式不需要它：去掉 --live 即可跑通全链路）" % msg)
        try:
            from py_clob_client.client import ClobClient            # noqa: PLC0415
        except ImportError:
            raise SystemExit("py-clob-client 仍不可用：%s" % msg)
        host = "https://clob.polymarket.com"
        c = ClobClient(host, key=self.private_key, chain_id=137,
                       signature_type=self.signature_type, funder=self.funder) \
            if self.funder else ClobClient(host, key=self.private_key, chain_id=137)
        if self.proxy:
            # ★★ 2026-10-02：SDK 0.34.6 底层是模块级 httpx 单例（不再是 requests），
            #   必须替换该单例的代理 —— 绝不动 os.environ（会污染 urllib，
            #   把平台请求也塞进隧道 → 405）。隧道只放白名单域名，CLOB 走这里。
            try:
                import py_clob_client.http_helpers.helpers as _hh   # noqa: PLC0415
                import httpx as _httpx                               # noqa: PLC0415
                _hh._http_client = _httpx.Client(http2=True, proxy=self.proxy)
                self._proxy_note = "已给 SDK（httpx）设置代理 %s" % self.proxy
            except Exception as e:                                   # noqa: BLE001
                self._proxy_note = "设置 SDK 代理失败：%s: %s" % (type(e).__name__, e)
        c.set_api_creds(c.create_or_derive_api_creds())
        return c

    def balance_usdc(self):
        """查平台 USDC 余额（实盘用；需 L2 凭证）。返回 dict 或 None。"""
        if self.client is None:
            return None
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams  # noqa: PLC0415
            params = BalanceAllowanceParams(signature_type=self.signature_type)
            r = self.client.get_balance_allowance(params)
            if isinstance(r, dict):
                return {"usdc": r.get("balance"), "raw": r}
            return {"usdc": None, "raw": r}
        except Exception as e:                                       # noqa: BLE001
            return {"usdc": None, "error": "%s: %s" % (type(e).__name__, e)}

    def proxy_note(self):
        return getattr(self, "_proxy_note", "")

    def place(self, sig, round_=1):
        """返回回执 dict（signal_id/filled_shares/avg_price/status/order_id/raw）。
        ★ 2026-10-02（审查）：round_ 必须透传 —— 原实现 place() 内部
          self.shares() 恒取第 1 档份数，导致马丁阶梯升档后下单量不递增
          （15 档永远只下 1 档的量），策略完全失效。
        """
        # ★ 2026-09-28（第二轮审查 P0）：**不要**在字段缺失时崩掉整个进程。
        #   服务端理论上一定给 signal_id，但"客户端因为一条畸形信号整进程退出"
        #   是不可接受的失败模式（退出后再也不会重连）。
        if not isinstance(sig, dict) or not sig.get("signal_id"):
            return {"signal_id": "", "status": "error", "requested_shares": 0,
                    "filled_shares": 0, "raw": {"error": "信号缺少 signal_id，已跳过"}}
        side = str(sig.get("side") or "").upper()
        # ★ 2026-10-04（用户口径 v2，反/顺必须分开）：
        #   方向以"信号方向 leg"为准 —— 服务器 low_rebound 策略本身是 reverse（买反边/便宜边），
        #   服务端下发的 side/price/token_id 都是**买入边**。所以：
        #     follow = 买信号方向（贵边 ~0.7）：token 用 leg 侧，价用 leg_price（服务端新字段）
        #     reverse= 买信号反边（便宜边 ~0.3）：跟随服务器买入边 token_id / limit_price
        leg = str(sig.get("leg") or side).upper()
        if self.direction == "follow":
            token_id = (sig.get("token_up") or "") if leg == "UP" else (sig.get("token_down") or "")
            my_side = leg
            raw_limit = float(sig.get("leg_price") or 0) or float(sig.get("entry_price") or 0)
            raw_limit += float(sig.get("exec_delta") or 0.05)
        else:  # reverse → 跟随服务器买入边（=信号反边，便宜边）
            token_id = (sig.get("token_down") or "") if leg == "UP" else (sig.get("token_up") or "")
            my_side = "DOWN" if leg == "UP" else "UP"
            raw_limit = float(sig.get("limit_price") or 0)
        limit = min(raw_limit, self.cap)
        if limit <= 0:
            return {"signal_id": sig["signal_id"], "status": "skipped_no_price",
                    "requested_shares": self.shares(round_), "filled_shares": 0,
                    "raw": {"limit": raw_limit}}
        over = raw_limit > self.cap + 1e-9
        want = self.shares(round_)
        if self.mode == "dry":
            return {"signal_id": sig["signal_id"], "status": "dry_run",
                    "requested_shares": want, "filled_shares": 0,
                    "dry": True, "raw": {}}
        if self.mode == "paper":
            # ★ 2026-09-28 修（审查 P0-1）：纸面回执的 **filled_shares 必须是 0**。
            #   原实现填 want（10×倍数），而服务端照 filled 扣次 —— 默认模式
            #   每收到一个信号就真扣一次已付的额度（按成交股数）。纸面成交只是本机模拟，
            #   把"模拟成交多少"放在 paper_filled 里给面板看，不参与计费。
            return {"signal_id": sig["signal_id"], "status": "paper",
                    "requested_shares": want, "filled_shares": 0,
                    "dry": True, "paper_filled": want, "avg_price": limit,
                    "order_id": "PAPER-" + sig["signal_id"][-12:],
                    "my_side": my_side, "direction": self.direction,
                    "raw": {"note": "纸面模拟成交（本机），回执不计费",
                            "would_fill": want, "limit": limit, "over_cap": over,
                            "sig_side": side, "direction": self.direction}}
        # ---- 实盘 ----
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType   # noqa: PLC0415
            from py_clob_client.order_builder.constants import BUY      # noqa: PLC0415
            if not token_id:
                return {"signal_id": sig["signal_id"], "status": "error",
                        "requested_shares": want, "filled_shares": 0,
                        "raw": {"error": "信号里没有对侧 token（服务端未能解析 CLOB token，"
                                         "常见于市场刚创建/网络抖动）—— 已跳过，不扣次"}}
            args = OrderArgs(price=round(limit, 3), size=want, side=BUY, token_id=token_id)
            signed = self.client.create_order(args)
            resp = self.client.post_order(signed, OrderType.FAK)
            oid = str((resp or {}).get("orderID") or (resp or {}).get("orderId") or "")
            matched = float((resp or {}).get("makingAmount") or 0)
            taking = float((resp or {}).get("takingAmount") or 0)
            avg = (taking / matched) if (matched > 0 and taking > 0) else None
            filled = int(round(matched)) if matched > 0 else 0
            status = "matched" if filled > 0 else str((resp or {}).get("status") or "unmatched")
            return {"signal_id": sig["signal_id"], "status": status,
                    "requested_shares": want, "filled_shares": filled,
                    "avg_price": avg, "order_id": oid,
                    "my_side": my_side, "direction": self.direction,
                    "raw": {"resp": resp, "limit": limit, "raw_limit": raw_limit,
                            "over_cap": over, "token_id": token_id,
                            "sig_side": side, "direction": self.direction}}
        except Exception as e:                                   # noqa: BLE001
            return {"signal_id": sig["signal_id"], "status": "error",
                    "requested_shares": want, "filled_shares": 0,
                    "raw": {"error": "%s: %s" % (type(e).__name__, e),
                            "limit": limit, "over_cap": over}}


# ---------------------------------------------------------------------------
# 状态 + 本地面板
# ---------------------------------------------------------------------------
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.time()
        self.connected = False
        self.last_poll = 0.0
        self.last_error = ""
        self.balance = None
        self.market = ""
        self.mode = ""
        self.multiplier = 1
        self.base_shares = 10
        self.group_step = LADDER_GROUP_STEP
        self.max_rounds = LADDER_MAX_ROUNDS
        self.shares_table = list(LADDER_SHARES_15)
        self.direction = "reverse"
        self.group_now = 1
        self.round_now = 1
        self.ladder = {}
        self.signals = []          # 最近 100 条：信号 + 回执
        self.counts = {"signals": 0, "ordered": 0, "filled": 0, "skipped": 0,
                       "charged": 0, "errors": 0, "paper": 0, "resets": 0}
        self.log = []
        self.stop = False          # 面板「关闭」= 只停引擎（不下单），面板/端口保持

    def add(self, item):
        with self.lock:
            self.signals.append(item)
            del self.signals[:-100]

    def push_log(self, msg):
        with self.lock:
            self.log.append((time.strftime("%H:%M:%S"), msg))
            del self.log[:-200]


# ---------------------------------------------------------------------------
# 登录页（★ 2026-10-03 流程改造：先登录，登录成功后才进入主面板）
# ---------------------------------------------------------------------------
LOGIN_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>FinHub--Polymarket平台信号跟单客户端 · 登录</title>
<style>
 body{margin:0;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
      background:linear-gradient(160deg,#eaf1fa 0%,#f6f8fc 45%,#e9f2ee 100%);
      min-height:100vh;display:flex;align-items:center;justify-content:center;color:#131a2a}
 .card{background:#fff;border:1px solid #e3e9f2;border-radius:18px;
       box-shadow:0 18px 48px rgba(20,40,80,.10);padding:34px 38px;width:390px;max-width:92vw}
 .logo{font-size:20px;font-weight:750;margin:0 0 4px;color:#1b2a45}
 .sub{color:#6b7891;font-size:12.5px;margin:0 0 20px;line-height:1.65}
 .sub b{color:#3a4a63}
 label{display:block;font-size:12px;color:#6b7891;margin:12px 0 5px}
 input{width:100%;box-sizing:border-box;border:1px solid #d9e1ee;border-radius:9px;
       padding:9px 11px;font-size:13.5px;background:#fbfcfe;color:#131a2a;outline:none}
 input:focus{border-color:#0f9d58}
 button{width:100%;margin-top:20px;background:#0f9d58;border:0;border-radius:9px;color:#fff;
        font-size:14px;font-weight:650;padding:10px;cursor:pointer}
 button:hover{background:#0c8a4c} button:disabled{opacity:.55;cursor:default}
 .err{color:#d64545;font-size:12.5px;margin-top:10px;display:none;background:#fdeded;
      border-radius:8px;padding:7px 10px}
 .reg{text-align:center;font-size:12px;color:#6b7891;margin-top:16px}
 .reg a{color:#0f9d58;text-decoration:none}
 .quit{position:fixed;top:16px;right:18px;background:#fff;border:1px solid #d9e1ee;
       border-radius:9px;color:#3a4a63;font-size:12.5px;padding:7px 14px;cursor:pointer}
 .quit:hover{background:#fdeded;color:#d64545;border-color:#d64545}
</style></head><body>
<div class="card">
  <p class="logo">FinHub · Polymarket 信号跟单</p>
  <div class="sub">先登录平台账号，<b>登录成功后才进入客户端</b>。<br>
    一个 KEY 对应一个市场，分组 / 档口 / 倍数在本机计算，服务器只发信号。</div>
  <label>平台用户名</label>
  <input id="u" autocomplete="username" placeholder="api.wanminguo.top 注册的用户名">
  <label>登录口令</label>
  <input id="p" type="password" autocomplete="current-password" placeholder="登录口令">
  <div class="err" id="err">__ERR__</div>
  <button id="btn" type="button">登 录</button>
  <div class="reg">还没有账号？<a href="https://api.wanminguo.top/me/register.php" target="_blank">去平台注册</a></div>
</div>
<script>
(function(){
  var u=document.getElementById('u'),p=document.getElementById('p'),
      err=document.getElementById('err'),btn=document.getElementById('btn');
  function go(){
    if(!u.value.trim()||!p.value){err.style.display='block';err.textContent='请填写用户名和登录口令';return;}
    btn.disabled=true;btn.textContent='登录中…';
    fetch('api/auth_login',{method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:'username='+encodeURIComponent(u.value.trim())+'&password='+encodeURIComponent(p.value)})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d&&d.ok){location.href='/';}
      else{err.style.display='block';err.textContent=(d&&d.error)||'登录失败，请检查用户名或口令';
           btn.disabled=false;btn.textContent='登 录';}
    })
    .catch(function(){err.style.display='block';err.textContent='网络错误，无法连接平台服务器';
           btn.disabled=false;btn.textContent='登 录';});
  }
  btn.addEventListener('click',go);
  p.addEventListener('keydown',function(e){if(e.key==='Enter')go();});
  u.focus();
})();
</script>
</body></html>"""


PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>FinHub--Polymarket平台信号跟单客户端</title>
<style>
 body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
      background:#f6f8fc;color:#131a2a;margin:0;padding:22px}}
 .wrap{{max-width:1180px;margin:0 auto}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#6b7891;font-size:13px;margin-bottom:18px}}
 .toolbar{{display:flex;gap:10px;align-items:center;margin-bottom:16px;flex-wrap:wrap}}
 .kpis{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;
        margin-bottom:16px}}
 .kpi{{background:#fff;border:1px solid #e3e9f2;border-radius:12px;padding:11px 13px}}
 .kpi .k{{font-size:11.5px;color:#6b7891}} .kpi .v{{font-size:19px;font-weight:650}}
 .btn{{display:inline-block;background:#fff;border:1px solid #d9e1ee;border-radius:8px;
      color:#3a4a63;font-size:12.5px;padding:7px 14px;cursor:pointer;text-decoration:none}}
 .btn:active{{background:#eef3fa}}
 .btn-go{{background:#0f9d58;border-color:#0f9d58;color:#fff}}
 .btn-stop{{background:#d64545;border-color:#d64545;color:#fff}}
 .btn-new{{background:#eef3fa;border-color:#c9d6e8;color:#3a4a63}}
 .cfg{{background:#fff;border:1px solid #e3e9f2;border-radius:12px;padding:12px 16px;
      margin-bottom:12px}}
 .cfg summary{{cursor:pointer;font-size:15px;font-weight:650;color:#1b2a45;
      padding:2px 0;list-style:none}}
 .cfg summary::before{{content:"▸ ";color:#6b7891;font-size:12px}}
 .cfg[open] summary::before{{content:"▾ "}}
 .cfg summary .tag{{font-size:11.5px;font-weight:600;margin-left:8px}}
 .cfg h2{{font-size:15px;margin:0 0 10px}}
 .cfg-row{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:9px 0}}
 .cfg label{{font-size:12px;color:#6b7891;white-space:nowrap}}
 .cfg input,.cfg select{{border:1px solid #d9e1ee;border-radius:8px;padding:6px 9px;
      font-size:12.5px;background:#fbfcfe;color:#131a2a}}
 .cfg input{{font-family:ui-monospace,Consolas,monospace}}
 .cfg-actions{{display:flex;gap:10px;align-items:center;margin-top:4px;flex-wrap:wrap}}
 .cfg-actions form{{margin:0}}
 table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e9f2;
        border-radius:12px;overflow:hidden;font-size:12.5px}}
 th,td{{padding:7px 10px;text-align:left;border-bottom:1px solid #eef2f8}}
 th{{background:#fbfcfe;color:#6b7891;font-size:11.5px}}
 .up{{color:#0f9d58}} .dn{{color:#d64545}} .mono{{font-family:ui-monospace,Consolas,monospace}}
 .sec{{font-size:15px;font-weight:650;color:#1b2a45;margin:20px 0 8px}}
 .pill{{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600}}
 .ok{{background:#e9f7ef;color:#0f9d58}} .bad{{background:#fdeded;color:#d64545}}
 .warn{{background:#fdf5e6;color:#b7791f}} .mute{{background:#f2f5fa;color:#6b7891}}
 pre{{background:#fff;border:1px solid #e3e9f2;border-radius:12px;padding:10px;
      font-size:11.5px;line-height:1.7;max-height:220px;overflow:auto}}
</style></head><body><div class="wrap">
<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">
  <div>
    <h1>FinHub--Polymarket平台信号跟单客户端</h1>
    <div class="sub">本机面板 · 数据区每 3 秒自动刷新（表单编辑不受影响） · 数据不出你的机器
     · 分组/档口为<b>本地计算</b>，服务器只发信号 · 一个 KEY 对应一个市场
     <br><span style="color:#5a6a85;font-size:12px">{links}</span></div>
  </div>
  <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;margin:2px 0 0">
    <span style="background:#e9f7ef;border:1px solid #cdeed9;color:#0f9d58;border-radius:8px;
         font-size:12.5px;padding:6px 12px" title="账号用户池剩余额度（KEY 不单独设限，任一 KEY 下单都从账号总额度扣，1 股 = 1 额度）">
      账号剩余额度(股) <b style="font-size:14px">{ubal}</b></span>
    <form method="post" action="api/auth_logout" style="margin:0"
          onsubmit="return confirm('确认退出平台登录？退出后需重新登录才能操作客户端。');">
      <button type="submit" class="btn btn-new" style="font-size:12.5px">退出登录</button>
    </form>
    <button type="button" class="btn btn-stop" style="font-size:12.5px"
            onclick="quitApp()">退出程序</button>
  </div>
</div>
<script>
function quitApp(){{  /* ★ 2026-10-03 二次确认：退出的是整个客户端进程 */
  if (!confirm('确认退出整个客户端程序？退出后将不再接收平台信号。')) return;
  if (!confirm('再次确认：退出的是【整个客户端进程】（不是只停引擎）。\n一但退出，整个系统将关闭，需要重新双击 exe 才能启动。确定退出？')) return;
  var f = document.createElement('form');
  f.method = 'POST'; f.action = 'api/quit';
  document.body.appendChild(f); f.submit();
}}
</script>
<div id="auth-box">{auth}</div>
<div class="sec">额度用量（按天）</div>
<div id="usage-box">{usage}</div>
<div class="sec">远程查看（公网隧道）</div>
<div id="tunnel-box">{tunnel}</div>
{cards}
<form method="post" action="api/new_config" style="margin:0 0 6px">
  <button type="submit" class="btn btn-new">＋ 新增配置</button>
</form>
<div class="sec">连接池（按配置统计）</div>
<div id="pool-box">{pool}</div>
<div class="sec">市场统计</div>
<div id="stats-box">{stats}</div>
<div class="sec">最近信号 · 回执 · 本地下单台账（总盈亏）</div>
<div id="ledger-box">{ledger}</div>
<h2 style="font-size:15px;margin:18px 0 8px">本机日志</h2>
<pre id="logs-box">{log}</pre>
<script>
(function(){{
  function dyn(){{
    fetch('api/state', {{cache:'no-store'}}).then(function(r){{return r.json();}})
    .then(function(d){{
      if (!d || !d.ok) return;
      var el;
      for (var cid in (d.pills||{{}})) {{
        el = document.getElementById('tag-'+cid);
        if (el) el.innerHTML = d.pills[cid];
      }}
      el = document.getElementById('usage-box');  if (el && d.usage)  el.innerHTML = d.usage;
      el = document.getElementById('tunnel-box'); if (el && d.tunnel) el.innerHTML = d.tunnel;
      el = document.getElementById('pool-box');   if (el && d.pool)   el.innerHTML = d.pool;
      el = document.getElementById('stats-box');  if (el && d.stats)  el.innerHTML = d.stats;
      el = document.getElementById('ledger-box'); if (el && d.ledger) el.innerHTML = d.ledger;
      el = document.getElementById('logs-box');   if (el && d.logs)   el.innerHTML = d.logs;
      el = document.getElementById('auth-box');   if (el && d.auth)   el.innerHTML = d.auth;
      // ★ 2026-10-03 看门狗：引擎"运行中"但超过 5 分钟没有轮询 → 提示疑似卡死
      if (d.hb) {{
        var stale = d.hb.alive && (d.hb.now - d.hb.last_poll) > 300;
        var w = document.getElementById('hb-warn');
        if (!w) {{
          w = document.createElement('div');
          w.id = 'hb-warn';
          w.style.cssText = 'color:#b7791f;background:#fdf5e6;padding:8px 12px;' +
              'border-radius:10px;font-size:13px;margin:8px 0;display:none';
          var wrap = document.querySelector('.wrap');
          if (wrap) wrap.insertBefore(w, wrap.firstChild);
        }}
        if (stale) {{
          w.style.display = 'block';
          w.textContent = '⚠ 引擎显示运行中，但已 ' +
              Math.round((d.hb.now - d.hb.last_poll)/60) +
              ' 分钟无轮询（疑似网络卡死）。请点「关闭程序」再「启动此配置」恢复。';
        }} else {{
          w.style.display = 'none';
        }}
      }}
    }}).catch(function(){{}});
  }}
  setInterval(dyn, 3000);
  dyn();
}})();
</script>
</div></body></html>"""


_MARKETS = ["BTC-5m", "ETH-5m", "SOL-5m", "XRP-5m", "DOGE-5m", "BNB-5m", "HYPE-5m"]


def _configs_html():
    """多配置折叠卡片：configs[] 每个一张卡，可独立编辑/保存/启动/关闭。
    一个 KEY 对应一个市场；「启动此配置」= 保存为激活配置并启动引擎。"""
    cfg = load_config()
    active = cfg.get("active_config") or ""
    esc = html.escape

    def inp(k, ph, w, cc):
        v = esc(str(cc.get(k) or ""))
        return ('<input name="%s" value="%s" placeholder="%s" style="width:%sem">'
                % (k, v, esc(ph), w))

    def sel(k, opts, cc, ph="请选择"):
        cur = str(cc.get(k) or "")
        o = '<option value="">%s</option>' % esc(ph)
        for opt in opts:
            o += '<option value="%s"%s>%s</option>' % (
                esc(opt), " selected" if cur == opt else "", esc(opt))
        return '<select name="%s">%s</select>' % (k, o)

    def strat_sel(cc):
        strat = str(cc.get("strategy") or "")
        if strat:
            return ('<select name="strategy">'
                    '<option value="%s" selected>%s</option>'
                    '<option value="">默认（低位回中·马丁阶梯）</option></select>'
                    % (esc(strat), esc(strat)))
        return ('<select name="strategy">'
                '<option value="" selected>默认（低位回中·马丁阶梯）</option></select>')

    def modes_sel(cc):
        o = ""
        for v, t in (("paper", "模拟盘"), ("live", "实盘")):
            s = " selected" if str(cc.get("mode") or "paper") == v else ""
            o += '<option value="%s"%s>%s</option>' % (v, s, t)
        return '<select name="mode">%s</select>' % o

    def muls_sel(cc):
        o = ""
        for v in LADDER_MULT_OPTIONS:
            s = " selected" if str(cc.get("multiplier") or 1) == str(v) else ""
            o += '<option value="%s"%s>×%s</option>' % (v, s, v)
        return '<select name="multiplier">%s</select>' % o

    def dirs_sel(cc):
        """方向：follow=顺向跟单（信号 UP 买 UP） / reverse=反向（信号 UP 买 DOWN）。
        默认反向（与服务器策略 low_rebound 的 direction=reverse 同口径）。"""
        cur = str(cc.get("direction") or "reverse")
        o = ""
        for v, t in (("reverse", "反向（信号涨我买跌）"), ("follow", "顺向（信号涨我买涨）")):
            s = " selected" if cur == v else ""
            o += '<option value="%s"%s>%s</option>' % (v, s, t)
        return '<select name="direction">%s</select>' % o

    def ladder_inputs(cc):
        """分组步长 / 最多档口 / 档口股数：档位表仅当 cc.shares 是 list 才显示自定义值；
        int（旧版基础份数遗留）一律显示默认 15 级表。"""
        if isinstance(cc.get("shares"), list):
            shares_html = inp("shares", "1,3,6,…", 46, cc)
        else:
            shares_html = ('<input name="shares" value="%s" style="width:46em">'
                           % ",".join(str(x) for x in LADDER_SHARES_15))
        gs = cc.get("group_step")
        gs_html = inp("group_step", "12（每N个信号一轮回）", 4, cc) if gs else (
            '<input name="group_step" value="%d" style="width:4em">' % LADDER_GROUP_STEP)
        mr = cc.get("max_rounds")
        mr_html = inp("max_rounds", "15", 3, cc) if mr else (
            '<input name="max_rounds" value="%d" style="width:3em">' % LADDER_MAX_ROUNDS)
        return gs_html, mr_html, shares_html

    configs = cfg.get("configs") or []
    if not configs:
        configs = [{"id": "c1", "name": "配置1", "enabled": True}]
    cards = []
    for i, cc in enumerate(configs):
        cid = cc.get("id") or ("c%d" % (i + 1))
        is_active = (cid == active)
        is_running = _engine_alive(cid)
        open_attr = " open" if is_active else ""
        tag = ('<span id="tag-%s" class="pill ok">运行中</span>' % html.escape(cid)
               if is_running else
               '<span id="tag-%s" class="pill mute">未运行</span>' % html.escape(cid))
        title = "%s（%s · %s · ×%s）" % (
            esc(cc.get("name") or cid),
            esc(market_from_key(cc.get("api_key")) or cc.get("market") or "待识别"),
            "实盘" if cc.get("mode") == "live" else "模拟盘",
            cc.get("multiplier") or 1)
        # 启动/关闭切换按钮（一个位置）：未运行显示「启动此配置」，运行中显示「关闭程序」
        if is_running:
            start_btn_html = (
                '<form method="post" action="api/stop" style="margin:0" '
                'onsubmit="return confirm(\'确认关闭？关闭后不再接收信号/下单，面板保持开启。\');">'
                '<input type="hidden" name="cid" value="%s">'
                '<button type="submit" class="btn btn-stop">关闭程序（只停引擎）</button></form>'
                % html.escape(cid))
        else:
            start_btn_html = (
                '<form method="post" action="api/start" style="margin:0">'
                '<input type="hidden" name="cid" value="%s">'
                '<button type="submit" class="btn btn-go">启动此配置</button></form>'
                % html.escape(cid))
        cards.append(
            '<details class="cfg"%s><summary>%s %s</summary>'
            '<form method="post" action="api/save_config">'
            '<input type="hidden" name="cid" value="%s">'
            '<div class="cfg-row">'
            '<label>配置名</label>%s'
            '<label>信号 API KEY</label>%s'
            '<label>模式</label>%s'
            '<label>倍数</label>%s'
            '<label>方向</label>%s'
            '</div>'
            '<div class="cfg-row">'
            '<label>分组步长</label>%s'
            '<label>最多档口</label>%s'
            '<label>档口股数（逗号分隔）</label>%s'
            '</div>'
            '<div class="cfg-row">'
            '<label>钱包私钥</label>%s'
            '<label>钱包地址</label>%s'
            '<label>交易 API</label>%s'
            '</div>'
            '<div class="cfg-row">'
            '<label>API Token ID</label>%s'
            '<label>API Token Secret</label>%s'
            '</div>'
            '<div class="cfg-actions">'
            '<button type="submit" class="btn btn-go">保存并应用</button>'
            '<span style="font-size:12px;color:#6b7891">KEY 在平台'
            '<a href="https://api.wanminguo.top/quant/me/" target="_blank">用户中心</a>'
            '订阅/领取，额度不足请充值</span>'
            '</div>'
            '</form>'
            '<div class="cfg-actions">'
            '%s'
            '<form method="post" action="api/reset_ladder" style="margin:0" '
            'onsubmit="return confirm(\'确认重置本配置的分组/档口并清空其下单台账？\');">'
            '<input type="hidden" name="cid" value="%s">'
            '<button type="submit" class="btn">重置分组/档口</button></form>'
            '<form method="post" action="api/delete_config" style="margin:0" '
            'onsubmit="return confirm(\'确认删除此配置？将一并清空该配置的下单台账（平台 KEY 与额度不受影响）。\');">'
            '<input type="hidden" name="cid" value="%s">'
            '<button type="submit" class="btn btn-stop">删除配置</button></form>'
            '</div>'
            '</details>'
            % (open_attr, title, tag, cid,
               inp("name", "配置1", 9, cc), inp("api_key", "pm_live_…", 32, cc),
               modes_sel(cc), muls_sel(cc), dirs_sel(cc),
               ladder_inputs(cc)[0], ladder_inputs(cc)[1], ladder_inputs(cc)[2],
               inp("private_key", "0x…（实盘用，只在本机）", 24, cc),
               inp("funder", "0x…（查余额用）", 18, cc),
               inp("pm_api", "（可选）", 10, cc),
               inp("token_id", "（钱包 API Token ID）", 20, cc),
               inp("token_secret", "（钱包 API Token Secret）", 22, cc),
               start_btn_html, html.escape(cid), cid))
    return "".join(cards)


_PM_BAL_CACHE = {}   # cid -> (ts, text)　★ 2026-10-05 实盘真实平台余额（60s 缓存，防 CLOB 限流）


def _pm_bal_txt(cid, cc, e):
    """连接池『PM 余额』单元格：
    实盘配置且引擎运行 → 查 Polymarket 钱包真实 USDC 余额（60s 缓存）；
    模拟盘 / 未运行 → “—”（用户口径：模拟盘不显示平台余额）。"""
    if (cc.get("mode") or "paper") != "live":
        return "—"
    if not (e and _engine_alive(cid)):
        return "—"
    now = time.time()
    hit = _PM_BAL_CACHE.get(cid)
    if hit and now - hit[0] < 60:
        return hit[1]
    trader = e.get("trader")
    if trader is None:
        return "—"
    try:
        r = trader.balance_usdc()
        v = (r or {}).get("usdc") if isinstance(r, dict) else None
        txt = ("<b>%.2f</b> U" % float(v)) if v is not None else "—"
    except Exception:                                      # noqa: BLE001
        txt = "—"
    _PM_BAL_CACHE[cid] = (now, txt)
    return txt


def render_dyn(st):
    """动态数据区（连接池/市场统计/台账/日志/配置状态徽标）。
    整页 render() 与 /api/state（3 秒局部刷新）共用同一份生成逻辑。"""
    with st.lock:
        c = dict(st.counts)
        logs = list(st.log)[::-1][:60]
        bal = st.balance
        conn = st.connected
        mode = st.mode
        mult = st.multiplier
        mk = st.market
        err = st.last_error
        grp = st.group_now
        rnd = st.round_now
    ledger = ledger_load()
    ledger_rev = list(reversed(ledger))[:100]
    cfg = load_config()
    configs = cfg.get("configs") or []

    # ---- 配置状态徽标（每张卡片 summary 右侧；JS 按 id=tag-<cid> 局部更新）----
    pills = {}
    for cc in configs:
        cid = cc.get("id") or "?"
        running = _engine_alive(cid)
        pills[cid] = ('<span id="tag-%s" class="pill %s">%s</span>'
                      % (html.escape(cid), "ok" if running else "mute",
                         "运行中" if running else "未运行"))

    # ---- 连接池表格：一行一配置（★ 2026-10-04 用户口径：删「账号剩余额度(股)」列，
    #      额度统一看顶栏 + 额度用量（按天），连接池只做配置运行/盈亏统计）----
    by_cid = ledger_stats(ledger, "cid")
    pool_rows = []
    for cc in configs:
        cid = cc.get("id") or "?"
        running = _engine_alive(cid)
        stt = "运行中" if running else "已停止"
        s_cls = "ok" if running else "mute"
        a = by_cid.get(cid) or {"orders": 0, "filled": 0, "pnl": 0.0}
        pnl = a["pnl"]
        pnl_txt = ('<span class="%s">%+.2f U</span>' % ("up" if pnl >= 0 else "dn", pnl)
                   if a["orders"] else "—")
        e = _ENGINES.get(cid)
        eng_state = e.get("state") if e and _engine_alive(cid) else None
        run_mk = market_from_key(cc.get("api_key")) or cc.get("market") or "—"
        if eng_state is not None:
            run_mk = (eng_state.market or cc.get("market") or "—")
        pm_bal = _pm_bal_txt(cid, cc, e)
        pool_rows.append(
            "<tr><td class='mono'>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>×%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td><span class='pill %s'>%s</span></td></tr>"
            % (html.escape(cc.get("name") or cid), html.escape(run_mk),
               "实盘" if cc.get("mode") == "live" else "模拟盘",
               pm_bal,
               cc.get("multiplier") or 1,
               a["orders"], a["filled"], pnl_txt, s_cls, stt))
    if not pool_rows:
        pool_rows = ["<tr><td colspan='10' style='color:#6b7891'>还没有配置 —— "
                     "点『＋ 新增配置』创建</td></tr>"]
    pool = ("<table><tr><th>配置</th><th>运行市场</th><th>模式</th>"
            "<th>PM 余额</th><th>份数倍数</th><th>下单次数</th><th>成交(股)</th>"
            "<th>盈亏</th><th>状态</th></tr>"
            + "".join(pool_rows) + "</table>")

    # ---- 市场统计卡片（区分市场）----
    by_mk = ledger_stats(ledger, "market")
    stat_cards = []
    for mk2, a in sorted(by_mk.items()):
        wr = ("%.1f%%" % a["winrate"]) if a["winrate"] is not None else "—"
        pnl = a["pnl"]
        pnl_c = ("<span class='up'>%+.2f U</span>" % pnl
                 if pnl >= 0 else "<span class='dn'>%+.2f U</span>" % pnl)
        stat_cards.append(
            '<div class="kpi"><div class="k">%s</div><div class="v">%s 单 · 胜率 %s<br>'
            '<span style="font-size:14px;font-weight:600">%s</span></div></div>'
            % (html.escape(mk2), a["orders"], wr, pnl_c))
    stats = ("<div class='kpis'>" + "".join(stat_cards) + "</div>"
             if stat_cards else '<p style="color:#6b7891;font-size:13px">暂无下单记录</p>')

    # ---- 最近信号 · 回执 · 本地下单台账（大表）----
    if not ledger_rev:
        lrows = ('<p style="color:#6b7891;font-size:13px">还没有下单台账。'
                 '收到信号并下单后会记录在这里，窗口结算后补盈亏。</p>')
    else:
        body = []
        for r in ledger_rev:
            sc = "up" if str(r.get("side") or "").upper() == "UP" else "dn"
            stt = str(r.get("status") or "-")
            is_paper = (stt == "paper")
            rc = "ok" if stt in ("matched", "paper") else (
                "mute" if stt in ("live", "unmatched", "skipped_over_cap",
                                  "skipped_no_price", "dry_run", "skipped_thin") else "bad")
            filled_txt = ("%s(模拟)" % r.get("filled")
                          if is_paper else (r.get("filled") if r.get("filled") else 0))
            pnl = r.get("pnl")
            if pnl is None:
                pnl_txt = '<span style="color:#9aa6bd">待结算</span>'
            elif pnl >= 0:
                pnl_txt = '<span class="up">%+.2f U</span>' % pnl
            else:
                pnl_txt = '<span class="dn">%+.2f U</span>' % pnl
            body.append(
                "<tr><td class='mono'>%s</td><td>%s</td><td>%s</td><td class='%s'>%s</td>"
                "<td class='mono'>%s</td><td class='mono'>%s</td><td>%s</td>"
                "<td class='mono'>%s</td><td>%s</td>"
                "<td><span class='pill %s'>%s</span></td><td class='mono'>%s</td><td>%s</td>"
                "<td class='mono'>%s</td></tr>"
                % (html.escape(r.get("t") or ""), html.escape(r.get("name") or "?"),
                   html.escape(r.get("market") or "?"), sc, r.get("side"),
                   r.get("want") or 0, r.get("filled") and filled_txt,
                   "第%s档" % (r.get("round") or "-"),
                   ("%.4f" % r["avg"]) if isinstance(r.get("avg"), (int, float)) else "-",
                   r.get("charged", "-"), rc, stt,
                   r.get("signal_id", "")[-10:], pnl_txt,
                   html.escape(r.get("slug") or (r.get("signal_id", "") or "").split(":")[0])))
        lrows = ("<table><tr><th>时间</th><th>配置</th><th>市场</th><th>方向</th>"
                 "<th>份数(档口×倍数)</th><th>成交(股)</th><th>档口</th><th>均价</th>"
                 "<th>扣次</th><th>状态</th><th>信号</th><th>盈亏</th>"
                 "<th>备注(窗口)</th></tr>"
                 + "".join(body) + "</table>")
    if err:
        lrows += '<p style="color:#d64545;font-size:12.5px">最近错误：%s</p>' % err
    logtxt = "\n".join("%s  %s" % (t, m) for t, m in logs) or "（空）"

    # ---- 用户登录区块（★ 2026-10-03 新增）----
    with _AUTH_LOCK:
        au = dict(_AUTH)
    user = au.get("user")
    if user:
        # ★ 2026-10-03 revoked（已吊销）的 KEY 不显示
        keys = [k for k in (au.get("keys") or [])
                if str(k.get("status") or "") != "revoked"]
        # ★ 2026-10-04（用户口径）：额度 = 账号级用户池（KEY 不单独设限）。
        #   显示服务端返回的 pool（sig_user_pool），不再按 KEY 求和，
        #   避免出现"KEY 有额度"的误解。
        pool_ = au.get("pool") or {}
        bal_sum = int(pool_.get("balance") or 0)
        used_sum = int(pool_.get("total_used") or 0)
        if keys:
            krows = []
            for k in keys:
                kst = str(k.get("status") or "")
                sc = "ok" if kst == "active" else ("bad" if kst == "revoked" else "mute")
                # ★ 2026-10-03 前缀都相同 → 显示后缀（服务端 key_suffix 末12位；旧数据回退前缀末8位）
                ksuf = (k.get("key_suffix") or "").strip()
                if not ksuf:
                    kp = (k.get("key_prefix") or "").strip()
                    ksuf = kp[-8:] if len(kp) > 8 else kp
                # ★ 2026-10-03 复制完整 KEY（服务端 key_plain；旧数据回退前缀+secret 拼）
                kplain = (k.get("key_plain") or "").strip()
                if not kplain:
                    _ks = (k.get("key_secret") or "").strip()
                    kplain = _ks if _ks else (k.get("key_prefix") or "")
                krows.append(
                    "<tr><td>%s</td><td class='mono'>%s</td>"
                    "<td><button type='button' class='btn' style='padding:3px 10px;font-size:11.5px' "
                    "onclick=\"var b=this;navigator.clipboard.writeText('%s').then(function(){"
                    "b.textContent='已复制';setTimeout(function(){b.textContent='复制';},1200);}"
                    ").catch(function(){alert('复制失败，请手动复制完整 KEY');});\">复制</button></td>"
                    "<td class='mono'>%s</td>"
                    "<td><span class='pill %s'>%s</span></td>"
                    "<td class='mono'>%s</td></tr>"
                    % (html.escape(k.get("market") or "—"),
                       html.escape(ksuf),
                       html.escape(kplain, quote=True),
                       html.escape(str(k.get("created_at") or "")[:10]),
                       sc, kst, html.escape(k.get("label") or "")))
            ktab = ("<table><tr><th>市场</th><th>KEY 后缀</th><th>复制 KEY</th>"
                    "<th>创建日期</th><th>状态</th><th>备注</th></tr>"
                    + "".join(krows) + "</table>")
        else:
            ktab = ('<p style="color:#6b7891;font-size:12.5px">名下还没有 API KEY —— '
                    '到平台<a href="https://api.wanminguo.top/quant/me/" target="_blank">'
                    '用户中心</a>订阅领取</p>')
        auth_html = (
            '<details class="cfg" open><summary>用户登录 · <b>%s</b>'
            '<span class="tag pill ok">已登录</span></summary>'
            '<div class="cfg-row">'
            '<label>用户名</label><span>%s</span>'
            '<label>昵称</label><span>%s</span>'
            '<label>邮箱</label><span>%s</span>'
            '<label>KEY 数</label><span>%d</span>'
            '<label>账号额度（用户池）</label><span class="mono">余额 %d 股 · 累计已用 %d 股</span>'
            '</div>'
            '<div style="margin:6px 0">%s</div>'
            '<div class="cfg-actions">'
            '<form method="post" action="api/auth_refresh" style="margin:0">'
            '<button type="submit" class="btn">刷新</button></form>'
            '<form method="post" action="api/auth_logout" style="margin:0" '
            'onsubmit="return confirm(\'确认退出平台登录？\');">'
            '<button type="submit" class="btn btn-stop">退出登录</button></form>'
            '<span style="font-size:12px;color:#6b7891">开机自启动：'
            '<button type="button" class="btn %s" style="padding:3px 10px;font-size:11.5px" '
            'onclick="var b=this;fetch(\'api/autostart?on=%s\',{method:\'POST\'}).then(function(r){'
            'return r.json();}).then(function(d){alert(d.message||\'完成\');location.reload();})'
            '.catch(function(){alert(\'网络错误\');});">%s</button></span>'
            '</div></details>'
            % (html.escape(user.get("username") or "?"),
               html.escape(user.get("username") or "?"),
               html.escape(user.get("nickname") or "—"),
               html.escape(user.get("email") or "—"),
               len(keys), bal_sum, used_sum, ktab,
               "ok" if autostart_status() else "mute",
               "0" if autostart_status() else "1",
               "已自启动（点击关闭）" if autostart_status() else "未自启动（点击开启）"))
    else:
        err_txt = ('<p style="color:#d64545;font-size:12.5px">%s</p>' % html.escape(au["error"])
                   if au.get("error") else "")
        auth_html = (
            '<details class="cfg" open><summary>用户登录'
            '<span class="tag pill mute">未登录</span></summary>'
            '<form method="post" action="api/auth_login">'
            '<div class="cfg-row">'
            '<label>用户名</label>'
            '<input name="username" placeholder="平台用户名" style="width:14em" required>'
            '<label>口令</label>'
            '<input type="password" name="password" placeholder="登录口令" '
            'style="width:14em" required>'
            '<button type="submit" class="btn btn-go">登录</button>'
            '</div>'
            '<div class="cfg-row"><span style="font-size:12px;color:#6b7891">'
            '登录后可在本面板查看名下 KEY、余额与每日额度用量。还没有账号？'
            '<a href="https://api.wanminguo.top/me/register.php" target="_blank">'
            '去平台注册</a></span></div>'
            '</form>%s</details>' % err_txt)

    # ---- 每日额度用量（★ 2026-10-03 新增）：柱状图 + 明细 ----
    usage = au.get("usage")
    if user and usage:
        days = usage.get("days") or []
        s = usage.get("sum") or {}
        recent = list(reversed(days[-14:]))          # 倒序 → 正序（近 14 天）
        mx = max([int(d.get("used") or 0) for d in recent] + [1])
        bars = []
        for d in recent:
            used = int(d.get("used") or 0)
            h = max(2, int(120 * used / mx))
            bars.append(
                '<div style="display:flex;flex-direction:column;align-items:center;'
                'justify-content:flex-end;flex:1;min-width:34px;height:140px">'
                '<div style="font-size:10px;color:#3a4a63">%s</div>'
                '<div style="width:26px;height:%dpx;background:%s;border-radius:5px 5px 0 0"></div>'
                '<div style="font-size:10px;color:#6b7891;margin-top:3px">%s</div></div>'
                % (used if used else "", h,
                   "#0f9d58" if used >= 0 else "#d64545",
                   str(d.get("day") or "")[5:]))
        bar_html = ('<div style="display:flex;gap:4px;align-items:flex-end;'
                    'background:#fff;border:1px solid #e3e9f2;border-radius:10px;'
                    'padding:10px 8px;margin:8px 0;overflow-x:auto">' + "".join(bars)
                    + "</div>")
        if days:
            drows = []
            for d in days[:30]:
                used = int(d.get("used") or 0)
                refd = int(d.get("refund") or 0)
                drows.append(
                    "<tr><td class='mono'>%s</td><td class='mono'>%s</td>"
                    "<td class='mono'>%s</td></tr>"
                    % (html.escape(str(d.get("day") or "")), used,
                       refd if refd else "—"))
            dtab = ("<table><tr><th>日期</th><th>使用额度(股)</th><th>退回(股)</th></tr>"
                    + "".join(drows) + "</table>")
        else:
            dtab = '<p style="color:#6b7891;font-size:12.5px">暂无额度消耗记录（实盘成交后回执扣股才会记入）</p>'
        usage_html = (
            '<details class="cfg" open><summary>额度用量（按天）'
            '<span class="tag pill ok">%d 天</span></summary>'
            '<div class="cfg-row">'
            '<label>额度合计</label><span class="mono">余额 %d 股</span>'
            '<label>累计消耗</label><span class="mono">%d 股</span>'
            '<label>区间已用</label><span class="mono">%d 股</span>'
            '<label>区间退回</label><span class="mono">%d 股</span>'
            '</div>%s%s</details>'
            % (len(days), int(s.get("balance_sum") or 0),
               int(s.get("total_used_sum") or 0),
               int(s.get("used_sum") or 0), int(s.get("refund_sum") or 0),
               bar_html, dtab))
    else:
        usage_html = (
            '<div class="cfg"><p style="color:#6b7891;font-size:12.5px;margin:4px 0">'
            '登录平台账号后显示每日额度用量（1 股 = 1 额度，实盘成交回执扣股）。'
            '</p></div>')

    # ---- 远程查看（公网隧道）（★ 2026-10-05 新增：每用户一条 + 订阅权限）----
    tunnel_html = ""
    if user:
        tun = tunnel_refresh()
        with _TUNNEL_LOCK:
            tun_err = _TUNNEL.get("error") or ""
            tun_uid = _TUNNEL.get("uid")
        sub = bool(tun.get("subscribed"))
        bal = tun.get("balance_usdt")
        price = tun.get("price_usdt")
        tun_url = (tun.get("url") or "")
        exp = (tun.get("expires_at") or "")[:19]
        running = _tunnel_is_alive()
        has_key = bool(tun_uid and os.path.exists(_tunnel_key_path(tun_uid)))
        if not sub:
            bal_txt = ("%.2f USDT" % float(bal)) if isinstance(bal, (int, float)) else "—"
            price_txt = ("%.2f USDT / 30 天" % float(price)) if isinstance(price, (int, float)) else "—"
            tunnel_html = (
                '<details class="cfg"><summary>远程查看（公网隧道）'
                '<span class="tag pill mute">未订阅</span></summary>'
                '<div class="cfg-row">'
                '<label>服务</label><span>公网远程查看本地面板 · 每用户独立地址</span>'
                '<label>价格</label><span class="mono">%s</span>'
                '<label>平台余额</label><span class="mono">%s</span>'
                '</div>'
                '<div class="cfg-actions">'
                '<form method="post" action="api/tunnel_subscribe" style="margin:0" '
                'onsubmit="return confirm(\'订阅将从平台余额扣款，确认订阅公网远程查看 30 天？\');">'
                '<button type="submit" class="btn btn-go">订阅（扣平台余额）</button></form>'
                '<span style="font-size:12px;color:#6b7891">订阅后用手机访问专属公网地址，'
                '可查看本地面板并启动/关闭配置；余额不足请到平台充值。</span>'
                '</div>'
                '%s</details>'
                % (price_txt, bal_txt,
                   ('<p style="color:#d64545;font-size:12.5px">%s</p>' % html.escape(tun_err)
                    if tun_err else "")))
        else:
            uname = ""
            with _AUTH_LOCK:
                _u = _AUTH.get("user") or {}
                uname = _u.get("username") or ""
            run_pill = ('<span class="pill ok">运行中</span>' if running
                        else '<span class="pill mute">已停止</span>')
            key_txt = ("本机已保存" if has_key else "未保存（订阅/取回时自动保存）")
            url_txt = html.escape(tun_url) if tun_url else "—"
            tunnel_html = (
                '<details class="cfg"><summary>远程查看（公网隧道）'
                '<span class="tag pill %s">%s</span></summary>'
                '<div class="cfg-row">'
                '<label>公网地址</label><span class="mono">%s</span>'
                '<button type="button" class="btn" style="padding:3px 10px;font-size:11.5px" '
                'onclick="var b=this;navigator.clipboard.writeText(\'%s\').then(function(){'
                'b.textContent=\'已复制\';setTimeout(function(){b.textContent=\'复制\';},1200);'
                '}).catch(function(){alert(\'复制失败，请手动复制地址\');});">复制</button>'
                '<label>到期时间</label><span class="mono">%s</span>'
                '<label>隧道状态</label>%s'
                '<label>私钥</label><span>%s</span>'
                '</div>'
                '<div class="cfg-actions">'
                '<form method="post" action="api/tunnel_start" style="margin:0">'
                '<button type="submit" class="btn btn-go">开启隧道</button></form>'
                '<form method="post" action="api/tunnel_stop" style="margin:0" '
                'onsubmit="return confirm(\'确认关闭公网隧道？关闭后手机将无法访问本地面板。\');">'
                '<button type="submit" class="btn btn-stop">关闭隧道</button></form>'
                '<span style="font-size:12px;color:#6b7891">开启后手机访问 %s 可看面板并启动/关闭配置；'
                '客户端开着时隧道自动保活重连。</span>'
                '</div>'
                '%s</details>'
                % ("ok" if running else "mute", "运行中" if running else "已停止",
                   url_txt, html.escape(tun_url, quote=True),
                   html.escape(exp) if exp else "—", run_pill, key_txt,
                   ("https://api.wanminguo.top/tunnel/%s/" % html.escape(uname)
                    if uname else html.escape(tun_url or "")),
                   ('<p style="color:#d64545;font-size:12.5px">%s</p>' % html.escape(tun_err)
                    if tun_err else "")))

    return {"ok": True, "pills": pills, "pool": pool, "stats": stats,
            "ledger": lrows, "logs": logtxt, "auth": auth_html,
            "usage": usage_html, "tunnel": tunnel_html,
            # ★ 2026-10-03 看门狗：引擎线程即使活着也可能卡死在网络调用上，
            #   面板据此判断"运行中但长时间无轮询"并提示重启。
            "hb": {"now": time.time(), "last_poll": st.last_poll,
                   "alive": bool(_ENGINES),
                   "started": st.started}}


def render(st):
    # ★ 2026-10-03 流程改造：未登录只显示独立登录页；登录成功后才进入主面板
    with _AUTH_LOCK:
        if not _AUTH.get("user"):
            err = html.escape(_AUTH.get("error") or "")
            return LOGIN_PAGE.replace("__ERR__", err)
        au = dict(_AUTH)
    ubal = sum(int(k.get("balance") or 0) for k in (au.get("keys") or []))
    d = render_dyn(st)
    return PAGE.format(cards=_configs_html(), pool=d["pool"], stats=d["stats"],
                       ledger=d["ledger"], log=d["logs"], auth=d["auth"],
                       usage=d["usage"], tunnel=d["tunnel"],
                       ubal=ubal, links=_panel_links_html())


class Dash(BaseHTTPRequestHandler):
    state = None

    def _host_ok(self):
        """★ 2026-09-28（第二轮审查 P1）：校验 Host，挡住 DNS rebinding。

        面板默认只绑 127.0.0.1，但浏览器里的恶意网页可以把某个域名解析到
        127.0.0.1 再打这个端口 —— 没有 Host 校验时对方就能读走 /api/state
        （里面有 token_id、order_id、CLOB 原始响应）。只认本机名字。

        ★ 2026-10-05（用户口径：手机公网/局域网查看面板）：
        · bind=0.0.0.0 时，允许本机局域网 IP 作 Host（同一 WiFi 手机直连）；
        · 服务器 Nginx 反代 /local/ 时，Host 是 api.wanminguo.top —— 放行自己域名。
          反代路径只在本机配置（见 finhub_tunnel.bat 说明），域名白名单不变相放开任意 Host。
        """
        host = str(self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1] if self.server else 0
        allow = {"127.0.0.1:%d" % port, "localhost:%d" % port,
                 "127.0.0.1", "localhost", "[::1]:%d" % port, "[::1]"}
        allow |= {"api.wanminguo.top", "api.wanminguo.top:80",
                  "api.wanminguo.top:443", "www.api.wanminguo.top",
                  "www.api.wanminguo.top:443"}
        try:
            _ip = _lan_ip()
            if _ip:
                allow |= {_ip, "%s:%d" % (_ip, port)}
        except Exception:                                      # noqa: BLE001
            pass
        return host in allow

    def do_GET(self):                                            # noqa: N802
        if not self._host_ok():
            body = b'{"ok":false,"error":"bad_host"}'
            self.send_response(403)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/state"):
            body = json.dumps(render_dyn(self.state), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        html = render(self.state).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def do_POST(self):                                           # noqa: N802
        """面板动作：
        /api/reset_ladder[?market=…] —— 删除本地组/档口重新开始；
        /api/save_config —— 保存『连接配置』到 config.json（KEY/市场/模式/倍数/钱包）；
        /api/start —— 按已保存配置启动引擎；
        /api/stop  —— 只停引擎（不下单），面板与端口保持。
        """
        if not self._host_ok():
            body = b'{"ok":false,"error":"bad_host"}'
            self.send_response(403)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # ★ 2026-10-03 流程改造：未登录只允许登录/登出/刷新/退出；其余面板操作拒绝
        if not (self.path.startswith("/api/auth_login")
                or self.path.startswith("/api/auth_logout")
                or self.path.startswith("/api/auth_refresh")
                or self.path.startswith("/api/quit")):
            with _AUTH_LOCK:
                _li = bool(_AUTH.get("user"))
            if not _li:
                body = b'{"ok":false,"error":"not_logged_in"}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        if self.path.startswith("/api/auth_login"):
            # ★ 2026-10-03 用户登录：用户名 + 口令 → 平台验证，session 存本地 cookies.txt
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(ln).decode("utf-8") if ln else ""
                f = urllib.parse.parse_qs(raw)
                uname = (f.get("username") or [""])[0].strip()
                pw = (f.get("password") or [""])[0]
            except Exception:                                     # noqa: BLE001
                uname, pw = "", ""
            ok = False
            if uname and pw:
                ok = auth_refresh(force=True, username=uname, password=pw)
            msg = "平台登录%s：%s" % ("成功" if ok else "失败",
                                   uname or "(未填用户名)")
            log(msg)
            if Dash.state:
                Dash.state.push_log(msg)
            err = ""
            if not ok:
                with _AUTH_LOCK:
                    err = _AUTH.get("error") or "用户名或口令错误"
            body = json.dumps({"ok": ok, "error": err}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/auth_logout"):
            auth_refresh(force=True, username="__LOGOUT__")
            msg = "已退出平台登录"
            log(msg)
            if Dash.state:
                Dash.state.push_log(msg)
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path.startswith("/api/auth_refresh"):
            # 手动刷新登录态 + 每日用量（引擎轮询也会低频自动刷新）
            ok = auth_refresh(force=True)
            msg = "已刷新登录态：" + ("在线" if ok and _AUTH.get("user") else "未登录")
            log(msg)
            if Dash.state:
                Dash.state.push_log(msg)
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        # ★ 2026-10-05 公网远程查看隧道：订阅 / 开启 / 关闭（均需已登录）
        if self.path.startswith("/api/tunnel_subscribe"):
            d, code = Api(None).tunnel("subscribe")
            ok = bool(d.get("ok"))
            info = d.get("data") if isinstance(d.get("data"), dict) else d
            msg = ""
            if ok:
                tunnel_refresh(force=True)
                with _TUNNEL_LOCK:
                    uid = _TUNNEL.get("uid")
                _tunnel_save_key(uid, info.get("private_key") or "")
                r = tunnel_start(panel_port=self.server.server_address[1])
                msg = "订阅成功" + ("，已开启公网隧道" if r.get("ok")
                                  else "，隧道启动失败：%s" % (r.get("message") or r.get("error") or ""))
            else:
                msg = "订阅失败：%s" % (d.get("message") or d.get("error") or "")
            log(msg)
            if Dash.state:
                Dash.state.push_log(msg)
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path.startswith("/api/tunnel_start"):
            r = tunnel_start(panel_port=self.server.server_address[1])
            msg = ("公网隧道已开启" if r.get("ok")
                   else "隧道开启失败：%s" % (r.get("message") or r.get("error") or ""))
            log(msg)
            if Dash.state:
                Dash.state.push_log(msg)
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path.startswith("/api/tunnel_stop"):
            r = tunnel_stop()
            msg = "公网隧道已关闭"
            log(msg)
            if Dash.state:
                Dash.state.push_log(msg)
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        # ★ 2026-10-05 开机自启动开关（写入 HKCU Run 注册表项，指向 exe --autostart）
        if self.path.startswith("/api/autostart"):
            qs = urllib.parse.urlparse(self.path).query
            on = urllib.parse.parse_qs(qs).get("on", ["1"])[0] not in ("0", "off", "false")
            ok, msg = autostart_set(bool(on))
            log(msg)
            if Dash.state:
                Dash.state.push_log(msg)
            body = json.dumps({"ok": ok, "message": msg}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/reset_ladder"):
            # ★ 2026-10-02（审查）：卡片级重置按 cid 定位 —— 原实现按表单
            #   market 字段重置，但面板已删「市场」下拉后该字段可能为空，
            #   一旦为空会 reset_ladder(None) 清空**全部市场**档口 + 清空全部台账（误伤）。
            # ★ 2026-10-04（多引擎）：档口按 KEY 隔离 —— 重置 = 重置该配置 KEY 的档口，
            #   并清空该配置的下单台账。
            qs = urllib.parse.urlparse(self.path).query
            market = urllib.parse.parse_qs(qs).get("market", [None])[0]
            cid = None
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(ln).decode("utf-8") if ln else ""
                f = urllib.parse.parse_qs(raw)
                if not market:
                    market = (f.get("market") or [None])[0]
                cid = (f.get("cid") or [None])[0]
            except Exception:                                      # noqa: BLE001
                pass
            market = (market or "").strip() or None
            cid = (cid or "").strip() or None
            cc_key = None
            if cid:
                cfgx = load_config()
                for c in (cfgx.get("configs") or []):
                    if c.get("id") == cid:
                        cc_key = (c.get("api_key") or "").strip() or None
                        if not market:
                            market = (c.get("market") or "").strip() or None
                        break
            if cc_key:
                lad = reset_ladder(key=cc_key)
                left = ledger_purge(cid=cid)
                scope_txt = "配置 %s（KEY 档口）" % cid
            elif market:
                lad = reset_ladder(key=None)
                left = ledger_purge(market=market)
                scope_txt = "市场 %s" % market
            else:
                lad = reset_ladder(key=None)
                left = ledger_purge()
                scope_txt = "全部"
            # 更新运行中引擎的档口状态（按 cid 精确；Dash.state 为兜底展示）
            if _engine_alive(cid):
                e = _ENGINES.get(cid) or {}
                es = e.get("state")
                if es is not None:
                    with es.lock:
                        es.ladder = ladder_scope(getattr(es, "key", ""))
                        ee = ladder_entry(es.ladder, es.market or (market or ""))
                        es.group_now = int(ee.get("group") or 1)
                        es.round_now = int(ee.get("round") or 1)
                    es.push_log("已重置本地分组/档口（%s）→ 第 1 组第 1 档，并清空台账（剩 %d 条）"
                                % (scope_txt, left))
            if Dash.state:
                with Dash.state.lock:
                    Dash.state.ladder = dict(lad)
                    e2 = ladder_entry(Dash.state.ladder, Dash.state.market or (market or ""))
                    Dash.state.group_now = int(e2.get("group") or 1)
                    Dash.state.round_now = int(e2.get("round") or 1)
                Dash.state.push_log("已重置本地分组/档口（%s）→ 第 1 组第 1 档，并清空台账（剩 %d 条）"
                                    % (scope_txt, left))
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path.startswith("/api/save_config"):
            cfg = load_config()
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(ln).decode("utf-8") if ln else ""
                f = urllib.parse.parse_qs(raw)
                def gv(k, d=""):
                    return (f.get(k) or [d])[0].strip()
            except Exception:                                     # noqa: BLE001
                f = {}
                def gv(k, d=""):
                    return d
            # ★ 2026-10-02 多配置：表单带 cid 保存对应配置并设为激活
            cid = gv("cid") or cfg.get("active_config") or ""
            cc = None
            for c in (cfg.get("configs") or []):
                if c.get("id") == cid:
                    cc = c
                    break
            if cc is None:
                cc = {"id": cid or "c1"}
                cfg.setdefault("configs", []).append(cc)
            for k in ("name", "market", "api_key", "mode", "private_key",
                      "funder", "pm_api", "strategy", "token_id", "token_secret",
                      "direction"):
                v = gv(k)
                if v:
                    cc[k] = v
            mult = gv("multiplier")
            if mult:
                try:
                    cc["multiplier"] = max(1, min(LADDER_MAX_MULT, int(mult)))
                except ValueError:
                    pass
            # ★ 2026-10-04（用户口径）：分组步长 / 最多档口 / 档口股数 —— 每配置可自定义
            try:
                gs = int(gv("group_step") or LADDER_GROUP_STEP)
                cc["group_step"] = max(1, min(LADDER_MAX_GROUP_STEP, gs))
            except ValueError:
                pass
            try:
                mr = int(gv("max_rounds") or LADDER_MAX_ROUNDS)
                cc["max_rounds"] = max(1, min(LADDER_MAX_ROUNDS_LIMIT, mr))
            except ValueError:
                pass
            shares_txt = gv("shares")
            if shares_txt:
                st_ = parse_shares_text(shares_txt)
                if st_ is None:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(("shares 格式错误：请输入逗号分隔的正整数（如 1,3,6,10）").encode("utf-8"))
                    return
                cc["shares"] = st_
            cc["enabled"] = True
            cfg["active_config"] = cid or "c1"
            save_config(cfg)
            msg = "已保存『连接配置』：%s · 市场=%s · 模式=%s · 倍数=×%s · 方向=%s · 组步长=%s · 最多档口=%s" % (
                cc.get("name") or cid,
                market_from_key(cc.get("api_key")) or cc.get("market") or "待识别",
                cc.get("mode") or "paper", cc.get("multiplier") or 1,
                "反向" if (cc.get("direction") or "reverse") == "reverse" else "顺向",
                cc.get("group_step") or LADDER_GROUP_STEP,
                cc.get("max_rounds") or LADDER_MAX_ROUNDS)
            log(msg)
            if Dash.state:
                Dash.state.push_log(msg)
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path.startswith("/api/new_config"):
            # ★ 2026-10-02 新增配置：cN+1，设为激活
            cfg = load_config()
            configs = cfg.setdefault("configs", [])
            n = len(configs) + 1
            while any(c.get("id") == "c%d" % n for c in configs):
                n += 1
            cid = "c%d" % n
            configs.append({"id": cid, "name": "配置%d" % n,
                            "market": "BTC-5m", "mode": "paper",
                            "multiplier": 1, "enabled": True})
            cfg["active_config"] = cid
            save_config(cfg)
            msg = "已新增配置 %s（编辑后点『保存并应用』）" % cid
            log(msg)
            if Dash.state:
                Dash.state.push_log(msg)
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path.startswith("/api/delete_config"):
            # ★ 2026-10-02 删除配置：从 config.json 移除；若是引擎正在跑的配置则一并停引擎
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(ln).decode("utf-8") if ln else ""
                f = urllib.parse.parse_qs(raw)
                cid = (f.get("cid") or [""])[0].strip()
            except Exception:                                      # noqa: BLE001
                cid = ""
            cfg = load_config()
            before = len(cfg.get("configs") or [])
            cfg["configs"] = [c for c in (cfg.get("configs") or [])
                              if (c.get("id") or "") != cid]
            if cfg.get("active_config") == cid:
                cfg["active_config"] = (cfg["configs"][0].get("id")
                                        if cfg["configs"] else "")
            save_config(cfg)
            # ★ 2026-10-02（用户口径）：删除配置 → 一并清掉该配置的下单台账
            left = ledger_purge(cid=cid)
            # 引擎正在跑这个配置 → 发停止指令（多引擎：精确停该配置）
            if _engine_alive(cid):
                e = _ENGINES.get(cid) or {}
                es = e.get("state")
                if es is not None:
                    es.stop = True
                msg = "已删除配置 %s（剩余 %d 个），并停其引擎、清空台账（剩 %d 条）" % (
                    cid, max(0, before - 1), left)
            else:
                msg = "已删除配置 %s（剩余 %d 个），并清空其台账（剩 %d 条）" % (
                    cid, max(0, before - 1), left)
            log(msg)
            if Dash.state:
                Dash.state.push_log(msg)
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path.startswith("/api/start"):
            # ★ 2026-10-02 按配置启动：表单带 cid 时先设为激活再启动
            # ★ 2026-10-04（多引擎）：不同 KEY 可并行；同一 KEY 互斥（start 内检查）
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(ln).decode("utf-8") if ln else ""
                f = urllib.parse.parse_qs(raw)
                cid = (f.get("cid") or [""])[0].strip()
            except Exception:                                      # noqa: BLE001
                cid = ""
            if cid:
                cfg = load_config()
                cfg["active_config"] = cid
                save_config(cfg)
            ok, msg = start_engine_from_config(cid=cid or None)
            log(msg)
            if Dash.state:
                Dash.state.push_log(msg)
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path.startswith("/api/stop"):
            # ★ 2026-10-04（多引擎）：只停指定配置的引擎；无 cid 时停全部引擎
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(ln).decode("utf-8") if ln else ""
                f = urllib.parse.parse_qs(raw)
                cid = (f.get("cid") or [""])[0].strip()
            except Exception:                                      # noqa: BLE001
                cid = ""
            stopped = []
            with _ENGINE_LOCK:
                for ecid, e in list(_ENGINES.items()):
                    if cid and ecid != cid:
                        continue
                    es = e.get("state")
                    if es is not None:
                        es.stop = True
                    stopped.append(ecid)
            if Dash.state:
                Dash.state.push_log("已发送关闭指令 —— 引擎将停止（面板与端口保持）：%s"
                                    % ("全部" if not cid else ("配置 " + cid)))
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path.startswith("/api/quit"):
            # ★ 2026-10-03：真正结束整个客户端进程（区别于"关闭程序"只停引擎）。
            #   无窗口打包后这是唯一的退出方式；先给引擎 1 秒收尾，然后强退。
            # ★ 2026-10-05（退出不了修复）：写 quit.flag 通知看门狗停止自动拉起，
            #   否则 finhub_watchdog.bat 会在进程退出后 30 秒内把它重新拉起。
            if Dash.state:
                Dash.state.stop = True
                Dash.state.push_log("正在退出整个客户端程序…")
            try:
                with open(os.path.join(CONFIG_DIR, "quit.flag"), "w",
                          encoding="utf-8") as _qf:
                    _qf.write("quit\n")
            except Exception:                                      # noqa: BLE001
                pass
            threading.Thread(target=lambda: (time.sleep(1), os._exit(0)),
                             daemon=True).start()
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        body = b'{"ok":false,"error":"not_found"}'
        self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                                   # 静音
        pass


# ---------------------------------------------------------------------------
# 引擎线程管理（面板「启动/关闭」用；面板/端口常驻，引擎可启停）
# ★ 2026-10-04（用户口径）：多引擎并行 —— 每个配置一个引擎线程，
#   同一 KEY（api_key）只能有一个引擎在跑（同一信号源不能重复下单），
#   不同 KEY 可并行启动（一个 KEY 对应一个市场）。
# ---------------------------------------------------------------------------
_ENGINE_LOCK = threading.Lock()
_ENGINES = {}    # cid -> {"thr": Thread, "key": api_key, "state": State}

# ★ 2026-10-05：面板可访问地址（本机 / 局域网 / 公网），main() 启动面板时设置，
#   render() 顶栏展示。公网路径 = 服务器 Nginx 反代 /local/ → SSH 反向隧道 → 本机 8787。
_PANEL_LINKS = ["http://127.0.0.1:8787/"]
_PUBLIC_PANEL_URL = "https://api.wanminguo.top/local/"


def _lan_ip():
    """取本机局域网 IP（用于 bind=0.0.0.0 时给手机访问的地址）。失败返回 None。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 53))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return ip or None
    except Exception:                                      # noqa: BLE001
        return None


def set_panel_links(bind, port):
    """按监听配置生成面板可访问地址列表（本机恒在；bind=0.0.0.0 加局域网；公网提示恒在）。"""
    links = ["http://127.0.0.1:%d/" % port]
    if bind in ("0.0.0.0", "::"):
        ip = _lan_ip()
        if ip:
            links.append("http://%s:%d/" % (ip, port))
    global _PANEL_LINKS
    _PANEL_LINKS = links


def _panel_links_html():
    """顶栏展示的面板访问地址（本机/局域网链接 + 公网远程提示）。"""
    parts = []
    for u in _PANEL_LINKS:
        parts.append('<a href="%s" style="color:#2f6fed">%s</a>' % (u, html.escape(u)))
    pub = '<a href="%s" style="color:#0f9d58">%s</a>' % (
        _PUBLIC_PANEL_URL, _PUBLIC_PANEL_URL)
    return ("面板：%s　·　公网远程查看：%s（手机可用；需电脑本机已开启『远程隧道』"
            " —— 见客户端目录 finhub_tunnel.bat）"
            % ("　".join(parts), pub))


def _engine_alive(cid):
    e = _ENGINES.get(cid)
    return bool(e and e.get("thr") and e["thr"].is_alive())


def _engines_alive_keys():
    """当前所有运行中引擎的 api_key 列表（同 KEY 互斥检查用）。"""
    with _ENGINE_LOCK:
        return [e["key"] for cid, e in _ENGINES.items() if _engine_alive(cid)]


def _engine_done(st):
    try:
        st.push_log("引擎已退出")
    except Exception:                                    # noqa: BLE001
        pass


def start_engine_from_config(overrides=None, cid=None):
    """从 config.json 的 configs[] 读参数并启动该配置的引擎线程（多引擎并行）。

    返回 (ok, msg)。面板 /api/start 与 main() 共用；同一 KEY 只能跑一个引擎。
    overrides: dict，可覆盖 market/mode/multiplier/base_shares/wait（CLI 优先）。
    """
    global _ENGINES
    cfg = load_config()
    if not cid:
        cid = cfg.get("active_config") or ""
    cc = None
    for c in (cfg.get("configs") or []):
        if c.get("id") == cid:
            cc = c
            break
    if cc is None:
        return False, "没有找到配置（configs[%s]）" % cid
    key = (cc.get("api_key") or "").strip()
    if not key:
        return False, "配置里没有 KEY —— 请先在『连接配置』填写信号 API KEY 并保存"
    if not cc.get("enabled"):
        return False, "该配置未启用（enabled=false）—— 保存时请勾选启用"
    # ★ 2026-10-04（用户口径）：同 KEY 互斥 —— 同一信号源不能开两个引擎重复下单
    with _ENGINE_LOCK:
        if _engine_alive(cid):
            return False, "该配置的引擎已在运行"
        for ocid, oe in _ENGINES.items():
            if ocid != cid and _engine_alive(ocid) and (oe.get("key") or "") == key:
                return False, ("KEY 已被配置「%s」使用（同一 KEY 不能开两个引擎，"
                               "避免同一信号重复下单）—— 请先关闭 %s，或为该配置换一把 KEY"
                               % (ocid, ocid))
    base = cfg.get("base") or DEFAULT_BASE
    # ★ 2026-10-02（用户口径）：面板已删「市场」选择 —— 市场由服务器按 KEY 返回，
    # ★ 2026-10-04（用户口径）：市场由 KEY 决定，不按配置名称/字段。
    #   引擎连接后用 market_from_key 从 KEY 解析市场（KEY 绑定市场不可改），
    #   配置里的 market 字段只是兼容兜底（不再信任它）。
    market = market_from_key(key) or (overrides or {}).get("market") or cc.get("market") or ""
    mode = (overrides or {}).get("mode") or cc.get("mode") or "paper"
    mult = int((overrides or {}).get("multiplier") or cc.get("multiplier") or 1)
    mult = max(1, min(LADDER_MAX_MULT, mult))
    base_shares = int(cc.get("base_shares") or 1)
    # ★ 2026-10-04（用户口径）：分组步长 / 最多档口 / 档口股数表 —— 每个配置可自定义
    try:
        group_step = int(cc.get("group_step") or LADDER_GROUP_STEP)
    except (TypeError, ValueError):
        group_step = LADDER_GROUP_STEP
    group_step = max(1, min(LADDER_MAX_GROUP_STEP, group_step))
    try:
        max_rounds = int(cc.get("max_rounds") or LADDER_MAX_ROUNDS)
    except (TypeError, ValueError):
        max_rounds = LADDER_MAX_ROUNDS
    max_rounds = max(1, min(LADDER_MAX_ROUNDS_LIMIT, max_rounds))
    shares_table = parse_shares_text(cc.get("shares"))
    if shares_table is None:
        shares_table = list(LADDER_SHARES_15)
    shares_table = shares_table[:max_rounds]
    wait = int((overrides or {}).get("wait") or cfg.get("wait") or 10)
    priv = (overrides or {}).get("private_key") or cc.get("private_key") or None
    funder = cc.get("funder") or None
    token_id = cc.get("token_id") or None
    token_secret = cc.get("token_secret") or None
    if mode == "live" and not priv:
        return False, "实盘模式需要填写 Polymarket 钱包私钥（只在本机使用）"
    import types
    args = types.SimpleNamespace(
        market=market, mode=mode, multiplier=mult, base_shares=max(1, base_shares),
        wait=min(15, max(5, wait)), since=0.0, private_key=priv, funder=funder,
        no_dashboard=True, no_tunnel=(mode == "paper"), tunnel=DEFAULT_TUNNEL,
        proxy_port=8788, price_cap=float(cfg.get("price_cap") or 0.85),
        token_id=token_id, token_secret=token_secret,
        group_step=group_step, max_rounds=max_rounds, shares_table=shares_table,
    )
    api = Api(key, base)
    proxy_url = None
    tp = None
    if not args.no_tunnel:
        host, _, port = args.tunnel.partition(":")
        tp = TunnelProxy((host, int(port or 8443)), args.proxy_port)
        tp.start()
        proxy_url = "http://127.0.0.1:%d" % args.proxy_port
        log("本地隧道代理已启动：http://127.0.0.1:%d → %s:%d"
            % (args.proxy_port, host, int(port or 8443)))
    trader = Trader(mode, args.base_shares, mult, args.price_cap,
                    args.private_key, funder, proxy=proxy_url,
                    token_id=token_id, token_secret=token_secret,
                    shares_table=shares_table, max_rounds=max_rounds,
                    direction=str(cc.get("direction") or "reverse"))
    st = State()
    st.cid = cid
    st.key = key
    st.mode = mode
    st.multiplier = mult
    st.base_shares = args.base_shares
    st.group_step = group_step
    st.max_rounds = max_rounds
    st.shares_table = shares_table
    st.direction = "reverse" if (cc.get("direction") or "reverse") == "reverse" else "follow"
    st.push_log("启动：市场=%s 模式=%s 倍数=×%s 方向=%s 组步长=%s 最多档口=%s 档口股数=%s"
                % (market, mode, mult,
                   "反向（信号涨我买跌）" if st.direction == "reverse" else "顺向（信号涨我买涨）",
                   group_step, max_rounds,
                   ",".join(str(x) for x in shares_table)))
    Dash.state = st

    def _entry(api_, trader_, st_, args_, tp_):
        try:
            run_engine(api_, trader_, st_, args_)
        except Exception as e:                                 # noqa: BLE001
            log("引擎异常退出：%s: %s" % (type(e).__name__, e))
        finally:
            if tp_:
                try:
                    tp_.stop()
                except Exception:                              # noqa: BLE001
                    pass
            with _ENGINE_LOCK:
                _ENGINES.pop(cid, None)

    thr = threading.Thread(target=_entry, args=(api, trader, st, args, tp), daemon=True)
    with _ENGINE_LOCK:
        _ENGINES[cid] = {"thr": thr, "key": key, "state": st, "trader": trader}
    thr.start()
    return True, "引擎已启动（市场=%s 模式=%s 倍数=×%s）" % (market, mode, mult)


# ---------------------------------------------------------------------------
# 主引擎
# ---------------------------------------------------------------------------
def run_engine(api, trader, st, args):
    # ★ 2026-09-28（审查 P2-9）：优先从**本地游标**恢复，而不是每次退回"1 小时前"。
    #   否则进程重启会把最近 10 分钟的信号再下一遍真单（服务端只保证不重复扣次）。
    # ★ 2026-10-04：游标按 KEY 隔离（多引擎并行互不干扰）。
    st.key = getattr(st, "key", "") or (getattr(api, "_key", "") or "")
    c_since, c_seen = load_cursor(key=st.key)
    since = float(args.since or 0) or c_since or (time.time() - 3600)
    seen = set(c_seen)
    if c_since:
        log("从本地游标恢复：since=%.0f，已记 %d 条已处理 signal_id（重启不会重复下单）"
            % (since, len(seen)))
    polls = 0
    backoff = 0.0          # 出错时的指数退避（成功一次就归零）
    # 启动时载入本地档口（分组/档口只认本机状态，按 KEY 隔离）
    st.ladder = ladder_scope(st.key)
    le = ladder_entry(st.ladder, args.market)
    st.group_now = int(le.get("group") or 1)
    st.round_now = int(le.get("round") or 1)
    log("开始订阅：市场=%s 模式=%s 倍数=×%s 本地档口=第%s组第%s档（%d份/单）等待=%ss"
        % (args.market, args.mode, args.multiplier, st.group_now, st.round_now,
           trader.shares(st.round_now), args.wait))
    if args.mode == "paper":
        log("纸面模式：本机模拟成交，回执里成交量填 0 —— **平台不会扣额度**。"
            "要真下单请用 --live。")
    # ★ 启动先补交上次没送出去的回执（含真实成交的那些）
    n_unsent = replay_unsent(api)
    if n_unsent:
        log("已补交 %d 笔此前失败的回执" % n_unsent)

    # 启动自检：额度余额（股，不够就不启动 —— 别让用户空跑）
    d, code = api.credits(ledger=5)
    if d.get("ok"):
        st.balance = (d.get("data", {}).get("credits") or {}).get("balance")
        st.market = (d.get("data", {}).get("sub") or {}).get("market", "")
        log("额度余额（股）：%s；订阅市场：%s" % (st.balance, st.market))
        if isinstance(st.balance, int) and st.balance <= 0:
            log("额度（股）为 0 —— 请先充值（1 股 = 1 额度）。现在只观察不下单。")
    else:
        log("取额度失败：%s %s（继续，但可能拉不到信号）"
            % (code, d.get("error") or d.get("message") or ""))

    while True:
        # ★ 面板「关闭」：只停引擎（不下单），面板与端口保持 —— 用户口径 2026-10-02
        if getattr(st, "stop", False):
            log("收到停止指令 —— 引擎已停止（面板保持开启）")
            st.push_log("引擎已停止 —— 可在面板『启动/关闭』重新启动")
            break
        # ★ market 必须透传（审查 P2-12）：原来这里不传，服务端就用默认市场，
        #   于是 `--market eth` 会静默拿到 BTC 的信号。
        d, code = api.signals(since=since, wait=args.wait, limit=20, market=args.market)
        st.last_poll = time.time()
        if not d.get("ok"):
            st.connected = False
            st.last_error = "%s %s" % (code, d.get("error") or "")
            log("拉信号失败：%s" % st.last_error)
            st.push_log("拉信号失败：%s" % st.last_error)
            # ★ 2026-09-28（第二轮审查 P1）：按状态码分类处理，别再"一律 3 秒重试"。
            #   · 402 额度用完：**不退出**（原来 return 2 直接结束进程，客户充值了
            #     也不会恢复，只能手动重启）—— 改成慢轮询等待充值到账；
            #   · 401/403 key 被吊销 / 市场锁定：明确告诉用户，指数退避（最多 5 分钟
            #     一次），因为这是"要人去处理"的错误，重试再快也没用；
            #   · 429/5xx/网络：指数退避（3s→6s→…→60s 封顶）。
            if code == 402:
                log("额度（股）用完了 —— 停下等你充值（充值到账后会自动继续，不用重启）")
                st.push_log("额度为 0，等待充值…")
                backoff = max(backoff, 60.0)
            elif code in (401, 403):
                msg = d.get("message") or d.get("error") or ""
                if "revoked" in str(d.get("error") or "") or "吊销" in str(msg):
                    log("★ 这把 key 已被吊销 —— 请到平台用户中心换一把新 key（不会自动恢复）")
                else:
                    log("★ 平台拒绝（HTTP %s）：%s —— 请按提示处理" % (code, msg))
                backoff = min(300.0, max(backoff * 2, 15.0))
            else:
                backoff = min(60.0, max(backoff * 2, 3.0))
            time.sleep(backoff)
            continue
        backoff = 0.0                     # 成功一次就把退避重置
        st.connected = True
        st.last_error = ""
        meta = d.get("meta") or {}
        if "credits" in meta:
            st.balance = (meta["credits"] or {}).get("balance")
        data = d.get("data") or {}
        if data.get("market"):
            st.market = data["market"]
        for s in data.get("signals") or []:
            # ★ 字段校验：缺 signal_id 的信号直接跳过并计数，**绝不让它把进程打崩**
            if not isinstance(s, dict) or not s.get("signal_id"):
                st.counts["errors"] += 1
                log("收到一条缺 signal_id 的信号，已跳过（平台侧异常，请反馈）")
                continue
            if s["signal_id"] in seen:
                continue
            # ★★ 第二轮审查 P0：单条信号的任何异常都**不能**带崩整个进程
            #   （原来一条畸形信号会让客户端退出且不再重连）。这里逐条兜住。
            try:
                _handle_one(s, api, trader, st, args, seen, since)
            except Exception as e:                               # noqa: BLE001
                st.counts["errors"] += 1
                log("处理信号 %s 时出错（已跳过，不影响后续）：%s: %s"
                    % (s.get("signal_id"), type(e).__name__, e))
                continue
            since = max(since, float(s.get("ts") or 0))
            save_cursor(since, seen, key=st.key)
        cur = ((data.get("cursor") or {}).get("next_since"))
        if cur and float(cur) > since + 0.5:
            since = float(cur)
            save_cursor(since, seen, key=st.key)
        if data.get("truncated"):
            log("本轮信号被 limit 截断了（还有没推完的）—— 下一轮会继续，不会漏")
        # ★ 结算推进本地档口：服务器只给市场事实（UP/DOWN），
        #   分组/档口由本机 ladder 语义推进 —— 绝不用服务器的档位。
        #   市场以引擎实测 st.market 为准（KEY 对应市场，面板不选市场）。
        # ★ 2026-10-04：档口按 KEY 隔离 —— 用 st.key 的档口子树读写。
        try:
            mk = st.market or args.market or ""
            sd, scode = api.settle(market=mk or None, last=40)
            if not sd.get("ok"):
                # ★ 2026-10-04（429 排查）：settle 失败不要立即回主循环 ——
                #   否则 settle 与下一轮 signals 落在同一秒，互相挤爆秒级 5qps。
                #   短退避 3s 让限流桶泄掉，随后继续正常轮询。
                log("拉结算失败（%s %s），短退避后继续" % (scode, sd.get("error") or ""))
                time.sleep(3.0)
            if sd.get("ok"):
                lad = ladder_scope(st.key)
                e = ladder_entry(lad, mk)
                last_ws = int(e.get("last_ws") or 0)
                last_side = str(e.get("last_side") or "").upper()
                if last_ws > 0 and last_side:
                    for w in (sd.get("data", {}).get("windows") or []):
                        if int(w.get("window_start") or 0) != last_ws:
                            continue
                        out = str(w.get("outcome") or "").upper()
                        if not out:
                            continue
                        win = (out == last_side)
                        e2 = ladder_update_result(lad, mk, win,
                                                  getattr(st, "group_step", None),
                                                  getattr(st, "max_rounds", None),
                                                  key=st.key)
                        st.group_now = int(e2.get("group") or 1)
                        st.round_now = int(e2.get("round") or 1)
                        st.ladder = dict(lad)
                        st.push_log("窗口 %s 结算 %s（我方 %s）：本地档口 → 第 %s 组第 %s 档"
                                    % (w.get("slug") or last_ws, out, last_side,
                                       st.group_now, st.round_now))
                        log("本地档口推进：窗口 %s 我方 %s 结算 %s → 第 %s 组第 %s 档"
                            % (last_ws, last_side, out, st.group_now, st.round_now))
                        # ★ 2026-10-02 台账盈亏：该窗口的订单补 win/pnl
                        try:
                            n = ledger_update_settle(mk, last_ws, out)
                            if n:
                                log("台账已结算 %d 笔（窗口 %s → %s）" % (n, last_ws, out))
                        except Exception as e4:                    # noqa: BLE001
                            log("台账结算失败：%s" % e4)
                        # 已推进过的窗口清掉，避免下一轮重复升档
                        e["last_ws"] = 0
                        e["last_side"] = ""
                        save_ladder_scope(st.key, lad)
                        break
        except Exception as e2:                                  # noqa: BLE001
            log("拉结算失败（不影响下单）：%s" % e2)
        # 每 ~2 分钟试一次补交暂存的回执
        polls += 1
        if polls % 12 == 0:
            replay_unsent(api)
        # 每轮都刷新一次余额（面板上要看得见额度在掉）
        if int(time.time()) % 15 < 2:
            cd, _ = api.credits(ledger=1)
            if cd.get("ok"):
                st.balance = ((cd.get("data") or {}).get("credits") or {}).get(
                    "balance", st.balance)


def _handle_one(s, api, trader, st, args, seen, since):
    """处理**一条**信号：下单 → 回执 → 记账。

    ★ 单独成函数是为了让 run_engine 能逐条 try/except —— 一条畸形信号
      绝不能把整个客户端带崩（第二轮审查 P0）。
    """
    seen.add(s["signal_id"])
    st.counts["signals"] += 1
    log("信号 %s %s 信号价=%s 我方限价=%s%s"
        % (s["signal_id"], s.get("side"), s.get("entry_price"),
           s.get("limit_capped") or s.get("limit_price"),
           "  ★信号价+滑点超过硬上限，按上限价挂（只会在盘口 ≤ 上限时成交，"
           "不会超付）" if s.get("over_cap") else ""))
    # ★ 盘口深度提示（第二轮审查的业务前提）：ask_sz 是信号那一刻最优档的
    #   挂单量。采集器自己的马丁档是 5/26/116/503 股，第 3/4 档常常吃不到
    #   （fill_ok=false 就是这个意思）；而我方一单只要 10×倍数 份。
    #   这里不擅自丢信号（用户口径是"不丢信号"），只**明确告知**，
    #   想看不过就加 --skip-thin。
    # ★ 本地档口（马丁阶梯）：份数 = 档位表[当前档] × 倍数，封顶 200。
    #   分组/档口只由本地 ladder.json 决定，服务器只发信号，不参与计算。
    # ★ 2026-10-04（多引擎）：档口按 KEY 隔离 —— 直接操作 st.key 的档口子树。
    if getattr(st, "ladder", None) is None:
        st.ladder = ladder_scope(getattr(st, "key", ""))
    lad = st.ladder
    e = ladder_entry(lad, st.market or args.market or "")
    # ★ PATCH 2026-10-02 重复档口修复：上一单未结算时不进场（与服务器引擎同口径）。
    #   结算要等窗口结束后的最终结果（约 5-10 分钟），若不等待，
    #   新信号会用「上一单还没推进」的旧档口 → 同一档口连下两单。
    if int(e.get("last_ws") or 0) > 0:
        st.counts["skipped"] += 1
        st.push_log("信号 %s 跳过：上一单窗口 %s 未结算（等结算推进档口）"
                    % (s["signal_id"], e.get("last_ws")))
        log("跳过信号 %s：上一单窗口 %s 未结算，等结算推进后再进场"
            % (s["signal_id"], e.get("last_ws")))
        return None
    round_ = int(e.get("round") or 1)
    want  = trader.shares(round_)
    depth = float(s.get("ask_sz") or 0.0)
    thin  = depth > 0 and depth + 1e-9 < want
    if thin and not s.get("src_fill_ok", True):
        log("提示：那一刻最优档只有 %.0f 份（采集器自己的档位也没吃满），"
            "我方要 %d 份 —— 可能只成交一部分（按实际成交量计额度）" % (depth, want))
    elif thin:
        log("提示：那一刻最优档只有 %.0f 份，我方要 %d 份 —— 可能只成交一部分"
            "（按实际成交量计额度）" % (depth, want))
    if thin and getattr(args, "skip_thin", False):
        rec = {"signal_id": s["signal_id"], "status": "skipped_thin",
               "requested_shares": want, "filled_shares": 0, "dry": True,
               "raw": {"ask_sz": depth, "want": want}}
        st.counts["skipped"] += 1
        log("按 --skip-thin 跳过（盘口太薄，不扣次）")
    else:
        rec = trader.place(s, round_=round_)
        rec["round"] = round_
        st.counts["ordered"] += 1
        if rec.get("status") == "paper":
            st.counts["paper"] += 1
        elif rec.get("filled_shares"):
            st.counts["filled"] += 1
        elif rec.get("status") in ("skipped_over_cap", "skipped_no_price", "dry_run",
                                   "skipped_thin"):
            st.counts["skipped"] += 1
        if rec.get("status") == "error":
            st.counts["errors"] += 1
        # 记录本单窗口（结算推进档口用；跳过/失败的单不算，不推进）
        if rec.get("status") not in ("skipped_over_cap", "skipped_no_price",
                                     "dry_run", "skipped_thin", "error"):
            try:
                ws = int(float(s.get("ts") or 0)) // 300 * 300   # 5m 窗口对齐
                e["last_ws"] = ws
                # ★ 2026-10-04（用户口径）：方向本地可调 —— 结算比对用**我方实际方向**
                #   （reverse 时信号 UP 我买 DOWN），不能用原始信号 side
                e["last_side"] = str(rec.get("my_side") or s.get("side") or "").upper()
                save_ladder_scope(st.key, lad)
            except Exception as e2:                               # noqa: BLE001
                log("记录本地档口窗口失败：%s" % e2)
    save_cursor(since, seen, key=st.key)  # ★ 下单前就落盘：崩了也不重放
    # 回传回执（纸面也回传：链路验证；服务端对纸面按 0 成交处理，不扣次）
    rd, rcode = api.receipt(rec)
    if rd.get("ok"):
        ch = (rd.get("data") or {}).get("charged") or 0
        st.counts["charged"] += ch
        st.balance = ((rd.get("meta") or {}).get("credits") or {}).get(
            "balance", st.balance)
        rec["charged"] = ch
        if (rd.get("data") or {}).get("dry"):
            log("回执已回传：纸面（不计费）余额=%s" % st.balance)
        else:
            log("回执已回传：成交=%s 扣次=%s 余额=%s"
                % (rec.get("filled_shares"), ch, st.balance))
    else:
        st.counts["errors"] += 1
        log("回执回传失败：%s %s" % (rcode, rd.get("error") or rd.get("message") or ""))
        # ★ 含真实成交的回执必须留住 —— 否则这笔永远扣不到次
        if int(rec.get("filled_shares") or 0) > 0:
            spool_receipt(rec)
    st.add(dict(s, receipt=rec, ts=(s.get("ts") or time.time())))
    st.push_log("信号 %s → %s（成交 %s）"
                % (s["signal_id"], rec.get("status"),
                   rec.get("paper_filled") if rec.get("status") == "paper"
                   else rec.get("filled_shares")))
    # ★ 2026-10-02 本地下单台账（orders.jsonl）：每次下单都落盘一笔，
    #   窗口结算后再补 win/pnl。面板「信号·回执·台账」大表从这里读。
    try:
        cfg = load_config()
        # ★ 2026-10-04（多引擎 BUG）：台账 cid 必须取**引擎自己的 cid**（st.cid），
        #   不能读全局 active_config —— 并行时谁最后点过启动，active_config 就是谁，
        #   会把另一个引擎下的单标错配置（如 BTC 单被记成「配置2」）。
        cid = getattr(st, "cid", "") or (cfg.get("active_config") or "")
        cname = ""
        for c in (cfg.get("configs") or []):
            if c.get("id") == cid:
                cname = c.get("name") or cid
                break
        ws = int(float(s.get("ts") or 0)) // 300 * 300
        mk = st.market or args.market or ""
        ledger_append({
            "ts": rec.get("ts") or s.get("ts") or time.time(),
            "t": time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(rec.get("ts") or s.get("ts") or time.time())),
            "cid": cid, "name": cname or cid, "market": mk,
            # ★ 2026-10-04（方向列 BUG）：side 必须记**实际买入方向**（rec.my_side），
            #   不能记信号原始 side —— reverse 配置买反边（信号 DOWN 买 UP），
            #   旧写法两行都显示信号方向（DOWN/DOWN），误导"反/顺方向一样"。
            "signal_id": s.get("signal_id", ""),
            "side": rec.get("my_side") or s.get("side"),
            "round": rec.get("round", round_), "want": int(rec.get("requested_shares") or 0),
            "filled": int(rec.get("paper_filled") if rec.get("status") == "paper"
                          else rec.get("filled_shares") or 0),
            "avg": rec.get("avg_price") or rec.get("avg") or None,
            "status": rec.get("status", ""), "charged": int(rec.get("charged") or 0),
            "ws": ws, "win": None, "pnl": None,
            # ★ 2026-10-03（用户口径）：备注 = 服务器策略页的窗口 slug
            #   （btc-updown-5m-1791005700），两边表格用它互相对照。
            "slug": str(s.get("slug") or ""),
        })
    except Exception as e3:                                      # noqa: BLE001
        log("写本地下单台账失败：%s" % e3)
    return rec


def cmd_status(api):
    d, code = api.credits(ledger=20)
    if not d.get("ok"):
        print("查询失败：HTTP %s %s" % (code, json.dumps(d, ensure_ascii=False)[:300]))
        return 1
    data = d["data"]
    c = data["credits"]
    print("订阅市场 : %s" % ((data.get("sub") or {}).get("market") or "（未绑定）"))
    print("剩余额度（股） : %s（累计获得 %s / 消耗 %s）" % (c["balance"], c["total_in"], c["total_used"]))
    print("计费规则 : %s" % data["product"]["credit_rule"])
    print("\n最近流水：")
    for l in data["ledger"]:
        print("  %+4d → 余额 %-5d [%s] %s  %s"
              % (l["delta"], l["balance_after"], l["reason"], l["ref"] or "-", l["created_at"]))
    return 0


def _acquire_singleton():
    """★ 2026-10-03（双开修复）：Windows 文件锁单实例互斥。
    返回持有的锁文件句柄（进程退出自动释放）；被占用返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import msvcrt
        d = CONFIG_DIR
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        f = open(os.path.join(d, "client.lock"), "a+", encoding="utf-8")
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            f.close()
            return None
        f.seek(0)
        f.truncate()
        f.write("pid=%d\n" % os.getpid())
        f.flush()
        return f
    except Exception:                              # noqa: BLE001
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="FinHub 信号客户端 v" + VERSION,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("--key", help="API key（也可以先跑 --save-key 存起来）")
    ap.add_argument("--base", default=None, help="平台地址（默认 %s）" % DEFAULT_BASE)
    ap.add_argument("--market", default="BTC-5m", help="订阅市场（当前只开放 BTC-5m）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_true", help="纸面模式（默认）：只记日志不下真单")
    g.add_argument("--live", action="store_true", help="实盘：真的下单（需 py-clob-client）")
    g.add_argument("--dry", action="store_true", help="只看信号，什么都不做")
    g.add_argument("--status", action="store_true", help="只看额度/订阅/流水，然后退出")
    ap.add_argument("--multiplier", type=int, default=None,
                    help="信号端倍数 1/2/5/10（默认 1；份数=档口股数×倍数）")
    ap.add_argument("--base-shares", type=int, default=10, help="基础份数（默认 10）")
    ap.add_argument("--wait", type=int, default=10,
                    help="长轮询等待秒数（默认 10；服务端上限 15）")
    ap.add_argument("--since", type=float, default=0.0, help="从这里之后的信号（unix 秒）")
    ap.add_argument("--port", type=int, default=8787, help="本地面板端口（默认 8787）")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="本地面板监听地址（默认 127.0.0.1 仅本机；填 0.0.0.0 可让同一局域网/公网设备访问）")
    ap.add_argument("--no-dashboard", action="store_true", help="不开本地面板")
    ap.add_argument("--autostart", action="store_true",
                    help="开机自启动模式：正常启动面板/引擎，但不自动打开浏览器（由『开机自启动』按钮写入）")
    ap.add_argument("--skip-thin", action="store_true",
                    help="最优档挂单量小于我方份数时跳过该信号（默认不跳过，只提示）")
    ap.add_argument("--tunnel", default=DEFAULT_TUNNEL, help="出网隧道 host:port")
    ap.add_argument("--no-tunnel", action="store_true", help="不起本地隧道代理")
    ap.add_argument("--proxy-port", type=int, default=8788, help="本地隧道代理端口")
    ap.add_argument("--private-key", default=None,
                    help="实盘钱包私钥（只在本机使用；也可用环境变量 FINHUB_PRIVATE_KEY）")
    ap.add_argument("--funder", default=None, help="实盘 funder 地址（代理钱包时用）")
    ap.add_argument("--save-key", action="store_true",
                    help="把 --key 存进 ~/.finhub/config.json（**不退出**，继续按选定模式跑）")
    ap.add_argument("--save-key-only", action="store_true",
                    help="只保存 key 然后退出（第一次配置时用）")
    ap.add_argument("--reset-ladder", metavar="MARKET", nargs="?", const="*",
                    help="删除本机分组/档口后退出（不传市场 = 全部市场；"
                         "也可以在本地面板点『重置分组/档口』）")
    args = ap.parse_args(argv)

    # ★ 2026-10-03（双开修复）：单实例锁 —— 曾出现两个 FinHubClient 同时跑，
    #   互相抢 client.log / ladder.json / orders.jsonl，且都"显示"占着 8787，
    #   其中一个引擎线程还会被文件锁拖死（8 小时无轮询）。这里用文件锁互斥。
    _singleton = _acquire_singleton()
    if _singleton is None and sys.platform == "win32":
        msg = "已有 FinHub 客户端实例在运行\n请先关闭旧实例（面板底部『退出程序』或任务管理器）再启动。"
        try:
            ctypes.windll.user32.MessageBoxW(0, msg, "FinHub 客户端", 0x10)
        except Exception:                                      # noqa: BLE001
            print(msg)
        return 1

    # ★ 本地档口重置：只清 ladder.json，不动 KEY/额度
    if args.reset_ladder is not None:
        mkt = None if args.reset_ladder == "*" else args.reset_ladder
        reset_ladder(mkt)
        print("已重置本地分组/档口%s → 全部回到第 1 组第 1 档"
              % ("" if mkt is None else ("（市场 %s）" % mkt)))
        return 0

    cfg = load_config()
    key = args.key or cfg.get("api_key")
    # ★ 多配置结构兼容（界面保存的 configs[]）：
    #   界面把 KEY/市场/模式/份数存成 configs[active_config]，引擎启动读取。
    #   未显式传 CLI 参数时按界面配置跑；显式参数优先。
    cfg_market = None
    cfg_mode_override = None
    cfg_mult = None
    if not key:
        cid = cfg.get("active_config") or ""
        for c in (cfg.get("configs") or []):
            if c.get("id") != cid or not c.get("api_key"):
                continue
            key = c["api_key"]
            cfg_market = c.get("market") or None
            cfg_mode_override = c.get("mode") or None
            # ★ 2026-10-04：cc.shares 已是档口股数表（list），旧 int 基础份数已废弃；
            #   倍数统一读 cc.multiplier
            mult_v = c.get("multiplier")
            try:
                cfg_mult = int(mult_v) if mult_v else None
            except (TypeError, ValueError):
                cfg_mult = None
            break
    if cfg_market and args.market == "BTC-5m":
        args.market = cfg_market
    if args.multiplier is None:
        args.multiplier = cfg_mult if cfg_mult is not None else 1
    # ★ --save-key-only 隐含"要保存"：README 让用户把 --save-key 换成 --save-key-only，
    #   如果只认 args.save_key，这一步就会**既没存 key 又一直跑下去**（实测踩到）。
    if (args.save_key or args.save_key_only) and args.key:
        cfg["api_key"] = args.key
        save_config(cfg)
        log("已保存到 %s" % CONFIG_PATH)
        # ★ 2026-09-28（第二轮审查 P0）：原来这里直接 return 0 ——
        #   而 README 的第 2 步带 --save-key、第 3 步就让用户开面板看信号，
        #   结果进程 0.17 秒就退出了，面板永远起不来。现在只有 --save-key-only 才退出。
        if args.save_key_only:
            return 0
    # ★ 2026-10-02 用户口径：**没有 KEY 也必须开端口** —— 启动面板，
    #   让用户在『连接配置』里填写 KEY 保存后点『启动』，而不是直接退出。
    if not key:
        st = State()
        st.mode = "paper"
        st.push_log("未找到已保存的 KEY —— 请在『连接配置』填写信号 API KEY 并保存，"
                    "然后点『启动』")
        if not args.no_dashboard:
            set_panel_links(args.bind, args.port)
            Dash.state = st
            srv = ThreadingHTTPServer((args.bind, args.port), Dash)
            log("本地面板： http://%s:%d（等待填写 KEY）" % (args.bind, args.port))
            log("没有 KEY 也保持端口开启 —— 面板『连接配置』里填写 KEY 保存后点『启动』")
            try:
                if not args.autostart:
                    threading.Timer(1.0, lambda: webbrowser.open(
                        "http://127.0.0.1:%d" % args.port)).start()
            except Exception:                                    # noqa: BLE001
                pass
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                return 0
        else:
            print("缺少 API key：用 --key 传一次并加 --save-key 存起来，或先跑 --status")
            return 1

    base = args.base or cfg.get("base") or DEFAULT_BASE
    api = Api(key, base)
    if args.status:
        return cmd_status(api)

    mode = "live" if args.live else ("dry" if args.dry else "paper")
    if not (args.live or args.dry or args.paper) and cfg_mode_override:
        mode = cfg_mode_override
    mult = max(1, min(LADDER_MAX_MULT, args.multiplier))
    # 私钥优先命令行，其次环境变量 FINHUB_PRIVATE_KEY，再其次界面保存的配置
    #（2026-10-02：用户从面板保存私钥后直接点『启动』，CLI 不应再拦）
    priv = args.private_key or os.environ.get("FINHUB_PRIVATE_KEY") or None
    if not priv:
        cid = cfg.get("active_config") or ""
        for c in (cfg.get("configs") or []):
            if c.get("id") == cid:
                priv = c.get("private_key") or None
                break
    if mode == "live" and not priv:
        print("实盘需要私钥：--private-key 0x… 或 环境变量 FINHUB_PRIVATE_KEY"
              "（只在本机使用，不会发给平台）")
        return 1
    if mode == "live":
        if args.no_dashboard:
            # 纯命令行（无面板）：保留回车确认，避免误开实盘
            print("=" * 74)
            print("实盘模式：将用你的钱包真下单。风险自负；信号只表示方向，不保证盈利。")
            print("继续请按回车（Ctrl-C 取消）…")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                return 1
        else:
            # 面板常驻模式：面板上点『启动』本身就是确认动作，这里不再阻塞
            log("实盘模式：面板『启动』即视为确认 —— 将用你的钱包真下单，风险自负")
    args.private_key = priv
    args.mode = mode          # run_engine 里要用（别让它去猜 --paper/--live/--dry）

    st = State()
    st.mode = mode
    st.multiplier = mult
    st.base_shares = args.base_shares

    # ★ 面板常驻（主线程），引擎独立线程可启停 —— 用户口径 2026-10-02：
    #   关闭 = 只停引擎（不下单、不连接平台信号），**端口/面板保持**。
    #   ★ 2026-10-02（用户口径）：面板模式**不自动启动引擎** ——
    #     每个配置由「启动此配置 / 关闭程序」按钮手动控制（实盘配置尤其不能开机自跑）。
    if not args.no_dashboard:
        set_panel_links(args.bind, args.port)
        Dash.state = st
        srv = ThreadingHTTPServer((args.bind, args.port), Dash)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log("本地面板： http://%s:%d（引擎未自动启动，请在各配置点『启动此配置』）" % (args.bind, args.port))
        # ★ 2026-10-03（无窗口打包）：自动打开本地面板，双击 exe 后直接看到界面
        try:
            if not args.autostart:
                threading.Timer(1.0, lambda: webbrowser.open(
                    "http://127.0.0.1:%d" % args.port)).start()
        except Exception:                                      # noqa: BLE001
            pass
        # ★ 2026-10-03（用户登录）：启动时自动检查一次平台登录态（本地 cookies.txt）
        try:
            auth_refresh(force=True)
            if _AUTH.get("user"):
                log("平台登录态：已登录（%s）" % _AUTH["user"].get("username"))
            else:
                log("平台登录态：未登录（面板顶部『用户登录』填写用户名/口令）")
        except Exception:                                        # noqa: BLE001
            pass
        # ★ 2026-10-05（公网远程查看）：启动时自动恢复隧道 + 保活重连线程。
        #   已订阅 + 本机已有私钥 → 自动拉起；之后掉线由保活线程自动重连。
        try:
            if _AUTH.get("user"):
                tunnel_refresh(force=True)
                with _TUNNEL_LOCK:
                    _uinfo = dict(_TUNNEL.get("info") or {})
                    _uid = _TUNNEL.get("uid")
                if (_uinfo.get("subscribed")
                        and _uid and os.path.exists(_tunnel_key_path(_uid))):
                    r = tunnel_start(panel_port=args.port)
                    log("公网隧道：" + ("已自动开启" if r.get("ok")
                                      else "启动失败：%s" % (r.get("message") or r.get("error") or "")))
                threading.Thread(target=_tunnel_keepalive,
                                 args=(args.port,), daemon=True).start()
        except Exception:                                        # noqa: BLE001
            pass
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            log("收到 Ctrl-C，退出")
            if Dash.state:
                Dash.state.stop = True
            return 0

    ok, msg = start_engine_from_config({
        "market": args.market, "mode": mode, "multiplier": mult,
        "base_shares": args.base_shares, "wait": args.wait,
        "private_key": priv,
    })
    log(msg)
    st.push_log(msg)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("收到 Ctrl-C，退出")
        if Dash.state:
            Dash.state.stop = True
        return 0


if __name__ == "__main__":
    sys.exit(main())
