"""Browser pass over the UI (Playwright + installed Chrome): every view and
tab is opened, the main flows are exercised, and any uncaught script error
fails the run. usage: t_ui.py <repo>"""
import sys, time
from playwright.sync_api import sync_playwright
from harness import admin, make_app, server

repo = sys.argv[1]
fails = []
def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name, extra if not cond else "")
    if not cond: fails.append(name)

with server(repo, 55010) as (base, data, srv):
    cl = admin(base)
    a = make_app(cl, name="Alpha", code="import time\nprint('ALPHA-LOG-LINE', flush=True)\nwhile True: time.sleep(0.5)\n")
    b = make_app(cl, name="Beta", code="print('beta')\n")
    cl.req("POST", f"/api/apps/{a}/control?action=start"); cl.wait_status(a, "running")
    with sync_playwright() as p:
        br = p.chromium.launch(channel="chrome", headless=True)
        pg = br.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        # "Failed to load resource" lines are covered by the response check below.
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
        failed = []
        pg.on("response", lambda r: failed.append(f"{r.status} {r.url}") if r.status >= 400 and base in r.url else None)
        pg.goto(base + "/"); pg.wait_for_timeout(1500)
        check("login screen shown", pg.is_visible("#auth-screen") and not pg.is_visible("#app-shell"))
        pg.fill("#username", "admin"); pg.fill("#password", "wrong-password-1"); pg.click("#login-submit-btn"); pg.wait_for_timeout(800)
        check("bad login shows a toast", pg.locator("#toast-container .toast").count() >= 1 and pg.is_visible("#auth-screen"))
        failed.clear()
        pg.fill("#password", "hunter2hunter2"); pg.click("#login-submit-btn"); pg.wait_for_timeout(2000)
        check("login", pg.is_visible("#app-shell") and pg.inner_text("#current-user") == "admin")
        check("app list rendered", pg.locator("#app-list .app-row").count() == 2)
        check("styles applied", pg.evaluate("getComputedStyle(document.body).backgroundColor") not in ("rgba(0, 0, 0, 0)", "rgb(255, 255, 255)")
              and pg.evaluate("getComputedStyle(document.querySelector('.modal-overlay')).position") == "fixed")
        check("theme attribute set before boot", pg.evaluate("document.documentElement.getAttribute('data-theme')") in ("dark", "light"))
        check("pwa manifest + icons injected", pg.locator("link[rel=manifest]").count() == 1 and pg.locator("link[rel=apple-touch-icon]").count() == 1)

        pg.click(f"#app-list .app-row[data-app-id='{a}']"); pg.wait_for_timeout(1500)
        check("dashboard", pg.inner_text("#dash-title") == "Alpha" and "RUNNING" in pg.inner_text("#dash-status").upper())
        check("terminal connected", pg.evaluate("!!window.term && termSocket && termSocket.readyState === 1"))
        pg.keyboard.type("echo ui-$((20+3))\n"); pg.wait_for_timeout(1200)
        check("terminal echoes", "ui-23" in pg.evaluate("(() => { const b = window.term.buffer.active; let s = ''; for (let i = 0; i < b.length; i++) s += b.getLine(i).translateToString(true) + '\\n'; return s; })()"))

        pg.click("#tabbtn-files"); pg.wait_for_timeout(800)
        check("file tree", pg.locator("#file-tree .file-row[data-name='main.py']").count() == 1)
        pg.click("#file-tree .file-row[data-name='main.py']"); pg.wait_for_timeout(1000)
        check("editor opens file", "ALPHA-LOG-LINE" in pg.evaluate("aceEditor.getValue()") and pg.inner_text("#current-filename") == "main.py")
        pg.evaluate("aceEditor.setValue(aceEditor.getValue() + '# edited in ui\\n', -1)")
        check("dirty indicator", pg.is_visible("#editor-dirty-indicator"))
        pg.click("#editor-save-btn"); pg.wait_for_timeout(1000)
        got = cl.req("GET", f"/api/apps/{a}/files/download", params={"path": "main.py"}).text
        check("save writes the file", got.endswith("# edited in ui\n") and not pg.is_visible("#editor-dirty-indicator"))
        pg.evaluate("promptFileOp('create_file')"); pg.fill("#file-name-input", "notes.txt"); pg.click("#file-name-confirm-btn"); pg.wait_for_timeout(900)
        check("create file", pg.locator("#file-tree .file-row[data-name='notes.txt']").count() == 1)
        pg.set_input_files("input[type=file][onchange*='uploadFile']", {"name": "up.txt", "mimeType": "text/plain", "buffer": b"uploaded"}) if pg.locator("input[type=file][onchange*='uploadFile']").count() else pg.evaluate("0")
        pg.wait_for_timeout(1500)
        check("upload via file input", cl.req("GET", f"/api/apps/{a}/files/download", params={"path": "up.txt"}).text == "uploaded")

        pg.click("#tabbtn-logs"); pg.wait_for_timeout(1800)
        check("logs shown", "ALPHA-LOG-LINE" in pg.inner_text("#log-viewer"))
        pg.fill("#log-search", "ALPHA"); pg.wait_for_timeout(400)
        check("log filter highlights", pg.locator("#log-viewer mark").count() >= 1)
        pg.fill("#log-search", "")

        pg.click("#tabbtn-packages"); pg.wait_for_timeout(4000)
        check("packages listed", pg.locator("#pkg-list tr").count() >= 1 and "pip" in pg.inner_text("#pkg-list").lower())

        pg.click("#tabbtn-settings"); pg.wait_for_timeout(1500)
        check("settings populated", pg.input_value("#setting-name") == "Alpha" and pg.input_value("#setting-entrypoint") == "main.py")
        pg.select_option("#setting-policy", "always"); pg.wait_for_timeout(300)
        check("settings dirty", pg.evaluate("isSettingsDirty()"))
        pg.fill("#setting-env", '{"UI_VAR": "1"}'); pg.click("#settings-save-btn"); pg.wait_for_timeout(1500)
        st = cl.status(a)
        check("settings saved", st["restart_policy"] == "always" and '"UI_VAR"' in st["env_json"] and not pg.evaluate("isSettingsDirty()"))

        pg.click(f"#app-list .app-row[data-app-id='{b}']"); pg.wait_for_timeout(1500)
        check("switch app", pg.inner_text("#dash-title") == "Beta" and pg.inner_text("#current-filename") == "No file open")
        pg.click("#btn-start"); pg.wait_for_timeout(3500)
        check("start button works", cl.status(b)["last_start_time"] is not None)

        pg.click("#system-nav-btn"); pg.wait_for_timeout(2500)
        check("system view", pg.is_visible("#system-view") and "%" in pg.inner_text("#sys-cpu-val") and pg.locator("#sys-used-ports span").count() >= 1)
        pg.evaluate("openBackupsModal()"); pg.wait_for_timeout(800)
        pg.evaluate("createBackup()"); pg.wait_for_timeout(6000)
        check("backup created from UI", len(cl.req("GET", "/api/system/backups").json()) == 1 and "full-" in pg.inner_text("#sys-backups-list"))
        check("backup outcome shown", "Backup finished" in pg.inner_text("#backup-op-panel") and not pg.is_disabled("#btn-create-backup"))
        pg.reload(); pg.wait_for_timeout(2500); pg.evaluate("openBackupsModal()"); pg.wait_for_timeout(1200)
        check("backup outcome still shown after a reload", "Backup finished" in pg.inner_text("#backup-op-panel"))
        pg.evaluate("dismissBackupOp()")
        check("outcome can be dismissed", not pg.is_visible("#backup-op-panel"))
        name = cl.req("GET", "/api/system/backups").json()[0]["filename"]
        blob = cl.req("GET", f"/api/system/backups/{name}/download").content
        cl.req("DELETE", f"/api/system/backups/{name}")
        pg.set_input_files("#backup-upload-input", {"name": name, "mimeType": "application/gzip", "buffer": blob}); pg.wait_for_timeout(3000)
        check("uploaded backup listed and offered for restore", name in pg.inner_text("#sys-backups-list")
              and pg.is_visible("#restore-backup-modal") and pg.inner_text("#restore-backup-filename") == name)
        pg.evaluate("closeModal('restore-backup-modal')")
        pg.evaluate("closeModal('backups-modal')")
        pg.evaluate("openUsersModal()"); pg.wait_for_timeout(800)
        check("users modal", "admin" in pg.inner_text("#users-list"))
        pg.evaluate("closeModal('users-modal')")

        pg.keyboard.press("Control+k"); pg.wait_for_timeout(400)
        check("command palette", pg.is_visible("#command-palette") and pg.locator("#palette-results .palette-item").count() > 5)
        pg.fill("#palette-input", "alph"); pg.keyboard.press("Enter"); pg.wait_for_timeout(1500)
        check("palette opens app", pg.inner_text("#dash-title") == "Alpha" and "#/app/" in pg.url)
        pg.keyboard.press("Escape"); pg.evaluate("document.activeElement && document.activeElement.blur()")
        pg.keyboard.press("?"); pg.wait_for_timeout(400)
        check("shortcuts modal", pg.is_visible("#shortcuts-modal")); pg.keyboard.press("Escape")
        pg.evaluate("toggleTheme()"); t1 = pg.evaluate("document.documentElement.getAttribute('data-theme')")
        pg.evaluate("toggleTheme()"); t2 = pg.evaluate("document.documentElement.getAttribute('data-theme')")
        check("theme toggles", t1 != t2)
        pg.reload(); pg.wait_for_timeout(2500)
        check("deep link survives reload", pg.inner_text("#dash-title") == "Alpha")
        check("service worker registered", pg.evaluate("navigator.serviceWorker.getRegistrations().then(r => r.length)") == 1)
        pg.evaluate("logout()"); pg.wait_for_timeout(2000)
        check("logout", pg.is_visible("#auth-screen"))
        failed = [f for f in failed if "/api/auth/login" not in f]
        check("no uncaught script errors", not errors, str(errors[:5]))
        check("no failed requests", not failed, str(failed[:5]))
        br.close()
print("FAILED" if fails else "ALL OK", fails); sys.exit(1 if fails else 0)
