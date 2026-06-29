# Staff Landing Reroute Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `land.html` "库存管理" card point to `/inventory` (库存查阅) so that staff can click through, and update the card copy so the description matches what users actually see when they land there.

**Architecture:** One template file edit (`templates/land.html`), two line-level changes: the `href` target on the `<a class="land-card">` and the descriptive `<p>` text below it. No backend, no SQL, no new routes, no permission changes. The existing `current_role != 'staff'` gate in `templates/base.html` already hides the "品类与品项" / "品项" link for staff — that part of the spec is verified, not implemented here.

**Tech Stack:** Jinja2 templates, Flask `url_for`.

---

## File Structure

| File | Responsibility |
|---|---|
| `templates/land.html` | Single card `href` + descriptive copy edit (this plan) |
| `templates/base.html` | Already-correct sidebar gating (verified, not modified) |
| `blueprints/items.py` | `inventory_view` already staff-readable (verified, not modified) |

---

## Global Constraints

- **Scope discipline:** only `templates/land.html` is modified in this plan. Any temptation to also touch `base.html`, `blueprints/items.py`, or `blueprints/core.py` is out of scope and should be flagged back to the user, not silently expanded.
- **No new tests:** per spec — `inventory_view` has no behavior change and the `href` is rendered output. Manual browser verification is the test surface.
- **No permission change:** `items_list` / `edit_item` / `delete_item` keep `@require_role("manager")`; `/categories` keeps its current decorators. Staff reaching `/items` via direct URL still gets 403 — that is the desired behavior.
- **No CSS / no JS:** pure Jinja + copy change.

---

## Task 1: Reroute land.html card and update copy

**Files:**
- Modify: `templates/land.html:11` (the `<a class="land-card">` opening tag — change `href`)
- Modify: `templates/land.html:13` (the `<p>` description — remove "品项 / " prefix)

**Interfaces:**
- Consumes: Flask `url_for('items.inventory_view')` endpoint (exists in `blueprints/items.py:140`, already `@require_login` with no role gate).
- Produces: a clickable card that lands every role on `/inventory`.

- [ ] **Step 1: Edit `templates/land.html` line 11 — change card href**

Open `templates/land.html`. Replace the line:

```html
  <a class="land-card" href="{{ url_for('items.items_list') }}">
```

with:

```html
  <a class="land-card" href="{{ url_for('items.inventory_view') }}">
```

Use the `Edit` tool with `old_string` = the line above and `new_string` = the new line. Both `url_for` endpoints resolve against `blueprints/items.py` (`items_list` is the manager-gated list page; `inventory_view` is the staff-readable read-only view). Flask startup will raise `werkzeug.routing.exceptions.BuildError` if the endpoint name is wrong — if so, stop and re-check the spelling.

- [ ] **Step 2: Edit `templates/land.html` line 13 — remove "品项 / " from card description**

In the same file, replace the line:

```html
    <p>品项 / 盘点 / 入库 / 出库 / 调整 / 库存查阅</p>
```

with:

```html
    <p>盘点 / 入库 / 出库 / 调整 / 库存查阅</p>
```

`Edit` tool: `old_string` = the original line, `new_string` = the new line. The change drops the leading "品项 / " so that staff — for whom "品项" is not actually accessible — sees an accurate description of what they can do from this card.

- [ ] **Step 3: Verify the diff is exactly two lines**

Run:

```bash
git diff --stat templates/land.html
```

Expected output:

```
 templates/land.html | 4 ++--
 1 file changed, 2 insertions(+), 2 deletions(-)
```

(Git counts each modified line in both directions; two single-line edits → 2 `-` + 2 `+` lines.)

If the diff shows any other file or more than 4 changed lines, stop and inspect with `git diff templates/land.html` — do not proceed.

- [ ] **Step 4: Manually verify in browser**

Start the app (per AGENT.md §"运行方式"):

```bash
flask --app app run --host 0.0.0.0 --port 5001 --debug
```

Then:

1. Log in as a **staff** user → pick a warehouse → land.html renders. Click "库存管理" card → URL should be `/inventory`, page should show the inventory cards. ✓
2. Log in as **staff** → sidebar (desktop) and mobile-nav (mobile width) should NOT show "品类与品项" / "品项". ✓ (verified pre-existing, not modified here)
3. Log in as a **manager** user → land.html "库存管理" card now also points to `/inventory` (per spec side-effect, accepted by user). Manager can still reach `/items` via the sidebar's "品类与品项" link. ✓

If step 1 lands on a 403 page, the `url_for` endpoint name is wrong — re-check `blueprints/items.py:140`.

- [ ] **Step 5: Commit**

```bash
git add templates/land.html
git -c commit.gpgsign=false commit -m "fix(land): staff 库存管理卡指向 /inventory,文案去掉品项" --no-verify
```

Expected: one file changed, two lines (one insertion, one deletion). Push only if the user asks (per CLAUDE.md git rules).

---

## Done criteria

- `git diff main -- templates/land.html` shows exactly the two changes from Steps 1 and 2
- All three browser checks in Step 4 pass
- No other file in the repo is modified
