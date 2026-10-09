#!/bin/sh
# Generate the Xcode project, build the watch app, and (unless --no-install)
# install and launch it on the paired Apple Watch with devicectl.
#
#     scripts/build_watch.sh                 # build + install + launch
#     scripts/build_watch.sh --no-install    # build only
#     WATCH_UDID=... scripts/build_watch.sh  # pick the watch explicitly
#
# Needs xcodegen (brew install xcodegen) and Xcode's command line tools. The
# watch has to be paired with a phone that this Mac can reach; `xcrun devicectl
# list devices` shows it as "available (paired)".
set -e
cd "$(dirname "$0")/../SensorStreamerWatch"

xcodegen generate --quiet
LOG=DerivedData/xcodebuild.log
mkdir -p DerivedData
if ! xcodebuild -project SensorStreamer.xcodeproj -scheme SensorStreamer -configuration Debug \
    -destination 'generic/platform=watchOS' -derivedDataPath DerivedData \
    -allowProvisioningUpdates build > "$LOG" 2>&1; then
    grep -E "error:" "$LOG" | head -20
    echo "** BUILD FAILED ** (full log: SensorStreamerWatch/$LOG)"
    exit 1
fi
grep -E "BUILD|Signing Identity" "$LOG" | sort -u

APP=DerivedData/Build/Products/Debug-watchos/SensorStreamer.app
test -d "$APP" || { echo "build failed: no $APP"; exit 1; }
[ "$1" = "--no-install" ] && exit 0

if [ -z "$WATCH_UDID" ]; then
    WATCH_UDID=$(xcrun devicectl list devices 2>/dev/null | grep -i "Apple Watch" | awk '{for (i=1;i<=NF;i++) if ($i ~ /^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$/) print $i}' | head -1)
fi
test -n "$WATCH_UDID" || { echo "no Apple Watch in 'xcrun devicectl list devices'; set WATCH_UDID"; exit 1; }
echo "installing on $WATCH_UDID"
xcrun devicectl device install app --device "$WATCH_UDID" "$APP"
xcrun devicectl device process launch --device "$WATCH_UDID" com.figlab.sensorstreamer || \
    echo "installed; launch it from the watch (the launch needs the watch unlocked)"
