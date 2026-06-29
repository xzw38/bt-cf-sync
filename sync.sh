#!/usr/bin/env bash
set -euo pipefail

WATCH_DIR="${WATCH_DIR:-/bt-nginx}"
STATE_FILE="${STATE_FILE:-/data/owned-domains.txt}"
LOG_FILE="${LOG_FILE:-/data/sync.log}"

: "${CF_API_TOKEN:?CF_API_TOKEN is required}"
: "${BASE_DOMAIN:?BASE_DOMAIN is required}"
: "${OWNER_ID:=default}"
: "${PUBLIC_IP:=auto}"
: "${PROXIED:=true}"
: "${SLEEP_SECONDS:=60}"
: "${ADOPT_EXISTING:=true}"
: "${DELETE_MISSING:=true}"
: "${RECORD_TYPE:=A}"

mkdir -p "$(dirname "$STATE_FILE")" "$(dirname "$LOG_FILE")"
touch "$STATE_FILE"

log() {
  echo "$(date '+%F %T') $*" | tee -a "$LOG_FILE"
}

normalize_bool() {
  case "${1,,}" in
    true|1|yes|y|on) echo "true" ;;
    false|0|no|n|off) echo "false" ;;
    *) return 1 ;;
  esac
}

PROXIED_JSON="$(normalize_bool "$PROXIED" || true)"
ADOPT_EXISTING_BOOL="$(normalize_bool "$ADOPT_EXISTING" || true)"
DELETE_MISSING_BOOL="$(normalize_bool "$DELETE_MISSING" || true)"

if [ -z "$PROXIED_JSON" ] || [ -z "$ADOPT_EXISTING_BOOL" ] || [ -z "$DELETE_MISSING_BOOL" ]; then
  log "ERROR: PROXIED, ADOPT_EXISTING, and DELETE_MISSING must be true or false"
  exit 1
fi

case "$RECORD_TYPE" in
  A|AAAA) ;;
  *)
    log "ERROR: RECORD_TYPE must be A or AAAA"
    exit 1
    ;;
esac

get_public_ip() {
  if [ "$PUBLIC_IP" != "auto" ]; then
    echo "$PUBLIC_IP"
    return 0
  fi

  if [ "$RECORD_TYPE" = "AAAA" ]; then
    curl -6 -fsS https://api64.ipify.org
  else
    curl -4 -fsS https://api.ipify.org
  fi
}

cf_request() {
  curl -fsS "$@" \
    -H "Authorization: Bearer $CF_API_TOKEN" \
    -H "Content-Type: application/json"
}

get_zone_id() {
  cf_request -G "https://api.cloudflare.com/client/v4/zones" \
    --data-urlencode "name=$BASE_DOMAIN" |
    jq -r '.result[0].id // empty'
}

ZONE_ID="$(get_zone_id)"
if [ -z "$ZONE_ID" ]; then
  log "ERROR: Cannot get Cloudflare Zone ID for $BASE_DOMAIN"
  exit 1
fi

API="https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records"
MARK="bt-cf-sync:$OWNER_ID"

get_current_domains() {
  local base_re
  base_re="$(echo "$BASE_DOMAIN" | sed 's/\./\\./g')"

  if [ ! -d "$WATCH_DIR" ]; then
    log "WARN: WATCH_DIR does not exist: $WATCH_DIR"
    return 0
  fi

  grep -hRE '^[[:space:]]*server_name[[:space:]]+' "$WATCH_DIR"/*.conf 2>/dev/null |
    sed -E 's/^[[:space:]]*server_name[[:space:]]+//; s/;//g' |
    tr '[:space:]' '\n' |
    sed '/^$/d' |
    grep -v '^\*' |
    grep -v '^_' |
    grep -Ei "(^|\\.)${base_re}$" |
    sort -u || true
}

cf_get_record() {
  local domain="$1"
  cf_request -G "$API" \
    --data-urlencode "type=$RECORD_TYPE" \
    --data-urlencode "name=$domain"
}

record_payload() {
  local domain="$1"
  local ip="$2"

  jq -n \
    --arg type "$RECORD_TYPE" \
    --arg name "$domain" \
    --arg content "$ip" \
    --arg comment "$MARK" \
    --argjson proxied "$PROXIED_JSON" \
    '{
      type: $type,
      name: $name,
      content: $content,
      ttl: 1,
      proxied: $proxied,
      comment: $comment
    }'
}

cf_create_record() {
  local domain="$1"
  local ip="$2"
  cf_request -X POST "$API" --data "$(record_payload "$domain" "$ip")" >/dev/null
}

cf_update_record() {
  local id="$1"
  local domain="$2"
  local ip="$3"
  cf_request -X PUT "$API/$id" --data "$(record_payload "$domain" "$ip")" >/dev/null
}

cf_delete_record() {
  local id="$1"
  cf_request -X DELETE "$API/$id" >/dev/null
}

ensure_record() {
  local domain="$1"
  local ip="$2"
  local resp id old_ip comment

  resp="$(cf_get_record "$domain" || true)"
  id="$(echo "$resp" | jq -r '.result[0].id // empty')"
  old_ip="$(echo "$resp" | jq -r '.result[0].content // empty')"
  comment="$(echo "$resp" | jq -r '.result[0].comment // empty')"

  if [ -z "$id" ]; then
    log "CREATE: $RECORD_TYPE $domain -> $ip"
    cf_create_record "$domain" "$ip"
    return 0
  fi

  if [ "$old_ip" = "$ip" ]; then
    if [[ "$comment" == *"$MARK"* ]]; then
      log "OK: $RECORD_TYPE $domain -> $ip"
      return 0
    fi

    if [ "$ADOPT_EXISTING_BOOL" = "true" ]; then
      log "ADOPT: $RECORD_TYPE $domain -> $ip"
      cf_update_record "$id" "$domain" "$ip"
      return 0
    fi

    log "SKIP: $domain already points to $ip but is not owned by $MARK"
    return 1
  fi

  if [[ "$comment" == *"$MARK"* ]]; then
    log "UPDATE: $RECORD_TYPE $domain $old_ip -> $ip"
    cf_update_record "$id" "$domain" "$ip"
    return 0
  fi

  log "SKIP: $domain already points to another IP: $old_ip"
  return 1
}

delete_if_owned() {
  local domain="$1"
  local ip="$2"
  local resp id old_ip comment

  resp="$(cf_get_record "$domain" || true)"
  id="$(echo "$resp" | jq -r '.result[0].id // empty')"
  old_ip="$(echo "$resp" | jq -r '.result[0].content // empty')"
  comment="$(echo "$resp" | jq -r '.result[0].comment // empty')"

  if [ -z "$id" ]; then
    log "DELETE-SKIP: $domain not found"
    return 0
  fi

  if [ "$old_ip" = "$ip" ] && [[ "$comment" == *"$MARK"* ]]; then
    log "DELETE: $RECORD_TYPE $domain"
    cf_delete_record "$id"
  else
    log "DELETE-SKIP: $domain is not owned by $MARK"
  fi
}

run_once() {
  local ip tmp_current tmp_new_state tmp_removed

  ip="$(get_public_ip)"
  log "SYNC START: domain=$BASE_DOMAIN owner=$OWNER_ID type=$RECORD_TYPE ip=$ip proxied=$PROXIED_JSON"

  tmp_current="$(mktemp)"
  tmp_new_state="$(mktemp)"
  tmp_removed="$(mktemp)"

  get_current_domains > "$tmp_current"

  while read -r domain; do
    [ -z "$domain" ] && continue
    if ensure_record "$domain" "$ip"; then
      echo "$domain" >> "$tmp_new_state"
    fi
  done < "$tmp_current"

  sort -u "$tmp_new_state" -o "$tmp_new_state"

  if [ "$DELETE_MISSING_BOOL" = "true" ]; then
    comm -23 <(sort -u "$STATE_FILE") <(sort -u "$tmp_new_state") > "$tmp_removed" || true

    while read -r domain; do
      [ -z "$domain" ] && continue
      delete_if_owned "$domain" "$ip"
    done < "$tmp_removed"
  fi

  cp "$tmp_new_state" "$STATE_FILE"
  rm -f "$tmp_current" "$tmp_new_state" "$tmp_removed"

  log "SYNC DONE"
}

while true; do
  run_once || log "SYNC ERROR"
  sleep "$SLEEP_SECONDS"
done
