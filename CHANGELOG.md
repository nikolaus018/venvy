# Changelog

What changed in each release, newest first. Venvy shows the sections newer than
the version it runs under System → Venvy → What's new, and the release on GitHub
uses the same text. Every release needs its section here: the release workflow
stops without one.

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
