"""
Daily tip allocation dashboard — FastAPI backend.

Pulls Clover payments for a chosen *local* calendar day, then splits tips
among employees who were on shift at each transaction minute.
"""

from __future__ import annotations

import csv
import io
import math
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, Field
from starlette.templating import Jinja2Templates

import database
import email_service

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
# Local: load optional .env files. On Render, configure variables in the dashboard instead.
# override=True: .env wins over empty/preset vars in the shell (fixes "SMTP_HOST not set" when .env is correct).
load_dotenv(BASE_DIR.parent / ".env", override=True)
load_dotenv(BASE_DIR / ".env", override=True)

# Seven employees — order is used for display and deterministic cent splits.
EMPLOYEES: list[str] = [
    "Cathy",
    "Olivia",
    "Maren",
    "K",
    "Gaby",
    "Constance",
    "Julie",
]

PAGE_LIMIT = 1000


def _bootstrap_settings_from_env() -> None:
    """
    Optional production overrides (e.g. Render): set on each process start if present.

    - MANAGER_EMAIL: if non-empty, updates SQLite manager address (same as UI settings).
    - TEST_MODE_EMAIL_ONLY: if this env var exists at all, sets test mode on/off from value
      (true/1/yes/on vs anything else). If unset, leaves DB as seeded / last saved.
    """
    raw_mgr = os.environ.get("MANAGER_EMAIL")
    if raw_mgr is not None and raw_mgr.strip():
        try:
            normalized = validate_email(raw_mgr.strip(), check_deliverability=False).normalized
            database.set_manager_email(normalized)
        except EmailNotValidError:
            pass

    if "TEST_MODE_EMAIL_ONLY" in os.environ:
        v = os.environ.get("TEST_MODE_EMAIL_ONLY", "").strip().lower()
        database.set_test_mode(v in ("1", "true", "yes", "on"))


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Create SQLite schema and seed employees / default manager email.
    database.init_db(EMPLOYEES)
    _bootstrap_settings_from_env()
    yield


app = FastAPI(title="Tip allocation dashboard", lifespan=_lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# -----------------------------------------------------------------------------
# Environment & Clover HTTP
# -----------------------------------------------------------------------------


def _require_clover_config() -> tuple[str, str, str]:
    """Return (base_url, merchant_id, token) or raise HTTPException."""
    base = os.environ.get("CLOVER_BASE_URL", "").strip()
    mid = os.environ.get("CLOVER_MERCHANT_ID", "").strip()
    token = os.environ.get("CLOVER_API_TOKEN", "").strip()
    missing = [k for k, v in [
        ("CLOVER_BASE_URL", base),
        ("CLOVER_MERCHANT_ID", mid),
        ("CLOVER_API_TOKEN", token),
    ] if not v]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Missing environment variable(s): {', '.join(missing)}. Set them before starting the app.",
        )
    return base, mid, token


def _payments_url(base_url: str, merchant_id: str) -> str:
    """Build …/v3/merchants/{mId}/payments (base may already include /v3)."""
    base = base_url.rstrip("/")
    path = f"/merchants/{merchant_id}/payments"
    if base.endswith("/v3"):
        return f"{base}{path}"
    return f"{base}/v3{path}"


def _local_day_bounds_ms(day: date) -> tuple[int, int]:
    """Local midnight .. next midnight as Clover ms (end exclusive)."""
    tz = datetime.now().astimezone().tzinfo
    start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=tz)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _created_time_filter_params(start_ms: int, end_ms: int) -> list[tuple[str, str]]:
    """Two filter= params (Clover range query)."""
    return [
        ("filter", f"createdTime>={start_ms}"),
        ("filter", f"createdTime<{end_ms}"),
    ]


def _nested_id(obj: Any) -> str | None:
    if obj is None:
        return None
    if isinstance(obj, dict):
        inner = obj.get("id")
        return str(inner) if inner is not None else None
    return str(obj)


def normalize_payment(raw: dict[str, Any]) -> dict[str, Any] | None:
    """
    Map one Clover payment element to a flat dict.
    Returns None if required fields are unusable.
    """
    pid = raw.get("id")
    if not pid:
        return None
    try:
        created_ms = int(raw["createdTime"]) if raw.get("createdTime") is not None else None
    except (TypeError, ValueError):
        return None
    if created_ms is None:
        return None

    # Local datetime for shift logic (minute-rounded later).
    try:
        utc_dt = datetime.fromtimestamp(created_ms / 1000.0, tz=timezone.utc)
        local_dt = utc_dt.astimezone()
    except (OSError, OverflowError, ValueError):
        return None

    amt = raw.get("amount")
    tip = raw.get("tipAmount")
    tax = raw.get("taxAmount")
    try:
        amount_cents = int(amt) if amt is not None else 0
        tip_cents = int(tip) if tip is not None else 0
        tax_cents = int(tax) if tax is not None else 0
    except (TypeError, ValueError):
        return None

    return {
        "payment_id": str(pid),
        "order_id": _nested_id(raw.get("order")) or "",
        "amount_cents": amount_cents,
        "tip_amount_cents": tip_cents,
        "tax_amount_cents": tax_cents,
        "created_time_ms": created_ms,
        "created_at_local_iso": local_dt.isoformat(),
        "created_at_utc_iso": utc_dt.isoformat(),
        "result": str(raw.get("result") or ""),
    }


def fetch_clover_payments_for_date(target: date) -> list[dict[str, Any]]:
    """
    GET all payments for one local calendar day from Clover (paginated).
    Raises HTTPException on HTTP / config errors.
    """
    base_url, merchant_id, token = _require_clover_config()
    url = _payments_url(base_url, merchant_id)
    start_ms, end_ms = _local_day_bounds_ms(target)
    filter_params = _created_time_filter_params(start_ms, end_ms)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    session = requests.Session()
    all_elements: list[dict[str, Any]] = []
    offset = 0

    while True:
        params: list[tuple[str, Any]] = [
            ("limit", PAGE_LIMIT),
            ("offset", offset),
        ]
        params.extend(filter_params)
        try:
            resp = session.get(url, headers=headers, params=params, timeout=60)
        except requests.RequestException as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Clover request failed: {exc}",
            ) from exc

        if resp.status_code != 200:
            snippet = (resp.text or "")[:400]
            raise HTTPException(
                status_code=502,
                detail=f"Clover API error {resp.status_code}: {snippet}",
            )

        try:
            payload = resp.json()
        except ValueError:
            raise HTTPException(status_code=502, detail="Clover returned non-JSON body.")

        if isinstance(payload, dict) and "elements" in payload:
            elements = payload["elements"]
        elif isinstance(payload, list):
            elements = payload
        else:
            raise HTTPException(status_code=502, detail="Unexpected Clover JSON shape.")

        if not elements:
            break
        all_elements.extend(elements)
        if len(elements) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT

    out: list[dict[str, Any]] = []
    for el in all_elements:
        if not isinstance(el, dict):
            continue
        norm = normalize_payment(el)
        if norm is not None:
            out.append(norm)
    return out


# -----------------------------------------------------------------------------
# Shift parsing & tip allocation
# -----------------------------------------------------------------------------


def _parse_hhmm(s: str) -> tuple[int, int]:
    """Parse 'HH:MM' or 'H:MM' → (hour, minute)."""
    s = (s or "").strip()
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time (use HH:MM): {s!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Time out of range: {s!r}")
    return h, m


def _minute_of(dt: datetime) -> datetime:
    """Floor to the minute (used for inclusive shift matching)."""
    return dt.replace(second=0, microsecond=0)


def _shift_blocks_to_minutes(
    day: date,
    blocks: list[dict[str, str]],
    tz: tzinfo,
) -> list[tuple[datetime, datetime]]:
    """
    Convert [{'start':'09:00','end':'12:00'}, ...] to list of
    (start_minute, end_minute) datetime pairs in local tz, inclusive on both ends.
    """
    ranges: list[tuple[datetime, datetime]] = []
    for b in blocks:
        sh, sm = _parse_hhmm(b.get("start", ""))
        eh, em = _parse_hhmm(b.get("end", ""))
        start = datetime(day.year, day.month, day.day, sh, sm, 0, tzinfo=tz)
        end = datetime(day.year, day.month, day.day, eh, em, 0, tzinfo=tz)
        start_m = _minute_of(start)
        end_m = _minute_of(end)
        if end_m < start_m:
            raise ValueError(f"Shift end before start: {b}")
        ranges.append((start_m, end_m))
    return ranges


def _is_working_at(
    tx_minute: datetime,
    ranges: list[tuple[datetime, datetime]],
) -> bool:
    """True if tx_minute falls in any inclusive [start, end] minute block."""
    for start_m, end_m in ranges:
        if start_m <= tx_minute <= end_m:
            return True
    return False


def _scheduled_hours(blocks: list[dict[str, str]], day: date, tz: tzinfo) -> float:
    """Total scheduled hours from shift blocks (simple end-start sum)."""
    total_sec = 0.0
    for b in blocks:
        sh, sm = _parse_hhmm(b.get("start", ""))
        eh, em = _parse_hhmm(b.get("end", ""))
        start = datetime(day.year, day.month, day.day, sh, sm, 0, tzinfo=tz)
        end = datetime(day.year, day.month, day.day, eh, em, 0, tzinfo=tz)
        if end < start:
            continue
        total_sec += (end - start).total_seconds()
    return round(total_sec / 3600.0, 2)


def split_tip_cents(total_cents: int, names: list[str]) -> dict[str, int]:
    """Split whole cents across names; sum always equals total_cents."""
    n = len(names)
    if n == 0:
        return {}
    names_sorted = sorted(names)
    base = total_cents // n
    rem = total_cents % n
    return {name: base + (1 if i < rem else 0) for i, name in enumerate(names_sorted)}


def split_tip_cents_by_fractions(total_cents: int, fractions: dict[str, float]) -> dict[str, int]:
    """
    Split ``total_cents`` by non-negative weights (e.g. 0.5 + 0.5 or 1/3 + 2/3).
    Weights are normalized to sum to 1. Uses largest-remainder so cents add up exactly.
    Only employees in EMPLOYEES with positive weight receive cents.
    """
    if total_cents <= 0:
        return {}
    weights = {k: max(0.0, float(fractions.get(k, 0.0))) for k in EMPLOYEES}
    active = [k for k in EMPLOYEES if weights[k] > 0.0]
    if not active:
        return {}
    wsum = sum(weights[k] for k in active)
    if wsum <= 0:
        return {}

    raw = {k: total_cents * weights[k] / wsum for k in active}
    floors = {k: int(math.floor(raw[k] + 1e-12)) for k in active}
    assigned = sum(floors.values())
    remainder = max(0, total_cents - assigned)
    # Hamilton: give one extra cent to the `remainder` people with largest fractional parts.
    order = sorted(
        active,
        key=lambda k: (raw[k] - floors[k], k),
        reverse=True,
    )
    out = dict(floors)
    for i in range(min(remainder, len(order))):
        out[order[i]] += 1
    return out


def run_allocation(
    target: date,
    payments: list[dict[str, Any]],
    shifts_input: dict[str, list[dict[str, str]]],
    manual_by_payment_id: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """
    Core allocation logic. Returns JSON-serializable dict for API + exports.

    ``manual_by_payment_id``: optional map ``payment_id -> { employee: weight }``.
    Those tips skip time-based rules and split by normalized weights only.
    """
    manual_by_payment_id = manual_by_payment_id or {}
    tz = datetime.now().astimezone().tzinfo

    # Build per-employee minute ranges (only known employees).
    shift_ranges: dict[str, list[tuple[datetime, datetime]]] = {}
    scheduled_hours: dict[str, float] = {}
    for name in EMPLOYEES:
        blocks = shifts_input.get(name) or []
        if not blocks:
            shift_ranges[name] = []
            scheduled_hours[name] = 0.0
            continue
        shift_ranges[name] = _shift_blocks_to_minutes(day=target, blocks=blocks, tz=tz)
        scheduled_hours[name] = _scheduled_hours(blocks, target, tz)

    total_sales_cents = sum(p["amount_cents"] for p in payments)
    clover_total_tips_cents = sum(p["tip_amount_cents"] for p in payments)

    tipped = [p for p in payments if p["tip_amount_cents"] > 0]

    employee_allocated: dict[str, int] = {e: 0 for e in EMPLOYEES}
    employee_tip_events: dict[str, int] = {e: 0 for e in EMPLOYEES}

    tx_rows: list[dict[str, Any]] = []
    unassigned_total_cents = 0
    count_manual = 0
    count_time = 0

    for p in tipped:
        tip_cents = p["tip_amount_cents"]
        local_raw = datetime.fromtimestamp(
            p["created_time_ms"] / 1000.0, tz=timezone.utc
        ).astimezone()
        tx_minute = _minute_of(local_raw)
        pid = p["payment_id"]

        if pid in manual_by_payment_id:
            # Party / special tip: split by fixed fractions (ignores shift clock).
            count_manual += 1
            weights = manual_by_payment_id[pid]
            split_map = split_tip_cents_by_fractions(tip_cents, weights)
            wsum = sum(max(0.0, float(weights.get(k, 0.0))) for k in EMPLOYEES)
            norm_display = {
                k: round(max(0.0, float(weights.get(k, 0.0))) / wsum, 4)
                for k in EMPLOYEES
                if wsum > 0 and max(0.0, float(weights.get(k, 0.0))) > 0
            }
            recipients = sorted([k for k, c in split_map.items() if c > 0])
            n_active = len(recipients)
            unassigned = n_active == 0
            if unassigned:
                unassigned_total_cents += tip_cents
                per_person = 0.0
            else:
                per_person = (tip_cents / n_active) / 100.0
                for name, cents in split_map.items():
                    employee_allocated[name] += cents
                    if cents > 0:
                        employee_tip_events[name] += 1

            tx_rows.append({
                "created_at_local": local_raw.isoformat(),
                "payment_id": pid,
                "tip_amount_cents": tip_cents,
                "tip_amount_dollars": round(tip_cents / 100.0, 2),
                "allocation_mode": "manual",
                "employees_working": recipients,
                "active_employee_count": n_active,
                "per_person_tip_dollars": round(per_person, 2) if not unassigned else 0.0,
                "per_person_split_cents": split_map,
                "manual_fractions_normalized": norm_display,
                "unassigned": unassigned,
                "result": p.get("result", ""),
            })
            continue

        # Default: time-based split among employees on shift at this minute.
        count_time += 1
        working = [
            e for e in EMPLOYEES
            if _is_working_at(tx_minute, shift_ranges[e])
        ]
        n_active = len(working)
        unassigned = n_active == 0

        if unassigned:
            unassigned_total_cents += tip_cents
            split_map = {}
            per_person = 0.0
        else:
            split_map = split_tip_cents(tip_cents, working)
            per_person = (tip_cents / n_active) / 100.0
            for name, cents in split_map.items():
                employee_allocated[name] += cents
                employee_tip_events[name] += 1

        tx_rows.append({
            "created_at_local": local_raw.isoformat(),
            "payment_id": pid,
            "tip_amount_cents": tip_cents,
            "tip_amount_dollars": round(tip_cents / 100.0, 2),
            "allocation_mode": "time",
            "employees_working": working,
            "active_employee_count": n_active,
            "per_person_tip_dollars": round(per_person, 2) if not unassigned else 0.0,
            "per_person_split_cents": split_map,
            "manual_fractions_normalized": {},
            "unassigned": unassigned,
            "result": p.get("result", ""),
        })

    allocated_employee_total = sum(employee_allocated.values())
    pool = sum(p["tip_amount_cents"] for p in tipped)
    recomputed_sum = allocated_employee_total + unassigned_total_cents
    diff = pool - recomputed_sum

    employee_table = []
    for e in EMPLOYEES:
        employee_table.append({
            "employee": e,
            "allocated_tip_cents": employee_allocated[e],
            "allocated_tip_dollars": round(employee_allocated[e] / 100.0, 2),
            "transactions_shared": employee_tip_events[e],
            "scheduled_hours": scheduled_hours[e],
        })

    return {
        "date": target.isoformat(),
        "payments_count_all": len(payments),
        "payments_count_with_tips": len(tipped),
        "tip_transactions_time_based": count_time,
        "tip_transactions_manual": count_manual,
        "total_sales_cents": total_sales_cents,
        "total_sales_dollars": round(total_sales_cents / 100.0, 2),
        "clover_total_tips_cents": clover_total_tips_cents,
        "clover_total_tips_dollars": round(clover_total_tips_cents / 100.0, 2),
        "tip_pool_cents": pool,
        "tip_pool_dollars": round(pool / 100.0, 2),
        "allocated_employee_total_cents": allocated_employee_total,
        "allocated_employee_total_dollars": round(allocated_employee_total / 100.0, 2),
        "unassigned_total_cents": unassigned_total_cents,
        "unassigned_total_dollars": round(unassigned_total_cents / 100.0, 2),
        "allocated_plus_unassigned_cents": recomputed_sum,
        "allocated_plus_unassigned_dollars": round(recomputed_sum / 100.0, 2),
        "reconciliation_difference_cents": diff,
        "reconciliation_difference_dollars": round(diff / 100.0, 2),
        "employees": employee_table,
        "transactions": tx_rows,
    }


# -----------------------------------------------------------------------------
# Pydantic request bodies
# -----------------------------------------------------------------------------


class ShiftBlockIn(BaseModel):
    start: str = Field(..., description="HH:MM local")
    end: str = Field(..., description="HH:MM local")


class ManualRuleIn(BaseModel):
    """Override how one payment’s tip is split (e.g. party tip). Ignores shift time for that payment only."""

    payment_id: str = Field(..., description="Clover payment id")
    fractions: dict[str, float] = Field(
        default_factory=dict,
        description="Employee name -> weight (e.g. 0.5 and 0.5, or 1 and 2). Weights are normalized.",
    )


class CalculateIn(BaseModel):
    date: str = Field(..., description="YYYY-MM-DD")
    shifts: dict[str, list[ShiftBlockIn]] = Field(default_factory=dict)
    manual_rules: list[ManualRuleIn] = Field(
        default_factory=list,
        description="Optional per-payment manual fraction splits before time-based allocation for the rest.",
    )


class ConfirmSendIn(CalculateIn):
    """Same payload as calculate, plus overwrite when re-confirming a day."""

    overwrite: bool = False


class AppSettingsPayload(BaseModel):
    """Save manager inbox, test-mode switch, and per-employee emails (empty = not set yet)."""

    manager_email: str
    test_mode: bool
    employees: dict[str, str] = Field(default_factory=dict)


def _validate_email_optional(label: str, raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        validate_email(s, check_deliverability=False)
    except EmailNotValidError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid email for {label}: {exc}") from exc
    return s


def _validate_email_required(label: str, raw: str) -> str:
    s = _validate_email_optional(label, raw)
    if not s:
        raise HTTPException(status_code=400, detail=f"{label} is required.")
    return s


def _build_manual_map(
    rules: list[ManualRuleIn],
    tipped_payment_ids: set[str],
) -> dict[str, dict[str, float]]:
    """
    Validate manual rules and return payment_id -> full employee weight dict.
    Raises HTTPException on bad input.
    """
    seen: set[str] = set()
    out: dict[str, dict[str, float]] = {}

    for rule in rules:
        pid = rule.payment_id.strip()
        if not pid:
            raise HTTPException(status_code=400, detail="Manual rule has empty payment_id.")
        if pid in seen:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate manual rule for payment_id {pid!r}. Remove duplicates.",
            )
        seen.add(pid)

        if pid not in tipped_payment_ids:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Manual rule payment_id {pid!r} is not a tipped payment on this date "
                    "(refresh Clover and pick an id from tipped payments)."
                ),
            )

        for name in rule.fractions:
            if name not in EMPLOYEES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Manual rule for {pid!r}: unknown employee {name!r}.",
                )

        weights = {k: max(0.0, float(rule.fractions.get(k, 0.0))) for k in EMPLOYEES}
        if sum(weights.values()) <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"Manual rule for {pid!r}: enter at least one positive fraction/weight.",
            )
        out[pid] = weights

    return out


def _parse_date(s: str) -> date:
    try:
        y, m, d = (int(x) for x in s.split("-", 2))
        return date(y, m, d)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid date; use YYYY-MM-DD.") from exc


def _monday_of_week_containing(d: date) -> date:
    """Calendar week Mon–Sun: return the Monday of the week that contains ``d``."""
    return d - timedelta(days=d.weekday())


def _normalize_shifts_in(raw: dict[str, list[ShiftBlockIn]]) -> dict[str, list[dict[str, str]]]:
    """Keep only the seven employees; coerce to plain dicts."""
    out: dict[str, list[dict[str, str]]] = {}
    for name in EMPLOYEES:
        blocks = raw.get(name) or []
        out[name] = [{"start": b.start, "end": b.end} for b in blocks]
    return out


def _hhmm_to_ampm(hhmm: str) -> str:
    """24h 'HH:MM' -> 'H:MM AM/PM' for email display."""
    parts = (hhmm or "").strip().split(":")
    h = int(parts[0]) % 24
    m = int(parts[1]) if len(parts) > 1 else 0
    period = "AM" if h < 12 else "PM"
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{m:02d} {period}"


def _format_blocks_ampm(blocks: list[dict[str, str]]) -> str:
    if not blocks:
        return "—"
    return "; ".join(
        f"{_hhmm_to_ampm(b['start'])} - {_hhmm_to_ampm(b['end'])}" for b in blocks
    )


def _execute_calculate(body: CalculateIn) -> tuple[date, dict[str, list[dict[str, str]]], dict[str, dict[str, float]], dict[str, Any]]:
    """
    Shared path: validate shifts, fetch Clover, build manual map, return allocation result.
    """
    target = _parse_date(body.date)
    shifts_plain = _normalize_shifts_in(body.shifts)
    if not any(len(v) > 0 for v in shifts_plain.values()):
        raise HTTPException(
            status_code=400,
            detail="Enter at least one shift block before calculating.",
        )
    payments = fetch_clover_payments_for_date(target)
    if not payments:
        raise HTTPException(
            status_code=404,
            detail="No payments returned from Clover for this date (check date, merchant, or API limits).",
        )
    tipped_ids = {p["payment_id"] for p in payments if p["tip_amount_cents"] > 0}
    manual_map = _build_manual_map(body.manual_rules, tipped_ids)
    try:
        result = run_allocation(target, payments, shifts_plain, manual_map)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return target, shifts_plain, manual_map, result


def _build_confirm_preview_rows(
    shifts_plain: dict[str, list[dict[str, str]]],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Employees with at least one shift block + hours + tips from allocation."""
    rows: list[dict[str, Any]] = []
    by_emp = {r["employee"]: r for r in result["employees"]}
    for name in EMPLOYEES:
        blocks = shifts_plain.get(name) or []
        if not blocks:
            continue
        info = by_emp[name]
        rows.append({
            "name": name,
            "blocks": blocks,
            "hours_worked": round(float(info["scheduled_hours"]), 2),
            "tip_allocated_cents": int(info["allocated_tip_cents"]),
            "tip_allocated_dollars": round(float(info["allocated_tip_dollars"]), 2),
            "shift_label_ampm": _format_blocks_ampm(blocks),
        })
    return rows


def _resolve_recipient(
    employee_name: str,
    employee_email: str,
    manager_email: str,
    test_mode: bool,
) -> tuple[str, str]:
    """
    Returns (actual_to_address, subject_tag).
    In test mode all mail goes to manager; subject should include intended employee.
    """
    mgr = (manager_email or "").strip() or database.DEFAULT_MANAGER_EMAIL
    if test_mode:
        return mgr, employee_name
    em = (employee_email or "").strip()
    if not em:
        return mgr, f"{employee_name} (fallback)"
    return em, employee_name


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------


@app.get("/health")
async def health() -> JSONResponse:
    """Lightweight check for uptime monitors (Render, etc.)."""
    return JSONResponse({"ok": True})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Any:
    # Starlette expects (request, template_name, context); request is injected into context automatically.
    return templates.TemplateResponse(
        request,
        "index.html",
        {"employees": EMPLOYEES},
    )


@app.get("/api/payments")
async def api_payments(date_str: str = Query(..., alias="date")) -> JSONResponse:
    """Fetch Clover payments for a local calendar day (all amounts, not only tips)."""
    target = _parse_date(date_str)
    try:
        payments = fetch_clover_payments_for_date(target)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    total_sales = sum(p["amount_cents"] for p in payments)
    total_tips = sum(p["tip_amount_cents"] for p in payments)
    with_tips = sum(1 for p in payments if p["tip_amount_cents"] > 0)

    return JSONResponse({
        "ok": True,
        "date": target.isoformat(),
        "count": len(payments),
        "count_with_tips": with_tips,
        "total_sales_cents": total_sales,
        "total_sales_dollars": round(total_sales / 100.0, 2),
        "total_tips_cents": total_tips,
        "total_tips_dollars": round(total_tips / 100.0, 2),
        "payments": payments,
    })


@app.post("/api/calculate")
async def api_calculate(body: CalculateIn) -> JSONResponse:
    try:
        _t, _s, _m, result = _execute_calculate(body)
    except HTTPException:
        raise
    return JSONResponse({"ok": True, **result})


def _csv_employees(result: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["employee", "allocated_tip_dollars", "allocated_tip_cents", "transactions_shared", "scheduled_hours"])
    for row in result["employees"]:
        w.writerow([
            row["employee"],
            row["allocated_tip_dollars"],
            row["allocated_tip_cents"],
            row["transactions_shared"],
            row["scheduled_hours"],
        ])
    return buf.getvalue()


def _csv_transactions(result: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "created_at_local",
        "payment_id",
        "allocation_mode",
        "tip_dollars",
        "tip_cents",
        "active_employee_count",
        "employees_working",
        "per_person_tip_dollars_display",
        "manual_fractions_normalized",
        "unassigned",
        "result",
    ])
    for row in result["transactions"]:
        mf = row.get("manual_fractions_normalized") or {}
        mf_str = ";".join(f"{k}={v}" for k, v in sorted(mf.items()))
        w.writerow([
            row["created_at_local"],
            row["payment_id"],
            row.get("allocation_mode", "time"),
            row["tip_amount_dollars"],
            row["tip_amount_cents"],
            row["active_employee_count"],
            ";".join(row["employees_working"]),
            row["per_person_tip_dollars"],
            mf_str,
            row["unassigned"],
            row["result"],
        ])
    return buf.getvalue()


@app.post("/api/export/employees")
async def export_employees(body: CalculateIn) -> StreamingResponse:
    try:
        _t, _s, _mm, result = _execute_calculate(body)
    except HTTPException:
        raise
    data = _csv_employees(result)
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="employee_summary_{body.date}.csv"'},
    )


@app.post("/api/export/transactions")
async def export_transactions(body: CalculateIn) -> StreamingResponse:
    try:
        _t, _sp, _mm, result = _execute_calculate(body)
    except HTTPException:
        raise
    data = _csv_transactions(result)
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="tip_transactions_{body.date}.csv"'},
    )


# -----------------------------------------------------------------------------
# Settings, confirm & send, weekly / two-week summaries
# -----------------------------------------------------------------------------


@app.get("/api/settings")
async def api_get_settings() -> JSONResponse:
    rows = database.get_all_employee_settings()
    by_name = {r["employee_name"]: r for r in rows}
    ordered = []
    for name in EMPLOYEES:
        r = by_name.get(name)
        if r:
            ordered.append(
                {
                    "employee_name": r["employee_name"],
                    "employee_email": r["employee_email"] or "",
                    "is_active": bool(r["is_active"]),
                }
            )
        else:
            ordered.append({"employee_name": name, "employee_email": "", "is_active": True})
    smtp = email_service.smtp_env_status()
    return JSONResponse(
        {
            "ok": True,
            "employees": ordered,
            "manager_email": database.get_manager_email(),
            "test_mode": database.get_test_mode(),
            "smtp_ready": smtp["ready"],
            "smtp_missing": smtp["missing"],
            "smtp_hint": smtp["hint"],
        }
    )


@app.post("/api/settings")
async def api_save_settings(body: AppSettingsPayload) -> JSONResponse:
    mgr = _validate_email_required("Manager email", body.manager_email)
    database.set_manager_email(mgr)
    database.set_test_mode(body.test_mode)
    for name in EMPLOYEES:
        raw = (body.employees or {}).get(name, "")
        em = _validate_email_optional(f"Employee {name}", raw)
        database.upsert_employee_email(name, em, 1)
    return JSONResponse({"ok": True, "message": "Settings saved."})


@app.get("/api/confirm/status")
async def api_confirm_status(date_str: str = Query(..., alias="date")) -> JSONResponse:
    row = database.get_confirmation_for_date(date_str)
    if not row:
        return JSONResponse({"ok": True, "confirmed": False})
    return JSONResponse(
        {
            "ok": True,
            "confirmed": True,
            "confirmed_at": row["confirmed_at"],
            "email_sent_count": row["email_sent_count"],
            "manager_email_sent": bool(row["manager_email_sent"]),
            "overwrite_flag": bool(row["overwrite_flag"]),
        }
    )


@app.post("/api/confirm/preview")
async def api_confirm_preview(body: CalculateIn) -> JSONResponse:
    try:
        target, shifts_plain, _mm, result = _execute_calculate(body)
    except HTTPException:
        raise
    preview_rows = _build_confirm_preview_rows(shifts_plain, result)
    return JSONResponse(
        {
            "ok": True,
            "date": target.isoformat(),
            "preview_employees": preview_rows,
            "allocation": result,
        }
    )


@app.post("/api/confirm/send")
async def api_confirm_send(body: ConfirmSendIn) -> JSONResponse:
    try:
        target, shifts_plain, _mm, result = _execute_calculate(body)
    except HTTPException:
        raise

    work_date = target.isoformat()
    existing = database.get_confirmation_for_date(work_date)
    if existing and not body.overwrite:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"This date ({work_date}) is already confirmed at {existing['confirmed_at']}. "
                "Submit again with overwrite=true to replace records and resend emails.",
                "requires_overwrite": True,
                "confirmed_at": existing["confirmed_at"],
            },
        )

    preview_rows = _build_confirm_preview_rows(shifts_plain, result)
    if not preview_rows:
        raise HTTPException(
            status_code=400,
            detail="No employees with shift blocks — nothing to confirm.",
        )

    manager_email = database.get_manager_email()
    test_mode = database.get_test_mode()
    emp_emails = database.get_employee_email_map()

    confirmed_at = datetime.now(timezone.utc).isoformat()
    overwrite_flag = 1 if (existing and body.overwrite) else 0

    if existing and body.overwrite:
        database.delete_confirmation_for_date(work_date)

    # --- Send emails before DB insert so we can report failures (still save if partial?) ---
    # User asked: save records and send — we save after successful sends, or save anyway?
    # Spec: "save the final confirmed daily records" then send — if send fails, still save?
    # Practical: save to DB first, then send (so data isn't lost). User can resend manually later.
    # Alternative: send first then save — if DB fails emails already went.
    # We save first, then send emails, then update log counts (or store counts from send result).

    per_employee_db = [
        {
            "name": r["name"],
            "blocks": r["blocks"],
            "hours_worked": r["hours_worked"],
            "tip_cents": r["tip_allocated_cents"],
        }
        for r in preview_rows
    ]

    log_id = database.insert_confirmation_bundle(
        work_date=work_date,
        confirmed_at_iso=confirmed_at,
        overwrite_flag=overwrite_flag,
        unassigned_cents=int(result["unassigned_total_cents"]),
        clover_tips_cents=int(result["clover_total_tips_cents"]),
        recon_diff_cents=int(result["reconciliation_difference_cents"]),
        allocated_total_cents=int(result["allocated_employee_total_cents"]),
        per_employee=per_employee_db,
        email_sent_count=0,
        manager_sent=0,
    )

    email_results: list[dict[str, Any]] = []
    sent_employee = 0

    for r in preview_rows:
        name = r["name"]
        to_addr, subject_tag = _resolve_recipient(
            name, emp_emails.get(name, ""), manager_email, test_mode
        )
        shift_line = _format_blocks_ampm(r["blocks"])
        body_txt = (
            f"Hi {name},\n\n"
            f"Here is your work summary for today:\n\n"
            f"Date: {work_date}\n"
            f"Shift: {shift_line}\n"
            f"Hours worked: {r['hours_worked']:.2f}\n"
            f"Tips allocated: ${r['tip_allocated_dollars']:.2f}\n\n"
            f"Thank you.\n"
        )
        subj = (
            f"[TEST] Daily Work Summary for {subject_tag}"
            if test_mode
            else "Your work summary for today"
        )
        try:
            email_service.send_plain_email(to_addr, subj, body_txt)
            email_results.append({"employee": name, "to": to_addr, "ok": True, "error": None})
            sent_employee += 1
        except Exception as exc:  # pragma: no cover - network
            email_results.append({"employee": name, "to": to_addr, "ok": False, "error": str(exc)})

    mgr_lines = [
        f"Daily Summary - {work_date}",
        "",
        "Per employee:",
    ]
    for r in preview_rows:
        mgr_lines.append(
            f"{r['name']}: {r['hours_worked']:.2f} hours, ${r['tip_allocated_dollars']:.2f} tips"
        )
    mgr_lines.extend(
        [
            "",
            f"Employees worked (with shifts): {len(preview_rows)}",
            f"Total allocated tips: ${result['allocated_employee_total_dollars']:.2f}",
            f"Total unassigned tips: ${result['unassigned_total_dollars']:.2f}",
            f"Clover total tips (day): ${result['clover_total_tips_dollars']:.2f}",
            f"Reconciliation difference: ${result['reconciliation_difference_dollars']:.2f}",
        ]
    )
    mgr_body = "\n".join(mgr_lines)
    mgr_subj = f"[TEST] Manager Daily Summary - {work_date}" if test_mode else f"Daily Summary - {work_date}"
    mgr_ok = False
    mgr_err: str | None = None
    try:
        email_service.send_plain_email(manager_email, mgr_subj, mgr_body)
        mgr_ok = True
    except Exception as exc:
        mgr_err = str(exc)

    # Update log counts
    with database.get_conn() as conn:
        conn.execute(
            """
            UPDATE daily_confirmation_log
            SET email_sent_count = ?, manager_email_sent = ?
            WHERE id = ?
            """,
            (sent_employee, 1 if mgr_ok else 0, log_id),
        )
        conn.commit()

    return JSONResponse(
        {
            "ok": True,
            "message": "Day confirmed and emails processed.",
            "work_date": work_date,
            "confirmed_at": confirmed_at,
            "overwrite": bool(overwrite_flag),
            "employee_emails": email_results,
            "employee_emails_sent_ok": sent_employee,
            "manager_email_sent": mgr_ok,
            "manager_email_error": mgr_err,
            "test_mode": test_mode,
        }
    )


def _fmt_us_short(d: date) -> str:
    return f"{d.month}/{d.day}"


@app.get("/api/summary/weekly")
async def api_summary_weekly(week_start: str = Query(..., alias="week_start")) -> JSONResponse:
    ws = _monday_of_week_containing(_parse_date(week_start))
    detail, totals = database.weekly_hours_detail(ws)
    # Pretty lines for UI
    lines: dict[str, list[str]] = {}
    for emp, pairs in detail.items():
        lines[emp] = [f"{_fmt_us_short(_parse_date(d))} {h} hours" for d, h in pairs]
    week_end = ws + timedelta(days=6)
    return JSONResponse(
        {
            "ok": True,
            "week_start": ws.isoformat(),
            "week_end": week_end.isoformat(),
            "by_employee_lines": lines,
            "totals_hours": totals,
        }
    )


@app.get("/api/summary/two-week")
async def api_summary_two_week(period_start: str = Query(..., alias="period_start")) -> JSONResponse:
    ps = _monday_of_week_containing(_parse_date(period_start))
    totals = database.two_week_totals(ps)
    pe = ps + timedelta(days=13)
    return JSONResponse(
        {
            "ok": True,
            "period_start": ps.isoformat(),
            "period_end": pe.isoformat(),
            "totals_hours": totals,
        }
    )


@app.get("/api/export/weekly.csv")
async def export_weekly_csv(week_start: str = Query(..., alias="week_start")) -> StreamingResponse:
    ws = _monday_of_week_containing(_parse_date(week_start))
    detail, totals = database.weekly_hours_detail(ws)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["employee", "work_date", "hours_worked"])
    for emp in sorted(detail.keys()):
        for d_iso, h in detail[emp]:
            w.writerow([emp, d_iso, f"{h:.2f}"])
    w.writerow([])
    w.writerow(["employee", "week_total_hours"])
    for emp in sorted(totals.keys()):
        w.writerow([emp, f"{totals[emp]:.2f}"])
    data = buf.getvalue()
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="weekly_{ws.isoformat()}.csv"'},
    )


@app.get("/api/export/two-week.csv")
async def export_two_week_csv(period_start: str = Query(..., alias="period_start")) -> StreamingResponse:
    ps = _monday_of_week_containing(_parse_date(period_start))
    totals = database.two_week_totals(ps)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["employee", "total_hours_two_weeks"])
    for emp in sorted(totals.keys()):
        w.writerow([emp, f"{totals[emp]:.2f}"])
    data = buf.getvalue()
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="two_week_{ps.isoformat()}.csv"'},
    )
