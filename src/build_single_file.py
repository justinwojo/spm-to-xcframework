#!/usr/bin/env python3
"""Regenerate ../spm-to-xcframework from src/spm_to_xcframework.py.

Pure byte-for-byte copy + chmod +x. No transformation. The source
module is directly runnable (it owns its shebang and main guard),
so the generated artifact is literally the source under a different
filename.

This builder must NOT synthesize behavior that doesn't exist in the
source — otherwise the sync-check test (which is a plain file
comparison) becomes meaningless. If you find yourself wanting to edit
text here, edit the source module instead.
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "spm_to_xcframework.py"
DST = HERE.parent / "spm-to-xcframework"


def main() -> int:
    if not SRC.is_file():
        print(f"source missing: {SRC}", file=sys.stderr)
        return 1
    data = SRC.read_bytes()
    DST.write_bytes(data)
    mode = DST.stat().st_mode
    DST.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"Wrote {DST} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
