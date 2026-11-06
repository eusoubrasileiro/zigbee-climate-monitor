#!/usr/bin/env bash
# Open the Zigbee join window and report pairing progress.
#
# Usage: scripts/pair.sh [max_seconds_to_wait] [seconds_to_watch_after_pairing]
#
# The window stays open while watching so a device that drops off can rejoin,
# and it always closes when the script ends.
set -u

LIMIT=${1:-900}
WATCH=${2:-180}

set_window() {
  docker exec mosquitto mosquitto_pub -t zigbee2mqtt/bridge/request/permit_join -m "{\"time\":$1}"
}
address_in() {
  sed -E 's/.*"ieee_address":"([^"]+)".*/\1/' <<<"$1"
}

trap 'set_window 0; echo "Join window closed."' EXIT

exec 3< <(docker exec mosquitto mosquitto_sub -W "$((LIMIT + WATCH + 60))" -t zigbee2mqtt/bridge/event)
echo "Join window open. Put the device in pairing mode, once, then leave it alone."

deadline=$((SECONDS + LIMIT))
watch_until=0
renew_at=0
while :; do
  # Zigbee2MQTT limits one opening to 254 seconds, so keep renewing it.
  if ((SECONDS >= renew_at)); then
    set_window 254
    renew_at=$((SECONDS + 240))
  fi
  if ((watch_until && SECONDS >= watch_until)); then
    echo "Stable: still on the network after ${WATCH}s."
    exit 0
  fi
  if ((!watch_until && SECONDS >= deadline)); then
    echo "Nothing paired within ${LIMIT}s." >&2
    exit 1
  fi
  IFS= read -r -t 5 line <&3 || { sleep 0.2; continue; }
  case "$line" in
    *'"device_joined"'*) echo "$(date +%T) joined: $(address_in "$line")" ;;
    *'"device_leave"'*)
      echo "$(date +%T) LEFT the network: $(address_in "$line"). Waiting for it to rejoin."
      watch_until=0
      deadline=$((SECONDS + LIMIT))
      ;;
    *'"device_interview"'*'"status":"failed"'*)
      echo "$(date +%T) interview FAILED: $(address_in "$line")"
      ;;
    *'"device_interview"'*'"status":"successful"'*)
      address=$(address_in "$line")
      echo "$(date +%T) paired: $address (MQTT topic: zigbee2mqtt/$address)"
      watch_until=$((SECONDS + WATCH))
      ;;
  esac
done
