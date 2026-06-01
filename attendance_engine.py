"""
attendance_engine.py
====================
Industrial-grade attendance processing engine for Cosmo Hydraulic Industries.
Rewritten for production use per requirements document.

Architecture
------------
SESSION-BASED state machine (not day-based counting).
Strict IN/OUT state per employee: OUTSIDE → INSIDE → OUTSIDE.
Each punch is processed in strict chronological order.

Shift Ownership Rule
--------------------
A shift belongs to the DATE of its IN punch.
Night shift: 20:00 IN (Sat) → 08:00 OUT (Sun) = SATURDAY shift.

State Machine
-------------
  State: OUTSIDE  (initial)
    punch received → transition to INSIDE, record IN timestamp

  State: INSIDE
    punch received:
      - gap < DUPLICATE_THRESH_MIN  → duplicate, IGNORED
      - same day OR gap <= IMPOSSIBLE_H  → valid OUT, close shift
      - different day AND gap > IMPOSSIBLE_H → missing OUT (INVALID),
        new punch starts a FRESH IN session

Shift Classification (after OUT received)
-----------------------------------------
  raw_h < 0           → INVALID  (clock error, negative duration)
  raw_h > IMPOSSIBLE_H → INVALID  (>24h, physically impossible)
  raw_h > INVALID_H   → SUSPICIOUS (>20h, admin review required)
  raw_h > SUSPICIOUS_H → SUSPICIOUS (>16h, flagged with warning)
  raw_h < 0.1          → INVALID  (mis-scan, too short)
  otherwise            → VALID

Day Status (after shift aggregation)
-------------------------------------
  present         Mon–Fri with valid shift, worked >= min_hours
  half_day        Mon–Fri with valid shift, worked >= half_day_min but < min_hours
  absent          Mon–Fri, no valid shift (or suspicious/invalid only)
  saturday        Saturday with valid shift
  saturday_off    Saturday, no valid shift
  sunday          Sunday with valid shift AND sat+mon both present
  sunday_isolated Sunday with valid shift BUT sat OR mon absent (OT-rate only)
  sunday_off      Sunday, no shift, sat+mon both present → no deduction
  sunday_absent   Sunday, no shift, sat OR mon absent → counts as absent
  holiday         Admin-marked holiday → NEVER deducts salary
  error           Data error (e.g. raw_h < 0 producing negative worked_h)
"""

import json
import logging
import os
import shutil
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, date
from collections import defaultdict
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("synix.attendance")

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS  (all configurable)
# ─────────────────────────────────────────────────────────────────────────────
LUNCH_CUT_H          = 0.5    # 30 min lunch automatically deducted per shift
FULL_DAY_H           = 8.0    # normal duty hours after lunch cut
HALF_DAY_MIN_H       = 4.0    # minimum hours worked for half-day credit
DUPLICATE_THRESH_MIN = 2      # punches within 2 minutes = duplicate → ignore second
SUSPICIOUS_H         = 16.0   # shifts longer than 16h flagged as suspicious
INVALID_H            = 20.0   # shifts longer than 20h = suspicious + needs admin review
IMPOSSIBLE_H         = 24.0   # shifts longer than 24h = impossible clock error → discard


# ─────────────────────────────────────────────────────────────────────────────
#  ENUMS  (string-based for clean JSON serialisation)
# ─────────────────────────────────────────────────────────────────────────────
class ShiftState(str, Enum):
    VALID      = "valid"
    INVALID    = "invalid"      # missing OUT or negative duration
    SUSPICIOUS = "suspicious"   # duration > SUSPICIOUS_H, needs review
    APPROVED   = "approved"     # admin manually approved a suspicious shift
    ORPHAN_OUT = "orphan_out"   # OUT punch with no preceding IN → ignored


class DayStatus(str, Enum):
    PRESENT         = "present"
    HALF_DAY        = "half_day"
    ABSENT          = "absent"
    SATURDAY        = "saturday"
    SATURDAY_OFF    = "saturday_off"
    SUNDAY          = "sunday"
    SUNDAY_ISOLATED = "sunday_isolated"   # sat or mon absent, worked → OT only
    SUNDAY_OFF      = "sunday_off"        # sun not worked, sat+mon present
    SUNDAY_ABSENT   = "sunday_absent"     # sun not worked, sat or mon absent
    HOLIDAY         = "holiday"           # admin-marked, never deducts
    ERROR           = "error"             # data integrity problem


# ─────────────────────────────────────────────────────────────────────────────
#  DATA FACTORY FUNCTIONS  (plain dicts for JSON compatibility)
# ─────────────────────────────────────────────────────────────────────────────
def make_shift(
    in_dt: datetime,
    out_dt: Optional[datetime],
    state: ShiftState,
    raw_h: float = 0.0,
    worked_h: float = 0.0,
    ot_h: float = 0.0,
    note: str = "",
) -> dict:
    """
    Create a standardised shift record dict.

    Fields
    ------
    in       : ISO datetime string or None
    out      : ISO datetime string or None
    in_date  : YYYY-MM-DD of IN punch (shift ownership date)
    state    : ShiftState value
    raw_h    : total hours from IN to OUT (before lunch deduction)
    worked_h : net hours after lunch deduction (capped at FULL_DAY_H for base)
    ot_h     : hours above FULL_DAY_H
    note     : human-readable explanation of any decision
    """
    return {
        "in":       in_dt.strftime("%Y-%m-%d %H:%M:%S") if in_dt else None,
        "out":      out_dt.strftime("%Y-%m-%d %H:%M:%S") if out_dt else None,
        "in_date":  in_dt.strftime("%Y-%m-%d") if in_dt else None,
        "state":    state.value,
        "raw_h":    round(raw_h, 4),
        "worked_h": round(worked_h, 4),
        "ot_h":     round(ot_h, 4),
        "note":     note,
    }


def make_day_record(
    date_str: str,
    status: DayStatus,
    worked_h: float = 0.0,
    ot_h: float = 0.0,
    shifts: Optional[list] = None,
    holiday: bool = False,
    note: str = "",
    in_str: str = "-",
    out_str: str = "-",
) -> dict:
    """
    Create a standardised day record dict.
    Aggregates one or more shifts for a single calendar date.

    Fields
    ------
    date     : YYYY-MM-DD
    status   : DayStatus value
    worked_h : total net hours worked across all valid shifts
    ot_h     : total overtime hours
    in       : first IN time (HH:MM) or "-"
    out      : last OUT time (HH:MM) or "-"
    shifts   : list of raw shift dicts contributing to this day
    holiday  : True if admin-marked as holiday
    note     : any audit note
    """
    return {
        "date":     date_str,
        "status":   status.value,
        "worked_h": round(worked_h, 4),
        "ot_h":     round(ot_h, 4),
        "in":       in_str,
        "out":      out_str,
        "shifts":   shifts or [],
        "holiday":  holiday,
        "note":     note,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  AUDIT LOG
#  Every significant decision is recorded here for traceability.
# ─────────────────────────────────────────────────────────────────────────────
_audit_log: List[dict] = []


def audit(eid: str, msg: str, level: str = "info") -> None:
    """
    Append an entry to the in-memory audit log and emit to Python logging.

    Parameters
    ----------
    eid   : employee ID the event relates to
    msg   : human-readable description of the decision/event
    level : "debug" | "info" | "warning" | "error"
    """
    entry = {
        "ts":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "eid":   eid,
        "level": level,
        "msg":   msg,
    }
    _audit_log.append(entry)
    log_fn = getattr(logger, level, logger.info)
    log_fn(f"[{eid}] {msg}")


def get_audit_log() -> List[dict]:
    """Return a copy of the full audit log."""
    return list(_audit_log)


def clear_audit_log() -> None:
    """Clear all audit log entries (e.g. at start of new file processing)."""
    _audit_log.clear()


def export_audit_log(filepath: str) -> None:
    """
    Write audit log to a JSON file atomically (temp → rename).
    Safe even if the program crashes during write.
    """
    _atomic_json_save(filepath, _audit_log)
    logger.info(f"Audit log exported: {filepath}")


# ─────────────────────────────────────────────────────────────────────────────
#  UTILITY HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _parse_dt(s: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM:SS' string to datetime. Raises ValueError on bad input."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _date_str(dt: datetime) -> str:
    """Return YYYY-MM-DD string from datetime."""
    return dt.strftime("%Y-%m-%d")


def _time_str(dt: datetime) -> str:
    """Return HH:MM string from datetime."""
    return dt.strftime("%H:%M")


def _weekday(date_str: str) -> int:
    """Return weekday integer: 0=Monday, 5=Saturday, 6=Sunday."""
    return datetime.strptime(date_str, "%Y-%m-%d").weekday()


def _month_full_range(any_date_str: str) -> List[str]:
    """
    Return all date strings for the full calendar month containing any_date_str.

    Example: "2025-05-15" → ["2025-05-01", ..., "2025-05-31"]
    """
    first = datetime.strptime(any_date_str, "%Y-%m-%d").replace(day=1)
    if first.month == 12:
        last = first.replace(year=first.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        last = first.replace(month=first.month + 1, day=1) - timedelta(days=1)
    result = []
    d = first
    while d <= last:
        result.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return result


def _month_days(month_label: str) -> int:
    """
    Return actual number of calendar days in the month.

    Parameters
    ----------
    month_label : "May 2025" format

    Returns
    -------
    int — 28, 29, 30, or 31
    """
    try:
        first = datetime.strptime(month_label, "%B %Y").replace(day=1)
        if first.month == 12:
            last = first.replace(year=first.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            last = first.replace(month=first.month + 1, day=1) - timedelta(days=1)
        return last.day
    except Exception:
        return 30  # safe default


def _is_31st_of_31day_month(date_str: str, month_label: str) -> bool:
    """
    Return True if date_str is the 31st day of a 31-day month.
    Used for payroll 31-day month rule: 31st absent = no extra deduction.
    """
    return (
        _month_days(month_label) == 31 and
        int(date_str.split("-")[2]) == 31
    )


# ─────────────────────────────────────────────────────────────────────────────
#  ATOMIC JSON SAVE  (req #20)
#  Write → temp file → rename → backup. Never corrupts existing data.
# ─────────────────────────────────────────────────────────────────────────────
def _atomic_json_save(filepath: str, data: object, backup: bool = True) -> None:
    """
    Atomically save data to filepath as JSON.

    Steps:
      1. Write JSON to a temp file in the same directory.
      2. If a backup path exists, rename existing to .bak.
      3. Rename temp file → filepath.

    This ensures the file is either complete or the old version is intact;
    a crash cannot leave a half-written file.

    Parameters
    ----------
    filepath : destination path
    data     : JSON-serialisable object
    backup   : if True, keep .bak of the previous version
    """
    dir_name  = os.path.dirname(os.path.abspath(filepath))
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str, ensure_ascii=False)
        # Backup previous version
        if backup and os.path.exists(filepath):
            shutil.copy2(filepath, filepath + ".bak")
        # Atomic rename (POSIX: atomic; Windows: best-effort)
        os.replace(tmp_path, filepath)
        logger.debug(f"Atomic save OK: {filepath}")
    except Exception as exc:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise exc


def _safe_json_load(filepath: str) -> object:
    """
    Load JSON from filepath with corruption recovery.
    If main file is corrupt, tries .bak file.
    Returns None if both fail.
    """
    for path in (filepath, filepath + ".bak"):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"JSON load failed for {path}: {e}")
    logger.error(f"All load attempts failed for {filepath}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1 — RAW PUNCH INGESTION
#  Read .dat file, parse, deduplicate, return sorted punch lists.
# ─────────────────────────────────────────────────────────────────────────────
def ingest_dat(filepath: str) -> Tuple[Dict[str, List[datetime]], str]:
    """
    Parse a biometric .dat file into sorted, deduplicated punch lists.

    File Format (each line, tab- or space-delimited)
    -------------------------------------------------
        EmployeeID    YYYY-MM-DD HH:MM:SS    [extra fields ignored]
    OR  EmployeeID    YYYY-MM-DD    HH:MM:SS  (split across fields)

    Processing Steps
    ----------------
    1. Parse each line, skip invalid/blank lines.
    2. Sort each employee's punches chronologically.
    3. Deduplicate: remove any punch within DUPLICATE_THRESH_MIN of the previous.

    Returns
    -------
    punches     : { eid: [datetime, ...] sorted ascending }
    month_label : "May 2025"   (derived from earliest punch date)
    """
    raw: Dict[str, List[datetime]] = defaultdict(list)
    month_dates: set = set()
    parse_errors = 0
    lines_read   = 0

    logger.info(f"ingest_dat: reading {filepath}")
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for lineno, raw_line in enumerate(f, 1):
            lines_read += 1
            line = raw_line.strip()
            if not line:
                continue

            # Support both tab-separated and space-separated formats
            parts = (
                [p.strip() for p in line.split("\t")]
                if "\t" in line
                else line.split()
            )
            if len(parts) < 2:
                continue

            try:
                eid = parts[0].strip()
                if not eid:
                    continue

                # Handle date and time split across two fields
                dt_str = parts[1].strip()
                if len(dt_str) == 10:  # "YYYY-MM-DD" only → time is in parts[2]
                    if len(parts) < 3:
                        continue
                    dt_str = dt_str + " " + parts[2].strip()

                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                raw[eid].append(dt)
                month_dates.add(_date_str(dt))

            except (ValueError, IndexError):
                parse_errors += 1
                logger.debug(f"Line {lineno}: parse error → {raw_line.rstrip()!r}")
                continue

    if parse_errors:
        logger.warning(f"ingest_dat: {parse_errors}/{lines_read} lines failed to parse")

    if not month_dates:
        logger.warning("ingest_dat: no valid punches found")
        return {}, ""

    # Step 2+3: Sort and deduplicate each employee's punch list
    deduped: Dict[str, List[datetime]] = {}
    for eid, times in raw.items():
        times.sort()
        clean: List[datetime] = []
        dup_count = 0
        for t in times:
            if clean and (t - clean[-1]).total_seconds() < DUPLICATE_THRESH_MIN * 60:
                audit(eid,
                      f"Duplicate punch removed: {t} "
                      f"(within {DUPLICATE_THRESH_MIN}min of {clean[-1]})",
                      "debug")
                dup_count += 1
                continue
            clean.append(t)
        if dup_count:
            audit(eid, f"Total duplicates removed: {dup_count}", "info")
        deduped[eid] = clean

    all_dates   = sorted(month_dates)
    month_label = datetime.strptime(all_dates[0], "%Y-%m-%d").strftime("%B %Y")
    total_punches = sum(len(v) for v in deduped.values())
    logger.info(
        f"ingest_dat complete: {len(deduped)} employees, "
        f"{total_punches} punches, month={month_label}"
    )
    return deduped, month_label


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2 — STATE MACHINE SESSION BUILDER  (req #6, #7, #8, #9, #10)
#  Converts sorted punch list into closed shift records.
# ─────────────────────────────────────────────────────────────────────────────
def build_sessions(eid: str, punches: List[datetime]) -> List[dict]:
    """
    Process a chronologically-sorted punch list through the IN/OUT state machine.

    State Machine Rules
    -------------------
    Initial state: OUTSIDE

    OUTSIDE + punch:
        → Record as IN timestamp, transition to INSIDE

    INSIDE + punch:
        Case A — gap < DUPLICATE_THRESH_MIN:
            Duplicate. Ignore. Stay INSIDE.

        Case B — different calendar day AND gap > IMPOSSIBLE_H:
            Previous shift had no OUT → mark INVALID (that day = absent).
            Current punch starts a NEW IN session (req #8).
            *** This is NOT treated as an OUT ***

        Case C — all other gaps (including night shifts crossing midnight):
            Valid OUT. Close shift. Compute duration. Classify. Transition to OUTSIDE.

    Night Shifts (req #10)
    ----------------------
    Example: IN @ 20:00 Sat, OUT @ 06:00 Sun.
    The state machine handles this naturally — OUT on next day is a valid Case C
    as long as gap <= IMPOSSIBLE_H (24h). No special night-shift logic required.

    Multiple Shifts Per Day (req #9)
    ---------------------------------
    Example: 08:00 IN → 12:00 OUT → 13:00 IN → 17:00 OUT.
    Both shifts are captured. Each OUT closes the current session;
    the next punch opens a new one.

    End of Data
    -----------
    If still INSIDE when punches run out, shift is INVALID (missing OUT).

    Parameters
    ----------
    eid     : employee ID (for audit logging)
    punches : sorted list of datetime objects (already deduplicated)

    Returns
    -------
    List of shift dicts (see make_shift)
    """
    shifts: List[dict] = []

    # State variables
    state_inside: bool            = False
    current_in:   Optional[datetime] = None

    for punch in punches:
        if not state_inside:
            # ── OUTSIDE: this punch is always an IN ──────────────────────────
            state_inside = True
            current_in   = punch
            audit(eid, f"IN  @ {punch}", "debug")

        else:
            # ── INSIDE: this punch is a potential OUT ────────────────────────
            gap_h       = (punch - current_in).total_seconds() / 3600.0
            new_day     = _date_str(punch) != _date_str(current_in)

            # ── Case B: new calendar day + gap beyond 24h = missing OUT ──────
            if new_day and gap_h > IMPOSSIBLE_H:
                audit(
                    eid,
                    f"INVALID shift: IN={current_in}, no OUT detected before {punch} "
                    f"(gap={gap_h:.1f}h > {IMPOSSIBLE_H}h). "
                    f"Day {_date_str(current_in)} → ABSENT. "
                    f"New IN session starts at {punch}.",
                    "warning",
                )
                shifts.append(make_shift(
                    current_in, None,
                    ShiftState.INVALID,
                    note=(
                        f"Missing OUT — next punch arrived {gap_h:.1f}h later "
                        f"on a different day. Day = ABSENT."
                    ),
                ))
                # Current punch becomes a new IN (NOT treated as OUT)
                current_in = punch
                audit(eid, f"New IN @ {punch} (after invalid shift)", "debug")
                continue  # Stay INSIDE with new current_in

            # ── Case C: valid OUT (including cross-midnight night shifts) ─────
            raw_h = gap_h
            audit(eid, f"OUT @ {punch}  raw={raw_h:.2f}h", "debug")

            # Classify by duration
            if raw_h < 0:
                # Negative duration → clock error (system clock jump, DST, etc.)
                audit(eid, f"Clock error: negative duration {raw_h:.2f}h → INVALID", "error")
                shifts.append(make_shift(
                    current_in, punch, ShiftState.INVALID,
                    note=f"Negative duration {raw_h:.2f}h — clock error or DST issue",
                ))

            elif raw_h > IMPOSSIBLE_H:
                # > 24h: impossible even with a missed punch; discard entirely
                audit(eid, f"Impossible shift {raw_h:.1f}h (>24h) → INVALID", "error")
                shifts.append(make_shift(
                    current_in, punch, ShiftState.INVALID,
                    note=f"Duration {raw_h:.1f}h exceeds 24h — physically impossible, discarded",
                ))

            elif raw_h >= INVALID_H:
                # 20h–24h: suspicious, needs admin review before payroll
                worked_h = max(0.0, raw_h - LUNCH_CUT_H)
                ot_h     = max(0.0, worked_h - FULL_DAY_H)
                audit(eid, f"Suspicious shift {raw_h:.1f}h (>{INVALID_H}h) → admin review required", "warning")
                shifts.append(make_shift(
                    current_in, punch, ShiftState.SUSPICIOUS,
                    raw_h=raw_h, worked_h=worked_h, ot_h=ot_h,
                    note=f"Duration {raw_h:.1f}h > {INVALID_H}h — ADMIN REVIEW REQUIRED before payroll",
                ))

            elif raw_h >= SUSPICIOUS_H:
                # 16h–20h: flagged with warning but still counted
                worked_h = max(0.0, raw_h - LUNCH_CUT_H)
                ot_h     = max(0.0, worked_h - FULL_DAY_H)
                audit(eid, f"Long shift {raw_h:.1f}h (>{SUSPICIOUS_H}h) → SUSPICIOUS (flagged)", "warning")
                shifts.append(make_shift(
                    current_in, punch, ShiftState.SUSPICIOUS,
                    raw_h=raw_h, worked_h=worked_h, ot_h=ot_h,
                    note=f"Long shift {raw_h:.1f}h > {SUSPICIOUS_H}h — flagged, verify with employee",
                ))

            elif raw_h < 0.1:
                # < 6 minutes: probable mis-scan or biometric error
                audit(eid, f"Very short shift {raw_h*60:.0f}min — probable mis-scan → INVALID", "warning")
                shifts.append(make_shift(
                    current_in, punch, ShiftState.INVALID,
                    note=f"Duration {raw_h*60:.0f}min < 6min — probable mis-scan, discarded",
                ))

            else:
                # Normal valid shift: compute worked hours and OT
                worked_h = max(0.0, raw_h - LUNCH_CUT_H)
                ot_h     = max(0.0, worked_h - FULL_DAY_H)
                shifts.append(make_shift(
                    current_in, punch, ShiftState.VALID,
                    raw_h=raw_h, worked_h=worked_h, ot_h=ot_h,
                ))

            # Transition back to OUTSIDE
            state_inside = False
            current_in   = None

    # ── End of punch list: open session = missing OUT ────────────────────────
    if state_inside and current_in is not None:
        audit(
            eid,
            f"End of data: open IN @ {current_in} with no OUT → INVALID, day ABSENT",
            "warning",
        )
        shifts.append(make_shift(
            current_in, None, ShiftState.INVALID,
            note="Missing OUT — end of data reached without OUT punch",
        ))

    return shifts


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3 — CROSS-MONTH SHIFT SPLIT  (req #18)
#  Splits shifts that span a month boundary at midnight.
# ─────────────────────────────────────────────────────────────────────────────
def split_cross_month_shift(shift: dict) -> List[dict]:
    """
    If a shift's IN and OUT fall in different calendar months, split it
    proportionally at the month boundary (midnight of last day of IN's month).

    Example
    -------
    IN:  May 31 20:00
    OUT: Jun  1 08:00
    → Part 1: May 31 20:00 → May 31 23:59:59  (4h in May)
    → Part 2: Jun  1 00:00 → Jun  1 08:00    (8h in June)

    Hours and OT are split proportionally (not simply halved).
    Lunch deduction is prorated based on each part's share of total raw hours.

    Only VALID, SUSPICIOUS, and APPROVED shifts are split.
    INVALID / ORPHAN_OUT shifts are returned unchanged.

    Returns
    -------
    List of 1 or 2 shift dicts.
    """
    if not shift["in"] or not shift["out"]:
        return [shift]

    # Only split shifts that will actually contribute to payroll
    if shift["state"] not in (
        ShiftState.VALID.value,
        ShiftState.SUSPICIOUS.value,
        ShiftState.APPROVED.value,
    ):
        return [shift]

    in_dt  = _parse_dt(shift["in"])
    out_dt = _parse_dt(shift["out"])

    # Same month → no split needed
    if (in_dt.year, in_dt.month) == (out_dt.year, out_dt.month):
        return [shift]

    # Find the exact boundary: midnight at start of OUT's month
    if in_dt.month == 12:
        boundary = datetime(in_dt.year + 1, 1, 1)
    else:
        boundary = datetime(in_dt.year, in_dt.month + 1, 1)

    total_raw_h = shift["raw_h"]
    h_before    = (boundary - in_dt).total_seconds() / 3600.0
    h_after     = (out_dt - boundary).total_seconds() / 3600.0

    # Prorate lunch deduction by proportion of each part
    if total_raw_h > 0:
        lunch_before = LUNCH_CUT_H * (h_before / total_raw_h)
        lunch_after  = LUNCH_CUT_H * (h_after  / total_raw_h)
    else:
        lunch_before = lunch_after = 0.0

    worked_before = max(0.0, h_before - lunch_before)
    ot_before     = max(0.0, worked_before - FULL_DAY_H)
    worked_after  = max(0.0, h_after  - lunch_after)
    ot_after      = max(0.0, worked_after  - FULL_DAY_H)

    logger.info(
        f"Cross-month shift split: {in_dt} → {out_dt} | "
        f"Before boundary: {h_before:.2f}h | After: {h_after:.2f}h"
    )

    part1 = make_shift(
        in_dt,
        boundary - timedelta(seconds=1),  # last second of IN's month
        ShiftState(shift["state"]),
        raw_h=h_before, worked_h=worked_before, ot_h=ot_before,
        note=f"Cross-month split — {in_dt.strftime('%B')} portion ({h_before:.2f}h raw)",
    )
    part2 = make_shift(
        boundary,
        out_dt,
        ShiftState(shift["state"]),
        raw_h=h_after, worked_h=worked_after, ot_h=ot_after,
        note=f"Cross-month split — {out_dt.strftime('%B')} portion ({h_after:.2f}h raw)",
    )
    return [part1, part2]


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4 — SHIFT → DAY AGGREGATION  (req #11)
#  Groups shifts by IN date, computes per-day status and totals.
# ─────────────────────────────────────────────────────────────────────────────
def aggregate_days(
    eid: str,
    shifts: List[dict],
    full_range: List[str],
    holidays: Optional[Dict[str, bool]] = None,
    half_day_min_h: float = HALF_DAY_MIN_H,
) -> Dict[str, dict]:
    """
    Map each shift to its IN date (shift ownership rule, req #11).
    For each date in full_range, determine day status and accumulate hours.

    Night shift ownership example (req #11):
        Sat 20:00 IN → Sun 08:00 OUT → this shift belongs to SATURDAY

    Parameters
    ----------
    eid            : employee ID
    shifts         : list of shift dicts from build_sessions / split_cross_month_shift
    full_range     : all date strings for the target month
    holidays       : { date_str: True } for admin-marked holidays
    half_day_min_h : minimum hours for half-day credit (configurable)

    Returns
    -------
    { date_str: day_record_dict }
    """
    if holidays is None:
        holidays = {}

    # ── Group valid/approved shifts by IN date ────────────────────────────────
    # Only VALID, APPROVED, and SUSPICIOUS-APPROVED shifts contribute to presence.
    # INVALID and SUSPICIOUS (unapproved) make the day ABSENT.
    shifts_by_date: Dict[str, List[dict]] = defaultdict(list)
    invalid_dates:  set = set()   # dates where an INVALID shift was the only shift

    for s in shifts:
        in_date = s.get("in_date")
        if not in_date:
            continue
        state = s["state"]

        if state in (ShiftState.VALID.value, ShiftState.APPROVED.value):
            shifts_by_date[in_date].append(s)
        elif state == ShiftState.INVALID.value:
            # Track invalid, but don't add to shifts_by_date
            invalid_dates.add(in_date)
            audit(eid, f"INVALID shift on {in_date} — day may become ABSENT", "debug")
        elif state == ShiftState.SUSPICIOUS.value:
            # Suspicious unapproved: still add (admin can review; payroll will skip if not approved)
            # We mark them separately so UI can highlight
            shifts_by_date[in_date].append(s)
            audit(eid, f"SUSPICIOUS shift on {in_date} included pending admin review", "warning")
        # ORPHAN_OUT is completely ignored

    result: Dict[str, dict] = {}

    for date_str in full_range:
        # ── Holiday check first — overrides everything ──────────────────────
        if holidays.get(date_str, False):
            result[date_str] = make_day_record(
                date_str, DayStatus.HOLIDAY,
                holiday=True, note="Company holiday — no salary deduction",
            )
            audit(eid, f"{date_str} → HOLIDAY (admin-marked)", "info")
            continue

        dow    = _weekday(date_str)
        is_sun = (dow == 6)
        is_sat = (dow == 5)

        day_shifts = shifts_by_date.get(date_str, [])

        if day_shifts:
            # ── Day has at least one valid/suspicious shift ──────────────────
            # Filter to only shifts that count for payroll (valid + approved)
            payroll_shifts = [
                s for s in day_shifts
                if s["state"] in (ShiftState.VALID.value, ShiftState.APPROVED.value)
            ]
            suspicious_only = (len(payroll_shifts) == 0 and len(day_shifts) > 0)

            if suspicious_only:
                # Only suspicious unreviewed shifts → treat as absent pending review
                status = (
                    DayStatus.SATURDAY_OFF if is_sat else
                    DayStatus.SUNDAY_OFF   if is_sun else
                    DayStatus.ABSENT
                )
                result[date_str] = make_day_record(
                    date_str, status,
                    shifts=day_shifts,
                    note="Only suspicious shifts (pending admin review) — treated as absent for now",
                )
                audit(eid, f"{date_str} → {status.value} (suspicious only, pending review)", "warning")
                continue

            # Aggregate hours from valid/approved shifts
            total_worked_h = sum(s["worked_h"] for s in payroll_shifts)
            total_ot_h     = sum(s["ot_h"]     for s in payroll_shifts)

            # Determine IN/OUT display strings (first IN, last OUT)
            all_in_dts  = [_parse_dt(s["in"])  for s in payroll_shifts if s.get("in")]
            all_out_dts = [_parse_dt(s["out"]) for s in payroll_shifts if s.get("out")]
            in_str  = _time_str(min(all_in_dts))  if all_in_dts  else "-"
            out_str = _time_str(max(all_out_dts)) if all_out_dts else "-"

            # Classify day status by day-of-week and hours worked
            if is_sun:
                # Sunday presence: always 'sunday' (Sunday rules applied in step 5)
                status = DayStatus.SUNDAY
            elif is_sat:
                status = DayStatus.SATURDAY
            else:
                # Weekday
                if total_worked_h >= FULL_DAY_H:
                    status = DayStatus.PRESENT
                elif total_worked_h >= half_day_min_h:
                    status = DayStatus.HALF_DAY
                    audit(eid, f"{date_str} → HALF_DAY ({total_worked_h:.2f}h worked)", "info")
                elif total_worked_h > 0:
                    # Worked but less than half-day minimum
                    status = DayStatus.ABSENT
                    audit(
                        eid,
                        f"{date_str} worked only {total_worked_h:.2f}h "
                        f"(< half-day {half_day_min_h}h) → ABSENT",
                        "warning",
                    )
                else:
                    status = DayStatus.ABSENT

            result[date_str] = make_day_record(
                date_str, status,
                worked_h=total_worked_h, ot_h=total_ot_h,
                shifts=day_shifts,
                in_str=in_str, out_str=out_str,
            )

        else:
            # ── No valid shifts for this date ──────────────────────────────
            if is_sun:
                status = DayStatus.SUNDAY_OFF    # Sunday rules applied later
            elif is_sat:
                status = DayStatus.SATURDAY_OFF
            else:
                status = DayStatus.ABSENT

            invalid_note = ""
            if date_str in invalid_dates:
                invalid_note = " (had INVALID shift — missing OUT)"
                audit(eid, f"{date_str} → {status.value}{invalid_note}", "info")

            result[date_str] = make_day_record(
                date_str, status,
                note=invalid_note.strip(),
            )

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 5 — SUNDAY RULE APPLICATION  (req #12)
#  Must run AFTER aggregate_days.
# ─────────────────────────────────────────────────────────────────────────────
def apply_sunday_rules(eid: str, days: Dict[str, dict]) -> Dict[str, dict]:
    """
    Apply Sunday salary rules based on adjacent Saturday and Monday status.

    Rule (req #12)
    --------------
    For each Sunday:
        sat_absent = previous day (Saturday) is absent OR saturday_off OR half_day
        mon_absent = next day (Monday) is absent

    If sat_absent OR mon_absent:
        sunday_off  → sunday_absent    (no work, counts toward deduction)
        sunday      → sunday_isolated  (worked, but OT-rate only, no base salary)

    If sat_absent AND mon_absent are BOTH False:
        sunday_off stays sunday_off    (no deduction, no bonus — normal rest day)
        sunday stays sunday            (full bonus: base + OT-rate)

    Important: this operates on the sorted date list, so it correctly handles
    month-start Sundays (no previous Saturday in data) and month-end Sundays
    (no following Monday in data).
    """
    date_list = sorted(days.keys())

    for i, ds in enumerate(date_list):
        if _weekday(ds) != 6:   # Only process Sundays
            continue

        prev_ds = date_list[i - 1] if i > 0 else None
        next_ds = date_list[i + 1] if i < len(date_list) - 1 else None

        # Saturday absent if: missing from data, absent, saturday_off, or half_day
        # Saturday PRESENT (DayStatus.SATURDAY) = not absent → Sunday gets paid-off benefit
        # Sunday_absent / sunday_off / holiday on Saturday → also treated as present for Sunday rule
        SAT_PRESENT_STATUSES = {
            DayStatus.SATURDAY.value,
            DayStatus.HOLIDAY.value,
        }
        sat_absent = (
            prev_ds is None or
            days[prev_ds]["status"] not in SAT_PRESENT_STATUSES
        )
        # Monday absent if: missing or absent (half_day on Monday also counts as absent for this rule)
        MON_PRESENT_STATUSES = {
            DayStatus.PRESENT.value,
            DayStatus.HOLIDAY.value,
            DayStatus.HALF_DAY.value,   # half-day Monday = still came in, Sunday benefit applies
        }
        mon_absent = (
            next_ds is None or
            days[next_ds]["status"] not in MON_PRESENT_STATUSES
        )

        cur_status = days[ds]["status"]

        if sat_absent or mon_absent:
            reason = f"sat_absent={sat_absent}, mon_absent={mon_absent}"
            if cur_status == DayStatus.SUNDAY_OFF.value:
                days[ds]["status"] = DayStatus.SUNDAY_ABSENT.value
                days[ds]["note"]   = f"Sunday → ABSENT ({reason}) — no work, counts as deduction"
                audit(eid, f"{ds} Sunday OFF → SUNDAY_ABSENT ({reason})", "info")

            elif cur_status == DayStatus.SUNDAY.value:
                days[ds]["status"] = DayStatus.SUNDAY_ISOLATED.value
                days[ds]["note"]   = f"Sunday worked → ISOLATED ({reason}) — OT-rate only, no base salary"
                audit(eid, f"{ds} Sunday WORKED → SUNDAY_ISOLATED ({reason})", "info")

        else:
            # Both sat and mon present → Sunday is a proper bonus day
            if cur_status == DayStatus.SUNDAY.value:
                audit(eid, f"{ds} Sunday WORKED → SUNDAY (full bonus: base + OT)", "debug")
            elif cur_status == DayStatus.SUNDAY_OFF.value:
                audit(eid, f"{ds} Sunday OFF → SUNDAY_OFF (no deduction, no bonus)", "debug")

    return days


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN PIPELINE
#  Orchestrates all 5 steps for a complete .dat file.
# ─────────────────────────────────────────────────────────────────────────────
def process_dat_file(
    filepath: str,
    existing_holidays: Optional[Dict[str, Dict[str, bool]]] = None,
    half_day_min_h: float = HALF_DAY_MIN_H,
) -> Tuple[Dict[str, Dict[str, dict]], str]:
    """
    Full attendance processing pipeline for one .dat file.

    Pipeline
    --------
    1. ingest_dat   → parse + deduplicate punches
    2. build_sessions → state machine → shift list per employee
    3. split_cross_month_shift → split any shifts spanning month boundary
    4. aggregate_days → group shifts by IN date, determine day status
    5. apply_sunday_rules → adjust Sunday statuses based on neighbours

    Parameters
    ----------
    filepath          : path to .dat file
    existing_holidays : { eid: { date_str: bool } } from DataStore
    half_day_min_h    : minimum hours for half-day (configurable, default 4h)

    Returns
    -------
    attendance  : { eid: { date_str: day_record } }
    month_label : "May 2025"
    """
    if existing_holidays is None:
        existing_holidays = {}

    # ── Step 1: Ingest ────────────────────────────────────────────────────────
    punches, month_label = ingest_dat(filepath)
    if not punches:
        logger.error("process_dat_file: no valid punches found — aborting")
        return {}, ""

    # Compute full date range for this month
    all_dates  = sorted({_date_str(dt) for times in punches.values() for dt in times})
    full_range = _month_full_range(all_dates[0])

    result: Dict[str, Dict[str, dict]] = {}

    for eid, times in punches.items():
        logger.info(f"Processing employee {eid}: {len(times)} punches")

        # ── Step 2: State machine → shifts ───────────────────────────────────
        shifts = build_sessions(eid, times)
        audit(eid, f"build_sessions produced {len(shifts)} shifts", "info")

        # ── Step 3: Cross-month split ─────────────────────────────────────────
        all_shifts: List[dict] = []
        for s in shifts:
            parts = split_cross_month_shift(s)
            all_shifts.extend(parts)
            if len(parts) > 1:
                audit(eid, f"Cross-month shift split: {s['in']} → {s['out']}", "info")

        # Retrieve per-employee holiday markers
        emp_holidays = existing_holidays.get(str(eid), {})

        # ── Step 4: Aggregate to day records ──────────────────────────────────
        days = aggregate_days(eid, all_shifts, full_range, emp_holidays, half_day_min_h)

        # ── Step 5: Sunday rules ──────────────────────────────────────────────
        days = apply_sunday_rules(eid, days)

        result[eid] = days

    logger.info(
        f"process_dat_file complete: {len(result)} employees, "
        f"month={month_label}, range={full_range[0]}→{full_range[-1]}"
    )
    return result, month_label


# ─────────────────────────────────────────────────────────────────────────────
#  ADMIN OPERATIONS  (req #24)
# ─────────────────────────────────────────────────────────────────────────────
def mark_holiday(
    attendance: Dict[str, Dict[str, dict]],
    date_str: str,
    is_holiday: bool = True,
) -> Dict[str, Dict[str, dict]]:
    """
    Mark a date as a company holiday for ALL employees in attendance.
    Holiday dates never deduct salary regardless of presence/absence.

    Parameters
    ----------
    attendance : full attendance dict { eid: { date_str: day_record } }
    date_str   : "YYYY-MM-DD"
    is_holiday : True to mark, False to unmark

    Returns
    -------
    Modified attendance dict (same object, mutated in place)
    """
    for eid in attendance:
        if date_str in attendance[eid]:
            rec = attendance[eid][date_str]
            rec["holiday"] = is_holiday
            if is_holiday:
                rec["status"] = DayStatus.HOLIDAY.value
                rec["note"]   = "Company holiday — admin marked"
            else:
                # Restore to absent/off when unmarked (engine re-run will fix properly)
                rec["status"] = DayStatus.ABSENT.value
                rec["note"]   = "Holiday unmarked by admin"
            audit(
                eid,
                f"{date_str} {'marked as HOLIDAY' if is_holiday else 'holiday UNMARKED'}",
                "info",
            )
    return attendance


def approve_suspicious_shift(
    attendance: Dict[str, Dict[str, dict]],
    eid: str,
    date_str: str,
) -> bool:
    """
    Admin approves all suspicious shifts for a specific employee+date.
    Approved shifts are counted in payroll normally.
    Re-applies Sunday rules after approval since a newly-present Saturday or
    Monday will affect the adjacent Sunday's paid-off status.

    Returns True if any suspicious shifts were found and approved.
    """
    if eid not in attendance or date_str not in attendance[eid]:
        return False
    day    = attendance[eid][date_str]
    found  = False
    for s in day.get("shifts", []):
        if s["state"] == ShiftState.SUSPICIOUS.value:
            s["state"] = ShiftState.APPROVED.value
            s["note"] += " [Admin approved]"
            found = True
    if found:
        audit(eid, f"Suspicious shift(s) on {date_str} approved by admin", "info")
        # Re-compute day status now that shifts are approved
        payroll_shifts = [
            sh for sh in day["shifts"]
            if sh["state"] in (ShiftState.VALID.value, ShiftState.APPROVED.value)
        ]
        if payroll_shifts:
            day["worked_h"] = sum(s["worked_h"] for s in payroll_shifts)
            day["ot_h"]     = sum(s["ot_h"]     for s in payroll_shifts)
            dow = _weekday(date_str)
            if dow == 6:
                day["status"] = DayStatus.SUNDAY.value
            elif dow == 5:
                day["status"] = DayStatus.SATURDAY.value
            else:
                day["status"] = DayStatus.PRESENT.value
        # Re-apply Sunday rules for this employee — approval of a Saturday/Monday
        # can change the adjacent Sunday's paid-off eligibility
        attendance[eid] = apply_sunday_rules(eid, attendance[eid])
    return found


def manual_override(
    attendance: Dict[str, Dict[str, dict]],
    eid: str,
    date_str: str,
    new_status: str,
    worked_h: float = 0.0,
    ot_h: float = 0.0,
    note: str = "",
) -> bool:
    """
    Admin manually overrides attendance for a specific employee+date.
    Records old status in note for audit trail.

    Returns True if the override was applied.
    """
    if eid not in attendance or date_str not in attendance[eid]:
        logger.warning(f"manual_override: {eid}/{date_str} not found")
        return False
    old_status = attendance[eid][date_str]["status"]
    attendance[eid][date_str].update({
        "status":   new_status,
        "worked_h": round(worked_h, 4),
        "ot_h":     round(ot_h, 4),
        "note":     f"[MANUAL OVERRIDE] was={old_status} → {new_status}. {note}",
    })
    audit(
        eid,
        f"Manual override {date_str}: {old_status} → {new_status}  "
        f"worked={worked_h}h ot={ot_h}h",
        "info",
    )
    # Re-apply Sunday rules: overriding a Saturday or Monday status
    # can change the adjacent Sunday's paid-off eligibility
    attendance[eid] = apply_sunday_rules(eid, attendance[eid])
    return True


def add_manual_punch(
    attendance: Dict[str, Dict[str, dict]],
    eid: str,
    date_str: str,
    in_time_str: str,
    out_time_str: str,
    note: str = "Manual punch by admin",
) -> bool:
    """
    Admin manually inserts an IN/OUT punch pair for a specific employee+date.
    Recomputes worked hours and updates day status.

    Parameters
    ----------
    in_time_str  : "HH:MM" or "HH:MM:SS"
    out_time_str : "HH:MM" or "HH:MM:SS"

    Returns True on success.
    """
    if eid not in attendance or date_str not in attendance[eid]:
        return False
    try:
        in_dt  = datetime.strptime(f"{date_str} {in_time_str}",  "%Y-%m-%d %H:%M")
    except ValueError:
        in_dt  = datetime.strptime(f"{date_str} {in_time_str}",  "%Y-%m-%d %H:%M:%S")
    try:
        out_dt = datetime.strptime(f"{date_str} {out_time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        out_dt = datetime.strptime(f"{date_str} {out_time_str}", "%Y-%m-%d %H:%M:%S")

    raw_h    = (out_dt - in_dt).total_seconds() / 3600.0
    if raw_h <= 0:
        logger.warning(f"add_manual_punch: non-positive duration {raw_h}h for {eid}/{date_str}")
        return False

    worked_h = max(0.0, raw_h - LUNCH_CUT_H)
    ot_h     = max(0.0, worked_h - FULL_DAY_H)

    new_shift = make_shift(
        in_dt, out_dt, ShiftState.VALID,
        raw_h=raw_h, worked_h=worked_h, ot_h=ot_h,
        note=note,
    )

    rec = attendance[eid][date_str]
    rec["shifts"].append(new_shift)
    rec["in"]       = _time_str(in_dt)
    rec["out"]      = _time_str(out_dt)
    rec["worked_h"] = round(rec["worked_h"] + worked_h, 4)
    rec["ot_h"]     = round(rec["ot_h"]     + ot_h,     4)

    # Update status
    dow = _weekday(date_str)
    if rec["worked_h"] >= FULL_DAY_H:
        if dow == 6:   rec["status"] = DayStatus.SUNDAY.value
        elif dow == 5: rec["status"] = DayStatus.SATURDAY.value
        else:          rec["status"] = DayStatus.PRESENT.value
    elif rec["worked_h"] >= HALF_DAY_MIN_H:
        rec["status"] = DayStatus.HALF_DAY.value
    rec["note"] = f"[Manual punch added] {note}"

    audit(eid, f"Manual punch added: {date_str} {in_time_str}→{out_time_str} ({worked_h:.2f}h)", "info")
    # Re-apply Sunday rules: a new punch on Saturday or Monday changes adjacent Sunday
    attendance[eid] = apply_sunday_rules(eid, attendance[eid])
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  UNIT-TEST STYLE SAMPLE CASES  (req #27)
# ─────────────────────────────────────────────────────────────────────────────
def run_engine_tests() -> List[str]:
    """
    Built-in unit tests for all major edge cases.
    Returns list of "[PASS] ..." or "[FAIL] ..." strings.

    Run from CLI:
        python attendance_engine.py test

    Covers
    ------
    1.  Missing OUT → INVALID shift
    2-4. Normal shift (8.5h) → VALID, correct worked_h and ot_h
    5.  Night shift (20:00→06:00) → VALID, in_date = IN day, raw_h = 10h
    6.  Double shift same day → 2 valid shifts
    7.  Duplicate punch (1 min) → removed, only 1 shift
    8.  New-day punch after unclosed IN → day1 INVALID (req #8)
    9.  Suspicious long shift (17.5h) → SUSPICIOUS
    10. Sunday absence rule: sat absent → sunday_off becomes sunday_absent
    11. Sunday isolated: sat absent + sun worked → sunday_isolated
    12. Cross-month shift splits into 2 parts
    13. Holiday mark → status=holiday, holiday=True
    14. Manual override
    15. Manual punch add
    """
    results: List[str] = []

    def check(name: str, cond: bool, got=None) -> None:
        status = "PASS" if cond else "FAIL"
        suffix = f"  [got: {got!r}]" if got is not None and not cond else ""
        results.append(f"[{status}] {name}{suffix}")

    BASE = datetime(2025, 5, 1)   # Thursday

    # ── TEST 1: Missing OUT ───────────────────────────────────────────────────
    shifts_1 = build_sessions("T01", [BASE.replace(hour=8)])
    check(
        "T01 Missing OUT → INVALID shift",
        shifts_1[0]["state"] == ShiftState.INVALID.value,
    )

    # ── TEST 2-4: Normal full day 08:00→16:30 ─────────────────────────────────
    punches_2 = [BASE.replace(hour=8), BASE.replace(hour=16, minute=30)]
    shifts_2  = build_sessions("T02", punches_2)
    check("T02 Normal 8.5h shift → VALID",
          shifts_2[0]["state"] == ShiftState.VALID.value)
    check("T02 Normal 8.5h: worked_h=8.0",
          abs(shifts_2[0]["worked_h"] - 8.0) < 0.01, shifts_2[0]["worked_h"])
    check("T02 Normal 8.5h: ot_h=0",
          abs(shifts_2[0]["ot_h"]) < 0.01, shifts_2[0]["ot_h"])

    # ── TEST 5: Night shift ───────────────────────────────────────────────────
    night_in  = BASE.replace(hour=20)
    night_out = (BASE + timedelta(days=1)).replace(hour=6)
    shifts_3  = build_sessions("T03", [night_in, night_out])
    check("T03 Night shift 20:00→06:00 → VALID",
          shifts_3[0]["state"] == ShiftState.VALID.value)
    check("T03 Night shift in_date = IN day",
          shifts_3[0]["in_date"] == BASE.strftime("%Y-%m-%d"))
    check("T03 Night shift raw_h=10h",
          abs(shifts_3[0]["raw_h"] - 10.0) < 0.01, shifts_3[0]["raw_h"])

    # ── TEST 6: Double shift same day ─────────────────────────────────────────
    punches_4 = [
        BASE.replace(hour=8),  BASE.replace(hour=12),
        BASE.replace(hour=13), BASE.replace(hour=17),
    ]
    shifts_4 = build_sessions("T04", punches_4)
    check("T04 Double shift → 2 valid shifts",
          len([s for s in shifts_4 if s["state"] == ShiftState.VALID.value]) == 2)

    # ── TEST 7: Duplicate punch ───────────────────────────────────────────────
    t1 = BASE.replace(hour=8, minute=0)
    t2 = BASE.replace(hour=8, minute=1)  # 1 min later → duplicate
    t3 = BASE.replace(hour=16, minute=30)
    clean_5 = [t1]
    for t in [t2, t3]:
        if (t - clean_5[-1]).total_seconds() >= DUPLICATE_THRESH_MIN * 60:
            clean_5.append(t)
    shifts_5 = build_sessions("T05", clean_5)
    check("T05 Duplicate punch (1min) removed → 1 shift",
          len(shifts_5) == 1 and shifts_5[0]["state"] == ShiftState.VALID.value)

    # ── TEST 8: New-day punch = new IN, not OUT (req #8) ─────────────────────
    day1_in = BASE.replace(hour=8)
    day2_in = (BASE + timedelta(days=2)).replace(hour=8)   # 2 days later (>24h)
    shifts_6 = build_sessions("T06", [day1_in, day2_in])
    check("T06 New day punch (>24h) after unclosed IN → day1 INVALID",
          shifts_6[0]["state"] == ShiftState.INVALID.value, shifts_6[0]["state"])

    # ── TEST 9: Suspicious long shift ────────────────────────────────────────
    long_in  = BASE.replace(hour=6)
    long_out = BASE.replace(hour=23, minute=30)  # 17.5h
    shifts_7 = build_sessions("T07", [long_in, long_out])
    check("T07 17.5h shift → SUSPICIOUS",
          shifts_7[0]["state"] == ShiftState.SUSPICIOUS.value, shifts_7[0]["state"])

    # ── TEST 10: Sunday absence rule (sat absent → sunday_absent) ─────────────
    sat_ds = "2025-05-03"   # Saturday
    sun_ds = "2025-05-04"   # Sunday
    mon_ds = "2025-05-05"   # Monday
    def _mk(status, wh=0):
        return {"status": status, "worked_h": wh, "ot_h": 0, "holiday": False,
                "note": "", "shifts": [], "in": "-", "out": "-", "date": ""}
    fake_10 = {
        sat_ds: _mk(DayStatus.SATURDAY_OFF.value),   # sat absent
        sun_ds: _mk(DayStatus.SUNDAY_OFF.value),
        mon_ds: _mk(DayStatus.PRESENT.value, 8),
    }
    after_10 = apply_sunday_rules("T10", fake_10)
    check("T10 Sunday rule: Sat SATURDAY_OFF → sunday_off becomes sunday_absent",
          after_10[sun_ds]["status"] == DayStatus.SUNDAY_ABSENT.value,
          after_10[sun_ds]["status"])

    # T10b: sat PRESENT (SATURDAY) → sunday_off should stay sunday_off
    fake_10b = {
        sat_ds: _mk(DayStatus.SATURDAY.value, 8),    # sat present
        sun_ds: _mk(DayStatus.SUNDAY_OFF.value),
        mon_ds: _mk(DayStatus.PRESENT.value, 8),
    }
    after_10b = apply_sunday_rules("T10b", fake_10b)
    check("T10b Sunday rule: Sat PRESENT → sunday_off stays sunday_off",
          after_10b[sun_ds]["status"] == DayStatus.SUNDAY_OFF.value,
          after_10b[sun_ds]["status"])

    # ── TEST 11: Sunday isolated (sat absent + sun worked) ────────────────────
    fake_11 = {
        sat_ds: _mk(DayStatus.SATURDAY_OFF.value),   # sat absent
        sun_ds: _mk(DayStatus.SUNDAY.value, 8),
        mon_ds: _mk(DayStatus.PRESENT.value, 8),
    }
    after_11 = apply_sunday_rules("T11", fake_11)
    check("T11 Sunday isolated: sat absent + sun worked → sunday_isolated",
          after_11[sun_ds]["status"] == DayStatus.SUNDAY_ISOLATED.value,
          after_11[sun_ds]["status"])

    # T11b: sat PRESENT + sun worked → SUNDAY (full bonus)
    fake_11b = {
        sat_ds: _mk(DayStatus.SATURDAY.value, 8),    # sat present
        sun_ds: _mk(DayStatus.SUNDAY.value, 8),
        mon_ds: _mk(DayStatus.PRESENT.value, 8),
    }
    after_11b = apply_sunday_rules("T11b", fake_11b)
    check("T11b Sunday: sat+mon both present + sun worked → SUNDAY (full bonus)",
          after_11b[sun_ds]["status"] == DayStatus.SUNDAY.value,
          after_11b[sun_ds]["status"])

    # ── TEST 12: Cross-month shift split ──────────────────────────────────────
    may31_in  = datetime(2025, 5, 31, 20, 0)
    jun1_out  = datetime(2025, 6,  1,  8, 0)
    raw_shift = make_shift(may31_in, jun1_out, ShiftState.VALID,
                           raw_h=12.0, worked_h=11.5, ot_h=3.5)
    parts = split_cross_month_shift(raw_shift)
    check("T12 Cross-month shift splits into 2", len(parts) == 2, len(parts))
    check("T12 Cross-month: part1 in_date = May",
          parts[0]["in_date"] == "2025-05-31", parts[0]["in_date"])
    check("T12 Cross-month: part2 in_date = June",
          parts[1]["in_date"] == "2025-06-01", parts[1]["in_date"])

    # ── TEST 13: Holiday mark ─────────────────────────────────────────────────
    fake_att = {
        "T13": {
            "2025-05-01": _mk(DayStatus.ABSENT.value),
        }
    }
    fake_att["T13"]["2025-05-01"]["date"] = "2025-05-01"
    fake_att["T13"]["2025-05-01"]["shifts"] = []
    fake_att["T13"]["2025-05-01"]["in"] = "-"
    fake_att["T13"]["2025-05-01"]["out"] = "-"
    marked = mark_holiday(fake_att, "2025-05-01", True)
    check("T13 Holiday mark → status=holiday",
          marked["T13"]["2025-05-01"]["status"] == DayStatus.HOLIDAY.value,
          marked["T13"]["2025-05-01"]["status"])
    check("T13 Holiday mark → holiday=True",
          marked["T13"]["2025-05-01"]["holiday"] is True)

    # ── TEST 14: Manual override ──────────────────────────────────────────────
    fake_att_14 = {
        "T14": {
            "2025-05-05": {**_mk(DayStatus.ABSENT.value),
                           "date": "2025-05-05", "shifts": [], "in": "-", "out": "-"},
        }
    }
    ok = manual_override(fake_att_14, "T14", "2025-05-05", DayStatus.PRESENT.value, 8.0, 0.0, "admin fix")
    check("T14 Manual override returns True", ok is True, ok)
    check("T14 Manual override sets status=present",
          fake_att_14["T14"]["2025-05-05"]["status"] == DayStatus.PRESENT.value,
          fake_att_14["T14"]["2025-05-05"]["status"])

    # ── TEST 15: Manual punch add ─────────────────────────────────────────────
    fake_att_15 = {
        "T15": {
            "2025-05-06": {**_mk(DayStatus.ABSENT.value),
                           "date": "2025-05-06", "shifts": [], "in": "-", "out": "-"},
        }
    }
    ok15 = add_manual_punch(fake_att_15, "T15", "2025-05-06", "08:00", "16:30", "manual entry")
    check("T15 Manual punch add returns True", ok15 is True, ok15)
    check("T15 Manual punch: worked_h=8.0",
          abs(fake_att_15["T15"]["2025-05-06"]["worked_h"] - 8.0) < 0.01,
          fake_att_15["T15"]["2025-05-06"]["worked_h"])

    return results


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("=" * 60)
        print("  Attendance Engine — Built-in Unit Tests")
        print("=" * 60)
        passed = failed = 0
        for line in run_engine_tests():
            print(line)
            if line.startswith("[PASS]"):
                passed += 1
            else:
                failed += 1
        print("-" * 60)
        print(f"  Results: {passed} passed, {failed} failed")
        print("=" * 60)
        sys.exit(0 if failed == 0 else 1)
