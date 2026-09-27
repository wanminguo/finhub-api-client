# 更新日志

本文件记录本仓库的显著变更，格式参照
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 变更

- **品牌更名：`PULSAR 脉冲星` → `FinHub API`**（站点顶栏改为 FinHub 在上、API 在下）。
  仅改名，**接口、参数、错误码、套餐口径一律未变** —— 已按字节核对，
  本次改动的文件只有 `README.md` / `pm_api_client.py` / `CHANGELOG.md` 三个。
  客户端代码无需任何改动（品牌字符串不出现在任何 API 字段里）。

- 仓库定位说明：本仓库只是 **FinHub 的数据 API 客户端**。FinHub 还有另一条业务线
  「USDT 收款通道」（商户收款 API，平台只做接口与对账、资金直连商户钱包），
  与数据客户端**不是同一个产品**，将来单独开仓库，不混进这里。

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

<!-- 发布后可在此追加对比链接，例如：
[Unreleased]: https://github.com/wanminguo/finhub-api-client/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/wanminguo/finhub-api-client/releases/tag/v1.0.0
-->
