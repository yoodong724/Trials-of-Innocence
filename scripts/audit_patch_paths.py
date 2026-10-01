#!/usr/bin/env python3
"""Check all install, backup and temporary paths for a Windows game folder."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from patch_layout import DEFAULT_GAME_ROOT, audit_windows_paths
from toi_common import ToiError, load_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--game-root", default=DEFAULT_GAME_ROOT)
    args = parser.parse_args()
    try:
        print(json.dumps(audit_windows_paths(load_json(args.manifest), args.game_root), ensure_ascii=False))
        return 0
    except (ToiError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
