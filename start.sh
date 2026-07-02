#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${ROOT_DIR}/data"
CONFIG_FILE="${DATA_DIR}/monitor-config.json"

info() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m==>\033[0m %s\n' "$*"; }
err()  { printf '\n\033[1;31m==>\033[0m %s\n' "$*" >&2; }

require_docker() {
  command -v docker >/dev/null 2>&1 || {
    err "Docker is required. Install Docker Desktop and try again."
    exit 1
  }
  docker compose version >/dev/null 2>&1 || {
    err "Docker Compose v2 is required."
    exit 1
  }
}

valid_email() {
  [[ "$1" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]
}

write_config() {
  local alert_to="$1"
  local smtp_password="${2:-}"
  local email_enabled="false"
  if [[ -n "${smtp_password}" ]]; then
    email_enabled="true"
  fi

  mkdir -p "${DATA_DIR}"
  python3 - "${CONFIG_FILE}" "${alert_to}" "${smtp_password}" "${email_enabled}" <<'PY'
import json
import sys

path, alert_to, password, email_enabled = sys.argv[1:5]
payload = {
    "alert_to": alert_to,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_user": alert_to,
    "smtp_password": password,
    "smtp_use_tls": True,
    "email_alerts_enabled": email_enabled == "true",
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2)
    handle.write("\n")
PY
}

first_time_setup() {
  if [[ -f "${CONFIG_FILE}" ]]; then
    return
  fi

  info "First-time setup"
  echo "This tool deploys a fresh IDMP + TSDB + TDGPT stack and watches step-1"
  echo "verification emails. Enter where alerts should be sent."
  echo

  local alert_to=""
  while true; do
    read -r -p "Alert email address: " alert_to
    if valid_email "${alert_to}"; then
      break
    fi
    err "Please enter a valid email address."
  done

  echo
  echo "Optional: enter an SMTP app password to email alerts to you."
  echo "Press Enter to skip email delivery (alerts will appear in logs and at http://localhost:18088/alerts)."
  local smtp_password=""
  read -r -s -p "SMTP app password (hidden): " smtp_password || true
  echo

  write_config "${alert_to}" "${smtp_password}"
  info "Saved monitor settings to ${CONFIG_FILE}"
}

pull_latest_images() {
  info "Pulling latest TDengine images (IDMP, TSDB, TDGPT, IDMP AI)"
  docker compose -f "${ROOT_DIR}/docker-compose.yml" pull
}

deploy_stack() {
  local fresh="${1:-false}"
  cd "${ROOT_DIR}"

  if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:6042 -sTCP:LISTEN >/dev/null 2>&1; then
    if ! docker ps --format '{{.Names}}' | grep -qx 'idmp-monitor-idmp'; then
      err "Port 6042 is already in use by another process."
      echo "Stop the other IDMP stack first, for example:"
      echo "  docker compose -p idmp-tsdb-tdgpt down"
      exit 1
    fi
  fi

  if [[ "${fresh}" == "true" ]]; then
    warn "Removing existing stack and volumes for a clean deployment"
    docker compose down -v --remove-orphans || true
  fi

  info "Building monitor and starting the full stack"
  docker compose up -d --build

  info "Waiting for IDMP to become healthy"
  local attempt=0
  until docker inspect --format='{{json .State.Health.Status}}' idmp-monitor-idmp 2>/dev/null | grep -q healthy; do
    attempt=$((attempt + 1))
    if [[ "${attempt}" -ge 60 ]]; then
      err "IDMP did not become healthy in time. Check: docker compose logs tdengine-idmp"
      exit 1
    fi
    sleep 5
  done

  info "Stack is up"
  echo
  echo "  IDMP UI:          http://localhost:6042"
  echo "  Monitor health:   http://localhost:18088/health"
  echo "  Monitor alerts:   http://localhost:18088/alerts"
  echo "  Monitor logs:     docker logs -f idmp-verification-monitor"
  echo
  echo "The monitor is now watching IDMP logs for step-1 verification email delivery."
}

usage() {
  cat <<EOF
Usage: ./start.sh [--fresh]

  ./start.sh         Pull latest images, deploy stack, start monitoring
  ./start.sh --fresh Wipe volumes and deploy a completely fresh IDMP stack

No .env file is required. On first run you will be prompted for an alert email.
EOF
}

main() {
  local fresh="false"
  case "${1:-}" in
    "") ;;
    --fresh) fresh="true" ;;
    -h|--help) usage; exit 0 ;;
    *) err "Unknown option: $1"; usage; exit 1 ;;
  esac

  require_docker
  first_time_setup
  pull_latest_images
  deploy_stack "${fresh}"
}

main "$@"
