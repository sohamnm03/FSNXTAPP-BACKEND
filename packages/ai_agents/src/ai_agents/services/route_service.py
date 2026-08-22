"""Route discovery + interactive selection.

After login we show every page/route we can find in the terminal and let the
operator choose which ones to test. Testing 3 pages the operator actually
cares about beats crawling 15 shallowly - fewer pages means we can spend far
more checks per page, which is where the accuracy comes from.
"""
import os
import re
import sys
from urllib.parse import urljoin, urlparse

from ai_agents import config
from ai_agents.services.dom_service import get_page_elements

# Paths that are never worth testing as a page.
_SKIP_RE = re.compile(
    r"(logout|log-?out|sign-?out|\.(png|jpe?g|gif|svg|ico|css|js|json|map|woff2?|ttf|pdf|xlsx?|csv)$)",
    re.I,
)
# Route-ish strings inside JS bundles: React Router `path:"/x"`, and similar.
_BUNDLE_ROUTE_RE = re.compile(r"""path\s*:\s*["'](/[A-Za-z0-9\-_/]*)["']""")
_BUNDLE_PLAIN_RE = re.compile(r"""["'](/[a-z][a-z0-9\-]{2,}(?:/[a-z0-9\-]+)*)["']""")


def _origin(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _path_of(url):
    p = urlparse(url)
    return p.path or "/"


def friendly_name(path):
    """'/facility-creation' -> 'Facility Creation' - a label a non-technical
    reader recognises, used in the terminal menu and in the report."""
    seg = [s for s in path.split("/") if s]
    if not seg:
        return "Home"
    return re.sub(r"[-_]+", " ", seg[-1]).strip().title()


def _collect_from_dom(page, origin):
    found = {}
    for el in get_page_elements(page):
        h = el.get("href")
        if not h or h.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(page.url, h).split("#")[0].split("?")[0]
        if not absolute.startswith(origin):
            continue
        path = _path_of(absolute)
        if _SKIP_RE.search(path) or "/login" in path:
            continue
        found.setdefault(path, "menu")
    return found


def _collect_from_bundles(page, context, origin):
    """SPA nav is often buttons, not links - the router table in the JS bundle
    reveals routes the DOM never shows."""
    found = {}
    try:
        srcs = page.evaluate(
            "() => Array.from(document.querySelectorAll('script[src]')).map(s => s.src)")
    except Exception:
        return found
    for src in srcs[:8]:
        if not src.startswith(origin):
            continue
        try:
            body = context.request.get(src, timeout=15000).text()
        except Exception:
            continue
        hits = set(_BUNDLE_ROUTE_RE.findall(body))
        if not hits:
            # Fall back to plain quoted paths, which is noisier - only trust it
            # when the explicit `path:` form found nothing.
            hits = set(_BUNDLE_PLAIN_RE.findall(body))
        for path in hits:
            if _SKIP_RE.search(path) or "/login" in path:
                continue
            if ":" in path or "*" in path:      # dynamic segment, not directly visitable
                continue
            if path.startswith(("/api", "/static", "/assets", "/_")):
                continue
            if len(path) > 60:
                continue
            found.setdefault(path, "app routes")
    return found


def discover_routes(page, context, start_url):
    """Returns [{path, url, name, source}] sorted with the landing page first."""
    origin = _origin(start_url)
    found = {}
    found[_path_of(start_url)] = "current page"
    found.update({k: v for k, v in _collect_from_dom(page, origin).items() if k not in found})
    found.update({k: v for k, v in _collect_from_bundles(page, context, origin).items()
                  if k not in found})

    landing = _path_of(start_url)
    routes = [{"path": p, "url": origin + p, "name": friendly_name(p), "source": src}
              for p, src in found.items()]
    routes.sort(key=lambda r: (r["path"] != landing, r["path"]))
    return routes



def configured_routes(routes, start_url):
    """Add explicitly configured focus routes even when the current role menu hides them.

    Route discovery from the DOM is role-dependent. Capability tests need to visit
    both creation and approval routes even when a role is not supposed to see one
    of them, so configured routes are treated as authoritative test targets.
    """
    origin = _origin(start_url)
    merged = {route["path"].rstrip("/") or "/": dict(route) for route in routes}
    raw = config.TEST_ROUTES or (config.FOCUS_ROUTES if config.FOCUS_ONLY else "")
    for item in raw.split(","):
        path = (item.strip() or "").split("?")[0].split("#")[0]
        if not path:
            continue
        if not path.startswith("/"):
            path = "/" + path
        key = path.rstrip("/") or "/"
        merged.setdefault(
            key,
            {
                "path": path,
                "url": origin + path,
                "name": friendly_name(path),
                "source": "configured",
            },
        )
    landing = _path_of(start_url).rstrip("/") or "/"
    result = list(merged.values())
    result.sort(key=lambda route: ((route["path"].rstrip("/") or "/") != landing, route["path"]))
    return result

def print_routes(routes):
    print("\n" + "=" * 74)
    print(" PAGES FOUND ON THIS SITE")
    print("=" * 74)
    width = max((len(r["path"]) for r in routes), default=10)
    for i, r in enumerate(routes, 1):
        print(f"  {i:>3}  {r['path']:<{width}}   {r['name']:<24} [{r['source']}]")
    print("=" * 74)


def _parse_selection(raw, count):
    """Accepts '1,3', '2-5', '1-3,7', 'all', or a literal path like /dashboard."""
    raw = (raw or "").strip().lower()
    if raw in ("", "all", "a", "*"):
        return list(range(count))
    picked = []
    for part in raw.replace(" ", ",").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            picked.extend(i - 1 for i in range(lo, hi + 1) if 1 <= i <= count)
        elif part.isdigit():
            i = int(part)
            if 1 <= i <= count:
                picked.append(i - 1)
    return sorted(dict.fromkeys(picked))


def select_routes(routes):
    """Interactive picker. Honours TEST_ROUTES for non-interactive/CI runs and
    falls back to every page when there's no terminal to prompt on."""
    if not routes:
        return []
    print_routes(routes)

    preset = config.TEST_ROUTES.strip()
    if not preset and config.FOCUS_ONLY:
        preset = config.FOCUS_ROUTES
        print(f"\nFocused test mode is enabled: {preset}")

    if preset:
        wanted = {p.strip().lower().rstrip("/") or "/" for p in preset.split(",") if p.strip()}
        chosen = [r for r in routes if r["path"].lower().rstrip("/") in wanted
                  or r["path"].lower().rstrip("/") in {"/" + w.lstrip("/") for w in wanted}]
        if chosen:
            print(f"\nTEST_ROUTES is set - testing: {', '.join(r['path'] for r in chosen)}")
            return chosen
        print(f"\nTEST_ROUTES='{preset}' matched nothing; falling back to the prompt.")

    if not config.INTERACTIVE_ROUTE_SELECTION:
        print("\nNon-interactive route selection is enabled - testing every discovered page.")
        return routes if config.MAX_PAGES <= 0 else routes[:config.MAX_PAGES]

    if not sys.stdin.isatty():
        print("\nNo interactive terminal detected - testing every page found.")
        return routes if config.MAX_PAGES <= 0 else routes[:config.MAX_PAGES]

    print("\nWhich pages should I test?")
    print("  numbers: 1,3,5     range: 2-6     mix: 1-3,7")
    print("  ENTER or 'all' = every page       'q' = quit")
    while True:
        try:
            raw = input("\n  Your choice > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nNo selection made - testing every page found.")
            return routes if config.MAX_PAGES <= 0 else routes[:config.MAX_PAGES]
        if raw.lower() in ("q", "quit", "exit"):
            return []
        idxs = _parse_selection(raw, len(routes))
        if idxs:
            chosen = [routes[i] for i in idxs]
            print("\n  Selected:")
            for r in chosen:
                print(f"    - {r['path']}  ({r['name']})")
            return chosen
        print("  Didn't understand that. Try e.g. 1,3 or 2-5 or all.")

