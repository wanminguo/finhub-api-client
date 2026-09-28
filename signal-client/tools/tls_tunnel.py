#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tls_tunnel.py —— 出网隧道 v2：**外层 TLS + CONNECT**（把内层 SNI 藏起来）
================================================================================
为什么必须换掉 SNI 版（2026-09-28 实测结论）：

  旧版把客户端的 ClientHello **原样转发**，而 ClientHello 里的 SNI
  （`clob.polymarket.com`）是**明文**的。国内链路上的 DPI 看见它就向两端注入 RST。
  对照实验（同一台服务器、同一个端口、同一个隧道，只换 SNI）：

      SNI=example.com           → 握手完成，活了 0.9s，上行 1518B
      SNI=clob.polymarket.com   → 0.0s 被 RST，上行 1424B

  也就是说：**不是安全组、不是代码 bug，是设计问题** —— 只要客户端把目标的
  SNI 明文发出来，这条路在国内就是不通的。

v2 的设计：

     客户端 ──(外层 TLS，SNI=api.wanminguo.top)──▶ 本隧道 ──▶ clob.polymarket.com
                    └─ 里面发 CONNECT clob.polymarket.com:443
                    └─ 之后的字节是**内层 TLS**（客户端↔Polymarket），对 DPI 不可见

  · 外层 TLS 用**平台自己的证书**终结（域名国内可直连、SNI 无害）；
  · 内层 TLS 仍是客户端与 Polymarket 的**端到端**加密：平台看不到下单内容、
    更拿不到私钥；平台能看到的只有"连了哪个域名、多少字节"（和旧版一样）；
  · 安全边界从"按 SNI 判断"改成"按 CONNECT 目标判断"，白名单/仅 443/并发上限不变。

用法：
    python3 tls_tunnel.py --listen 0.0.0.0:8443 \
        --cert /path/fullchain.pem --key /path/privkey.pem
"""

import argparse
import os
import socket
import ssl
import sys
import threading
import time

DEFAULT_ALLOW = [
    "clob.polymarket.com",                    # 下单 / 查单 / 余额
    "gamma-api.polymarket.com",               # 市场元数据
    "data-api.polymarket.com",                # 持仓 / 成交
    "ws-subscriptions-clob.polymarket.com",   # 订单簿推送
    "polygon-rpc.com",                        # 查余额 / approve（Polygon RPC）
    "rpc.ankr.com",
]

LOG_PATH = "/var/log/finhub_tunnel.log"
MAX_CONN = 400
MAX_PER_IP = 20               # ★ 单 IP 并发上限（防单个 IP 占满整个隧道）
HELLO_TIMEOUT = 12.0          # 等 CONNECT 请求的超时
IDLE_TIMEOUT = 900.0          # 通道建好后的兜底（死连接回收）
_n = 0
_per_ip = {}                  # ip -> 当前连接数
_lock = threading.Lock()


def log(msg: str) -> None:
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    sys.stderr.write(line + "\n")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def allowed(host: str, allow) -> bool:
    return any(host == d or host.endswith("." + d) for d in allow)


class CertKeeper:
    """按需重载证书。

    ★ 2026-09-28 自查发现的问题：隧道原来是**启动时**加载一次证书，
      而宝塔/Let's Encrypt 续签只会 reload nginx，**不会重启我们这个 Python 服务**。
      后果：证书到期那天，客户端的"外层 TLS 握手"会因证书过期/不匹配而失败 ——
      整条下单链路**静默失效**（只在隧道日志里留一行握手失败）。
      这里定期检查证书文件的 mtime/size，变了就重建 SSLContext 并热切换，
      全程不中断已有连接、也不需要人工重启。
    """

    def __init__(self, cert: str, key: str, check_every: float = 300.0):
        self.cert = cert
        self.key = key
        self.check_every = check_every
        self.stamp = None
        self.ctx = None
        self.lock = threading.Lock()
        self.last_check = 0.0
        if not self.reload(force=True):
            raise RuntimeError("证书加载失败: %s / %s" % (cert, key))

    def _stamp(self):
        try:
            return tuple((os.stat(p).st_mtime, os.stat(p).st_size)
                         for p in (self.cert, self.key))
        except OSError:
            return None

    def reload(self, force: bool = False) -> bool:
        st = self._stamp()
        if st is None:
            log("证书文件读不到（%s / %s）—— 保留当前证书继续服务" % (self.cert, self.key))
            return False
        with self.lock:
            if not force and st == self.stamp:
                return False
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                ctx.load_cert_chain(self.cert, self.key)
            except (OSError, ssl.SSLError) as e:
                log("证书重载失败（继续用旧的）: %s" % e)
                return False
            first = self.ctx is None
            self.ctx = ctx
            self.stamp = st
            if not first:
                log("★ 证书已热重载（文件变了：mtime=%s）" % (st[0][0],))
            return True

    def maybe_reload(self):
        """在 accept 循环里定期调用（很便宜：两次 stat）。"""
        now = time.time()
        if now - self.last_check >= self.check_every:
            self.last_check = now
            self.reload()


def read_connect_request(tls_sock: socket.socket):
    """从外层 TLS 通道里读一条 HTTP CONNECT 请求（只读头部，最多 8KB）。"""
    tls_sock.settimeout(HELLO_TIMEOUT)
    buf = b""
    while b"\r\n\r\n" not in buf:
        if len(buf) > 8192:
            return None, buf
        try:
            chunk = tls_sock.recv(1)          # 逐字节：绝不能多读（后面就是内层 TLS 字节）
        except OSError:
            return None, buf
        if not chunk:
            return None, buf
        buf += chunk
    head = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
    return head, buf


def pipe(a: socket.socket, b: socket.socket, counter=None):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
            if counter is not None:
                counter[0] += len(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(conn: socket.socket, addr, allow, ctx: ssl.SSLContext):
    global _n
    t0 = time.time()
    sent = 0
    target = "?"
    tls = None
    try:        # ---- ① 外层 TLS 握手（用平台自己的证书）----
        try:
            conn.settimeout(HELLO_TIMEOUT)
            tls = ctx.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError) as e:
            log("%s 外层 TLS 握手失败: %s: %s" % (addr[0], type(e).__name__, e))
            return

        # ---- ② 读 CONNECT 目标 ----
        line, _ = read_connect_request(tls)
        if not line:
            log("%s 拒绝（没收到 CONNECT 请求）" % addr[0])
            return
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            log("%s 拒绝（不是 CONNECT：%r）" % (addr[0], line[:60]))
            tls.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        hostport = parts[1]
        host = hostport.rsplit(":", 1)[0].lower()
        try:
            port = int(hostport.rsplit(":", 1)[1]) if ":" in hostport else 443
        except ValueError:
            port = 0
        target = "%s:%d" % (host, port)

        if port != 443:
            log("%s 拒绝（只允许 443）: %s" % (addr[0], target))
            tls.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        if not allowed(host, allow):
            log("%s 拒绝（不在白名单）: %s" % (addr[0], target))
            tls.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return

        # ---- ③ 连上游并放行 ----
        try:
            up = socket.create_connection((host, 443), timeout=12)
        except OSError as e:
            log("%s 上游连不上 %s: %s" % (addr[0], target, e))
            tls.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        tls.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        tls.settimeout(IDLE_TIMEOUT)
        up.settimeout(IDLE_TIMEOUT)

        cnt = [0]
        t = threading.Thread(target=pipe, args=(tls, up, cnt), daemon=True)
        t.start()
        pipe(up, tls)
        sent = cnt[0]
        up.close()
    except OSError:
        pass
    finally:
        try:
            (tls or conn).close()
        except OSError:
            pass
        with _lock:
            _n -= 1
            _per_ip[addr[0]] = max(0, _per_ip.get(addr[0], 1) - 1)
            if _per_ip.get(addr[0], 0) <= 0:
                _per_ip.pop(addr[0], None)
        log("%s 断开 → %s 时长=%.1fs 上行=%dB" % (addr[0], target, time.time() - t0, sent))


def main() -> int:
    global _n
    ap = argparse.ArgumentParser(description="FinHub 出网隧道 v2（外层 TLS + CONNECT）")
    ap.add_argument("--listen", default="0.0.0.0:8443")
    ap.add_argument("--cert", required=True, help="fullchain.pem")
    ap.add_argument("--key", required=True, help="privkey.pem")
    ap.add_argument("--allow", default="", help="逗号分隔的白名单域名（默认内置一组）")
    ap.add_argument("--max-conn", type=int, default=MAX_CONN)
    ap.add_argument("--max-per-ip", type=int, default=MAX_PER_IP,
                    help="单个 IP 的并发上限（默认 %d）" % MAX_PER_IP)
    ap.add_argument("--cert-check", type=float, default=300.0,
                    help="每隔多少秒检查一次证书是否续签（默认 300）")
    args = ap.parse_args()

    allow = [x.strip() for x in args.allow.split(",") if x.strip()] or DEFAULT_ALLOW
    host, _, port = args.listen.partition(":")
    port = int(port or 8443)

    keeper = CertKeeper(args.cert, args.key, check_every=max(1.0, args.cert_check))
    ctx = keeper.ctx

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(128)
    srv.settimeout(30)          # ★ 让 accept 可被打断，好定期检查证书是否续签了
    log("隧道 v2 已启动：%s:%d（外层 TLS + CONNECT，白名单=%s）"
        % (host, port, ",".join(allow)))
    log("证书：%s（每 %.0f 秒检查一次续签，变更即热重载）"
        % (args.cert, keeper.check_every))

    while True:
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            keeper.maybe_reload()
            continue
        except KeyboardInterrupt:
            break
        except OSError:
            continue
        with _lock:
            ip = addr[0]
            if _n >= args.max_conn:
                log("%s 拒绝（全局并发已达上限 %d）" % (ip, args.max_conn))
                conn.close()
                continue
            if _per_ip.get(ip, 0) >= args.max_per_ip:
                log("%s 拒绝（单 IP 并发已达上限 %d —— 隧道是共享资源，"
                    "单个 IP 不能占满）" % (ip, args.max_per_ip))
                conn.close()
                continue
            _n += 1
            _per_ip[ip] = _per_ip.get(ip, 0) + 1
        threading.Thread(target=handle, args=(conn, addr, allow, keeper.ctx),
                         daemon=True).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
