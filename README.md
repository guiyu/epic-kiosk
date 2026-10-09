# Epic Kiosk

[![Blog](https://img.shields.io/badge/Blog-910501.xyz-orange)](https://blog.910501.xyz/)
[![Bilibili](https://img.shields.io/badge/B%E7%AB%99-59438380-00a1d6?logo=bilibili)](https://space.bilibili.com/59438380)
[![YouTube](https://img.shields.io/badge/YouTube-10000%20AI%20Share-ff0000?logo=youtube&logoColor=white)](https://www.youtube.com/channel/UCqgvZnCN9-9pZcL4SWxmnDw)
![License: GPL-3.0-or-later](https://img.shields.io/badge/License-GPL--3.0--or--later-blue.svg)
![Docker](https://img.shields.io/badge/Docker-Compose-blue?logo=docker)
![Python](https://img.shields.io/badge/Python-3.12-yellow?logo=python)

Epic Kiosk 是一个基于 Docker 的 Epic Games 每周免费游戏自动领取服务，支持 Epic 官方授权登录、二次验证、真实库存查询、队列调度和多 WARP 出口。

本分支基于 [10000ge10000/epic-kiosk](https://github.com/10000ge10000/epic-kiosk)，增加了类似 Heroic/Legendary 的官方授权码登录流程。

> 公益站点：[https://epic.910501.xyz/](https://epic.910501.xyz/)

<p align="center">
  <img src="assets/dashboard.png" alt="Epic Kiosk Dashboard" width="100%" style="max-width: 920px;">
</p>

## 特性

- **官方登录与二次验证**：在 Epic 官方页面输入密码和验证器验证码，再将一次性 `authorizationCode` 粘贴到本地控制台；无需关闭两步验证。
- **统一账号与真实库存**：控制台和资产页使用同一个授权账号；官方授权成功后即可读取 Epic 库存，网页会话验证结果单独显示。
- **会话复用**：领取任务复用账号的浏览器 profile；需要时刷新 OAuth 凭据并建立网页会话。临时网页错误不会删除已保存的授权。
- **人工验证模式**：设置 `EPIC_CAPTCHA_MODE=manual` 后，遇到人机验证停止自动处理并提示用户到 Epic 官方页面亲自完成；普通 AI 接口不参与验证挑战。
- **多游戏流程**：只有本周期全部周免均确认 `claimed/owned` 才会标记账号完成，失败项会延迟补跑。
- **周期调度**：Web 每小时检查当前周免集合；新周期按 5 个账号一批、每批间隔 10 分钟；周期内新增账号会自动补排，同周期已完成账号不会重复领取。
- **登录与领取分离**：托管账号时只验证登录并保存账号，实际领取由后续周免周期任务执行。
- **多 WARP 出口**：单个 `epic-warp` 容器提供 10 个内部 WARP 实例，Worker 按账号稳定分配代理出口。
- **Docker 部署**：`web`、`worker`、`redis`、`warp` 四个服务通过 Docker Compose 本地构建和运行。

## 快速开始

### 手动部署（推荐）

推荐使用官方授权登录和人工验证模式。普通模型调用可选用 OpenAI-compatible API，与 Epic 登录和二次验证分开配置。

```bash
git clone https://github.com/guiyu/epic-kiosk.git
cd epic-kiosk
cp .env.example .env
nano .env
```

创建仓库外 Secret 目录和文件：

```bash
sudo install -d -m 0700 -o 1002 -g 1002 /etc/epic-kiosk/secrets
sudo install -m 0600 -o 1002 -g 1002 /dev/null /etc/epic-kiosk/secrets/captcha_gemini_api_key
sudo install -m 0600 -o 1002 -g 1002 /dev/null /etc/epic-kiosk/secrets/captcha_siliconflow_api_key
sudo install -m 0600 -o 1002 -g 1002 /dev/null /etc/epic-kiosk/secrets/captcha_nvidia_api_key
# 人工验证模式下，以上兼容旧配置的文件可保持为空。
```

生成内部接口令牌和 Fernet 凭据密钥：

```bash
openssl rand -hex 32 | sudo tee /etc/epic-kiosk/secrets/internal_api_token >/dev/null
openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n' | sudo tee /etc/epic-kiosk/secrets/epic_credential_keys >/dev/null
sudo chown 1002:1002 /etc/epic-kiosk/secrets/*
sudo chmod 0600 /etc/epic-kiosk/secrets/*
```

创建 `docker-compose.override.yml`，让人工验证模式传入 Worker（仅在 `.env` 设置该变量不会自动传入容器）：

```yaml
services:
  worker:
    environment:
      EPIC_CAPTCHA_MODE: manual
```

启动服务：

```bash
docker compose up -d --build
```

访问控制台：

```text
http://服务器IP:18000
```

### Linux 安装脚本

仓库授权用户完成克隆后，可以运行交互式安装脚本。脚本默认按 SiliconFlow 兼容配置引导；如果使用 NVIDIA 或自定义 OpenAI-compatible 服务，建议使用上面的手动部署流程。

```bash
chmod +x install.sh
./install.sh
```

## 配置说明

### 普通 AI 接口（可选）

普通模型调用支持 OpenAI-compatible `/v1/chat/completions`。供应商返回的模型列表不一定代表模型可用，应使用普通文本请求验证实际调用。此配置不用于自动完成人机验证。

将 API Key 保存在仓库外的 `0600` Secret 文件中，在 `.env` 中只填写文件路径、接口地址和模型名：

```env
ORDINARY_AI_API_KEY_PATH=/etc/epic-kiosk/secrets/ordinary_ai_api_key
API_BASE_URL=https://your-provider.example/v1
PRIMARY_MODEL=your-model-id
```

如需启用，将上面的 `docker-compose.override.yml` 改为：

```yaml
services:
  worker:
    environment:
      EPIC_CAPTCHA_MODE: manual
      API_PROVIDER: custom
      API_BASE_URL: ${API_BASE_URL}
      API_KEY_FILE: /run/secrets/ordinary_ai_api_key
      PRIMARY_MODEL: ${PRIMARY_MODEL}
      PRIMARY_MODEL_FALLBACK: ${PRIMARY_MODEL}
    secrets:
      - ordinary_ai_api_key
secrets:
  ordinary_ai_api_key:
    file: ${ORDINARY_AI_API_KEY_PATH:?set ORDINARY_AI_API_KEY_PATH}
```

`INTERNAL_API_TOKEN` 只用于 `web` 与 `worker` 的内部接口认证，必须是独立随机值，不能与模型 API Key 共用。Secret 文件保持 `0600`，不要把真实内容写入 `.env`。

仓库保留旧版模型与验证服务配置以兼容已有部署；推荐部署应明确设置人工验证模式。旧版模型配置说明见 [模型配置](docs/MODEL_CONFIG.md)。

### WARP 出口

当前 Compose 使用单容器多 WARP 架构：

```text
epic-warp:19000-19009
控制接口：http://epic-warp:18080/restart/{idx}
实例数量：10
```

Worker 会根据账号邮箱稳定选择一个 WARP index，并把 `HTTP_PROXY` / `HTTPS_PROXY` 注入本次浏览器任务。网络超时或浏览器驱动异常时，系统优先重启对应 index，而不是重启整个 WARP 容器。

可调恢复参数：

```env
WARP_CONTROL_RESTART_RETRIES=3
WARP_CONTROL_RESTART_BACKOFF_SECONDS=5
WARP_CONTAINER_FALLBACK_RESTARTS=1
```

WARP 控制接口请求会显式绕过 `HTTP_PROXY` / `HTTPS_PROXY`，避免 Worker 调用 `epic-warp:18080` 时被自己的 WARP 代理劫持。当控制接口短暂返回 `503` 时，Worker 会先退避，并同时检查 `/health` 与代理端口连通性；只有单出口恢复失败时，才最多执行一次整容器兜底重启。

### 任务恢复

- 供应商失败：默认延迟 15/60 分钟重试；连续 3 次失败后熔断 10 分钟。
- 人工验证模式：遇到挑战会提示用户亲自在 Epic 官方页面验证或领取，再回到资产页刷新；不调用模型或外部验证服务。
- 网络超时：默认延迟 10 分钟重试，最多 2 次。
- Cookie 失效：清理该账号浏览器 profile 后立即重试，默认 1 次。
- 多游戏领取：成功游戏先入库，失败游戏进入一次延迟补跑。

### 周免调度与账号状态

- Web 服务启用 APScheduler，每 3600 秒检查一次 Epic 当前周免集合；Worker 不运行自己的每日调度器，只消费 Redis 任务队列。
- 当前周免集合会生成一个周期 ID。只有检测到新周期，或周期内出现尚未分配的新账号时，才会创建领取任务。
- 默认每批 5 个账号，批次间隔 600 秒。账号按邮箱排序分组，不是随机分组。
- 当前周期所有预期游戏均为 `claimed` 或 `owned` 后，账号才会标记为完成；已完成账号不会在同一周期重复领取。
- `verify` 模式只执行登录和验证码验证，不执行游戏领取；领取任务由后续周期调度触发。
- 周期中途新增的托管账号会在下一次小时刷新时自动补排，不需要等待下一个周免周期。

## 使用方法

1. 打开 Web 控制台，选择「Epic 官方登录（推荐）」。
2. 点击「打开 Epic 官方登录」，在 Epic 页面输入账号、密码和二次验证码。
3. 复制登录后返回的 `authorizationCode` 或完整 JSON，粘贴到本地页面并连接账号。这里不填写验证器的 6 位验证码。
4. 官方授权成功后，本地加密保存可刷新凭据并自动加载资产页；浏览器登录验证在后台继续，两者分别显示结果。
5. 在「资产」查看 Epic 真实库存，点击「领取本周免费游戏」可复用当前账号；后续周免任务由周期调度自动安排。

授权码为一次性短期凭据，请勿发到聊天、日志或 Issue。过期后重新打开官方登录页面获取；不要提交其他来源不明的 Token 或账号 ID。

资产页展示的是 Epic 账号库存，不限于本工具领取的记录。旧密码账号仍可托管并接受周期调度；查询真实库存和资产页手动领取需要先完成官方授权。大型库存首次加载可能需要几十秒；名称和封面通过有限并发请求补全，无商品信息时使用应用名称。切换账号需要重新进行官方授权。

官方登录模式的删除按钮使用当前本地账号会话确认；旧邮箱密码模式仍需密码确认。删除会清除托管数据库记录和对应浏览器 profile，不会删除 Epic 平台上的账号或游戏。

## 目录结构

```text
epic-kiosk/
├── app/                    # FastAPI 后端、自动化和业务服务
├── templates/              # Web 页面模板
├── data/                   # SQLite、浏览器 profile、图片和日志
├── docs/                   # 部署与模型配置文档
├── worker.py               # Redis 队列 Worker
├── docker-compose.yml      # Docker Compose 编排
├── Dockerfile              # Web 镜像
├── Dockerfile.worker       # Worker 镜像
└── install.sh              # Linux 一键部署脚本
```

## 安全说明

- Epic 凭据使用 Fernet/MultiFernet 加密后保存在 `data/kiosk.db`；数据库、密钥文件和浏览器 profile 必须一起按敏感数据管理。
- Redis/APScheduler 任务只传递 `run_id`，不传递邮箱、密码或 Epic Token。
- 官方登录的密码和二次验证码只在 Epic 页面填写；OAuth 凭据在服务端加密保存，刷新操作按账号加锁，避免 Web 与 Worker 同时轮换。
- 浏览器本地存储保存的是本工具的账号访问令牌，用于库存查询和领取操作；它也应视为敏感凭据。不要共享浏览器配置或在不受信任的设备上托管账号。
- `.env` 不应提交到 Git；真实 API Key、内部令牌和凭据密钥只放在仓库外 Secret 文件中。
- 日志、截图、Issue、PR 中不要公开 API Key、Cookie、Token、Epic 密码或完整生产配置。
- 对外开放 Web 控制台前，应额外配置反向代理访问控制、面板鉴权或防火墙白名单。

## 升级

进入你的项目目录后执行：

```bash
git pull
docker compose up -d --build
```

仅重建 Worker：

```bash
docker compose build worker && docker compose up -d worker
```

## 故障排查

查看服务状态：

```bash
docker compose ps
docker exec epic-redis redis-cli LLEN task_queue
```

查看日志：

```bash
docker compose logs --tail=200 worker
docker compose logs --tail=200 web
ls data/logs/
tail -50 data/logs/runtime-$(date +%Y-%m-%d).log
```

常见问题：

- `未配置 API Key`：检查四个 Secret 路径存在、文件权限为 `0600`，并确认模型 Key 文件不是空文件。
- API 返回 `401` / `403`：Key 无效、额度不可用或 Provider 权限不足。
- API 返回 `404`：模型 ID 不存在，或当前账号无权调用该模型。
- 二次验证：使用官方登录，在 Epic 页面输入验证码；无需关闭两步验证。
- 官方授权成功但网页失败：库存可以独立加载，授权会保留；查看任务日志的失败阶段和 HTTP 状态，稍后重试。只有明确的凭据失效才需重新授权。
- 人机验证提示：到 Epic 官方商店亲自完成验证或领取，再刷新资产页。普通 AI 接口可用不代表人机验证会自动完成。
- `Requires Base Game`：该免费项是 DLC，需要先拥有基础游戏，系统会跳过或记录失败原因。
- WARP 控制接口短暂 `503`：Worker 调用控制接口时会绕过环境代理，并按 `WARP_CONTROL_RESTART_RETRIES` 和 `WARP_CONTROL_RESTART_BACKOFF_SECONDS` 退避重试，同时检查 `/health` 与代理端口连通性，避免连续重启整个 `epic-warp` 容器。
- 爬虫访问 `robots.txt`、`sitemap.xml`、`llms.txt`、`security.txt`：这些公开元文件会返回 200；明显错误的 CSS `url(...)` 路径会返回 204，以减少无意义 404 日志。

## 相关文档

- [快速开始](docs/QUICKSTART.md)
- [模型配置](docs/MODEL_CONFIG.md)

## 许可证

`pyproject.toml` 声明本项目使用 `GPL-3.0-or-later`。当前仓库尚未包含独立 `LICENSE` 文件，建议后续补充完整许可证文本。

## 致谢

- 原项目：[QIN2DIM/epic-awesome-gamer](https://github.com/QIN2DIM/epic-awesome-gamer)
- OpenAI-compatible Provider：NVIDIA、SiliconFlow 及其他兼容服务

## 免责声明

本项目仅供学习和技术研究使用。请合理使用，遵守 Epic Games 服务条款。开发者不对因使用本项目导致的任何损失承担责任。

*Created by [一万](https://github.com/10000ge10000) | 公益站点：[epic.910501.xyz](https://epic.910501.xyz/)*
