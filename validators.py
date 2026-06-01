"""
validators.py
=============
Data validation, DataStore, and persistence layer for Synix Payroll.

Responsibilities
----------------
1. DataStore class — single source of truth for all runtime data.
   - employees, attendance, month_label, sig_path
   - Atomic save with .bak recovery
   - Corruption-safe load

2. Validation functions for:
   - Punch data integrity
   - Employee record fields
   - Payroll result sanity checks

3. Report helpers:
   - Collect all validation warnings for display in UI
"""

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from attendance_engine import (
    DayStatus,
    ShiftState,
    _month_days,
    _weekday,
    audit,
)

logger = logging.getLogger("synix.validators")

# ─────────────────────────────────────────────────────────────────────────────
#  FILE PATHS
# ─────────────────────────────────────────────────────────────────────────────
DATA_FILE    = "synix_data.json"
AUDIT_FILE   = "synix_audit.json"


# ─────────────────────────────────────────────────────────────────────────────
#  ATOMIC SAVE / SAFE LOAD  (req #20)
# ─────────────────────────────────────────────────────────────────────────────
def _atomic_save(filepath: str, data: object) -> None:
    """
    Save data to filepath atomically via a temp file + os.replace().
    Also keeps a .bak of the previous version.

    Guarantees
    ----------
    - On success: filepath contains the new data.
    - On crash:   filepath contains the old data (or .bak if no old existed).
    - Never leaves a half-written file at filepath.
    """
    dir_path = os.path.dirname(os.path.abspath(filepath)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str, ensure_ascii=False)
        # Keep backup of previous version
        if os.path.exists(filepath):
            shutil.copy2(filepath, filepath + ".bak")
        # Atomic rename (POSIX atomic; Windows fallback)
        os.replace(tmp_path, filepath)
        logger.debug(f"Atomic save OK → {filepath}")
    except Exception as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        logger.error(f"Atomic save FAILED for {filepath}: {exc}")
        raise


def _safe_load(filepath: str) -> Optional[dict]:
    """
    Load JSON from filepath with .bak fallback for corruption recovery.

    Returns parsed dict, or None if both primary and .bak fail.
    """
    for path in (filepath, filepath + ".bak"):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if path != filepath:
                logger.warning(f"Loaded from backup: {path}")
            return data
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            logger.warning(f"Load failed [{path}]: {e}")
    logger.error(f"All load attempts failed for {filepath}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  DATA STORE  (req #20)
# ─────────────────────────────────────────────────────────────────────────────
class DataStore:
    """
    Central runtime data store for the Synix application.

    Attributes
    ----------
    employees   : { eid_str: {"name", "salary", "advance", "paid"} }
    attendance  : { eid_str: { date_str: day_record } }
    month_label : "May 2025" (from the last loaded .dat file)
    sig_path    : absolute path to accountant signature image

    All mutations go through save(), which writes atomically.
    All loads come through load(), which recovers from .bak if needed.
    """

    def __init__(self) -> None:
        self.employees:   Dict[str, dict]             = {}
        self.attendance:  Dict[str, Dict[str, dict]]  = {}
        self.month_label: str                          = ""
        self.sig_path:    str                          = ""
        self.load()

    # ── Persistence ──────────────────────────────────────────────────────────
    def load(self) -> None:
        """Load persisted state from DATA_FILE with .bak fallback."""
        data = _safe_load(DATA_FILE)
        if data is None:
            logger.info("DataStore: no existing data file — starting fresh")
            return
        self.employees   = data.get("employees",   {})
        self.attendance  = data.get("attendance",  {})
        self.month_label = data.get("month_label", "")
        self.sig_path    = data.get("sig_path",    "")
        logger.info(
            f"DataStore loaded: {len(self.employees)} employees, "
            f"month={self.month_label or '(none)'}"
        )

    def save(self) -> None:
        """Atomically persist all state to DATA_FILE."""
        _atomic_save(DATA_FILE, {
            "employees":   self.employees,
            "attendance":  self.attendance,
            "month_label": self.month_label,
            "sig_path":    self.sig_path,
        })

    # ── Employee helpers ─────────────────────────────────────────────────────
    def emp_info(self, eid) -> dict:
        """
        Return employee info dict, with safe defaults if employee not configured.
        Always returns a copy (not a reference) to prevent accidental mutation.
        """
        defaults = {"name": str(eid), "salary": 0.0, "advance": 0.0, "paid": False}
        return {**defaults, **self.employees.get(str(eid), {})}

    def set_emp(
        self,
        eid,
        name: str,
        salary: float,
        advance: float = 0.0,
        paid: bool = False,
    ) -> None:
        """
        Update or create an employee record and save.

        Parameters
        ----------
        eid     : employee ID (any type; stored as str)
        name    : display name
        salary  : monthly salary (must be >= 0)
        advance : advance taken this month (must be >= 0)
        paid    : has salary been paid this month
        """
        errors = validate_employee_record(str(eid), name, salary, advance)
        if errors:
            logger.warning(f"set_emp validation warnings for {eid}: {errors}")
        self.employees[str(eid)] = {
            "name":    name.strip() or str(eid),
            "salary":  max(0.0, float(salary)),
            "advance": max(0.0, float(advance)),
            "paid":    bool(paid),
        }
        self.save()

    def all_eids(self) -> List[str]:
        """Return all employee IDs present in attendance data, sorted."""
        return sorted(
            self.attendance.keys(),
            key=lambda x: (not x.isdigit(), x.zfill(10) if x.isdigit() else x),
        )

    # ── Attendance holiday helpers ────────────────────────────────────────────
    def set_holiday(self, date_str: str, is_holiday: bool = True) -> None:
        """
        Mark/unmark a date as a company holiday for ALL employees.
        Saves after marking.
        """
        from attendance_engine import mark_holiday
        self.attendance = mark_holiday(self.attendance, date_str, is_holiday)
        self.save()

    def get_all_holidays(self) -> Dict[str, Dict[str, bool]]:
        """
        Return { eid: { date_str: True } } for all holidays across all employees.
        Used when re-processing .dat files to preserve previously marked holidays.
        """
        holidays: Dict[str, Dict[str, bool]] = {}
        for eid, days in self.attendance.items():
            hols = {ds: True for ds, rec in days.items() if rec.get("holiday")}
            if hols:
                holidays[eid] = hols
        return holidays

    def summary_stats(self) -> dict:
        """
        Return aggregate statistics across all employees for the sidebar.

        Returns
        -------
        {
          total_employees, avg_present, avg_absent,
          total_ot_h, paid_count, unpaid_count
        }
        """
        from payroll_engine import calc_all_salaries
        eids = self.all_eids()
        if not eids:
            return {}
        payroll = calc_all_salaries(self.employees, self.attendance, self.month_label)
        total   = len(eids)
        return {
            "total_employees": total,
            "avg_present":  sum(r["work_days"]      for r in payroll.values()) / total,
            "avg_absent":   sum(r["absent_days"]     for r in payroll.values()) / total,
            "total_ot_h":   sum(r["total_ot_h"]      for r in payroll.values()),
            "paid_count":   sum(1 for e in eids if self.emp_info(e).get("paid")),
            "unpaid_count": sum(1 for e in eids if not self.emp_info(e).get("paid")),
        }


# ─────────────────────────────────────────────────────────────────────────────
#  VALIDATION FUNCTIONS  (req #19)
# ─────────────────────────────────────────────────────────────────────────────
def validate_employee_record(
    eid: str,
    name: str,
    salary: float,
    advance: float,
) -> List[str]:
    """
    Validate employee record fields.

    Returns
    -------
    List of warning/error strings (empty = all valid).
    """
    warnings: List[str] = []
    if not eid or not eid.strip():
        warnings.append("Employee ID is empty.")
    if not name or not name.strip():
        warnings.append(f"Employee {eid}: name is empty — will default to ID.")
    if salary < 0:
        warnings.append(f"Employee {eid}: salary {salary} is negative — must be >= 0.")
    if salary == 0:
        warnings.append(f"Employee {eid}: salary is zero — payroll will compute Rs 0.")
    if salary > 10_000_000:
        warnings.append(f"Employee {eid}: salary {salary} seems unusually high — verify.")
    if advance < 0:
        warnings.append(f"Employee {eid}: advance {advance} is negative — must be >= 0.")
    if advance > salary and salary > 0:
        warnings.append(
            f"Employee {eid}: advance ({advance}) exceeds salary ({salary}). "
            "Net will be forced to zero."
        )
    return warnings


def validate_attendance_record(
    eid: str,
    date_str: str,
    rec: dict,
) -> List[str]:
    """
    Validate a single day attendance record for data integrity.

    Returns
    -------
    List of warning strings (empty = clean).
    """
    warnings: List[str] = []
    status   = rec.get("status", "")
    worked_h = float(rec.get("worked_h", 0))
    ot_h     = float(rec.get("ot_h", 0))

    # Status must be a recognised DayStatus
    valid_statuses = {s.value for s in DayStatus}
    if status not in valid_statuses:
        warnings.append(
            f"{eid}/{date_str}: unknown status {status!r}. "
            f"Valid: {sorted(valid_statuses)}"
        )

    # worked_h should not be negative
    if worked_h < 0:
        warnings.append(f"{eid}/{date_str}: worked_h={worked_h} is negative (data error).")

    # worked_h should not exceed 24h (physically impossible)
    if worked_h > 24:
        warnings.append(f"{eid}/{date_str}: worked_h={worked_h} exceeds 24h (impossible).")

    # ot_h should not exceed worked_h
    if ot_h > worked_h:
        warnings.append(
            f"{eid}/{date_str}: ot_h={ot_h} > worked_h={worked_h} "
            "(OT cannot exceed total worked hours)."
        )

    # Holidays should have zero worked hours (warning if non-zero)
    if rec.get("holiday") and worked_h > 0:
        warnings.append(
            f"{eid}/{date_str}: holiday=True but worked_h={worked_h} > 0 — verify."
        )

    # Absent days should have zero worked hours
    if status in (DayStatus.ABSENT.value, DayStatus.SATURDAY_OFF.value,
                  DayStatus.SUNDAY_ABSENT.value):
        if worked_h > 0:
            warnings.append(
                f"{eid}/{date_str}: status={status} but worked_h={worked_h} > 0 "
                "(absent days should have no worked hours)."
            )

    # Date format check
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        warnings.append(f"{eid}: invalid date_str format {date_str!r} — expected YYYY-MM-DD.")

    return warnings


def validate_full_attendance(
    attendance: Dict[str, Dict[str, dict]],
) -> Dict[str, List[str]]:
    """
    Run validate_attendance_record on every day for every employee.

    Returns
    -------
    { eid: [list of warnings] }  — only includes employees with warnings.
    """
    all_warnings: Dict[str, List[str]] = {}
    for eid, days in attendance.items():
        emp_warns: List[str] = []
        for date_str, rec in days.items():
            emp_warns.extend(validate_attendance_record(eid, date_str, rec))
        if emp_warns:
            all_warnings[eid] = emp_warns
    if all_warnings:
        total = sum(len(v) for v in all_warnings.values())
        logger.warning(f"validate_full_attendance: {total} issues found across {len(all_warnings)} employees")
    else:
        logger.info("validate_full_attendance: all records clean")
    return all_warnings


def validate_shift(eid: str, shift: dict) -> List[str]:
    """
    Validate a single shift record for data integrity.

    Returns
    -------
    List of warning strings.
    """
    warnings: List[str] = []
    state    = shift.get("state", "")
    raw_h    = float(shift.get("raw_h", 0))
    worked_h = float(shift.get("worked_h", 0))
    ot_h     = float(shift.get("ot_h", 0))

    valid_states = {s.value for s in ShiftState}
    if state not in valid_states:
        warnings.append(f"{eid}: unknown shift state {state!r}")

    if state == ShiftState.VALID.value:
        if not shift.get("in") or not shift.get("out"):
            warnings.append(f"{eid}: VALID shift missing in/out timestamps")
        if raw_h <= 0:
            warnings.append(f"{eid}: VALID shift has raw_h={raw_h} <= 0")
        if worked_h < 0:
            warnings.append(f"{eid}: VALID shift has worked_h={worked_h} < 0")
        if ot_h < 0:
            warnings.append(f"{eid}: VALID shift has ot_h={ot_h} < 0")

    if state == ShiftState.SUSPICIOUS.value:
        warnings.append(
            f"{eid}: SUSPICIOUS shift on {shift.get('in_date', '?')} "
            f"(raw={raw_h:.1f}h) — admin review pending"
        )

    return warnings


def find_suspicious_shifts(
    attendance: Dict[str, Dict[str, dict]],
) -> List[dict]:
    """
    Return a list of all suspicious/unapproved shifts across all employees.

    Each entry: { eid, date_str, shift_dict }
    Useful for the admin review panel.
    """
    found: List[dict] = []
    for eid, days in attendance.items():
        for date_str, rec in days.items():
            for shift in rec.get("shifts", []):
                if shift.get("state") == ShiftState.SUSPICIOUS.value:
                    found.append({"eid": eid, "date_str": date_str, "shift": shift})
    return found


def find_missing_salary_employees(
    employees: Dict[str, dict],
    attendance: Dict[str, Dict[str, dict]],
) -> List[str]:
    """
    Return list of employee IDs that have attendance but no salary configured.
    These will compute Rs 0 net salary.
    """
    missing = []
    for eid in attendance:
        info = employees.get(str(eid), {})
        if float(info.get("salary", 0)) <= 0:
            missing.append(eid)
    return missing


def attendance_consistency_report(
    attendance: Dict[str, Dict[str, dict]],
    month_label: str,
) -> dict:
    """
    Build a summary consistency report for the loaded attendance data.

    Returns
    -------
    {
      total_employees,
      total_days_processed,
      total_present,
      total_absent,
      total_sunday_worked,
      total_ot_h,
      suspicious_shifts_count,
      employees_with_warnings,
      month_label,
      actual_month_days,
    }
    """
    total_present = total_absent = total_sun = total_ot = 0
    suspicious_count = 0
    employees_processed = len(attendance)
    total_days = 0

    for eid, days in attendance.items():
        total_days += len(days)
        for date_str, rec in days.items():
            s = rec.get("status", "")
            if s in (DayStatus.PRESENT.value, DayStatus.SATURDAY.value,
                     DayStatus.HALF_DAY.value):
                total_present += 1
            elif s in (DayStatus.ABSENT.value, DayStatus.SATURDAY_OFF.value,
                       DayStatus.SUNDAY_ABSENT.value):
                total_absent += 1
            elif s in (DayStatus.SUNDAY.value, DayStatus.SUNDAY_ISOLATED.value):
                total_sun += 1
            total_ot += float(rec.get("ot_h", 0))
            for shift in rec.get("shifts", []):
                if shift.get("state") == ShiftState.SUSPICIOUS.value:
                    suspicious_count += 1

    validation_warns = validate_full_attendance(attendance)

    return {
        "month_label":             month_label,
        "actual_month_days":       _month_days(month_label),
        "total_employees":         employees_processed,
        "total_days_processed":    total_days,
        "total_present":           total_present,
        "total_absent":            total_absent,
        "total_sunday_worked":     total_sun,
        "total_ot_h":              round(total_ot, 2),
        "suspicious_shifts_count": suspicious_count,
        "employees_with_warnings": len(validation_warns),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  QUICK SELF-TEST
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("DataStore + validators loaded OK.")
    errs = validate_employee_record("1", "Ramesh Kumar", 18000, 2000)
    print("Valid employee (expect no errors):", errs)
    errs2 = validate_employee_record("", "", -100, 99999)
    print("Invalid employee (expect warnings):", errs2)
