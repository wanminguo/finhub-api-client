# 更新日志

本文件记录本仓库的显著变更，格式参照
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-09-27

初始发布。

### 新增

- `pm_api_client.py` —— PULSAR 脉冲星（Polymarket 5 分钟涨跌盘数据 API）的
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
[Unreleased]: https://github.com/<你的账号>/pm-api-client/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/<你的账号>/pm-api-client/releases/tag/v1.0.0
-->
