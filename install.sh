#!/usr/bin/env bash
# Venvy installer and updater for Debian / Ubuntu.
#
#   curl -fsSL https://github.com/nikolaus018/venvy/releases/latest/download/install.sh | sudo bash
#
# Options go after `bash -s --`:
#
#   ... | sudo bash -s -- --port 8080 --host 127.0.0.1
#
#   --host ADDR       address to bind (default 0.0.0.0; 127.0.0.1 = tunnel only)
#   --port N          port (default 54783)
#   --version TAG     install a specific release (default: latest)
#   --with-rust       also install cargo/rustc so Rust apps can be hosted
#   --with-node       also install nodejs/npm so Node.js apps can be hosted
#   --with-go         also install Go (the current release from go.dev)
#   --with-bun        also install Bun
#   --with-deno       also install Deno
#   --with-php        also install php-cli and Composer
#   --with-ruby       also install Ruby and Bundler
#   --with-java       also install a headless Java runtime
#   --with-all        all of the runtimes above, and Rust
#   --app-users       run Venvy as root with one Unix user per hosted app
#   --uninstall       remove the service and code (data and config are kept)
#
# Run it again at any time to update: it downloads the release, verifies its
# checksum, prepares it next to the running one, snapshots the database,
# switches over, and switches back by itself if the new version does not
# come up healthy.
#
# Layout:
#   /opt/venvy/releases/<version>/   code + its own Python venv
#   /opt/venvy/current               -> the active release
#   /var/lib/venvy/                  data: data.db, apps/, backups/, .venvy_key
#   /etc/venvy/venvy.env             settings (never overwritten by an update)
#
# The whole script is one function called on its last line, so a download
# that was cut off is a syntax error and runs nothing.

venvy_install() {
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
# The service user has to be able to read the code this (root) installs,
# whatever umask the admin's shell happens to use.
umask 022

REPO="nikolaus018/venvy"
CODE_DIR="/opt/venvy"
CONF_DIR="/etc/venvy"
ENV_FILE="$CONF_DIR/venvy.env"
UNIT_FILE="/etc/systemd/system/venvy.service"
DEFAULT_DATA_DIR="/var/lib/venvy"
KEEP_DB_SNAPSHOTS=5

VERSION="latest"; HOST=""; PORT=""; WITH_RUST=0; WITH_NODE=0; WITH_GO=0; WITH_BUN=0; WITH_DENO=0; WITH_PHP=0; WITH_RUBY=0; WITH_JAVA=0; APP_USERS=""; UNINSTALL=0

# Printed from here rather than read back out of this file: piped into bash
# from curl there is no file to read.
usage() {
  cat <<'USAGE'
Venvy installer and updater for Debian / Ubuntu.

  curl -fsSL https://github.com/nikolaus018/venvy/releases/latest/download/install.sh | sudo bash

Options go after `bash -s --`:

  --host ADDR       address to bind (default 0.0.0.0; 127.0.0.1 = tunnel only)
  --port N          port (default 54783)
  --version TAG     install a specific release (default: latest)
  --with-rust       also install cargo/rustc so Rust apps can be hosted
  --with-node       also install nodejs/npm so Node.js apps can be hosted
  --with-go         also install Go (the current release from go.dev)
  --with-bun        also install Bun
  --with-deno       also install Deno
  --with-php        also install php-cli and Composer
  --with-ruby       also install Ruby and Bundler
  --with-java       also install a headless Java runtime
  --with-all        all of the runtimes above, and Rust
  --app-users       run Venvy as root with one Unix user per hosted app
  --uninstall       remove the service and code (data and config are kept)

Run it again at any time to update. Settings are in /etc/venvy/venvy.env,
data in /var/lib/venvy, code in /opt/venvy.
USAGE
}

say()  { printf '\033[1;34m[venvy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[venvy]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[venvy]\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host)      [ $# -ge 2 ] || die "--host needs a value"; HOST="$2"; shift 2 ;;
    --port)      [ $# -ge 2 ] || die "--port needs a value"; PORT="$2"; shift 2 ;;
    --version)   [ $# -ge 2 ] || die "--version needs a value"; VERSION="$2"; shift 2 ;;
    --with-rust) WITH_RUST=1; shift ;;
    --with-node) WITH_NODE=1; shift ;;
    --with-go)   WITH_GO=1; shift ;;
    --with-bun)  WITH_BUN=1; shift ;;
    --with-deno) WITH_DENO=1; shift ;;
    --with-php)  WITH_PHP=1; shift ;;
    --with-ruby) WITH_RUBY=1; shift ;;
    --with-java) WITH_JAVA=1; shift ;;
    --with-all)  WITH_RUST=1; WITH_NODE=1; WITH_GO=1; WITH_BUN=1; WITH_DENO=1; WITH_PHP=1; WITH_RUBY=1; WITH_JAVA=1; shift ;;
    --app-users) APP_USERS=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "Unknown option: $1 (see --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "This needs root. Run:  curl -fsSL <url> | sudo bash"
command -v apt-get   >/dev/null 2>&1 || die "This installer supports Debian and Ubuntu (apt-get not found)."
command -v systemctl >/dev/null 2>&1 || die "systemd is required (systemctl not found)."
if [ -n "$PORT" ]; then
  case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac
  [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "--port must be between 1 and 65535"
fi
case "$HOST" in *[!0-9A-Za-z.:_-]*) die "--host contains unexpected characters" ;; esac
case "$VERSION" in *[!0-9A-Za-z._-]*) die "--version contains unexpected characters" ;; esac

# ── Uninstall ────────────────────────────────────────────────────────────────
if [ "$UNINSTALL" -eq 1 ]; then
  DATA_DIR="$(systemctl show -p WorkingDirectory --value venvy.service 2>/dev/null || true)"
  say "Stopping and removing the Venvy service..."
  systemctl disable --now venvy.service >/dev/null 2>&1 || true
  rm -f "$UNIT_FILE"
  systemctl daemon-reload
  rm -rf "$CODE_DIR/releases" "$CODE_DIR/current"
  rmdir "$CODE_DIR" 2>/dev/null || true
  say "Venvy is uninstalled. Kept on purpose (delete them yourself if you are sure):"
  say "  data:    ${DATA_DIR:-$DEFAULT_DATA_DIR}"
  say "  config:  $CONF_DIR"
  exit 0
fi

# ── Where things are (fresh install vs. an existing one) ─────────────────────
# App records store absolute paths, so an existing data directory stays where
# it is — including one from the pre-installer days, in /opt/venvy itself.
DATA_DIR=""
if [ -f "$UNIT_FILE" ]; then
  WD="$(systemctl show -p WorkingDirectory --value venvy.service 2>/dev/null || true)"
  if [ -n "$WD" ] && [ -f "$WD/data.db" ]; then DATA_DIR="$WD"; fi
fi
if [ -z "$DATA_DIR" ] && [ -f "$CODE_DIR/data.db" ]; then DATA_DIR="$CODE_DIR"; fi
EXISTING_DATA=1
if [ -z "$DATA_DIR" ]; then DATA_DIR="$DEFAULT_DATA_DIR"; [ -f "$DATA_DIR/data.db" ] || EXISTING_DATA=0; fi

# Settings: flags win, then what is already in the env file, then defaults.
env_get() { [ -f "$ENV_FILE" ] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 || true; }
[ -n "$HOST" ]      || HOST="$(env_get HOST)";                   [ -n "$HOST" ]      || HOST="0.0.0.0"
[ -n "$PORT" ]      || PORT="$(env_get PORT)";                   [ -n "$PORT" ]      || PORT="54783"
[ -n "$APP_USERS" ] || APP_USERS="$(env_get VENVY_APP_USERS)";   [ -n "$APP_USERS" ] || APP_USERS="0"

# Who the service runs as.
if [ "$APP_USERS" = "1" ]; then
  RUN_USER="root"; RUN_GROUP="root"
elif [ "$EXISTING_DATA" -eq 1 ]; then
  RUN_USER="$(stat -c %U "$DATA_DIR/data.db")"; RUN_GROUP="$(stat -c %G "$DATA_DIR/data.db")"
else
  RUN_USER="venvy"; RUN_GROUP="venvy"
fi

if [ "$VERSION" = "latest" ]; then
  BASE_URL="${VENVY_BASE_URL:-https://github.com/$REPO/releases/latest/download}"
else
  BASE_URL="${VENVY_BASE_URL:-https://github.com/$REPO/releases/download/$VERSION}"
fi

say "Installing Venvy ($VERSION) — data in $DATA_DIR, service user $RUN_USER, http://$HOST:$PORT"

# ── 1. System packages (apt only runs if something is missing) ───────────────
PKGS="python3 python3-venv curl ca-certificates git tar"
[ "$WITH_RUST" -eq 1 ] && PKGS="$PKGS cargo rustc build-essential"
[ "$WITH_NODE" -eq 1 ] && PKGS="$PKGS nodejs npm"
[ "$WITH_PHP" -eq 1 ]  && PKGS="$PKGS php-cli composer unzip"
[ "$WITH_RUBY" -eq 1 ] && PKGS="$PKGS ruby ruby-bundler build-essential"
[ "$WITH_JAVA" -eq 1 ] && PKGS="$PKGS default-jre-headless"
# Bun and Deno come as zip files; Go needs nothing beyond curl and tar.
[ "$WITH_BUN" -eq 1 ] || [ "$WITH_DENO" -eq 1 ] && PKGS="$PKGS unzip"
MISSING=""
for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"; done
if [ -n "$MISSING" ]; then
  say "Installing system packages:$MISSING"
  apt-get update -y -q
  # shellcheck disable=SC2086
  apt-get install -y -q --no-install-recommends $MISSING
fi
# Runtimes that are not in Debian/Ubuntu's repositories, or too old there to be
# any use (Go): the official release, checked against the checksum its
# publisher lists. A tool that is already installed is left alone.
case "$(uname -m)" in
  x86_64|amd64)  ARCH_GO=amd64; ARCH_BUN=x64;     ARCH_DENO=x86_64 ;;
  aarch64|arm64) ARCH_GO=arm64; ARCH_BUN=aarch64; ARCH_DENO=aarch64 ;;
  *)             ARCH_GO="";    ARCH_BUN="";      ARCH_DENO="" ;;
esac

fetch_to() {  # URL FILE — or stop with a message
  curl -fsSL --retry 3 -o "$2" "$1" || { rm -rf "${WORK:-}"; die "Could not download $1"; }
}
verify_sha256() {  # FILE EXPECTED NAME
  [ -n "$2" ] || { rm -rf "${WORK:-}"; die "No checksum was published for $3."; }
  [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$2" ] || { rm -rf "${WORK:-}"; die "Checksum mismatch for $3 — nothing was installed."; }
}

install_go() {
  command -v go >/dev/null 2>&1 && return 0
  [ -n "$ARCH_GO" ] || die "Go: unsupported CPU architecture $(uname -m)."
  say "Installing Go..."
  WORK="$(mktemp -d)"
  # shellcheck disable=SC2046
  set -- $(python3 - "$ARCH_GO" <<'PY'
import json, sys, urllib.request
rel = json.load(urllib.request.urlopen("https://go.dev/dl/?mode=json", timeout=30))[0]
for f in rel["files"]:
    if f["os"] == "linux" and f["arch"] == sys.argv[1] and f["kind"] == "archive":
        print(f["filename"], f["sha256"])
        break
PY
  )
  [ $# -eq 2 ] || { rm -rf "$WORK"; die "Could not find the current Go release on go.dev."; }
  fetch_to "https://go.dev/dl/$1" "$WORK/go.tgz"
  verify_sha256 "$WORK/go.tgz" "$2" "$1"
  rm -rf /usr/local/go
  tar -C /usr/local -xzf "$WORK/go.tgz"
  ln -sf /usr/local/go/bin/go /usr/local/bin/go
  ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt
  rm -rf "$WORK"
}

install_bun() {
  command -v bun >/dev/null 2>&1 && return 0
  [ -n "$ARCH_BUN" ] || die "Bun: unsupported CPU architecture $(uname -m)."
  say "Installing Bun..."
  WORK="$(mktemp -d)"
  base="https://github.com/oven-sh/bun/releases/latest/download"; zip="bun-linux-$ARCH_BUN.zip"
  fetch_to "$base/$zip" "$WORK/$zip"
  fetch_to "$base/SHASUMS256.txt" "$WORK/SHASUMS256.txt"
  verify_sha256 "$WORK/$zip" "$(awk -v f="$zip" '$2 == f || $2 == "./" f {print $1}' "$WORK/SHASUMS256.txt")" "$zip"
  unzip -q -o "$WORK/$zip" -d "$WORK"
  install -m 0755 "$WORK/bun-linux-$ARCH_BUN/bun" /usr/local/bin/bun
  rm -rf "$WORK"
}

install_deno() {
  command -v deno >/dev/null 2>&1 && return 0
  [ -n "$ARCH_DENO" ] || die "Deno: unsupported CPU architecture $(uname -m)."
  say "Installing Deno..."
  WORK="$(mktemp -d)"
  base="https://github.com/denoland/deno/releases/latest/download"; zip="deno-$ARCH_DENO-unknown-linux-gnu.zip"
  fetch_to "$base/$zip" "$WORK/$zip"
  fetch_to "$base/$zip.sha256sum" "$WORK/$zip.sha256sum"
  verify_sha256 "$WORK/$zip" "$(awk '{print $1; exit}' "$WORK/$zip.sha256sum")" "$zip"
  unzip -q -o "$WORK/$zip" -d "$WORK"
  install -m 0755 "$WORK/deno" /usr/local/bin/deno
  rm -rf "$WORK"
}

[ "$WITH_GO" -eq 1 ]   && install_go
[ "$WITH_BUN" -eq 1 ]  && install_bun
[ "$WITH_DENO" -eq 1 ] && install_deno

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10 or newer is required (found $(python3 -V 2>&1)). Debian 12 / Ubuntu 22.04 or later have it."

# ── 2. Download and verify ───────────────────────────────────────────────────
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
say "Downloading $BASE_URL/venvy.tar.gz"
curl -fsSL --retry 3 -o "$TMP/venvy.tar.gz"        "$BASE_URL/venvy.tar.gz" \
  || die "Download failed. Is the release published (and the repository public)?"
curl -fsSL --retry 3 -o "$TMP/venvy.tar.gz.sha256" "$BASE_URL/venvy.tar.gz.sha256" \
  || die "Could not download the checksum file."
( cd "$TMP" && sha256sum -c --status venvy.tar.gz.sha256 ) \
  || die "Checksum mismatch — the download is corrupt or was tampered with. Nothing was changed."

mkdir "$TMP/src"
tar -xzf "$TMP/venvy.tar.gz" -C "$TMP/src"
for f in main.py requirements.txt VERSION venvy/app.py web/index.html; do
  [ -f "$TMP/src/$f" ] || die "The release archive is incomplete (missing $f). Nothing was changed."
done
REL_VERSION="$(tr -cd '0-9A-Za-z._-' < "$TMP/src/VERSION")"
[ -n "$REL_VERSION" ] || die "The release archive has no version."

# ── 3. Prepare the release next to the running one ───────────────────────────
REL_DIR="$CODE_DIR/releases/$REL_VERSION"
PREV_DIR="$(readlink -f "$CODE_DIR/current" 2>/dev/null || true)"
mkdir -p "$CODE_DIR/releases"
STAGE="$CODE_DIR/releases/.staging-$REL_VERSION"
rm -rf "$STAGE"
mv "$TMP/src" "$STAGE"

say "Building the Python environment for $REL_VERSION..."
python3 -m venv "$STAGE/.venv"
"$STAGE/.venv/bin/pip" install -q --disable-pip-version-check -r "$STAGE/requirements.txt" \
  || { rm -rf "$STAGE"; die "Installing Python dependencies failed. Nothing was changed."; }
# Does it even import? Checked from an empty directory, before anything is stopped.
( cd "$TMP" && "$STAGE/.venv/bin/python" -c "import sys; sys.path.insert(0, '$STAGE'); import venvy.app" >/dev/null 2>"$TMP/import.log" ) \
  || { cat "$TMP/import.log" >&2; rm -rf "$STAGE"; die "The new version failed to load. Nothing was changed."; }
chown -R root:root "$STAGE"
chmod -R go-w "$STAGE"

# ── 4. Service user, data and config ─────────────────────────────────────────
if [ "$RUN_USER" = "venvy" ] && ! id venvy >/dev/null 2>&1; then
  say "Creating the 'venvy' system user..."
  useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin venvy
fi
if [ ! -d "$DATA_DIR" ]; then
  mkdir -p "$DATA_DIR"
  chown "$RUN_USER:$RUN_GROUP" "$DATA_DIR"
  # With per-app users each app's user must be able to pass through (not
  # list) this directory to reach its own files; the database, key and
  # backups inside are closed individually.
  if [ "$APP_USERS" = "1" ]; then chmod 711 "$DATA_DIR"; else chmod 750 "$DATA_DIR"; fi
fi

mkdir -p "$CONF_DIR"
chmod 755 "$CONF_DIR"
if [ ! -f "$ENV_FILE" ]; then
  say "Writing $ENV_FILE"
  ( umask 077; cat > "$ENV_FILE" <<EOF
# Venvy settings — read by the systemd service. Edit, then:
#   sudo systemctl restart venvy
# This file is never overwritten by an update.

# Address and port to listen on. Use 127.0.0.1 to be reachable only through
# a Cloudflare tunnel (cloudflared on this host).
HOST=$HOST
PORT=$PORT

# Set to 1 once Venvy is only reached over HTTPS (e.g. through the tunnel),
# so the session cookie is never sent over plain HTTP.
SESSION_COOKIE_SECURE=0

# One Unix user per hosted app (requires the service to run as root).
VENVY_APP_USERS=$APP_USERS

# Origins allowed to call the API from a browser, comma separated.
# Same-origin use needs nothing here.
#ALLOWED_ORIGINS=https://venvy.example.com

# Only these client IPs may use Venvy, comma separated.
#IP_ALLOWLIST=203.0.113.7

# Where cloudflared connects from, if not this host.
#TRUSTED_PROXY_IPS=127.0.0.1,::1

# Shared secret proving a request came through your tunnel: set it here and
# send the same value as the X-Venvy-Proxy-Secret header from a Cloudflare
# request-header transform rule.
#VENVY_PROXY_SECRET=
EOF
  )
else
  # Existing file: only touch what was asked for on the command line.
  env_set() {
    if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"; fi
  }
  env_set HOST "$HOST"; env_set PORT "$PORT"; env_set VENVY_APP_USERS "$APP_USERS"
fi
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"

# ── 5. Stop, snapshot the database, switch over ──────────────────────────────
if systemctl is-active --quiet venvy.service; then
  say "Stopping the running Venvy..."
  systemctl stop venvy.service
fi

if [ -f "$DATA_DIR/data.db" ]; then
  # Named like Venvy's own database-only backups, so it shows up (and can be
  # downloaded) under System → Backups.
  SNAP="$DATA_DIR/backups/data-$(date -u +%Y%m%d-%H%M%S).db"
  say "Snapshotting the database -> $SNAP"
  install -d -m 700 -o "$RUN_USER" -g "$RUN_GROUP" "$DATA_DIR/backups"
  python3 - "$DATA_DIR/data.db" "$SNAP" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
  chown "$RUN_USER:$RUN_GROUP" "$SNAP"; chmod 600 "$SNAP"
  # shellcheck disable=SC2012
  ls -1t "$DATA_DIR"/backups/data-*.db 2>/dev/null | tail -n +$((KEEP_DB_SNAPSHOTS + 1)) | while read -r old; do rm -f "$old"; done
fi

rm -rf "$REL_DIR"
mv "$STAGE" "$REL_DIR"
ln -sfn "$REL_DIR" "$CODE_DIR/current"

cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Venvy application manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$DATA_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
ExecStart=$CODE_DIR/current/.venv/bin/python $CODE_DIR/current/main.py
Restart=always
RestartSec=5
TimeoutStopSec=30
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable venvy.service >/dev/null 2>&1
systemctl restart venvy.service

# ── 6. Health check, automatic rollback ──────────────────────────────────────
case "$HOST" in 0.0.0.0|'') PROBE_HOST="127.0.0.1" ;; ::|'[::]') PROBE_HOST="[::1]" ;; *:*) PROBE_HOST="[$HOST]" ;; *) PROBE_HOST="$HOST" ;; esac
say "Waiting for Venvy to come up..."
HEALTHY=0
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "http://$PROBE_HOST:$PORT/api/health" 2>/dev/null; then HEALTHY=1; break; fi
  sleep 1
done

if [ "$HEALTHY" -ne 1 ]; then
  warn "Venvy $REL_VERSION did not become healthy. Last log lines:"
  journalctl -u venvy.service -n 30 --no-pager >&2 || true
  if [ -n "$PREV_DIR" ] && [ -d "$PREV_DIR" ] && [ "$PREV_DIR" != "$REL_DIR" ]; then
    warn "Switching back to $(basename "$PREV_DIR")..."
    ln -sfn "$PREV_DIR" "$CODE_DIR/current"
    systemctl restart venvy.service
    rm -rf "$REL_DIR"
    for _ in $(seq 1 60); do
      if curl -fsS -o /dev/null "http://$PROBE_HOST:$PORT/api/health" 2>/dev/null; then
        die "The update was rolled back; the previous version is running again. The database snapshot is in $DATA_DIR/backups."
      fi
      sleep 1
    done
    die "The update failed and the previous version did not come back up either. Check:  journalctl -u venvy -e"
  fi
  die "Venvy is installed but not responding. Check:  journalctl -u venvy -e"
fi

# Keep the active release and the one before it.
for d in "$CODE_DIR"/releases/*/; do
  d="${d%/}"
  [ "$d" = "$REL_DIR" ] || [ "$d" = "$PREV_DIR" ] || rm -rf "$d"
done

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
case "$HOST" in 127.0.0.1|localhost|::1) SHOW="127.0.0.1 (this machine only — reach it through your tunnel)" ;; *) SHOW="${IP:-$HOST}" ;; esac
echo
say "Venvy $REL_VERSION is running."
say "  Open:      http://$SHOW:$PORT"
[ "$EXISTING_DATA" -eq 0 ] && say "             the first visit creates the admin account"
say "  Settings:  $ENV_FILE   (then: systemctl restart venvy)"
say "  Data:      $DATA_DIR"
say "  Logs:      journalctl -u venvy -f"
say "  Update:    run this same command again"
}

venvy_install "$@"
