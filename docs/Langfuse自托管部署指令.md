# Langfuse 云服务器自托管部署指令

> 用途：交给部署 Agent，在用户的云服务器上以 Docker 部署 Langfuse 自托管实例（server + ClickHouse + PostgreSQL + Redis），并通过公网 HTTPS 反向代理对外暴露。桌面客户端后续经该 HTTPS 地址上报 trace。
> 执行者：部署 Agent（非开发 Agent）。本文件只负责「把 Langfuse 跑在云服务器上并对外可用」，不含任何应用代码改动。
> 最后更新：2026-07-30

---

## 0. 任务目标（一句话）

在云服务器上用官方 `docker-compose` 起一套 Langfuse v3 自托管服务，配 Caddy 反代 + Let's Encrypt TLS，开放 443 入站；最终交付一个 `https://<YOUR_DOMAIN>` 地址 + 一对 project 级 `public/secret key` 给开发侧配置。

---

## 1. 前置清单（开始执行前先核对，缺一项先问用户）

- [ ] 一台云服务器（推荐 2 vCPU / 4 GB 内存起步；ClickHouse 较吃内存，生产建议 4 GB+）。
- [ ] 服务器操作系统：Ubuntu 22.04 / 24.04 LTS（或 Debian 12）。其他发行版需自行调整包管理命令。
- [ ] 一个已备案/可解析到该服务器公网 IP 的域名（如 `langfuse.your-domain.com`）。**必须能用域名签发 TLS 证书**，裸 IP 无法自动签发 Let's Encrypt。
- [ ] 服务器已放行安全组/防火墙：**入站放 22（SSH）、80（ACME 校验）、443（业务）；出站全通**。3000/ClickHouse/Postgres/Redis **不要**对公网开放。
- [ ] 具有 sudo 权限的登录账号（SSH key 或密码）。
- [ ] 能访问 `github.com`（拉取官方 compose）与 Docker Hub（拉镜像）。若服务器在内网需配置代理，先解决再继续。

**占位符约定**（实际执行时替换为用户真实值）：

| 占位符 | 含义 | 示例 |
|---|---|---|
| `<YOUR_DOMAIN>` | 对外访问域名 | `langfuse.your-domain.com` |
| `<SERVER_IP>` | 云服务器公网 IP | `1.2.3.4` |
| `<EMAIL>` | TLS 证书注册邮箱（ACME） | `admin@your-domain.com` |

---

## 2. 部署步骤

### 步骤 1：SSH 登录服务器

```bash
ssh <user>@<SERVER_IP>
```

### 步骤 2：安装 Docker 与 Docker Compose v2

```bash
# Ubuntu / Debian
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg lsb-release

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 验证
sudo docker version
sudo docker compose version   # 需显示 v2.x
```

### 步骤 3：创建工作目录与 .env

```bash
sudo mkdir -p /opt/langfuse
sudo chown $USER:$USER /opt/langfuse
cd /opt/langfuse

# 生成强随机密钥（每条命令单独生成，互不相同）
openssl rand -base64 32   # → NEXTAUTH_SECRET
openssl rand -base64 32   # → SALT
openssl rand -base64 32   # → ENCRYPTION_KEY
openssl rand -base64 16   # → POSTGRES_PASSWORD
openssl rand -base64 16   # → CLICKHOUSE_PASSWORD
```

把上面生成的密钥填入下方 `.env`（**用真实生成值替换占位**）。先用官方 `.env.example` 作模板再改：

```bash
curl -fsSL https://raw.githubusercontent.com/langfuse/langfuse/main/.env.example -o .env
```

然后用编辑器（如 `nano .env` 或 `vim .env`）确保至少包含以下关键项（官方模板可能已含，按实际版本补齐/覆盖）：

```dotenv
# ---- 对外访问地址（必须改为真实域名） ----
LANGFUSE_BASE_URL=https://<YOUR_DOMAIN>

# ---- NextAuth（Web 登录鉴权） ----
NEXTAUTH_SECRET=<上一步生成的 NEXTAUTH_SECRET>
NEXTAUTH_URL=https://<YOUR_DOMAIN>

# ---- 数据加密密钥（v3 必填，用于加密存储的 trace 内容） ----
ENCRYPTION_KEY=<上一步生成的 ENCRYPTION_KEY>

# ---- 数据库/存储（compose 内网服务名，密码用生成的强随机值） ----
POSTGRES_USER=langfuse
POSTGRES_PASSWORD=<POSTGRES_PASSWORD>
POSTGRES_DB=langfuse
POSTGRES_HOST=db
POSTGRES_PORT=5432

CLICKHOUSE_USER=langfuse
CLICKHOUSE_PASSWORD=<CLICKHOUSE_PASSWORD>
CLICKHOUSE_HOST=clickhouse
CLICKHOUSE_PORT=8123
CLICKHOUSE_DB=default
CLICKHOUSE_CLUSTER_ENABLED=false

REDIS_HOST=redis
REDIS_PORT=6379

# 服务端签名 salt
SALT=<上一步生成的 SALT>

# 端口：server 容器内 3000，宿主机映射也用 3000（仅内网/localhost 可达）
PORT=3000
```

> 注意：官方 `docker-compose.yml` 版本不同，变量名可能微调。**以官方 `.env.example` 当前版本为准**，本段为最小必填集；不要删掉模板里其他必填项。执行后若 `docker compose config` 报错缺少变量，按其提示补齐。

### 步骤 4：获取官方 docker-compose.yml（锁定版本，禁止 latest）

```bash
cd /opt/langfuse
# 拉取与 .env.example 同分支的 compose（main 为最新稳定；如需固定版本替换为对应 tag，如 v3.30.0）
curl -fsSL https://raw.githubusercontent.com/langfuse/langfuse/main/docker-compose.yml -o docker-compose.yml

# 校验 compose 语法（会列出缺失的环境变量）
docker compose config
```

> 安全纪律：**禁止把镜像 tag 写成 `latest`**。若改用发布 tag，请同步下载该 tag 下的 `docker-compose.yml` 与 `.env.example`（同 tag 路径），保证三者一致。

### 步骤 5：安装 Caddy 作为反向代理 + 自动 TLS

```bash
# 安装 Caddy（Ubuntu/Debian 官方源）
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update
sudo apt-get install -y caddy
```

写入 Caddyfile（反代到本地 `:3000`，自动签发并续期 Let's Encrypt 证书）：

```bash
sudo tee /etc/caddy/Caddyfile > /dev/null <<'EOF'
<YOUR_DOMAIN> {
    reverse_proxy 127.0.0.1:3000
}
EOF

sudo systemctl reload caddy
sudo systemctl status caddy   # 确认 active
```

> 说明：Caddy 首次访问会自动向 Let's Encrypt 申请证书（需 80 端口可达，已在步骤 1 开放）。若域名尚未解析到本机，证书申请会失败——请先确保 DNS A 记录已指向 `<SERVER_IP>` 且生效。

### 步骤 6：启动 Langfuse

```bash
cd /opt/langfuse
docker compose up -d

# 观察日志，等待所有服务 healthy（首次拉镜像 + ClickHouse 初始化可能需 1-3 分钟）
docker compose ps
docker compose logs -f langfuse-server   # Ctrl-C 退出跟随
```

确认 `langfuse-server` 状态为 `healthy` 或至少 `Up`（不断重启则需查日志定位，常见于 `.env` 缺变量或密钥长度不足）。

### 步骤 7：初始化（Web 控制台）

1. 浏览器打开 `https://<YOUR_DOMAIN>`。
2. 注册首个账号（成为 owner）。
3. 进入 **Settings → Projects → Create Project**，新建一个 project。
4. 在该 project 的 **Settings → API Keys** 页面，生成一对 key：
   - `public_key`：形如 `pk-lf-...`
   - `secret_key`：形如 `sk-lf-...`
5. **把这对 key 与 `https://<YOUR_DOMAIN>` 通过安全渠道交给开发侧**（例如写入用户提供的配置文档，不要明文贴在公开频道）。

### 步骤 8：收紧防火墙（仅放 443）

确认安全组/iptables 仅对公网开放 `22/80/443`；`3000`、`5432`、`8123`、`6379` 仅绑定 `127.0.0.1` / 内网。Docker 默认会写 iptables，必要时显式在 compose 中用 `ports: "127.0.0.1:3000:3000"` 约束 server 端口只监听本机（官方 compose 已通常如此，复核一下）。

### 步骤 9：设置开机自启与备份（运维收尾）

```bash
# 开机自启（docker compose v2 配合 restart: unless-stopped 已在官方 compose 内置；确保 docker 服务开机）
sudo systemctl enable docker

# 备份：定期导出 Postgres + ClickHouse 数据卷（示例，按需接入定时任务/云盘快照）
# docker compose exec db pg_dump -U langfuse langfuse > /backup/langfuse_pg_$(date +%F).sql
# ClickHouse 数据在 volume，建议直接对数据卷所在云盘做快照
```

---

## 3. 客户端侧配置（交给开发侧，不在本服务器执行）

开发机 / 用户机的环境变量（由应用读取，前缀 `CODING_AGENT_`）：

```bash
export CODING_AGENT_LANGFUSE_ENABLED=true
export CODING_AGENT_LANGFUSE_BASE_URL=https://<YOUR_DOMAIN>
export CODING_AGENT_LANGFUSE_PUBLIC_KEY=pk-lf-...
export CODING_AGENT_LANGFUSE_SECRET_KEY=sk-lf-...
```

> 与 Langfuse Cloud 切换：仅改 `LANGFUSE_BASE_URL` 与对应 key，应用代码零改动。

---

## 4. 验收清单（部署完成后逐项核对）

- [ ] `https://<YOUR_DOMAIN>` 浏览器可访问，地址栏有有效 TLS 锁标（非自签）。
- [ ] 能注册/登录 Web 控制台，能创建 project 并生成 public/secret key。
- [ ] `docker compose ps` 中 `langfuse-server`、`db`、`clickhouse`、`redis` 均为 `Up`/`healthy`，无反复重启。
- [ ] 公网 **仅** 443 可达；`nmap -p 3000,5432,8123 <SERVER_IP>` 应显示 filtered/closed。
- [ ] 把交付的 key + URL 配置到开发机后，跑一轮 turn，Langfuse UI 能看到对应 trace（此项由开发侧联调，部署 Agent 只需确认服务端可达）。
- [ ] 已对 Postgres/ClickHouse 数据安排备份策略。

---

## 5. 常见故障与回滚

| 现象 | 排查 |
|---|---|
| Caddy 起不来 / 证书签发失败 | 确认 80 端口入站开放、域名 A 记录已生效且 TTL 已过、服务器时间正确 |
| `langfuse-server` 不断重启 | `docker compose logs langfuse-server`；多为 `.env` 缺变量或 `ENCRYPTION_KEY`/`NEXTAUTH_SECRET` 长度不足（需 ≥ 32 字节 base64） |
| 页面 502 | Caddy 反代目标 `127.0.0.1:3000` 未起；确认 `docker compose ps` server 端口映射为 `3000:3000` |
| ClickHouse 初始化慢/失败 | 内存不足；临时调大 swap 或升配后再起 |

**回滚**：`cd /opt/langfuse && docker compose down` 停止并移除容器（数据卷保留，重跑 `up -d` 可恢复）。彻底清理数据需额外 `docker compose down -v`（会删库，慎用）。

---

## 6. 安全红线（部署 Agent 必须遵守）

- 绝不把 `secret_key`、`NEXTAUTH_SECRET`、`SALT`、`ENCRYPTION_KEY`、`POSTGRES/CLICKHOUSE` 密码提交到任何代码仓库或公开日志。
- 绝不将 3000/5432/8123/6379 暴露到公网。
- 镜像版本必须锁定 tag，禁止 `latest`。
- 证书用 Let's Encrypt 自动签发，不要自签或关闭 TLS。
