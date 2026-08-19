# Operations

Deploying, backing up, upgrading and troubleshooting a Venvy instance.

## Deployment

### systemd (recommended)

Run Venvy as a dedicated unprivileged user. It launches managed apps as
itself, so whatever it can reach, they can reach.

```bash
sudo useradd --system --create-home --shell /bin/bash venvy
sudo -u venvy python3 -m venv /home/venvy/.venv
sudo -u venvy /home/venvy/.venv/bin/pip install "venvy[metrics]"
sudo -u venvy mkdir -p /home/venvy/data
```

`/etc/systemd/system/venvy.service`:

```ini
[Unit]
Description=Venvy application manager
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=venvy
Group=venvy
WorkingDirectory=/home/venvy/data
Environment=VENVY_DATA_DIR=/home/venvy/data
Environment=VENVY_HOST=127.0.0.1
Environment=VENVY_PORT=54783
Environment=SESSION_COOKIE_SECURE=1
ExecStart=/home/venvy/.venv/bin/venvy
Restart=on-failure
RestartSec=5

# Venvy needs to create transient scopes for the apps it manages.
Delegate=yes

# Give managed apps time to exit before the whole unit is killed.
TimeoutStopSec=30
KillMode=mixed

# Modest hardening. It cannot go much further: Venvy's job is executing code.
NoNewPrivileges=no
PrivateTmp=false
ProtectSystem=false

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now venvy
sudo journalctl -u venvy -f
```

> **On the hardening directives.** Venvy exists to run arbitrary programs and
> create virtualenvs, so `ProtectSystem=strict` and friends break it rather
> than secure it. The isolation that matters here is the unprivileged user plus
> not exposing the port — not systemd sandboxing.

### Docker

```bash
docker compose up -d
docker compose logs -f venvy
```

The Compose file binds to `127.0.0.1` on purpose. Add the `cloudflared`
sidecar with `--profile tunnel` once `CLOUDFLARE_TUNNEL_TOKEN` is in `.env`.

Everything lives in the `venvy-data` volume. Back that up as a unit.

### Behind a reverse proxy

```nginx
location / {
    proxy_pass http://127.0.0.1:54783;
    proxy_http_version 1.1;

    # The terminal is a WebSocket; without these it will not connect.
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection "upgrade";

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;

    # A terminal or a long pip install must not be cut off mid-flight.
    proxy_read_timeout 3600s;

    client_max_body_size 64M;   # ≥ VENVY_MAX_UPLOAD_BYTES
}
```

Set `SESSION_COOKIE_SECURE=1` once TLS terminates in front.

> Venvy resolves client IPs from `CF-Connecting-IP` only, and deliberately
> ignores `X-Forwarded-For` (see [security.md](security.md)). Behind a plain
> reverse proxy the IP allowlist and login lockout therefore key on the proxy's
> address. Enforce client restrictions at the proxy in that setup.

---

## Backup and restore

Three things make up an instance. Two of them are useless without each other.

| | Safe to copy live? | |
|---|---|---|
| `data.db` | **No** — WAL mode | Use the backup endpoint |
| `.venvy_key` | Yes | Decrypts the secrets in `data.db` |
| `apps/` | Yes | Code, virtualenvs and logs |

### Taking a backup

```bash
curl -X GET  http://127.0.0.1:54783/api/health          # confirm it is up
# From the UI: Settings → Backup, or:
curl -X POST http://127.0.0.1:54783/api/system/backup \
     -H "x-csrf-token: $CSRF" -b "session=$SESSION;csrf_token=$CSRF"
```

That writes `backups/data-YYYYMMDD-HHMMSS.db` using SQLite's `VACUUM INTO`,
which reads inside its own transaction and always produces a consistent single
file. A plain `cp` of `data.db` can miss committed data still in the `-wal`
file.

Then copy the rest:

```bash
rsync -a /home/venvy/data/apps/       /backup/venvy/apps/
cp      /home/venvy/data/.venvy_key   /backup/venvy/.venvy_key
cp      /home/venvy/data/backups/data-*.db /backup/venvy/
```

A nightly cron:

```bash
0 3 * * * cd /home/venvy/data && \
  curl -fsS -X POST http://127.0.0.1:54783/api/system/backup \
       -H "x-csrf-token: $CSRF" -b "session=$SESSION;csrf_token=$CSRF" >/dev/null && \
  rsync -a --delete apps/ /backup/venvy/apps/ && \
  cp .venvy_key /backup/venvy/ && \
  find backups/ -name 'data-*.db' -mtime +14 -delete
```

> **Guard `.venvy_key` like a private key.** Without it, the encrypted columns
> in a restored database are unrecoverable — the app environment variables and
> the Cloudflare credentials are simply gone.

### Restoring

```bash
sudo systemctl stop venvy

cd /home/venvy/data
cp /backup/venvy/data-20260819-030000.db  data.db
cp /backup/venvy/.venvy_key               .venvy_key
chmod 600 .venvy_key
rsync -a /backup/venvy/apps/              apps/
rm -f data.db-wal data.db-shm             # stale sidecars from the old database
chown -R venvy:venvy /home/venvy/data

sudo systemctl start venvy
```

On startup Venvy reconciles the restored rows against reality: anything marked
`running` whose process is gone is cleared, and only start-on-boot apps are
started. Verify with `GET /api/health` and the app list.

---

## Upgrading

```bash
sudo systemctl stop venvy
sudo -u venvy /home/venvy/.venv/bin/pip install --upgrade "venvy[metrics]"
sudo systemctl start venvy
```

Take a backup first. Schema migrations are additive and run automatically at
startup; they are append-only, so an older database upgrades in place. There is
no automatic downgrade — restore from backup instead.

---

## Monitoring

`GET /api/health` is unauthenticated and outside the IP allowlist, so an
external monitor can always reach it. It performs a real database round-trip:

```json
{ "ok": true, "status": "healthy", "uptime_seconds": 3671 }
```

`503` with `"degraded"` means the database is unreachable or corrupt.

Audit events are `INFO` log lines prefixed `AUDIT:` — grep them or ship them
to a log aggregator:

```bash
journalctl -u venvy | grep AUDIT:
```

---

## Troubleshooting

### "python3-venv is not available"

Venvy probes for it at startup and prints the exact command. Install it:

```bash
sudo apt-get install -y python3.12-venv
```

Or set `VENVY_AUTO_INSTALL_SYSTEM_DEPS=1` to let Venvy try — off by default,
because a server process should not silently change host packages.

### Apps start without resource limits

The log says systemd-run is unavailable. Expected in most containers and on
non-systemd hosts. Apps still run; their CPU and memory ceilings are simply not
enforced. On a systemd host, check `Delegate=yes` is in the unit.

### A published hostname returns Cloudflare error 1016

Nothing is on the other end of the tunnel — the `cloudflared` connector is not
running. Settings → Test Cloudflare credentials reports the active connection
count and warns when it is zero.

### The terminal will not connect

- Through a reverse proxy: the `Upgrade` and `Connection` headers are missing
  (see the nginx block above).
- As a viewer: terminals require `operator` or `admin`.
- Not on Linux: PTYs are POSIX-only, and the close reason says so.

### Port already in use

Venvy re-checks an app's port immediately before launch and reassigns it to a
free one if it has been taken since, logging the change. If an app insists on a
fixed port, pin it in the app's settings — a pinned port is never reassigned.

### Resource graphs are empty

`psutil` is not installed. `pip install "venvy[metrics]"`. Everything else
keeps working through `/proc`.

### After a hard kill, apps show as running but are not

Restart Venvy. Startup reconciliation clears stale rows. If a real orphan is
still holding a port, find it with `ss -lptn 'sport = :PORT'` and kill it.
