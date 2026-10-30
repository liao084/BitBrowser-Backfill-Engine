#!/bin/sh
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
output="$source_dir/dist/windows"
cd "$source_dir"

# 只生成启动器；运行时沿用业务目录中维护的 JSON，不复制配置或制作 ZIP。
mkdir -p "$output"
CGO_ENABLED=1 GOOS=windows GOARCH=amd64 \
    CC="${CC:-x86_64-w64-mingw32-gcc}" \
    CXX="${CXX:-x86_64-w64-mingw32-g++}" \
    CGO_LDFLAGS="${CGO_LDFLAGS:-} -static -static-libgcc -static-libstdc++" \
    go build -trimpath -ldflags="-s -w -H=windowsgui -extldflags=-static" -o "$output/Launcher.exe" .

printf 'Built: %s\n' "$output/Launcher.exe"
