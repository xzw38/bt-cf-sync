#!/usr/bin/env python3
from __future__ import annotations

import base64
import html
import ipaddress
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def getenv(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"{name} is required")
    return value or ""


def parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y", "on"}:
        return True
    if normalized in {"false", "0", "no", "n", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


@dataclass
class Config:
    cf_api_token: str = field(default_factory=lambda: getenv("CF_API_TOKEN", required=True))
    base_domain: str = field(default_factory=lambda: getenv("BASE_DOMAIN", required=True).strip(".").lower())
    owner_id: str = field(default_factory=lambda: getenv("OWNER_ID", "default"))
    public_ip: str = field(default_factory=lambda: getenv("PUBLIC_IP", "auto"))
    proxied: bool = field(default_factory=lambda: parse_bool(getenv("PROXIED", "true"), "PROXIED"))
    sleep_seconds: int = field(default_factory=lambda: int(getenv("SLEEP_SECONDS", "60")))
    adopt_existing: bool = field(default_factory=lambda: parse_bool(getenv("ADOPT_EXISTING", "true"), "ADOPT_EXISTING"))
    delete_missing: bool = field(default_factory=lambda: parse_bool(getenv("DELETE_MISSING", "true"), "DELETE_MISSING"))
    record_type: str = field(default_factory=lambda: getenv("RECORD_TYPE", "A").upper())
    watch_dir: Path = field(default_factory=lambda: Path(getenv("WATCH_DIR", "/bt-nginx")))
    state_file: Path = field(default_factory=lambda: Path(getenv("STATE_FILE", "/data/owned-domains.txt")))
    log_file: Path = field(default_factory=lambda: Path(getenv("LOG_FILE", "/data/sync.log")))
    web_bind: str = field(default_factory=lambda: getenv("WEB_BIND", "0.0.0.0"))
    web_port: int = field(default_factory=lambda: int(getenv("WEB_PORT", "8080")))
    web_username: str = field(default_factory=lambda: getenv("WEB_USERNAME", ""))
    web_password: str = field(default_factory=lambda: getenv("WEB_PASSWORD", ""))

    @property
    def mark(self) -> str:
        return f"bt-cf-sync:{self.owner_id}"


cfg = Config()
if cfg.record_type not in {"A", "AAAA"}:
    raise RuntimeError("RECORD_TYPE must be A or AAAA")

cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
cfg.state_file.touch(exist_ok=True)

state_lock = threading.Lock()
last_status: dict[str, Any] = {
    "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "lastSyncAt": None,
    "lastError": None,
    "syncing": False,
    "publicIp": None,
    "zoneId": None,
    "domains": [],
    "counts": {},
}


def log(message: str) -> None:
    line = f"{time.strftime('%F %T')} {message}"
    print(line, flush=True)
    with cfg.log_file.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def request_json(method: str, url: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None
    headers = {
        "Authorization": f"Bearer {cfg.cf_api_token}",
        "Content-Type": "application/json",
    }
    if data is not None:
        body = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Cloudflare API error {exc.code}: {detail}") from exc


def request_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return resp.read().decode("utf-8").strip()


def build_url(path: str, query: dict[str, str] | None = None) -> str:
    url = f"https://api.cloudflare.com/client/v4{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


def get_public_ip() -> str:
    if cfg.public_ip != "auto":
        return cfg.public_ip
    url = "https://api64.ipify.org" if cfg.record_type == "AAAA" else "https://api.ipify.org"
    ip = request_text(url)
    ipaddress.ip_address(ip)
    return ip


def get_zone_id() -> str:
    data = request_json("GET", build_url("/zones", {"name": cfg.base_domain}))
    result = data.get("result") or []
    if not result:
        raise RuntimeError(f"Cannot get Cloudflare Zone ID for {cfg.base_domain}")
    return result[0]["id"]


def cf_records(zone_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page = 1
    while True:
        data = request_json(
            "GET",
            build_url(
                f"/zones/{zone_id}/dns_records",
                {
                    "type": cfg.record_type,
                    "per_page": "500",
                    "page": str(page),
                },
            ),
        )
        records.extend(data.get("result") or [])
        info = data.get("result_info") or {}
        if page >= int(info.get("total_pages") or 1):
            return records
        page += 1


def cf_get_record(zone_id: str, domain: str) -> dict[str, Any] | None:
    data = request_json(
        "GET",
        build_url(
            f"/zones/{zone_id}/dns_records",
            {
                "type": cfg.record_type,
                "name": domain,
            },
        ),
    )
    result = data.get("result") or []
    return result[0] if result else None


def record_payload(domain: str, ip: str) -> dict[str, Any]:
    return {
        "type": cfg.record_type,
        "name": domain,
        "content": ip,
        "ttl": 1,
        "proxied": cfg.proxied,
        "comment": cfg.mark,
    }


def cf_create_record(zone_id: str, domain: str, ip: str) -> None:
    request_json("POST", build_url(f"/zones/{zone_id}/dns_records"), record_payload(domain, ip))


def cf_update_record(zone_id: str, record_id: str, domain: str, ip: str) -> None:
    request_json("PUT", build_url(f"/zones/{zone_id}/dns_records/{record_id}"), record_payload(domain, ip))


def cf_delete_record(zone_id: str, record_id: str) -> None:
    request_json("DELETE", build_url(f"/zones/{zone_id}/dns_records/{record_id}"))


def read_owned_domains() -> set[str]:
    if not cfg.state_file.exists():
        return set()
    return {line.strip() for line in cfg.state_file.read_text(encoding="utf-8").splitlines() if line.strip()}


def write_owned_domains(domains: set[str]) -> None:
    cfg.state_file.write_text("\n".join(sorted(domains)) + ("\n" if domains else ""), encoding="utf-8")


def is_managed_domain(domain: str) -> bool:
    domain = domain.strip(".").lower()
    return domain == cfg.base_domain or domain.endswith("." + cfg.base_domain)


def scan_bt_domains() -> set[str]:
    if not cfg.watch_dir.is_dir():
        log(f"WARN: WATCH_DIR does not exist: {cfg.watch_dir}")
        return set()

    domains: set[str] = set()
    server_name_re = re.compile(r"^\s*server_name\s+([^;]+);", re.MULTILINE)
    for conf_file in cfg.watch_dir.glob("*.conf"):
        try:
            text = conf_file.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            log(f"WARN: cannot read {conf_file}: {exc}")
            continue
        for match in server_name_re.finditer(text):
            for raw_name in re.split(r"\s+", match.group(1).strip()):
                domain = raw_name.strip().strip(";").strip(".").lower()
                if not domain or domain.startswith("*") or domain == "_":
                    continue
                if is_managed_domain(domain):
                    domains.add(domain)
    return domains


def status_for(domain: str, bt_domains: set[str], owned_domains: set[str], cf_record: dict[str, Any] | None, public_ip: str) -> dict[str, Any]:
    cf_ip = (cf_record or {}).get("content")
    comment = (cf_record or {}).get("comment") or ""
    cf_exists = cf_record is not None
    is_owned = cfg.mark in comment

    if domain in bt_domains and not cf_exists:
        status = "missing"
        label = "未挂载"
    elif domain in bt_domains and cf_ip == public_ip:
        status = "ok" if is_owned else "adoptable"
        label = "正常" if is_owned else "可接管"
    elif domain in bt_domains and cf_exists:
        status = "other"
        label = "指向其他 VPS"
    elif domain not in bt_domains and domain in owned_domains:
        status = "orphan"
        label = "待删除"
    else:
        status = "external"
        label = "仅 Cloudflare"

    return {
        "domain": domain,
        "btExists": domain in bt_domains,
        "cfExists": cf_exists,
        "cfIp": cf_ip,
        "localIp": public_ip,
        "proxied": (cf_record or {}).get("proxied"),
        "comment": comment,
        "ownedByThis": is_owned,
        "stateTracked": domain in owned_domains,
        "status": status,
        "label": label,
    }


def build_status(zone_id: str, public_ip: str, bt_domains: set[str], owned_domains: set[str]) -> dict[str, Any]:
    cf_by_name = {
        record["name"].strip(".").lower(): record
        for record in cf_records(zone_id)
        if is_managed_domain(record.get("name", ""))
    }
    all_domains = sorted(bt_domains | owned_domains | set(cf_by_name.keys()))
    rows = [status_for(domain, bt_domains, owned_domains, cf_by_name.get(domain), public_ip) for domain in all_domains]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "startedAt": last_status.get("startedAt"),
        "lastSyncAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lastError": None,
        "syncing": False,
        "baseDomain": cfg.base_domain,
        "ownerId": cfg.owner_id,
        "recordType": cfg.record_type,
        "proxied": cfg.proxied,
        "publicIp": public_ip,
        "zoneId": zone_id,
        "watchDir": str(cfg.watch_dir),
        "deleteMissing": cfg.delete_missing,
        "adoptExisting": cfg.adopt_existing,
        "webAuthEnabled": bool(cfg.web_username and cfg.web_password),
        "domains": rows,
        "counts": counts,
    }


def ensure_record(zone_id: str, domain: str, ip: str) -> bool:
    record = cf_get_record(zone_id, domain)
    if not record:
        log(f"CREATE: {cfg.record_type} {domain} -> {ip}")
        cf_create_record(zone_id, domain, ip)
        return True

    old_ip = record.get("content")
    comment = record.get("comment") or ""
    if old_ip == ip:
        if cfg.mark in comment:
            log(f"OK: {cfg.record_type} {domain} -> {ip}")
            return True
        if cfg.adopt_existing:
            log(f"ADOPT: {cfg.record_type} {domain} -> {ip}")
            cf_update_record(zone_id, record["id"], domain, ip)
            return True
        log(f"SKIP: {domain} already points to {ip} but is not owned by {cfg.mark}")
        return False

    if cfg.mark in comment:
        log(f"UPDATE: {cfg.record_type} {domain} {old_ip} -> {ip}")
        cf_update_record(zone_id, record["id"], domain, ip)
        return True

    log(f"SKIP: {domain} already points to another IP: {old_ip}")
    return False


def delete_if_owned(zone_id: str, domain: str, ip: str) -> None:
    record = cf_get_record(zone_id, domain)
    if not record:
        log(f"DELETE-SKIP: {domain} not found")
        return
    comment = record.get("comment") or ""
    if record.get("content") == ip and cfg.mark in comment:
        log(f"DELETE: {cfg.record_type} {domain}")
        cf_delete_record(zone_id, record["id"])
    else:
        log(f"DELETE-SKIP: {domain} is not owned by {cfg.mark}")


def run_once() -> None:
    with state_lock:
        last_status["syncing"] = True
    zone_id = get_zone_id()
    public_ip = get_public_ip()
    bt_domains = scan_bt_domains()
    previous_owned = read_owned_domains()
    new_owned: set[str] = set()

    log(f"SYNC START: domain={cfg.base_domain} owner={cfg.owner_id} type={cfg.record_type} ip={public_ip} proxied={str(cfg.proxied).lower()}")
    for domain in sorted(bt_domains):
        if ensure_record(zone_id, domain, public_ip):
            new_owned.add(domain)

    if cfg.delete_missing:
        for domain in sorted(previous_owned - new_owned):
            delete_if_owned(zone_id, domain, public_ip)

    write_owned_domains(new_owned)
    status = build_status(zone_id, public_ip, bt_domains, new_owned)
    with state_lock:
        last_status.clear()
        last_status.update(status)
    log("SYNC DONE")


def sync_loop() -> None:
    while True:
        try:
            run_once()
        except Exception as exc:
            log(f"SYNC ERROR: {exc}")
            with state_lock:
                last_status["syncing"] = False
                last_status["lastError"] = str(exc)
        time.sleep(cfg.sleep_seconds)


def tail_log(max_lines: int = 80) -> list[str]:
    if not cfg.log_file.exists():
        return []
    lines = cfg.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max_lines:]


def page_html(snapshot: dict[str, Any]) -> str:
    rows = snapshot.get("domains") or []
    counts = snapshot.get("counts") or {}
    status_order = [
        ("ok", "正常"),
        ("missing", "未挂载"),
        ("other", "指向其他 VPS"),
        ("orphan", "待删除"),
        ("adoptable", "可接管"),
        ("external", "仅 CF"),
    ]
    cards = "".join(
        f'<div class="metric"><span>{label}</span><strong>{counts.get(key, 0)}</strong></div>'
        for key, label in status_order
    )
    table_rows = "".join(
        f"""
        <tr>
          <td><code>{html.escape(row["domain"])}</code></td>
          <td>{'是' if row["btExists"] else '否'}</td>
          <td>{'有' if row["cfExists"] else '无'}</td>
          <td>{html.escape(str(row.get("cfIp") or "-"))}</td>
          <td>{html.escape(str(row.get("localIp") or "-"))}</td>
          <td>{'是' if row["ownedByThis"] else '否'}</td>
          <td><span class="badge {html.escape(row["status"])}">{html.escape(row["label"])}</span></td>
        </tr>
        """
        for row in rows
    ) or '<tr><td colspan="7" class="empty">暂无域名。等待下一次扫描，或检查宝塔 Nginx 配置目录挂载。</td></tr>'
    log_lines = "\n".join(html.escape(line) for line in tail_log())
    auth_note = "已启用" if snapshot.get("webAuthEnabled") else "未启用"
    error = snapshot.get("lastError")
    error_html = f'<div class="alert">最近错误：{html.escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>bt-cf-sync 状态</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #101214;
      --panel: #181b1f;
      --panel-2: #20252b;
      --line: #303741;
      --text: #ece7dd;
      --muted: #a9b0b8;
      --green: #39d98a;
      --yellow: #f4c95d;
      --red: #ff6b6b;
      --blue: #6cb6ff;
      --cyan: #60d6d6;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", "Noto Sans SC", sans-serif;
      letter-spacing: 0;
    }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px 18px 36px; }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{ margin: 0 0 8px; font-size: 28px; font-weight: 700; }}
    .sub {{ color: var(--muted); line-height: 1.6; }}
    .meta {{ text-align: right; color: var(--muted); line-height: 1.7; font-size: 14px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; margin: 22px 0; }}
    .metric {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }}
    .metric span {{ display: block; color: var(--muted); font-size: 13px; }}
    .metric strong {{ display: block; font-size: 26px; margin-top: 4px; }}
    .section {{ margin-top: 18px; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }}
    table {{ width: 100%; border-collapse: collapse; min-width: 820px; }}
    th, td {{ padding: 12px 14px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; }}
    th {{ color: var(--muted); font-size: 13px; background: var(--panel-2); }}
    tr:last-child td {{ border-bottom: 0; }}
    code {{ color: var(--text); }}
    .badge {{ display: inline-flex; min-width: 82px; justify-content: center; border-radius: 999px; padding: 4px 10px; font-size: 13px; border: 1px solid var(--line); }}
    .ok {{ color: var(--green); border-color: rgba(57,217,138,.45); }}
    .missing, .orphan {{ color: var(--yellow); border-color: rgba(244,201,93,.45); }}
    .other {{ color: var(--red); border-color: rgba(255,107,107,.45); }}
    .adoptable {{ color: var(--cyan); border-color: rgba(96,214,214,.45); }}
    .external {{ color: var(--blue); border-color: rgba(108,182,255,.45); }}
    .alert {{ margin: 18px 0; padding: 12px 14px; border: 1px solid rgba(255,107,107,.45); color: var(--red); border-radius: 8px; background: rgba(255,107,107,.08); }}
    .logs {{ background: #0b0d0f; border: 1px solid var(--line); border-radius: 8px; padding: 14px; overflow: auto; max-height: 360px; color: #d4d0c8; font-size: 13px; line-height: 1.55; }}
    .empty {{ color: var(--muted); text-align: center; }}
    @media (max-width: 760px) {{
      header {{ display: block; }}
      .meta {{ text-align: left; margin-top: 12px; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>bt-cf-sync 状态</h1>
        <div class="sub">宝塔目录：<code>{html.escape(str(snapshot.get("watchDir") or ""))}</code><br>域名：<code>{html.escape(str(snapshot.get("baseDomain") or ""))}</code> · Owner：<code>{html.escape(str(snapshot.get("ownerId") or ""))}</code></div>
      </div>
      <div class="meta">
        本机 IP：<code>{html.escape(str(snapshot.get("publicIp") or "-"))}</code><br>
        记录类型：{html.escape(str(snapshot.get("recordType") or ""))} · 认证：{auth_note}<br>
        上次同步：{html.escape(str(snapshot.get("lastSyncAt") or "尚未成功"))}
      </div>
    </header>
    {error_html}
    <section class="metrics">{cards}</section>
    <section class="section table-wrap">
      <table>
        <thead><tr><th>域名</th><th>宝塔存在</th><th>CF 记录</th><th>CF 指向</th><th>本机 IP</th><th>本机管理</th><th>状态</th></tr></thead>
        <tbody>{table_rows}</tbody>
      </table>
    </section>
    <section class="section">
      <h2>最近日志</h2>
      <pre class="logs">{log_lines}</pre>
    </section>
  </main>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def check_auth(self) -> bool:
        if not (cfg.web_username and cfg.web_password):
            return True
        header = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"{cfg.web_username}:{cfg.web_password}".encode()).decode()
        if header == expected:
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="bt-cf-sync"')
        self.end_headers()
        return False

    def do_GET(self) -> None:
        if not self.check_auth():
            return
        with state_lock:
            snapshot = json.loads(json.dumps(last_status))
        if self.path == "/api/status":
            body = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in {"/", "/status"}:
            body = page_html(snapshot).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    threading.Thread(target=sync_loop, daemon=True).start()
    server = ThreadingHTTPServer((cfg.web_bind, cfg.web_port), Handler)
    log(f"WEB START: http://{cfg.web_bind}:{cfg.web_port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
