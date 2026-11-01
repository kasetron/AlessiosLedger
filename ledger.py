#!/usr/bin/env python3
"""
Ledger — personal finance tracker
Run: python ledger.py
Open: http://localhost:5001
"""
import sqlite3, os, re, json, uuid, calendar, webbrowser, threading
from datetime import date, datetime
from flask import Flask, g, jsonify, request

app = Flask(__name__)
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.db")
BACKUP_DIR = os.path.join(os.path.dirname(DB), "backups")
BACKUP_KEEP = 14
METHODS = ("Conto", "Paypal", "Contanti", "Libretto", "Risparmio")

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
        project_id  INTEGER REFERENCES projects(id),
        transfer_id TEXT,
        recurrence_id INTEGER
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
    CREATE TABLE IF NOT EXISTS recurrences (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        amount         REAL    NOT NULL,
        method         TEXT    NOT NULL,
        category       TEXT    NOT NULL DEFAULT '',
        description    TEXT    NOT NULL DEFAULT '',
        project_id     INTEGER REFERENCES projects(id),
        day            INTEGER NOT NULL,
        last_generated TEXT    NOT NULL,
        active         INTEGER NOT NULL DEFAULT 1
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
    if 'transfer_id' not in cols:
        db.execute("ALTER TABLE transactions ADD COLUMN transfer_id TEXT")
    if 'recurrence_id' not in cols:
        db.execute("ALTER TABLE transactions ADD COLUMN recurrence_id INTEGER")

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

# ── Backup automatico ─────────────────────────────────────────────────────────
_last_backup_day = None

def daily_backup():
    """Una copia consistente del DB al giorno in backups/, tenendo le ultime BACKUP_KEEP.
    Usa VACUUM INTO (snapshot corretto anche con il WAL), non una semplice copia del file."""
    global _last_backup_day
    today = date.today().isoformat()
    if _last_backup_day == today:
        return
    _last_backup_day = today
    try:
        if not os.path.exists(DB):
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dest = os.path.join(BACKUP_DIR, f"ledger-{today}.db")
        if not os.path.exists(dest):
            tmp = dest + ".tmp"
            if os.path.exists(tmp):
                os.remove(tmp)
            src = sqlite3.connect(DB)
            try:
                src.execute("VACUUM INTO ?", (tmp,))  # snapshot coerente in un solo file
            finally:
                src.close()
            os.replace(tmp, dest)  # il file finale compare solo a copia completata
        names = sorted(n for n in os.listdir(BACKUP_DIR) if n.startswith("ledger-") and n.endswith(".db"))
        for old in names[:-BACKUP_KEEP]:
            os.remove(os.path.join(BACKUP_DIR, old))
    except Exception as e:  # un backup fallito non deve mai bloccare l'app
        print(f"  ! backup automatico fallito: {e}")

@app.before_request
def _backup_once_a_day():
    daily_backup()

# ── Helpers ───────────────────────────────────────────────────────────────────
def row_to_dict(row): return dict(row) if row else None
def rows_to_list(rows): return [dict(r) for r in rows]

# ── Auto rollover ─────────────────────────────────────────────────────────────
def compute_rollover(db, year, month, skip_zero):
    """Scrive in `carryovers` il riporto di (year, month) calcolato dal mese precedente."""
    prev_m, prev_y = (month-1, year) if month > 1 else (12, year-1)
    rows = db.execute(
        "SELECT category, SUM(amount) as s FROM transactions "
        "WHERE strftime('%Y',date)=? AND strftime('%m',date)=? "
        "AND is_balance=0 AND category!='' AND method!='Risparmio' "
        "AND project_id IS NULL AND transfer_id IS NULL "
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
        if skip_zero and abs(unused) <= 0.01:
            continue
        db.execute(
            "INSERT OR REPLACE INTO carryovers(year,month,category,amount) VALUES(?,?,?,?)",
            (year, month, c["name"], unused)
        )
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"rollover_done_{year}_{month:02d}", "1"))
    db.commit()

def auto_rollover_if_needed(db, year, month):
    key = f"rollover_done_{year}_{month:02d}"
    if db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone():
        return
    prev_m, prev_y = (month-1, year) if month > 1 else (12, year-1)
    # Se il mese precedente è vuoto non c'è nulla da riportare: senza questo controllo
    # sfogliare un mese futuro/primo mese raddoppia i budget e "congela" il riporto.
    has_prev = db.execute(
        "SELECT 1 FROM transactions WHERE strftime('%Y',date)=? AND strftime('%m',date)=? LIMIT 1",
        (str(prev_y), f"{prev_m:02d}")
    ).fetchone()
    if not has_prev:
        return
    compute_rollover(db, year, month, skip_zero=True)

# ── Ricorrenti ────────────────────────────────────────────────────────────────
# Una ricorrenza (mensile) è un modello + `last_generated` (ultimo mese già creato). Le occorrenze
# scadute (data <= oggi) vengono create all'apertura dei mesi. Cancellare un'occorrenza non la
# ricrea, perché last_generated è già avanzato. I giorni 29-31 diventano l'ultimo giorno del mese.
def clip_day(y, m, day):
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))

def month_key(d):
    return d.strftime("%Y-%m")

def prev_month_key(d):
    return f"{d.year-1}-12" if d.month == 1 else f"{d.year}-{d.month-1:02d}"

def generate_recurrences(db, today=None):
    today = today or date.today()
    changed = False
    for r in db.execute("SELECT * FROM recurrences WHERE active=1").fetchall():
        y, m = map(int, r["last_generated"].split("-"))
        last = r["last_generated"]
        while True:
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            d = clip_day(y, m, r["day"])
            if d > today:
                break
            db.execute(
                "INSERT INTO transactions(amount,method,category,date,description,is_balance,project_id,recurrence_id) "
                "VALUES(?,?,?,?,?,0,?,?)",
                (r["amount"], r["method"], r["category"], d.isoformat(), r["description"], r["project_id"], r["id"]))
            last = f"{y}-{m:02d}"
        if last != r["last_generated"]:
            db.execute("UPDATE recurrences SET last_generated=? WHERE id=?", (last, r["id"]))
            changed = True
    if changed:
        db.commit()

def make_recurrence(db, d, today=None):
    """Crea la ricorrenza a partire da una transazione (dict con amount, method, category, date,
    description, project_id) e ne restituisce l'id. Niente retroattività: se la data è nel passato
    le occorrenze già trascorse non vengono ricreate, ci si limita a quelle future."""
    today = today or date.today()
    start = date.fromisoformat(d["date"])
    if start >= today:
        last = month_key(start)
    else:
        due_now = clip_day(today.year, today.month, start.day) <= today
        last = max(month_key(today) if due_now else prev_month_key(today), month_key(start))
    cur = db.execute(
        "INSERT INTO recurrences(amount,method,category,description,project_id,day,last_generated) VALUES(?,?,?,?,?,?,?)",
        (d["amount"], d["method"], d.get("category", ""), d.get("description", ""), d.get("project_id"), start.day, last))
    return cur.lastrowid

# ── API: Transactions ─────────────────────────────────────────────────────────
@app.route("/api/txs")
def get_txs():
    year  = request.args.get("year",  type=int)
    month = request.args.get("month", type=int)
    db = get_db()
    generate_recurrences(db)
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
    rid = make_recurrence(db, d) if d.get("repeat") == "monthly" and not d.get("is_balance") else None
    cur = db.execute(
        "INSERT INTO transactions(amount,method,category,date,description,is_balance,project_id,recurrence_id) VALUES(?,?,?,?,?,?,?,?)",
        (d["amount"], d["method"], d.get("category",""), d["date"],
         d.get("description",""), int(d.get("is_balance", False)), d.get("project_id"), rid)
    )
    db.commit()
    return jsonify(row_to_dict(db.execute("SELECT * FROM transactions WHERE id=?", (cur.lastrowid,)).fetchone())), 201

@app.route("/api/txs/<int:tid>", methods=["PUT"])
def update_tx(tid):
    d = request.json
    db = get_db()
    row = db.execute("SELECT transfer_id, recurrence_id, date FROM transactions WHERE id=?", (tid,)).fetchone()
    if row and row["transfer_id"]:
        return jsonify({"error": "usa /api/transfers per modificare un trasferimento"}), 400
    db.execute(
        "UPDATE transactions SET amount=?,method=?,category=?,date=?,description=?,is_balance=?,project_id=? WHERE id=?",
        (d["amount"], d["method"], d.get("category",""), d["date"],
         d.get("description",""), int(d.get("is_balance", False)), d.get("project_id"), tid)
    )
    # repeat: '' = solo questa · 'monthly' = rendila ricorrente · 'following' = questa e le prossime
    #         della serie · 'stop' = interrompi la serie (le occorrenze già create restano)
    mode, rid = d.get("repeat", ""), row["recurrence_id"] if row else None
    if rid and mode == "following":
        db.execute("UPDATE recurrences SET amount=?,method=?,category=?,description=?,project_id=?,day=? WHERE id=?",
            (d["amount"], d["method"], d.get("category",""), d.get("description",""), d.get("project_id"),
             int(d["date"][8:10]), rid))
        for t in db.execute("SELECT id, date FROM transactions WHERE recurrence_id=? AND date>?", (rid, row["date"])).fetchall():
            y, m = int(t["date"][:4]), int(t["date"][5:7])
            db.execute("UPDATE transactions SET amount=?,method=?,category=?,description=?,project_id=?,date=? WHERE id=?",
                (d["amount"], d["method"], d.get("category",""), d.get("description",""), d.get("project_id"),
                 clip_day(y, m, int(d["date"][8:10])).isoformat(), t["id"]))
    elif rid and mode == "stop":
        db.execute("UPDATE recurrences SET active=0 WHERE id=?", (rid,))
    elif not rid and mode == "monthly" and not d.get("is_balance"):
        db.execute("UPDATE transactions SET recurrence_id=? WHERE id=?", (make_recurrence(db, d), tid))
    db.commit()
    return jsonify(row_to_dict(db.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone()))

def delete_tx_and_pair(db, tid):
    """Elimina la transazione; se è metà di un trasferimento elimina anche l'altra metà."""
    row = db.execute("SELECT transfer_id FROM transactions WHERE id=?", (tid,)).fetchone()
    if row and row["transfer_id"]:
        db.execute("DELETE FROM transactions WHERE transfer_id=?", (row["transfer_id"],))
    else:
        db.execute("DELETE FROM transactions WHERE id=?", (tid,))

@app.route("/api/txs/<int:tid>", methods=["DELETE"])
def delete_tx(tid):
    db = get_db()
    if request.args.get("series") == "1":
        # elimina anche le occorrenze successive e interrompe la serie
        row = db.execute("SELECT recurrence_id, date FROM transactions WHERE id=?", (tid,)).fetchone()
        if row and row["recurrence_id"]:
            db.execute("DELETE FROM transactions WHERE recurrence_id=? AND date>?", (row["recurrence_id"], row["date"]))
            db.execute("UPDATE recurrences SET active=0 WHERE id=?", (row["recurrence_id"],))
    delete_tx_and_pair(db, tid)
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/txs/bulk-delete", methods=["POST"])
def bulk_delete():
    ids = request.json.get("ids", [])
    db = get_db()
    for i in ids:
        delete_tx_and_pair(db, i)
    db.commit()
    return jsonify({"deleted": len(ids)})

# ── API: Transfers ────────────────────────────────────────────────────────────
# Un trasferimento tra metodi = due transazioni (−importo su "from", +importo su "to")
# legate dallo stesso transfer_id. Non sono entrate né uscite: toccano solo i saldi.
def parse_transfer(d):
    try:
        amount = round(float(d["amount"]), 2)
    except (KeyError, TypeError, ValueError):
        return None
    src, dst, day = d.get("from"), d.get("to"), d.get("date")
    if amount <= 0 or src not in METHODS or dst not in METHODS or src == dst or not day:
        return None
    return amount, src, dst, day, d.get("description", "")

def insert_transfer(db, transfer_id, p):
    amount, src, dst, day, desc = p
    db.executemany(
        "INSERT INTO transactions(amount,method,category,date,description,is_balance,transfer_id) VALUES(?,?,?,?,?,0,?)",
        [(-amount, src, "", day, desc, transfer_id), (amount, dst, "", day, desc, transfer_id)]
    )

@app.route("/api/transfers", methods=["POST"])
def add_transfer():
    p = parse_transfer(request.json or {})
    if not p:
        return jsonify({"error": "trasferimento non valido"}), 400
    db = get_db()
    transfer_id = uuid.uuid4().hex
    insert_transfer(db, transfer_id, p)
    db.commit()
    return jsonify({"transfer_id": transfer_id}), 201

@app.route("/api/transfers/<transfer_id>", methods=["PUT"])
def update_transfer(transfer_id):
    p = parse_transfer(request.json or {})
    if not p:
        return jsonify({"error": "trasferimento non valido"}), 400
    db = get_db()
    if not db.execute("SELECT 1 FROM transactions WHERE transfer_id=?", (transfer_id,)).fetchone():
        return jsonify({"error": "trasferimento non trovato"}), 404
    db.execute("DELETE FROM transactions WHERE transfer_id=?", (transfer_id,))
    insert_transfer(db, transfer_id, p)
    db.commit()
    return jsonify({"transfer_id": transfer_id})

@app.route("/api/transfers/<transfer_id>", methods=["DELETE"])
def delete_transfer(transfer_id):
    db = get_db()
    db.execute("DELETE FROM transactions WHERE transfer_id=?", (transfer_id,))
    db.commit()
    return jsonify({"ok": True})

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
    db = get_db()
    compute_rollover(db, year, month, skip_zero=False)
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
    db.execute("UPDATE recurrences SET project_id=NULL WHERE project_id=?", (pid,))
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/projects/<int:pid>/spending")
def project_spending(pid):
    db = get_db()
    # Spesa NETTA: i rimborsi/entrate del progetto (amount>0) riducono il totale speso.
    row = db.execute(
        "SELECT COALESCE(SUM(amount),0) as total FROM transactions WHERE project_id=? AND is_balance=0",
        (pid,)
    ).fetchone()
    txs = db.execute(
        "SELECT * FROM transactions WHERE project_id=? ORDER BY date DESC, id DESC",
        (pid,)
    ).fetchall()
    return jsonify({"spent": round(-row["total"], 2), "transactions": rows_to_list(txs)})

# ── API: Analytics ────────────────────────────────────────────────────────────
@app.route("/api/analytics")
def analytics():
    """Aggregati mensili per la tab Analisi, tra i mesi start e end (formato YYYY-MM).
    - spend: spesa NETTA per (mese, categoria, metodo); esclude saldi iniziali, trasferimenti,
      spese di progetto (che hanno un budget a parte) ed entrate ('guadagni'); le voci senza
      categoria contano solo se sono uscite.
    - projects: spese nette dei progetti per (mese, metodo), tenute separate.
    - balances: saldo di fine mese per metodo (tutto incluso: saldi iniziali, trasferimenti, progetti)."""
    start, end = request.args.get("start", ""), request.args.get("end", "")
    if not (re.fullmatch(r"\d{4}-\d{2}", start) and re.fullmatch(r"\d{4}-\d{2}", end)) or start > end:
        return jsonify({"error": "intervallo non valido"}), 400
    db = get_db()
    spend = db.execute(
        "SELECT substr(date,1,7) AS month, category, method, ROUND(-SUM(amount),2) AS amount "
        "FROM transactions WHERE substr(date,1,7) BETWEEN ? AND ? "
        "AND is_balance=0 AND transfer_id IS NULL AND project_id IS NULL "
        "AND category!='guadagni' AND (category!='' OR amount<0) "
        "GROUP BY month, category, method", (start, end)).fetchall()
    projects = db.execute(
        "SELECT substr(date,1,7) AS month, method, ROUND(-SUM(amount),2) AS amount "
        "FROM transactions WHERE substr(date,1,7) BETWEEN ? AND ? "
        "AND is_balance=0 AND project_id IS NOT NULL "
        "GROUP BY month, method", (start, end)).fetchall()
    running = {r["method"]: r["s"] for r in db.execute(
        "SELECT method, SUM(amount) AS s FROM transactions WHERE substr(date,1,7) < ? GROUP BY method", (start,))}
    per_month = {}
    for r in db.execute(
            "SELECT substr(date,1,7) AS month, method, SUM(amount) AS s FROM transactions "
            "WHERE substr(date,1,7) BETWEEN ? AND ? GROUP BY month, method", (start, end)):
        per_month.setdefault(r["month"], {})[r["method"]] = r["s"]
    balances, y, m = [], int(start[:4]), int(start[5:])
    while f"{y}-{m:02d}" <= end:
        key = f"{y}-{m:02d}"
        for meth, v in per_month.get(key, {}).items():
            running[meth] = running.get(meth, 0.0) + v
        balances.append({"month": key, "balances": {k: round(v, 2) for k, v in running.items()}})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return jsonify({"spend": rows_to_list(spend), "projects": rows_to_list(projects), "balances": balances})

# ── Backup / Restore ──────────────────────────────────────────────────────────
@app.route("/api/backup")
def backup():
    db = get_db()
    return jsonify({
        "txs":        rows_to_list(db.execute("SELECT * FROM transactions ORDER BY id").fetchall()),
        "cats":       rows_to_list(db.execute("SELECT * FROM categories ORDER BY sort_order").fetchall()),
        "carryovers": rows_to_list(db.execute("SELECT * FROM carryovers").fetchall()),
        "projects":   rows_to_list(db.execute("SELECT * FROM projects ORDER BY id").fetchall()),
        "recurrences": rows_to_list(db.execute("SELECT * FROM recurrences ORDER BY id").fetchall()),
    })

@app.route("/api/restore", methods=["POST"])
def restore():
    data = request.json
    db = get_db()
    db.execute("DELETE FROM transactions")
    db.execute("DELETE FROM recurrences")
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
    for r in data.get("recurrences", []):
        db.execute("INSERT INTO recurrences(id,amount,method,category,description,project_id,day,last_generated,active) VALUES(?,?,?,?,?,?,?,?,?)",
            (r["id"],r["amount"],r["method"],r.get("category",""),r.get("description",""),r.get("project_id"),r["day"],r["last_generated"],r.get("active",1)))
    for t in data.get("txs", []):
        db.execute("INSERT INTO transactions(id,amount,method,category,date,description,is_balance,project_id,transfer_id,recurrence_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (t["id"],t["amount"],t["method"],t.get("category",""),t["date"],t.get("description",""),int(t.get("is_balance",0)),t.get("project_id"),t.get("transfer_id"),t.get("recurrence_id")))
    for co in data.get("carryovers", []):
        db.execute("INSERT INTO carryovers(year,month,category,amount) VALUES(?,?,?,?)",
            (co["year"],co["month"],co["category"],co["amount"]))
        # il backup non contiene `meta`: segna i mesi già riportati, altrimenti
        # l'auto-rollover li ricalcolerebbe sovrascrivendo i riporti ripristinati
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
            (f"rollover_done_{co['year']}_{co['month']:02d}", "1"))
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
.pill-Conto{background:#1a3a5520;color:var(--blue)}.pill-Paypal{background:var(--gold-dim);color:var(--gold)}.pill-Contanti{background:var(--green-dim);color:var(--green)}.pill-Libretto{background:#3d2a5020;color:var(--purple)}.pill-Risparmio{background:#e0a06020;color:var(--sav)}.pill-project{background:#3d2a5020;color:var(--purple)}.pill-transfer{background:var(--gold-dim);color:var(--gold)}
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
.field-row{display:flex;gap:10px}.field-row .field{flex:1;min-width:0}
.field input:focus{border-color:var(--gold)}.field input::placeholder{color:var(--muted)}
.sign-row{display:flex;gap:8px}
.sign-btns{display:flex;background:var(--surface2);border:1px solid var(--border);border-radius:8px;overflow:hidden}
.sign-btn{background:none;border:none;color:var(--muted);font-size:18px;font-weight:700;width:44px;cursor:pointer;transition:all .15s;font-family:monospace}
.sign-btn.is-neg{background:var(--red-dim);color:var(--red)}.sign-btn.is-pos{background:var(--green-dim);color:var(--green)}.sign-btn.is-xfer{background:var(--gold-dim);color:var(--gold)}
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

/* analisi */
.balance-card.click{cursor:pointer}.balance-card.click:hover{border-color:var(--gold)}
.an-filters{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
.seg{display:inline-flex;border:1px solid var(--border);border-radius:8px;overflow:hidden;background:var(--surface)}
.seg button{background:none;border:none;color:var(--muted);padding:7px 11px;font-size:12px;font-family:'DM Sans',sans-serif;cursor:pointer;transition:all .15s}
.seg button+button{border-left:1px solid var(--border)}
.seg button:hover{color:var(--text);background:var(--surface2)}
.seg button.on{background:var(--gold-dim);color:var(--gold)}
.an-cmp{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
#an-body{transition:opacity .15s}
.an-tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
@media(max-width:600px){.an-tiles{grid-template-columns:1fr}}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:14px 16px}
.tile-l{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);font-family:'DM Mono',monospace;margin-bottom:6px}
.tile-v{font-size:24px;font-weight:600;letter-spacing:-.5px;line-height:1.2}
.tile-d{font-size:12px;margin-top:4px;color:var(--muted2)}
.dl{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted2)}.dl.up{color:var(--red)}.dl.down{color:var(--green)}
.lg-row{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted2);margin-bottom:10px}
.lg{display:inline-flex;align-items:center;gap:6px}.lg i{width:10px;height:10px;border-radius:2px;display:inline-block}
.an-note{font-size:11px;color:var(--muted);margin-top:8px}
.an-svg{width:100%;height:auto;display:block;overflow:visible}
.an-svg text{font-family:'DM Mono',monospace;font-size:10px;fill:var(--muted)}
.an-svg .hit{fill:transparent;outline:none}
.an-svg .hit:hover,.an-svg .hit:focus{fill:rgba(240,235,224,.06)}
.cmp-row{display:grid;grid-template-columns:130px 1fr 64px 64px 104px;gap:10px;align-items:center;padding:7px 8px;border-bottom:1px solid var(--border);border-radius:6px;cursor:pointer;outline:none;transition:background .1s}
.cmp-row:hover,.cmp-row:focus{background:var(--surface2)}.cmp-row.sel{background:var(--gold-dim)}
.cmp-row.head{cursor:default;font-size:10px;color:var(--muted);font-family:'DM Mono',monospace;text-transform:uppercase;letter-spacing:.5px;padding-top:0}
.cmp-row.head:hover{background:none}
.cmp-row.head .cmp-num{font-size:10px;color:var(--muted)}
.cmp-row.total{cursor:default;border-bottom:none;font-weight:500}.cmp-row.total:hover{background:none}
.cmp-name{font-size:13px;color:var(--muted2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cmp-bars{display:flex;flex-direction:column;gap:2px}
.cmp-bar{height:8px;border-radius:0 4px 4px 0;min-width:2px}
.cmp-num{font-family:'DM Mono',monospace;font-size:12px;text-align:right}.cmp-num.b{color:var(--muted2)}
@media(max-width:600px){.cmp-row{grid-template-columns:84px 1fr 54px 84px;gap:8px}.cmp-num{font-size:11px}.cmp-num.b{display:none}
  .an-cmp{width:100%}.an-cmp>div{flex:1;min-width:0;width:auto!important}}
.an-scroll{overflow-x:auto}
.an-table{width:100%;border-collapse:collapse;font-family:'DM Mono',monospace;font-size:12px}
.an-table th{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);font-weight:500;padding:8px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}
.an-table th:first-child,.an-table td:first-child{text-align:left;white-space:nowrap}
.an-table td:first-child{font-family:'DM Sans',sans-serif;font-size:13px;color:var(--muted2)}
.an-table td{padding:7px 8px;border-bottom:1px solid var(--border);text-align:right}
.an-table td.z{color:var(--muted)}.an-table tr.foot td{font-weight:500;color:var(--text)}.an-table tr.sep td{border-top:1px solid var(--border2);color:var(--purple)}
.an-tip{position:fixed;display:none;z-index:300;pointer-events:none;background:var(--surface2);border:1px solid var(--border2);border-radius:9px;padding:9px 12px;box-shadow:0 8px 24px rgba(0,0,0,.55);font-size:12px;min-width:150px;max-width:260px}
.an-tip-t{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);font-family:'DM Mono',monospace;margin-bottom:6px}
.an-tip-r{display:flex;align-items:center;gap:8px;padding:2px 0}
.an-tip-r i{width:10px;height:3px;border-radius:2px;flex-shrink:0}.an-tip-r i.none{background:transparent}
.an-tip-r b{font-family:'DM Mono',monospace;font-weight:500;color:var(--text);margin-left:auto;padding-left:12px}
.an-tip-r span{color:var(--muted2)}
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
    <button class="tab-btn" id="tab-analysis" onclick="switchTab('analysis',this)">Analisi</button>
    <button class="tab-btn" onclick="switchTab('categories',this)">Categorie</button>
    <button class="tab-btn" onclick="switchTab('projects',this)">Progetti</button>
  </div>

  <!-- Panoramica -->
  <div class="tab-pane active" id="pane-overview">
    <div class="card">
      <div class="card-title" style="margin-bottom:14px">
        Riepilogo mensile
        <span style="color:var(--muted);font-size:9px;margin-left:8px;font-family:'DM Mono',monospace">saldi iniziali e trasferimenti esclusi</span>
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

  <!-- Analisi -->
  <div class="tab-pane" id="pane-analysis">
    <div class="an-filters">
      <div class="seg" id="seg-range"></div>
      <div id="csel-amethod" style="width:140px"></div>
      <div id="csel-acat" style="width:170px"></div>
      <div class="an-cmp"><span>Confronta</span><div id="csel-aa" style="width:152px"></div><span>con</span><div id="csel-ab" style="width:152px"></div></div>
      <div class="seg" id="seg-sort"></div>
      <div class="seg" id="seg-view"></div>
    </div>
    <div id="an-body"></div>
    <div class="an-tip" id="an-tip"></div>
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
          <button class="sign-btn" id="btn-xfer" onclick="setSign(0)" type="button" title="Trasferimento tra metodi">⇄</button>
        </div>
        <input type="number" id="f-amount" placeholder="0.00" step="0.01" min="0" style="flex:1">
      </div>
    </div>
    <div class="field"><label>Descrizione</label><input type="text" id="f-desc" placeholder="es. spesa coop, lezione guida…"></div>
    <div class="field" id="field-cat"><label>Categoria / Progetto</label><div id="csel-fcat-m"></div></div>
    <div class="field"><label id="lbl-method">Metodo di pagamento</label><div id="csel-fmethod-m"></div></div>
    <div class="field" id="field-to" style="display:none"><label>A</label><div id="csel-fmethod-to"></div></div>
    <div class="field-row">
      <div class="field"><label>Data</label><input type="date" id="f-date"></div>
      <div class="field" id="field-repeat"><label id="lbl-repeat">Ripeti</label><div id="csel-repeat"></div></div>
    </div>
    <div class="toggle-row" id="row-isbal">
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
let editId=null, editTid=null, sign=-1, sortCol='date', sortDir='desc';
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

// Stesso menu del dialog, due scelte indipendenti: una categoria (radio) e, in fondo,
// un progetto opzionale (toggle). Valore: {cat:string, proj:number|null}
class CSMix extends CS {
  setOpts(opts, keepVal=false) {
    this.opts=opts;
    if(!keepVal) this.val={cat:'',proj:null};
    this._render();
  }
  _label() {
    const parts=[];
    if(this.val.cat) parts.push(this.val.cat);
    if(this.val.proj){const p=STATE.projects.find(p=>p.id===this.val.proj); parts.push('▸ '+(p?p.name:'progetto #'+this.val.proj));}
    return parts.join(' · ');
  }
  _render() {
    const lbl=this._label();
    this.btn.textContent=lbl||'— nessuna —';
    this.btn.style.color=lbl?'var(--text)':'var(--muted)';
    this.ul.innerHTML='';
    this.opts.forEach(o=>{
      const li=document.createElement('li');
      if(o.header){li.className='csel-item csel-group-header';li.textContent=o.header;this.ul.appendChild(li);return;}
      const sel=o.value===''?false:o.value.startsWith('proj:')?this.val.proj===parseInt(o.value.slice(5)):this.val.cat===o.value.slice(4);
      li.className='csel-item'+(o.projItem?' csel-proj-item':'')+(sel?' csel-sel':'')+(o.value===''?' csel-ph':'');
      li.textContent=o.label;
      li.addEventListener('click', e=>{
        e.stopPropagation();
        const prevCat=this.val.cat;
        if(o.value==='') this.val={cat:'',proj:null};
        else if(o.value.startsWith('proj:')){const id=parseInt(o.value.slice(5)); this.val={...this.val,proj:this.val.proj===id?null:id};}
        else {const c=o.value.slice(4); this.val={...this.val,cat:this.val.cat===c?'':c};}
        this._render(); this.close_();
        this.onCh&&this.onCh(this.val, this.val.cat!==prevCat);
      });
      this.ul.appendChild(li);
    });
  }
}

let sFC, sFM, sFT, sNG, sMFC, sMFM, sMTo, sRep, sAM, sAC, sAA, sAB;

function buildCatModalOpts(keepProjId=null) {
  // progetti attivi + (in modifica) quello già assegnato, anche se archiviato
  const activeProjects = STATE.projects.filter(p=>!p.archived||p.id===keepProjId);
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
  const tOpts   = [{value:'',label:'Tutto'},{value:'out',label:'Uscite'},{value:'in',label:'Entrate'},{value:'bal',label:'Saldi'},{value:'xfer',label:'Trasf.'}];
  const gOpts   = [{value:'NECESSARIE',label:'Necessarie'},{value:'AGGIUNTIVE',label:'Aggiuntive'}];

  if (!sFC) {
    sFC  = new CS('csel-fcat',    catOpts,     '', ()=>renderTxTable(), true);
    sFM  = new CS('csel-fmethod', mOpts,       '', ()=>renderTxTable(), true);
    sFT  = new CS('csel-ftype',   tOpts,       '', ()=>renderTxTable(), true);
    sNG  = new CS('csel-newgroup',gOpts, 'NECESSARIE', null, true);
    sMFC = new CSMix('csel-fcat-m', buildCatModalOpts(), {cat:'',proj:null}, (v,catChanged)=>{if(catChanged&&v.cat==='guadagni')setSign(1);});
    sMFM = new CS('csel-fmethod-m', METHODS.map(m=>({value:m,label:m})), 'Conto', null);
    sMTo = new CS('csel-fmethod-to', METHODS.map(m=>({value:m,label:m})), 'Contanti', null);
    sRep = new CS('csel-repeat', [{value:'none',label:'Mai'},{value:'monthly',label:'Ogni mese'}], 'none', null);
    sAM  = new CS('csel-amethod', [{value:'',label:'Tutti i metodi'},...METHODS.map(m=>({value:m,label:m}))], '', v=>{AN.method=v;renderAnalysis();}, true);
    sAC  = new CS('csel-acat', [{value:'',label:'Tutte le categorie'}], '', v=>{AN.cat=v;renderAnalysis();}, true);
    sAA  = new CS('csel-aa', [], '', v=>{AN.a=v;renderAnalysis();}, true);
    sAB  = new CS('csel-ab', [], '', v=>{AN.b=v;renderAnalysis();}, true);
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
  if(anActive()) await loadAnalysis();
}

// ── Balances ──
function renderBalances() {
  const b=STATE.balances;
  const avail=METHODS.filter(m=>m!=='Risparmio').reduce((a,m)=>a+(b[m]||0),0);
  document.getElementById('balance-strip').innerHTML=
    METHODS.map(m=>`<div class="balance-card click bc-${m}" onclick="openAnalysis('${m}')" title="Apri l'analisi di ${m}">
      <div class="b-label">${m}</div>
      <div class="b-value ${(b[m]||0)<0?'b-neg':''}">${euroAbs(b[m]||0)}</div>
    </div>`).join('')+
    `<div class="balance-card click bc-total" onclick="openAnalysis('')" title="Apri l'analisi">
      <div class="b-label">Disponibile</div>
      <div class="b-value ${avail<0?'b-neg':''}">${euroAbs(avail)}</div>
    </div>`;
}

// Lista per la UI: ogni trasferimento (coppia di movimenti con lo stesso transfer_id)
// diventa UNA riga {isTransfer, from, to, amount>0}; il resto resta invariato e in ordine.
function mergedTxs() {
  const legs={};
  STATE.txs.forEach(t=>{ if(t.transfer_id) (legs[t.transfer_id]=legs[t.transfer_id]||[]).push(t); });
  const seen=new Set(), rows=[];
  STATE.txs.forEach(t=>{
    if(!t.transfer_id){rows.push(t);return;}
    if(seen.has(t.transfer_id))return;
    seen.add(t.transfer_id);
    const out=legs[t.transfer_id].find(x=>x.amount<0), inn=legs[t.transfer_id].find(x=>x.amount>0);
    if(out&&inn) rows.push({...out, isTransfer:true, from:out.method, to:inn.method, amount:inn.amount});
    else rows.push(...legs[t.transfer_id]);  // metà orfana: resta visibile come riga normale
  });
  return rows;
}

// Spesa netta per categoria del mese corrente (rimborsi in categoria sottraggono),
// coerente con il calcolo del riporto. Esclude saldi, Risparmio e transazioni di progetto.
function catSpend() {
  const spend={};
  STATE.txs.filter(t=>!t.is_balance&&t.category&&t.method!=='Risparmio'&&!t.project_id).forEach(t=>{
    spend[t.category]=(spend[t.category]||0)-t.amount;
  });
  return spend;
}

// Escape per tutto ciò che è testo utente inserito via innerHTML
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// "YYYY-MM-DD" → "DD/MM[/YYYY]" senza passare da Date (niente slittamenti di fuso)
const fmtDate=(s,withYear=false)=>{const [y,m,d]=s.split('-');return `${d}/${m}${withYear?'/'+y:''}`;};

// ── Overview ──
function renderOverview() {
  const real=STATE.txs.filter(t=>!t.is_balance&&!t.transfer_id);
  // i trasferimenti non sono entrate/uscite, ma quelli verso/da Risparmio contano come risparmio
  const savX=mergedTxs().filter(t=>t.isTransfer).reduce((a,t)=>a+(t.to==='Risparmio'?t.amount:0)-(t.from==='Risparmio'?t.amount:0),0);
  const sav =real.filter(t=>t.method==='Risparmio'&&t.amount<0).reduce((a,t)=>a+Math.abs(t.amount),0)+savX;
  const inc =real.filter(t=>t.amount>0&&t.method!=='Risparmio').reduce((a,t)=>a+t.amount,0);
  const exp =real.filter(t=>t.amount<0&&t.method!=='Risparmio').reduce((a,t)=>a+t.amount,0);
  const net =inc+exp;
  document.getElementById('ni-in').textContent  = euroAbs(inc);
  document.getElementById('ni-out').textContent = '−'+euroAbs(Math.abs(exp));
  document.getElementById('ni-sav').textContent = sav>0?euroAbs(sav):'—';
  const nel=document.getElementById('ni-net');
  nel.textContent=euroSgn(net); nel.style.color=net>=0?'var(--green)':'var(--red)';

  renderTrend();

  const spend=catSpend();
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
        <div class="cat-name">${esc(c.name)}</div>
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
        return `<div class="alert-row"><span>${esc(c.name)}</span>
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
  const spend=catSpend();
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
        <span>${a.level==='danger'?'🔴':'🟡'} <strong>${esc(a.name)}</strong></span>
        <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <span class="trend-stat">ritmo ${(a.pace*100).toFixed(0)}% — proiezione ${euroAbs(proj)} / €${a.eff.toFixed(0)}</span>
          <span class="badge ${a.level==='danger'?'over':'warn'}">${a.level==='danger'?'ACCELERA':'ATTENZIONE'}</span>
        </div></div>`;
    }).join('')}</div>`;
}

// ── Tx table ──
function txCatLabel(t) {
  if (t.isTransfer) return '<span class="pill pill-transfer">⇄ trasferimento</span>';
  const cat=t.category?`<span class="pill">${esc(t.category)}</span>`:'';
  let proj='';
  if (t.project_id) {
    const p=STATE.projects.find(p=>p.id===t.project_id);
    proj=`<span class="pill pill-project">${p?'▸ '+esc(p.name):'progetto #'+t.project_id}</span>`;
  }
  return cat||proj ? cat+(cat&&proj?' ':'')+proj : '<span style="color:var(--muted)">—</span>';
}

function visibleTxs() {
  const search=(document.getElementById('search-input')?.value||'').toLowerCase();
  const catF=sFC?.value||'', methF=sFM?.value||'', typeF=sFT?.value||'';

  return mergedTxs().filter(t=>{
    if(search&&!t.description.toLowerCase().includes(search)
      &&!(t.isTransfer&&'trasferimento'.includes(search))
      &&!(t.category||'').toLowerCase().includes(search)
      &&!(STATE.projects.find(p=>p.id===t.project_id)?.name||'').toLowerCase().includes(search)) return false;
    if(catF) {
      if(t.isTransfer) return false;
      if(catF.startsWith('cat:') && t.category!==(catF.slice(4))) return false;
      if(catF.startsWith('proj:') && t.project_id!==parseInt(catF.slice(5))) return false;
    }
    if(methF&&t.method!==methF&&!(t.isTransfer&&t.to===methF)) return false;
    if(typeF==='out'&&(t.amount>=0||t.is_balance||t.isTransfer)) return false;
    if(typeF==='in'&&(t.amount<=0||t.is_balance||t.isTransfer)) return false;
    if(typeF==='bal'&&!t.is_balance) return false;
    if(typeF==='xfer'&&!t.isTransfer) return false;
    return true;
  });
}

function renderTxTable() {
  let rows=visibleTxs();

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
    const ds=fmtDate(t.date);
    const amtCls=t.is_balance||t.isTransfer?'amt-neu':t.amount>=0?'amt-pos':'amt-neg';
    const sel=selectedIds.has(t.id);
    return `<tr class="${t.is_balance?'tx-balance':''} ${sel?'tx-selected':''}" onclick="rowClick(event,${t.id})">
      <td class="td-check" onclick="event.stopPropagation()">
        <input type="checkbox" ${sel?'checked':''} onchange="toggleSel(${t.id},this.checked)">
      </td>
      <td><span class="tx-date">${ds}</span></td>
      <td>${esc(t.description)||'—'}${t.recurrence_id?'<span title="Ricorrente" style="font-size:11px;color:var(--muted);margin-left:6px">↻</span>':''}${t.is_balance?'<span style="font-size:10px;color:var(--muted);margin-left:6px">↩</span>':''}</td>
      <td class="hide-sm">${txCatLabel(t)}</td>
      <td class="hide-sm">${t.isTransfer?`<span class="pill pill-${t.from}">${t.from}</span> → <span class="pill pill-${t.to}">${t.to}</span>`:`<span class="pill pill-${t.method}">${t.method}</span>`}</td>
      <td><span class="tx-amt ${amtCls}">${t.isTransfer?euroAbs(t.amount):euroSgn(t.amount)}</span></td>
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
function toggleAll(cb){visibleTxs().forEach(t=>cb.checked?selectedIds.add(t.id):selectedIds.delete(t.id));renderTxTable();}
async function bulkDelete(){
  if(!selectedIds.size||!confirm(`Eliminare ${selectedIds.size} transazioni?`))return;
  await api('/api/txs/bulk-delete',{method:'POST',body:{ids:[...selectedIds]}});
  selectedIds.clear();showToast('Transazioni eliminate');await render();
}

// ── Cat list ──
function renderCatList() {
  const spend=catSpend();
  const co=STATE.coMap;
  document.getElementById('cat-list').innerHTML=['NECESSARIE','AGGIUNTIVE'].map(g=>{
    return `<div class="group-label">${g}</div>`+
      STATE.cats.filter(c=>c.grp===g).map(c=>{
        const hint=co[c.name]>0?` <span style="color:var(--amber);font-size:10px">(+${co[c.name].toFixed(0)} riportato)</span>`
                  :co[c.name]<0?` <span style="color:var(--red);font-size:10px">(${co[c.name].toFixed(0)} malus)</span>`:'';
        return `<div class="cat-edit-row">
          <div class="cat-edit-name">${esc(c.name)}${hint}</div>
          <div class="cat-spent-lbl">${spend[c.name]>0?euroAbs(spend[c.name]):''}</div>
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
      <span style="color:var(--muted2);font-size:13px">${esc(u.name)}</span>
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
      const rawPct=p.total_budget?s.spent/p.total_budget*100:0;  // non clampato: serve per soglie warn/over
      const pct=Math.max(0,Math.min(rawPct,100));
      const rem=p.total_budget!=null?p.total_budget-s.spent:null;
      // spent è la spesa netta: se negativa il progetto è in positivo (ottenuti, non speso)
      const spentLbl=(s.spent<0?'ottenuti ':'speso ')+euroAbs(s.spent);
      const cls=rawPct>100?'over':rawPct>80?'warn':'';
      const txHtml=s.transactions.map(t=>{
        const ds=fmtDate(t.date,true);
        return `<div class="proj-tx-item">
          <span class="proj-tx-date">${ds}</span>
          <span class="proj-tx-desc">${esc(t.description)||'—'}${t.category?` <span class="pill" style="font-size:10px;margin-left:4px">${esc(t.category)}</span>`:''}</span>
          <span class="proj-tx-amt" style="color:${t.is_balance?'var(--muted)':t.amount>=0?'var(--green)':'var(--red)'}">${euroSgn(t.amount)}</span>
        </div>`;
      }).join('')||'<div style="color:var(--muted);font-size:12px;padding:6px 0">Nessuna spesa ancora — seleziona questo progetto quando aggiungi una transazione</div>';

      return `<div class="proj-card">
        <div class="proj-header">
          <div>
            <div class="proj-name">${esc(p.name)}</div>
            ${p.description?`<div class="proj-desc">${esc(p.description)}</div>`:''}
            <div class="proj-meta">creato il ${p.created_at} · ${s.transactions.length} transazioni</div>
          </div>
          <div class="proj-actions">
            <button class="btn-secondary btn-sm" onclick="archiveProject(${p.id})">Archivia</button>
            <button class="btn-danger btn-sm" onclick="deleteProject(${p.id})">Elimina</button>
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
            <span>${spentLbl}</span>
            ${rem>=0
              ?`<span style="color:var(--green)">rimangono ${euroAbs(rem)}</span>`
              :`<span style="color:var(--red)">sforato di ${euroAbs(Math.abs(rem))}</span>`}
            <span>budget ${euroAbs(p.total_budget)}</span>
          </div>`
          :`<div style="font-family:'DM Mono',monospace;font-size:12px;color:var(--muted2)">${spentLbl} — nessun limite impostato</div>`}
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
          <div><div class="proj-name">${esc(p.name)}</div>${p.description?`<div class="proj-desc">${esc(p.description)}</div>`:''}</div>
          <div class="proj-actions">
            <button class="btn-secondary btn-sm" onclick="archiveProject(${p.id})">Ripristina</button>
            <button class="btn-danger btn-sm" onclick="deleteProject(${p.id})">Elimina</button>
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

async function archiveProject(id) {
  const p = STATE.projects.find(p=>p.id===id); if (!p) return;
  const newArchived = p.archived ? 0 : 1;
  const action = newArchived ? 'archiviare' : 'ripristinare';
  if (!confirm(`Vuoi ${action} il progetto "${p.name}"?`)) return;
  await api(`/api/projects/${id}`, {method:'PUT', body:{
    name:p.name, total_budget:p.total_budget, description:p.description, archived:newArchived
  }});
  showToast(newArchived ? `"${p.name}" archiviato` : `"${p.name}" ripristinato`);
  await render();
}

async function deleteProject(id) {
  const p = STATE.projects.find(p=>p.id===id); if (!p) return;
  const name = p.name;
  if (!confirm(`Eliminare definitivamente il progetto "${name}"?\nLe transazioni collegate rimarranno ma non saranno più associate al progetto.`)) return;
  await api(`/api/projects/${id}`, {method:'DELETE'});
  showToast(`"${name}" eliminato`);
  await render();
}

// ── Modal ──
function openModal(id=null) {
  editId=id; editTid=null;
  sMFC.setOpts(buildCatModalOpts(STATE.txs.find(t=>t.id===id)?.project_id??null), false);
  // "Ripeti": nuova/normale → Mai | Ogni mese · già ricorrente → a cosa applicare la modifica
  const recurring=!!STATE.txs.find(t=>t.id===id)?.recurrence_id;
  document.getElementById('lbl-repeat').textContent=recurring?'Serie ricorrente':'Ripeti';
  sRep.setOpts(recurring
    ?[{value:'none',label:'Solo questa'},{value:'following',label:'Questa e le prossime'},{value:'stop',label:'Interrompi la serie'}]
    :[{value:'none',label:'Mai'},{value:'monthly',label:'Ogni mese'}], false);

  if (id) {
    const tx=STATE.txs.find(t=>t.id===id); if(!tx)return;
    const xfer=tx.transfer_id?mergedTxs().find(r=>r.isTransfer&&r.transfer_id===tx.transfer_id):null;
    if(xfer) editTid=xfer.transfer_id;
    document.getElementById('modal-title').textContent=xfer?'Modifica trasferimento':'Modifica transazione';
    document.getElementById('f-amount').value=Math.abs(xfer?xfer.amount:tx.amount);
    document.getElementById('f-desc').value=tx.description;
    // restore cat/project selection
    sMFC.value={cat:tx.category||'', proj:tx.project_id||null};
    sMFM.value=xfer?xfer.from:tx.method;
    if(xfer) sMTo.value=xfer.to;
    document.getElementById('f-date').value=tx.date;
    document.getElementById('f-isbal').checked=!!tx.is_balance;
    document.getElementById('btn-del-tx').style.display='block';
    setSign(xfer?0:tx.amount>=0?1:-1);
  } else {
    document.getElementById('modal-title').textContent='Nuova transazione';
    document.getElementById('f-amount').value='';
    document.getElementById('f-desc').value='';
    sMFC.value={cat:'',proj:null}; sMFM.value='Conto'; sMTo.value='Contanti';
    document.getElementById('f-isbal').checked=false;
    document.getElementById('btn-del-tx').style.display='none';
    const d=new Date();
    const day=(d.getFullYear()===curYear&&d.getMonth()+1===curMonth)?d.getDate():1;
    document.getElementById('f-date').value=`${curYear}-${String(curMonth).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
    setSign(-1);
  }
  // un trasferimento esistente resta tale; una transazione esistente non diventa trasferimento
  document.getElementById('btn-neg').style.display=editTid?'none':'';
  document.getElementById('btn-pos').style.display=editTid?'none':'';
  document.getElementById('btn-xfer').style.display=(id&&!editTid)?'none':'';
  document.getElementById('modal').style.display='flex';
  setTimeout(()=>document.getElementById('f-amount').focus(),80);
}
function closeModal(){document.getElementById('modal').style.display='none';}
function overlayClick(e){if(e.target.id==='modal')closeModal();}
function setSign(s){
  sign=s;
  document.getElementById('btn-neg').className='sign-btn'+(s===-1?' is-neg':'');
  document.getElementById('btn-pos').className='sign-btn'+(s===1?' is-pos':'');
  document.getElementById('btn-xfer').className='sign-btn'+(s===0?' is-xfer':'');
  // in modalità trasferimento: niente categoria/saldo iniziale, "Metodo" diventa "Da" e compare "A"
  const x=s===0;
  document.getElementById('field-cat').style.display=x?'none':'';
  document.getElementById('row-isbal').style.display=x?'none':'';
  document.getElementById('field-to').style.display=x?'':'none';
  document.getElementById('lbl-method').textContent=x?'Da':'Metodo di pagamento';
  syncRepeat();
}
// la ricorrenza non si applica a trasferimenti né a saldi iniziali
function syncRepeat(){
  document.getElementById('field-repeat').style.display=(sign===0||document.getElementById('f-isbal').checked)?'none':'';
}
document.getElementById('f-isbal').addEventListener('change',syncRepeat);

async function saveTx() {
  const raw=parseFloat(document.getElementById('f-amount').value);
  if(isNaN(raw)||raw<0){alert('Importo non valido');return;}
  const date=document.getElementById('f-date').value;
  if(!date){alert('Inserisci una data');return;}

  if(sign===0){
    if(!(raw>0)){alert('Importo non valido');return;}
    if(sMFM.value===sMTo.value){alert('Scegli due metodi diversi');return;}
    const body={amount:raw, from:sMFM.value, to:sMTo.value, date, description:document.getElementById('f-desc').value.trim()};
    if(editTid) await api(`/api/transfers/${editTid}`,{method:'PUT',body});
    else        await api('/api/transfers',           {method:'POST',body});
    closeModal(); await render(); return;
  }

  // categoria e progetto (entrambi opzionali) dal menu unificato
  const {cat:category, proj:project_id} = sMFC.value;
  const repeat=sRep.value==='none'?'':sRep.value;
  if(repeat==='following'&&!confirm('Applicare la modifica a questa e a tutte le prossime occorrenze?')) return;
  if(repeat==='stop'&&!confirm('Interrompere la serie? Le occorrenze già create restano.')) return;

  const payload={
    amount:sign*raw,
    description:document.getElementById('f-desc').value.trim(),
    category, project_id, repeat,
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
  if(editTid) await api(`/api/transfers/${editTid}`,{method:'DELETE'});
  else {
    const tx=STATE.txs.find(t=>t.id===editId);
    const series=!!tx?.recurrence_id&&confirm('Questa transazione è ricorrente.\n\nOK = elimina anche le prossime occorrenze e interrompi la serie\nAnnulla = elimina solo questa');
    await api(`/api/txs/${editId}${series?'?series=1':''}`,{method:'DELETE'});
  }
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
  if(name==='analysis') loadAnalysis();
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
    sFC=sFM=sFT=sNG=sMFC=sMFM=sMTo=sRep=sAM=sAC=sAA=sAB=null;
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

// ── Analisi ──
// Colori (validati con validate_palette su sfondo #181410): mese in analisi / mese di
// riferimento / altri mesi. Stessa assegnazione in tutti i grafici (il colore segue il mese).
const AN_A='#b08a24', AN_B='#4a90d9', AN_DIM='#7a7265', AN_NONE='__none__';
const AN={range:6, method:'', cat:'', a:null, b:null, sort:'spend', view:'chart', data:null, key:'', req:0};
let AN_TIPS=[];

const ymKey=(y,m)=>y+'-'+String(m).padStart(2,'0');
function addMonths(ym,d){
  let [y,m]=ym.split('-').map(Number); m+=d;
  while(m>12){m-=12;y++;} while(m<1){m+=12;y--;}
  return ymKey(y,m);
}
const monShort=ym=>SM[+ym.slice(5)-1];
const monLong=ym=>MONTHS[+ym.slice(5)-1]+' '+ym.slice(0,4);
const catName=c=>c===AN_NONE?'senza categoria':c;
const fmtK=v=>{const a=Math.abs(v), s=v<0?'−':'';
  return a>=1000 ? s+(a/1000).toFixed(a%1000?1:0).replace('.',',')+'K' : s+String(Math.round(a*100)/100).replace('.',',');};
const lblK=v=>'€'+(v>=1000?fmtK(v):Math.round(v));
const anActive=()=>document.getElementById('pane-analysis').classList.contains('active');

function niceStep(raw){const mag=Math.pow(10,Math.floor(Math.log10(raw))), f=raw/mag; return (f<=1?1:f<=2?2:f<=5?5:10)*mag;}
function niceTicks(max,n=4){
  if(!(max>0)) return [0,1];
  const step=niceStep(max/n), top=Math.ceil(max/step-1e-9)*step, t=[];
  for(let v=0;v<=top+1e-9;v+=step) t.push(Math.round(v*100)/100);
  return t;
}
function niceDomain(lo,hi,n=4){
  if(hi<=lo) hi=lo+1;
  const step=niceStep((hi-lo)/n), l=Math.floor(lo/step+1e-9)*step, h=Math.ceil(hi/step-1e-9)*step, t=[];
  for(let v=l;v<=h+1e-9;v+=step) t.push(Math.round(v*100)/100);
  return {lo:t[0], hi:t[t.length-1], ticks:t};
}
// colonna: spigolo arrotondato (4px) solo in alto, squadrato sulla linea di base
function barPath(x,y,w,h,r=4){
  if(!(h>0)) return '';
  r=Math.min(r,h,w/2);
  return `M${x} ${y+h}V${y+r}A${r} ${r} 0 0 1 ${x+r} ${y}H${x+w-r}A${r} ${r} 0 0 1 ${x+w} ${y+r}V${y+h}Z`;
}
function anDelta(cur,prev){
  if(!(prev>0.005)) return cur>0.005?{cls:'up',txt:'nuovo'}:{cls:'',txt:'—'};
  const p=(cur-prev)/prev*100;
  return {cls:p>0.5?'up':p<-0.5?'down':'', txt:(p>0.5?'▲ +':p<-0.5?'▼ −':'• ')+Math.abs(p).toFixed(0)+'%'};
}
const tipId=t=>AN_TIPS.push(t)-1;

// ── tooltip (testo sempre via textContent: i nomi sono dati dell'utente) ──
function showTip(t){
  const el=document.getElementById('an-tip'); el.replaceChildren();
  const h=document.createElement('div'); h.className='an-tip-t'; h.textContent=t.title; el.appendChild(h);
  t.rows.forEach(r=>{
    const row=document.createElement('div'); row.className='an-tip-r';
    const k=document.createElement('i'); if(r.color) k.style.background=r.color; else k.className='none';
    const l=document.createElement('span'); l.textContent=r.label;
    const v=document.createElement('b'); v.textContent=r.value;
    row.append(k,l,v); el.appendChild(row);
  });
  el.style.display='block';
}
function placeTip(x,y){
  const el=document.getElementById('an-tip'), w=el.offsetWidth, h=el.offsetHeight;
  let L=x+14, T=y+14;
  if(L+w>innerWidth-8) L=x-w-14;
  if(T+h>innerHeight-8) T=y-h-14;
  el.style.left=Math.max(8,L)+'px'; el.style.top=Math.max(8,T)+'px';
}
function hideTip(){
  document.getElementById('an-tip').style.display='none';
  const xh=document.getElementById('an-xh'), xd=document.getElementById('an-xd');
  if(xh) xh.style.display='none'; if(xd) xd.style.display='none';
}
function bindTips(root){
  const on=(t,x,y)=>{
    showTip(AN_TIPS[+t.dataset.t]); placeTip(x,y);
    const xh=document.getElementById('an-xh'), xd=document.getElementById('an-xd');
    if(xh&&xd&&t.dataset.x){
      xh.setAttribute('x1',t.dataset.x); xh.setAttribute('x2',t.dataset.x); xh.style.display='';
      xd.setAttribute('cx',t.dataset.x); xd.setAttribute('cy',t.dataset.y); xd.style.display='';
    }
  };
  root.addEventListener('pointermove',e=>{const t=e.target.closest('[data-t]'); t?on(t,e.clientX,e.clientY):hideTip();});
  root.addEventListener('pointerleave',hideTip);
  root.addEventListener('focusin',e=>{const t=e.target.closest('[data-t]'); if(t){const r=t.getBoundingClientRect(); on(t,r.left+r.width/2,r.top);}});
  root.addEventListener('focusout',hideTip);
}

// ── dati ──
async function loadAnalysis(){
  const months=anMonths(), start=addMonths(months[0],-1), end=months[months.length-1];
  const key=months[0]+'|'+end;
  if(AN.key!==key){AN.key=key; AN.a=AN.b=null;}   // nuovo periodo → confronto di default
  const my=++AN.req, body=document.getElementById('an-body');
  if(AN.data) body.style.opacity=.5;                 // durante il ricaricamento resta il vecchio grafico
  try {
    const data=await api(`/api/analytics?start=${start}&end=${end}`);
    if(my!==AN.req) return;
    AN.data={...data, months:[start,...months]};
    renderAnalysis();
  } finally { if(my===AN.req) body.style.opacity=1; }
}
function anMonths(){
  const end=ymKey(curYear,curMonth), n=AN.range==='year'?curMonth:AN.range;
  return Array.from({length:n},(_,i)=>addMonths(end,i-(n-1)));
}
function anModel(){
  const d=AN.data, months=anMonths(), all=d.months;
  const rows=d.spend.filter(r=>AN.method?r.method===AN.method:r.method!=='Risparmio');
  const byMC={};
  rows.forEach(r=>{const c=r.category||AN_NONE, m=byMC[r.month]||(byMC[r.month]={}); m[c]=(m[c]||0)+r.amount;});
  const val=(m,c)=>(byMC[m]&&byMC[m][c])||0;
  const cats=[...new Set(rows.map(r=>r.category||AN_NONE))];
  const total=m=>AN.cat?val(m,AN.cat):cats.reduce((s,c)=>s+val(m,c),0);
  // di default si analizza l'ultimo mese del periodo CON spese (es. a inizio mese, quando quello
  // in corso è ancora vuoto, si confronta il mese scorso col precedente invece di un "−100%" ovunque)
  const a=AN.a&&all.includes(AN.a)?AN.a:([...months].reverse().find(m=>total(m)>.005)||months[months.length-1]);
  let b=AN.b&&all.includes(AN.b)&&AN.b!==a?AN.b:addMonths(a,-1);
  if(!all.includes(b)||b===a) b=all.find(m=>m!==a)||null;
  const projRows=d.projects.filter(r=>!AN.method||r.method===AN.method);
  const proj=m=>projRows.filter(r=>r.month===m).reduce((s,r)=>s+r.amount,0);
  const bal=m=>{
    const e=d.balances.find(x=>x.month===m); if(!e) return 0;
    return AN.method?(e.balances[AN.method]||0)
      :Object.entries(e.balances).filter(([k])=>k!=='Risparmio').reduce((s,[,v])=>s+v,0);
  };
  return {months, all, cats, val, total, a, b, proj, bal};
}

// ── render ──
function segHTML(key,items){
  return items.map(([v,l])=>`<button class="${String(AN[key])===String(v)?'on':''}" onclick="anSet('${key}','${v}')">${l}</button>`).join('');
}
function anSet(k,v){
  AN[k]=(k==='range'&&v!=='year')?+v:v;
  if(k==='range') loadAnalysis(); else renderAnalysis();
}
function anPickCat(c){ AN.cat=AN.cat===c?'':c; renderAnalysis(); }
function openAnalysis(method){
  AN.method=method;
  switchTab('analysis',document.getElementById('tab-analysis'));
}
function anSyncControls(M){
  document.getElementById('seg-range').innerHTML=segHTML('range',[['3','3 mesi'],['6','6 mesi'],['12','12 mesi'],['year','Anno']]);
  document.getElementById('seg-sort').innerHTML=segHTML('sort',[['spend','Spesa'],['delta','Variazione']]);
  document.getElementById('seg-view').innerHTML=segHTML('view',[['chart','Grafici'],['table','Tabella']]);
  sAM.value=AN.method;
  sAC.setOpts([{value:'',label:'Tutte le categorie'},
    ...STATE.cats.filter(c=>c.name!=='guadagni').map(c=>({value:c.name,label:c.name})),
    {value:AN_NONE,label:'senza categoria'}],true);
  sAC.value=AN.cat;
  const mo=M.all.map(m=>({value:m,label:monLong(m)}));
  sAA.setOpts(mo,true); sAA.value=M.a;
  sAB.setOpts(mo,true); sAB.value=M.b||'';
}
function renderAnalysis(){
  if(!AN.data) return;
  const M=anModel();
  anSyncControls(M);
  AN_TIPS=[]; hideTip();
  document.getElementById('an-body').innerHTML = AN.view==='table'
    ? anTableHTML(M)
    : anTilesHTML(M)+anCardMonths(M)+anCardCompare(M)+anCardBalance(M);
}
const anScope=()=>(AN.cat?catName(AN.cat):'tutte le categorie')+' · '+(AN.method||'tutti i metodi');
const anWidth=()=>Math.max(document.getElementById('an-body').clientWidth-42,280);
const lgItem=(c,t)=>`<span class="lg"><i style="background:${c}"></i>${esc(t)}</span>`;

function anTilesHTML(M){
  const ta=M.total(M.a), tb=M.b?M.total(M.b):0, dl=anDelta(ta,tb);
  const avg=M.months.reduce((s,m)=>s+M.total(m),0)/M.months.length;
  const proj=M.months.reduce((s,m)=>s+M.proj(m),0);
  return `<div class="an-tiles">
    <div class="tile"><div class="tile-l">Spesa ${esc(monLong(M.a))}</div><div class="tile-v">${euroAbs(ta)}</div>
      <div class="tile-d">${M.b?`<span class="dl ${dl.cls}">${dl.txt}</span> vs ${esc(monLong(M.b))} (${euroAbs(tb)})`:'—'}</div></div>
    <div class="tile"><div class="tile-l">Media mensile</div><div class="tile-v">${euroAbs(avg)}</div>
      <div class="tile-d">negli ultimi ${M.months.length} mesi · ${esc(anScope())}</div></div>
    <div class="tile"><div class="tile-l">Progetti (a parte)</div><div class="tile-v">${euroAbs(proj)}</div>
      <div class="tile-d">escluse dai grafici · stesso periodo</div></div>
  </div>`;
}

function anCardMonths(M){
  const W=anWidth(), H=230, L=44, R=8, T=14, B=26, pw=W-L-R, ph=H-T-B, n=M.months.length, band=pw/n;
  const vals=M.months.map(m=>Math.max(0,M.total(m)));
  const ticks=niceTicks(Math.max(...vals,1)), top=ticks[ticks.length-1];
  const yy=v=>T+ph-(v/top)*ph;
  let g='';
  ticks.forEach(t=>{g+=`<line x1="${L}" x2="${W-R}" y1="${yy(t)}" y2="${yy(t)}" stroke="var(--border)" stroke-width="1"/>`
    +`<text x="${L-8}" y="${yy(t)+3}" text-anchor="end">${fmtK(t)}</text>`;});
  g+=`<line x1="${L}" x2="${W-R}" y1="${yy(0)}" y2="${yy(0)}" stroke="var(--border2)" stroke-width="1"/>`;
  M.months.forEach((m,i)=>{
    const v=vals[i], bw=Math.min(24,band*.6), x=L+i*band+(band-bw)/2;
    const col=m===M.a?AN_A:m===M.b?AN_B:AN_DIM;
    g+=`<path d="${barPath(x,yy(v),bw,yy(0)-yy(v))}" fill="${col}"/>`;
    if((m===M.a||m===M.b)&&v>0&&band>=44) g+=`<text x="${x+bw/2}" y="${yy(v)-5}" text-anchor="middle" style="fill:var(--muted2)">${lblK(v)}</text>`;
    const yr=(i===0||m.endsWith('-01'))?" '"+m.slice(2,4):'';
    g+=`<text x="${x+bw/2}" y="${H-8}" text-anchor="middle">${monShort(m)}${yr}</text>`;
    const pm=addMonths(m,-1), hasPrev=M.all.includes(pm), tot=M.total(m);
    const rows=[{color:col,label:AN.cat?catName(AN.cat):'Spesa',value:euroAbs(tot)}];
    if(hasPrev) rows.push({label:'vs '+monShort(pm),value:anDelta(tot,M.total(pm)).txt});
    if(!AN.cat) [...M.cats].map(c=>[c,M.val(m,c)]).filter(x=>x[1]>.005).sort((p,q)=>q[1]-p[1]).slice(0,3)
      .forEach(([c,vv])=>rows.push({label:catName(c),value:euroAbs(vv)}));
    g+=`<rect class="hit" x="${L+i*band}" y="${T}" width="${band}" height="${ph+B}" data-t="${tipId({title:monLong(m),rows})}" tabindex="0" role="img" aria-label="${esc(monLong(m))}: ${euroAbs(tot)}"/>`;
  });
  return `<div class="card"><div class="card-head"><div class="card-title">Spesa per mese · ${esc(anScope())}</div></div>
    <div class="lg-row">${lgItem(AN_A,monLong(M.a))}${M.b?lgItem(AN_B,monLong(M.b)):''}${lgItem(AN_DIM,'altri mesi')}</div>
    <svg class="an-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="group" aria-label="Spesa per mese">${g}</svg></div>`;
}

function anCardCompare(M){
  const head=`<div class="card"><div class="card-head"><div class="card-title">Spesa per categoria${M.b?' · '+esc(monLong(M.a))+' vs '+esc(monLong(M.b)):''}</div></div>`;
  if(!M.b) return head+'<div class="empty-state">Nessun altro mese con cui confrontare</div></div>';
  const rows=M.cats.map(c=>({c,a:Math.max(0,M.val(M.a,c)),b:Math.max(0,M.val(M.b,c))}))
    .filter(r=>r.a>.005||r.b>.005);
  rows.sort(AN.sort==='delta'?(x,y)=>(y.a-y.b)-(x.a-x.b):(x,y)=>y.a-x.a||y.b-x.b);
  if(!rows.length) return head+'<div class="empty-state">Nessuna spesa in questi mesi</div></div>';
  const maxV=Math.max(...rows.flatMap(r=>[r.a,r.b]),1);
  const sgn=d=>d>.005?'▲':d<-.005?'▼':'•';
  const cls=d=>d>.005?'up':d<-.005?'down':'';
  const lines=rows.map(r=>{
    const d=r.a-r.b, dl=anDelta(r.a,r.b);
    const tip={title:catName(r.c),rows:[
      {color:AN_A,label:monLong(M.a),value:euroAbs(r.a)},
      {color:AN_B,label:monLong(M.b),value:euroAbs(r.b)},
      {label:'variazione',value:euroSgn(d)+' ('+dl.txt+')'}]};
    return `<div class="cmp-row ${AN.cat===r.c?'sel':''}" data-cat="${esc(r.c)}" data-t="${tipId(tip)}" tabindex="0" role="button" aria-pressed="${AN.cat===r.c}">
      <div class="cmp-name">${esc(catName(r.c))}</div>
      <div class="cmp-bars"><div class="cmp-bar" style="width:${(r.a/maxV*100).toFixed(1)}%;background:${AN_A}"></div><div class="cmp-bar" style="width:${(r.b/maxV*100).toFixed(1)}%;background:${AN_B}"></div></div>
      <div class="cmp-num">${euroAbs(r.a)}</div><div class="cmp-num b">${euroAbs(r.b)}</div>
      <div class="cmp-num dl ${cls(d)}">${sgn(d)} ${euroSgn(d)}</div></div>`;
  }).join('');
  const ta=rows.reduce((s,r)=>s+r.a,0), tb=rows.reduce((s,r)=>s+r.b,0), td=ta-tb;
  return head+`<div class="lg-row">${lgItem(AN_A,monLong(M.a))}${lgItem(AN_B,monLong(M.b))}</div>
    <div class="cmp-row head"><div>categoria</div><div></div><div style="text-align:right">${esc(monShort(M.a))}</div><div class="cmp-num b" style="font-family:inherit">${esc(monShort(M.b))}</div><div style="text-align:right">variazione</div></div>
    ${lines}
    <div class="cmp-row total"><div class="cmp-name">Totale</div><div></div><div class="cmp-num">${euroAbs(ta)}</div><div class="cmp-num b">${euroAbs(tb)}</div><div class="cmp-num dl ${cls(td)}">${sgn(td)} ${euroSgn(td)}</div></div>
    <div class="an-note">Clicca una categoria per vederne l'andamento nel grafico mensile.</div></div>`;
}

function anCardBalance(M){
  const W=anWidth(), H=210, L=44, R=8, T=16, B=26, pw=W-L-R, ph=H-T-B, n=M.months.length, band=pw/n;
  const vals=M.months.map(m=>M.bal(m));
  const dom=niceDomain(Math.min(0,...vals),Math.max(0,...vals));
  const yy=v=>T+ph-((v-dom.lo)/(dom.hi-dom.lo))*ph, xx=i=>L+band*(i+.5);
  const col=AN.method?MCOLOR[AN.method]:AN_A;
  let g='';
  dom.ticks.forEach(t=>{g+=`<line x1="${L}" x2="${W-R}" y1="${yy(t)}" y2="${yy(t)}" stroke="${t===0?'var(--border2)':'var(--border)'}" stroke-width="1"/>`
    +`<text x="${L-8}" y="${yy(t)+3}" text-anchor="end">${fmtK(t)}</text>`;});
  const pts=vals.map((v,i)=>[xx(i),yy(v)]);
  const line=pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join('');
  const base=yy(Math.max(dom.lo,Math.min(0,dom.hi)));
  g+=`<path d="${line}L${pts[n-1][0].toFixed(1)} ${base}L${pts[0][0].toFixed(1)} ${base}Z" fill="${col}" fill-opacity=".1"/>`;
  g+=`<path d="${line}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  g+=`<line id="an-xh" x1="0" x2="0" y1="${T}" y2="${T+ph}" stroke="var(--muted)" stroke-width="1" style="display:none"/>`;
  g+=`<circle id="an-xd" r="4" fill="${col}" stroke="var(--surface)" stroke-width="2" style="display:none"/>`;
  const last=pts[n-1];
  g+=`<circle cx="${last[0]}" cy="${last[1]}" r="4" fill="${col}" stroke="var(--surface)" stroke-width="2"/>`;
  g+=`<text x="${Math.min(last[0],W-R-4)}" y="${last[1]-10}" text-anchor="end" style="fill:var(--muted2)">${vals[n-1]<0?'−':''}${euroAbs(vals[n-1])}</text>`;
  M.months.forEach((m,i)=>{
    const yr=(i===0||m.endsWith('-01'))?" '"+m.slice(2,4):'';
    g+=`<text x="${xx(i)}" y="${H-8}" text-anchor="middle">${monShort(m)}${yr}</text>`;
    const pv=i>0?vals[i-1]:null, v=vals[i];
    const rows=[{color:col,label:AN.method||'Disponibile',value:(v<0?'−':'')+euroAbs(v)}];
    if(pv!==null) rows.push({label:'vs '+monShort(M.months[i-1]),value:euroSgn(v-pv)});
    g+=`<rect class="hit" x="${L+i*band}" y="${T}" width="${band}" height="${ph+B}" data-t="${tipId({title:monLong(m),rows})}" data-x="${xx(i)}" data-y="${yy(v)}" tabindex="0" role="img" aria-label="${esc(monLong(m))}: ${(v<0?'−':'')+euroAbs(v)}"/>`;
  });
  return `<div class="card"><div class="card-head"><div class="card-title">Saldo ${esc(AN.method||'disponibile (senza Risparmio)')} a fine mese</div></div>
    <svg class="an-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="group" aria-label="Saldo a fine mese">${g}</svg></div>`;
}

// vista tabella: stessi numeri dei grafici, senza bisogno di hover
function anTableHTML(M){
  const sum=c=>M.months.reduce((s,m)=>s+M.val(m,c),0);
  const cats=M.cats.filter(c=>M.months.some(m=>Math.abs(M.val(m,c))>.005)).sort((x,y)=>sum(y)-sum(x));
  const cell=v=>`<td class="${Math.abs(v)<.005?'z':''}">${Math.abs(v)<.005?'—':euroAbs(v)}</td>`;
  const tot=m=>cats.reduce((s,c)=>s+M.val(m,c),0);
  return `<div class="card"><div class="card-head"><div class="card-title">Spesa per categoria e mese · ${esc(AN.method||'tutti i metodi')}</div></div>
    <div class="an-scroll"><table class="an-table"><thead><tr><th>categoria</th>${M.months.map(m=>`<th>${esc(monShort(m))} ${m.slice(2,4)}</th>`).join('')}<th>totale</th></tr></thead><tbody>
    ${cats.map(c=>`<tr><td>${esc(catName(c))}</td>${M.months.map(m=>cell(M.val(m,c))).join('')}<td>${euroAbs(sum(c))}</td></tr>`).join('')}
    <tr class="foot"><td>Totale</td>${M.months.map(m=>cell(tot(m))).join('')}<td>${euroAbs(M.months.reduce((s,m)=>s+tot(m),0))}</td></tr>
    <tr class="sep"><td>Progetti (a parte)</td>${M.months.map(m=>cell(M.proj(m))).join('')}<td>${euroAbs(M.months.reduce((s,m)=>s+M.proj(m),0))}</td></tr>
    <tr><td>Saldo a fine mese</td>${M.months.map(m=>`<td>${M.bal(m)<0?'−':''}${euroAbs(M.bal(m))}</td>`).join('')}<td></td></tr>
    </tbody></table></div></div>`;
}

(function anInit(){
  const body=document.getElementById('an-body');
  bindTips(body);
  body.addEventListener('click',e=>{const r=e.target.closest('[data-cat]'); if(r) anPickCat(r.dataset.cat);});
  body.addEventListener('keydown',e=>{
    if((e.key==='Enter'||e.key===' ')&&e.target.matches('[data-cat]')){e.preventDefault(); anPickCat(e.target.dataset.cat);}
  });
  let t; window.addEventListener('resize',()=>{clearTimeout(t); t=setTimeout(()=>{if(anActive()) renderAnalysis();},150);});
})();

render();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return HTML

if __name__ == "__main__":
    init_db()
    daily_backup()
    port = int(os.environ.get("PORT", 5001))
    host = os.environ.get("HOST", "127.0.0.1")  # solo locale: nessuna autenticazione
    url  = f"http://localhost:{port}"
    print(f"\n  ╔═════════════════════════════════════╗")
    print(f"  ║   Ledger  →  {url}  ║")
    print(f"  ╚═════════════════════════════════════╝\n")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, debug=False)