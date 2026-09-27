#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
examples/settle_recent.py —— 取最近 N 个已结算窗口，用来核对结算
============================================================================
这个例子演示什么
----------------
1. ``PmApi().settle(market=..., last=N)`` 拿最近 N 个**已结算**窗口；
2. 打印官方 ``outcome``（UP/DOWN）以及采集器算的四条规则 ``rules.A/B/C/D``
   和它们是否一致（``agree``）；
3. ``rules.so/sc/to/tc`` = 开盘现货 / 收盘现货 / 开盘 TWAP / 收盘 TWAP，
   ``--verify`` 会**用这四个数在本地自己复算方向**并与官方 outcome 对比 ——
   这个端点存在的意义就是"你不用信我，自己算"；
4. 统计最近 N 个窗口的方向分布，快速感知这段行情是单边还是震荡。

用法
----
    export PM_API_KEY=pm_live_xxxxxxxx
    python examples/settle_recent.py                # 全部市场最近 10 个
    python examples/settle_recent.py -n 30 --market btc
    python examples/settle_recent.py --slug btc-updown-5m-1790389200  # 单个窗口
    python examples/settle_recent.py --verify       # 本地复算并对比
    python examples/settle_recent.py --json

说明
----
这是**最便宜**的端点（一行 JSON，无计算），适合做结算对账。
``last`` 取值范围 1~500；不传时接口默认 20。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pm_api_client import (  # noqa: E402
    MARKETS,
    PmError,
    add_common_args,
    build_api,
    print_error,
    quota_line,
)


#: rules 里的四个价格字段：
#:   so = 开盘现货(settle open spot)   sc = 收盘现货(settle close spot)
#:   to = 开盘 TWAP(settle open twap)  tc = 收盘 TWAP(settle close twap)
#: 「边界点假设」是最朴素的复算口径：收盘 > 开盘 即 UP。
LOCAL_RULES = {
    "A": ("so", "sc"),
    "B": ("to", "tc"),
    "C": ("so", "tc"),
    "D": ("to", "sc"),
}


def local_direction(rule, pair):
    """用 rules 里的 so/sc/to/tc 在本地复算方向（不依赖服务端的 outcome）。

    ★ 这是**故意朴素**的「边界点假设」：只比开盘和收盘两个点。
    真实市场规则比的是 Chainlink TWAP 在**整个区间**上的均值与区间起点，
    所以本地复算与官方 outcome 存在一致率差异 —— 那正是这个 API 要帮你
    度量的东西（见响应里的 ``fam``：不同假设下的胜率）。
    """
    a_key, b_key = pair
    a, b = rule.get(a_key), rule.get(b_key)
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return None
    if a == b:
        return "FLAT"
    return "UP" if b > a else "DOWN"


def local_all_rules(rule):
    """返回 ``{"A": "UP", "B": "DOWN", ...}``，缺字段的规则为 None。"""
    return {name: local_direction(rule, pair) for name, pair in LOCAL_RULES.items()}


def main():
    ap = argparse.ArgumentParser(
        description="取最近 N 个已结算窗口（官方 outcome + 四条规则读数）",
        epilog="文档：https://api.wanminguo.top/polymarket/docs.php",
    )
    add_common_args(ap)  # settle 的 market 默认是 all，所以这里不加 --market 的 choices
    ap.add_argument("--market", default="all",
                    help="all（默认，不筛）或 %s" % "/".join(MARKETS))
    ap.add_argument("-n", "--last", type=int, default=10,
                    help="最近 N 个已结算窗口（1~500，默认 10）")
    ap.add_argument("--slug", default=None, help="只取某一个窗口的结算记录")
    ap.add_argument("--verify", action="store_true",
                    help="用 rules.so/sc/to/tc 在本地复算四条规则并与官方 outcome 比一致率")
    ap.add_argument("--json", action="store_true", help="打印原始 JSON")
    ap.add_argument("--retries", type=int, default=2, help="429 rate_limited 自动重试次数")
    a = ap.parse_args()

    if a.last < 1 or a.last > 500:
        ap.error("--last 必须在 1~500 之间（接口的上限）")

    api = build_api(argv_key=a.api_key, base=a.base, timeout=a.timeout,
                    max_retries=a.retries)

    try:
        if a.slug:
            data, meta = api.settle(slug=a.slug)
        else:
            data, meta = api.settle(
                market=None if a.market == "all" else a.market, last=a.last)
    except PmError as e:
        print_error(e)
        return 1

    if a.json:
        print(json.dumps({"ok": True, "data": dict(data), "meta": meta.raw},
                         ensure_ascii=False, indent=2))
        return 0

    if a.slug:
        # slug 模式载荷是 {"window": {...}}，不是 {"windows": [...]}
        w = data.get("window") or {}
        print(json.dumps(w, ensure_ascii=False, indent=2))
        print(quota_line(meta))
        return 0

    rows = data.get("windows") or []
    print("市场 %s —— 最近 %s 个已结算窗口（接口返回 %s 条）"
          % (a.market, a.last, data.get("count")))
    print()
    print("%-30s %-6s %-5s %-5s %s" % ("slug", "outcome", "样本", "规则", "备注"))
    print("-" * 78)

    tally = {"UP": 0, "DOWN": 0, "FLAT": 0, None: 0}
    agree_rules = {name: 0 for name in LOCAL_RULES}
    for w in rows:
        rules = w.get("rules") or {}
        outcome = w.get("outcome")
        tally[outcome if outcome in tally else None] += 1
        note = "开盘偏移=%s 订单=%s/%s" % (
            w.get("open_offset"), w.get("used"), w.get("orders"))
        if a.verify:
            guesses = local_all_rules(rules)
            for name, g in guesses.items():
                if g is not None and g == outcome:
                    agree_rules[name] += 1
            shown = " ".join(
                "%s=%s" % (name, guesses[name] or "?") for name in sorted(LOCAL_RULES))
            note = "本地复算 %s（官方=%s）" % (shown, outcome)
        print("%-30s %-6s %-5s %-5s %s" % (
            w.get("slug"), outcome if outcome is not None else "-",
            w.get("n_samples"), "一致" if rules.get("agree") else "分歧", note))

    print()
    print("方向分布：UP=%s DOWN=%s FLAT=%s 未知=%s"
          % (tally["UP"], tally["DOWN"], tally["FLAT"], tally[None]))
    if a.verify and rows:
        print("本地边界点复算 vs 官方 outcome 的一致率（共 %s 条）：" % len(rows))
        for name in sorted(LOCAL_RULES):
            a_key, b_key = LOCAL_RULES[name]
            print("  规则 %s（%s vs %s）  %s/%s" % (
                name, a_key, b_key, agree_rules[name], len(rows)))
        print("  ↳ 规则 B（TWAP 开/收）最接近官方口径，但也**不等于**官方口径：")
        print("     官方看的是 Chainlink TWAP 在**整个区间**上的值与区间起点。")
        print("     一致率低不代表数据有问题 —— 这正是「边界点假设」的偏差。")

    print()
    print(quota_line(meta))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
