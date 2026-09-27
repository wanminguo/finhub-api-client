# 更新日志

本文件记录本仓库的显著变更，格式参照
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 修复

- **隧道域名解析失败时自动回退到备用 IP**（signal-client）：实测
  `tun.api.wanminguo.top` 没有 DNS 记录，客户端会直接报
  `Name or service not known`、`--live` 完全连不上（而服务端隧道其实在跑）。
  现在解析失败会自动回退到备用 IP 并在日志里写明；DNS 记录补上后自动用回域名。
  客户文档同步补了"看到回退日志不用慌 / 看到 timed out 是服务端 8443 没放行"。

## [1.1.0] - 2026-09-28

### 新增

- **`signal-client/` —— 信号订阅客户端**（FinHub 的第二条产品线，与数据 API
  **互不依赖**）：跑在客户自己机器上，长轮询取 BTC-5m 实时信号 → 本地 FAK 限价下单
  （私钥只在客户机器上，平台不代持、不代下单、不解密隧道流量）→ 回执上报计次。
  - 纯标准库（纸面模式零依赖）；实盘可选官方 `py-clob-client`。
  - 自带本地面板（`127.0.0.1:8787`，含 Host 校验）、出网隧道本地代理
    （只绑 127.0.0.1、只放白名单域名与 443、并发上限 64）。
  - 价格口径：信号价 + 0.05、**硬上限 0.85**、FAK 限价；没成交不扣次。
  - 回执可靠性：退避重试 + 含真实成交的失败回执落盘（`~/.finhub/unsent.jsonl`）
    下次自动补交；游标落盘（不会重启重放真单）。
  - `signal-client/README.md` 里含**安装、参数、隧道、计次规则、风险与最坏情况**；
    `signal-client/tools/client_bt.py` 是回测脚本（口径可审计）。
    ★ 明确写了「平台不承诺任何收益」与真实回测数字（93 单 / 胜率 78.5% /
    盈亏平衡 74.8% / 置信区间跨过平衡点）。
- README 新增「[信号订阅](README.md#信号订阅另一条产品线)」一节（中英双语），
  端点表补上 `/v1/signals.php`、`/v1/receipt.php`、`/v1/credits.php`，
  并说明它**按成交次数计费、不消耗每日请求配额**。

### 变更

- **品牌更名：`PULSAR 脉冲星` → `FinHub API`**（站点顶栏改为 FinHub 在上、API 在下）。
  仅改名，**接口、参数、错误码、套餐口径一律未变** —— 已按字节核对，
  本次改动的文件只有 `README.md` / `pm_api_client.py` / `CHANGELOG.md` 三个。
  客户端代码无需任何改动（品牌字符串不出现在任何 API 字段里）。

- 仓库定位说明（更新）：本仓库是 **FinHub 的公开客户端仓库**，现在有**两**条线 ——
  根目录是**数据 API 客户端**，`signal-client/` 是**信号订阅客户端**（2026-09-28 加入）。
  FinHub 还有第三条线「USDT 收款通道」（商户收款 API，平台只做接口与对账、
  资金直连商户钱包），与这两个客户端**不是同一个产品**，将来单独开仓库，不混进这里。

- **修正一处过时表述**：原「因子与信号目录」一节写着"这些信号不在 API 响应里、
  也不是本站推荐的下单依据"，容易被读成"本站不做信号"。现在明确区分：
  那批是**数据侧评估的因子**（仍不在数据 API 响应里），
  而**信号订阅**是另一条独立产品线（有自己的端点、客户端与计费口径）。

## [1.0.0] - 2026-09-27

初始发布。

### 新增

- `pm_api_client.py` —— FinHub API（Polymarket 5 分钟涨跌盘数据 API）的
  Python 客户端，**零第三方依赖**（仅标准库）：
  - 端点方法：`window()` / `settle()` / `history()` / `samples()` / `stats()` /
    `index()`，以及自动翻页的 `iter_history()`。
  - 统一的错误信封解析：失败一律抛 `PmError` 子类
    （`PmRateLimited` / `PmQuotaExceeded` / `PmTransportError`），
    带上 `status` / `code` / `message` / `retry_after` / `quota` / `payload`。
  - **两种 429 分开处理**：`rate_limited` 按 `Retry-After` 自动重试（默认 2 次），
    `daily_quota_exceeded` 永不自动重试。
  - `meta.quota` 退避辅助：`meta.remaining_ratio` / `meta.should_back_off()`。
  - 如实透传 `beat_source` / `beat_trusted`，绝不吞掉锚点可信度标记；
    并提供严格判断 `data.is_trusted_beat`。
  - 同时兼容 `{ok, data, meta}` 标准信封与 `/v1/index.php` 的扁平结构
    （按「响应里有没有 `data` 层」判断形状，不是给该端点打特例补丁）。
- `examples/` —— 四个可直接运行的示例，全部支持 `--help`：
  - `current_window.py`：当前窗口快照，`--series` 用纯标准库画 ASCII 走势图。
  - `settle_recent.py`：最近 N 个已结算窗口，`--verify` 用 `rules.so/sc/to/tc`
    在本地复算四条规则并与官方 `outcome` 比对一致率。
  - `history.py`：按时间范围拉历史列表，手动与自动翻页。
  - `samples.py`：原始 2 秒样本，支持 `--tail` / `--raw` / `--csv` 导出。
- `README.md` —— 中英双语说明：7 个市场、端点表、响应信封、错误码表、
  套餐与限额、客户端用法，以及「为什么 `price_to_beat` 必须用官方 Chainlink 读数」。
- `PUBLISHING.md` —— 发布步骤（含 push 前的泄密自查）。
- `requirements.txt` —— 空依赖清单（仅注释）。
- `LICENSE` —— MIT。
- `.gitignore` —— Python 产物与本地凭证（`.env` / `*.csv` / `*.jsonl` 等）。

[未发布]: https://github.com/wanminguo/finhub-api-client/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/wanminguo/finhub-api-client/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/wanminguo/finhub-api-client/releases/tag/v1.0.0
