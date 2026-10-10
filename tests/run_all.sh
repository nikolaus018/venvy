#!/bin/sh
# End-to-end regression suite. Every script boots a real Venvy server on a
# throwaway data directory and drives it over HTTP/WebSocket.
#
#   pip install -r tests/requirements.txt     (plus Venvy's own dependencies)
#   tests/run_all.sh                          # ~6 minutes
#   PYTHON=/path/to/python tests/run_all.sh
#   SLOW=1 tests/run_all.sh                   # also the 2-minute login-lockout sweep test
#
# One line per script; details of a failure are in tests/.out/<name>.log.
cd "$(dirname "$0")" || exit 2
R="$(cd .. && pwd)"; PYB="${PYTHON:-python3}"; mkdir -p .out; fail=0
run() { name="$1"; shift; if "$@" > ".out/$name.log" 2>&1; then echo "PASS $name"; else echo "FAIL $name (see tests/.out/$name.log)"; fail=1; fi; }
SMOKE_EXPECT_SHUTDOWN_LOG=1 run smoke "$PYB" smoke.py "$R"
for c in double_start restart_storm stop_crashloop shutdown_logs; do run "lifecycle_$c" "$PYB" t_lifecycle.py "$R" "$c"; done
for c in stuck_status totp_nonascii settings_secret key_perms; do run "misc_$c" "$PYB" t_misc.py "$R" "$c"; done
NTERM=30 run terminal_many "$PYB" t_misc.py "$R" terminal_db
run terminal_session "$PYB" t_term2.py "$R"
run restore_running_app "$PYB" t_restore.py "$R"
run restore_integrity "$PYB" t_restore2.py "$R"
run totp "$PYB" t_totp.py "$R"
run login_timing "$PYB" t_timing.py "$R"
run proxy_trust "$PYB" t_proxy.py "$R"
run env_unreadable "$PYB" t_env.py "$R"
run files "$PYB" t_fs.py "$R"
run small "$PYB" t_small.py "$R"
# Browser pass; needs `pip install playwright` and an installed Google Chrome.
"$PYB" -c "import playwright" 2>/dev/null && run ui "$PYB" t_ui.py "$R"
[ -n "$SLOW" ] && run login_lockout "$PYB" t_misc.py "$R" login_lockout
exit $fail
