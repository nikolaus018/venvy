# Changelog

What changed in each release, newest first. Venvy shows the sections newer than
the version it runs under System → Venvy → What's new, and the release on GitHub
uses the same text. Every release needs its section here: the release workflow
stops without one.

## v4.8.1

- Publishing an app through the Cloudflare tunnel only publishes that app's own port. Before, any port on the server could be put on the internet from an app's Tunnel tab: another app's (one the operator might not even have access to), Venvy's own, or a database listening on localhost because it trusts the machine. A port that is also another app's, or that something other than the app is listening on, is refused too.
- An app can no longer take over Venvy's own address. Venvy now remembers the names it is signed in to under; such a name can't become an app's domain, isn't handed to an app even if one already has it, and can't be published for an app through the tunnel, nor can a tunnel hostname that points at Venvy. Before, an operator reaching Venvy by its LAN address could give an app Venvy's public name, and the next admin to sign in there would have typed their password into the app.
- A hostname in the tunnel that wasn't published from Venvy (another service on this server, or elsewhere) can only be taken over by an admin, and a hostname another app published only by someone with access to that app.
- An app served on its own domain no longer receives `X-Venvy-Proxy-Secret`: with it, the app could have passed for the tunnel and claimed any client address. Forwarding headers the client made up are dropped as well.
- Making a deploy key could hand a root-owned file to the app: the key's folder belongs to the app, which could swap the new key for a link at the right moment and have Venvy (running as root) change the owner of whatever it pointed at, `/etc/shadow` included. The key is now made where only root can reach it and moved into place whole. Showing the public key no longer follows a link the app put there.
- Deleting an app now also ends whatever it left running as its own user (a daemon, a `nohup` from the terminal). Those kept the user from being removed, and a new app that got the same id later would have had them running with its files.
- Copying a folder into itself in the file manager is refused. It used to copy itself into the copy until the disk was full.
- The file list no longer shows the size of a file outside the app that a link points to, nor whether a folder outside it exists.
- Viewers can't run the import scan on the Packages tab any more: it reads the app's source, which a viewer doesn't get to see.
- With the login limiter's table full (tens of thousands of made-up usernames), new failed logins were no longer counted at all. Now the stalest entry makes room.
- Download links that were asked for and never used are cleaned up, and there can be 200 at a time per user.
- An app that crashed shows as crashed (red dot, CRASHED in the list, the "stopped unexpectedly" notice with View logs) instead of just "Stopped": a crashed process is recorded as stopped, so the crash display never came up for the most common crash. The API marks such an app `crashed`. Starting an app that crashes at once now says the start failed instead of waiting half a minute for nothing.
- The terminal on a phone starts at the screen's width; it used to start at 220 columns and garble the first prompt when it was resized a moment later. The prompt shows the path inside the app (`~/static`) instead of the whole data directory.
- The charts in System no longer squash their time labels and end dot to fit the card.
- The CPU and memory bars in System have a light track in the light theme, not a dark one.
- With System open, the sidebar no longer also highlights the app that was open before.
- Uptime over a day reads in days ("27d 8h 26m"), not "656h 17m 10s".
- Logs on a phone: "All logs" has its own icon instead of a second download arrow, and the log and file toolbar buttons are big enough for a finger on touch screens.

## v4.8.0

- Releases are signed, and nothing unsigned is installed. A release is signed on the maintainer's machine with a key that is never on GitHub; the installer and the updater check the signature before they unpack anything (the updater against the key the server was installed with). Before, the only check was a checksum published beside the release, which proved the download wasn't corrupt, but someone who got into the GitHub account could have shipped any update with a matching one. Servers on 4.7.1 and earlier take this update as before, and every update after it is checked.
- A hosted app can no longer pass for another client. Apps reach Venvy over loopback like cloudflared does, so an app could send its own `CF-Connecting-IP` and get past the IP allowlist and the per-address login lockout. Venvy now looks at who owns the connection (Linux): one opened by a hosted app is taken to come from the app itself.
- A backup reads the apps' folders without following anything an app changes under it. An app that swapped one of its folders for a link at the right moment could have had root copy files from elsewhere into the backup, which a restore would then have handed to the app.
- Repeated login lockouts get longer: 5, 10, 20, 40 minutes, then an hour, remembered for a day. At a flat five minutes, someone patient could try about 5,700 passwords a day against one account; now about 480. A correct login clears it.
- The file manager, copies and backups never read a file that is a hard link to someone else's (more than one link, not owned by the folder's owner). Linux refuses to make such links by default; this no longer depends on that setting.

## v4.7.1

- The UI loads faster: its Tailwind CSS is now built ahead of time (24 KB) instead of being generated in the browser on every visit by a 400 KB script. It looks exactly the same.
- The System view on a phone: the disk usage no longer runs into the uptime.

## v4.7.0

- The bundled Tailwind CSS runtime now ships with its license notice (vendor/LICENSE-tailwindcss).
- A new install gives every hosted app its own Unix user. Before, that needed `--app-users`, and by default every app, terminal and package install ran as Venvy's own user: it could read Venvy's database, the key that decrypts every app's secrets, and every other app, so an operator could make themselves admin from a terminal. `--shared-user` keeps the old way. An existing install keeps the mode it has; `--app-users` switches it over.
- The terminal no longer claims to be "JAILED" and unable to reach the host. It now says ISOLATED when the shell runs as the app's own user, and SHARED USER (in amber) when it runs as Venvy's, with what that means.
- Logs on a phone: the filter has a row of its own instead of two characters' room beside the buttons, and the clear button is no longer cut off.
- Bigger tap targets for "Open Menu" and System → Refresh on a phone.
- When an app had crashed, the next app list sent it without its id, name or status, which broke the dashboard until the following refresh.
- File names in the backup panel are escaped, and a push webhook with a garbled signature header answers 401 instead of 500.

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
