#!/data/data/com.termux/files/usr/bin/bash

BATTERY="$(termux-battery-status)"
MEM_AVAIL_KB="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
MEM_TOTAL_KB="$(awk '/MemTotal:/ {print $2}' /proc/meminfo)"
STORAGE_TOTAL_KB="$(df -Pk "$HOME" | awk 'NR==2 {print $2}')"
STORAGE_USED_KB="$(df -Pk "$HOME" | awk 'NR==2 {print $3}')"
STORAGE_FREE_KB="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
STORAGE_USED_PERCENT="$(df -Pk "$HOME" | awk 'NR==2 {gsub("%","",$5); print $5}')"

jq -n \
  --arg manufacturer "$(getprop ro.product.manufacturer)" \
  --arg model "$(getprop ro.product.model)" \
  --arg android "$(getprop ro.build.version.release)" \
  --arg kernel "$(uname -r)" \
  --argjson battery "$BATTERY" \
  --argjson mem_total_kb "$MEM_TOTAL_KB" \
  --argjson mem_available_kb "$MEM_AVAIL_KB" \
  --argjson storage_total_kb "$STORAGE_TOTAL_KB" \
  --argjson storage_used_kb "$STORAGE_USED_KB" \
  --argjson storage_free_kb "$STORAGE_FREE_KB" \
  --argjson storage_used_percent "$STORAGE_USED_PERCENT" \
  '{
    device: {
      manufacturer: $manufacturer,
      model: $model,
      android: $android,
      kernel: $kernel
    },
    battery: $battery,
    memory: {
      total_kb: $mem_total_kb,
      available_kb: $mem_available_kb
    },
    storage: {
      total_kb: $storage_total_kb,
      used_kb: $storage_used_kb,
      free_kb: $storage_free_kb,
      used_percent: $storage_used_percent
    }
  }'
