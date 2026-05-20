"""Allow `python -m spm_to_xcframework` to invoke the CLI entry point.

End users normally run the single-file `/spm-to-xcframework` script,
which embeds its own `if __name__ == "__main__": sys.exit(main())`.
This shim makes the package equivalent when invoked as a module — handy
for development against the split source tree.
"""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
