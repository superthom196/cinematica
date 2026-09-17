#!/usr/bin/env bash
#
# fake-app.sh — stand in for the Android TV app.
#
#   ./fake-app.sh [http://mediabox.lan:8090]
#
# Heartbeats the player protocol, prints every command the server sends, acks
# it, and pretends to play: on `play` it reports state=playing for that job with
# an advancing position; on `stop` it goes back to idle. Ctrl-C to quit.
#
# Exists so the server side can be exercised end to end -- health, the launch
# handoff, /api/nowplaying, the stop path -- without an APK, a TV, or adb.
# bash + curl + python3 (for JSON only); nothing else.
set -u

BASE="${1:-http://localhost:8090}"
ID="${APP_ID:-fake-$$}"
NAME="${APP_NAME:-Fake TV app}"
VER="${APP_VERSION:-0.0.1-fake}"
DUR="${APP_DURATION:-7200}"
WAIT="${APP_WAIT:-8}"          # long-poll seconds asked of the server

state=idle; job=""; title=""; pos=0; ack=""
last=$SECONDS

stamp(){ date +%H:%M:%S; }
say(){ echo "[$(stamp)] $*"; }

trap 'echo; say "bye"; exit 0' INT TERM

say "heartbeating $BASE as id=$ID name=\"$NAME\" (wait=${WAIT}s)"

while true; do
  # advance the fake playhead by however long the last round trip took
  now=$SECONDS
  if [ "$state" = playing ]; then
    pos=$(( pos + now - last ))
    if [ "$pos" -ge "$DUR" ]; then pos=$DUR; state=ended; say "state -> ended"; fi
  fi
  last=$now

  body=$(ID="$ID" NAME="$NAME" VER="$VER" ST="$state" JOB="$job" TITLE="$title" \
         POS="$pos" DUR="$DUR" ACK="$ack" WAIT="$WAIT" python3 -c '
import json, os
e = os.environ
d = {"id": e["ID"], "name": e["NAME"], "version": e["VER"],
     "state": e["ST"], "wait": float(e["WAIT"])}
if e["JOB"]:   d["job"] = e["JOB"]
if e["TITLE"]: d["title"] = e["TITLE"]
if e["ST"] in ("buffering", "playing", "paused"):
    d["position_s"] = float(e["POS"])
    d["duration_s"] = float(e["DUR"])
if e["ACK"]:   d["ack"] = int(e["ACK"])
print(json.dumps(d))')

  t0=$SECONDS
  resp=$(curl -sS -m $(( WAIT + 12 )) -H 'Content-Type: application/json' \
              -d "$body" "$BASE/api/player/heartbeat" 2>/dev/null) || resp=""
  if [ -z "$resp" ]; then
    say "no answer from $BASE — retrying"
    ack=""; sleep 2; continue
  fi

  # One line of fields, US-separated. NOT tabs: tab is whitespace to `read`,
  # which collapses runs of it, so a command with an empty field shifted every
  # field after it along by one.
  line=$(RESP="$resp" python3 -c '
import json, os, sys
try:
    d = json.loads(os.environ["RESP"] or "{}")
except Exception:
    print("!\x1fbad JSON\x1f\x1f\x1f\x1f"); sys.exit()
c = d.get("cmd")
if not c:
    sys.exit()
def f(v): return str("" if v is None else v).replace("\x1f", " ").replace("\n", " ")
print("\x1f".join([f(c.get("seq")), f(c.get("type")), f(c.get("job")),
                    f(c.get("title")), f(c.get("url")),
                    "transcoded" if c.get("transcoded") else "direct"]))')

  if [ -n "$line" ]; then
    IFS=$'\x1f' read -r cseq ctype cjob ctitle curl_ cmode <<<"$line"
    say "cmd seq=$cseq type=$ctype job=${cjob:-–} title=\"${ctitle:-}\" ${cmode:-}"
    [ -n "${curl_:-}" ] && say "    url=$curl_"
    ack="$cseq"
    case "$ctype" in
      play) state=playing; job="$cjob"; title="$ctitle"; pos=0
            say "state -> playing (job $job)" ;;
      stop) state=idle; job=""; title=""; pos=0
            say "state -> idle" ;;
      *)    say "unknown command type: $ctype" ;;
    esac
  else
    ack=""                      # nothing pending; the previous ack has landed
  fi

  # The server holds the request open for `wait`, so this normally does not
  # fire. It is here so a refusing/erroring server cannot be hot-looped.
  [ $(( SECONDS - t0 )) -lt 1 ] && sleep 1
done
