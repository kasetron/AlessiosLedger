#!/usr/bin/env python3
"""
Ledger — personal finance tracker
Run: python ledger.py
Open: http://localhost:5000
"""
import sqlite3, os, json, webbrowser, threading
from datetime import date, datetime
from flask import Flask, g, jsonify, request

app = Flask(__name__)
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.db")

# ── DB ────────────────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db: db.close()

def init_db():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS projects (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT    NOT NULL UNIQUE,
        total_budget REAL,
        description  TEXT    NOT NULL DEFAULT '',
        archived     INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT    NOT NULL DEFAULT (date('now'))
    );
    CREATE TABLE IF NOT EXISTS transactions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        amount      REAL    NOT NULL,
        method      TEXT    NOT NULL DEFAULT 'Conto',
        category    TEXT    NOT NULL DEFAULT '',
        date        TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        is_balance  INTEGER NOT NULL DEFAULT 0,
        project_id  INTEGER REFERENCES projects(id)
    );
    CREATE TABLE IF NOT EXISTS categories (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT    NOT NULL UNIQUE,
        grp        TEXT    NOT NULL DEFAULT 'AGGIUNTIVE',
        threshold  REAL,
        sort_order INTEGER NOT NULL DEFAULT 99
    );
    CREATE TABLE IF NOT EXISTS carryovers (
        year      INTEGER NOT NULL,
        month     INTEGER NOT NULL,
        category  TEXT    NOT NULL,
        amount    REAL    NOT NULL,
        PRIMARY KEY (year, month, category)
    );
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """)

    # migrations for existing DBs
    cols = [r[1] for r in db.execute("PRAGMA table_info(transactions)").fetchall()]
    if 'project_id' not in cols:
        db.execute("ALTER TABLE transactions ADD COLUMN project_id INTEGER REFERENCES projects(id)")

    # seed categories if empty
    if not db.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
        cats = [
            ("guadagni","NECESSARIE",None,0),("casa","NECESSARIE",135,1),
            ("utenze","NECESSARIE",100,2),("cibo e spesa","NECESSARIE",180,3),
            ("salute","NECESSARIE",None,4),("benessere","NECESSARIE",None,5),
            ("istruzione","NECESSARIE",None,6),("telefonia","NECESSARIE",7,7),
            ("tasse e trattenute","NECESSARIE",None,8),
            ("stuccherie","AGGIUNTIVE",None,10),("frank","AGGIUNTIVE",None,11),
            ("trasporti","AGGIUNTIVE",None,12),("viaggi e vacanze","AGGIUNTIVE",None,13),
            ("musica e concerti","AGGIUNTIVE",None,14),("cinema e teatro","AGGIUNTIVE",None,15),
            ("libri e dischi","AGGIUNTIVE",None,16),("elettronica","AGGIUNTIVE",None,17),
            ("hobby","AGGIUNTIVE",None,18),("sport","AGGIUNTIVE",None,19),
            ("amicizie","AGGIUNTIVE",None,20),("feste","AGGIUNTIVE",None,21),
            ("abbigliamento","AGGIUNTIVE",None,22),("cura personale","AGGIUNTIVE",None,23),
        ]
        db.executemany("INSERT INTO categories(name,grp,threshold,sort_order) VALUES(?,?,?,?)", cats)
    db.commit()
    db.close()

# ── Helpers ───────────────────────────────────────────────────────────────────
def row_to_dict(row): return dict(row) if row else None
def rows_to_list(rows): return [dict(r) for r in rows]

# ── Auto rollover ─────────────────────────────────────────────────────────────
def auto_rollover_if_needed(db, year, month):
    key = f"rollover_done_{year}_{month:02d}"
    if db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone():
        return
    prev_m, prev_y = (month-1, year) if month > 1 else (12, year-1)
    rows = db.execute(
        "SELECT category, SUM(amount) as s FROM transactions "
        "WHERE strftime('%Y',date)=? AND strftime('%m',date)=? "
        "AND is_balance=0 AND category!='' AND method!='Risparmio' "
        "AND project_id IS NULL "
        "GROUP BY category",
        (str(prev_y), f"{prev_m:02d}")
    ).fetchall()
    net = {r["category"]: r["s"] for r in rows}
    cats = db.execute("SELECT name, threshold FROM categories WHERE threshold IS NOT NULL").fetchall()
    for c in cats:
        prev_co = db.execute(
            "SELECT amount FROM carryovers WHERE year=? AND month=? AND category=?",
            (prev_y, prev_m, c["name"])
        ).fetchone()
        eff = c["threshold"] + (prev_co["amount"] if prev_co else 0.0)
        unused = round(eff + net.get(c["name"], 0.0), 2)
        if abs(unused) > 0.01:
            db.execute(
                "INSERT OR REPLACE INTO carryovers(year,month,category,amount) VALUES(?,?,?,?)",
                (year, month, c["name"], unused)
            )
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, "1"))
    db.commit()

# ── API: Transactions ─────────────────────────────────────────────────────────
@app.route("/api/txs")
def get_txs():
    year  = request.args.get("year",  type=int)
    month = request.args.get("month", type=int)
    db = get_db()
    if year and month:
        auto_rollover_if_needed(db, year, month)
        rows = db.execute(
            "SELECT * FROM transactions WHERE strftime('%Y',date)=? AND strftime('%m',date)=? ORDER BY date DESC, id DESC",
            (str(year), f"{month:02d}")
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM transactions ORDER BY date DESC, id DESC").fetchall()
    return jsonify(rows_to_list(rows))

@app.route("/api/txs", methods=["POST"])
def add_tx():
    d = request.json
    db = get_db()
    cur = db.execute(
        "INSERT INTO transactions(amount,method,category,date,description,is_balance,project_id) VALUES(?,?,?,?,?,?,?)",
        (d["amount"], d["method"], d.get("category",""), d["date"],
         d.get("description",""), int(d.get("is_balance", False)), d.get("project_id"))
    )
    db.commit()
    return jsonify(row_to_dict(db.execute("SELECT * FROM transactions WHERE id=?", (cur.lastrowid,)).fetchone())), 201

@app.route("/api/txs/<int:tid>", methods=["PUT"])
def update_tx(tid):
    d = request.json
    db = get_db()
    db.execute(
        "UPDATE transactions SET amount=?,method=?,category=?,date=?,description=?,is_balance=?,project_id=? WHERE id=?",
        (d["amount"], d["method"], d.get("category",""), d["date"],
         d.get("description",""), int(d.get("is_balance", False)), d.get("project_id"), tid)
    )
    db.commit()
    return jsonify(row_to_dict(db.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone()))

@app.route("/api/txs/<int:tid>", methods=["DELETE"])
def delete_tx(tid):
    db = get_db()
    db.execute("DELETE FROM transactions WHERE id=?", (tid,))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/txs/bulk-delete", methods=["POST"])
def bulk_delete():
    ids = request.json.get("ids", [])
    db = get_db()
    db.executemany("DELETE FROM transactions WHERE id=?", [(i,) for i in ids])
    db.commit()
    return jsonify({"deleted": len(ids)})

# ── API: Balances ─────────────────────────────────────────────────────────────
@app.route("/api/balances")
def get_balances():
    db = get_db()
    rows = db.execute("SELECT method, SUM(amount) as total FROM transactions GROUP BY method").fetchall()
    return jsonify({r["method"]: round(r["total"], 2) for r in rows})

# ── API: Categories ───────────────────────────────────────────────────────────
@app.route("/api/cats")
def get_cats():
    rows = get_db().execute("SELECT * FROM categories ORDER BY sort_order, id").fetchall()
    return jsonify(rows_to_list(rows))

@app.route("/api/cats", methods=["POST"])
def add_cat():
    d = request.json
    db = get_db()
    max_ord = db.execute("SELECT MAX(sort_order) as m FROM categories").fetchone()["m"] or 0
    cur = db.execute(
        "INSERT INTO categories(name,grp,threshold,sort_order) VALUES(?,?,?,?)",
        (d["name"], d.get("grp","AGGIUNTIVE"), d.get("threshold"), max_ord+1)
    )
    db.commit()
    return jsonify(row_to_dict(db.execute("SELECT * FROM categories WHERE id=?", (cur.lastrowid,)).fetchone())), 201

@app.route("/api/cats/<int:cid>", methods=["PUT"])
def update_cat(cid):
    d = request.json
    db = get_db()
    db.execute(
        "UPDATE categories SET name=?,grp=?,threshold=?,sort_order=? WHERE id=?",
        (d["name"], d["grp"], d.get("threshold"), d.get("sort_order", 99), cid)
    )
    db.commit()
    return jsonify(row_to_dict(db.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()))

@app.route("/api/cats/<int:cid>", methods=["DELETE"])
def delete_cat(cid):
    get_db().execute("DELETE FROM categories WHERE id=?", (cid,))
    get_db().commit()
    return jsonify({"ok": True})

# ── API: Carryovers ───────────────────────────────────────────────────────────
@app.route("/api/carryovers")
def get_carryovers():
    year  = request.args.get("year",  type=int)
    month = request.args.get("month", type=int)
    db = get_db()
    if year and month:
        rows = db.execute("SELECT * FROM carryovers WHERE year=? AND month=?", (year, month)).fetchall()
    else:
        rows = db.execute("SELECT * FROM carryovers").fetchall()
    return jsonify(rows_to_list(rows))

@app.route("/api/carryovers/force", methods=["POST"])
def force_rollover():
    d = request.json
    year, month = d["year"], d["month"]
    prev_m, prev_y = (month-1, year) if month > 1 else (12, year-1)
    db = get_db()
    rows = db.execute(
        "SELECT category, SUM(amount) as s FROM transactions "
        "WHERE strftime('%Y',date)=? AND strftime('%m',date)=? "
        "AND is_balance=0 AND category!='' AND method!='Risparmio' "
        "AND project_id IS NULL "
        "GROUP BY category",
        (str(prev_y), f"{prev_m:02d}")
    ).fetchall()
    net = {r["category"]: r["s"] for r in rows}
    cats = db.execute("SELECT name, threshold FROM categories WHERE threshold IS NOT NULL").fetchall()
    for c in cats:
        prev_co = db.execute(
            "SELECT amount FROM carryovers WHERE year=? AND month=? AND category=?",
            (prev_y, prev_m, c["name"])
        ).fetchone()
        eff = c["threshold"] + (prev_co["amount"] if prev_co else 0.0)
        unused = round(eff + net.get(c["name"], 0.0), 2)
        db.execute(
            "INSERT OR REPLACE INTO carryovers(year,month,category,amount) VALUES(?,?,?,?)",
            (year, month, c["name"], unused)
        )
    key = f"rollover_done_{year}_{month:02d}"
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, "1"))
    db.commit()
    return jsonify(rows_to_list(db.execute("SELECT * FROM carryovers WHERE year=? AND month=?", (year, month)).fetchall()))

# ── API: Projects ─────────────────────────────────────────────────────────────
@app.route("/api/projects")
def get_projects():
    include_archived = request.args.get("archived", "0") == "1"
    db = get_db()
    if include_archived:
        rows = db.execute("SELECT * FROM projects ORDER BY archived, created_at DESC").fetchall()
    else:
        rows = db.execute("SELECT * FROM projects WHERE archived=0 ORDER BY created_at DESC").fetchall()
    return jsonify(rows_to_list(rows))

@app.route("/api/projects", methods=["POST"])
def add_project():
    d = request.json
    db = get_db()
    cur = db.execute(
        "INSERT INTO projects(name, total_budget, description) VALUES(?,?,?)",
        (d["name"], d.get("total_budget"), d.get("description",""))
    )
    db.commit()
    return jsonify(row_to_dict(db.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone())), 201

@app.route("/api/projects/<int:pid>", methods=["PUT"])
def update_project(pid):
    d = request.json
    db = get_db()
    db.execute(
        "UPDATE projects SET name=?, total_budget=?, description=?, archived=? WHERE id=?",
        (d["name"], d.get("total_budget"), d.get("description",""), int(d.get("archived", 0)), pid)
    )
    db.commit()
    return jsonify(row_to_dict(db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()))

@app.route("/api/projects/<int:pid>", methods=["DELETE"])
def delete_project(pid):
    db = get_db()
    # unlink transactions before deleting
    db.execute("UPDATE transactions SET project_id=NULL WHERE project_id=?", (pid,))
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/projects/<int:pid>/spending")
def project_spending(pid):
    db = get_db()
    row = db.execute(
        "SELECT COALESCE(SUM(amount),0) as total FROM transactions WHERE project_id=? AND amount<0",
        (pid,)
    ).fetchone()
    txs = db.execute(
        "SELECT * FROM transactions WHERE project_id=? ORDER BY date DESC",
        (pid,)
    ).fetchall()
    return jsonify({"spent": abs(row["total"]), "transactions": rows_to_list(txs)})

# ── Backup / Restore ──────────────────────────────────────────────────────────
@app.route("/api/backup")
def backup():
    db = get_db()
    return jsonify({
        "txs":        rows_to_list(db.execute("SELECT * FROM transactions ORDER BY id").fetchall()),
        "cats":       rows_to_list(db.execute("SELECT * FROM categories ORDER BY sort_order").fetchall()),
        "carryovers": rows_to_list(db.execute("SELECT * FROM carryovers").fetchall()),
        "projects":   rows_to_list(db.execute("SELECT * FROM projects ORDER BY id").fetchall()),
    })

@app.route("/api/restore", methods=["POST"])
def restore():
    data = request.json
    db = get_db()
    db.execute("DELETE FROM transactions")
    db.execute("DELETE FROM categories")
    db.execute("DELETE FROM carryovers")
    db.execute("DELETE FROM projects")
    db.execute("DELETE FROM meta")
    for p in data.get("projects", []):
        db.execute("INSERT INTO projects(id,name,total_budget,description,archived,created_at) VALUES(?,?,?,?,?,?)",
            (p["id"],p["name"],p.get("total_budget"),p.get("description",""),p.get("archived",0),p.get("created_at",str(date.today()))))
    for c in data.get("cats", []):
        db.execute("INSERT INTO categories(id,name,grp,threshold,sort_order) VALUES(?,?,?,?,?)",
            (c["id"],c["name"],c["grp"],c.get("threshold"),c.get("sort_order",99)))
    for t in data.get("txs", []):
        db.execute("INSERT INTO transactions(id,amount,method,category,date,description,is_balance,project_id) VALUES(?,?,?,?,?,?,?,?)",
            (t["id"],t["amount"],t["method"],t.get("category",""),t["date"],t.get("description",""),int(t.get("is_balance",0)),t.get("project_id")))
    for co in data.get("carryovers", []):
        db.execute("INSERT INTO carryovers(year,month,category,amount) VALUES(?,?,?,?)",
            (co["year"],co["month"],co["category"],co["amount"]))
    db.commit()
    return jsonify({"ok": True})

# ── Frontend ──────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Alessio's Ledger</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0e0c09;--surface:#181410;--surface2:#211d17;--surface3:#2c271f;
  --border:#332e24;--border2:#42392c;--text:#f0ebe0;--muted:#7a7265;--muted2:#a8a090;
  --gold:#c9a84c;--gold-dim:#c9a84c22;--green:#6bbd82;--green-dim:#6bbd8222;
  --red:#e06060;--red-dim:#e0606022;--amber:#d4933a;--amber-dim:#d4933a22;
  --blue:#6ab0d4;--purple:#a888e0;--sav:#e0a060;--r:10px;
}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;font-size:14px;line-height:1.5;min-height:100vh}
::-webkit-scrollbar{width:6px;height:6px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
input[type=number]{-moz-appearance:textfield}
input[type=number]::-webkit-outer-spin-button,input[type=number]::-webkit-inner-spin-button{-webkit-appearance:none;opacity:0;transition:opacity .15s}
input[type=number]:hover::-webkit-outer-spin-button,input[type=number]:hover::-webkit-inner-spin-button{opacity:.35}

.header{background:var(--surface);border-bottom:1px solid var(--border);padding:0 24px;height:58px;position:sticky;top:0;z-index:50;display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:12px}
.logo{display:flex;align-items:baseline;gap:8px}
.logo-text{font-family:'Instrument Serif',serif;font-size:22px;letter-spacing:-.3px}
.logo-year{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);letter-spacing:1px}
.header-center{display:flex;align-items:center;gap:10px;justify-content:center}
.month-nav{display:flex;align-items:center;gap:6px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:3px 5px}
.month-nav button{background:none;border:none;color:var(--muted2);width:26px;height:26px;border-radius:6px;cursor:pointer;font-size:14px;display:flex;align-items:center;justify-content:center;transition:all .15s}
.month-nav button:hover{background:var(--surface3);color:var(--text)}
.month-label{font-family:'DM Mono',monospace;font-size:12px;letter-spacing:.5px;min-width:106px;text-align:center;color:var(--text);font-weight:500;text-transform:uppercase}
.today-chip{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted2);background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:4px 9px;white-space:nowrap}
.header-actions{display:flex;gap:6px;justify-content:flex-end}
.btn-ghost{background:none;border:1px solid var(--border);color:var(--muted);padding:5px 11px;border-radius:7px;cursor:pointer;font-size:12px;font-family:'DM Sans',sans-serif;transition:all .15s}
.btn-ghost:hover{border-color:var(--gold);color:var(--text)}

.main{max-width:990px;margin:0 auto;padding:26px 20px 60px}

.balance-strip{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:22px}
@media(max-width:800px){.balance-strip{grid-template-columns:repeat(3,1fr)}}
@media(max-width:500px){.balance-strip{grid-template-columns:repeat(2,1fr)}}
.balance-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:13px 15px;position:relative;overflow:hidden;transition:border-color .2s}
.balance-card:hover{border-color:var(--border2)}
.balance-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;opacity:.65}
.bc-Conto::before{background:var(--blue)}.bc-Paypal::before{background:var(--gold)}.bc-Contanti::before{background:var(--green)}.bc-Libretto::before{background:var(--purple)}.bc-Risparmio::before{background:var(--sav)}.bc-total::before{background:linear-gradient(90deg,var(--blue),var(--gold),var(--green))}
.b-label{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:7px;font-family:'DM Mono',monospace}
.b-value{font-family:'DM Mono',monospace;font-size:18px;font-weight:500;letter-spacing:-.5px}
.bc-total .b-value{font-size:20px}.b-neg{color:var(--red)}

.tabs{display:flex;gap:2px;margin-bottom:20px;border-bottom:1px solid var(--border)}
.tab-btn{background:none;border:none;border-bottom:2px solid transparent;color:var(--muted);padding:10px 16px;font-size:13px;font-family:'DM Sans',sans-serif;font-weight:500;cursor:pointer;margin-bottom:-1px;transition:all .15s}
.tab-btn:hover{color:var(--muted2)}.tab-btn.active{color:var(--text);border-bottom-color:var(--gold)}
.tab-pane{display:none}.tab-pane.active{display:block}

.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:20px;margin-bottom:14px}
.card-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.card-title{font-size:10px;text-transform:uppercase;letter-spacing:1.2px;color:var(--muted);font-family:'DM Mono',monospace}

.net-row{display:grid;grid-template-columns:repeat(4,1fr)}
.net-item{text-align:center;padding:8px 0}
.net-item+.net-item{border-left:1px solid var(--border)}
.ni-label{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);font-family:'DM Mono',monospace;margin-bottom:5px}
.ni-value{font-family:'DM Mono',monospace;font-size:20px;font-weight:500;letter-spacing:-1px}

.dash-grid{display:grid;grid-template-columns:1.2fr 1fr;gap:14px}
@media(max-width:680px){.dash-grid{grid-template-columns:1fr}}

.cat-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border)}
.cat-row:last-child{border-bottom:none}
.cat-name{width:130px;flex-shrink:0;font-size:13px;color:var(--muted2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-track{flex:1;height:4px;background:var(--surface3);border-radius:2px;overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width .4s cubic-bezier(.4,0,.2,1);background:var(--gold)}
.bar-fill.warn{background:var(--amber)}.bar-fill.over{background:var(--red)}.bar-fill.income{background:var(--green)}.bar-fill.project{background:var(--purple)}
.cat-amount{font-family:'DM Mono',monospace;font-size:12px;min-width:62px;text-align:right;color:var(--text)}
.cat-limit{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);min-width:60px;text-align:right}

.method-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)}
.method-row:last-child{border-bottom:none}
.method-name{width:90px;font-size:13px;color:var(--muted2)}
.method-amount{font-family:'DM Mono',monospace;font-size:12px;min-width:68px;text-align:right}

.alert-row{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border);font-size:13px}
.alert-row:last-child{border-bottom:none}
.badge{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.5px;padding:2px 7px;border-radius:4px;font-weight:500}
.badge.over{background:var(--red-dim);color:var(--red);border:1px solid #e0606030}
.badge.warn{background:var(--amber-dim);color:var(--amber);border:1px solid #d4933a30}
.badge.proj{background:#3d2a5030;color:var(--purple);border:1px solid #a888e030}

.trend-row{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border);font-size:13px;flex-wrap:wrap;gap:6px}
.trend-row:last-child{border-bottom:none}
.trend-stat{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted2)}

.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
.search-box{flex:1;min-width:150px;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-size:13px;font-family:'DM Sans',sans-serif;outline:none;transition:border-color .15s}
.search-box:focus{border-color:var(--gold)}.search-box::placeholder{color:var(--muted)}
.btn-primary{background:var(--gold);color:#0e0c09;border:none;padding:8px 16px;border-radius:8px;font-size:13px;font-weight:600;font-family:'DM Sans',sans-serif;cursor:pointer;transition:opacity .15s;white-space:nowrap}
.btn-primary:hover{opacity:.85}
.btn-danger{background:var(--red-dim);border:1px solid #e0606030;color:var(--red);padding:7px 12px;border-radius:8px;font-size:12px;font-family:'DM Sans',sans-serif;cursor:pointer;transition:all .15s}
.btn-danger:hover{background:#e0606030}
.btn-secondary{background:var(--surface2);border:1px solid var(--border);color:var(--muted2);padding:8px 13px;border-radius:8px;font-size:12px;font-family:'DM Sans',sans-serif;cursor:pointer;transition:all .15s}
.btn-secondary:hover{color:var(--text)}
.btn-sm{padding:5px 10px;font-size:11px}

.tx-wrap{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}
.tx-table{width:100%;border-collapse:collapse}
.tx-table th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);font-family:'DM Mono',monospace;padding:11px 14px;border-bottom:1px solid var(--border);font-weight:500;background:var(--surface2);cursor:pointer;user-select:none;white-space:nowrap}
.tx-table th:hover{color:var(--muted2)}.tx-table th.no-sort{cursor:default}
.tx-table th.sort-asc::after{content:' ↑'}.tx-table th.sort-desc::after{content:' ↓'}
.tx-table td{padding:10px 14px;border-bottom:1px solid var(--border);font-size:13px;transition:background .1s}
.tx-table tr:last-child td{border-bottom:none}
.tx-table tbody tr:hover td{background:var(--surface2);cursor:pointer}
.tx-table tbody tr.tx-selected td{background:#c9a84c08}
.tx-table tbody tr.tx-balance td{opacity:.5}.tx-table tbody tr.tx-balance:hover td{opacity:.8}
.th-check,.td-check{width:38px!important;padding-right:2px!important}
input[type=checkbox]{appearance:none;width:15px;height:15px;border:1px solid var(--border2);border-radius:4px;background:var(--surface2);cursor:pointer;vertical-align:middle;position:relative;transition:all .15s;display:inline-block}
input[type=checkbox]:checked{background:var(--gold);border-color:var(--gold)}
input[type=checkbox]:checked::after{content:'✓';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:9px;color:#0e0c09;font-weight:800}
.pill{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-family:'DM Mono',monospace;background:var(--surface3);color:var(--muted2)}
.pill-Conto{background:#1a3a5520;color:var(--blue)}.pill-Paypal{background:var(--gold-dim);color:var(--gold)}.pill-Contanti{background:var(--green-dim);color:var(--green)}.pill-Libretto{background:#3d2a5020;color:var(--purple)}.pill-Risparmio{background:#e0a06020;color:var(--sav)}.pill-project{background:#3d2a5020;color:var(--purple)}
.tx-amt{font-family:'DM Mono',monospace;font-size:13px;font-weight:500;text-align:right}
.amt-pos{color:var(--green)}.amt-neg{color:var(--red)}.amt-neu{color:var(--muted)}
.tx-date{font-family:'DM Mono',monospace;font-size:12px;color:var(--muted2)}
.empty-state{text-align:center;padding:48px;color:var(--muted);font-size:13px}

/* custom select */
.csel{position:relative;width:100%}
.csel-trigger{width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:10px 32px 10px 12px;border-radius:8px;font-size:14px;font-family:'DM Sans',sans-serif;cursor:pointer;text-align:left;outline:none;transition:border-color .15s;position:relative;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.csel-trigger:focus,.csel-trigger.open{border-color:var(--gold)}
.csel-trigger::after{content:'▾';position:absolute;right:10px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:11px;pointer-events:none;transition:transform .15s}
.csel-trigger.open::after{transform:translateY(-50%) rotate(180deg)}
.csel-list{position:absolute;top:calc(100% + 4px);left:0;right:0;z-index:400;background:var(--surface2);border:1px solid var(--border2);border-radius:9px;box-shadow:0 8px 24px rgba(0,0,0,.55);overflow:hidden;max-height:240px;overflow-y:auto;display:none}
.csel-list.open{display:block;animation:fadeIn .12s ease}
.csel-item{padding:9px 12px;font-size:13px;font-family:'DM Sans',sans-serif;cursor:pointer;color:var(--muted2);transition:background .08s}
.csel-item:hover{background:var(--surface3);color:var(--text)}.csel-item.csel-sel{color:var(--gold)}.csel-item.csel-ph{color:var(--muted);font-style:italic}
.csel-item.csel-group-header{padding:6px 12px 3px;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);font-family:'DM Mono',monospace;cursor:default;background:var(--surface3)}
.csel-item.csel-group-header:hover{background:var(--surface3);color:var(--muted)}
.csel-item.csel-proj-item{padding-left:20px;color:var(--purple)}
.csel-sm .csel-trigger{padding:8px 26px 8px 10px;font-size:13px}

/* modal */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.78);z-index:200;display:flex;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(6px)}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes slideUp{from{transform:translateY(12px);opacity:0}to{transform:translateY(0);opacity:1}}
.modal{background:var(--surface);border:1px solid var(--border2);border-radius:14px;padding:26px;width:100%;max-width:430px;animation:slideUp .2s cubic-bezier(.4,0,.2,1);max-height:90vh;overflow-y:auto}
.modal-title{font-family:'Instrument Serif',serif;font-size:20px;margin-bottom:20px;color:var(--text)}
.field{margin-bottom:13px}
.field label{display:block;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);font-family:'DM Mono',monospace;margin-bottom:6px}
.field input[type=text],.field input[type=number],.field input[type=date]{width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:10px 12px;border-radius:8px;font-size:14px;font-family:'DM Sans',sans-serif;outline:none;transition:border-color .15s}
.field input:focus{border-color:var(--gold)}.field input::placeholder{color:var(--muted)}
.sign-row{display:flex;gap:8px}
.sign-btns{display:flex;background:var(--surface2);border:1px solid var(--border);border-radius:8px;overflow:hidden}
.sign-btn{background:none;border:none;color:var(--muted);font-size:18px;font-weight:700;width:44px;cursor:pointer;transition:all .15s;font-family:monospace}
.sign-btn.is-neg{background:var(--red-dim);color:var(--red)}.sign-btn.is-pos{background:var(--green-dim);color:var(--green)}
.toggle-row{display:flex;align-items:center;gap:10px;padding:8px 0}
.toggle-label{font-size:13px;color:var(--muted2);cursor:pointer;user-select:none}
.toggle{position:relative;width:34px;height:19px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;inset:0;background:var(--surface3);border:1px solid var(--border2);border-radius:10px;cursor:pointer;transition:all .2s}
.toggle-slider::before{content:'';position:absolute;height:13px;width:13px;left:2px;bottom:2px;background:var(--muted);border-radius:50%;transition:all .2s}
.toggle input:checked+.toggle-slider{background:var(--gold-dim);border-color:var(--gold)}
.toggle input:checked+.toggle-slider::before{transform:translateX(15px);background:var(--gold)}
.modal-actions{display:flex;gap:8px;margin-top:20px}
.btn-cancel{flex:1;background:var(--surface2);border:1px solid var(--border);color:var(--muted2);padding:10px;border-radius:8px;font-size:13px;font-family:'DM Sans',sans-serif;cursor:pointer;transition:all .15s}
.btn-cancel:hover{color:var(--text)}
.btn-save{flex:2;background:var(--gold);border:none;color:#0e0c09;padding:10px;border-radius:8px;font-size:13px;font-weight:600;font-family:'DM Sans',sans-serif;cursor:pointer;transition:opacity .15s}
.btn-save:hover{opacity:.85}
.btn-del-tx{background:var(--red-dim);border:1px solid #e0606025;color:var(--red);padding:10px 13px;border-radius:8px;font-size:14px;cursor:pointer;transition:all .15s}
.btn-del-tx:hover{background:#e0606035}

/* categories tab */
.group-label{font-size:10px;text-transform:uppercase;letter-spacing:1.2px;color:var(--gold);font-family:'DM Mono',monospace;padding:14px 0 6px}
.cat-edit-row{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--border)}
.cat-edit-row:last-of-type{border-bottom:none}
.cat-edit-name{flex:1;font-size:13px;color:var(--muted2)}
.cat-spent-lbl{font-family:'DM Mono',monospace;font-size:12px;color:var(--muted);min-width:66px;text-align:right}
.threshold-input{width:74px;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:6px;font-size:12px;font-family:'DM Mono',monospace;text-align:right;outline:none;transition:border-color .15s;-moz-appearance:textfield}
.threshold-input:focus{border-color:var(--gold)}
.threshold-input::-webkit-inner-spin-button,.threshold-input::-webkit-outer-spin-button{opacity:0;transition:opacity .15s}
.threshold-input:hover::-webkit-inner-spin-button,.threshold-input:hover::-webkit-outer-spin-button{opacity:.35}
.btn-x{background:none;border:none;color:var(--muted);cursor:pointer;font-size:16px;width:26px;height:26px;border-radius:5px;display:flex;align-items:center;justify-content:center;transition:all .15s}
.btn-x:hover{background:var(--red-dim);color:var(--red)}
.add-cat-row{display:flex;gap:8px;margin-top:16px;padding-top:16px;border-top:1px solid var(--border)}
.add-cat-row input{flex:1;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-size:13px;font-family:'DM Sans',sans-serif;outline:none;transition:border-color .15s}
.add-cat-row input:focus{border-color:var(--gold)}.add-cat-row input::placeholder{color:var(--muted)}

/* projects tab */
.proj-card{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:10px;transition:border-color .2s}
.proj-card:hover{border-color:var(--border2)}
.proj-card.archived{opacity:.5}
.proj-header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:10px}
.proj-name{font-size:15px;color:var(--text);font-weight:500}
.proj-desc{font-size:12px;color:var(--muted);margin-top:2px}
.proj-meta{font-size:10px;color:var(--muted);font-family:'DM Mono',monospace;margin-top:3px}
.proj-actions{display:flex;gap:6px;flex-shrink:0}
.proj-stats{margin-bottom:10px}
.proj-bar-row{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.proj-amounts{display:flex;justify-content:space-between;font-family:'DM Mono',monospace;font-size:11px;color:var(--muted2)}
.proj-tx-list{border-top:1px solid var(--border);padding-top:10px;margin-top:10px}
.proj-tx-item{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border);font-size:12px}
.proj-tx-item:last-child{border-bottom:none}
.proj-tx-date{font-family:'DM Mono',monospace;color:var(--muted2);min-width:54px}
.proj-tx-desc{flex:1;padding:0 10px;color:var(--muted2)}
.proj-tx-amt{font-family:'DM Mono',monospace;color:var(--red)}
.proj-add-form{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:10px}
.proj-add-form input{background:var(--surface3);border:1px solid var(--border);color:var(--text);padding:8px 10px;border-radius:6px;font-size:13px;font-family:'DM Sans',sans-serif;outline:none;transition:border-color .15s;width:100%}
.proj-add-form input:focus{border-color:var(--gold)}.proj-add-form input::placeholder{color:var(--muted)}
.proj-form-row{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}

.rollover-item{display:flex;justify-content:space-between;padding:6px 0;font-size:13px;border-bottom:1px solid var(--border)}
.rollover-item:last-child{border-bottom:none}
.rollover-preview-box{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:14px;margin-top:12px}

.toast{position:fixed;bottom:24px;right:24px;background:var(--surface2);border:1px solid var(--border2);color:var(--text);padding:10px 18px;border-radius:9px;font-size:13px;z-index:999;animation:fadeIn .2s ease;box-shadow:0 4px 16px rgba(0,0,0,.4)}
.divider{height:1px;background:var(--border);margin:8px 0}

@media(max-width:600px){.header{padding:0 12px}.main{padding:14px 12px 60px}.hide-sm{display:none!important}.modal{padding:18px}.net-row{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>

<div class="header">
  <div class="logo">
    <div class="logo-text">Alessio's Ledger</div>
    <div class="logo-year" id="header-year"></div>
  </div>
  <div class="header-center">
    <div class="month-nav">
      <button onclick="changeMonth(-1)">‹</button>
      <span class="month-label" id="month-label">—</span>
      <button onclick="changeMonth(1)">›</button>
    </div>
    <div class="today-chip" id="today-chip">—</div>
  </div>
  <div class="header-actions">
    <button class="btn-ghost" onclick="exportBackup()">↓ Backup</button>
    <button class="btn-ghost" onclick="document.getElementById('importFile').click()">↑ Ripristina</button>
    <input type="file" id="importFile" accept=".json" style="display:none" onchange="importBackup(event)">
  </div>
</div>

<div class="main">
  <div class="balance-strip" id="balance-strip"></div>
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('overview',this)">Panoramica</button>
    <button class="tab-btn" onclick="switchTab('transactions',this)">Transazioni</button>
    <button class="tab-btn" onclick="switchTab('categories',this)">Categorie</button>
    <button class="tab-btn" onclick="switchTab('projects',this)">Progetti</button>
  </div>

  <!-- Panoramica -->
  <div class="tab-pane active" id="pane-overview">
    <div class="card">
      <div class="card-title" style="margin-bottom:14px">
        Riepilogo mensile
        <span style="color:var(--muted);font-size:9px;margin-left:8px;font-family:'DM Mono',monospace">saldi iniziali esclusi</span>
      </div>
      <div class="net-row">
        <div class="net-item"><div class="ni-label">Entrate</div><div class="ni-value" id="ni-in" style="color:var(--green)">—</div></div>
        <div class="net-item"><div class="ni-label">Uscite</div><div class="ni-value" id="ni-out" style="color:var(--red)">—</div></div>
        <div class="net-item"><div class="ni-label">Risparmio</div><div class="ni-value" id="ni-sav" style="color:var(--sav)">—</div></div>
        <div class="net-item"><div class="ni-label">Saldo netto</div><div class="ni-value" id="ni-net">—</div></div>
      </div>
    </div>
    <div id="trend-container"></div>
    <div class="dash-grid">
      <div>
        <div class="card">
          <div class="card-head"><div class="card-title">Spesa per categoria</div></div>
          <div id="cat-breakdown"><div class="empty-state">Nessuna transazione</div></div>
        </div>
      </div>
      <div>
        <div class="card"><div class="card-title" style="margin-bottom:14px">Metodi di pagamento</div><div id="method-breakdown"></div></div>
        <div class="card"><div class="card-title" style="margin-bottom:14px">Soglie mensili</div><div id="threshold-section"></div></div>
      </div>
    </div>
  </div>

  <!-- Transazioni -->
  <div class="tab-pane" id="pane-transactions">
    <div class="toolbar">
      <input class="search-box" type="text" id="search-input" placeholder="Cerca…" oninput="renderTxTable()">
      <div id="csel-fcat" style="width:160px"></div>
      <div id="csel-fmethod" style="width:130px"></div>
      <div id="csel-ftype" style="width:110px"></div>
      <button class="btn-danger" id="btn-bulk-del" style="display:none" onclick="bulkDelete()">✕ Elimina selezionate</button>
      <button class="btn-primary" onclick="openModal()" style="margin-left:auto">+ Aggiungi</button>
    </div>
    <div class="tx-wrap">
      <table class="tx-table">
        <thead>
          <tr>
            <th class="th-check no-sort"><input type="checkbox" id="check-all" onchange="toggleAll(this)"></th>
            <th id="th-date" onclick="sortBy('date')">Data</th>
            <th id="th-description" onclick="sortBy('description')">Descrizione</th>
            <th id="th-category" class="hide-sm" onclick="sortBy('category')">Categoria / Progetto</th>
            <th id="th-method" class="hide-sm" onclick="sortBy('method')">Metodo</th>
            <th id="th-amount" onclick="sortBy('amount')" style="text-align:right">Importo</th>
          </tr>
        </thead>
        <tbody id="tx-body"></tbody>
      </table>
    </div>
  </div>

  <!-- Categorie -->
  <div class="tab-pane" id="pane-categories">
    <div class="card">
      <div class="card-head"><div class="card-title">Categorie e soglie mensili (€)</div></div>
      <div id="cat-list"></div>
      <div class="add-cat-row">
        <input type="text" id="new-cat-name" placeholder="Nome nuova categoria…">
        <div id="csel-newgroup" style="width:140px"></div>
        <button class="btn-primary" onclick="addCategory()">Aggiungi</button>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><div class="card-title">Riporto budget non utilizzato</div></div>
      <div style="font-size:13px;color:var(--muted2);margin-bottom:12px">Trasferisci come bonus al mese successivo i budget non spesi (sfori inclusi come malus).</div>
      <button class="btn-secondary" onclick="calcRollover()">Calcola riporto →</button>
      <div id="rollover-preview" style="display:none"></div>
      <div id="rollover-actions" style="display:none;margin-top:10px">
        <button class="btn-primary" onclick="applyRollover()">Applica al mese successivo</button>
      </div>
    </div>
  </div>

  <!-- Progetti -->
  <div class="tab-pane" id="pane-projects">
    <div class="proj-add-form">
      <div style="font-size:10px;text-transform:uppercase;letter-spacing:1.2px;color:var(--muted);font-family:'DM Mono',monospace;margin-bottom:10px">Nuovo progetto</div>
      <input type="text" id="new-proj-name" placeholder="Nome progetto (es. Patente, Viaggio Giappone…)">
      <div class="proj-form-row">
        <input type="number" id="new-proj-budget" placeholder="Budget € (opzionale)" min="0" step="1" style="flex:1;-moz-appearance:textfield">
        <input type="text" id="new-proj-desc" placeholder="Note opzionali…" style="flex:2">
        <button class="btn-primary" onclick="addProject()">Aggiungi</button>
      </div>
    </div>
    <div id="projects-active-list"></div>
    <div id="projects-archived-section" style="display:none">
      <div style="font-size:10px;text-transform:uppercase;letter-spacing:1.2px;color:var(--muted);font-family:'DM Mono',monospace;padding:8px 0 12px">Archiviati</div>
      <div id="projects-archived-list"></div>
    </div>
    <button class="btn-secondary" id="btn-toggle-archived" onclick="toggleArchivedProjects()" style="margin-top:4px">Mostra archiviati</button>
  </div>
</div>

<!-- Modal transazione -->
<div class="modal-overlay" id="modal" style="display:none" onclick="overlayClick(event)">
  <div class="modal">
    <div class="modal-title" id="modal-title">Nuova transazione</div>
    <div class="field">
      <label>Importo</label>
      <div class="sign-row">
        <div class="sign-btns">
          <button class="sign-btn is-neg" id="btn-neg" onclick="setSign(-1)" type="button">−</button>
          <button class="sign-btn" id="btn-pos" onclick="setSign(1)" type="button">+</button>
        </div>
        <input type="number" id="f-amount" placeholder="0.00" step="0.01" min="0" style="flex:1">
      </div>
    </div>
    <div class="field"><label>Descrizione</label><input type="text" id="f-desc" placeholder="es. spesa coop, lezione guida…"></div>
    <div class="field"><label>Categoria / Progetto</label><div id="csel-fcat-m"></div></div>
    <div class="field"><label>Metodo di pagamento</label><div id="csel-fmethod-m"></div></div>
    <div class="field"><label>Data</label><input type="date" id="f-date"></div>
    <div class="toggle-row">
      <label class="toggle"><input type="checkbox" id="f-isbal"><span class="toggle-slider"></span></label>
      <span class="toggle-label" onclick="document.getElementById('f-isbal').click()">Saldo iniziale / riporto — escluso dal riepilogo</span>
    </div>
    <div class="modal-actions">
      <button class="btn-del-tx" id="btn-del-tx" style="display:none" onclick="deleteTx()" type="button">✕</button>
      <button class="btn-cancel" onclick="closeModal()" type="button">Annulla</button>
      <button class="btn-save" onclick="saveTx()" type="button">Salva</button>
    </div>
  </div>
</div>

<script>
'use strict';
const MONTHS=['Gennaio','Febbraio','Marzo','Aprile','Maggio','Giugno','Luglio','Agosto','Settembre','Ottobre','Novembre','Dicembre'];
const DAYS=['DOM','LUN','MAR','MER','GIO','VEN','SAB'];
const SM=['gen','feb','mar','apr','mag','giu','lug','ago','set','ott','nov','dic'];
const METHODS=['Conto','Paypal','Contanti','Libretto','Risparmio'];
const MCOLOR={Conto:'#6ab0d4',Paypal:'#c9a84c',Contanti:'#6bbd82',Libretto:'#a888e0',Risparmio:'#e0a060'};

const today = new Date();
let curYear  = today.getFullYear();
let curMonth = today.getMonth()+1;
let editId=null, sign=-1, sortCol='date', sortDir='desc';
let selectedIds=new Set();
let showArchived=false;

let STATE={txs:[],cats:[],balances:{},carryovers:[],coMap:{},projects:[]};

// ── API ──
async function api(path, opts={}) {
  const res = await fetch(path, {
    headers:{'Content-Type':'application/json'},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined
  });
  if (!res.ok) { showToast('Errore server '+res.status); throw new Error(res.status); }
  return res.json();
}

// ── Custom Select ──
class CS {
  constructor(anchorId, opts, val, onCh, sm=false) {
    this.anchor=typeof anchorId==='string'?document.getElementById(anchorId):anchorId;
    this.opts=opts; this.val=val; this.onCh=onCh; this.isOpen=false;
    this.root=document.createElement('div');
    this.root.className='csel'+(sm?' csel-sm':'');
    this.btn=document.createElement('button');
    this.btn.type='button'; this.btn.className='csel-trigger';
    this.ul=document.createElement('ul');
    this.ul.className='csel-list';
    this.root.append(this.btn, this.ul);
    this.anchor.replaceChildren(this.root);
    this.btn.addEventListener('click', e=>{e.stopPropagation(); this.isOpen?this.close_():this.open_();});
    document.addEventListener('click', ()=>this.close_());
    this._render();
  }
  setOpts(opts, keepVal=false) {
    this.opts=opts;
    if(!keepVal) { const f=opts.find(o=>!o.header); this.val=f?.value??f??''; }
    this._render();
  }
  get value() { return this.val; }
  set value(v) { this.val=v; this._render(); }
  open_() { this.isOpen=true; this.btn.classList.add('open'); this.ul.classList.add('open'); }
  close_() { this.isOpen=false; this.btn.classList.remove('open'); this.ul.classList.remove('open'); }
  _render() {
    // find label for current val
    let lbl='—';
    for (const o of this.opts) {
      if (o.header) continue;
      if ((o?.value??o)===this.val) { lbl=o?.label??o; break; }
    }
    this.btn.textContent=lbl;
    this.btn.style.color=(!this.val||this.val==='') ? 'var(--muted)' : 'var(--text)';
    this.ul.innerHTML='';
    this.opts.forEach(o=>{
      if (o.header) {
        const li=document.createElement('li');
        li.className='csel-item csel-group-header'; li.textContent=o.header;
        this.ul.appendChild(li); return;
      }
      const v=o?.value??o, l=o?.label??o;
      const li=document.createElement('li');
      li.className='csel-item'+(o.projItem?' csel-proj-item':'')+(v===this.val?' csel-sel':'')+(v===''?' csel-ph':'');
      li.textContent=l;
      li.addEventListener('click', e=>{
        e.stopPropagation(); this.val=v; this._render(); this.close_();
        this.onCh&&this.onCh(v);
      });
      this.ul.appendChild(li);
    });
  }
}

let sFC, sFM, sFT, sNG, sMFC, sMFM;

function buildCatModalOpts() {
  const activeProjects = STATE.projects.filter(p=>!p.archived);
  const opts = [
    {value:'', label:'— nessuna —'},
    {header:'CATEGORIE'},
    ...STATE.cats.map(c=>({value:'cat:'+c.name, label:c.name})),
  ];
  if (activeProjects.length) {
    opts.push({header:'PROGETTI'});
    activeProjects.forEach(p=>opts.push({value:'proj:'+p.id, label:p.name, projItem:true}));
  }
  return opts;
}

function initSelects() {
  const catOpts = [
    {value:'', label:'Tutte le categorie'},
    {header:'CATEGORIE'},
    ...STATE.cats.map(c=>({value:'cat:'+c.name, label:c.name})),
    ...(STATE.projects.length?[{header:'PROGETTI'}]:[]),
    ...STATE.projects.map(p=>({value:'proj:'+p.id, label:p.name, projItem:true})),
  ];
  const mOpts   = [{value:'',label:'Tutti i metodi'},...METHODS.map(m=>({value:m,label:m}))];
  const tOpts   = [{value:'',label:'Tutto'},{value:'out',label:'Uscite'},{value:'in',label:'Entrate'},{value:'bal',label:'Saldi'}];
  const gOpts   = [{value:'NECESSARIE',label:'Necessarie'},{value:'AGGIUNTIVE',label:'Aggiuntive'}];

  if (!sFC) {
    sFC  = new CS('csel-fcat',    catOpts,     '', ()=>renderTxTable(), true);
    sFM  = new CS('csel-fmethod', mOpts,       '', ()=>renderTxTable(), true);
    sFT  = new CS('csel-ftype',   tOpts,       '', ()=>renderTxTable(), true);
    sNG  = new CS('csel-newgroup',gOpts, 'NECESSARIE', null, true);
    sMFC = new CS('csel-fcat-m',  buildCatModalOpts(), '', v=>{if(v==='cat:guadagni')setSign(1);});
    sMFM = new CS('csel-fmethod-m', METHODS.map(m=>({value:m,label:m})), 'Conto', null);
  } else {
    sFC.setOpts(catOpts, true);
    sMFC.setOpts(buildCatModalOpts(), true);
  }
}

// ── Render master ──
async function render() {
  const [txs, cats, balances, carryovers, projects] = await Promise.all([
    api(`/api/txs?year=${curYear}&month=${curMonth}`),
    api('/api/cats'),
    api('/api/balances'),
    api(`/api/carryovers?year=${curYear}&month=${curMonth}`),
    api('/api/projects?archived=1'),
  ]);
  const coMap={};
  carryovers.forEach(r=>coMap[r.category]=r.amount);
  STATE={txs, cats, balances, carryovers, coMap, projects};

  document.getElementById('month-label').textContent = MONTHS[curMonth-1].toUpperCase()+' '+curYear;
  document.getElementById('header-year').textContent = curYear;
  document.getElementById('today-chip').textContent  =
    DAYS[today.getDay()]+' '+today.getDate()+' '+SM[today.getMonth()].toUpperCase();

  initSelects();
  renderBalances();
  renderOverview();
  renderTxTable();
  renderCatList();
  await renderProjects();
}

// ── Balances ──
function renderBalances() {
  const b=STATE.balances;
  const avail=METHODS.filter(m=>m!=='Risparmio').reduce((a,m)=>a+(b[m]||0),0);
  document.getElementById('balance-strip').innerHTML=
    METHODS.map(m=>`<div class="balance-card bc-${m}">
      <div class="b-label">${m}</div>
      <div class="b-value ${(b[m]||0)<0?'b-neg':''}">${euroAbs(b[m]||0)}</div>
    </div>`).join('')+
    `<div class="balance-card bc-total">
      <div class="b-label">Disponibile</div>
      <div class="b-value ${avail<0?'b-neg':''}">${euroAbs(avail)}</div>
    </div>`;
}

// ── Overview ──
function renderOverview() {
  const real=STATE.txs.filter(t=>!t.is_balance);
  const sav =real.filter(t=>t.method==='Risparmio'&&t.amount<0).reduce((a,t)=>a+Math.abs(t.amount),0);
  const inc =real.filter(t=>t.amount>0&&t.method!=='Risparmio').reduce((a,t)=>a+t.amount,0);
  const exp =real.filter(t=>t.amount<0&&t.method!=='Risparmio').reduce((a,t)=>a+t.amount,0);
  const net =inc+exp;
  document.getElementById('ni-in').textContent  = euroAbs(inc);
  document.getElementById('ni-out').textContent = '−'+euroAbs(Math.abs(exp));
  document.getElementById('ni-sav').textContent = sav>0?euroAbs(sav):'—';
  const nel=document.getElementById('ni-net');
  nel.textContent=euroSgn(net); nel.style.color=net>=0?'var(--green)':'var(--red)';

  renderTrend();

  const spend={};
  real.filter(t=>t.category&&t.amount<0&&t.method!=='Risparmio'&&!t.project_id).forEach(t=>{
    spend[t.category]=(spend[t.category]||0)+Math.abs(t.amount);
  });
  const co=STATE.coMap;
  const visible=STATE.cats.filter(c=>c.threshold||(spend[c.name]>0));
  if (!visible.length) {
    document.getElementById('cat-breakdown').innerHTML='<div class="empty-state">Nessuna spesa registrata</div>';
  } else {
    const maxV=Math.max(...visible.map(c=>{
      if(c.name==='guadagni') return real.filter(t=>t.category==='guadagni'&&t.amount>0).reduce((a,t)=>a+t.amount,0);
      return spend[c.name]||0;
    }),1);
    document.getElementById('cat-breakdown').innerHTML=visible.map(c=>{
      const isInc=c.name==='guadagni';
      const val=isInc?real.filter(t=>t.category==='guadagni'&&t.amount>0).reduce((a,t)=>a+t.amount,0):(spend[c.name]||0);
      const eff=c.threshold!=null?c.threshold+(co[c.name]||0):null;
      const pct=eff?Math.min(val/eff*100,100):val/maxV*100;
      const cls=isInc?'income':eff&&val>eff?'over':eff&&val>eff*.8?'warn':'';
      const coNote=co[c.name]>0?`<span style="color:var(--amber);font-size:10px"> +${co[c.name].toFixed(0)}</span>`
                  :co[c.name]<0?`<span style="color:var(--red);font-size:10px"> ${co[c.name].toFixed(0)}</span>`:'';
      return `<div class="cat-row">
        <div class="cat-name">${c.name}</div>
        <div class="bar-track"><div class="bar-fill ${cls}" style="width:${pct.toFixed(1)}%"></div></div>
        <div class="cat-amount">${euroAbs(val)}</div>
        <div class="cat-limit">${eff!=null?'/ €'+eff.toFixed(0)+coNote:''}</div>
      </div>`;
    }).join('');
  }

  const mOut={};METHODS.forEach(m=>mOut[m]=0);
  real.filter(t=>t.amount<0).forEach(t=>{if(mOut[t.method]!==undefined)mOut[t.method]+=Math.abs(t.amount);});
  const maxM=Math.max(...Object.values(mOut),1);
  document.getElementById('method-breakdown').innerHTML=
    METHODS.filter(m=>mOut[m]>0).map(m=>`
    <div class="method-row">
      <div class="method-name">${m}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${(mOut[m]/maxM*100).toFixed(1)}%;background:${MCOLOR[m]}"></div></div>
      <div class="method-amount">−${euroAbs(mOut[m])}</div>
    </div>`).join('')||'<div class="empty-state">Nessuna uscita</div>';

  const over=STATE.cats.filter(c=>c.threshold&&(spend[c.name]||0)>c.threshold+(co[c.name]||0));
  const warn=STATE.cats.filter(c=>c.threshold&&!over.includes(c)&&(spend[c.name]||0)>(c.threshold+(co[c.name]||0))*.8);
  document.getElementById('threshold-section').innerHTML=
    !over.length&&!warn.length
    ?'<div style="color:var(--green);font-size:13px;padding:6px 0">✓ Nessuna soglia superata</div>'
    :[...over,...warn].map(c=>{
        const val=spend[c.name]||0, eff=c.threshold+(co[c.name]||0), isO=val>eff;
        return `<div class="alert-row"><span>${c.name}</span>
          <div style="display:flex;gap:8px;align-items:center">
            <span style="font-family:'DM Mono',monospace;font-size:12px;color:var(--muted2)">${euroAbs(val)} / €${eff.toFixed(0)}</span>
            <span class="badge ${isO?'over':'warn'}">${isO?'SFORATO':'ATTENZIONE'}</span>
          </div></div>`;
      }).join('');
}

// ── Trend ──
function renderTrend() {
  const isCur=today.getFullYear()===curYear&&today.getMonth()+1===curMonth;
  const dim=new Date(curYear,curMonth,0).getDate();
  const elapsed=isCur?today.getDate():dim;
  const tf=elapsed/dim;
  const co=STATE.coMap;
  const spend={};
  STATE.txs.filter(t=>!t.is_balance&&t.category&&t.amount<0&&t.method!=='Risparmio'&&!t.project_id).forEach(t=>{
    spend[t.category]=(spend[t.category]||0)+Math.abs(t.amount);
  });
  const alerts=STATE.cats.filter(c=>c.threshold&&spend[c.name]).map(c=>{
    const eff=c.threshold+(co[c.name]||0), val=spend[c.name]||0;
    const pace=(val/eff)/Math.max(tf,.01);
    return{name:c.name,pace,val,eff,level:pace>1.6?'danger':pace>1.25?'warn':null};
  }).filter(a=>a.level&&a.val<a.eff);
  const el=document.getElementById('trend-container');
  if(!alerts.length){el.innerHTML='';return;}
  el.innerHTML=`<div class="card" style="border-color:#d4933a28;margin-bottom:14px">
    <div class="card-title" style="color:var(--amber);margin-bottom:12px">⚡ Trend — ${elapsed}/${dim} giorni</div>
    ${alerts.map(a=>{
      const proj=(a.val/elapsed)*dim;
      return `<div class="trend-row">
        <span>${a.level==='danger'?'🔴':'🟡'} <strong>${a.name}</strong></span>
        <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <span class="trend-stat">ritmo ${(a.pace*100).toFixed(0)}% — proiezione ${euroAbs(proj)} / €${a.eff.toFixed(0)}</span>
          <span class="badge ${a.level==='danger'?'over':'warn'}">${a.level==='danger'?'ACCELERA':'ATTENZIONE'}</span>
        </div></div>`;
    }).join('')}</div>`;
}

// ── Tx table ──
function txCatLabel(t) {
  if (t.project_id) {
    const p=STATE.projects.find(p=>p.id===t.project_id);
    return p ? `<span class="pill pill-project">▸ ${p.name}</span>` : `<span class="pill pill-project">progetto #${t.project_id}</span>`;
  }
  return t.category ? `<span class="pill">${t.category}</span>` : '<span style="color:var(--muted)">—</span>';
}

function renderTxTable() {
  const search=(document.getElementById('search-input')?.value||'').toLowerCase();
  const catF=sFC?.value||'', methF=sFM?.value||'', typeF=sFT?.value||'';

  let rows=STATE.txs.filter(t=>{
    if(search&&!t.description.toLowerCase().includes(search)
      &&!(t.category||'').toLowerCase().includes(search)
      &&!(STATE.projects.find(p=>p.id===t.project_id)?.name||'').toLowerCase().includes(search)) return false;
    if(catF) {
      if(catF.startsWith('cat:') && t.category!==(catF.slice(4))) return false;
      if(catF.startsWith('proj:') && t.project_id!==parseInt(catF.slice(5))) return false;
    }
    if(methF&&t.method!==methF) return false;
    if(typeF==='out'&&(t.amount>=0||t.is_balance)) return false;
    if(typeF==='in'&&(t.amount<=0||t.is_balance)) return false;
    if(typeF==='bal'&&!t.is_balance) return false;
    return true;
  });

  rows=[...rows].sort((a,b)=>{
    let va=a[sortCol],vb=b[sortCol];
    if(sortCol==='date'){va=new Date(va);vb=new Date(vb);}
    if(sortCol==='amount'){va=a.amount;vb=b.amount;}
    return sortDir==='asc'?(va<vb?-1:va>vb?1:0):(va>vb?-1:va<vb?1:0);
  });

  ['date','description','category','method','amount'].forEach(col=>{
    const th=document.getElementById('th-'+col);if(!th)return;
    th.className=sortCol===col?'sort-'+sortDir:'';
    if(col==='amount')th.style.textAlign='right';
  });

  const tbody=document.getElementById('tx-body');
  if(!rows.length){tbody.innerHTML='<tr><td colspan="6"><div class="empty-state">Nessuna transazione trovata</div></td></tr>';return;}
  tbody.innerHTML=rows.map(t=>{
    const d=new Date(t.date);
    const ds=d.getDate().toString().padStart(2,'0')+'/'+(d.getMonth()+1).toString().padStart(2,'0');
    const amtCls=t.is_balance?'amt-neu':t.amount>=0?'amt-pos':'amt-neg';
    const sel=selectedIds.has(t.id);
    return `<tr class="${t.is_balance?'tx-balance':''} ${sel?'tx-selected':''}" onclick="rowClick(event,${t.id})">
      <td class="td-check" onclick="event.stopPropagation()">
        <input type="checkbox" ${sel?'checked':''} onchange="toggleSel(${t.id},this.checked)">
      </td>
      <td><span class="tx-date">${ds}</span></td>
      <td>${t.description||'—'}${t.is_balance?'<span style="font-size:10px;color:var(--muted);margin-left:6px">↩</span>':''}</td>
      <td class="hide-sm">${txCatLabel(t)}</td>
      <td class="hide-sm"><span class="pill pill-${t.method}">${t.method}</span></td>
      <td><span class="tx-amt ${amtCls}">${euroSgn(t.amount)}</span></td>
    </tr>`;
  }).join('');
  document.getElementById('check-all').checked=rows.length>0&&rows.every(t=>selectedIds.has(t.id));
  document.getElementById('btn-bulk-del').style.display=selectedIds.size>0?'inline-block':'none';
}

function sortBy(col) {
  sortDir=(sortCol===col&&sortDir==='asc')?'desc':'asc';
  if(sortCol!==col&&col==='date')sortDir='desc';
  sortCol=col;renderTxTable();
}
function rowClick(e,id){if(e.target.type!=='checkbox')openModal(id);}
function toggleSel(id,on){on?selectedIds.add(id):selectedIds.delete(id);renderTxTable();}
function toggleAll(cb){STATE.txs.forEach(t=>cb.checked?selectedIds.add(t.id):selectedIds.delete(t.id));renderTxTable();}
async function bulkDelete(){
  if(!selectedIds.size||!confirm(`Eliminare ${selectedIds.size} transazioni?`))return;
  await api('/api/txs/bulk-delete',{method:'POST',body:{ids:[...selectedIds]}});
  selectedIds.clear();showToast('Transazioni eliminate');await render();
}

// ── Cat list ──
function renderCatList() {
  const spend={};
  STATE.txs.filter(t=>!t.is_balance&&t.category&&t.amount<0).forEach(t=>{
    spend[t.category]=(spend[t.category]||0)+Math.abs(t.amount);
  });
  const co=STATE.coMap;
  document.getElementById('cat-list').innerHTML=['NECESSARIE','AGGIUNTIVE'].map(g=>{
    return `<div class="group-label">${g}</div>`+
      STATE.cats.filter(c=>c.grp===g).map(c=>{
        const hint=co[c.name]>0?` <span style="color:var(--amber);font-size:10px">(+${co[c.name].toFixed(0)} riportato)</span>`
                  :co[c.name]<0?` <span style="color:var(--red);font-size:10px">(${co[c.name].toFixed(0)} malus)</span>`:'';
        return `<div class="cat-edit-row">
          <div class="cat-edit-name">${c.name}${hint}</div>
          <div class="cat-spent-lbl">${spend[c.name]?euroAbs(spend[c.name]):''}</div>
          <input class="threshold-input" type="number" min="0" step="1"
            placeholder="soglia €" value="${c.threshold!=null?c.threshold:''}"
            onchange="updateThreshold(${c.id},this.value)">
          <button class="btn-x" onclick="deleteCategory(${c.id})">×</button>
        </div>`;
      }).join('');
  }).join('');
}

// ── Rollover ──
async function calcRollover() {
  let nm=curMonth+1, ny=curYear;
  if(nm>12){nm=1;ny++;}
  const net={};
  STATE.txs.filter(t=>!t.is_balance&&t.category&&t.method!=='Risparmio'&&!t.project_id).forEach(t=>{
    net[t.category]=(net[t.category]||0)+t.amount;  // somma con segno
  });
  const co = STATE.coMap;
  const items = STATE.cats.filter(c=>c.threshold).map(c=>{
    const eff   = c.threshold + (co[c.name]||0);
    const delta = parseFloat((eff + (net[c.name]||0)).toFixed(2));  // eff + somma netta
    return {name:c.name, eff, spent: -(net[c.name]||0), delta};
  }).filter(c=>Math.abs(c.delta)>0.01);

  const prev=document.getElementById('rollover-preview');
  const acts=document.getElementById('rollover-actions');
  if(!items.length){
    prev.style.display='block';
    prev.innerHTML='<div class="rollover-preview-box"><div style="color:var(--muted);font-size:13px">Budget completamente esauriti.</div></div>';
    acts.style.display='none'; return;
  }
  prev.style.display='block';
  prev.innerHTML=`<div class="rollover-preview-box">
    <div style="display:grid;grid-template-columns:1fr 90px 90px 90px;gap:0;font-size:10px;color:var(--muted);font-family:'DM Mono',monospace;padding-bottom:6px;margin-bottom:2px;border-bottom:1px solid var(--border);letter-spacing:.5px">
      <span>CATEGORIA</span>
      <span style="text-align:right">BUDGET</span>
      <span style="text-align:right">SPESO</span>
      <span style="text-align:right">RIPORTO</span>
    </div>
    ${items.map(u=>`
    <div style="display:grid;grid-template-columns:1fr 90px 90px 90px;gap:0;padding:6px 0;border-bottom:1px solid var(--border)">
      <span style="color:var(--muted2);font-size:13px">${u.name}</span>
      <span style="font-family:'DM Mono',monospace;font-size:12px;color:var(--muted);text-align:right">€${u.eff.toFixed(0)}</span>
      <span style="font-family:'DM Mono',monospace;font-size:12px;color:var(--muted);text-align:right">${euroAbs(u.spent)}</span>
      <span style="font-family:'DM Mono',monospace;font-size:12px;font-weight:500;text-align:right;color:${u.delta>0?'var(--green)':'var(--red)'}">${u.delta>0?'+':'−'}${euroAbs(Math.abs(u.delta))}</span>
    </div>`).join('')}
    <div style="display:grid;grid-template-columns:1fr 90px 90px 90px;gap:0;padding:7px 0 2px;font-size:11px;color:var(--muted);font-family:'DM Mono',monospace">
      <span></span><span></span><span></span>
      <span style="text-align:right;color:var(--muted2)">→ ${MONTHS[nm-1].slice(0,3)} ${ny}</span>
    </div>
  </div>`;
  acts.style.display='block';
}
async function applyRollover() {
  let nm=curMonth+1,ny=curYear;
  if(nm>12){nm=1;ny++;}
  await api('/api/carryovers/force',{method:'POST',body:{year:ny,month:nm}});
  document.getElementById('rollover-preview').style.display='none';
  document.getElementById('rollover-actions').style.display='none';
  showToast(`Riporto applicato a ${MONTHS[nm-1]} ${ny}`);
}

// ── Projects ──
async function renderProjects() {
  const allProjects = await api('/api/projects?archived=1');
  STATE.projects = allProjects;
  const active   = allProjects.filter(p=>!p.archived);
  const archived = allProjects.filter(p=>p.archived);

  const el = document.getElementById('projects-active-list');
  if (!active.length) {
    el.innerHTML='<div class="empty-state">Nessun progetto attivo. Creane uno sopra.</div>';
  } else {
    // fetch spending for all active projects in parallel
    const spending = await Promise.all(active.map(p=>api(`/api/projects/${p.id}/spending`)));
    el.innerHTML = active.map((p,i)=>{
      const s=spending[i];
      const pct=p.total_budget?Math.min(s.spent/p.total_budget*100,100):null;
      const rem=p.total_budget!=null?p.total_budget-s.spent:null;
      const cls=pct>100?'over':pct>80?'warn':'';
      const txHtml=s.transactions.map(t=>{
        const d=new Date(t.date);
        const ds=d.getDate().toString().padStart(2,'0')+'/'+(d.getMonth()+1).toString().padStart(2,'0')+'/'+d.getFullYear();
        return `<div class="proj-tx-item">
          <span class="proj-tx-date">${ds}</span>
          <span class="proj-tx-desc">${t.description||'—'}</span>
          <span class="proj-tx-amt">${euroSgn(t.amount)}</span>
        </div>`;
      }).join('')||'<div style="color:var(--muted);font-size:12px;padding:6px 0">Nessuna spesa ancora — seleziona questo progetto quando aggiungi una transazione</div>';

      return `<div class="proj-card">
        <div class="proj-header">
          <div>
            <div class="proj-name">${p.name}</div>
            ${p.description?`<div class="proj-desc">${p.description}</div>`:''}
            <div class="proj-meta">creato il ${p.created_at} · ${s.transactions.length} transazioni</div>
          </div>
          <div class="proj-actions">
            <button class="btn-secondary btn-sm" onclick="archiveProject(${p.id},${p.archived},'${p.name.replace(/'/g,"\\'")}',${p.total_budget||'null'},'${p.description.replace(/'/g,"\\'")}')">Archivia</button>
            <button class="btn-danger btn-sm" onclick="deleteProject(${p.id},'${p.name.replace(/'/g,"\\'")}')">Elimina</button>
          </div>
        </div>
        <div class="proj-stats">
          ${p.total_budget!=null?`
          <div class="proj-bar-row">
            <div class="bar-track" style="height:5px;flex:1">
              <div class="bar-fill ${cls}" style="width:${(pct||0).toFixed(1)}%"></div>
            </div>
          </div>
          <div class="proj-amounts">
            <span>speso ${euroAbs(s.spent)}</span>
            ${rem>=0
              ?`<span style="color:var(--green)">rimangono ${euroAbs(rem)}</span>`
              :`<span style="color:var(--red)">sforato di ${euroAbs(Math.abs(rem))}</span>`}
            <span>budget €${p.total_budget}</span>
          </div>`
          :`<div style="font-family:'DM Mono',monospace;font-size:12px;color:var(--muted2)">speso ${euroAbs(s.spent)} — nessun limite impostato</div>`}
        </div>
        <div class="proj-tx-list">${txHtml}</div>
      </div>`;
    }).join('');
  }

  const arcEl=document.getElementById('projects-archived-list');
  if (archived.length && showArchived) {
    arcEl.innerHTML=archived.map(p=>`
      <div class="proj-card archived">
        <div class="proj-header">
          <div><div class="proj-name">${p.name}</div>${p.description?`<div class="proj-desc">${p.description}</div>`:''}</div>
          <div class="proj-actions">
            <button class="btn-secondary btn-sm" onclick="archiveProject(${p.id},${p.archived},'${p.name.replace(/'/g,"\\'")}',${p.total_budget||'null'},'${p.description.replace(/'/g,"\\'")}')">Ripristina</button>
            <button class="btn-danger btn-sm" onclick="deleteProject(${p.id},'${p.name.replace(/'/g,"\\'")}')">Elimina</button>
          </div>
        </div>
      </div>`).join('');
  } else {
    arcEl.innerHTML='';
  }
}

function toggleArchivedProjects() {
  showArchived=!showArchived;
  document.getElementById('projects-archived-section').style.display=showArchived?'block':'none';
  document.getElementById('btn-toggle-archived').textContent=showArchived?'Nascondi archiviati':'Mostra archiviati';
  renderProjects();
}

async function addProject() {
  const name=document.getElementById('new-proj-name').value.trim();
  if(!name){alert('Inserisci un nome');return;}
  const budget=document.getElementById('new-proj-budget').value;
  const desc=document.getElementById('new-proj-desc').value.trim();
  await api('/api/projects',{method:'POST',body:{
    name, description:desc,
    total_budget:budget?parseFloat(budget):null
  }});
  document.getElementById('new-proj-name').value='';
  document.getElementById('new-proj-budget').value='';
  document.getElementById('new-proj-desc').value='';
  showToast(`Progetto "${name}" creato`);
  await render();
}

async function archiveProject(id, currentArchived, name, budget, desc) {
  const newArchived = currentArchived ? 0 : 1;
  const action = newArchived ? 'archiviare' : 'ripristinare';
  if (!confirm(`Vuoi ${action} il progetto "${name}"?`)) return;
  await api(`/api/projects/${id}`, {method:'PUT', body:{
    name, total_budget:budget, description:desc, archived:newArchived
  }});
  showToast(newArchived ? `"${name}" archiviato` : `"${name}" ripristinato`);
  await render();
}

async function deleteProject(id, name) {
  if (!confirm(`Eliminare definitivamente il progetto "${name}"?\nLe transazioni collegate rimarranno ma non saranno più associate al progetto.`)) return;
  await api(`/api/projects/${id}`, {method:'DELETE'});
  showToast(`"${name}" eliminato`);
  await render();
}

// ── Modal ──
function openModal(id=null) {
  editId=id;
  sMFC.setOpts(buildCatModalOpts(), false);

  if (id) {
    const tx=STATE.txs.find(t=>t.id===id); if(!tx)return;
    document.getElementById('modal-title').textContent='Modifica transazione';
    document.getElementById('f-amount').value=Math.abs(tx.amount);
    document.getElementById('f-desc').value=tx.description;
    // restore cat/project selection
    if (tx.project_id) sMFC.value='proj:'+tx.project_id;
    else sMFC.value=tx.category?'cat:'+tx.category:'';
    sMFM.value=tx.method;
    document.getElementById('f-date').value=tx.date;
    document.getElementById('f-isbal').checked=!!tx.is_balance;
    document.getElementById('btn-del-tx').style.display='block';
    setSign(tx.amount>=0?1:-1);
  } else {
    document.getElementById('modal-title').textContent='Nuova transazione';
    document.getElementById('f-amount').value='';
    document.getElementById('f-desc').value='';
    sMFC.value=''; sMFM.value='Conto';
    document.getElementById('f-isbal').checked=false;
    document.getElementById('btn-del-tx').style.display='none';
    const d=new Date();
    const day=(d.getFullYear()===curYear&&d.getMonth()+1===curMonth)?d.getDate():1;
    document.getElementById('f-date').value=`${curYear}-${String(curMonth).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
    setSign(-1);
  }
  document.getElementById('modal').style.display='flex';
  setTimeout(()=>document.getElementById('f-amount').focus(),80);
}
function closeModal(){document.getElementById('modal').style.display='none';}
function overlayClick(e){if(e.target.id==='modal')closeModal();}
function setSign(s){
  sign=s;
  document.getElementById('btn-neg').className='sign-btn'+(s===-1?' is-neg':'');
  document.getElementById('btn-pos').className='sign-btn'+(s===1?' is-pos':'');
}

async function saveTx() {
  const raw=parseFloat(document.getElementById('f-amount').value);
  if(isNaN(raw)||raw<0){alert('Importo non valido');return;}
  const date=document.getElementById('f-date').value;
  if(!date){alert('Inserisci una data');return;}

  // parse cat/project from unified select
  const selVal = sMFC.value || '';
  let category='', project_id=null;
  if (selVal.startsWith('proj:')) {
    project_id=parseInt(selVal.slice(5));
    // use project name as category label for display in tx table
    category='';
  } else if (selVal.startsWith('cat:')) {
    category=selVal.slice(4);
  }

  const payload={
    amount:sign*raw,
    description:document.getElementById('f-desc').value.trim(),
    category, project_id,
    method:sMFM.value,
    date,
    is_balance:document.getElementById('f-isbal').checked
  };
  if(editId) await api(`/api/txs/${editId}`,{method:'PUT',body:payload});
  else       await api('/api/txs',           {method:'POST',body:payload});
  closeModal(); await render();
}

async function deleteTx() {
  if(!editId||!confirm('Eliminare questa transazione?'))return;
  await api(`/api/txs/${editId}`,{method:'DELETE'});
  closeModal(); await render();
}

// ── Categories ──
async function updateThreshold(id,val) {
  const c=STATE.cats.find(c=>c.id===id);if(!c)return;
  await api(`/api/cats/${id}`,{method:'PUT',body:{...c,threshold:val?parseFloat(val):null}});
  await render();
}
async function addCategory() {
  const name=document.getElementById('new-cat-name').value.trim().toLowerCase();
  if(!name)return;
  if(STATE.cats.find(c=>c.name===name)){alert('Categoria già presente');return;}
  await api('/api/cats',{method:'POST',body:{name,grp:sNG.value}});
  document.getElementById('new-cat-name').value='';
  await render();
}
async function deleteCategory(id) {
  if(!confirm('Eliminare questa categoria?'))return;
  await api(`/api/cats/${id}`,{method:'DELETE'});
  await render();
}

// ── Navigation ──
async function changeMonth(delta) {
  curMonth+=delta;
  if(curMonth>12){curMonth=1;curYear++;}
  if(curMonth<1){curMonth=12;curYear--;}
  selectedIds.clear();
  if(document.getElementById('search-input'))document.getElementById('search-input').value='';
  if(sFC)sFC.value='';if(sFM)sFM.value='';if(sFT)sFT.value='';
  document.getElementById('rollover-preview').style.display='none';
  document.getElementById('rollover-actions').style.display='none';
  await render();
}
function switchTab(name,btn) {
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('pane-'+name).classList.add('active');
}

// ── Backup / Restore ──
async function exportBackup() {
  const data=await api('/api/backup');
  const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download=`ledger_backup_${curYear}_${String(curMonth).padStart(2,'0')}.json`;
  a.click();
}
async function importBackup(e) {
  const file=e.target.files[0];if(!file)return;
  const text=await file.text();
  try {
    const data=JSON.parse(text);
    if(!data.txs||!data.cats)throw 0;
    if(!confirm(`Importare ${data.txs.length} transazioni? I dati attuali saranno sovrascritti.`))return;
    await api('/api/restore',{method:'POST',body:data});
    sFC=sFM=sFT=sNG=sMFC=sMFM=null;
    showToast('Backup ripristinato');
    await render();
  } catch{alert('File non valido');}
  e.target.value='';
}

// ── Utils ──
const euroAbs=n=>'€'+Math.abs(n).toFixed(2).replace('.',',');
const euroSgn=n=>(n>=0?'+':'−')+'€'+Math.abs(n).toFixed(2).replace('.',',');
function showToast(msg,ms=2400) {
  const t=document.createElement('div');t.className='toast';t.textContent=msg;
  document.body.appendChild(t);setTimeout(()=>t.remove(),ms);
}

document.addEventListener('keydown',e=>{
  if(e.key==='Escape')closeModal();
  if(e.key==='n'&&!e.target.matches('input,select,textarea,[contenteditable]')
    &&document.getElementById('modal').style.display==='none') openModal();
});

render();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return HTML

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    url  = f"http://localhost:{port}"
    print(f"\n  ╔═════════════════════════════════════╗")
    print(f"  ║   Ledger  →  {url}  ║")
    print(f"  ╚═════════════════════════════════════╝\n")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=port, debug=False)