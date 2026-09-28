#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
examples/current_window.py —— 取「当前窗口」的实时快照
============================================================================
这个例子演示什么
----------------
1. 用 ``PmApi().window(market=...)`` 取**当前** 5 分钟涨跌盘窗口；
2. 打印结算方向的三个直接输入：``price_to_beat`` / ``official_bp`` /
   ``spot_bp``，以及 ``leading_side``；
3. ★ **如实读取 `beat_trusted`**：``False`` 时官方 Chainlink 读数没进来，
   ``price_to_beat`` 退化成自算值（系统性偏约 3bp），此时不要用它判方向；
4. ``--series`` 拉时间序列（``series=1``）并用**纯标准库**画一条 ASCII 走势图 —— 
   当前窗口的序列**免费档也能拿**（这是官方的漏斗口），历史窗口才需要样本权限；
5. 每次读 ``meta.quota``，剩余不足 10% 时提示降频 —— 别等撞 429。

用法
----
    export PM_API_KEY=pm_live_xxxxxxxx
    python examples/current_window.py                 # 默认 btc
    python examples/current_window.py --market eth    # 换市场
    python examples/current_window.py --series --n 60 # 带序列 + ASCII 图
    python examples/current_window.py --json          # 打印原始 JSON

注意
----
轮询本端点的合理频率是 1~2 秒（数据本身就是 2 秒一条）。
但 ``free`` 档只有 500 次/天，2 秒轮询约 17 分钟就打光 —— 要持续轮询请升级套餐。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pm_api_client import (  # noqa: E402
    PmError,
    add_common_args,
    beat_line,
    build_api,
    print_error,
    quota_line,
)


def ascii_chart(values, width=64, height=9, label="official_bp"):
    """用纯标准库画一条极简走势图（不引 matplotlib，保持零依赖）。"""
    pts = [v for v in values if isinstance(v, (int, float))]
    if len(pts) < 2:
        return "(序列点不足，无法画图)"
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    rows = []
    for r in range(height, 0, -1):
        thr = lo + span * (r - 1) / (height - 1)
        line = []
        for i, v in enumerate(pts):
            col = int(round(i * (width - 1) / max(1, len(pts) - 1)))
            while len(line) < col:
                line.append(" ")
            line.append("#" if v >= thr else " ")
        rows.append("".join(line).rstrip())
    head = "  %s %.2f ~ %.2f（%d 点）" % (label, lo, hi, len(pts))
    axis = "  0" + "-" * (width - 6) + "%d" % (len(pts) - 1)
    return "\n".join(["  " + r for r in rows] + [head, axis])


def main():
    ap = argparse.ArgumentParser(
        description="取当前 5 分钟涨跌盘窗口的实时快照（含 price to beat 与三个 bp）",
        epilog="文档：https://api.wanminguo.top/quant/polymarket/docs.php",
    )
    add_common_args(ap, default_market="btc")
    ap.add_argument("--slug", default=None, help="指定窗口 slug（默认取最新）")
    ap.add_argument("--series", action="store_true",
                    help="附带时间序列（series=1），当前窗口免费档也能用")
    ap.add_argument("--n", type=int, default=120, help="序列最多返回点数（1~2000，默认 120）")
    ap.add_argument("--json", action="store_true", help="打印原始 JSON 而不是表格")
    ap.add_argument("--retries", type=int, default=2,
                    help="遇 429 rate_limited 自动重试次数（默认 2；0 = 关闭）")
    a = ap.parse_args()

    api = build_api(argv_key=a.api_key, base=a.base, timeout=a.timeout,
                    max_retries=a.retries)

    try:
        data, meta = api.window(market=a.market, slug=a.slug,
                                series=a.series, n=a.n if a.series else None)
    except PmError as e:
        print_error(e)
        return 1

    if a.json:
        print(json.dumps({"ok": True, "data": dict(data),
                          "meta": meta.raw}, ensure_ascii=False, indent=2))
        return 0

    print("窗口 %s" % data.get("slug"))
    print("  市场        %s" % data.get("market"))
    print("  剩余        %s 秒" % data.get("remaining"))
    print("  样本数      %s 条（约 2 秒一条）" % data.get("n_samples"))
    print("  数据延迟    %s 秒" % data.get("sample_age_sec"))
    print()
    print("── 结算方向的三类输入 ─────────────────────────────────────")
    print("  price to beat   %s" % data.get("price_to_beat"))
    print("  官方 TWAP60     %s   （%+.2f bp）" % (
        data.get("official"), data.get("official_bp") or 0.0))
    print("  现货中位        %s   （%+.2f bp，%s 家源）" % (
        data.get("spot_composite"), data.get("spot_bp") or 0.0,
        data.get("spot_n_sources")))
    print("  60 秒动量       %+.2f bp" % (data.get("momentum_bp") or 0.0))
    print("  领先方向        %s" % data.get("leading_side"))
    print("  市场隐含 UP     %s" % data.get("implied_up"))
    print()
    print(beat_line(data))
    vbp = data.get("venue_bp") or {}
    if vbp:
        print("  [各源 bp] %s" % "  ".join(
            "%s=%+.2f" % (k, v) for k, v in sorted(vbp.items())))
    if data.get("settled"):
        s = data["settled"]
        rules = s.get("rules") or {}
        print("  [已结算] 官方 outcome = %s（规则一致=%s）"
              % (s.get("outcome"), rules.get("agree")))

    if a.series:
        series = data.get("series") or []
        print()
        print("── 官方 bp 走势（series_points_total=%s，返回 %s 点）────────"
              % (data.get("series_points_total"), data.get("series_points_returned")))
        print(ascii_chart([p.get("official_bp") for p in series]))
        print("  ↑ 正值 = 官方 TWAP60 在 price to beat 之上（偏 UP）")

    print()
    print(quota_line(meta))
    print("  数据新鲜度 feed.ok=%s latest_age=%ss"
          % (meta.feed.ok, meta.feed.latest_age_sec))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
