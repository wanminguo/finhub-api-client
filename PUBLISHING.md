# 发布指南（PUBLISHING）

本目录已经是一个**完整的、可直接发布的仓库**：只有源码、示例与文档，
不含任何密钥、凭证、私有地址或本机环境依赖。

> ⚠️ **本机没有安装 git** —— 所以下面只有**请你在有 git 的机器上照抄**的命令，
> 没有 `git init` / `commit` / `push` 被执行过。也不要在这里跑任何部署脚本。

---

## 0. 先决条件

- 已安装 git（`git --version` 能打印版本）。
- GitHub 上已建好一个**空仓库**（**不要**勾选 Add README / .gitignore / license，
  否则会与本地首次提交冲突）。
- 已配置 SSH key 并加到 GitHub（`ssh -T git@github.com` 返回 `Hi <你的账号>!`）。
  用 HTTPS 也行，把下面的 remote 换成
  `https://github.com/wanminguo/finhub-api-client.git` 即可。

---

## 1. 发布命令序列（照抄）

在**仓库父目录**（即包含 `finhub-api-client/` 的那一层）执行：

```bash
cd publish/finhub-api-client
git init && git add -A && git commit -m "feat: initial release"
git branch -M main
git remote add origin git@github.com:wanminguo/finhub-api-client.git
git push -u origin main
```

把 `<你的账号>` 换成你自己的 GitHub 用户名（**去掉尖括号**）。

---

## 2. ★ push 之前必须人工确认一次

`.gitignore` 已经覆盖了凭证与本地数据，但**它是兜底，不是放心**。
在 `git push` **之前**先跑：

```bash
git status
```

逐行确认暂存区里**没有**这些东西：

| 不能出现 | 说明 |
|---|---|
| `.env` / `.env.*` | 环境变量文件，最常见的 key 泄漏源 |
| `*.csv` / `pm_*.csv` / `samples*.csv` | 导出的样本数据（`--csv` 的产物） |
| `*.jsonl` | 采集器原始样本，体积大且属于数据而非代码 |
| 任何真实的 API key | 形如 `pm_live_` + 一长串字符；**代码里只允许占位符** |
| `__pycache__/` / `*.pyc` | Python 编译产物，不该进仓库 |

辅助检查（`git add -A` 之后仍然有效）：

```bash
git diff --cached --name-only          # 只看这次要提交的文件名清单
git status --short --ignored           # 被忽略的东西一眼可见
```

另外，提交前建议再搜一遍工作区里的凭证字样：

```bash
# 模式里的关键字都写成 xxx[y]yy，这样这条命令自身不会被自己命中
grep -rniE 'PRIVATE[_]KEY|LMTS[_]TOKEN|passw[o]rd|secr[e]t|pm_live_[A-Za-z0-9]{8,}' . \
  --exclude-dir=.git
```

> 上面把关键字写成 `PRIVATE[_]KEY` / `passw[o]rd` 这种带方括号的形式，是为了让
> **这条检查命令自身**不会被自己的模式命中（否则仓库里永远有一个假阳性）。
> 方括号里的字符只有一种可能，所以匹配的仍然是同一个字符串 —— 一个字符都不少。

**唯一允许命中的是 `pm_live_xxxxxxxx` 这类占位符**（文档里教用户填 key 用的）。
命中任何**真实** key 或密码 —— 立刻停下，删掉再提交。

> 一旦真实 key 被 push 上去，它就已经进入远端历史：**改文件再提交是删不掉的**，
> 必须去站点后台**吊销该 key**并重新签发。

---

## 3. 首次发布后

- 在仓库 **About**（GitHub 仓库页右上角齿轮）里填：
  - **Website**：<https://api.wanminguo.top/polymarket/>
  - **Description**：FinHub API — Polymarket 5-minute up/down market data API, Python client (zero dependencies)
  - **Topics**：`polymarket` `prediction-market` `api-client` `python` `finhub`

  这样仓库页会带上站点链接与正确分类，也更容易被搜到。
- 发布一个 **Release / Tag** `v1.0.0`，说明直接引用 `CHANGELOG.md` 的 1.0.0 一节。
- 之后每次改动记得同步更新 `CHANGELOG.md`。

---

## 3b. 后续更新怎么推（本文档第 1 节只适用于**首次**发布）

首次发布之后，推送更新的最小命令序列（在仓库目录里执行）：

```bash
cd publish/finhub-api-client
git add -A
git status --short                 # ★ 先看一眼改了哪些文件，别盲推
git commit -m "docs: 品牌更名 PULSAR 脉冲星 → FinHub API"
git push
```

字段名/接口没变、只是文档与品牌变化的版本，**不必**打新 tag；
等接口或客户端行为有变化时再发 `v1.0.1` / `v1.1.0`。

### 仓库已更名：`pm-api-client` → `finhub-api-client`

品牌改为 **FinHub API**、且计划覆盖「所有关联的 API 接口」后，旧名 `pm-api-client` 偏窄，
**已决定更名**（趁仓库还新、几乎没有外部引用时改，越晚越麻烦）。

**改名由仓库所有者操作**：GitHub → 仓库 **Settings → Repository name**。
（用 PAT 改名需要 `Administration: write`，权限比推代码大得多，不值得为省两步去开。）

改完后要同步的地方（**本工作区里都已经改好了**，列出以防将来又忘）：

| 位置 | 应是什么 |
|---|---|
| `CHANGELOG.md` 底部的链接引用 | `https://github.com/wanminguo/finhub-api-client/...`（顺手把 `<你的账号>` 写成了真实账号，链接才点得开） |
| `PUBLISHING.md` 第 1 节的 remote | `git@github.com:wanminguo/finhub-api-client.git` |
| `README.md` 里 clone 之后的路径示例 | `cp finhub-api-client/pm_api_client.py ...` / `$(pwd)/finhub-api-client` |
| `pm_api_client.py` 的 **User-Agent** | `finhub-api-client-python/<版本>`（**站点上那份示例也要一致**，否则两边对不上） |
| 本地已有 checkout 的人 | `git remote set-url origin git@github.com:wanminguo/finhub-api-client.git` |

GitHub 会为旧仓库名保留**自动重定向**，所以旧链接不会失效；但 clone 地址会变。

---

## 4. 常见问题

**`git push` 报 `failed to push some refs` / `rejected`**
远端仓库不是空的（建仓时勾了 README 等）。要么把远端清空重建，
要么改用 `git pull --rebase origin main` 再 push。

**`git commit` 报 `Author identity unknown`**
先设置身份：

```bash
git config --global user.name  "<你的名字>"
git config --global user.email "<你的邮箱>"
```

**文件权限 / 换行符警告（`LF will be replaced by CRLF`）**
在 Windows 上属正常提示，不影响发布。想统一行尾可以加一个 `.gitattributes`
（`* text=auto eol=lf`），本次初始发布不强制。

**提交后才发现漏了文件**
直接 `git add <文件> && git commit -m "chore: add ..." && git push` 即可，
不用重做初始提交。

---

## 5. 本仓库的发布前自检清单

- [ ] `python -m py_compile pm_api_client.py examples/*.py` 全部通过
- [ ] 四个示例 `python examples/<name>.py --help` 都能打印帮助并正常退出
- [ ] `git status` 里没有 `.env` / `*.csv` / `*.jsonl` / 真实 key / `__pycache__`
- [ ] README 里出现的相对路径（`examples/*.py`、`LICENSE` 等）都真实存在
- [ ] `CHANGELOG.md` 已记录本次版本
