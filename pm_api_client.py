#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pm_api_client.py —— FinHub API · Polymarket 5 分钟涨跌盘数据 API 的官方 Python 客户端
============================================================================================

**零第三方依赖**（只用标准库：urllib / json / dataclasses / typing），
和站点采集器的风格一致 —— 你把它拷进任何 Python 3.8+ 环境都能直接跑。

服务地址：https://api.wanminguo.top/quant/polymarket/v1/
接口文档：https://api.wanminguo.top/quant/polymarket/docs.php
注册免费 key：https://api.wanminguo.top/me/register.php

这个 API 给的是 **Polymarket 5 分钟涨跌盘（up/down）的结算输入数据**：
官方 Chainlink TWAP60 + 多家现货 + CLOB 盘口，**约 2 秒一条**，覆盖 7 个市场
（btc / eth / sol / xrp / doge / hype / bnb）。

快速开始
--------
    export PM_API_KEY=pm_live_xxxxxxxx    # Windows: set PM_API_KEY=...

    from pm_api_client import PmApi

    api = PmApi()                       # 从环境变量读 key
    w, meta = api.window(market='btc')  # 当前窗口快照
    print(w.price_to_beat, w.official_bp, w.leading_side)

    if w.beat_trusted is False:
        ...                             # ★ 这个窗口的 beat 是自算退化值，别拿它判方向

设计要点（这几条是这个客户端存在的原因）
----------------------------------------
1. **错误信封**：HTTP 4xx/5xx 的响应体是 ``{"ok": false, "error": "...", "message": "..."}``。
   本客户端把它统一抛成 :class:`PmError`，``e.code`` 就是你需要的错误码字符串，
   ``e.retry_after`` / ``e.quota`` 等也一并带上 —— 不用自己解析 body。
   成功信封是 ``{ok, data, meta}``，但 **``/v1/index.php`` 是唯一例外**
   （扁平结构，字段全在顶层，没有 ``data``/``meta``）；客户端按「有没有 ``data``
   层」判断形状，两种都能吃，见 :meth:`PmApi.index`。
2. **429 分两种，处理方式完全相反**：
   ``rate_limited``（超 QPS）→ 等 ``Retry-After`` 秒后重试；
   ``daily_quota_exceeded``（当日配额用完）→ **重试没有意义**，请等到 ``quota.reset_at``。
   客户端自动重试**只**对前者生效，后者永远不会被重试（也永远不该被你重试）。
3. **``meta.quota`` 每次响应都有**：调用方据此提前降频，不用等撞墙。
   ``meta.remaining_ratio`` / ``meta.should_back_off()`` 是给这件事用的糖。
   （例外：``/v1/index.php`` 没有 ``meta``，所以没有配额可读。）
4. **``beat_trusted`` 如实透传**：``False`` 表示该窗口官方 Chainlink 读数没进来，
   ``price_to_beat`` 退化成自算值（系统性偏约 3bp，而 60 秒真实位移中位数只有 1.83bp）
   —— 此时**不能**用它判方向。本客户端绝不吞掉这个标记。
5. **端点路径都带 ``.php``**：本站没有配置 URL rewrite，``/v1/window`` 会直接 404。

注意事项
--------
* 轮询 ``window.php`` 的合理频率是 **1~2 秒**（数据本身就是 2 秒一条，更密没有意义）。
* ★ 但先看日配额：``free`` 档 500 次/天，2 秒轮询约 **17 分钟**就打光。
* 没有任何 key 被硬编码在本文件里，也请**不要**把 key 提交进 Git。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

__version__ = "1.0.0"

#: 服务基线。可用环境变量 ``PM_API_BASE`` 覆盖（自建/灰度节点用）。
DEFAULT_BASE = "https://api.wanminguo.top/quant/polymarket/v1"

#: 没带 key 时提示去哪注册。
REGISTER_URL = "https://api.wanminguo.top/me/register.php"
DOCS_URL = "https://api.wanminguo.top/quant/polymarket/docs.php"
ENDPOINTS_URL = "https://api.wanminguo.top/quant/polymarket/endpoints.php"

#: 7 个市场的短名（``?market=`` 用这些）。
MARKETS: Tuple[str, ...] = ("btc", "eth", "sol", "xrp", "doge", "hype", "bnb")

#: 短名 → slug 前缀。
MARKET_PREFIXES: Dict[str, str] = {m: "%s-updown-5m" % m for m in MARKETS}

#: 采集节奏：约 2 秒一条。建议轮询间隔不要小于这个数。
SAMPLE_INTERVAL_SEC = 2.0

#: 429 里的两个错误码，语义完全不同，**不要**混为一谈。
ERR_RATE_LIMITED = "rate_limited"
ERR_DAILY_QUOTA_EXCEEDED = "daily_quota_exceeded"

#: 默认重试次数（只对 ``rate_limited`` 生效）。
DEFAULT_MAX_RETRIES = 2

#: 信封字段。没有 ``data`` 层时，这些键属于元信息、不属于载荷。
_ENVELOPE_KEYS = frozenset(("ok", "error", "message", "meta"))

#: 遇到「顶层即载荷」的响应时，是否往 stderr 打一行提示（只打一次）。
#: 想彻底安静就设成 False 或环境变量 ``PM_QUIET_DEVIATION=1``。
_DEBUG_FLAT_ENVELOPE = os.environ.get("PM_QUIET_DEVIATION", "") == ""


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class PmError(Exception):
    """接口返回了 ``{"ok": false, ...}`` 信封，或 HTTP 层面失败。

    属性
    ----
    status       HTTP 状态码（网络层失败时为 ``None``）
    code         接口的 ``error`` 字符串，如 ``bad_market`` / ``rate_limited``
    message      接口的 ``message``（人类可读，通常是中文）
    retry_after  秒数。``rate_limited`` 时可用；来自响应头 ``Retry-After``
                 或 body 里的 ``retry_after_sec``
    quota        失败信封里带的配额快照（``daily_quota_exceeded`` 时有）
    payload      完整原始 body（dict），便于调用方自己取额外字段
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        status: Optional[int] = None,
        payload: Optional[Mapping[str, Any]] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        self.code = code or "unknown_error"
        self.message = message or ""
        self.status = status
        self.payload: Dict[str, Any] = dict(payload or {})
        self.retry_after = retry_after
        q = self.payload.get("quota")
        self.quota: Optional[Dict[str, Any]] = q if isinstance(q, dict) else None
        super().__init__(self._render())

    def _render(self) -> str:
        bits = []
        if self.status is not None:
            bits.append("HTTP %s" % self.status)
        bits.append(self.code)
        head = " ".join(bits)
        if self.message:
            return "%s: %s" % (head, self.message)
        return head

    # -- 语义糖：让调用方不必记住错误码字符串 --
    @property
    def is_rate_limited(self) -> bool:
        """超 QPS。等 ``retry_after`` 秒后重试是合理的。"""
        return self.code == ERR_RATE_LIMITED

    @property
    def is_quota_exceeded(self) -> bool:
        """当日配额用完。**重试没有意义**，等到 ``reset_at``。"""
        return self.code == ERR_DAILY_QUOTA_EXCEEDED

    @property
    def reset_at(self) -> Optional[str]:
        """``daily_quota_exceeded`` 时的配额重置时刻（UTC ISO8601）。"""
        if self.quota:
            v = self.quota.get("reset_at")
            return v if isinstance(v, str) else None
        return None

    @property
    def available_markets(self) -> Optional[List[str]]:
        """``bad_market`` 时接口会回一个 ``markets`` 数组。"""
        v = self.payload.get("markets")
        return list(v) if isinstance(v, list) else None


class PmMissingApiKey(PmError):
    """本地就没拿到 key —— 根本没发请求。"""

    def __init__(self) -> None:
        super().__init__(
            "missing_api_key",
            "未设置 API key。请先到 %s 注册免费 key，"
            "然后 `export PM_API_KEY=pm_live_...`（Windows: `set PM_API_KEY=...`），"
            "或显式传入 PmApi(api_key='...')。" % REGISTER_URL,
        )


class PmRateLimited(PmError):
    """``429 rate_limited`` —— 超过每秒请求上限。"""


class PmQuotaExceeded(PmError):
    """``429 daily_quota_exceeded`` —— 当日配额用完，重试无意义。"""


class PmTransportError(PmError):
    """连不上 / 超时 / TLS 失败 / 响应不是 JSON。"""


_ERROR_CLASSES = {
    ERR_RATE_LIMITED: PmRateLimited,
    ERR_DAILY_QUOTA_EXCEEDED: PmQuotaExceeded,
}


# ---------------------------------------------------------------------------
# 响应信封的两个元数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Quota:
    """``meta.quota`` —— **每次响应都会返回**，据此做退避，不用等撞墙。"""

    daily_limit: int = 0
    used_today: int = 0
    remaining: int = 0
    reset_at: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Optional[Mapping[str, Any]]) -> "Quota":
        d = d or {}

        def _i(k: str) -> int:
            v = d.get(k)
            return int(v) if isinstance(v, (int, float)) else 0

        ra = d.get("reset_at")
        return cls(
            daily_limit=_i("daily_limit"),
            used_today=_i("used_today"),
            remaining=_i("remaining"),
            reset_at=ra if isinstance(ra, str) else None,
        )

    @property
    def is_unlimited(self) -> bool:
        """``daily_quota = 0`` 的语义是**不限量**，不是禁用。"""
        return self.daily_limit <= 0

    @property
    def remaining_ratio(self) -> float:
        """剩余比例，1.0 = 满血。不限量时恒为 1.0。"""
        if self.is_unlimited:
            return 1.0
        return max(0.0, min(1.0, self.remaining / float(self.daily_limit)))

    def should_back_off(self, threshold: float = 0.10) -> bool:
        """剩余低于 ``threshold``（默认 10%）就该降频了。"""
        return (not self.is_unlimited) and self.remaining_ratio < threshold


@dataclass(frozen=True)
class Feed:
    """``meta.feed`` —— 采集侧数据新鲜度，用来自查"我拿到的是不是陈数据"。"""

    ok: bool = False
    latest_slug: Optional[str] = None
    latest_age_sec: Optional[float] = None
    raw: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, d: Optional[Mapping[str, Any]]) -> "Feed":
        if not isinstance(d, Mapping):
            return cls()
        age = d.get("latest_age_sec")
        slug = d.get("latest_slug")
        return cls(
            ok=bool(d.get("ok")),
            latest_slug=slug if isinstance(slug, str) else None,
            latest_age_sec=float(age) if isinstance(age, (int, float)) else None,
            raw=dict(d),
        )


@dataclass(frozen=True)
class Meta:
    """成功响应的 ``meta``：套餐 / 配额 / 限额 / 数据新鲜度 / 服务器时间。"""

    endpoint: str = ""
    plan: str = ""
    key_prefix: str = ""
    quota: Quota = Quota()
    limits: Dict[str, Any] = None  # type: ignore[assignment]
    feed: Feed = Feed()
    server_ts: Optional[int] = None
    raw: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # frozen dataclass 里给可变默认值的标准写法
        if self.limits is None:
            object.__setattr__(self, "limits", {})
        if self.raw is None:
            object.__setattr__(self, "raw", {})

    @classmethod
    def from_dict(cls, d: Optional[Mapping[str, Any]]) -> "Meta":
        d = d or {}
        ts = d.get("server_ts")
        limits = d.get("limits")
        return cls(
            endpoint=str(d.get("endpoint") or ""),
            plan=str(d.get("plan") or ""),
            key_prefix=str(d.get("key_prefix") or ""),
            quota=Quota.from_dict(d.get("quota")),
            limits=dict(limits) if isinstance(limits, Mapping) else {},
            feed=Feed.from_dict(d.get("feed")),
            server_ts=int(ts) if isinstance(ts, (int, float)) else None,
            raw=dict(d),
        )

    @property
    def remaining(self) -> int:
        return self.quota.remaining

    @property
    def remaining_ratio(self) -> float:
        return self.quota.remaining_ratio

    def should_back_off(self, threshold: float = 0.10) -> bool:
        return self.quota.should_back_off(threshold)

    @property
    def history_days(self) -> int:
        v = self.limits.get("history_days")
        return int(v) if isinstance(v, (int, float)) else 0

    @property
    def max_samples_per_call(self) -> int:
        v = self.limits.get("max_samples_per_call")
        return int(v) if isinstance(v, (int, float)) else 0

    @property
    def max_windows_per_call(self) -> int:
        v = self.limits.get("max_windows_per_call")
        return int(v) if isinstance(v, (int, float)) else 0

    @property
    def qps(self) -> int:
        v = self.limits.get("qps")
        return int(v) if isinstance(v, (int, float)) else 0


# ---------------------------------------------------------------------------
# 成功响应：既能当 dict 用，也能当对象用
# ---------------------------------------------------------------------------

class Result(dict):
    """``data`` 载荷。

    它 **就是** 一个 dict（``r["slug"]`` / ``"slug" in r`` / ``r.get(...)`` 都能用），
    同时支持属性访问（``r.slug``）—— 因为 ``slug`` / ``market`` 这类名字在
    载荷里很常见，但 ``class`` / ``from`` 这类是 Python 保留字，只能走下标。

    ★ 关键的可信度标记一律如实透传：``beat_source`` / ``beat_trusted``。
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(
                "%r 不在响应载荷里；可用字段：%s"
                % (name, ", ".join(sorted(self.keys())) or "(空)")
            ) from None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self[name]
        except KeyError:
            raise AttributeError(name) from None

    @property
    def beat_trusted(self) -> Optional[bool]:
        """``False`` = 官方读数缺失，``price_to_beat`` 是自算退化值，**别用它判方向**。

        载荷没这个字段时返回 ``None``（例如 ``settle`` 端点不适用）。
        """
        v = self.get("beat_trusted")
        return v if isinstance(v, bool) else None

    @property
    def is_trusted_beat(self) -> bool:
        """严格判断：只有接口明确说 ``True`` 才算可信。"""
        return self.get("beat_trusted") is True


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """``Retry-After`` 可能是秒数，也可能是 HTTP 日期。两种都认。"""
    if not value:
        return None
    v = value.strip()
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(v)
        if dt is None:
            return None
        now = time.time()
        if dt.tzinfo is None:
            return None
        return max(0.0, dt.timestamp() - now)
    except Exception:
        return None


class PmApi:
    """Polymarket 数据聚合 API 客户端。

    参数
    ----
    api_key   显式 key；不传则读环境变量 ``PM_API_KEY``
    base      服务基线，默认 :data:`DEFAULT_BASE`（也可用 ``PM_API_BASE`` 覆盖）
    timeout   单次请求超时（秒），默认 15
    max_retries 遇到 ``rate_limited`` 时最多自动重试几次，默认 2；
              设为 0 可完全关闭自动重试。``daily_quota_exceeded`` **永不**自动重试
    user_agent 便于服务端区分调用方
    allow_no_key 允许在没有任何 key 时构造实例 —— **只对免鉴权端点有意义**
               （目前仅 ``/v1/index.php``）。其它端点会返回 401 missing_api_key

    用法::

        api = PmApi()                              # 读 PM_API_KEY
        api = PmApi(api_key="pm_live_...")         # 显式传
        api = PmApi(timeout=5, max_retries=0)      # 关掉自动重试

        # 还没注册 key 时，先自举看一眼端点与套餐（index.php 免鉴权）
        api = PmApi.public()
        data, _ = api.index()
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base: Optional[str] = None,
        timeout: float = 15.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        user_agent: Optional[str] = None,
        allow_no_key: bool = False,
    ) -> None:
        key = (api_key if api_key is not None else os.environ.get("PM_API_KEY", "")) or ""
        key = key.strip()
        # allow_no_key 只对免鉴权端点（目前仅 /v1/index.php）有意义。
        if not key and not allow_no_key:
            raise PmMissingApiKey()
        self.api_key = key
        self.base = (base or os.environ.get("PM_API_BASE") or DEFAULT_BASE).rstrip("/")
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.user_agent = user_agent or "finhub-api-client-python/%s" % __version__
        #: 最近一次响应的 :class:`Meta`（方便只看数据不接返回值的写法）
        self.last_meta: Optional[Meta] = None
        self.last_headers: Dict[str, str] = {}
        #: 自动翻页时探测到的套餐 max_windows_per_call，探测一次后缓存
        self._history_page_cap: Optional[int] = None
        #: 「顶层即载荷」的提示只打一次
        self._warned_flat_envelope = False

    @classmethod
    def public(
        cls,
        base: Optional[str] = None,
        timeout: float = 15.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> "PmApi":
        """构造一个**不带 key** 的实例，用来访问免鉴权端点（``/v1/index.php``）。

        这是给「还没注册 key，先看看这个 API 有什么」这一步用的 ——
        注册页与端点清单都故意公开，能省一步流失就省一步。

        ★ 除 ``index()`` 之外的任何端点都会返回 ``401 missing_api_key``。
        ``index()`` 记得传 ``send_key=False``（本方法已经没 key，默认也不会发）。
        """
        return cls(base=base, timeout=timeout, max_retries=max_retries,
                   allow_no_key=True)

    # -- 低层 ------------------------------------------------------------

    def _build_url(self, path: str, params: Mapping[str, Any]) -> str:
        clean: Dict[str, Any] = {}
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, bool):
                clean[k] = "1" if v else "0"
            else:
                clean[k] = v
        url = "%s/%s" % (self.base, path.lstrip("/"))
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
        return url

    def request(self, path: str, send_key: bool = True,
                **params: Any) -> Tuple[Result, Meta]:
        """发一个 GET，返回 ``(data, meta)``。

        ``data`` 是 :class:`Result`（dict 子类，支持属性访问），
        ``meta`` 是 :class:`Meta`。

        失败一律抛 :class:`PmError` 子类。

        ``send_key=False`` 时不带 ``X-Api-Key`` 头 ——
        **只有 ``/v1/index.php`` 用得上**（它是全站唯一免鉴权的端点）。
        """
        url = self._build_url(path, params)
        attempt = 0
        while True:
            try:
                data, meta = self._once(url, send_key=send_key)
            except PmRateLimited as e:
                # ★ 只有 rate_limited 才值得重试。daily_quota_exceeded 走到
                #   下面那个分支（它是 PmQuotaExceeded，不是 PmRateLimited）。
                if attempt >= self.max_retries:
                    raise
                wait = e.retry_after if e.retry_after is not None else 1.0
                attempt += 1
                time.sleep(max(0.0, wait))
                continue
            # 放在这一层（而不是 _once 里）：任何走完 _once 的响应都会留下 meta
            self.last_meta = meta
            return data, meta

    def _once(self, url: str, send_key: bool = True) -> Tuple[Result, Meta]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if send_key and self.api_key:
            # 官方推荐的鉴权头。也支持 Authorization: Bearer，但本客户端
            # 用 X-Api-Key —— 不会像 ?api_key= 那样把 key 写进 access log。
            # key 为空时**不发这个头**（比发一个空的 X-Api-Key 更干净）。
            headers["X-Api-Key"] = self.api_key
        req = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                headers = {k.lower(): v for k, v in resp.headers.items()}
                raw = resp.read()
        except urllib.error.HTTPError as e:
            headers = {k.lower(): v for k, v in (e.headers or {}).items()}
            try:
                body = e.read()
            except Exception:
                body = b""
            payload = self._decode(body)
            code = str(payload.get("error") or "http_%s" % e.code)
            msg = str(payload.get("message") or "")
            retry_after = _parse_retry_after(headers.get("retry-after"))
            if retry_after is None:
                ra = payload.get("retry_after_sec")
                if isinstance(ra, (int, float)):
                    retry_after = float(ra)
            cls = _ERROR_CLASSES.get(code, PmError)
            raise cls(code, msg, status=e.code, payload=payload,
                      retry_after=retry_after) from None
        except urllib.error.URLError as e:
            # 注意：socket.timeout 在 3.10+ 是 OSError/TimeoutError 的别名，
            # 在更老的版本里 urllib 会把它包成 URLError；两条路径都要给清晰的错。
            raise PmTransportError(
                "transport_error",
                "无法连接 %s：%s（检查网络 / 代理 / 出站 443）" % (self.base, e.reason),
            ) from None
        except (TimeoutError, socket.timeout) as e:
            raise PmTransportError(
                "timeout", "请求超时（%.0fs）：%s" % (self.timeout, url)
            ) from e

        self.last_headers = headers
        payload = self._decode(raw)

        # 万一服务端在 HTTP 200 里塞了 ok:false（正常不会有，但别让调用方拿到空数据）
        if payload.get("ok") is False:
            code = str(payload.get("error") or "unknown_error")
            cls = _ERROR_CLASSES.get(code, PmError)
            retry_after = _parse_retry_after(headers.get("retry-after"))
            if retry_after is None:
                ra = payload.get("retry_after_sec")
                if isinstance(ra, (int, float)):
                    retry_after = float(ra)
            raise cls(code, str(payload.get("message") or ""), status=status,
                      payload=payload, retry_after=retry_after)

        data = payload.get("data")
        if data is not None:
            meta = Meta.from_dict(payload.get("meta"))
            if isinstance(data, dict):
                return Result(data), meta
            # 少数端点可能直接给数组（例如自描述清单），包一层保持返回类型稳定
            return Result({"items": data}), meta

        # ---- 没有 data 层：顶层就是载荷 ----
        # ★ 全站只有 /v1/index.php 这样（它的字段全在顶层，既没有 data 也没有 meta）。
        #   这里**刻意不写成 index() 的特例**：按「有没有 data」判断形状，
        #   将来站点把 index.php 统一成标准信封，这段自然就不再触发，不会二次损坏。
        self._warn_shape_once(payload, url)
        meta = Meta.from_dict(payload.get("meta"))
        if not isinstance(payload, dict):
            return Result({"items": payload}), meta
        flat = {k: v for k, v in payload.items() if k not in _ENVELOPE_KEYS}
        return Result(flat), meta

    def _warn_shape_once(self, payload: Mapping[str, Any], url: str) -> None:
        """顶层即载荷时提醒一次。

        不抛异常：这是服务端的既有形状，客户端必须能吃下；
        但也不能沉默 —— 调用方需要知道 `meta` 是空的（没有配额/限额可读）。
        """
        if self._warned_flat_envelope or not _DEBUG_FLAT_ENVELOPE:
            return
        self._warned_flat_envelope = True
        sys.stderr.write(
            "[pm_api_client] 提示：%s 的响应没有 data/meta 层，已把顶层当作载荷"
            "返回（meta 为空，读不到配额/限额）。这通常意味着该端点不走标准信封"
            "（如 /v1/index.php）。\n" % url
        )

    @staticmethod
    def _decode(raw: bytes) -> Dict[str, Any]:
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            head = raw[:200].decode("utf-8", errors="replace")
            raise PmTransportError(
                "bad_json", "响应不是合法 JSON（前 200 字节）：%s" % head
            ) from None
        return obj if isinstance(obj, dict) else {"data": obj}

    # -- 端点 ------------------------------------------------------------

    def window(
        self,
        market: Optional[str] = None,
        slug: Optional[str] = None,
        series: bool = False,
        n: Optional[int] = None,
        raw: bool = False,
    ) -> Tuple[Result, Meta]:
        """``GET /v1/window.php`` —— **主力端点**：当前（或指定）窗口的快照。

        参数
        ----
        market  ``btc``（默认）/ ``eth`` / ``sol`` / ``xrp`` / ``doge`` / ``hype`` / ``bnb``；
                也接受 ``ETH-5m`` / ``eth-updown-5m`` 写法。
                **只在没传 ``slug`` 时起作用** —— 传了 slug 就按那个 slug 取。
        slug    指定窗口，如 ``btc-updown-5m-1790389200``
        series  ``True`` = 附带时间序列（画图/做特征用）。
                当前窗口任何套餐都能用（免费档的漏斗口）；
                **历史**窗口需要套餐 ``max_samples_per_call > 0``
        n       序列最多返回点数，1~2000（超了等距抽样，**末点必留**）
        raw     ``True`` = 序列里含原始盘口字段（需套餐 ``samples_raw`` 功能，体积大）

        返回的 ``data`` 里最该先看的三个字段：
        ``price_to_beat`` / ``official_bp`` / ``beat_trusted``。
        """
        return self.request(
            "window.php",
            market=market,
            slug=slug,
            series=series or None,
            n=n if (series and n is not None) else None,
            raw=raw or None,
        )

    def settle(
        self,
        market: Optional[str] = None,
        slug: Optional[str] = None,
        last: Optional[int] = None,
    ) -> Tuple[Result, Meta]:
        """``GET /v1/settle.php`` —— 结算核对。

        * 传 ``slug``：返回单个窗口记录，载荷是 ``{"window": {...}}``。
        * 传 ``last=N``（或不传，默认 20）：返回 ``{"count": N, "windows": [...]}``。
        * ``market`` 默认 ``all``（不筛）；筛了就只回该市场。

        ``outcome`` 是官方结算结果；``rules.so/sc/to/tc`` 是开盘/收盘的现货与
        TWAP，**你可以自己复算，不用信我** —— 这是这个端点的设计意图。
        """
        return self.request("settle.php", market=market, slug=slug, last=last)

    def history(
        self,
        market: Optional[str] = None,
        frm: Optional[Union[int, str]] = None,
        to: Optional[Union[int, str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        compact: bool = False,
    ) -> Tuple[Result, Meta]:
        """``GET /v1/history.php`` —— 历史窗口列表（新的在前，支持分页）。

        参数
        ----
        market  ``all``（默认，不筛）或某个市场短名
        frm/to  unix 秒，或 ``2026-09-22`` 这种日期串
        limit   上限 = 套餐 ``max_windows_per_call``（免费档默认 20）；
                ★ **不要写死 200** —— 超出上限会被接口收敛或报错
        offset  分页偏移
        compact ``True`` = 只返回 slug / window_start / outcome / n_samples

        载荷里 ``total`` 是符合条件的总数，``older_available`` 便于翻页。

        注意：**范围超限会整体 403**（``history_depth_exceeded``），
        接口不做静默截断 —— 少给数据不报错比报错更糟。
        """
        return self.request(
            "history.php",
            market=market,
            **{"from": frm, "to": to},
            limit=limit,
            offset=offset,
            compact=compact or None,
        )

    def samples(
        self,
        slug: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        tail: bool = False,
        raw: bool = False,
    ) -> Tuple[Result, Meta]:
        """``GET /v1/samples.php`` —— 某窗口的**原始 2 秒样本**。

        需要套餐 ``max_samples_per_call > 0``（免费档是 0，会 403
        ``samples_not_in_plan``）。

        参数
        ----
        slug   必填
        limit  上限 = 套餐 ``max_samples_per_call``
        tail   ``True`` = 取最后 N 条（做实时最常用）
        raw    ``True`` = 返回原始 JSONL 字段；否则返回加工后的字段
        """
        return self.request(
            "samples.php", slug=slug, limit=limit, offset=offset,
            tail=tail or None, raw=raw or None,
        )

    def stats(self) -> Tuple[Result, Meta]:
        """``GET /v1/stats.php`` —— 预聚合统计 + 完整健康度。**免费档可用**。

        用来快速判断数据质量与规则表现，不用先买历史。
        """
        return self.request("stats.php")

    def index(self, send_key: bool = True) -> Tuple[Result, Meta]:
        """``GET /v1/index.php`` —— 端点清单 / 市场清单 / 套餐表（客户端自举用）。

        适合在启动时读一次，把可用市场与限额拿到手，而不是把路径硬编码。

        ★★ **本端点是全站唯一不走标准信封的端点。**
        ------------------------------------------------------------------
        其它 ``/v1/*`` 一律是 ``{ok, data, meta}``；``index.php`` 是**扁平**的，
        **既没有 ``data`` 也没有 ``meta``**，所有字段都在**顶层**：

            ok, service, note, auth, endpoints, markets, market_codes,
            response_envelope, error_codes, plans, health, server_ts

        所以本方法返回的 ``data`` 就是那个顶层字典本身：::

            data, meta = api.index()
            data["markets"]       # ['btc-updown-5m', 'eth-updown-5m', ...]
            data["market_codes"]  # ['btc', 'eth', 'sol', 'xrp', 'doge', 'hype', 'bnb']
            data["plans"]         # 套餐表
            meta.plan, meta.quota # ★ 都是空的 —— 本端点没有 meta

        本客户端对「有/没有 ``data`` 层」两种形状都能解析（按形状判断，
        不是给这个端点开特例），所以将来站点把 ``index.php`` 统一成标准信封，
        这里也不会有任何破坏。

        参数
        ----
        send_key  ``False`` = 不带 ``X-Api-Key`` 头发请求。``index.php`` 是
                  **免鉴权**的，所以即使没有 key 也能自举（此时可以配
                  ``PmApi(api_key='')``… 但构造器要求非空 key，见 ``allow_no_key``
                  说明；常规用法是带着 key 发，服务端会忽略它）。
        """
        return self.request("index.php", send_key=send_key)

    # -- 便捷封装 --------------------------------------------------------

    def current_window(
        self, market: Optional[str] = None, series: bool = False, n: Optional[int] = None
    ) -> Tuple[Result, Meta]:
        """:meth:`window` 的语义别名 —— 明确表示"当前窗口"。"""
        return self.window(market=market, series=series, n=n)

    def iter_history(
        self,
        market: Optional[str] = None,
        frm: Optional[Union[int, str]] = None,
        to: Optional[Union[int, str]] = None,
        page_size: Optional[int] = None,
        compact: bool = True,
        max_pages: int = 100,
    ):
        """按页迭代 ``history.php``，自动翻页（生成器）。

        ``page_size`` 不传就用套餐的 ``max_windows_per_call``（从第一页的
        ``meta.limits`` 读），所以免费档也不会因为写死 200 而报错。

        「范围超限整体 403」这条规则在这里同样适用：一旦请求到超出
        ``history_days`` 的页，会抛 :class:`PmError`（``history_depth_exceeded``），
        调用方自行 catch 即可停止。
        """
        offset = 0
        pages = 0
        limit = page_size
        # 不知道套餐上限时，第一次只请求 1 条，把 meta.limits 拿到手再决定页大小。
        # 写死 limit=200 在免费档（max_windows_per_call=20）会被接口收敛甚至报错。
        # 探测结果缓存在实例上，同一个 PmApi 再翻页就不用重复探测了。
        while pages < max_pages:
            if limit is None:
                if self._history_page_cap is None:
                    _, meta0 = self.history(
                        market=market, frm=frm, to=to, limit=1, offset=0, compact=True
                    )
                    self._history_page_cap = max(1, meta0.max_windows_per_call or 20)
                limit = min(200, self._history_page_cap)
            data, meta = self.history(
                market=market, frm=frm, to=to, limit=limit, offset=offset, compact=compact
            )
            rows = data.get("windows") or []
            if not rows:
                return
            yield data, meta
            pages += 1
            offset += len(rows)
            if not data.get("older_available"):
                return


# ---------------------------------------------------------------------------
# 给 example / 交互式使用的小工具（不属于对外 API 契约，但很省事）
# ---------------------------------------------------------------------------

def build_api(
    argv_key: Optional[str] = None,
    base: Optional[str] = None,
    timeout: float = 15.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> PmApi:
    """``--api-key`` 优先，其次环境变量 ``PM_API_KEY``；都没有就清晰报错退出。

    报错信息里会告诉你**去哪注册免费 key**。
    """
    try:
        return PmApi(api_key=argv_key, base=base, timeout=timeout, max_retries=max_retries)
    except PmMissingApiKey as e:
        raise SystemExit(
            "%s\n\n提示：\n"
            "  1) 打开 %s 注册并领取免费 key（free 档 0 元）\n"
            "  2) Linux/macOS:  export PM_API_KEY=pm_live_xxxxxxxx\n"
            "     Windows:      set PM_API_KEY=pm_live_xxxxxxxx\n"
            "  3) 或者每次显式传：--api-key pm_live_xxxxxxxx\n"
            % (e, REGISTER_URL)
        ) from None


def add_common_args(parser, default_market: Optional[str] = None) -> None:
    """给 example 的 argparse 挂上统一的 ``--api-key`` / ``--base`` / ``--market``。

    注意：``--help`` 由 argparse 在任何网络请求之前处理，
    所以即使没设 key，``python examples/xxx.py --help`` 也一定能正常打印。
    """
    g = parser.add_argument_group("连接")
    g.add_argument("--api-key", default=None,
                   help="API key；不传则读环境变量 PM_API_KEY")
    g.add_argument("--base", default=None,
                   help="服务基线（默认 %s，也可用 PM_API_BASE）" % DEFAULT_BASE)
    g.add_argument("--timeout", type=float, default=15.0, help="单次请求超时秒数（默认 15）")
    if default_market is not None:
        g.add_argument("--market", default=default_market, choices=list(MARKETS),
                       help="市场短名（默认 %s）" % default_market)


def quota_line(meta: Optional[Meta]) -> str:
    """一行配额摘要，给 CLI 输出用。"""
    if meta is None:
        return ""
    q = meta.quota
    if q.is_unlimited:
        return "  [配额] 不限量（%s 档）" % (meta.plan or "?")
    pct = q.remaining_ratio * 100.0
    tail = ""
    if q.should_back_off():
        tail = "  ← 剩余不足 10%，请降频！"
    return "  [配额] %s/%s 已用，剩 %s（%.1f%%）%s" % (
        q.used_today, q.daily_limit, q.remaining, pct, tail,
    )


def beat_line(data: Mapping[str, Any]) -> str:
    """★★ ``price_to_beat`` 的可信度提示 —— 一定要看这一行。

    ``beat_trusted == False`` 时接口给的是**自算退化值**（系统性偏约 3bp，
    而 60 秒真实位移中位数只有 1.83bp）—— 此时拿它判方向是不可信的。
    """
    trusted = data.get("beat_trusted")
    src = data.get("beat_source")
    if trusted is True:
        return "  [beat] 来源=%s  ✅ 可信（官方 Chainlink 读数）" % src
    if trusted is False:
        return ("  [beat] 来源=%s  ⚠️ 不可信：官方读数缺失，price_to_beat 退化成"
                "自算值（偏约 3bp），**不要**用它判方向" % src)
    return "  [beat] 来源=%s（响应里没有 beat_trusted 字段）" % src


def parse_when(value: Optional[str]) -> Optional[Union[int, str]]:
    """``--from`` / ``--to`` 的统一解析：纯数字当 unix 秒，否则当日期串透传。

    接口自己就认 ``2026-09-22`` 和 unix 秒两种写法，所以这里只做区分不做转换。
    """
    if value is None:
        return None
    v = value.strip()
    if v == "":
        return None
    if v.lstrip("-").isdigit():
        return int(v)
    return v


def print_error(e: PmError, file=None) -> None:
    """把 :class:`PmError` 打印成人能看懂的样子（并区分两种 429）。"""
    import sys as _sys

    out = file or _sys.stderr
    print("接口报错: %s" % e, file=out)
    if e.is_rate_limited:
        wait = e.retry_after if e.retry_after is not None else 1.0
        print("  → 超过 QPS 上限。建议 sleep %.1fs 后重试（本客户端默认已自动重试）。" % wait,
              file=out)
    elif e.is_quota_exceeded:
        print("  → 当日配额用完，**重试没有意义**。请等到 %s（UTC 零点）再跑，"
              "或升级套餐。" % (e.reset_at or "明天 UTC 零点"), file=out)
    if e.available_markets:
        print("  → 可用市场：%s" % ", ".join(e.available_markets), file=out)
    if e.code in ("missing_api_key", "invalid_api_key"):
        print("  → 到 %s 领取免费 key。" % REGISTER_URL, file=out)
    if e.code in ("samples_not_in_plan", "history_not_in_plan",
                  "history_depth_exceeded", "plan_feature_denied"):
        print("  → 该功能/深度不在当前套餐内，见 %s" % ENDPOINTS_URL, file=out)


__all__ = [
    "PmApi",
    "PmError",
    "PmMissingApiKey",
    "PmRateLimited",
    "PmQuotaExceeded",
    "PmTransportError",
    "Result",
    "Meta",
    "Quota",
    "Feed",
    "MARKETS",
    "MARKET_PREFIXES",
    "DEFAULT_BASE",
    "REGISTER_URL",
    "DOCS_URL",
    "ENDPOINTS_URL",
    "SAMPLE_INTERVAL_SEC",
    "ERR_RATE_LIMITED",
    "ERR_DAILY_QUOTA_EXCEEDED",
    "DEFAULT_MAX_RETRIES",
    "build_api",
    "add_common_args",
    "quota_line",
    "beat_line",
    "parse_when",
    "print_error",
]
