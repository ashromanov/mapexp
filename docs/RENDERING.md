# Frontend rendering

The rendering experiment is consolidated into the main MapExp checkout. The
separate `MapExp-fast-render` worktree was retired; normal development and CI run
from `MapExp` on `main`.

Cards clone a static template and retain DOM identity by event ID. Only changed
text is written. Map markers and uncertainty circles update in place; circles
share Leaflet's Canvas renderer. Search renders coalesce into animation frames.
Off-screen cards use content visibility with remembered intrinsic heights. Static
nodes, icon strings and formatting are cached. Mobile sheet resizing snaps to its
final geometry rather than repeatedly relaying out the feed/map during animation.

An initial local Chromium experiment with 1,000 synthetic incidents measured a
repeat-render median of 881.6 ms before these changes and 13.3 ms afterward. Across
16 repeated renders, marker/circle creations fell from 13,600 each to the original
800 each, with no additional feed subtree replacements. These are historical local
measurements, not a production latency or frame-rate guarantee.

```bash
node static/ui.test.mjs
# With playwright-core installed as a temporary development dependency:
MAPEXP_PLAYWRIGHT_MODULE=/path/to/playwright-core/index.mjs node static/render.test.mjs
```

The browser test covers filters, literal text escaping, unchanged-node reuse,
updates/deletions, details, selection, conditional revalidation, map retry and mobile
sheet states. [Delivery measurements](SCALING.md) and [CI/CD](DEPLOYMENT.md) describe
the backend path and automated checks.
