#!/bin/bash
# Keep the phone reachable over Wi-Fi adb, on whatever network it is.
# The address lives in config.toml ([phone].serial, "ip:5555"). When the phone is not there:
#   1. reconnect to the same address (a short Wi-Fi drop);
#   2. find the phone on the local network by its hardware serial ([phone].hw_serial), checking every host
#      with port 5555 open (a new network or a new DHCP address), then write the new address into config.toml
#      and restart the engine so it uses it;
#   3. Wi-Fi band changes and reboots switch off both Wireless debugging and tcpip mode. MacroDroid on the phone
#      turns Wireless debugging back on; when its mDNS service shows up, connect to it and switch adbd back to
#      tcpip 5555.
# Every adb call is time-limited so a phone that drops mid-command can't hang this loop.
cd "$(dirname "$0")"
if command -v timeout >/dev/null; then t() { timeout 20 "$@"; }; else t() { perl -e 'alarm shift; exec @ARGV' 20 "$@"; }; fi  # Linux | macOS

cfg() { sed -n "s/^$1 *= *\"\([^\"]*\)\".*/\1/p" config.toml | head -1; }
HW="$(cfg hw_serial)"; HW="${HW:-ZD222RWWGT}"
is_phone() { [ "$(t adb -s "$1" shell getprop ro.serialno 2>/dev/null | tr -d '\r')" = "$HW" ]; }

use() {  # the phone answers at $1 (ip:5555): make it the configured address
  [ "$1" = "$TCP" ] && return
  sed -i.bak "s/^serial *= *\"[^\"]*\"/serial = \"$1\"/" config.toml && rm -f config.toml.bak
  echo "$(date +%H:%M:%S) keeper: phone moved $TCP -> $1; restarting the engine"
  TCP="$1"
  if command -v systemctl >/dev/null; then sudo -n systemctl restart content-engine 2>/dev/null; fi
}

scan() {  # every host on our IPv4 subnets with port 5555 open that is our phone
  for net in $(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | grep '/24$' | cut -d. -f1-3); do
    for i in $(seq 1 254); do
      (timeout 1 bash -c "echo > /dev/tcp/$net.$i/5555" 2>/dev/null && echo "$net.$i:5555") &
    done
    wait
  done | while read -r a; do
    t adb connect "$a" >/dev/null 2>&1
    if is_phone "$a"; then echo "$a"; break; fi
    t adb disconnect "$a" >/dev/null 2>&1
  done
}

misses=0
while true; do
  TCP="$(cfg serial)"
  if ! t adb -s "$TCP" get-state 2>/dev/null | grep -q device; then
    t adb disconnect "$TCP" >/dev/null 2>&1
    if t adb connect "$TCP" 2>/dev/null | grep -q "connected to" && is_phone "$TCP"; then
      misses=0
    else
      P=$(t adb mdns services | awk -v hw="adb-$HW" '$0 ~ hw && /tls-connect/ {print $NF; exit}')
      if [ -n "$P" ]; then
        t adb connect "$P" >/dev/null 2>&1; sleep 2
        t adb -s "$P" tcpip 5555 >/dev/null 2>&1; sleep 4
        NEW="${P%%:*}:5555"
        t adb connect "$NEW" >/dev/null 2>&1
        echo "$(date +%H:%M:%S) keeper: restored $NEW via $P"
        is_phone "$NEW" && use "$NEW"
      else
        misses=$((misses + 1))
        if [ $((misses % 4)) -eq 1 ]; then  # a subnet scan about once a minute while the phone is missing
          NEW="$(scan)"
          [ -n "$NEW" ] && { use "$NEW"; misses=0; }
        fi
      fi
    fi
  else
    misses=0
  fi
  sleep 15
done
