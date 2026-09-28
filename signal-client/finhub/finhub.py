#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FinHub 信号客户端 —— 连接 signal API、按信号下单、并把回执回传（计次凭据）
================================================================================
一段话说明它干什么：
    本机跑一个小程序 → 长轮询订阅你的 BTC 信号 → 到点按信号方向挂**限价单**
    （信号价 + 0.05，硬上限 0.85，FAK：吃到多少算多少）→ 把**成交回执**回传给平台。
    平台上"1 次 = 一个成功下单回执"，所以没成交不扣你的次数。

对外只依赖标准库：
    · 信号订阅 / 回执上报 / 次数查询：urllib（标准库）
    · 本地面板：http.server（标准库），浏览器打开 http://127.0.0.1:8787
    · 出网隧道：本地 CONNECT 代理（标准库 socket），**任何**库都能通过它出去
      （国内直连 Polymarket 不通，所以走隧道；代理只监听 127.0.0.1 + 只放白名单域名）
    · **实盘下单**需要官方 SDK：pip install py-clob-client
      —— 纸面模式（默认）不需要它，零依赖就能跑通全链路。

三种运行模式：
    paper   纸面：照信号"假设成交"，只记日志与面板（默认；用来验证链路与体验）
    live    实盘：真的下单（需要 py-clob-client + 你的钱包私钥，私钥只在本机）
    dry     只看信号、什么都不做（排查用）

安全底线（代码里就是这么写的）：
    1. 私钥/API 凭证**只在你的机器上**，绝不发往平台；隧道是**不解密转发**（TLS 端到端）
    2. 本地代理只绑 127.0.0.1，且**只放白名单域名**，不是通用翻墙出口
    3. 实盘必须显式 `--live`；首次会打印风险确认
    4. 启动先查次数余额；余额为 0 会提示充值而不是空跑

没有自动"本金上限"这种功能：每收到一个信号就下**一单**，单笔金额 =
基础份数 × 倍数 × 限价，所以**控制投入靠的是 --base-shares / --multiplier
与你自己往 Polymarket 里放多少钱**，而不是靠软件里的开关。

用法（完整参数见 --help）：
    python -m finhub --key <你的APIKEY> --paper
    python -m finhub --key <你的APIKEY> --live --private-key <0x...> --multiplier 1
    python -m finhub --key <你的APIKEY> --status        # 只看次数/订阅，不下单
"""

import argparse
import json
import os
import socket
import socketserver
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
DEFAULT_BASE = "https://api.wanminguo.top/polymarket"
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


def load_cursor(path=CURSOR_PATH):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return float(d.get("since") or 0.0), [str(x) for x in (d.get("seen") or [])]
    except Exception:                                            # noqa: BLE001
        return 0.0, []


def save_cursor(since, seen, path=CURSOR_PATH, force=False):
    """把游标/已处理 signal_id 落盘（节流 + 失败不刷屏）。

    ★ 为什么节流：原来每轮长轮询（10 秒一次）都写一次盘，日志里还会因为
      目录不可写刷一条错 —— 既没必要又淹没信息。只有游标真的前进了才写。
    """
    since = float(since)
    if not force and _cursor_saved[0] > 0 and abs(since - _cursor_saved[0]) < 0.5:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"since": since, "seen": list(seen)[-CURSOR_KEEP:]}, fh)
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
# 平台 API（信号 / 回执 / 次数）
# ---------------------------------------------------------------------------
class Api:
    """平台 API 客户端。

    ★★ 2026-09-28（第二轮审查 P0）：平台请求**绝不允许**走本地出网隧道。
      上一版在实盘初始化里写了 `os.environ.setdefault("HTTP_PROXY", ...)` ——
      那是**进程全局**的，urllib 也会读它，于是平台请求被塞进本地那个
      **只支持 CONNECT** 的代理，代理对普通 GET/POST 一律回 405 →
      `--live`（默认带隧道）时信号/次数/回执三个接口**全线 405**，
      而且日志只写"拉信号失败：405"，看起来像平台挂了。
      现在：API 用**自带 opener + 空 ProxyHandler**，无论环境变量怎么设都不走代理；
      隧道只给 SDK 用（见 Trader._make_client）。
    """

    def __init__(self, key, base=DEFAULT_BASE, timeout=35):
        self.key = key
        self.base = base.rstrip("/")
        self.timeout = timeout
        # 显式"不走任何代理"（含环境变量 HTTP_PROXY/HTTPS_PROXY）
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _req(self, path, method="GET", body=None, query=None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"X-Api-Key": self.key, "Accept": "application/json",
                   "User-Agent": "finhub-client/%s" % VERSION}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace")), r.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw), e.code
            except ValueError:
                return {"ok": False, "error": "http_%d" % e.code, "raw": raw[:400]}, e.code
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": "network", "message": str(e)}, 0

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
    """把"信号"变成"订单"，并产出**回执**（平台据此计次）。

    纸面：按自己的限价模拟成交（数量取基础份数 × 倍数），**但回执里成交量填 0**
          —— 平台只认真实成交（见下面 place() 的注释）。
    实盘：交给官方 SDK（py-clob-client）下 FAK 限价单，读真实回执。
    """

    def __init__(self, mode, base_shares, multiplier, cap, private_key=None,
                 funder=None, signature_type=1, proxy=None):
        self.mode = mode
        self.base_shares = base_shares
        self.multiplier = multiplier
        self.cap = cap
        self.private_key = private_key
        self.funder = funder
        self.signature_type = signature_type
        self.proxy = proxy
        self.client = None
        if mode == "live":
            self.client = self._make_client()

    def shares(self):
        return max(1, int(self.base_shares * self.multiplier))

    def _make_client(self):
        try:
            from py_clob_client.client import ClobClient            # noqa: PLC0415
        except ImportError:
            raise SystemExit(
                "实盘模式需要官方 SDK：pip install py-clob-client\n"
                "（纸面模式不需要它：去掉 --live 即可跑通全链路）")
        host = "https://clob.polymarket.com"
        c = ClobClient(host, key=self.private_key, chain_id=137,
                       signature_type=self.signature_type, funder=self.funder) \
            if self.funder else ClobClient(host, key=self.private_key, chain_id=137)
        if self.proxy:
            # ★★ 2026-09-28（第二轮审查 P0）：**只给 SDK 的会话设代理**，
            #   绝不动 os.environ（那会污染 urllib，把平台请求也塞进隧道 → 405）。
            #   SDK 底层是 requests，直接改它的 session.proxies 最稳。
            try:
                sess = getattr(c, "session", None) or getattr(
                    getattr(c, "client", None), "session", None)
                if sess is not None:
                    sess.proxies.update({"http": self.proxy, "https": self.proxy})
                    self._proxy_note = "已给 SDK 会话设置代理 %s" % self.proxy
                else:
                    self._proxy_note = ("警告：拿不到 SDK 的 session，无法设置代理，"
                                        "实盘可能需要 `export HTTPS_PROXY=%s`" % self.proxy)
            except Exception as e:                                   # noqa: BLE001
                self._proxy_note = "设置 SDK 代理失败：%s: %s" % (type(e).__name__, e)
        c.set_api_creds(c.create_or_derive_api_creds())
        return c

    def proxy_note(self):
        return getattr(self, "_proxy_note", "")

    def place(self, sig):
        """返回回执 dict（signal_id/filled_shares/avg_price/status/order_id/raw）。"""
        # ★ 2026-09-28（第二轮审查 P0）：**不要**在字段缺失时崩掉整个进程。
        #   服务端理论上一定给 signal_id，但"客户端因为一条畸形信号整进程退出"
        #   是不可接受的失败模式（退出后再也不会重连）。
        if not isinstance(sig, dict) or not sig.get("signal_id"):
            return {"signal_id": "", "status": "error", "requested_shares": 0,
                    "filled_shares": 0, "raw": {"error": "信号缺少 signal_id，已跳过"}}
        side = str(sig.get("side") or "").upper()
        # ★ 2026-09-28 修（审查 P1-7）：服务端现在给的是**没 cap 过的原始价**
        #   limit_price，另外给 limit_capped（已按硬上限压过）与 over_cap 标记。
        #   原实现只读 limit_price，而服务端当时已经 min() 过 0.85 —— 于是
        #   `limit > cap` 永远不成立，over_cap 形同虚设。
        raw_limit = float(sig.get("limit_price") or 0)
        capped = sig.get("limit_capped")
        limit = float(capped) if capped is not None else min(raw_limit, self.cap)
        if limit <= 0:
            return {"signal_id": sig["signal_id"], "status": "skipped_no_price",
                    "requested_shares": self.shares(), "filled_shares": 0,
                    "raw": {"limit": raw_limit}}
        over = bool(sig.get("over_cap")) or (raw_limit > self.cap + 1e-9)
        want = self.shares()
        if self.mode == "dry":
            return {"signal_id": sig["signal_id"], "status": "dry_run",
                    "requested_shares": want, "filled_shares": 0,
                    "dry": True, "raw": {}}
        if self.mode == "paper":
            # ★ 2026-09-28 修（审查 P0-1）：纸面回执的 **filled_shares 必须是 0**。
            #   原实现填 want（10×倍数），而服务端照 filled 扣次 —— 默认模式
            #   每收到一个信号就真扣一次已付的次数。纸面成交只是本机模拟，
            #   把"模拟成交多少"放在 paper_filled 里给面板看，不参与计费。
            return {"signal_id": sig["signal_id"], "status": "paper",
                    "requested_shares": want, "filled_shares": 0,
                    "dry": True, "paper_filled": want, "avg_price": limit,
                    "order_id": "PAPER-" + sig["signal_id"][-12:],
                    "raw": {"note": "纸面模拟成交（本机），回执不计次",
                            "would_fill": want, "limit": limit, "over_cap": over}}
        # ---- 实盘 ----
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType   # noqa: PLC0415
            from py_clob_client.order_builder.constants import BUY      # noqa: PLC0415
            token = sig.get("token_id") or ""
            if not token:
                return {"signal_id": sig["signal_id"], "status": "error",
                        "requested_shares": want, "filled_shares": 0,
                        "raw": {"error": "信号里没有 token_id（服务端未能解析 CLOB token，"
                                         "常见于市场刚创建/网络抖动）—— 已跳过，不扣次"}}
            args = OrderArgs(price=round(limit, 3), size=want, side=BUY, token_id=token)
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
                    "raw": {"resp": resp, "limit": limit, "raw_limit": raw_limit,
                            "over_cap": over, "token_id": token}}
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
        self.signals = []          # 最近 100 条：信号 + 回执
        self.counts = {"signals": 0, "ordered": 0, "filled": 0, "skipped": 0,
                       "charged": 0, "errors": 0, "paper": 0}
        self.log = []

    def add(self, item):
        with self.lock:
            self.signals.append(item)
            del self.signals[:-100]

    def push_log(self, msg):
        with self.lock:
            self.log.append((time.strftime("%H:%M:%S"), msg))
            del self.log[:-200]


PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>FinHub 信号客户端</title>
<meta http-equiv="refresh" content="3">
<style>
 body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
      background:#f6f8fc;color:#131a2a;margin:0;padding:22px}}
 .wrap{{max-width:1020px;margin:0 auto}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#6b7891;font-size:13px;margin-bottom:18px}}
 .kpis{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;
        margin-bottom:16px}}
 .kpi{{background:#fff;border:1px solid #e3e9f2;border-radius:12px;padding:11px 13px}}
 .kpi .k{{font-size:11.5px;color:#6b7891}} .kpi .v{{font-size:19px;font-weight:650}}
 table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e9f2;
        border-radius:12px;overflow:hidden;font-size:12.5px}}
 th,td{{padding:7px 10px;text-align:left;border-bottom:1px solid #eef2f8}}
 th{{background:#fbfcfe;color:#6b7891;font-size:11.5px}}
 .up{{color:#0f9d58}} .dn{{color:#d64545}} .mono{{font-family:ui-monospace,Consolas,monospace}}
 .pill{{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600}}
 .ok{{background:#e9f7ef;color:#0f9d58}} .bad{{background:#fdeded;color:#d64545}}
 .warn{{background:#fdf5e6;color:#b7791f}} .mute{{background:#f2f5fa;color:#6b7891}}
 pre{{background:#fff;border:1px solid #e3e9f2;border-radius:12px;padding:10px;
      font-size:11.5px;line-height:1.7;max-height:220px;overflow:auto}}
</style></head><body><div class="wrap">
<h1>FinHub 信号客户端</h1>
<div class="sub">本机面板 · 每 3 秒自动刷新 · 服务地址与模式见下表（数据不出你的机器）</div>
{kpis}
<h2 style="font-size:15px;margin:18px 0 8px">最近信号与回执</h2>
{rows}
<h2 style="font-size:15px;margin:18px 0 8px">本机日志</h2>
<pre>{log}</pre>
</div></body></html>"""


def render(st):
    with st.lock:
        c = dict(st.counts)
        sigs = list(st.signals)[::-1][:40]
        logs = list(st.log)[::-1][:60]
        bal = st.balance
        conn = st.connected
        mode = st.mode
        mult = st.multiplier
        base = st.base_shares
        mk = st.market
        err = st.last_error
        up = int(time.time() - st.started)
    kpis = [
        ("连接", ("正常" if conn else "断开"), "ok" if conn else "bad"),
        ("模式", mode, "ok" if mode == "live" else "mute"),
        ("剩余次数", "—" if bal is None else str(bal), "ok"),
        ("市场 / 倍数", "%s ×%s (%d 份)" % (mk, mult, base * mult), "mute"),
        ("收到信号", c["signals"], "mute"),
        ("已下单", c["ordered"], "mute"),
        ("实盘成交", c["filled"], "ok"),
        ("纸面模拟", c.get("paper", 0), "mute"),
        ("跳过/失败", "%d / %d" % (c["skipped"], c["errors"]),
         "bad" if c["errors"] else "mute"),
        ("本机运行", "%d 分 %d 秒" % (up // 60, up % 60), "mute"),
    ]
    kh = "".join('<div class="kpi"><div class="k">%s</div><div class="v"><span class="pill %s">%s</span></div></div>'
                 % (k, cls, v) for k, v, cls in kpis)
    if not sigs:
        rows = '<p style="color:#6b7891;font-size:13px">还没有信号。等到平台推送就会出现在这里。</p>'
    else:
        body = []
        for s in sigs:
            sc = "up" if s.get("side") == "UP" else "dn"
            r = s.get("receipt") or {}
            stt = str(r.get("status") or "-")
            is_paper = (stt == "paper")
            rc = "ok" if stt in ("matched", "paper") else (
                "mute" if stt in ("live", "unmatched", "skipped_over_cap",
                                  "skipped_no_price", "dry_run") else "bad")
            # 纸面回执的 filled_shares 恒为 0（不计费），面板上显示模拟份数
            shown = r.get("paper_filled") if is_paper else r.get("filled_shares", "-")
            body.append(
                "<tr><td class='mono'>%s</td><td class='%s'>%s</td><td class='mono'>%s</td>"
                "<td class='mono'>%s</td><td class='mono'>%s</td><td class='mono'>%s</td>"
                "<td><span class='pill %s'>%s</span></td><td class='mono'>%s</td></tr>"
                % (time.strftime("%H:%M:%S", time.localtime(s.get("ts", 0))), sc,
                   s.get("side"), s.get("entry_price"), s.get("limit_price"),
                   ("%s(模拟)" % shown) if is_paper else shown,
                   r.get("avg_price", "-") or "-", rc, stt,
                   r.get("charged", "-")))
        rows = ("<table><tr><th>时间</th><th>方向</th><th>信号价</th><th>我们的限价</th>"
                "<th>成交份数</th><th>成交均价</th><th>状态</th><th>扣次</th></tr>"
                + "".join(body) + "</table>")
    if err:
        rows += '<p style="color:#d64545;font-size:12.5px">最近错误：%s</p>' % err
    logtxt = "\n".join("%s  %s" % (t, m) for t, m in logs) or "（空）"
    return PAGE.format(kpis=kh, rows=rows, log=logtxt)


class Dash(BaseHTTPRequestHandler):
    state = None

    def _host_ok(self):
        """★ 2026-09-28（第二轮审查 P1）：校验 Host，挡住 DNS rebinding。

        面板虽然只绑 127.0.0.1，但浏览器里的恶意网页可以把某个域名解析到
        127.0.0.1 再打这个端口 —— 没有 Host 校验时对方就能读走 /api/state
        （里面有 token_id、order_id、CLOB 原始响应）。只认本机名字。
        """
        host = str(self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1] if self.server else 0
        allow = {"127.0.0.1:%d" % port, "localhost:%d" % port,
                 "127.0.0.1", "localhost", "[::1]:%d" % port, "[::1]"}
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
            with self.state.lock:
                data = {"connected": self.state.connected, "mode": self.state.mode,
                        "balance": self.state.balance, "counts": self.state.counts,
                        "signals": self.state.signals[-40:]}
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
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

    def log_message(self, *a):                                   # 静音
        pass


# ---------------------------------------------------------------------------
# 主引擎
# ---------------------------------------------------------------------------
def run_engine(api, trader, st, args):
    # ★ 2026-09-28（审查 P2-9）：优先从**本地游标**恢复，而不是每次退回"1 小时前"。
    #   否则进程重启会把最近 10 分钟的信号再下一遍真单（服务端只保证不重复扣次）。
    c_since, c_seen = load_cursor()
    since = float(args.since or 0) or c_since or (time.time() - 3600)
    seen = set(c_seen)
    if c_since:
        log("从本地游标恢复：since=%.0f，已记 %d 条已处理 signal_id（重启不会重复下单）"
            % (since, len(seen)))
    polls = 0
    backoff = 0.0          # 出错时的指数退避（成功一次就归零）
    log("开始订阅：市场=%s 模式=%s 倍数=×%s（%d 份/单）等待=%ss"
        % (args.market, args.mode, args.multiplier, trader.shares(), args.wait))
    if args.mode == "paper":
        log("纸面模式：本机模拟成交，回执里成交量填 0 —— **平台不会扣次数**。"
            "要真下单请用 --live。")
    # ★ 启动先补交上次没送出去的回执（含真实成交的那些）
    n_unsent = replay_unsent(api)
    if n_unsent:
        log("已补交 %d 笔此前失败的回执" % n_unsent)

    # 启动自检：次数余额（不够就不启动 —— 别让用户空跑）
    d, code = api.credits(ledger=5)
    if d.get("ok"):
        st.balance = (d.get("data", {}).get("credits") or {}).get("balance")
        st.market = (d.get("data", {}).get("sub") or {}).get("market", "")
        log("次数余额：%s；订阅市场：%s" % (st.balance, st.market))
        if isinstance(st.balance, int) and st.balance <= 0:
            log("次数为 0 —— 请先充值（1 次 = 一个成功下单回执）。现在只观察不下单。")
    else:
        log("取次数失败：%s %s（继续，但可能拉不到信号）"
            % (code, d.get("error") or d.get("message") or ""))

    while True:
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
            #   · 402 次数用完：**不退出**（原来 return 2 直接结束进程，客户充值了
            #     也不会恢复，只能手动重启）—— 改成慢轮询等待充值到账；
            #   · 401/403 key 被吊销 / 市场锁定：明确告诉用户，指数退避（最多 5 分钟
            #     一次），因为这是"要人去处理"的错误，重试再快也没用；
            #   · 429/5xx/网络：指数退避（3s→6s→…→60s 封顶）。
            if code == 402:
                log("次数用完了 —— 停下等你充值（充值到账后会自动继续，不用重启）")
                st.push_log("次数为 0，等待充值…")
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
            save_cursor(since, seen)
        cur = ((data.get("cursor") or {}).get("next_since"))
        if cur and float(cur) > since + 0.5:
            since = float(cur)
            save_cursor(since, seen)
        if data.get("truncated"):
            log("本轮信号被 limit 截断了（还有没推完的）—— 下一轮会继续，不会漏")
        # 每 ~2 分钟试一次补交暂存的回执
        polls += 1
        if polls % 12 == 0:
            replay_unsent(api)
        # 每轮都刷新一次余额（面板上要看得见次数在掉）
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
    want  = trader.shares()
    depth = float(s.get("ask_sz") or 0.0)
    thin  = depth > 0 and depth + 1e-9 < want
    if thin and not s.get("src_fill_ok", True):
        log("提示：那一刻最优档只有 %.0f 份（采集器自己的档位也没吃满），"
            "我方要 %d 份 —— 可能只成交一部分（按实际成交量计次）" % (depth, want))
    elif thin:
        log("提示：那一刻最优档只有 %.0f 份，我方要 %d 份 —— 可能只成交一部分"
            "（按实际成交量计次）" % (depth, want))
    if thin and getattr(args, "skip_thin", False):
        rec = {"signal_id": s["signal_id"], "status": "skipped_thin",
               "requested_shares": want, "filled_shares": 0, "dry": True,
               "raw": {"ask_sz": depth, "want": want}}
        st.counts["skipped"] += 1
        log("按 --skip-thin 跳过（盘口太薄，不扣次）")
    else:
        rec = trader.place(s)
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
    save_cursor(since, seen)          # ★ 下单前就落盘：崩了也不重放
    # 回传回执（纸面也回传：链路验证；服务端对纸面按 0 成交处理，不扣次）
    rd, rcode = api.receipt(rec)
    if rd.get("ok"):
        ch = (rd.get("data") or {}).get("charged") or 0
        st.counts["charged"] += ch
        st.balance = ((rd.get("meta") or {}).get("credits") or {}).get(
            "balance", st.balance)
        rec["charged"] = ch
        if (rd.get("data") or {}).get("dry"):
            log("回执已回传：纸面（不计次）余额=%s" % st.balance)
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
    return rec


def cmd_status(api):
    d, code = api.credits(ledger=20)
    if not d.get("ok"):
        print("查询失败：HTTP %s %s" % (code, json.dumps(d, ensure_ascii=False)[:300]))
        return 1
    data = d["data"]
    c = data["credits"]
    print("订阅市场 : %s" % ((data.get("sub") or {}).get("market") or "（未绑定）"))
    print("剩余次数 : %s（累计获得 %s / 消耗 %s）" % (c["balance"], c["total_in"], c["total_used"]))
    print("计次规则 : %s" % data["product"]["credit_rule"])
    print("\n最近流水：")
    for l in data["ledger"]:
        print("  %+4d → 余额 %-5d [%s] %s  %s"
              % (l["delta"], l["balance_after"], l["reason"], l["ref"] or "-", l["created_at"]))
    return 0


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
    g.add_argument("--status", action="store_true", help="只看次数/订阅/流水，然后退出")
    ap.add_argument("--multiplier", type=int, default=1, help="倍数 ×1~×5（默认 1）")
    ap.add_argument("--base-shares", type=int, default=10, help="基础份数（默认 10）")
    ap.add_argument("--wait", type=int, default=10,
                    help="长轮询等待秒数（默认 10；服务端上限 15）")
    ap.add_argument("--since", type=float, default=0.0, help="从这里之后的信号（unix 秒）")
    ap.add_argument("--port", type=int, default=8787, help="本地面板端口（默认 8787）")
    ap.add_argument("--no-dashboard", action="store_true", help="不开本地面板")
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
    args = ap.parse_args(argv)

    cfg = load_config()
    key = args.key or cfg.get("api_key")
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
    if not key:
        print("缺少 API key：用 --key 传一次并加 --save-key 存起来，或先跑 --status")
        return 1

    base = args.base or cfg.get("base") or DEFAULT_BASE
    api = Api(key, base)
    if args.status:
        return cmd_status(api)

    mode = "live" if args.live else ("dry" if args.dry else "paper")
    mult = max(1, min(5, args.multiplier))
    # 私钥优先命令行，其次环境变量 FINHUB_PRIVATE_KEY（避免进 shell 历史/进程列表）
    priv = args.private_key or os.environ.get("FINHUB_PRIVATE_KEY") or None
    if mode == "live" and not priv:
        print("实盘需要私钥：--private-key 0x… 或 环境变量 FINHUB_PRIVATE_KEY"
              "（只在本机使用，不会发给平台）")
        return 1
    if mode == "live":
        print("=" * 74)
        print("实盘模式：将用你的钱包真下单。风险自负；信号只表示方向，不保证盈利。")
        print("继续请按回车（Ctrl-C 取消）…")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            return 1
    args.private_key = priv

    st = State()
    st.mode = mode
    st.multiplier = mult
    st.base_shares = args.base_shares
    args.mode = mode          # run_engine 里要用（别让它去猜 --paper/--live/--dry）

    proxy_url = None
    tp = None
    if not args.no_tunnel:
        host, _, port = args.tunnel.partition(":")
        tp = TunnelProxy((host, int(port or 8443)), args.proxy_port)
        tp.start()
        proxy_url = "http://127.0.0.1:%d" % args.proxy_port

    trader = Trader(mode, args.base_shares, mult, float(
        (load_config().get("price_cap") or 0.85)), args.private_key, args.funder,
        proxy=proxy_url)

    if not args.no_dashboard:
        Dash.state = st
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Dash)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log("本地面板： http://127.0.0.1:%d" % args.port)
        if proxy_url:
            log("要让其它程序走隧道：设置 HTTPS_PROXY=%s" % proxy_url)

    try:
        return run_engine(api, trader, st, args)
    except KeyboardInterrupt:
        log("收到 Ctrl-C，退出")
        return 0
    finally:
        if tp:
            tp.stop()


if __name__ == "__main__":
    sys.exit(main())
