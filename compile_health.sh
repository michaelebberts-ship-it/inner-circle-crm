#!/bin/bash
# One-time setup: compile and sign the Apple Health reader.
# Run this once, then restart serve_os.py.
# The first time fetch_health_data runs, macOS will ask for Health permission.

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "▶ Compiling fetch_health_data.swift..."
swiftc fetch_health_data.swift -o fetch_health_data

echo "▶ Signing with HealthKit entitlements..."
codesign --sign - --entitlements healthkit.entitlements --force fetch_health_data

echo "✅ Done! Testing binary..."
./fetch_health_data

echo ""
echo "If you see 'Health access denied', open System Settings → Privacy & Security → Health"
echo "and allow 'fetch_health_data' to read data."
