"""
SQLite persistence for the Tips Board: settings, confirmed days, shift blocks.

Uses a single file ``tip_board.db`` next to the app. All times for stored shifts
are local HH:MM strings as entered in the UI.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# Default manager inbox for first run and test mode (see README).
DEFAULT_MANAGER_EMAIL = "CATHYZHANG0404@GMAIL.COM"

DB_PATH = Path(__file__).resolve().parent / "tip_board.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(employee_names: list[str]) -> None:
    """Create tables and seed defaults."""
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS employee_settings (
                employee_name TEXT PRIMARY KEY,
                employee_email TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS manager_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                manager_email TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                test_mode INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS daily_confirmation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_date TEXT NOT NULL UNIQUE,
                confirmed_at TEXT NOT NULL,
                email_sent_count INTEGER NOT NULL DEFAULT 0,
                manager_email_sent INTEGER NOT NULL DEFAULT 0,
                overwrite_flag INTEGER NOT NULL DEFAULT 0,
                unassigned_tips_cents INTEGER NOT NULL DEFAULT 0,
                clover_total_tips_cents INTEGER NOT NULL DEFAULT 0,
                reconciliation_diff_cents INTEGER NOT NULL DEFAULT 0,
                allocated_employee_total_cents INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS confirmed_shift_block (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_id INTEGER NOT NULL,
                work_date TEXT NOT NULL,
                employee_name TEXT NOT NULL,
                block_start TEXT NOT NULL,
                block_end TEXT NOT NULL,
                FOREIGN KEY (log_id) REFERENCES daily_confirmation_log(id)
            );
            CREATE TABLE IF NOT EXISTS confirmed_daily_record (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_id INTEGER NOT NULL,
                work_date TEXT NOT NULL,
                employee_name TEXT NOT NULL,
                shift_start TEXT,
                shift_end TEXT,
                shift_blocks_json TEXT NOT NULL DEFAULT '[]',
                hours_worked REAL NOT NULL,
                tip_allocated_cents INTEGER NOT NULL,
                confirmed_at TEXT NOT NULL,
                FOREIGN KEY (log_id) REFERENCES daily_confirmation_log(id)
            );
            CREATE INDEX IF NOT EXISTS idx_shift_block_date ON confirmed_shift_block(work_date);
            CREATE INDEX IF NOT EXISTS idx_shift_block_log ON confirmed_shift_block(log_id);
            CREATE INDEX IF NOT EXISTS idx_daily_record_date ON confirmed_daily_record(work_date);
            CREATE INDEX IF NOT EXISTS idx_daily_record_log ON confirmed_daily_record(log_id);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO manager_settings (id, manager_email) VALUES (1, ?)",
            (DEFAULT_MANAGER_EMAIL,),
        )
        conn.execute("INSERT OR IGNORE INTO app_settings (id, test_mode) VALUES (1, 1)")
        for name in employee_names:
            conn.execute(
                "INSERT OR IGNORE INTO employee_settings (employee_name, employee_email, is_active) VALUES (?, '', 1)",
                (name,),
            )
        conn.commit()


# --- Settings ---


def get_all_employee_settings() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT employee_name, employee_email, is_active FROM employee_settings ORDER BY employee_name"
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_employee_email(name: str, email: str, is_active: int = 1) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO employee_settings (employee_name, employee_email, is_active)
            VALUES (?, ?, ?)
            ON CONFLICT(employee_name) DO UPDATE SET
                employee_email = excluded.employee_email,
                is_active = excluded.is_active
            """,
            (name, email, is_active),
        )
        conn.commit()


def get_manager_email() -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT manager_email FROM manager_settings WHERE id = 1").fetchone()
    return (row["manager_email"] if row else "") or DEFAULT_MANAGER_EMAIL


def set_manager_email(email: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO manager_settings (id, manager_email) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET manager_email = excluded.manager_email",
            (email,),
        )
        conn.commit()


def get_test_mode() -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT test_mode FROM app_settings WHERE id = 1").fetchone()
    return bool(row["test_mode"]) if row else True


def set_test_mode(enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (id, test_mode) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET test_mode = excluded.test_mode",
            (1 if enabled else 0,),
        )
        conn.commit()


def get_employee_email_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for r in get_all_employee_settings():
        out[r["employee_name"]] = (r["employee_email"] or "").strip()
    return out


# --- Confirmations ---


def get_confirmation_for_date(work_date: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM daily_confirmation_log WHERE work_date = ?", (work_date,)
        ).fetchone()
    return dict(row) if row else None


def delete_confirmation_for_date(work_date: str) -> None:
    """Remove log and all child rows for a calendar day."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM daily_confirmation_log WHERE work_date = ?", (work_date,)
        ).fetchone()
        if not row:
            return
        lid = row["id"]
        conn.execute("DELETE FROM confirmed_shift_block WHERE log_id = ?", (lid,))
        conn.execute("DELETE FROM confirmed_daily_record WHERE log_id = ?", (lid,))
        conn.execute("DELETE FROM daily_confirmation_log WHERE id = ?", (lid,))
        conn.commit()


def insert_confirmation_bundle(
    work_date: str,
    confirmed_at_iso: str,
    overwrite_flag: int,
    unassigned_cents: int,
    clover_tips_cents: int,
    recon_diff_cents: int,
    allocated_total_cents: int,
    per_employee: list[dict[str, Any]],
    email_sent_count: int,
    manager_sent: int,
) -> int:
    """
    ``per_employee`` items: name, blocks: list[{start,end}], hours_worked, tip_cents.
    Returns new log id.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO daily_confirmation_log (
                work_date, confirmed_at, email_sent_count, manager_email_sent, overwrite_flag,
                unassigned_tips_cents, clover_total_tips_cents, reconciliation_diff_cents,
                allocated_employee_total_cents
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                work_date,
                confirmed_at_iso,
                email_sent_count,
                manager_sent,
                overwrite_flag,
                unassigned_cents,
                clover_tips_cents,
                recon_diff_cents,
                allocated_total_cents,
            ),
        )
        log_id = int(cur.lastrowid)

        for row in per_employee:
            name = row["name"]
            blocks: list[dict[str, str]] = row["blocks"]
            hours = float(row["hours_worked"])
            tips = int(row["tip_cents"])
            j = json.dumps(blocks, separators=(",", ":"))
            first_start = blocks[0]["start"] if blocks else ""
            last_end = blocks[-1]["end"] if blocks else ""
            conn.execute(
                """
                INSERT INTO confirmed_daily_record (
                    log_id, work_date, employee_name, shift_start, shift_end,
                    shift_blocks_json, hours_worked, tip_allocated_cents, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_id,
                    work_date,
                    name,
                    first_start,
                    last_end,
                    j,
                    hours,
                    tips,
                    confirmed_at_iso,
                ),
            )
            for b in blocks:
                conn.execute(
                    """
                    INSERT INTO confirmed_shift_block (log_id, work_date, employee_name, block_start, block_end)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (log_id, work_date, name, b["start"], b["end"]),
                )
        conn.commit()
    return log_id


def list_confirmed_daily_records(work_date: str) -> list[dict[str, Any]]:
    """Rows for one work_date (for resending emails from saved confirmation)."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT employee_name, shift_blocks_json, hours_worked, tip_allocated_cents, confirmed_at
            FROM confirmed_daily_record
            WHERE work_date = ?
            ORDER BY employee_name
            """,
            (work_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_confirmation_email_stats(log_id: int, email_sent_count: int, manager_email_sent: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE daily_confirmation_log
            SET email_sent_count = ?, manager_email_sent = ?
            WHERE id = ?
            """,
            (email_sent_count, manager_email_sent, log_id),
        )
        conn.commit()


# --- Summaries (confirmed data only) ---


def weekly_hours_detail(week_start: date) -> tuple[dict[str, list[tuple[str, float]]], dict[str, float]]:
    """
    Returns (per_employee list of (date_iso, hours), per_employee week total hours).
    Only ``confirmed_daily_record`` rows. ``week_start`` must be a **Monday**; range is Mon–Sun (7 days).
    """
    end = week_start + timedelta(days=6)
    d0 = week_start.isoformat()
    d1 = end.isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT work_date, employee_name, hours_worked
            FROM confirmed_daily_record
            WHERE work_date >= ? AND work_date <= ?
            ORDER BY employee_name, work_date
            """,
            (d0, d1),
        ).fetchall()

    by_emp: dict[str, list[tuple[str, float]]] = defaultdict(list)
    totals: dict[str, float] = defaultdict(float)
    for r in rows:
        wd = r["work_date"]
        en = r["employee_name"]
        h = round(float(r["hours_worked"]), 2)
        by_emp[en].append((wd, h))
        totals[en] += h
    totals = {k: round(v, 2) for k, v in totals.items()}
    return dict(by_emp), dict(totals)


def two_week_totals(period_start: date) -> dict[str, float]:
    """``period_start`` should be a **Monday**; sums 14 days = two Mon–Sun weeks."""
    end = period_start + timedelta(days=13)
    d0 = period_start.isoformat()
    d1 = end.isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT employee_name, SUM(hours_worked) as th
            FROM confirmed_daily_record
            WHERE work_date >= ? AND work_date <= ?
            GROUP BY employee_name
            ORDER BY employee_name
            """,
            (d0, d1),
        ).fetchall()
    return {r["employee_name"]: round(float(r["th"]), 2) for r in rows}
