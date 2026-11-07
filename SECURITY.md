# Security

Venvy runs as root on the server it manages and hands out shells into the apps it hosts, so a
flaw in it is a flaw in the whole machine. This page says what it is built to protect, what it
deliberately does not, how releases can be checked, and how to report a problem.

## Reporting a vulnerability

Please report it privately through GitHub:
**[Report a vulnerability](https://github.com/nikolaus018/venvy/security/advisories/new)**
(the repository's Security tab → *Report a vulnerability*). Don't open a public issue for it.

Include what you did, what happened, and the version (System → Venvy, or `/opt/venvy/current/VERSION`).
I aim to reply within a week, fix confirmed issues in a release as quickly as their severity
asks for, and credit you in the changelog and the advisory unless you would rather not be named.

Only the latest release gets fixes. Updating is one click in System, or the install command again.

## What Venvy protects

With the installer's default of **one Unix user per app**:

| Threat | How it is handled |
|---|---|
| A hosted app reading Venvy's database, its encryption key, or another app | Each app, its terminal, jobs, builds and package installs run as its own locked `venvy-app-<id>` user that owns only its `0700` directory. Venvy's own files are root-only. |
| An app tricking root into touching files outside its folder (symlink or hard-link swaps) | No path is used twice: file operations, copies, backups, deploy keys and chowns walk from the app directory with `O_NOFOLLOW` and work on directory descriptors. A swapped link is refused, never followed. |
| An app posing as another client on loopback (to pass the IP allowlist or the login lockout) | A loopback connection is only taken for cloudflared's when no hosted app owns the socket (Linux, `/proc/net/tcp`). `VENVY_PROXY_SECRET` additionally requires a secret header from the tunnel. |
| An app taking over Venvy's own address | Hostnames Venvy has been signed in under can't become an app's domain or tunnel route. |
| Publishing a port that isn't the app's (another app, a database on localhost) | The tunnel only publishes one of that app's own ports, never Venvy's or another app's, and none that something other than the app is listening on. |
| Password guessing | scrypt hashes; per-address and per-username lockouts that grow from 5 minutes to an hour; the same response time for unknown users; optional TOTP with replay protection. |
| Session theft through a hosted app on another port of the same host | A session needs the HttpOnly cookie **and** a per-session key kept in Venvy's own `localStorage`, which other ports can't read. |
| Cross-site requests and injected markup | CSRF tokens on every change; a Content-Security-Policy without inline script; event handlers that can only call Venvy's own functions. |
| A stolen database or backup | App variables, the Cloudflare token, TOTP and webhook secrets are encrypted with a key kept beside the database (not in it); session cookies and API tokens are stored as hashes only. |
| A first-run install found by someone else before its owner | Creating the first admin needs a one-time code printed by the installer and stored root-only on the server. |
| A tampered release or update | Releases are signed with an Ed25519 key that is never on GitHub. The installer and the updater install nothing that doesn't verify (see below). |

## What Venvy does not protect against

These are deliberate, so you can decide whether Venvy fits:

- **Admins and operators are trusted with the apps.** An operator can run any command as an
  app's user (terminal, build commands, jobs) and read that app's files and variables. Give
  people the *viewer* role, or limit them to chosen apps, if that is more than they should have.
- **`--shared-user` gives up isolation.** Every app then runs as the same `venvy` user, can read
  every other app and Venvy's database and key, and an operator's terminal can too.
- **Apps share the host's network and kernel.** Venvy is not a container or VM sandbox: an app
  can reach anything on the network the host can, and a kernel exploit is out of its hands.
  CPU and memory limits come from systemd scopes and need systemd.
- **Other software on the host is trusted.** Anything else running there can claim any client
  address on loopback unless `VENVY_PROXY_SECRET` is set, and root is root.
- **Plain HTTP is your choice.** Venvy speaks HTTP; reached by a LAN address, passwords cross the
  network unencrypted. Put it behind the Cloudflare tunnel (or another TLS proxy) and bind it to
  `127.0.0.1`.
- **The very first `curl | sudo bash` trusts what it downloads**, as any such install does.
  Verify it by hand first if that matters to you (next section).

## Checking a release yourself

Each release carries `venvy.tar.gz`, its SHA-256 (`venvy.tar.gz.sha256`) and a signature of that
checksum (`venvy.tar.gz.sha256.sig`). Releases are signed with this key:

```
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAajX9BBDJMsBY6qPPyBa9/svuCnIenVQIx7WhO2tGpUM=
-----END PUBLIC KEY-----
```

SHA-256 of the key (DER): `b803bd57d71eb69dd05f0bedf68aad7a8be6af56d35abea6fbc0db279877b384`.
It is also `RELEASE_PUBKEY` in [install.sh](install.sh). Compare it in more than one place
(this file, `install.sh`, a release you installed before) before you rely on it.

To install without running anything you haven't checked (OpenSSL 3, as on Debian 12 and Ubuntu 22.04):

```bash
V=v4.8.2   # the release you want
B=https://github.com/nikolaus018/venvy/releases/download/$V
mkdir venvy-$V && cd venvy-$V
for f in venvy.tar.gz venvy.tar.gz.sha256 venvy.tar.gz.sha256.sig; do curl -fsSLO "$B/$f"; done
cat > venvy-release-key.pem <<'EOF'
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAajX9BBDJMsBY6qPPyBa9/svuCnIenVQIx7WhO2tGpUM=
-----END PUBLIC KEY-----
EOF
openssl pkeyutl -verify -pubin -inkey venvy-release-key.pem -rawin \
  -in venvy.tar.gz.sha256 -sigfile venvy.tar.gz.sha256.sig     # "Signature Verified Successfully"
sha256sum -c venvy.tar.gz.sha256                               # "venvy.tar.gz: OK"
tar -xzf venvy.tar.gz install.sh
less install.sh                                                # read it
sudo bash install.sh --version "$V"
```

The installer then downloads that release again and checks it against the key built into it,
and every later update is checked against the key the server was installed with.

**What the signature means.** `scripts/sign-release.sh` runs on the maintainer's machine, not in
CI. Before it signs, it unpacks the archive the release workflow built and compares it with the
tagged source, file by file. So a valid signature says: this archive is exactly the code at that
tag, and the maintainer looked at it. Someone who got into the GitHub account or the workflow
could publish a release, but not one that installs.

## How it is tested

The end-to-end suite starts a real server for every script and attacks it over HTTP and
WebSocket. On every push, CI runs it on Ubuntu, and runs the installer as root on a real host
in both isolation modes. The security-relevant parts:

- [`t_security.py`](tests/t_security.py): every route the server has, swept with the cookie
  alone, as a viewer and as an operator: anything outside a role answers 401 or 403.
- [`t_fs.py`](tests/t_fs.py): symlinks in and out of an app, swapped paths, copies into
  themselves, uploads that try to escape.
- [`t_hardening.py`](tests/t_hardening.py): oversized bodies, password prompts, deleting while
  busy, backups of a changing tree, archives that write through a link.
- [`t_proxy.py`](tests/t_proxy.py), [`t_timing.py`](tests/t_timing.py),
  [`t_totp.py`](tests/t_totp.py): spoofed client addresses, username timing, code replay.
- [`t_installed.py`](tests/t_installed.py) (in CI, as root): apps really run as their own user and
  can't read the database, and a planted symlink to `/etc` is refused. Around it, the
  [CI workflow](.github/workflows/ci.yml) checks that a corrupted download and a release signed
  with another key are rejected by the installer and by the updater, and that a release that
  doesn't start is rolled back.

CodeQL and a dependency audit (`pip-audit`) also run on every push, and the repository is
graded by the [OpenSSF Scorecard](https://securityscorecards.dev/viewer/?uri=github.com/nikolaus018/venvy).

## Status

Venvy is a young project with one maintainer, and it has **not been independently audited**.
The protections above are designed, documented and tested, but that is not the same as someone
else trying hard to break them. If you do, I would like to hear about it, whatever you find.
Security fixes are listed plainly in the [changelog](CHANGELOG.md), with what could have
happened before.
