#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
examples/history.py —— 按时间范围拉历史窗口列表（含分页）
============================================================================
这个例子演示什么
----------------
1. ``PmApi().history(...)`` 按 ``from`` / ``to`` 拉历史窗口列表，新的在前；
2. **分页**：演示 ``limit`` + ``offset`` 的手动翻页，
   以及用 :meth:`PmApi.iter_history` 自动翻页（生成器，逐页吐出）——
   自动翻页会先读 ``meta.limits.max_windows_per_call`` 决定页大小，
   所以免费档（上限 20）也不会因为写死 ``limit=200`` 而报错；
3. ``--compact`` 只取 slug / window_start / outcome / n_samples（省流量）；
4. ★ **范围超限会整体 403**（``history_depth_exceeded``），接口不做静默截断 ——
   本例子会捕获它并明确告诉你"超出套餐可回溯天数"，而不是悄悄少给数据。

用法
----
    export PM_API_KEY=pm_live_xxxxxxxx
    python examples/history.py --days 1                     # 最近 1 天
    python examples/history.py --from 2026-09-20 --to 2026-09-22
    python examples/history.py --market eth --days 3 --limit 50
    python examples/history.py --days 7 --pages 5           # 自动翻 5 页
    python examples/history.py --from 1789866900 --to 1789867000 --json

说明
----
* ``from`` / ``to`` 既接受 unix 秒，也接受 ``2026-09-22`` 这种日期串。
* ``limit`` 的上限 = 套餐 ``max_windows_per_call``（免费档 20，pro 档 2000）。
* 套餐的 ``history_days`` 决定能回溯多久：free=1 天，basic=7 天，pro=90 天。
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pm_api_client import (  # noqa: E402
    PmError,
    add_common_args,
    build_api,
    parse_when,
    print_error,
    quota_line,
)


def fmt_span(start_ts):
    """把 unix 秒格式化成「本地时间 + 距今多久」。"""
    if not isinstance(start_ts, (int, float)) or start_ts <= 0:
        return "-"
    local = time.strftime("%m-%d %H:%M:%S", time.localtime(start_ts))
    age_min = (time.time() - start_ts) / 60.0
    if age_min < 90:
        return "%s (%d 分钟前)" % (local, int(age_min))
    return "%s (%.1f 小时前)" % (local, age_min / 60.0)


def main():
    ap = argparse.ArgumentParser(
        description="按时间范围拉历史窗口列表（支持分页）",
        epilog="文档：https://api.wanminguo.top/polymarket/docs.php",
    )
    add_common_args(ap)
    ap.add_argument("--market", default="all",
                    help="all（默认，不筛）或 btc/eth/sol/xrp/doge/hype/bnb")
    ap.add_argument("--from", dest="frm", default=None,
                    help="起始时间：unix 秒或 2026-09-22（含）")
    ap.add_argument("--to", dest="to", default=None,
                    help="结束时间：unix 秒或 2026-09-22（含）")
    ap.add_argument("--days", type=float, default=None,
                    help="便捷写法：等价于 --from <now - N 天>；与 --from 互斥")
    ap.add_argument("--limit", type=int, default=None,
                    help="每页条数（不传由接口按套餐上限决定）")
    ap.add_argument("--offset", type=int, default=0, help="分页偏移（默认 0）")
    ap.add_argument("--pages", type=int, default=1,
                    help="自动翻页的页数上限（默认 1 = 只取一页）")
    ap.add_argument("--compact", action="store_true",
                    help="只要 slug/window_start/outcome/n_samples，省流量")
    ap.add_argument("--json", action="store_true", help="打印原始 JSON")
    ap.add_argument("--retries", type=int, default=2, help="429 rate_limited 自动重试次数")
    a = ap.parse_args()

    if a.days is not None and a.frm is not None:
        ap.error("--days 与 --from 互斥，选一个")

    frm = parse_when(a.frm)
    to = parse_when(a.to)
    if a.days is not None:
        frm = int(time.time() - a.days * 86400)
        print("提示：--days %s 已转换为 from=%s（%s）"
              % (a.days, frm, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(frm))))

    if a.pages < 1:
        ap.error("--pages 至少为 1")

    api = build_api(argv_key=a.api_key, base=a.base, timeout=a.timeout,
                    max_retries=a.retries)
    market = None if a.market == "all" else a.market
    dumped = []

    try:
        if a.pages > 1:
            # ★ 自动翻页：逐页 yield，调用方可以边收边处理（这里只打印）
            page = 0
            for data, meta in api.iter_history(
                market=market, frm=frm, to=to,
                page_size=a.limit, compact=a.compact, max_pages=a.pages,
            ):
                page += 1
                rows = data.get("windows") or []
                if a.json:
                    dumped.append({"ok": True, "data": dict(data), "meta": meta.raw})
                else:
                    print("── 第 %s 页：本页 %s 条，累计 %s/%s，offset=%s，"
                          "还有更老的吗=%s ──"
                          % (page, len(rows), data.get("offset", 0) + len(rows),
                             data.get("total"), data.get("offset"),
                             data.get("older_available")))
                    for w in rows:
                        print("   %-32s %-5s %s" % (
                            w.get("slug"), w.get("outcome") or "-",
                            fmt_span(w.get("window_start"))))
                if a.json is False:
                    print(quota_line(meta))
                if meta.should_back_off():
                    print("★ 配额剩余不足 10%%，已停止翻页（别等撞 429）")
                    break
            if a.json:
                print(json.dumps(dumped, ensure_ascii=False, indent=2))
            return 0

        # ---- 单页（手动 offset 分页的原语）----
        data, meta = api.history(
            market=market, frm=frm, to=to, limit=a.limit,
            offset=a.offset, compact=a.compact,
        )
    except PmError as e:
        print_error(e)
        if e.code == "history_depth_exceeded":
            print("  → 你请求的范围比套餐的 history_days 更老。"
                  "缩小 --days，或把 --from 往后挪。", file=sys.stderr)
        return 1

    if a.json:
        print(json.dumps({"ok": True, "data": dict(data), "meta": meta.raw},
                         ensure_ascii=False, indent=2))
        return 0

    rows = data.get("windows") or []
    print("市场=%s  count=%s  total=%s  offset=%s  limit=%s  compact=%s"
          % (data.get("market"), data.get("count"), data.get("total"),
             data.get("offset"), data.get("limit"), a.compact))
    print("范围：from=%s to=%s" % (data.get("from"), data.get("to")))
    print()
    for w in rows:
        print("   %-32s %-5s 样本%-5s %s" % (
            w.get("slug"), w.get("outcome") or "-",
            w.get("n_samples") if w.get("n_samples") is not None else "-",
            fmt_span(w.get("window_start"))))
    if not rows:
        print("   （空：这个范围内没有数据，或者分页 offset 已经到底了）")
    print()
    print("older_available=%s  → 下一页请加 --offset %s"
          % (data.get("older_available"), (data.get("offset") or 0) + len(rows)))
    print(quota_line(meta))
    print("套餐可回溯 %s 天（free=1 / basic=7 / pro=90）" % meta.history_days)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
