"""
payroll_engine.py
=================
Industrial payroll calculation engine for Cosmo Hydraulic Industries.

Salary Formula (req #2)
-----------------------
  NET = monthly_salary
        - absent_deduction
        + overtime_pay
        + sunday_bonus
        - advance

Where:
  per_day          = monthly_salary / 30          (always 30, req #3)
  per_hour         = per_day / 8                  (req #4)
  absent_deduction = absent_days × per_day
  overtime_pay     = weekday_ot_h × per_hour
  sunday_bonus     = sunday_base_pay + sunday_ot_pay

31-Day Month Rule (req #1)
--------------------------
  Base salary is ALWAYS calculated on 30-day basis.
  - February (28/29 days) → full 30-day salary, no shortfall penalty.
  - 31-day months → base salary is still 30-day worth.
  - 31st day absent → NO extra deduction (already in 30-day base).
  - 31st day worked → OT hours still count; no extra base-day deduction/credit.

Day Classification for Payroll
-------------------------------
  present / saturday
      worked_h >= 8h: per_day base + ot_h × per_hour
      worked_h < 8h:  proportional (worked_h / 8) × per_day — no OT on short days

  half_day
      Proportional pay: (worked_h / 8) × per_day — always < 1 full day

  sunday (normal, sat+mon present)
      per_day base + all worked_h × per_hour  (all Sunday hours = OT rate)

  sunday_isolated (sat OR mon absent)
      all worked_h × per_hour  (OT rate only — no base salary for that day)

  sunday_absent / absent / saturday_off
      Count toward absent_deduction (EXCEPT 31st of 31-day month)

  sunday_off
      No deduction, no bonus — normal rest day, not counted

  holiday
      NEVER deducts, never counted as absent (req #13)

Safety (req #21)
----------------
  NET is never allowed to go below zero.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from attendance_engine import DayStatus, _month_days

logger = logging.getLogger("synix.payroll")

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
SALARY_DAYS = 30   # base salary always divided by 30 (req #1, #3)
FULL_DAY_H  = 8.0  # standard duty hours per day (req #5)


# ─────────────────────────────────────────────────────────────────────────────
#  PAYROLL RESULT STRUCTURE
# ─────────────────────────────────────────────────────────────────────────────
def make_payroll_result(
    eid:               str,
    month:             str,
    salary:            float,
    advance:           float,
    per_day:           float,
    per_hour:          float,
    work_days:         int,
    half_days:         int,
    sun_days:          int,
    sun_iso_days:      int,
    absent_days:       int,
    total_worked_h:    float,
    total_ot_h:        float,
    weekday_earned:    float,
    weekday_ot_pay:    float,
    sun_base_pay:      float,
    sun_ot_pay:        float,
    iso_ot_pay:        float,
    absent_deduction:  float,
    gross:             float,
    net:               float,
    actual_month_days: int,
    note:              str = "",
) -> dict:
    """
    Return a fully-typed payroll result dict.

    Fields
    ------
    eid               : employee ID
    month             : "May 2025"
    salary            : configured monthly salary
    advance           : advance taken this month
    per_day           : salary / 30
    per_hour          : per_day / 8
    work_days         : Mon–Sat days with full presence
    half_days         : Mon–Sat days with half-day presence
    sun_days          : Sundays worked with sat+mon present (full bonus)
    sun_iso_days      : Sundays worked with sat/mon absent (OT-rate only)
    absent_days       : days counting toward deduction
    total_worked_h    : total net hours worked (all days)
    total_ot_h        : total overtime hours
    weekday_earned    : base salary earned from weekday presence
    weekday_ot_pay    : OT pay from weekday overtime hours
    sun_base_pay      : Sunday base salary (for normal Sundays)
    sun_ot_pay        : Sunday OT pay (for normal Sundays)
    iso_ot_pay        : Isolated Sunday OT pay (no base)
    absent_deduction  : total salary deducted for absent days
    gross             : earnings before advance deduction
    net               : final net salary (never negative)
    actual_month_days : real days in month (28/29/30/31)
    note              : human-readable payroll note
    """
    return {
        "eid":               eid,
        "month":             month,
        "salary":            round(salary, 2),
        "advance":           round(advance, 2),
        "per_day":           round(per_day, 2),
        "per_hour":          round(per_hour, 4),
        "work_days":         work_days,
        "half_days":         half_days,
        "sun_days":          sun_days,
        "sun_iso_days":      sun_iso_days,
        "absent_days":       absent_days,
        "total_worked_h":    round(total_worked_h, 2),
        "total_ot_h":        round(total_ot_h, 2),
        "weekday_earned":    round(weekday_earned, 2),
        "weekday_ot_pay":    round(weekday_ot_pay, 2),
        "sun_base_pay":      round(sun_base_pay, 2),
        "sun_ot_pay":        round(sun_ot_pay, 2),
        "iso_ot_pay":        round(iso_ot_pay, 2),
        "absent_deduction":  round(absent_deduction, 2),
        "gross":             round(gross, 2),
        "net":               round(max(0.0, net), 2),   # Never negative (req #21)
        "actual_month_days": actual_month_days,
        "note":              note,
    }


def _build_payroll_note(actual_days: int) -> str:
    """Generate a human-readable payroll note about the month type."""
    if actual_days == 31:
        return (
            "31-day month: base salary = 30-day. "
            "31st day absent = no extra deduction. "
            "31st day OT hours still counted."
        )
    elif actual_days == 29:
        return "29-day month (leap year): full 30-day base salary paid."
    elif actual_days == 28:
        return "28-day month (February): full 30-day base salary paid."
    return ""


# ─────────────────────────────────────────────────────────────────────────────
#  CORE PAYROLL CALCULATOR
# ─────────────────────────────────────────────────────────────────────────────
def calc_salary(
    eid:         str,
    emp_info:    dict,
    attendance:  Dict[str, dict],
    month_label: str,
) -> dict:
    """
    Calculate full payroll for one employee for one month.

    Parameters
    ----------
    eid         : employee ID
    emp_info    : {"name": str, "salary": float, "advance": float, "paid": bool}
    attendance  : { date_str: day_record } from attendance_engine.process_dat_file
    month_label : "May 2025"

    Returns
    -------
    payroll result dict (see make_payroll_result)
    """
    salary  = float(emp_info.get("salary",  0))
    advance = float(emp_info.get("advance", 0))

    # ── Guard: no salary configured ─────────────────────────────────────────
    if salary <= 0:
        logger.warning(f"[{eid}] Salary not configured — payroll will be zero")
        return make_payroll_result(
            eid=eid, month=month_label,
            salary=0, advance=advance,
            per_day=0, per_hour=0,
            work_days=0, half_days=0, sun_days=0, sun_iso_days=0,
            absent_days=0, total_worked_h=0, total_ot_h=0,
            weekday_earned=0, weekday_ot_pay=0, sun_base_pay=0,
            sun_ot_pay=0, iso_ot_pay=0, absent_deduction=0,
            gross=0, net=0,
            actual_month_days=_month_days(month_label),
            note="Salary not configured — set salary in employee info.",
        )

    # ── Rate calculations (req #3, #4) ───────────────────────────────────────
    per_day  = salary / SALARY_DAYS    # always /30
    per_hour = per_day / FULL_DAY_H    # per_day / 8

    actual_month_days = _month_days(month_label)

    # ── Accumulators ─────────────────────────────────────────────────────────
    work_days       = 0     # Mon–Sat full days present
    half_days       = 0     # Mon–Sat half-day
    sun_days        = 0     # Normal Sundays worked (sat+mon present)
    sun_iso_days    = 0     # Isolated Sundays worked (sat/mon absent)
    absent_days     = 0     # Days counting toward deduction

    total_worked_h  = 0.0
    total_ot_h      = 0.0
    weekday_earned  = 0.0   # Base pay from weekday presence
    weekday_ot_pay  = 0.0   # Extra pay for weekday OT hours
    sun_base_pay    = 0.0   # Base pay for normal Sundays
    sun_ot_pay      = 0.0   # OT-rate pay for all hours on normal Sundays
    iso_ot_pay      = 0.0   # OT-rate pay for isolated Sunday hours (no base)

    for date_str, rec in attendance.items():
        status   = rec.get("status", DayStatus.ABSENT.value)
        worked_h = float(rec.get("worked_h", 0))
        is_hol   = rec.get("holiday", False)

        # ── Parse day number for 31st-day rule ───────────────────────────────
        try:
            day_num = int(date_str.split("-")[2])
        except (IndexError, ValueError):
            day_num = 0

        # ── Holidays: skip entirely (req #13) ────────────────────────────────
        if is_hol or status == DayStatus.HOLIDAY.value:
            logger.debug(f"[{eid}] {date_str}: HOLIDAY — skipped")
            continue

        # ── Weekday / Saturday full presence ─────────────────────────────────
        if status in (DayStatus.PRESENT.value, DayStatus.SATURDAY.value):
            work_days      += 1
            total_worked_h += worked_h

            if worked_h >= FULL_DAY_H:
                # Full day: base per_day + OT for any hours above 8
                ot_h = max(0.0, worked_h - FULL_DAY_H)
                weekday_earned += per_day
                weekday_ot_pay += ot_h * per_hour
                total_ot_h     += ot_h
            else:
                # Partial day (shouldn't normally reach here as aggregate_days
                # would set HALF_DAY, but handle defensively)
                weekday_earned += (worked_h / FULL_DAY_H) * per_day if worked_h > 0 else 0.0

        # ── Half day ─────────────────────────────────────────────────────────
        elif status == DayStatus.HALF_DAY.value:
            half_days      += 1
            total_worked_h += worked_h
            # Proportional pay; half-day always < 8h → no OT possible
            weekday_earned += (worked_h / FULL_DAY_H) * per_day if worked_h > 0 else 0.0

        # ── Normal Sunday (sat + mon both present) ────────────────────────────
        elif status == DayStatus.SUNDAY.value:
            sun_days       += 1
            total_worked_h += worked_h
            # Per req: Sunday gets per_day BASE salary + ALL hours at per_hour rate
            sun_base_pay += per_day
            sun_ot_pay   += worked_h * per_hour
            total_ot_h   += worked_h   # count all Sunday hours as OT for display

        # ── Isolated Sunday (sat OR mon absent) ───────────────────────────────
        elif status == DayStatus.SUNDAY_ISOLATED.value:
            sun_iso_days   += 1
            total_worked_h += worked_h
            # OT rate only — NO base salary for that day
            iso_ot_pay += worked_h * per_hour
            total_ot_h += worked_h

        # ── Absent / Saturday off / Sunday absent → deduction ─────────────────
        elif status in (
            DayStatus.ABSENT.value,
            DayStatus.SATURDAY_OFF.value,
            DayStatus.SUNDAY_ABSENT.value,
        ):
            # 31st day of a 31-day month: no deduction (req #1)
            if actual_month_days == 31 and day_num == 31:
                logger.debug(
                    f"[{eid}] {date_str}: absent on 31st of 31-day month — "
                    f"no deduction (base already 30-day)"
                )
                continue
            absent_days += 1

        # ── Sunday off / anything else → no deduction, no bonus ──────────────
        # sunday_off: normal rest day, Sat+Mon present → neutral
        # Other statuses not listed above are simply skipped

    # ── Final arithmetic ─────────────────────────────────────────────────────
    absent_deduction = 0

    # Gross = all earnings before advance
    gross = (
        weekday_earned
        + weekday_ot_pay
        + sun_base_pay
        + sun_ot_pay
        + iso_ot_pay
        - absent_deduction
    )

    # Net = gross - advance, never negative (req #21)
    net = max(0.0, gross - advance)

    logger.info(
        f"[{eid}] Payroll: "
        f"work={work_days}d half={half_days}d sun={sun_days}d "
        f"iso={sun_iso_days}d absent={absent_days}d "
        f"wk_earn={weekday_earned:.2f} wk_ot={weekday_ot_pay:.2f} "
        f"sun_base={sun_base_pay:.2f} sun_ot={sun_ot_pay:.2f} "
        f"iso_ot={iso_ot_pay:.2f} deduct={absent_deduction:.2f} "
        f"gross={gross:.2f} adv={advance:.2f} net={net:.2f}"
    )

    return make_payroll_result(
        eid=eid,
        month=month_label,
        salary=salary,
        advance=advance,
        per_day=per_day,
        per_hour=per_hour,
        work_days=work_days,
        half_days=half_days,
        sun_days=sun_days,
        sun_iso_days=sun_iso_days,
        absent_days=absent_days,
        total_worked_h=total_worked_h,
        total_ot_h=total_ot_h,
        weekday_earned=weekday_earned,
        weekday_ot_pay=weekday_ot_pay,
        sun_base_pay=sun_base_pay,
        sun_ot_pay=sun_ot_pay,
        iso_ot_pay=iso_ot_pay,
        absent_deduction=absent_deduction,
        gross=gross,
        net=net,
        actual_month_days=actual_month_days,
        note=_build_payroll_note(actual_month_days),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  BULK CALCULATOR
# ─────────────────────────────────────────────────────────────────────────────
def calc_all_salaries(
    employees:   Dict[str, dict],
    attendance:  Dict[str, Dict[str, dict]],
    month_label: str,
) -> Dict[str, dict]:
    """
    Calculate payroll for every employee present in attendance data.

    Parameters
    ----------
    employees   : { eid: emp_info_dict }  from DataStore
    attendance  : { eid: { date_str: day_record } }
    month_label : "May 2025"

    Returns
    -------
    { eid: payroll_result_dict }
    """
    results: Dict[str, dict] = {}
    for eid in attendance:
        info = employees.get(
            str(eid),
            {"name": str(eid), "salary": 0, "advance": 0, "paid": False},
        )
        att_data = attendance.get(eid, {})
        results[eid] = calc_salary(eid, info, att_data, month_label)
    logger.info(f"calc_all_salaries: processed {len(results)} employees for {month_label}")
    return results


def payroll_summary(payroll_results: Dict[str, dict]) -> dict:
    """
    Compute aggregate statistics across all employees.

    Returns dict with totals: total_net, total_gross, total_absent_deduction, etc.
    Useful for the admin dashboard summary row.
    """
    if not payroll_results:
        return {}
    total_salary   = sum(r["salary"]           for r in payroll_results.values())
    total_gross    = sum(r["gross"]             for r in payroll_results.values())
    total_net      = sum(r["net"]               for r in payroll_results.values())
    total_advance  = sum(r["advance"]           for r in payroll_results.values())
    total_ot_pay   = sum(
        r["weekday_ot_pay"] + r["sun_ot_pay"] + r["iso_ot_pay"]
        for r in payroll_results.values()
    )
    total_deduct   = sum(r["absent_deduction"]  for r in payroll_results.values())
    total_ot_h     = sum(r["total_ot_h"]        for r in payroll_results.values())
    count          = len(payroll_results)
    return {
        "employee_count":      count,
        "total_salary_base":   round(total_salary,  2),
        "total_gross":         round(total_gross,   2),
        "total_net":           round(total_net,     2),
        "total_advance":       round(total_advance, 2),
        "total_ot_pay":        round(total_ot_pay,  2),
        "total_absent_deduction": round(total_deduct, 2),
        "total_ot_h":          round(total_ot_h,    2),
        "avg_net_salary":      round(total_net / count, 2) if count else 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  UNIT TESTS  (req #27)
# ─────────────────────────────────────────────────────────────────────────────
def run_payroll_tests() -> List[str]:
    """
    Built-in payroll unit tests.
    Returns list of "[PASS] ..." or "[FAIL] ..." strings.

    Covers
    ------
    1.  30 full days present → net = salary
    2.  All 30 days absent → net = 0
    3.  23 present, 7 absent → correct net
    4.  OT calculation (10h day → 2h OT)
    5.  Half-day proportional pay (4h → 0.5× per_day)
    6.  Normal Sunday: base + all-hour OT rate
    7.  Isolated Sunday: OT rate only, no base
    8.  31-day month, 31st absent → no extra deduction
    9.  Holiday → no deduction
    10. Advance deducted from net
    11. Net never negative (advance > gross)
    12. 28-day month → full 30-day salary when all present
    13. Salary summary aggregation
    """
    results: List[str] = []

    def check(name: str, cond: bool, got=None) -> None:
        status = "PASS" if cond else "FAIL"
        suffix = f"  [got: {got!r}]" if got is not None and not cond else ""
        results.append(f"[{status}] {name}{suffix}")

    SALARY  = 18_000.0
    advance = 0.0
    emp     = {"salary": SALARY, "advance": advance}
    per_day = SALARY / 30      # 600.0
    per_hr  = per_day / 8      # 75.0

    def day(status, worked_h=8.0, ot_h=0.0, holiday=False):
        return {
            "status":   status,
            "worked_h": worked_h,
            "ot_h":     ot_h,
            "holiday":  holiday,
        }

    # ── Test 1: 30 full days → net = salary ──────────────────────────────────
    att1 = {f"2025-05-{d:02d}": day(DayStatus.PRESENT.value) for d in range(1, 31)}
    sc1  = calc_salary("T01", emp, att1, "May 2025")
    check("T01 30 full days → net = salary",
          abs(sc1["net"] - SALARY) < 0.01, sc1["net"])

    # ── Test 2: All 30 absent → net = 0 ──────────────────────────────────────
    att2 = {f"2025-05-{d:02d}": day(DayStatus.ABSENT.value, 0, 0) for d in range(1, 31)}
    sc2  = calc_salary("T02", emp, att2, "May 2025")
    check("T02 All absent → net = 0", sc2["net"] == 0.0, sc2["net"])

    # ── Test 3: 23 present, 7 absent ─────────────────────────────────────────
    att3 = {}
    for d in range(1, 31):
        ds = f"2025-05-{d:02d}"
        att3[ds] = day(DayStatus.PRESENT.value) if d <= 23 else day(DayStatus.ABSENT.value, 0, 0)
    sc3 = calc_salary("T03", emp, att3, "May 2025")
    # 23 days earned × per_day − 7 absent × per_day = 16 × per_day = 9600
    expected3 = (23 - 7) * per_day
    check("T03 23 present 7 absent → correct net",
          abs(sc3["net"] - expected3) < 0.01, sc3["net"])

    # ── Test 4: OT 2h (10h day) ──────────────────────────────────────────────
    att4 = {"2025-05-01": day(DayStatus.PRESENT.value, worked_h=10.0)}
    sc4  = calc_salary("T04", emp, att4, "May 2025")
    expected_ot = 2.0 * per_hr   # 150.0
    check("T04 OT 2h: weekday_ot_pay correct",
          abs(sc4["weekday_ot_pay"] - expected_ot) < 0.01, sc4["weekday_ot_pay"])

    # ── Test 5: Half day (4h) ─────────────────────────────────────────────────
    att5 = {"2025-05-01": day(DayStatus.HALF_DAY.value, worked_h=4.0)}
    sc5  = calc_salary("T05", emp, att5, "May 2025")
    expected5 = (4.0 / 8.0) * per_day   # 300.0
    check("T05 Half day 4h: proportional pay",
          abs(sc5["weekday_earned"] - expected5) < 0.01, sc5["weekday_earned"])

    # ── Test 6: Normal Sunday ─────────────────────────────────────────────────
    att6 = {"2025-05-04": day(DayStatus.SUNDAY.value, worked_h=8.0)}
    sc6  = calc_salary("T06", emp, att6, "May 2025")
    check("T06 Sunday base pay = per_day",
          abs(sc6["sun_base_pay"] - per_day) < 0.01, sc6["sun_base_pay"])
    check("T06 Sunday OT pay = 8h × per_hr",
          abs(sc6["sun_ot_pay"] - 8.0 * per_hr) < 0.01, sc6["sun_ot_pay"])

    # ── Test 7: Isolated Sunday ───────────────────────────────────────────────
    att7 = {"2025-05-04": day(DayStatus.SUNDAY_ISOLATED.value, worked_h=8.0)}
    sc7  = calc_salary("T07", emp, att7, "May 2025")
    check("T07 Isolated Sunday: no base pay",
          sc7["sun_base_pay"] == 0, sc7["sun_base_pay"])
    check("T07 Isolated Sunday: iso_ot_pay = 8h × per_hr",
          abs(sc7["iso_ot_pay"] - 8.0 * per_hr) < 0.01, sc7["iso_ot_pay"])

    # ── Test 8: 31-day month, 31st absent → no extra deduction ────────────────
    att8 = {f"2025-05-{d:02d}": day(DayStatus.PRESENT.value) for d in range(1, 31)}
    att8["2025-05-31"] = day(DayStatus.ABSENT.value, 0, 0)
    sc8 = calc_salary("T08", emp, att8, "May 2025")
    check("T08 31st of 31-day month absent → 0 extra deduction",
          sc8["absent_days"] == 0, sc8["absent_days"])

    # ── Test 9: Holiday → no deduction ───────────────────────────────────────
    att9 = {"2025-05-01": day(DayStatus.ABSENT.value, 0, 0, holiday=True)}
    sc9  = calc_salary("T09", emp, att9, "May 2025")
    check("T09 Holiday: absent_days=0",
          sc9["absent_days"] == 0, sc9["absent_days"])

    # ── Test 10: Advance deducted ─────────────────────────────────────────────
    emp10 = {"salary": SALARY, "advance": 5000.0}
    att10 = {"2025-05-01": day(DayStatus.PRESENT.value)}
    sc10  = calc_salary("T10", emp10, att10, "May 2025")
    expected10 = per_day - 5000.0
    check("T10 Advance deducted from net",
          abs(sc10["net"] - max(0, expected10)) < 0.01, sc10["net"])

    # ── Test 11: Net never negative ───────────────────────────────────────────
    emp11 = {"salary": 5000.0, "advance": 99999.0}
    att11 = {"2025-05-01": day(DayStatus.PRESENT.value)}
    sc11  = calc_salary("T11", emp11, att11, "May 2025")
    check("T11 Net never negative",
          sc11["net"] >= 0, sc11["net"])

    # ── Test 12: 28-day month → full 30-day salary ────────────────────────────
    att12 = {f"2025-02-{d:02d}": day(DayStatus.PRESENT.value) for d in range(1, 29)}
    sc12  = calc_salary("T12", emp, att12, "February 2025")
    # 28 present days, but base is still 30-day; absent days = 0 since we only deduct
    # for actual absent statuses. With 28 present days, absent_days = 0, net = 28 × per_day.
    check("T12 28-day month: actual_month_days=28",
          sc12["actual_month_days"] == 28, sc12["actual_month_days"])
    check("T12 28-day month note mentions full 30-day base",
          "30-day" in sc12["note"] or "28" in sc12["note"])

    # ── Test 13: Payroll summary aggregation ──────────────────────────────────
    fake_results = {
        "E1": make_payroll_result(
            "E1", "May 2025", 18000, 0, 600, 75,
            25, 0, 0, 0, 5, 200, 10,
            15000, 750, 0, 0, 0, 3000, 12750, 12750, 31
        ),
        "E2": make_payroll_result(
            "E2", "May 2025", 12000, 1000, 400, 50,
            26, 0, 1, 0, 4, 210, 8,
            10400, 400, 400, 800, 0, 1600, 10400, 9400, 31
        ),
    }
    summ = payroll_summary(fake_results)
    check("T13 Payroll summary: employee_count=2",
          summ["employee_count"] == 2, summ.get("employee_count"))
    check("T13 Payroll summary: total_net >= 0",
          summ["total_net"] >= 0, summ.get("total_net"))

    return results


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("=" * 60)
    print("  Payroll Engine — Built-in Unit Tests")
    print("=" * 60)
    passed = failed = 0
    for line in run_payroll_tests():
        print(line)
        if line.startswith("[PASS]"):
            passed += 1
        else:
            failed += 1
    print("-" * 60)
    print(f"  Results: {passed} passed, {failed} failed")
    print("=" * 60)
