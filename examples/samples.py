#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
examples/samples.py —— 拉某个窗口的原始 2 秒样本
============================================================================
这个例子演示什么
----------------
1. ``PmApi().samples(slug, ...)`` 拉某窗口的**原始 2 秒样本**，
   这是做回测/对齐/复算的最小数据单元；
2. ``--tail`` 取最后 N 条（做实时最常用：只看最近几拍的官方 vs 现货 bp）；
3. ``--raw`` 返回采集器**原始 JSONL 字段**（含六家现货各自报价、盘口），
   不加这个参数时返回的是加工后的派生字段；
4. 把样本按 ``--csv`` 导出成 CSV，方便直接拖进 Excel / pandas；
5. ★ 这个端点需要套餐 ``max_samples_per_call > 0``（**免费档是 0**，会 403
   ``samples_not_in_plan``）—— 例子会把这件事讲清楚，而不是抛个栈了事。

用法
----
    export PM_API_KEY=pm_live_xxxxxxxx
    python examples/samples.py --slug btc-updown-5m-1790389200
    python examples/samples.py --slug <slug> --tail 30          # 最后 30 条
    python examples/samples.py --slug <slug> --raw --limit 20   # 原始字段
    python examples/samples.py --slug <slug> --limit 200 --csv samples.csv
    python examples/samples.py --slug <slug> --json

提示
----
不知道该填什么 slug？先用 examples/current_window.py 或
examples/history.py 拿一个（slug 形如 ``btc-updown-5m-1790389200``，
末段是窗口开始的 unix 秒）。
"""

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pm_api_client import (  # noqa: E402
    PmError,
    add_common_args,
    build_api,
    print_error,
    quota_line,
)

# 导出 CSV 时优先取这些字段（不存在就跳过）
PREFERRED_COLS = [
    "ts", "remaining", "price_to_beat", "beat_source", "beat_trusted",
    "official", "official_bp", "spot_composite", "spot_bp", "momentum_bp",
    "leading_side", "spot_n_sources", "implied_up", "liquidity",
    "computed_twap60", "computed_bp",
]


def pick_columns(rows):
    """列 = 优先字段（有值的） + 其他出现的标量字段。"""
    seen = []
    for c in PREFERRED_COLS:
        if any(c in r for r in rows):
            seen.append(c)
    for r in rows:
        for k, v in r.items():
            if k in seen:
                continue
            if isinstance(v, (dict, list)):
                continue  # 嵌套结构不进 CSV
            seen.append(k)
    return seen


def main():
    ap = argparse.ArgumentParser(
        description="拉某个窗口的原始 2 秒样本（需套餐支持）",
        epilog="文档：https://api.wanminguo.top/quant/polymarket/docs.php",
    )
    add_common_args(ap)  # samples 按 slug 取数，所以没有 --market
    ap.add_argument("--slug", required=False, default=None,
                    help="窗口 slug，如 btc-updown-5m-1790389200（必填）")
    ap.add_argument("--limit", type=int, default=None,
                    help="条数（不传由接口按套餐上限决定，默认上限 200）")
    ap.add_argument("--offset", type=int, default=0, help="分页偏移（默认 0）")
    ap.add_argument("--tail", action="store_true", help="取最后 N 条（做实时最常用）")
    ap.add_argument("--raw", action="store_true",
                    help="返回采集器原始 JSONL 字段（含各家现货报价）")
    ap.add_argument("--csv", dest="csv_path", default=None,
                    help="把样本导出到这个 CSV 文件")
    ap.add_argument("--json", action="store_true", help="打印原始 JSON")
    ap.add_argument("--retries", type=int, default=2, help="429 rate_limited 自动重试次数")
    a = ap.parse_args()

    if not a.slug:
        ap.error("--slug 是必填的（接口要求）。先用 examples/current_window.py "
                 "或 examples/history.py 拿一个 slug")

    api = build_api(argv_key=a.api_key, base=a.base, timeout=a.timeout,
                    max_retries=a.retries)

    try:
        data, meta = api.samples(slug=a.slug, limit=a.limit, offset=a.offset,
                                 tail=a.tail, raw=a.raw)
    except PmError as e:
        print_error(e)
        if e.code == "samples_not_in_plan":
            print("  → 免费档不含原始样本端点。当前窗口的 series 用 "
                  "examples/current_window.py --series 就能拿（免费档可用）。",
                  file=sys.stderr)
        return 1

    if a.json:
        print(json.dumps({"ok": True, "data": dict(data), "meta": meta.raw},
                         ensure_ascii=False, indent=2))
        return 0

    rows = data.get("samples") or []
    print("窗口 %s" % data.get("slug"))
    print("  样本总数 %s 条，本次返回 %s 条（offset=%s limit=%s tail=%s raw=%s）"
          % (data.get("total"), data.get("count"), data.get("offset"),
             data.get("limit"), data.get("tail"), data.get("raw")))
    print("  采样节奏约 2 秒一条；一整个 5 分钟窗口约 150 条")
    print()

    if rows:
        # 头部摘要：第一拍与最后一拍的对比，最能说明这一盘怎么走的
        first, last = rows[0], rows[-1]
        print("  首拍 ts=%s  官方 bp=%s  现货 bp=%s  剩余=%ss"
              % (first.get("ts"), first.get("official_bp"),
                 first.get("spot_bp"), first.get("remaining")))
        print("  末拍 ts=%s  官方 bp=%s  现货 bp=%s  剩余=%ss"
              % (last.get("ts"), last.get("official_bp"),
                 last.get("spot_bp"), last.get("remaining")))
        nb = last.get("beat_trusted")
        if nb is not None:
            print("  末拍 beat_trusted=%s（False 表示该窗口官方读数缺失，"
                  "beat 是自算退化值）" % nb)
        print()

        show = rows[-10:] if len(rows) > 10 else rows
        print("  最后 %s 条：" % len(show))
        print("  %-16s %-9s %-11s %-11s %-8s" % (
            "ts", "remaining", "official_bp", "spot_bp", "implied_up"))
        for s in show:
            print("  %-16s %-9s %-11s %-11s %-8s" % (
                s.get("ts"),
                "%6.1f" % s["remaining"] if isinstance(s.get("remaining"), (int, float)) else "-",
                "%+9.2f" % s["official_bp"] if isinstance(s.get("official_bp"), (int, float)) else "-",
                "%+9.2f" % s["spot_bp"] if isinstance(s.get("spot_bp"), (int, float)) else "-",
                s.get("implied_up") if s.get("implied_up") is not None else "-",
            ))
    else:
        print("  （没有样本：该窗口可能还没开始采集，或 offset 已经越过末尾）")

    if a.csv_path:
        cols = pick_columns(rows)
        with open(a.csv_path, "w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            wr.writeheader()
            for r in rows:
                wr.writerow({k: r.get(k) for k in cols})
        print()
        print("已导出 %s 行 × %s 列 → %s" % (len(rows), len(cols), a.csv_path))

    print()
    print(quota_line(meta))
    print("套餐 max_samples_per_call=%s（0 = 不含原始样本）"
          % meta.max_samples_per_call)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
