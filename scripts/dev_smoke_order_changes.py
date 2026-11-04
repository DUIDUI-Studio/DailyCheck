#!/usr/bin/env python3
"""Disposable #145 lifecycle: real loopback HTTP, SQLite dumps and optional no-JS phones.

Requires test dependencies; --browser also needs Python Playwright and Chromium.
Never use a shared database or localhost:8080. The root must be new and empty.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_relative_to(Path("/tmp").resolve()) or not root.name.startswith(
        "dailycheck-order-change-"
    ):
        parser.error("root must be a dedicated /tmp/dailycheck-order-change-* directory")
    if root.exists() and any(root.iterdir()):
        parser.error("root must be empty; choose a fresh disposable directory")
    root.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        DAILYCHECK_DB_DIR=str(root),
        DAILYCHECK_ORDER_CHANGES="true",
        DAILYCHECK_DISABLE_SCHEDULER="1",
        WECOM_STORE_ORDER_WEBHOOK="",
        WECOM_FEEDBACK_WEBHOOK="",
    )
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import pytest
    from werkzeug.serving import make_server

    import config
    from blueprints import order_changes_pure as ocp
    from blueprints import store_ordering_pure as sop
    from tests.test_order_changes_http import Inputs, SessionHTTP
    from tests.test_order_changes_pure import change_env, commercial, new_order

    patches = pytest.MonkeyPatch()
    fixture = change_env.__wrapped__(root, patches)
    env = next(fixture)
    app, master = env["app"], env["master"]
    app.config["WTF_CSRF_FORCE_ENABLED"] = True
    assert config.MASTER_DB.resolve() == root / "master.db"
    assert config.WAREHOUSE_DB_DIR.resolve() == root / "warehouses"
    order = new_order(env, ((201, 10), (203, 4), (202, 5), (206, 2)))
    oid = order["id"]
    ids = {line["canonical_id"]: line["id"] for line in order["order_items"]}
    sop.ship_partial(
        master,
        sop.open_warehouse_db,
        order_id=oid,
        operator_dc_code="dc_alpha",
        op_no="smoke-initial-ship",
        line_qty={ids[201]: 4},
        shipped_by=2,
    )
    sop.receive_partial(
        master,
        sop.open_warehouse_db,
        order_id=oid,
        op_no="smoke-initial-receive",
        item_qty={ids[201]: 2},
        received_by=1,
    )
    phones = {str(width): new_order(env) for width in (320, 375, 390)} if args.browser else {}
    snapshots = root / "sqlite-steps"
    snapshots.mkdir()

    def dump(label):
        import sqlite3
        from contextlib import closing

        for name, path in {"master": env["master_path"], **env["paths"]}.items():
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
                (snapshots / f"{label}-{name}.sql").write_text("\n".join(conn.iterdump()))

    dump("00-seed")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    sessions = {
        name: SessionHTTP(server, app, user, warehouse, root / "http-raw")
        for name, user, warehouse in [
            ("dc", 4, 1),
            ("store", 1, 3),
            ("manager", 5, 3),
            ("beta", 3, 2),
        ]
    }
    metadata = {
        "base_url": base,
        "db_root": str(root),
        "master_db": str(config.MASTER_DB),
        "order_id": oid,
        "line_ids": ids,
        "phone_orders": {width: order["id"] for width, order in phones.items()},
        "cookies": {name: session.cookie for name, session in sessions.items()},
    }
    (root / "browser-seed.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    external_attempts = []
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            external_attempts.append(str(address))
            raise RuntimeError("isolated smoke refuses external network")
        return original_connect(sock, address)

    patches.setattr(socket.socket, "connect", local_connect)
    dc, store = sessions["dc"], sessions["store"]
    detail = f"/store-ordering/orders/{oid}"
    try:
        before = commercial(master, oid)
        status, html, _ = dc.request(detail + "/changes/new")
        assert status == 200
        fields = Inputs(html).values
        intent = {
            "request_key": fields["request_key"],
            "base_change_revision": fields["base_change_revision"],
            "reason": "隔离验收：取消、减量与替换未发部分",
            f"operation_{ids[201]}": "replace",
            f"replacement_{ids[201]}": "207",
            f"replacement_qty_{ids[201]}": "3",
            f"operation_{ids[203]}": "reduce",
            f"remove_{ids[203]}": "1",
            f"operation_{ids[202]}": "cancel",
        }
        assert (
            dc.request(detail + "/changes", {**intent, "action": "publish"}, csrf=False)[0] == 400
        )
        status, html, _ = dc.request(detail + "/changes", {**intent, "action": "preview"})
        assert status == 200 and commercial(master, oid) == before
        seen = Inputs(html).values["seen_digest"]
        status, _, headers = dc.request(
            detail + "/changes", {**intent, "action": "publish", "seen_digest": seen}
        )
        assert status == 302 and commercial(master, oid) == before
        proposal_url = headers["Location"]
        metadata["proposal_id"] = int(proposal_url.rsplit("/", 1)[1])
        dump("01-proposed")
        dc.request(detail)
        status, body, _ = dc.request(
            detail + "/ship",
            {"op_no": "smoke-blocked", f"shipped_items[{ids[201]}]": "1"},
            ajax=True,
        )
        assert status == 409 and json.loads(body)["ok"] is False
        assert (
            dc.request(
                detail + "/ship",
                {"op_no": "smoke-unrelated", f"shipped_items[{ids[206]}]": "1"},
                ajax=True,
            )[0]
            == 200
        )
        dump("02-unrelated-shipped")
        status, notifications, _ = store.request("/notifications")
        assert status == 200 and proposal_url in notifications
        status, html, _ = store.request(proposal_url)
        decision = {
            "request_key": "smoke-accept",
            "seen_digest": Inputs(html).values["seen_digest"],
            "agree": "1",
        }
        assert store.request(proposal_url + "/accept", decision)[0] == 302
        accepted = sop.get_order_detail(master, oid)
        a = next(line for line in accepted["order_items"] if line["id"] == ids[201])
        b = next(
            line for line in accepted["order_items"] if line["replaces_order_item_id"] == ids[201]
        )
        assert (
            a["quantity"],
            a["original_quantity"],
            a["shipped_quantity"],
            a["fulfilled_quantity"],
        ) == (4, 10, 4, 2)
        assert (b["quantity"], b["source_warehouse_code"]) == (3, "dc_beta")
        dump("03-accepted")
        manager, beta = sessions["manager"], sessions["beta"]
        manager.request(detail)
        assert (
            manager.request(
                detail + "/receive",
                {"order_item_id": ids[201], "quantity": 2, "op_no": "smoke-receive-old"},
            )[0]
            == 302
        )
        beta.request(detail)
        assert (
            beta.request(
                detail + "/ship",
                {f"shipped_items[{b['id']}]": 3, "op_no": "smoke-ship-new"},
                ajax=True,
            )[0]
            == 200
        )
        dump("04-old-received-new-shipped")
        assert store.request(proposal_url + "/accept", decision)[0] == 302
        dump("05-replay")
        (root / "browser-seed.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        if args.browser:
            _phones(root, base, app, phones, metadata, master)
            dump("06-phones")
        summary = {
            "result": "PASS",
            "base_url": base,
            "db_root": str(root),
            "create_http": 302,
            "csrf_missing": 400,
            "affected_ship": 409,
            "unrelated_ship": 200,
            "accept_http": 302,
            "a_effective_original_shipped_received": [
                a["quantity"],
                a["original_quantity"],
                a["shipped_quantity"],
                a["fulfilled_quantity"],
            ],
            "b_quantity_source": [b["quantity"], b["source_warehouse_code"]],
            "external_network_attempts": external_attempts,
            "phones": list(phones),
            "proposal_status": ocp.proposal_detail(master, oid, metadata["proposal_id"])["status"],
        }
        assert not external_attempts
        (root / "http-evidence.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if args.serve:
            thread.join()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.close()
        patches.undo()


def _phones(root, base, app, orders, metadata, master):
    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for width, height in [(320, 568), (375, 812), (390, 844)]:
                order = orders[str(width)]
                oid, item = order["id"], order["order_items"][0]["id"]
                context = browser.new_context(
                    viewport={"width": width, "height": height}, java_script_enabled=False
                )
                context.route(
                    "**/*",
                    lambda route: (
                        route.continue_()
                        if route.request.url.startswith(base + "/")
                        else route.abort()
                    ),
                )

                def identity(user, warehouse, context=context):
                    cookie = app.session_interface.get_signing_serializer(app).dumps(
                        {"user_id": user, "warehouse_id": warehouse}
                    )
                    context.add_cookies([{"name": "session", "value": cookie, "url": base}])

                identity(4, 1)
                page = context.new_page()

                def capture(label, page=page, width=width):
                    page.screenshot(path=str(root / f"phone-{width}-{label}.png"), full_page=True)
                    (root / f"phone-{width}-{label}.html").write_text(page.content())
                    sizes = page.locator("html").bounding_box(), page.locator("body").bounding_box()
                    # Browser protocol evaluation observes geometry even when page JS is disabled.
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                        width,
                        label,
                        sizes,
                    )
                    button = page.get_by_role("button", name="同意整份方案", exact=True)
                    if button.count():
                        box = button.bounding_box()
                        assert (
                            box
                            and box["width"] > 0
                            and box["x"] >= 0
                            and box["x"] + box["width"] <= width
                        )

                detail = base + f"/store-ordering/orders/{oid}"
                page.goto(detail)
                page.get_by_role("link", name="提议订单变更", exact=True).click()
                page.locator(f'[name="operation_{item}"]').select_option("replace")
                page.locator(f'[name="replacement_{item}"]').select_option("207")
                page.locator(f'[name="replacement_qty_{item}"]').fill("3")
                page.locator('[name="reason"]').fill("手机无JS整份变更验收")
                capture("form")
                page.get_by_role("button", name="预览整份方案", exact=True).click()
                capture("preview")
                page.get_by_role("button", name="发布整份方案，等待订货方确认", exact=True).click()
                proposal_url = page.url
                capture("proposed")
                identity(1, 3)
                page.goto(base + "/notifications")
                capture("notifications")
                page.locator(f'a[href="{proposal_url.removeprefix(base)}"]').first.click()
                capture("compare")
                rejection_colors = page.locator(".order-change button.danger").evaluate(
                    "button => ({foreground:getComputedStyle(button).color,background:getComputedStyle(button).backgroundColor})"
                )
                assert rejection_colors == {
                    "foreground": "rgb(185, 28, 28)",
                    "background": "rgb(255, 255, 255)",
                }
                # An offline click cannot enqueue an approval: the server remains pending.
                context.set_offline(True)
                try:
                    page.get_by_role("button", name="同意整份方案", exact=True).click(timeout=2500)
                except Exception:
                    pass
                assert (
                    master.execute(
                        "SELECT status FROM store_order_change_proposals WHERE order_id=?", (oid,)
                    ).fetchone()[0]
                    == "pending"
                )
                context.set_offline(False)
                page.goto(proposal_url)
                page.get_by_role("button", name="同意整份方案", exact=True).click()
                capture("accepted")
                page.reload()
                assert page.get_by_text("已同意", exact=False).count()
                page.go_back()
                page.goto(proposal_url)
                assert not page.get_by_role("button", name="同意整份方案", exact=True).count()
                replacement = master.execute(
                    "SELECT id FROM store_order_items WHERE order_id=? AND replaces_order_item_id=?",
                    (oid, item),
                ).fetchone()[0]
                identity(3, 2)
                page.goto(detail)
                page.locator(f'[name="shipped_items[{replacement}]"]').fill("3")
                page.get_by_role("button", name="确认发货", exact=True).click()
                capture("shipped")
                identity(5, 3)
                page.goto(detail)
                receive_form = page.locator("form").filter(
                    has=page.locator(f'input[name="order_item_id"][value="{replacement}"]')
                )
                receive_form.locator('[name="quantity"]').fill("3")
                receive_form.get_by_role("button", name="收货", exact=True).click()
                capture("received")
                final_line = master.execute(
                    "SELECT shipped_quantity,fulfilled_quantity FROM store_order_items WHERE id=?",
                    (replacement,),
                ).fetchone()
                assert tuple(final_line) == (3, 3)
                results.append(
                    {
                        "width": width,
                        "height": height,
                        "javascript": False,
                        "proposal_url": proposal_url,
                        "offline_pending_then_explicit_retry": True,
                        "status": "accepted",
                        "replacement_shipped_received": [3, 3],
                        "rejection_button_colors": rejection_colors,
                        "overflow": False,
                    }
                )
                context.close()
        finally:
            browser.close()
    (root / "phone-evidence.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
