# Cosmo Payroll & Attendance Manager

A polished desktop attendance and payroll management app for Cosmo Hydraulic Industries.
Built with PyQt6 and Python, this tool processes attendance `.dat` files, classifies shifts, calculates payroll, and exports reports.

## 🚀 Features

- **Interactive PyQt6 desktop UI** with responsive charts and tables
- **Upload and process `.dat` attendance files** reliably
- **Smart shift classification** with duplicate, invalid, suspicious and approved shift handling
- **Monthly payroll calculation** with:
  - 30-day salary base
  - per-hour and overtime pay
  - Sunday bonus and isolated Sunday rules
  - absent deductions and holiday handling
- **Employee management** with editable name, salary, advance, and paid status
- **Admin controls** for:
  - marking holidays
  - approving suspicious shifts
  - manual attendance override and punch updates
- **Export payroll summary to Excel** (`.xlsx`)
- **Generate PDF salary slips** with optional signature support
- **Audit logging** for traceability and data review
- **Safe persistent storage** with atomic save/backup support

## ✅ Installation

1. Clone or copy the repository:

```powershell
cd "e:\Punching Cosmo"
```

2. Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate
```

3. Install dependencies:

```powershell
pip install -r requirements.txt
```

4. Run the application:

```powershell
python just.py
```

> If your environment uses a different entrypoint, replace `just.py` with the correct launcher script.

## 📦 Requirements

The required Python packages are listed in `requirements.txt`.
This project uses:

- `PyQt6` for the desktop user interface
- `reportlab` for PDF salary slip generation
- `openpyxl` for Excel export

## 🛠 Notes

- The app uses a 30-day salary basis for payroll calculations.
- Holidays are handled separately and do not deduct salary.
- The system prevents negative net salary results.

## 💡 Quick tips

- Keep attendance data files in one folder for easy import.
- Use the audit viewer to verify decisions made by the attendance engine.
- Save payroll exports with month labels like `Cosmo_Hydraulic_Payroll_May_2025.xlsx`.
