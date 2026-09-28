# 更新日志

本文件记录本仓库的显著变更，格式参照
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 变更

- **注册地址改为站点根 `/me/register.php`**（原 `/polymarket/me/register.php`）。
  用户中心已于 2026-09-28 从 `/polymarket/me/` 搬到站点根 `/me/`，与 `/admin`
  平级 —— 它服务**所有**业务线（数据接口 / 信号订阅 / 收款通道），
  挂在 `polymarket/` 下面等于宣称它只是数据接口的附属品。同一天管理台也在
  同样的理由下位于 `/admin/`，两者现在是对称的。
  本次改动的文件：`pm_api_client.py`（`REGISTER_URL` 常量）、`README.md`（3 处）。
  **旧地址保留 301 跳转**，所以已经在用旧链接的书签与脚本不会失效；
  若你在自己的代码里硬编码了旧的注册地址，可以不改（但建议改）。
  接口、参数、错误码、计费口径一律未变。

- **`signal-client/tools/client_bt.py` 的计次口径对齐平台当前设置（按回执）**。
  平台侧 `SIG_CHARGE_MODE` 已定为 `'receipt'`（**1 次 = 一个成功下单回执，与股数无关**），
  而回测脚本原来固定按「每 10 股 1 次」折算 —— 与实际扣次不是同一把尺子。
  现在脚本同样读 `CHARGE_MODE`，两种口径都能算，输出里会标明当前用的是哪一种。
  ★ 这**不是客户端行为变更**：`finhub/finhub.py` 不做计次判断，计次在服务端，
  本项只影响回测数字与平台扣次的一致性。

### 修复

- **客户端：外层隧道建链失败会重试 3 次**（signal-client）。实测发现外网链路上
  偶发会把到 8443 的连接掐掉（隧道侧连日志都没有，说明连接没到服务器）。
  现在 `_relay` 把"外层 TLS + CONNECT"抽成可重试步骤：失败退避重试（0.4s/0.8s），
  业务拒绝（白名单/端口）不重试。可靠性抽样：**顺序 20 次请求 20/20 成功、0 次重试**
  （真实 SDK 就是一次一请求的用法）；8 路并发突发时偶发 1/8 会在**内层**握手被重置
  （隧道日志无异常 → 属于链路/上游侧抖动，不是本仓库代码），
  所以实盘路径上不建议并发打 CLOB。

- **客户端本地代理：CONNECT 头与后续数据的分界不能再丢字节**（signal-client）。
  原来用带缓冲的 `rfile` 读 CONNECT 头 —— 客户端若把"CONNECT 头 + 后续数据"
  放在同一个 TCP 段里（流水线发送），后面的字节会被吞进缓冲区、**永不转发**。
  实测（`.deploy/_test_proxy_framing.py` 的 B 场景）：假隧道一个字节都收不到。
  现在从原始 socket **逐字节**读头，并把多出来的字节原样转给上游。

- **`TunnelProxy.stop()` 不释放监听端口**：只 `shutdown()` 没 `server_close()`，
  同进程内重启代理会报 WinError 10048。两个都调用并置空句柄。

- **服务端隧道：证书热重载**。原来只在启动时加载一次证书，而宝塔/Let's Encrypt
  续签只 reload nginx、**不会重启这个 Python 服务** —— 证书到期那天整条链路会静默失效。
  现在每 300 秒检查证书文件 mtime/size，变更即重建 SSLContext 热切换
  （实测：touch 后 30 秒内日志出现"★ 证书已热重载"，服务不中断）。

- **服务端隧道：单 IP 并发上限**（默认 20，`--max-per-ip`）。原来只有全局 400，
  单个 IP 可以把整个隧道占满。实测：上限设 5 时开 8 条连接 → 放行 5、拒 3。

- **★ 出网隧道改为「外层 TLS + CONNECT」—— 这是"国内能不能下单"的关键修复**
  （客户端 + 服务端隧道）：旧设计把目标的 ClientHello 原样转发，里面的
  `SNI=clob.polymarket.com` 是**明文**，国内链路上的 DPI 看见就注入 RST。
  对照实验（同一服务器端口、同一隧道，只换 SNI）：
  `example.com` 握手完成（0.9s），`clob.polymarket.com` **0.0s 被 RST**。
  现在客户端先与平台域名建立一层 TLS（`api.wanminguo.top`，国内可直连、证书有效），
  在这层加密通道里发 `CONNECT <目标>:443`，内层 TLS 藏在外层里。
  实测（从国内）：`GET https://clob.polymarket.com/ok` → **HTTP 200**，
  并取到真实 `condition_id`；白名单外的域名与 443 之外的端口仍被拒绝。
  默认隧道地址随之改为 `api.wanminguo.top:8443`（`tun.api.wanminguo.top` 无 DNS
  也无证书，做不了外层 TLS）。

- **隧道域名解析失败时回退到备用 IP**：解析不到时自动回退并在日志里写明；
  外层 TLS 仍按域名校验证书，**回退不影响安全性**。

- 客户文档把"隧道不解密"改写得更准确：外层 TLS 在平台侧终结（只用于隐藏 SNI），
  **下单签名与私钥是客户端与 Polymarket 之间内层 TLS 端到端加密的**，
  平台能看到的是"连了哪个域名、多少字节"。

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
