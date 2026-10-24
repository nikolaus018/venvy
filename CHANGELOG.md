# Changelog

What changed in each release, newest first. Venvy shows the sections newer than
the version it runs under System → Venvy → What's new, and the release on GitHub
uses the same text. Every release needs its section here: the release workflow
stops without one.

## v4.6.1

- What's new showed "Could not reach the release server: ClientResponseError" instead of the release notes. GitHub now sends a 9 KB header with its release downloads, and Venvy's HTTP client refused any header over 8 KB. Updating itself was not affected.
- For the same reason, an app served on its own domain answered 502 when it sent a header over 8 KB. Venvy's HTTP clients now accept headers up to 64 KB.

## v4.6.0

Security hardening before going into production.

- First-run setup needs a one-time setup code. Until the first admin existed, anyone who could reach a fresh install could create that admin and own the host. The code is printed by the installer, logged at startup and kept in `.setup-code` in the data directory until setup is done.
- Cookies are marked Secure and Strict-Transport-Security is sent whenever Venvy is reached over HTTPS, including through the Cloudflare tunnel. Before, both needed `SESSION_COOKIE_SECURE=1`, which a tunnel-only install easily went without. A plain-HTTP LAN address keeps working.
- The Content-Security-Policy no longer allows inline script, so markup that got into a page can't run any. The UI's buttons and fields name their handlers in `data-on-*` attributes instead of `onclick`, and those can only call Venvy's own functions. WebSockets may only connect back to Venvy's own host, no longer to any server.
- `/docs`, `/redoc` and `/openapi.json` are switched off: they showed anyone the full list of API endpoints.
- A new test checks every API route: the session cookie without this session's key gets 401, and viewers and operators get 403 on everything outside their role.

## v4.5.6

- After an update, the Updating window offers "Reload Venvy" again. It used to stop following the update as soon as the new version answered, while the update was still finishing, and lost track of the version it started from.
- The Venvy card shows "An update is under way…" during an update instead of "Not checked yet".
- An update that was asked for but never started (the server's update unit not running) shows as failed, with what to check, instead of waiting for good; Update asks again.
- An update from the UI keeps installing from the fork or mirror Venvy was installed from.
- When the release notes cannot be fetched, What's new tries again next time instead of an hour later.
- v4.5 and v4.5.0 count as the same version.

## v4.5.5

- An update from the UI could stay "running" for good: the installer rewrote the update script while that script was running it, and the old copy went astray. The script is now replaced, never rewritten, and is read whole before it runs.
- A status left at "running" by an update that is no longer running no longer blocks the next one.

## v4.5.4

- A new release shows up at once: Venvy asks GitHub which release is the latest instead of following its download link, which kept pointing at the previous release for a few minutes after each new one.
- The update itself fetches that exact release by name, so it cannot install the previous one by mistake.

## v4.5.3

- What's new showed an older release's notes as new: the notes were cached for an hour, and Check only refreshed the version. They now come from the release on offer and are fetched again when a newer one appears.
- When you are up to date, What's new says so instead of presenting your own release's notes as new.

## v4.5.2

- The Venvy card in System says when it last looked for a new release ("Up to date · checked 3 min ago").

## v4.5.1

- What's new in a release now shows right here in Venvy, for every version newer than yours — no trip to GitHub.

## v4.5.0

- Update Venvy from the UI: System shows the running version and, when a newer release is out, an Update button. It follows the update through Venvy's restart; a version that does not come up is rolled back by itself.
- The update runs as root through a unit the installer sets up. Venvy can only ask for the latest official release, checked against its checksum.
- Releases carry a VERSION file, and the installer is included in the release archive.

## v4.4.2

- An app's CPU now includes short-lived child processes, such as a compiler during a Rust build. It used to show under 1% while the server was at full load.
- On phones, the app's tab bar only scrolls sideways; it could be dragged up and down before.

## v4.4.1

- Git deploys over SSH keep working after switching to one Unix user per app: the app's deploy key is handed to its user.
- The "Restart it" checkbox in the health check settings lines up with the fields next to it.
- Text fields show a single focus ring instead of a doubled one.

## v4.4.0

- Deploy from git: build step, health-checked rollback, releases and push webhooks.
- Monitoring: health checks, metric history, resource alerts and notification channels (webhook, Slack, Discord, ntfy, Telegram, email).
- Scheduled jobs per app, scheduled backups with retention and off-site copies (S3, WebDAV, scp).
- API tokens, per-app user access, an audit trail, shared variables, environment history, log search and download, app domains, disk usage, duplicating an app, and a Python version per app.
