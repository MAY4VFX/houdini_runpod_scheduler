"""The rpfarm package.

``VERSION`` is not decoration. It goes out as the ``User-Agent`` on every
RunPod and pod-worker request, and the HDAs check it at import time: each
asset declares the minimum package version whose API surface it needs, so a
Houdini that was already running when the checkout updated -- and is therefore
still holding the OLD package in ``sys.modules`` while loading the NEW asset --
says "restart Houdini" instead of a bare ImportError naming some symbol.

**Bump the minor when the HDAs gain a dependency on something new** (a new
name, a changed signature) and raise the matching
``_MIN_RPFARM_VERSION`` in the asset that needs it. Bumping this alone is
harmless: the check is a floor, not an equality.
"""

VERSION = "2.1.0"
