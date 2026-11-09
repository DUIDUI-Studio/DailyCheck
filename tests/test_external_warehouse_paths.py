"""Real owned-SQLite controls for the configured external warehouse data root.

The child process runs the actual Flask auth hook, MCP UnitOfWork, canonical
selector, CLI creation, migration runner and one-shot importer with code root,
CWD and configured DB directory all distinct. Every database and sentinel is
synthetic and owned by pytest.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from warehouse_paths import resolve_warehouse_path, warehouse_registration_path

ROOT = Path(__file__).resolve().parents[1]

CHILD = r'''
import json, os, io, contextlib, sqlite3, sys, types
from pathlib import Path

REPO, ROOT = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
EXTERNAL, LEGACY, CWD_ROOT = ROOT/"external-root", ROOT/"legacy-code-root", ROOT/"cwd-root"
for path in (EXTERNAL/"warehouses", LEGACY/"db"/"warehouses", CWD_ROOT/"db"/"warehouses"):
    path.mkdir(parents=True, exist_ok=True)
os.chdir(CWD_ROOT)
os.environ["DAILYCHECK_DB_DIR"] = str(EXTERNAL)
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(REPO))

import config
config.BASE_DIR = LEGACY
import db
db.MASTER_DB = EXTERNAL/"master.db"; db.WAREHOUSE_DB_DIR = EXTERNAL/"warehouses"
db.init_master_db()
ext_wh = EXTERNAL/"warehouses"/"wh_ext.db"; db.init_warehouse_db(ext_wh)
db.MASTER_DB = LEGACY/"db"/"master.db"; db.WAREHOUSE_DB_DIR = LEGACY/"db"/"warehouses"
db.init_master_db()
leg_wh = LEGACY/"db"/"warehouses"/"wh_ext.db"; db.init_warehouse_db(leg_wh)
cwd_wh = CWD_ROOT/"db"/"warehouses"/"wh_ext.db"; db.init_warehouse_db(cwd_wh)
for path, label in ((ext_wh, "external"), (leg_wh, "legacy"), (cwd_wh, "cwd")):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sentinel(v TEXT)")
        conn.execute("INSERT INTO sentinel VALUES (?)", (label,))
db.MASTER_DB = EXTERNAL/"master.db"; db.WAREHOUSE_DB_DIR = EXTERNAL/"warehouses"

def seed_master(path):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) "
                     "VALUES (1,'admin','x',1,'2026-01-01 00:00:00')")
        conn.execute("INSERT INTO warehouses (id, code, name, db_path, warehouse_type, created_at) "
                     "VALUES (1,'wh_ext','External','db/warehouses/wh_ext.db','normal','2026-01-01 00:00:00')")
        conn.execute("INSERT INTO warehouse_users (user_id, warehouse_id, role) VALUES (1,1,'admin')")
        conn.commit()
seed_master(EXTERNAL/"master.db"); seed_master(LEGACY/"db"/"master.db")
def sentinel(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT v FROM sentinel").fetchone()[0]
def item_count(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

out = {"master_is_external": Path(config.MASTER_DB) == EXTERNAL/"master.db"}
from mcp_server.data import unit_of_work
with unit_of_work.warehouse_connection("db/warehouses/wh_ext.db", read_only=True) as conn:
    out["mcp_uow_sentinel"] = conn.execute("SELECT v FROM sentinel").fetchone()[0]
from blueprints import canonical as canonical_bp
with canonical_bp._open_warehouse("db/warehouses/wh_ext.db") as conn:
    out["canonical_sentinel"] = conn.execute("SELECT v FROM sentinel").fetchone()[0]
from mcp_server.service import canonical as mcp_canonical
with mcp_canonical._open_warehouse("db/warehouses/wh_ext.db") as conn:
    out["mcp_canonical_sentinel"] = conn.execute("SELECT v FROM sentinel").fetchone()[0]
from flask import Flask, g, session as flask_session
from blueprints import auth as auth_bp
app = Flask(__name__); app.secret_key = "synthetic-control"; app.register_blueprint(auth_bp.bp)
with app.test_request_context("/items"):
    flask_session["user_id"] = 1; flask_session["warehouse_id"] = 1
    auth_bp.load_user_and_warehouse()
    out["auth_path"] = g.warehouse_db_path
    out["auth_sentinel"] = sentinel(g.warehouse_db_path)
from cli import create_warehouse_cmd
before = {"external": item_count(ext_wh), "legacy": item_count(leg_wh)}
with contextlib.redirect_stdout(io.StringIO()):
    create_warehouse_cmd.callback("wh_new", "New", "normal", 1)
with sqlite3.connect(EXTERNAL/"master.db") as conn:
    out["cli_registration"] = conn.execute(
        "SELECT db_path FROM warehouses WHERE code='wh_new'").fetchone()[0]
out["cli_file_external"] = (EXTERNAL/"warehouses"/"wh_new.db").is_file()
out["cli_file_legacy_absent"] = not (LEGACY/"db"/"warehouses"/"wh_new.db").exists()
from migrations import multi_warehouse_fulfillment as mwf
observed = {}
def fake_migration(master_conn, warehouses):
    for code, conn in warehouses.items():
        observed[code] = {
            "path": conn.execute("PRAGMA database_list").fetchone()[2],
            "sentinel": (conn.execute("SELECT v FROM sentinel").fetchone()[0]
                         if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sentinel'").fetchone()
                         else "no-sentinel")}
    return {"observed": observed}
mwf.migrate_multi_warehouse_fulfillment = fake_migration
with sqlite3.connect(EXTERNAL/"master.db") as master:
    mwf.registered_up(master)
out["migration"] = observed
class Cell:
    def __init__(self, value): self.value = value
class Sheet:
    max_row = 2; max_column = 5
    rows = [["品项编码", "品项名称", "品项类别", "订货单位", "单价"],
            ["SYN-1", "synthetic item", "包材", "件", 1.5]]
    def cell(self, row, column): return Cell(self.rows[row - 1][column - 1])
class Workbook:
    active = Sheet()
fake = types.ModuleType("openpyxl"); fake.load_workbook = lambda *_a, **_k: Workbook()
sys.modules["openpyxl"] = fake
import db.import_items as importer
with contextlib.redirect_stdout(io.StringIO()):
    importer.import_items(str(ROOT/"synthetic.xlsx"), "wh_ext")
out["import"] = {"before": before,
                 "after": {"external": item_count(ext_wh), "legacy": item_count(leg_wh)}}
out["decoys"] = {"legacy": sentinel(leg_wh), "cwd": sentinel(cwd_wh)}
print(json.dumps(out, sort_keys=True))
'''


def _run_child(tmp_path):
    script = tmp_path / "data_root_control.py"
    script.write_text(CHILD)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-B", "-I", str(script), str(ROOT), str(tmp_path / "fixture")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )


def test_resolver_modes_preserve_absolute_default_and_bot_semantics(tmp_path):
    external = tmp_path / "external-not-db"
    legacy = tmp_path / "legacy-root"
    assert resolve_warehouse_path("db/warehouses/x.db", legacy, db_dir=external) == \
        external / "warehouses" / "x.db"
    assert resolve_warehouse_path("/absolute/x.db", legacy, db_dir=external) == \
        Path("/absolute/x.db")
    assert resolve_warehouse_path("db/warehouses/x.db") == Path("db/warehouses/x.db")
    assert resolve_warehouse_path("other/x.db", legacy, db_dir=external) == legacy / "other/x.db"
    assert resolve_warehouse_path("db/warehouses/x.db", legacy) == legacy / "db/warehouses/x.db"
    assert resolve_warehouse_path("other/x.db") == Path("other/x.db")
    assert warehouse_registration_path(external / "warehouses/x.db", base_dir=legacy,
                                       db_dir=external) == "db/warehouses/x.db"
    assert warehouse_registration_path(legacy / "db/warehouses/x.db", base_dir=legacy,
                                       db_dir=external) == "db/warehouses/x.db"
    assert resolve_warehouse_path("db/warehouses/x.db", legacy,
                                  db_dir=legacy / "db") == legacy / "db/warehouses/x.db"
    missing = resolve_warehouse_path("db/warehouses/missing.db", legacy, db_dir=external)
    assert missing == external / "warehouses" / "missing.db"
    assert not missing.exists() and not (legacy / "db/warehouses/missing.db").exists()


def test_item_routing_selector_uses_configured_root_and_stays_readonly(tmp_path, monkeypatch):
    import config
    from blueprints.item_routing_pure import routing_warehouse_connections

    external = tmp_path / "external-not-db"
    legacy = tmp_path / "legacy-root"
    for root, label in ((external, "external"), (legacy / "db", "legacy")):
        (root / "warehouses").mkdir(parents=True)
        with sqlite3.connect(root / "warehouses" / "dc.db") as conn:
            conn.execute("CREATE TABLE sentinel(value TEXT)")
            conn.execute("INSERT INTO sentinel VALUES (?)", (label,))
    monkeypatch.setattr(config, "BASE_DIR", legacy)
    monkeypatch.setattr(config, "DB_DIR", external)
    master = sqlite3.connect(":memory:")
    master.execute("CREATE TABLE warehouses(code TEXT, db_path TEXT, tenant_id INTEGER, "
                   "warehouse_type TEXT)")
    master.execute("INSERT INTO warehouses VALUES ('dc','db/warehouses/dc.db',1,"
                   "'distribution_center')")
    master.execute("INSERT INTO warehouses VALUES ('store','db/warehouses/dc.db',1,'storefront')")
    master.execute("INSERT INTO warehouses VALUES ('other','db/warehouses/dc.db',2,"
                   "'distribution_center')")
    with routing_warehouse_connections(master) as conns:
        assert set(conns) == {"dc"}
        assert conns["dc"].execute("SELECT value FROM sentinel").fetchone()[0] == "external"
        with pytest.raises(sqlite3.OperationalError):
            conns["dc"].execute("INSERT INTO sentinel VALUES ('unexpected')")
    with sqlite3.connect(legacy / "db/warehouses/dc.db") as conn:
        assert conn.execute("SELECT value FROM sentinel").fetchone()[0] == "legacy"

    absolute = external / "warehouses" / "absolute.db"
    with sqlite3.connect(absolute) as conn:
        conn.execute("CREATE TABLE sentinel(value TEXT)")
        conn.execute("INSERT INTO sentinel VALUES ('absolute')")
    master.execute("UPDATE warehouses SET db_path=? WHERE code='dc'", (str(absolute),))
    with routing_warehouse_connections(master) as conns:
        assert conns["dc"].execute("SELECT value FROM sentinel").fetchone()[0] == "absolute"

    master.execute("UPDATE warehouses SET db_path='db/warehouses/missing.db' WHERE code='dc'")
    missing_legacy = legacy / "db/warehouses/missing.db"
    with pytest.raises(sqlite3.OperationalError):
        with routing_warehouse_connections(master):
            pass
    assert not missing_legacy.exists()
    master.close()


def test_guard_real_db_still_refuses_legacy_code_root(tmp_path, monkeypatch):
    import config
    from blueprints import canonical_pure as cp

    legacy = tmp_path / "legacy-root"
    (legacy / "db/warehouses").mkdir(parents=True)
    target = legacy / "db/warehouses/wh.db"
    target.write_bytes(b"")
    monkeypatch.setattr(cp, "BASE_DIR", legacy)
    monkeypatch.setattr(config, "DB_DIR", tmp_path / "external-not-db")
    with pytest.raises(cp.RealDatabaseForbidden):
        cp.guard_real_db(target)


def test_guard_real_db_refuses_configured_external_root(tmp_path, monkeypatch):
    import config
    from blueprints import canonical_pure as cp

    external = tmp_path / "external-not-db"
    (external / "warehouses").mkdir(parents=True)
    target = external / "warehouses" / "wh_ext.db"
    target.write_bytes(b"")
    monkeypatch.setattr(config, "DB_DIR", external)
    with pytest.raises(cp.RealDatabaseForbidden):
        cp.guard_real_db(target)
    alias = external / "warehouses" / "alias.db"
    alias.symlink_to(target)
    with pytest.raises(cp.RealDatabaseForbidden):
        cp.guard_real_db(alias)
    monkeypatch.setitem(cp.CANONICAL_POLICY, "allow_real_db_write", True)
    cp.guard_real_db(target)


def test_actual_app_mcp_cli_migration_select_external_data_root(tmp_path):
    result = _run_child(tmp_path)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["master_is_external"] is True
    assert observed["mcp_uow_sentinel"] == "external"
    assert observed["canonical_sentinel"] == "external"
    assert observed["mcp_canonical_sentinel"] == "external"
    assert observed["auth_path"].endswith("external-root/warehouses/wh_ext.db")
    assert observed["auth_sentinel"] == "external"
    assert observed["cli_registration"] == "db/warehouses/wh_new.db"
    assert observed["cli_file_external"] is True
    assert observed["cli_file_legacy_absent"] is True
    assert observed["migration"]["wh_ext"]["sentinel"] == "external"
    assert all(item["path"].startswith(str(tmp_path / "fixture" / "external-root"))
               for item in observed["migration"].values())
    assert observed["import"]["after"]["external"] == observed["import"]["before"]["external"] + 1
    assert observed["import"]["after"]["legacy"] == observed["import"]["before"]["legacy"]
    assert observed["decoys"] == {"legacy": "legacy", "cwd": "cwd"}
