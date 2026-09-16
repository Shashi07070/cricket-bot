"""
db.py — SQLite persistence for bet alerts and accuracy tracking.

Schema: single 'alerts' table with full event context.
All functions use parameterised queries to prevent injection.
"""

import sqlite3
import logging
from datetime import date, datetime
from typing import Optional

DB_PATH = "alerts.db"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_ALERTS = """
CREATE TABLE IF NOT EXISTS alerts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        DATETIME DEFAULT CURRENT_TIMESTAMP,
    match_id         TEXT,
    session          TEXT,
    event            TEXT,
    signal           TEXT,
    line_before      REAL,
    line_after       REAL,
    fair_change      REAL,
    actual_change    REAL,
    deviation        REAL,
    score            TEXT,
    overs            TEXT,
    striker          TEXT,
    bowler           TEXT,
    result           TEXT DEFAULT 'PENDING',
    resolved_at      DATETIME,
    session_end_score INTEGER,
    notes            TEXT
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_match_session
    ON alerts(match_id, session, result);
"""


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def init_db(db_path: str = DB_PATH):
    """Create tables and indexes if they do not exist."""
    with _connect(db_path) as conn:
        conn.execute(_CREATE_ALERTS)
        conn.execute(_CREATE_INDEX)
    logger.info("Database initialised: %s", db_path)


def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Return a connection with row_factory set to Row."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def save_alert(signal_dict: dict, match_id: str, db_path: str = DB_PATH) -> int:
    """
    Persist a signal dict to the database.

    Parameters
    ----------
    signal_dict : dict
        Output of engine.detect_signal().
    match_id : str
        Current match identifier.

    Returns
    -------
    int
        Auto-generated alert ID.
    """
    ctx = signal_dict.get("context", {})
    sql = """
        INSERT INTO alerts
            (match_id, session, event, signal,
             line_before, line_after, fair_change, actual_change, deviation,
             score, overs, striker, bowler)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (
        match_id,
        signal_dict["session"],
        signal_dict["event"],
        signal_dict["signal"],
        signal_dict["line_before"],
        signal_dict["line_after"],
        signal_dict["fair_change"],
        signal_dict["actual_change"],
        signal_dict["deviation"],
        ctx.get("score"),
        ctx.get("overs"),
        ctx.get("striker"),
        ctx.get("bowler"),
    )
    with _connect(db_path) as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


def resolve_alert(
    alert_id: int,
    result: str,
    session_end_score: Optional[int] = None,
    notes: Optional[str] = None,
    db_path: str = DB_PATH,
):
    """
    Manually resolve a single alert.

    Parameters
    ----------
    result : str
        "WIN", "LOSS", or "VOID"
    """
    sql = """
        UPDATE alerts
        SET result = ?,
            resolved_at = CURRENT_TIMESTAMP,
            session_end_score = ?,
            notes = ?
        WHERE id = ?
    """
    with _connect(db_path) as conn:
        conn.execute(sql, (result.upper(), session_end_score, notes, alert_id))


def resolve_session_alerts(
    match_id: str,
    session: str,
    actual_score: int,
    db_path: str = DB_PATH,
) -> dict:
    """
    Auto-resolve all PENDING alerts for a match+session when the session ends.

    Resolution logic
    ----------------
    YES_OVER  → WIN if actual_score > line_after, else LOSS
    NOT_UNDER → WIN if actual_score < line_after, else LOSS

    Returns
    -------
    dict  {"total": int, "won": int, "lost": int}
    """
    pending = get_pending_alerts(match_id, session, db_path)
    won = lost = 0

    for row in pending:
        alert_id    = row["id"]
        signal      = row["signal"]
        line_after  = row["line_after"]

        if signal == "YES_OVER":
            result = "WIN" if actual_score > line_after else "LOSS"
        elif signal == "NOT_UNDER":
            result = "WIN" if actual_score < line_after else "LOSS"
        else:
            result = "VOID"

        resolve_alert(alert_id, result, actual_score, db_path=db_path)

        if result == "WIN":
            won += 1
        elif result == "LOSS":
            lost += 1

    return {"total": len(pending), "won": won, "lost": lost}


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def get_pending_alerts(
    match_id: str,
    session: str,
    db_path: str = DB_PATH,
) -> list:
    """Return all PENDING alerts for a given match and session."""
    sql = """
        SELECT * FROM alerts
        WHERE match_id = ? AND session = ? AND result = 'PENDING'
        ORDER BY id ASC
    """
    with _connect(db_path) as conn:
        rows = conn.execute(sql, (match_id, session)).fetchall()
    return [dict(r) for r in rows]


def get_alert(alert_id: int, db_path: str = DB_PATH) -> Optional[dict]:
    """Return a single alert by ID, or None if not found."""
    sql = "SELECT * FROM alerts WHERE id = ?"
    with _connect(db_path) as conn:
        row = conn.execute(sql, (alert_id,)).fetchone()
    return dict(row) if row else None


def get_today_alerts(db_path: str = DB_PATH) -> list:
    """Return all alerts created today (UTC date)."""
    today = date.today().isoformat()
    sql = """
        SELECT * FROM alerts
        WHERE date(timestamp) = ?
        ORDER BY id ASC
    """
    with _connect(db_path) as conn:
        rows = conn.execute(sql, (today,)).fetchall()
    return [dict(r) for r in rows]


def get_pending_all(db_path: str = DB_PATH) -> list:
    """Return all PENDING alerts across all matches."""
    sql = "SELECT * FROM alerts WHERE result = 'PENDING' ORDER BY id ASC"
    with _connect(db_path) as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def get_stats(date_filter: Optional[str] = None, db_path: str = DB_PATH) -> dict:
    """
    Compute accuracy statistics.

    Parameters
    ----------
    date_filter : str | None
        ISO date string "YYYY-MM-DD".  If None, all-time stats are returned.

    Returns
    -------
    dict with keys: total, won, lost, pending, accuracy,
                    by_session, by_event, by_signal
    """
    where_clause = ""
    params: list = []
    if date_filter:
        where_clause = "WHERE date(timestamp) = ?"
        params = [date_filter]

    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM alerts {where_clause} ORDER BY id ASC",
            params,
        ).fetchall()

    alerts = [dict(r) for r in rows]

    total   = len(alerts)
    won     = sum(1 for a in alerts if a["result"] == "WIN")
    lost    = sum(1 for a in alerts if a["result"] == "LOSS")
    pending = sum(1 for a in alerts if a["result"] == "PENDING")
    decided = won + lost
    accuracy = (won / decided * 100) if decided > 0 else 0.0

    by_session = _group_stats(alerts, "session", ["6over", "20over"])
    by_event   = _group_stats(alerts, "event",   ["wicket", "four", "six", "dot"])
    by_signal  = _group_stats(alerts, "signal",  ["YES_OVER", "NOT_UNDER"])

    # Best / worst session by percentage
    session_pcts = {
        k: v["pct"] for k, v in by_session.items() if v["total"] > 0
    }
    best  = max(session_pcts, key=session_pcts.get) if session_pcts else "N/A"
    worst = min(session_pcts, key=session_pcts.get) if session_pcts else "N/A"

    return {
        "total":      total,
        "won":        won,
        "lost":       lost,
        "pending":    pending,
        "accuracy":   round(accuracy, 1),
        "by_session": by_session,
        "by_event":   by_event,
        "by_signal":  by_signal,
        "best":       best,
        "worst":      worst,
    }


def _group_stats(alerts: list, key: str, categories: list) -> dict:
    """
    Build a breakdown dict for a given grouping key.

    Returns {category: {"total": int, "won": int, "pct": float}}
    """
    result = {}
    for cat in categories:
        subset  = [a for a in alerts if a.get(key) == cat]
        total   = len(subset)
        won     = sum(1 for a in subset if a["result"] == "WIN")
        lost    = sum(1 for a in subset if a["result"] == "LOSS")
        decided = won + lost
        pct     = round(won / decided * 100, 1) if decided > 0 else 0.0
        result[cat] = {"total": total, "won": won, "lost": lost, "pct": pct}
    return result
