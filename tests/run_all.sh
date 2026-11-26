#!/bin/sh
# End-to-end regression suite. Every script boots a real Venvy server on a
# throwaway data directory and drives it over HTTP/WebSocket.
#
#   pip install -r tests/requirements.txt     (plus Venvy's own dependencies)
#   tests/run_all.sh                          # ~3 minutes
#   PYTHON=/path/to/python tests/run_all.sh
#   SLOW=1 tests/run_all.sh                   # also the 2-minute login-lockout sweep test
#
# One line per script; details of a failure are in tests/.out/<name>.log.
cd "$(dirname "$0")" || exit 2
R="$(cd .. && pwd)"; PYB="${PYTHON:-python3}"; mkdir -p .out
FAILED=.out/.failed; rm -f "$FAILED"
run() { name="$1"; shift; if "$@" > ".out/$name.log" 2>&1; then echo "PASS $name"; else echo "FAIL $name (see tests/.out/$name.log)"; : > "$FAILED"; fi; }
# Background jobs ignore Ctrl-C in sh, so pass it on to the whole group.
trap 'trap - INT TERM; kill 0' INT TERM

# Alone: the smoke test comes first, and login_timing measures response times.
SMOKE_EXPECT_SHUTDOWN_LOG=1 run smoke "$PYB" smoke.py "$R"
run login_timing "$PYB" t_timing.py "$R"

# Four lanes side by side, each running its scripts in order. Each script has a
# fixed server port: scripts that share one must stay in the same lane
# (lifecycle; misc and terminal_many; backup and runtimes; ship and security).
(
    run restore_running_app "$PYB" t_restore.py "$R"
    run restore_integrity "$PYB" t_restore2.py "$R"
    run monitor "$PYB" t_monitor.py "$R"
    run platform_unit "$PYB" t_platform_unit.py "$R"
    run platform "$PYB" t_platform.py "$R"
) &
(
    run small "$PYB" t_small.py "$R"
    run hardening "$PYB" t_hardening.py "$R"
    run review_fixes "$PYB" t_review_fixes.py "$R"
    # Browser pass; needs `pip install playwright` and an installed Google Chrome.
    if "$PYB" -c "import playwright" 2>/dev/null; then
        run ui "$PYB" t_ui.py "$R"
    else
        echo "SKIP ui (playwright not installed)"
    fi
) &
(
    for c in double_start restart_storm stop_crashloop shutdown_logs; do run "lifecycle_$c" "$PYB" t_lifecycle.py "$R" "$c"; done
    run proxy_trust "$PYB" t_proxy.py "$R"
    run env_unreadable "$PYB" t_env.py "$R"
    run deploy "$PYB" t_deploy.py "$R"
    run platform_deploys "$PYB" t_platform2.py "$R"
) &
(
    for c in stuck_status totp_nonascii settings_secret key_perms crashed_app_listing; do run "misc_$c" "$PYB" t_misc.py "$R" "$c"; done
    NTERM=30 run terminal_many "$PYB" t_misc.py "$R" terminal_db
    run terminal_session "$PYB" t_term2.py "$R"
    run backup_status_upload "$PYB" t_backup.py "$R"
    run totp "$PYB" t_totp.py "$R"
    run files "$PYB" t_fs.py "$R"
    run ship "$PYB" t_ship.py "$R"
    run security "$PYB" t_security.py "$R"
    run runtimes "$PYB" t_runtimes.py "$R"
    run ops "$PYB" t_ops.py "$R"
) &
wait

# After the lanes: it uses the misc scripts' port.
[ -n "$SLOW" ] && run login_lockout "$PYB" t_misc.py "$R" login_lockout
[ ! -e "$FAILED" ]
