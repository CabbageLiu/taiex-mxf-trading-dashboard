#!/bin/sh
# Feed-liveness backstop — LAST RESORT for a wedged backend process where the
# in-process feed-health watchdog (IngestRunner._check_feed_health) can no
# longer run at all. It is deliberately NOT a second reconnect mechanism.
#
# It restarts the backend container only when the feed looks dead AND the
# in-process watchdog is evidently NOT handling it, for TWO consecutive polls:
#   * /status unreachable                              → process hung, OR
#   * in_market_session==true AND ingest_lag>THRESHOLD
#       AND feed.reconnect_count==0                    → watchdog never fired
#         (dead/disabled task — a healthy watchdog would have reconnect_count>0
#          long before lag crosses the loose 300s threshold).
# When reconnect_count>0 the in-process watchdog is already cycling (and is
# bounded by its own per-session cap); restarting would only reset that cap and
# burn into the 1000/day SinoPac login budget, so we stay out.
#
# A hard per-day restart cap (default 5) is a final backstop against loops.
# Intended to run every ~5 min via launchd/cron.
#
# Env overrides: STATUS_URL, LAG_THRESHOLD(300), COMPOSE_DIR, STATE_FILE,
#   DOCKER(docker), MAX_RESTARTS_PER_DAY(5).
set -eu

STATUS_URL="${STATUS_URL:-http://127.0.0.1:8000/status}"
LAG_THRESHOLD="${LAG_THRESHOLD:-300}"
STATE_FILE="${STATE_FILE:-/tmp/taiex-feed-liveness.state}"
DOCKER="${DOCKER:-docker}"
MAX_RESTARTS_PER_DAY="${MAX_RESTARTS_PER_DAY:-5}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="${COMPOSE_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
today() { date -u +%Y-%m-%d; }

# State file: "strikes restart_day restart_count"
prev_strikes=0 restart_day="" restart_count=0
if [ -f "$STATE_FILE" ]; then
    read -r prev_strikes restart_day restart_count < "$STATE_FILE" 2>/dev/null || true
    : "${prev_strikes:=0}" "${restart_day:=}" "${restart_count:=0}"
fi
[ "$restart_day" = "$(today)" ] || { restart_day="$(today)"; restart_count=0; }

save_state() { echo "$1 $restart_day $restart_count" > "$STATE_FILE"; }

json="$(curl -s --max-time 10 "$STATUS_URL" || true)"

if [ -z "$json" ]; then
    unhealthy=1  # process hung / unreachable
    detail="status-unreachable"
else
    read -r in_session lag rc <<EOF
$(printf '%s' "$json" | python3 -c '
import json, sys
d = json.load(sys.stdin)
feed = d.get("feed") or {}
ins = feed.get("in_market_session")
lag = d.get("ingest_lag_seconds")
rc = feed.get("reconnect_count", 0)
print(("true" if ins else "false"), (lag if lag is not None else -1), (rc if rc is not None else 0))
')
EOF
    unhealthy=0
    detail="in_session=$in_session lag=$lag reconnect_count=$rc"
    if [ "$in_session" = "true" ] && [ "${rc:-0}" -eq 0 ]; then
        if awk "BEGIN{exit !($lag < 0 || $lag > $LAG_THRESHOLD)}"; then
            unhealthy=1  # stale + watchdog never fired ⇒ watchdog not working
        fi
    fi
fi

if [ "$unhealthy" -ne 1 ]; then
    echo "$(stamp) feed-liveness: healthy/handled ($detail)"
    save_state 0
    exit 0
fi

strikes=$((prev_strikes + 1))
if [ "$strikes" -lt 2 ]; then
    echo "$(stamp) feed-liveness: unhealthy ($detail) — 1st strike, arming"
    save_state "$strikes"
    exit 0
fi

if [ "$restart_count" -ge "$MAX_RESTARTS_PER_DAY" ]; then
    echo "$(stamp) feed-liveness: unhealthy ($detail) but daily restart cap ($MAX_RESTARTS_PER_DAY) reached — NOT restarting"
    save_state "$strikes"
    exit 0
fi

echo "$(stamp) feed-liveness: unhealthy ($detail) for 2 polls — restarting backend (#$((restart_count + 1)) today)"
if ( cd "$COMPOSE_DIR" && "$DOCKER" compose restart backend ); then
    restart_count=$((restart_count + 1))
else
    echo "$(stamp) feed-liveness: restart command failed"
fi
save_state 0
