# bt-cf-sync

Sync BT Panel Nginx site domains to Cloudflare DNS from a small Docker container.

The container reads BT Panel Nginx vhost files, extracts `server_name` values under `BASE_DOMAIN`, and creates or updates Cloudflare DNS records for the current VPS.

## How It Works

```text
BT Panel creates /www/server/panel/vhost/nginx/example.lsjmax.top.conf
Container sees it through a read-only volume at /bt-nginx
Container extracts server_name example.lsjmax.top
Container creates or updates the Cloudflare DNS record
```

When `DELETE_MISSING=true`, domains removed from BT Panel are deleted from Cloudflare only if they are owned by this container.

## Cloudflare Token Permissions

Create a Cloudflare API token with:

- Zone - Zone - Read
- Zone - DNS - Edit

Limit the token to the specific zone, for example `lsjmax.top`.

## Docker Compose

Copy `.env.example` to `.env`, edit the values, then run:

```bash
docker compose up -d
docker logs -f bt-cf-sync
```

`compose.yaml` uses the GHCR image:

```yaml
services:
  bt-cf-sync:
    image: ghcr.io/xzw38/bt-cf-sync:latest
    container_name: bt-cf-sync
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - /www/server/panel/vhost/nginx:/bt-nginx:ro
      - ./data:/data
```

Replace `xzw38` if you publish the image under another GitHub account or organization.

## Portainer Stack

Use `stacks/portainer-stack.yaml` as the base. In Portainer, replace:

- `ghcr.io/xzw38/bt-cf-sync:latest`
- `CF_API_TOKEN`
- `BASE_DOMAIN`
- `OWNER_ID`

Each VPS should have a different `OWNER_ID`, such as `main`, `oracle-arm`, or `hk-vps`.

## Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `CF_API_TOKEN` | required | Cloudflare API token. |
| `BASE_DOMAIN` | required | Domain to sync, for example `lsjmax.top`. |
| `OWNER_ID` | `default` | Unique owner name for this VPS. |
| `PUBLIC_IP` | `auto` | Public IP to write. Use `auto` or a fixed IP. |
| `PROXIED` | `true` | Cloudflare orange-cloud proxy setting. |
| `SLEEP_SECONDS` | `60` | Sync interval. |
| `ADOPT_EXISTING` | `true` | Adopt existing records that already point to this VPS IP. |
| `DELETE_MISSING` | `true` | Delete owned records when the site disappears from BT Panel. |
| `RECORD_TYPE` | `A` | Use `A` for IPv4 or `AAAA` for IPv6. |
| `WATCH_DIR` | `/bt-nginx` | Container path for BT Panel Nginx vhost files. |

## Updating

After pushing to the `main` branch, GitHub Actions builds and pushes:

```text
ghcr.io/xzw38/bt-cf-sync:latest
```

On each VPS:

```bash
docker compose pull
docker compose up -d
```

If you use Portainer, redeploy the stack or enable automatic updates with your preferred updater.
