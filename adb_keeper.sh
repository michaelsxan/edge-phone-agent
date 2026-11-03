#!/bin/sh
# Keep the phone reachable on 192.168.1.150:5555.
# Wi-Fi band changes and reboots switch off both Wireless debugging and tcpip mode. MacroDroid on the
# phone turns Wireless debugging back on; as soon as its mDNS service shows up, reconnect and switch
# adbd back to tcpip 5555. Every adb call is time-limited so a phone that drops mid-command can't hang this loop.
TCP="192.168.1.150:5555"
t() { perl -e 'alarm shift; exec @ARGV' 20 "$@"; }
while true; do
  if ! t adb -s "$TCP" get-state 2>/dev/null | grep -q device; then
    t adb disconnect "$TCP" >/dev/null 2>&1
    if ! t adb connect "$TCP" 2>/dev/null | grep -q "connected to"; then
      P=$(t adb mdns services | awk '/adb-ZD222RWWGT/ && /tls-connect/ {print $NF; exit}')
      if [ -n "$P" ]; then
        t adb connect "$P" >/dev/null 2>&1; sleep 2
        t adb -s "$P" tcpip 5555 >/dev/null 2>&1; sleep 4
        t adb connect "$TCP" >/dev/null 2>&1
        echo "$(date +%H:%M:%S) keeper: restored $TCP via $P"
      fi
    fi
  fi
  sleep 15
done
