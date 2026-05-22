"""spm-to-xcframework — Build xcframeworks from Swift Package Manager packages.

This package implements the six-phase pipeline declared in REWRITE_DESIGN.md
§5:

    Fetch  →  Inspect  →  Plan  →  Prepare  →  Execute  →  Verify

The package is split into modules per phase (log, errors, config, model,
fetch, inspect, plan, prepare, execute/, output_manifest, verify, cli).
For end-user distribution `src/build_single_file.py` stitches them back
into a single executable script at the repo root.
"""
from __future__ import annotations

# Pull every public symbol from each phase module into the package
# namespace so callers can `from spm_to_xcframework import X` for any X
# regardless of which module actually defines it.
from .log import *  # noqa: F401,F403
from .errors import *  # noqa: F401,F403
from .diagnostics import *  # noqa: F401,F403
from .config import *  # noqa: F401,F403
from .model import *  # noqa: F401,F403
from .platforms import *  # noqa: F401,F403
from .fetch import *  # noqa: F401,F403
from .inspect import *  # noqa: F401,F403
from .plan import *  # noqa: F401,F403
from .prepare import *  # noqa: F401,F403
from .prune_child import *  # noqa: F401,F403
from .execute import *  # noqa: F401,F403
from .output_manifest import *  # noqa: F401,F403
from .verify import *  # noqa: F401,F403
from .cli import *  # noqa: F401,F403


# Underscore-prefixed helpers (e.g. `_check_binary_dynamic`, `_balanced_close`)
# are skipped by `import *`. Walk each submodule and rebind them at package
# scope so existing callers — notably `src/spm_to_xcframework_tests.py` —
# can still reach them via `from spm_to_xcframework import _name`.
def _reexport_underscore_names() -> None:
    # Resolve submodules via sys.modules rather than attribute access on
    # the package. The wildcard imports above have replaced some package
    # attributes (e.g. `prepare`, `verify`, `inspect`) with same-named
    # public callables defined inside those submodules, shadowing the
    # submodule itself — `getattr(package, "prepare")` then yields the
    # function, not the module. sys.modules always carries the submodule.
    #
    # Discovery is dynamic so newly added submodules pick up automatically
    # — there's no hand-curated list to drift against extraction work.
    # The wildcard imports above still hand-order the modules for
    # dependency-layering reasons, but the underscore walker doesn't
    # care about order: each submodule is visited exactly once.
    import pkgutil as _pkgutil
    import sys as _sys

    target_globals = globals()
    seen: set = set()
    for _info in _pkgutil.walk_packages(__path__, prefix=__name__ + "."):
        if _info.name in seen:
            continue
        seen.add(_info.name)
        _mod = _sys.modules.get(_info.name)
        if _mod is None:
            # Sub-package's __init__ hasn't run yet — force-import so its
            # public names are loaded (the wildcard imports above only
            # touch top-level submodules, not children of e.g. execute/).
            try:
                _mod = __import__(_info.name, fromlist=["*"])
            except ImportError:
                continue
        for _name in vars(_mod):
            if _name.startswith("_") and not _name.startswith("__"):
                target_globals[_name] = getattr(_mod, _name)


_reexport_underscore_names()
del _reexport_underscore_names
