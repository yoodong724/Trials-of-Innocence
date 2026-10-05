#!/bin/sh
patch_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Python 3를 찾을 수 없습니다. README.ko.txt를 확인하세요.' >&2
    exit 1
fi
exec python3 "$patch_directory/patch.py" restore --pause
