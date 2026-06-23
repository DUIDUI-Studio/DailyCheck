# CSS Cache-Busting Design — 2026-06-23

Replace the hand-maintained `?v=10` query string with an mtime-based version,
add `Cache-Control: no-cache` on HTML responses, and bump the service worker
cache name once to invalidate existing SW caches.

## Problem

`style.css` is referenced with a hardcoded `?v=10` in two templates. When the
file is edited, the operator must remember to bump `?v=10` manually, or users
see stale styles from two caches:

1. Browser HTTP cache
2. PWA service worker cache (`static/sw.js`, `CACHE_NAME = "dailycheck-v4"`)

The original ask was "replace `?v=N` with no-cache headers." That substitution
does not solve the problem in this app — `Cache-Control` headers do not
invalidate the service worker's cache, which is the more aggressive of the
two stale-CSS sources.

## Goal

Reliable CSS invalidation across browser HTTP cache, service worker cache,
and HTML back/forward navigation, with no manual version bumps.

## Changes

### 1. `app.py` — `asset_url` helper + `after_request` hook

`app.py` uses a `create_app()` factory (`app.py:16-66`). Add the helper
registration and the after_request hook **inside** `create_app()`, alongside
the existing `register_jinja_filters(app)` call at `app.py:59-60`. Also add
`import os` at the top of the file (currently only imported inside the
`__main__` block).

```python
# inside create_app(), after `app = Flask(__name__)`:

@app.template_global("asset_url")
def asset_url(filename):
    """URL for a static asset with an mtime-based cache buster."""
    path = os.path.join(app.static_folder, filename)
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        mtime = 0
    return url_for("static", filename=filename, v=mtime)


@app.after_request
def add_no_cache_for_html(response):
    """Add Cache-Control: no-cache to HTML responses only.

    Static assets keep Flask's default caching. Invalidation for those
    comes from the mtime-busted URL, not from a no-cache header.
    """
    ct = response.headers.get("Content-Type", "")
    if ct.startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    return response
```

`@app.template_global` is the cleaner factory-pattern equivalent of
`app.jinja_env.globals[...] = ...` — no separate assignment needed.

### 2. `templates/base.html` line 13

Before:
```html
<link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}?v=10" />
```

After:
```html
<link rel="stylesheet" href="{{ asset_url('style.css') }}" />
```

### 3. `templates/login.html` line 7

Before:
```html
<link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}?v=10" />
```

After:
```html
<link rel="stylesheet" href="{{ asset_url('style.css') }}" />
```

### 4. `static/sw.js` line 1 — bump `CACHE_NAME`

Before:
```js
const CACHE_NAME = "dailycheck-v4";
```

After:
```js
const CACHE_NAME = "dailycheck-v5";
```

The existing `activate` handler (`static/sw.js:20-27`) deletes any cache
whose name does not match `CACHE_NAME`, so this one-time bump invalidates
old SW caches. Future deploys do not need to re-bump — the mtime mechanism
handles ongoing invalidation.

## Files Touched

| File | Change |
|---|---|
| `app.py` | Add `asset_url` helper + `after_request` hook (~15 lines) |
| `templates/base.html` | One-line replacement |
| `templates/login.html` | One-line replacement |
| `static/sw.js` | `v4` → `v5` |

## Out of Scope

- Hashed filenames (overkill for one CSS file).
- Other static assets (icons, manifest, offline.html).
- Service worker strategy changes.

## Testing

1. **Render check** — load `/` and `/login`. The HTML must contain
   `style.css?v=<number>` where `<number>` matches
   `stat -f %m static/style.css`.
2. **Auto-bust check** — edit `static/style.css`, save, reload. The `?v=`
   value in the rendered HTML must change.
3. **Header check** — `curl -I http://localhost:5001/` → response includes
   `Cache-Control: no-cache`. `curl -I http://localhost:5001/static/style.css`
   → no `no-cache` header (default caching preserved).
4. **SW check** — load the app, run `caches.keys()` in DevTools. Only
   `dailycheck-v5` should be present.

## Risk

Low. All changes are additive or one-line replacements. `asset_url` falls
back to `v=0` if the file is missing, preserving previous behavior in that
edge case. SW version bump is a one-time cost.
