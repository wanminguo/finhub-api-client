#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_bt.py —— 按**客户端真实执行口径**回测信号产品（只读，不写任何业务文件）
================================================================================
为什么单独做一份（而不是复用 lr_p0.py）：
  lr_p0.py 分析的是**采集器自己的引擎**（7 个市场、多种仓位模式、滑点网格）。
  卖出去的产品是**客户端的口径**，和它不一样：
    · 只卖 BTC-5m（其它市场只给数据）
    · 只做 low_rebound 进场信号
    · 下单价 = 信号价 + 滑点，**硬上限 0.85**（超过上限的挂不上 → 不成交、不扣次）
    · 固定倍数：基础 10 份 × 倍数（×1~×5）
    · **1 次 = 1 份成交**（按成交股数计费），9.9U=300 次 → 0.033 U/次
    · 持有到结算（当前口径，不设止损/止盈）

回答客户最关心的三件事：
  ① 同一套规则跑真实历史，会赚还是会亏？（带 Wilson 置信区间，不拿小样本当结论）
  ② 滑点每多 1 分钱差多少？（决定 +0.05 这个取值是否安全）
  ③ 平台的次数费相对策略盈亏占多大比例？（决定这产品该怎么卖）

数据来源（全部只读）：
  /www/pmdata/orders*/*.jsonl   entry 行 = 信号（price 就是当时的 ask）
                                ★ 包含 orders_archived_* 归档目录，否则样本太小
  /www/pmdata/rounds.jsonl      slug → outcome（结算方向），独立于引擎自己的 pnl 字段
"""

import argparse
import glob
import json
import math
import os
import time

DATA = "/www/pmdata"
PREFIX = "btc-updown-5m"
BASE_SHARES = 10
MAX_MULT = 5
PRICE_CAP = 0.85
EXEC_DELTA = 0.05
CREDIT_USD = 9.9 / 300.0
CREDIT_PER_SHARES = 1
# ★ 必须与平台当前口径一致：polymarket/lib/signal.php 的 SIG_CHARGE_MODE
#   'receipt' = 1 次 = 一个成功回执（不看份数，当前启用）
#   'shares'  = 1 次 = 1 份成交（当前启用）
CHARGE_MODE = "receipt"


def credits_of(filled_shares: float) -> int:
    """按当前口径把成交量折算成扣次（和平台 sig_charge_for() 保持一致）。"""
    if filled_shares <= 0:
        return 0
    if CHARGE_MODE == "receipt":
        return 1
    return math.ceil(filled_shares / CREDIT_PER_SHARES)


def wilson(k, n, z=1.96):
    """二项比例的 Wilson 置信区间（小样本必备：9 单 8 胜不等于 89% 的胜率）。"""
    if n <= 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (p, max(0.0, c - h), min(1.0, c + h))


def load_outcomes():
    out = {}
    for path in (os.path.join(DATA, "rounds.jsonl"),
                 os.path.join(DATA, "stats", "rounds.jsonl")):
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                slug, oc = r.get("slug"), (r.get("outcome") or "").upper()
                if slug and oc in ("UP", "DOWN"):
                    out[slug] = oc
    return out


def order_dirs():
    """orders/ + orders_archived_*/（归档里有更早的信号，不能漏）。"""
    ds = [os.path.join(DATA, "orders")]
    ds += sorted(glob.glob(os.path.join(DATA, "orders_archived_*")))
    return [d for d in ds if os.path.isdir(d)]


def load_signals(market_prefix=PREFIX):
    sigs = []
    seen = set()
    for d in order_dirs():
        for f in glob.glob(os.path.join(d, "%s-*.jsonl" % market_prefix)):
            slug = os.path.basename(f)[:-6]
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if r.get("type") != "entry" or r.get("family") != "low_rebound":
                            continue
                        px, side = r.get("price"), (r.get("side") or "").upper()
                        if px is None or side not in ("UP", "DOWN"):
                            continue
                        key = (slug, r.get("trade_seq"))
                        if key in seen:            # 归档目录与现役目录可能有重叠
                            continue
                        seen.add(key)
                        sigs.append({"slug": slug, "ts": float(r.get("ts") or 0),
                                     "side": side, "ask": float(px),
                                     # ★ 盘口深度：那一刻最优档的挂单量（第二轮审查 P2）
                                     "ask_sz": float(r.get("ask_sz") or 0.0),
                                     "trade_seq": r.get("trade_seq")})
            except OSError:
                continue
    sigs.sort(key=lambda s: s["ts"])
    return sigs


def simulate(sigs, outcomes, slip, cap=PRICE_CAP, mult=1, limit_depth=False):
    """按一套固定规则跑一遍，返回统计。

    ★ 2026-09-28（第二轮审查 P2）：新增 limit_depth。
      原来只要 ask+slip ≤ 0.85 就当作**全部成交**，但客户端实盘是 FAK 限价：
      最优档只有 ask_sz 份时，超过的部分吃不到（服务端为此专门下发 ask_sz）。
      开了 limit_depth 就按 min(份数, ask_sz) 成交 —— 这才是"会看到什么"。
    """
    shares = BASE_SHARES * mult
    st = {"n": 0, "no_outcome": 0, "over_cap": 0, "fills": 0, "partial": 0,
          "win": 0, "loss": 0, "pnl": 0.0, "credits": 0, "asks": [], "fill_px": [],
          "shares": 0}
    for s in sigs:
        oc = outcomes.get(s["slug"])
        if oc is None:
            st["no_outcome"] += 1
            continue
        st["n"] += 1
        fill_px = s["ask"] + slip
        if fill_px > cap + 1e-9:
            st["over_cap"] += 1
            continue
        eff = shares
        if limit_depth:
            depth = float(s.get("ask_sz") or 0.0)
            if depth > 0 and depth < eff:
                eff = max(0.0, depth)
                st["partial"] += 1
        if eff <= 0:
            continue
        st["fills"] += 1
        st["asks"].append(s["ask"])
        st["fill_px"].append(fill_px)
        st["shares"] += eff
        st["credits"] += credits_of(eff)
        if s["side"] == oc:
            st["win"] += 1
            st["pnl"] += (1.0 - fill_px) * eff
        else:
            st["loss"] += 1
            st["pnl"] += (-fill_px) * eff
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=0, help="只看最近 N 天（0 = 全部）")
    ap.add_argument("--market", default=PREFIX)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    outcomes = load_outcomes()
    allsig = load_signals(args.market)
    if not allsig:
        raise SystemExit("没有信号数据（检查 %s/orders*/%s-*.jsonl）" % (DATA, args.market))

    if args.days > 0:
        cut = time.time() - args.days * 86400
        sigs = [s for s in allsig if s["ts"] >= cut]
    else:
        sigs = allsig
    t0, t1 = sigs[0]["ts"], sigs[-1]["ts"]
    days = max(0.05, (t1 - t0) / 86400.0)

    lines = []
    def P(s=""):
        print(s)
        lines.append(s)

    matched = [s for s in sigs if s["slug"] in outcomes]
    P("=" * 112)
    P("FinHub 信号产品 · 按客户端执行口径的回测（只读采集器历史）")
    P("=" * 112)
    P("市场        : %s（产品只卖 BTC）" % args.market)
    P("样本窗口    : %s ~ %s（%.2f 天）"
      % (time.strftime("%m-%d %H:%M", time.gmtime(t0)),
         time.strftime("%m-%d %H:%M", time.gmtime(t1)), days))
    P("信号条数    : %d 条（%d 条对上结算方向，%d 条缺 outcome）"
      % (len(sigs), len(matched), len(sigs) - len(matched)))
    P("信号频率    : %.1f 条/天" % (len(sigs) / days))
    P("执行口径    : 吃 ask(+滑点)，硬上限 %.2f；固定 %d 份 × 倍数；持有到结算"
      % (PRICE_CAP, BASE_SHARES))
    P("计次与单价  : %s → %.4f U/次（9.9U/300 次）"
      % ("1 次 = 一个成功回执（不看份数）" if CHARGE_MODE == "receipt"
         else "1 次 = %d 份成交" % CREDIT_PER_SHARES, CREDIT_USD))
    P("")
    P("盈亏口径    : 赢（方向对）= (1-成交价) × 份数；输 = -成交价 × 份数；")
    P("              次数费 = 成交单数 × 每次数 × 单价；净额 = 盈亏 - 次数费。")
    P("")

    P("%-14s %5s %5s %8s %-20s %8s %9s %9s %9s"
      % ("场景", "信号", "成交", "胜率", "胜率 95% 置信区间", "均ask", "盈亏U",
         "次数费U", "净额U"))
    P("-" * 112)

    grid = {}
    for mult in (1, 3, 5):
        for slip in (0.0, 0.01, 0.02, 0.05):
            st = simulate(matched, outcomes, slip, mult=mult)
            grid[(mult, slip)] = st
            p, lo, hi = wilson(st["win"], st["fills"])
            fee = st["credits"] * CREDIT_USD
            P("%-14s %5d %5d %7.1f%%  %5.1f%% ~ %5.1f%%      %6.3f %9.2f %9.2f %9.2f"
              % ("×%d 滑点+%.2f" % (mult, slip), st["n"], st["fills"], 100 * p,
                 100 * lo, 100 * hi,
                 (sum(st["asks"]) / len(st["asks"])) if st["asks"] else 0.0,
                 st["pnl"], fee, st["pnl"] - fee))
        P("")

    P("-" * 112)
    b = grid[(1, 0.05)]
    z = grid[(1, 0.0)]
    p, lo, hi = wilson(b["win"], b["fills"])
    # ★ 盈亏平衡胜率必须用**含滑点的成交价**（第二轮审查 P2）：
    #   赢只赚 (1-成交价)，输要亏成交价 → p(1-px) = (1-p)px → p = px。
    #   之前用原始 ask 均值当平衡点，等于把滑点白送了 5 个百分点，
    #   结论会显得比实际乐观。现在同时列出两个数，以**成交价**为准。
    fill_avg = (sum(b["fill_px"]) / len(b["fill_px"])) if b["fill_px"] else 0.0
    ask_avg  = (sum(b["asks"]) / len(b["asks"])) if b["asks"] else 0.0
    be = fill_avg
    fee = b["credits"] * CREDIT_USD
    per = b["pnl"] / b["fills"] if b["fills"] else 0.0

    P("判定（×1 / 滑点 +0.05 / 上限 0.85 —— 也就是客户端的默认口径）：")
    P("  · 成交 %d 单，胜率 %.1f%%（95%% 置信区间 %.1f%% ~ %.1f%%）"
      % (b["fills"], 100 * p, 100 * lo, 100 * hi))
    P("  · 平均信号价 %.3f → 平均**成交价** %.3f（这就是盈亏平衡胜率：赢赚 1-价，输亏价）"
      % (ask_avg, fill_avg))
    if lo > be:
        P("  · 结论：**置信区间下界仍高于平衡点** → 这段历史里存在正期望（样本 %d 单偏小，"
          "且实盘会随规模衰减）。" % b["fills"])
    elif hi < be:
        P("  · 结论：**置信区间上界低于平衡点** → 这段历史里期望为**负**，负得有统计意义。")
    else:
        P("  · 结论：**置信区间跨过平衡点** → 这段历史**分不出正负**（样本不足以判定；"
          "上界 %.1f%% 距平衡点 %.1f%% 还有 %.1f 个百分点）"
          % (100 * hi, 100 * be, 100 * (hi - be)))
    P("  · 每单净额 %.3f U；把滑点从 0 加到 0.05，每单少赚 %.3f U。"
      % (per, (z["pnl"] / max(1, z["fills"])) - per))
    P("  · 触到 0.85 上限而被丢弃的信号：%d 条（%.1f%%）。"
      % (b["over_cap"], 100.0 * b["over_cap"] / max(1, b["n"])))
    P("  · 次数费 / |盈亏| = %.1f%%（×1、+0.05）—— 平台抽水相对策略盈亏的量级。"
      % (100.0 * fee / max(1e-9, abs(b["pnl"]))))
    P("  · 倍数只线性放大盈亏与次数费，不改变方向：×5 的每单净额 ≈ ×1 的 5 倍。")
    P("")
    P("-" * 112)
    P("盘口深度约束下的对照（第二轮审查 P2）—— 上面的表假设**全部成交**，")
    P("  但实盘是 FAK 限价：最优档只有 ask_sz 份时，多出的部分吃不到。")
    P("  %-26s %6s %8s %10s %10s %10s"
      % ("场景", "成交单", "部分成交", "实际份数", "盈亏U", "每单U"))
    for mult in (1, 3, 5):
        a = grid[(mult, 0.05)]
        c = simulate(matched, outcomes, 0.05, mult=mult, limit_depth=True)
        P("  %-26s %6d %8d %10.0f %10.2f %10.3f"
          % ("×%d 无深度约束" % mult, a["fills"], a["partial"], a["shares"], a["pnl"],
             a["pnl"] / max(1, a["fills"])))
        P("  %-26s %6d %8d %10.0f %10.2f %10.3f"
          % ("×%d 按 ask_sz 限流" % mult, c["fills"], c["partial"], c["shares"], c["pnl"],
             c["pnl"] / max(1, c["fills"])))
    P("  ★ 「部分成交」= 那一刻最优档比我方份数还薄的单数；这些单只成交 ask_sz 份，")
    P("    所以份数少了、盈亏同比缩小（方向不变）。想避开这类信号，客户端加 --skip-thin。")
    P("")
    P("-" * 112)
    P("次数消耗速度与套餐寿命（定价的直接依据，用上面实测的信号频率算）：")
    rate = len(sigs) / days                       # 条/天（每条信号 = 一次下单）
    P("  · 实测信号频率 %.1f 条/天（每条信号客户端下一次单）" % rate)
    P("  · ★ 两种计次口径差别很大，这里都算出来（**平台当前启用 B：按回执**）：")
    P("      A 按份数：1 次 = %d 份成交 → ×1 扣 1 次、×5 扣 5 次（ceil(份数/%d)）"
      % (CREDIT_PER_SHARES, CREDIT_PER_SHARES))
    P("      B 按回执（当前）：1 次 = 一个成功下单回执 → 不管 ×1 还是 ×5 都只扣 1 次")
    P("  %-6s %-22s %-22s %-16s %-16s"
      % ("倍数", "A 次数/天", "B 次数/天", "A 300次可用", "B 300次可用"))
    for mult in (1, 3, 5):
        per_a = -(-(BASE_SHARES * mult) // CREDIT_PER_SHARES)
        cd_a, cd_b = rate * per_a, rate
        P("  %-6s %-22.0f %-22.0f %-16s %-16s"
          % ("×%d" % mult, cd_a, cd_b,
             "%.1f 天" % (300.0 / cd_a), "%.1f 天" % (300.0 / cd_b)))
    P("  ★ **平台当前用口径 B（按回执）**：套餐寿命与倍数无关（300 次恒为 %.1f 天），"
      % (300.0 / rate))
    P("    倍数只影响盈亏与资金占用；平台日收约 %.2f U（无论倍数）。"
      % (rate * CREDIT_USD))
    P("    口径 A（按份数）下倍数越高次数烧得越快（×5 的 300 次不到 1 天），")
    P("    日收可到 %.2f U/天 —— 但客户要多付 5 倍，解释成本高，**当前未启用**。"
      % (rate * 5 * CREDIT_USD))
    P("")
    P("怎么用这份报告：把 ①「置信区间 vs 平衡点」当作**能不能承诺盈利**的唯一依据；")
    P("② 滑点那两行当作**执行质量的敏感度**；③ 次数费占比当作**定价**的参照。")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print("\n报告已写入: %s" % args.out)


if __name__ == "__main__":
    main()
