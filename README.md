# bt-cf-sync

中文 | [English](#english)

把宝塔面板 Nginx 站点自动同步到 Cloudflare DNS 的 Docker 小工具。

容器会读取宝塔生成的 Nginx 站点配置，提取 `server_name` 里属于 `BASE_DOMAIN` 的域名，然后在 Cloudflare 自动创建、更新或删除 DNS 记录。

容器还带一个只读状态面板，默认只监听宿主机本地地址 `127.0.0.1:25708`，再映射到容器内 `8080`：

```text
http://127.0.0.1:25708
```

## 工作原理

```text
宝塔创建 /www/server/panel/vhost/nginx/app.example.com.conf
Docker 通过只读 volume 在 /bt-nginx 看到这个文件
脚本提取 server_name app.example.com
脚本调用 Cloudflare API 创建或更新 DNS 记录
```

如果 `DELETE_MISSING=true`，当宝塔站点被删除后，容器会删除它自己创建或接管过的 Cloudflare DNS 记录。

为了避免误删，多台 VPS 部署时，每台机器都应该设置不同的 `OWNER_ID`。

## Cloudflare Token 权限

创建 Cloudflare API Token 时只需要：

- Zone - Zone - Read
- Zone - DNS - Edit

范围建议限制到指定域名，例如 `example.com`。

## 部署方式一：Docker Compose + `.env`

这种方式适合你直接 SSH 到 VPS 上部署。变量写在 `.env`，`compose.yaml` 只负责引用。

复制环境变量示例：

```bash
cp .env.example .env
```

`.env` 示例：

```env
CF_API_TOKEN=your_cloudflare_api_token
BASE_DOMAIN=example.com
OWNER_ID=vps1
PUBLIC_IP=auto
PROXIED=true
SLEEP_SECONDS=60
ADOPT_EXISTING=true
DELETE_MISSING=true
RECORD_TYPE=A
WEB_USERNAME=admin
WEB_PASSWORD=change_me
```

`compose.yaml` 示例：

```yaml
services:
  bt-cf-sync:
    image: ghcr.io/xzw38/bt-cf-sync:latest
    container_name: bt-cf-sync
    restart: unless-stopped
    env_file:
      - .env
    ports:
      - "127.0.0.1:25708:8080"
    volumes:
      - /www/server/panel/vhost/nginx:/bt-nginx:ro
      - ./data:/data
```

编辑 `.env` 后启动：

```bash
docker compose up -d
docker logs -f bt-cf-sync
```

如果镜像发布在其他 GitHub 账号或组织下，把 `xzw38` 改成对应名称。

## 部署方式二：Portainer Stack 直接写变量

这种方式适合在 Portainer 的 Stacks 页面直接粘贴。变量写在 `environment` 里，不需要单独创建 `.env` 文件。

```yaml
services:
  bt-cf-sync:
    image: ghcr.io/xzw38/bt-cf-sync:latest
    container_name: bt-cf-sync
    restart: unless-stopped
    environment:
      CF_API_TOKEN: "your_cloudflare_api_token"
      BASE_DOMAIN: "example.com"
      OWNER_ID: "vps1"
      PUBLIC_IP: "auto"
      PROXIED: "true"
      SLEEP_SECONDS: "60"
      ADOPT_EXISTING: "true"
      DELETE_MISSING: "true"
      RECORD_TYPE: "A"
      WEB_USERNAME: "admin"
      WEB_PASSWORD: "change_me"
    ports:
      - "127.0.0.1:25708:8080"
    volumes:
      - /www/server/panel/vhost/nginx:/bt-nginx:ro
      - /opt/bt-cf-sync/data:/data
```

也可以直接参考 [stacks/portainer-stack.yaml](stacks/portainer-stack.yaml)。

在 Portainer 的 Stacks 里重点修改：

- `image`
- `CF_API_TOKEN`
- `BASE_DOMAIN`
- `OWNER_ID`

每台 VPS 的 `OWNER_ID` 要不同，例如：

```text
main
oracle-arm
hk-vps
```

两种方式本质一样：`.env` 是把变量放到文件里，Portainer Stack 是把变量直接写进 YAML。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CF_API_TOKEN` | 必填 | Cloudflare API Token。 |
| `BASE_DOMAIN` | 必填 | 要同步的主域名，例如 `example.com`。 |
| `OWNER_ID` | `default` | 当前 VPS 的唯一名称。 |
| `PUBLIC_IP` | `auto` | 写入 DNS 的公网 IP，`auto` 表示自动获取。 |
| `PROXIED` | `true` | 是否开启 Cloudflare 橙云代理。 |
| `SLEEP_SECONDS` | `60` | 每隔多少秒同步一次。 |
| `ADOPT_EXISTING` | `true` | 如果已有记录已经指向当前 VPS IP，是否接管并打标记。 |
| `DELETE_MISSING` | `true` | 宝塔站点消失后，是否删除自己拥有的 DNS 记录。 |
| `RECORD_TYPE` | `A` | DNS 记录类型，IPv4 用 `A`，IPv6 用 `AAAA`。 |
| `WATCH_DIR` | `/bt-nginx` | 容器内读取宝塔 Nginx 站点配置的路径。 |
| `WEB_USERNAME` | 空 | 状态面板 Basic Auth 用户名，留空则不启用认证。 |
| `WEB_PASSWORD` | 空 | 状态面板 Basic Auth 密码，留空则不启用认证。 |
| `WEB_PORT` | `8080` | 容器内部 Web 服务端口。 |

## 状态面板

在 VPS 本机打开：

```text
http://127.0.0.1:25708
```

如果你在本地电脑看远程 VPS，可以用 SSH 隧道：

```bash
ssh -L 25708:127.0.0.1:25708 root@your-vps-ip
```

然后本地浏览器打开：

```text
http://127.0.0.1:25708
```

如果确实想公网直接访问，把端口映射改成：

```yaml
ports:
  - "25708:8080"
```

面板会显示：

- 宝塔配置里扫描到的域名
- Cloudflare 是否已有对应记录
- Cloudflare 当前指向的 IP
- 是否指向本机 IP
- 是否由当前 `OWNER_ID` 管理
- 最近同步时间和最近日志

也可以读取 JSON API：

```text
http://127.0.0.1:25708/api/status
```

建议保持默认本地监听；如果公网暴露，务必设置 `WEB_USERNAME` 和 `WEB_PASSWORD`，并配合防火墙限制来源。

## 更新镜像

代码推送到 `main` 分支后，GitHub Actions 会自动构建并推送：

```text
ghcr.io/xzw38/bt-cf-sync:latest
```

每台 VPS 更新：

```bash
docker compose pull
docker compose up -d
```

如果使用 Portainer，可以手动 redeploy stack，或者配合 Watchtower 一类工具自动拉取新镜像。

## 发布到 GHCR

本项目已经包含 GitHub Actions 配置：

```text
.github/workflows/docker-image.yml
```

创建 GitHub 仓库并推送 `main` 分支后，Actions 会自动发布 `latest` 镜像到 GHCR。

如果 GitHub CLI 显示登录失效，重新登录一次即可：

```bash
gh auth login -h github.com
```

这是当前 Windows 用户的全局登录状态，不是每个项目单独登录。只要 `gh auth status` 正常，后续所有项目都能共用。

## 注意事项

- 容器通过 volume 读取宿主机目录，不会默认访问所有宿主机文件。
- `/www/server/panel/vhost/nginx:/bt-nginx:ro` 是只读挂载，容器只能读宝塔配置，不能修改。
- 如果 Cloudflare 已经有同名记录指向其他 IP，默认不会抢占。
- 如果要部署到多台 VPS，每台 VPS 都使用同一个镜像，只改环境变量。

## English

[中文](#bt-cf-sync) | English

Sync BT Panel Nginx site domains to Cloudflare DNS from a small Docker container.

The container reads BT Panel Nginx vhost files, extracts `server_name` values under `BASE_DOMAIN`, and creates, updates, or deletes Cloudflare DNS records for the current VPS.

The container also includes a read-only status dashboard. By default, it binds only to the host loopback address `127.0.0.1:25708` and maps to container port `8080`:

```text
http://127.0.0.1:25708
```

### How It Works

```text
BT Panel creates /www/server/panel/vhost/nginx/app.example.com.conf
The container sees it through a read-only volume at /bt-nginx
The script extracts server_name app.example.com
The script creates or updates the Cloudflare DNS record
```

When `DELETE_MISSING=true`, domains removed from BT Panel are deleted from Cloudflare only if they are owned by this container.

Use a different `OWNER_ID` on each VPS to avoid cross-machine deletion.

### Cloudflare Token Permissions

Create a Cloudflare API token with:

- Zone - Zone - Read
- Zone - DNS - Edit

Limit the token to the specific zone, for example `example.com`.

### Deployment Option 1: Docker Compose + `.env`

This option is best when deploying over SSH. Variables are stored in `.env`, and `compose.yaml` references that file.

Copy `.env.example` to `.env`, edit the values, then run:

```bash
cp .env.example .env
```

`.env` example:

```env
CF_API_TOKEN=your_cloudflare_api_token
BASE_DOMAIN=example.com
OWNER_ID=vps1
PUBLIC_IP=auto
PROXIED=true
SLEEP_SECONDS=60
ADOPT_EXISTING=true
DELETE_MISSING=true
RECORD_TYPE=A
WEB_USERNAME=admin
WEB_PASSWORD=change_me
```

`compose.yaml` example:

```yaml
services:
  bt-cf-sync:
    image: ghcr.io/xzw38/bt-cf-sync:latest
    container_name: bt-cf-sync
    restart: unless-stopped
    env_file:
      - .env
    ports:
      - "127.0.0.1:25708:8080"
    volumes:
      - /www/server/panel/vhost/nginx:/bt-nginx:ro
      - ./data:/data
```

Start it:

```bash
docker compose up -d
docker logs -f bt-cf-sync
```

Replace `xzw38` if you publish the image under another GitHub account or organization.

### Deployment Option 2: Portainer Stack With Inline Variables

This option is best for Portainer Stacks. Variables are written directly under `environment`, so no separate `.env` file is needed.

```yaml
services:
  bt-cf-sync:
    image: ghcr.io/xzw38/bt-cf-sync:latest
    container_name: bt-cf-sync
    restart: unless-stopped
    environment:
      CF_API_TOKEN: "your_cloudflare_api_token"
      BASE_DOMAIN: "example.com"
      OWNER_ID: "vps1"
      PUBLIC_IP: "auto"
      PROXIED: "true"
      SLEEP_SECONDS: "60"
      ADOPT_EXISTING: "true"
      DELETE_MISSING: "true"
      RECORD_TYPE: "A"
      WEB_USERNAME: "admin"
      WEB_PASSWORD: "change_me"
    ports:
      - "127.0.0.1:25708:8080"
    volumes:
      - /www/server/panel/vhost/nginx:/bt-nginx:ro
      - /opt/bt-cf-sync/data:/data
```

Use [stacks/portainer-stack.yaml](stacks/portainer-stack.yaml) as the base.

In Portainer, replace:

- `image`
- `CF_API_TOKEN`
- `BASE_DOMAIN`
- `OWNER_ID`

Each VPS should use a different `OWNER_ID`, such as `main`, `oracle-arm`, or `hk-vps`.

Both methods do the same thing: `.env` keeps variables in a file, while Portainer Stack writes them directly in YAML.

### Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `CF_API_TOKEN` | required | Cloudflare API token. |
| `BASE_DOMAIN` | required | Domain to sync, for example `example.com`. |
| `OWNER_ID` | `default` | Unique owner name for this VPS. |
| `PUBLIC_IP` | `auto` | Public IP to write. Use `auto` or a fixed IP. |
| `PROXIED` | `true` | Cloudflare orange-cloud proxy setting. |
| `SLEEP_SECONDS` | `60` | Sync interval. |
| `ADOPT_EXISTING` | `true` | Adopt existing records that already point to this VPS IP. |
| `DELETE_MISSING` | `true` | Delete owned records when the site disappears from BT Panel. |
| `RECORD_TYPE` | `A` | Use `A` for IPv4 or `AAAA` for IPv6. |
| `WATCH_DIR` | `/bt-nginx` | Container path for BT Panel Nginx vhost files. |
| `WEB_USERNAME` | empty | Basic Auth username for the status dashboard. Empty disables auth. |
| `WEB_PASSWORD` | empty | Basic Auth password for the status dashboard. Empty disables auth. |
| `WEB_PORT` | `8080` | Internal web service port. |

### Status Dashboard

On the VPS itself, open:

```text
http://127.0.0.1:25708
```

From your local computer, use an SSH tunnel:

```bash
ssh -L 25708:127.0.0.1:25708 root@your-vps-ip
```

Then open locally:

```text
http://127.0.0.1:25708
```

If you intentionally want public access, change the port mapping to:

```yaml
ports:
  - "25708:8080"
```

The dashboard shows:

- domains scanned from BT Panel configuration
- whether Cloudflare has matching records
- the current Cloudflare target IP
- whether the record points to this VPS
- whether the record is managed by the current `OWNER_ID`
- last sync time and recent logs

JSON API:

```text
http://127.0.0.1:25708/api/status
```

Keep the default local-only binding when possible. If exposing the port publicly, set `WEB_USERNAME` and `WEB_PASSWORD`, and restrict access with a firewall.

### Updating

After pushing to the `main` branch, GitHub Actions builds and pushes:

```text
ghcr.io/xzw38/bt-cf-sync:latest
```

On each VPS:

```bash
docker compose pull
docker compose up -d
```

If you use Portainer, redeploy the stack or use your preferred image updater.

### Publishing To GHCR

This repository includes the GitHub Actions workflow:

```text
.github/workflows/docker-image.yml
```

After creating the GitHub repository and pushing the `main` branch, Actions publishes the `latest` image to GHCR automatically.

If GitHub CLI reports an expired login, re-authenticate once:

```bash
gh auth login -h github.com
```

This is a global login for the current Windows user, not a per-project login. Once `gh auth status` works, other projects can reuse it.

### Notes

- The container reads host files only through explicitly mounted volumes.
- `/www/server/panel/vhost/nginx:/bt-nginx:ro` is read-only, so the container cannot modify BT Panel configuration.
- Existing Cloudflare records pointing to a different IP are not overwritten by default.
- For multiple VPS machines, use the same image and change only the environment variables.
