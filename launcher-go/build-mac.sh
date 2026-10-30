#!/bin/sh
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$source_dir"

bundle="$source_dir/dist/Launcher.app"
mkdir -p "$bundle/Contents/MacOS"
go build -trimpath -o "$bundle/Contents/MacOS/launcher" .
cp Launcher.Info.plist "$bundle/Contents/Info.plist"
codesign --force --sign - "$bundle"
for mode in backfill daily; do
    deployment=backfill
    config=backfill_launcher_config
    if [ "$mode" = daily ]; then
        deployment=dailyfill
        config=dailyfill_launcher_config
    fi
    mkdir -p "$source_dir/dist/$deployment/_release"
    # 只首次复制脱敏模板，不覆盖用户已经维护的正式配置。
    target="$source_dir/dist/$deployment/_release/$config.json"
    if [ ! -e "$target" ]; then
        cp "$source_dir/../$config.example.json" "$target"
    fi
done
printf 'Built: %s\n' "$bundle"
