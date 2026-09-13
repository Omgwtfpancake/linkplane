#!/data/data/com.termux/files/usr/bin/bash

echo "=== PHONE STATUS ==="
echo

echo "Device:"
getprop ro.product.manufacturer
getprop ro.product.model
echo

echo "Android:"
getprop ro.build.version.release
echo

echo "Kernel:"
uname -r
echo

echo "Uptime:"
uptime
echo

echo "Memory:"
free -h
echo

echo "Storage:"
df -h "$HOME"
echo

echo "Battery:"
termux-battery-status
