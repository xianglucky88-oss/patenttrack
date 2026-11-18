"""单一职责：本地 sqlite3 持久化。

- 业务记录：幂等 upsert / 计数 / 全量读取（既有接口，保持兼容）。
- 分页状态：查询版本、运行游标、每次请求的可复现边界、去重账本、隔离待补。
"""
import json, sqlite3, time
from .config import DB_PATH

# ---- schema -------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records(
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qp_runs(
    run_id        TEXT PRIMARY KEY,
    query_version TEXT NOT NULL,
    query_config  TEXT NOT NULL,
    mode          TEXT NOT NULL,
    page_size     INTEGER NOT NULL,
    status        TEXT NOT NULL,
    last_cursor   TEXT,
    next_cursor   TEXT,
    next_page     INTEGER NOT NULL DEFAULT 0,
    seen_total    INTEGER NOT NULL DEFAULT 0,
    total_hint    INTEGER,
    error         TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS qp_requests(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    request_json  TEXT NOT NULL,
    http_status   INTEGER,
    response_meta TEXT,
    error_code    TEXT,
    created_at    REAL NOT NULL,
    UNIQUE(run_id, seq)
);
CREATE TABLE IF NOT EXISTS qp_pages(
    run_id         TEXT NOT NULL,
    page_index     INTEGER NOT NULL,
    entry_cursor   TEXT,
    next_cursor    TEXT,
    total          INTEGER,
    first_id       TEXT,
    last_id        TEXT,
    count          INTEGER NOT NULL,
    boundary_json  TEXT NOT NULL,
    PRIMARY KEY(run_id, page_index)
);
CREATE TABLE IF NOT EXISTS qp_seen(
    run_id TEXT NOT NULL,
    rid    TEXT NOT NULL,
    PRIMARY KEY(run_id, rid)
);
CREATE TABLE IF NOT EXISTS qp_quarantine(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    code       TEXT NOT NULL,
    detail     TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def connect():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(_SCHEMA)
    return con


def init_schema(con):
    """在已有连接上建表（测试可用 :memory: 连接注入分页引擎）。"""
    con.executescript(_SCHEMA)
    return con


def connect_memory():
    con = sqlite3.connect(":memory:")
    return init_schema(con)


# ---- 业务记录（既有接口，签名与语义不变） ---------------------------------

def upsert(con, record):
    con.execute("INSERT INTO records(key,data) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (record.key, json.dumps(record.data, ensure_ascii=False)))

def commit(con): con.commit()

def count(con): return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]


# ---- 分页运行状态 --------------------------------------------------------

def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)

def create_run(con, *, run_id, query_version, query_config, mode, page_size):
    now = time.time()
    con.execute(
        "INSERT INTO qp_runs(run_id,query_version,query_config,mode,page_size,status,"
        "last_cursor,next_cursor,next_page,seen_total,total_hint,error,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,'',NULL,0,0,NULL,NULL,?,?)",
        (run_id, query_version, _dumps(query_config), mode, page_size,
         "running", now, now))

def get_run(con, run_id):
    row = con.execute("SELECT * FROM qp_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    cols = [c[0] for c in con.execute("SELECT * FROM qp_runs LIMIT 0").description]
    out = dict(zip(cols, row))
    out["query_config"] = json.loads(out["query_config"])
    return out

def update_run(con, run_id, **fields):
    if not fields:
        return
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE qp_runs SET {sets} WHERE run_id=?",
                (*fields.values(), run_id))

def find_resumable(con, query_version):
    """同查询版本下唯一可恢复的运行（running/partial/failed）。"""
    row = con.execute(
        "SELECT run_id FROM qp_runs WHERE query_version=? AND status IN "
        "('running','partial','failed') ORDER BY updated_at DESC LIMIT 1",
        (query_version,)).fetchone()
    return row[0] if row else None


# ---- 请求审计（可复现边界） ----------------------------------------------

def append_request(con, *, run_id, seq, request, http_status=None,
                   response_meta=None, error_code=None):
    con.execute(
        "INSERT INTO qp_requests(run_id,seq,request_json,http_status,"
        "response_meta,error_code,created_at) VALUES(?,?,?,?,?,?,?)",
        (run_id, seq, _dumps(request), http_status,
         _dumps(response_meta) if response_meta is not None else None,
         error_code, time.time()))

def next_seq(con, run_id):
    row = con.execute(
        "SELECT COALESCE(MAX(seq),0)+1 FROM qp_requests WHERE run_id=?",
        (run_id,)).fetchone()
    return row[0]

def list_requests(con, run_id):
    rows = con.execute(
        "SELECT seq,request_json,http_status,response_meta,error_code "
        "FROM qp_requests WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
    return [{"seq": s, "request": json.loads(rq), "http_status": st,
             "response_meta": json.loads(rm) if rm else None,
             "error_code": ec} for s, rq, st, rm, ec in rows]


# ---- 已确认页面边界 ------------------------------------------------------

def save_page(con, *, run_id, page_index, entry_cursor, next_cursor, total,
              first_id, last_id, count, boundary):
    con.execute(
        "INSERT INTO qp_pages(run_id,page_index,entry_cursor,next_cursor,total,"
        "first_id,last_id,count,boundary_json) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(run_id,page_index) DO UPDATE SET "
        "entry_cursor=excluded.entry_cursor,next_cursor=excluded.next_cursor,"
        "total=excluded.total,first_id=excluded.first_id,last_id=excluded.last_id,"
        "count=excluded.count,boundary_json=excluded.boundary_json",
        (run_id, page_index, entry_cursor, next_cursor, total, first_id,
         last_id, count, _dumps(boundary)))

def get_page(con, run_id, page_index):
    row = con.execute(
        "SELECT page_index,entry_cursor,next_cursor,total,first_id,last_id,"
        "count,boundary_json FROM qp_pages WHERE run_id=? AND page_index=?",
        (run_id, page_index)).fetchone()
    if row is None:
        return None
    pi, ec, nc, total, fid, lid, cnt, bj = row
    return {"page_index": pi, "entry_cursor": ec, "next_cursor": nc,
            "total": total, "first_id": fid, "last_id": lid,
            "count": cnt, "boundary": json.loads(bj)}

def list_pages(con, run_id):
    rows = con.execute(
        "SELECT page_index FROM qp_pages WHERE run_id=? ORDER BY page_index",
        (run_id,)).fetchall()
    return [get_page(con, run_id, r[0]) for r in rows]

def max_page_index(con, run_id):
    row = con.execute(
        "SELECT COALESCE(MAX(page_index),-1) FROM qp_pages WHERE run_id=?",
        (run_id,)).fetchone()
    return row[0]


# ---- 去重账本（按运行） ---------------------------------------------------

def seen_add(con, run_id, rid):
    """返回 True 表示新 id；False 表示此前已确认过。"""
    cur = con.execute("INSERT OR IGNORE INTO qp_seen(run_id,rid) VALUES(?,?)",
                      (run_id, str(rid)))
    return cur.rowcount == 1

def seen_count(con, run_id):
    return con.execute(
        "SELECT COUNT(*) FROM qp_seen WHERE run_id=?", (run_id,)).fetchone()[0]


# ---- 隔离待补 ------------------------------------------------------------

def quarantine(con, *, run_id, code, detail, scope):
    con.execute(
        "INSERT INTO qp_quarantine(run_id,code,detail,scope_json,created_at) "
        "VALUES(?,?,?,?,?)",
        (run_id, code, detail, _dumps(scope), time.time()))

def list_quarantine(con, run_id):
    rows = con.execute(
        "SELECT code,detail,scope_json FROM qp_quarantine WHERE run_id=? "
        "ORDER BY id", (run_id,)).fetchall()
    return [{"code": c, "detail": d, "scope": json.loads(s)} for c, d, s in rows]
