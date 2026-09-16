# Automation - Loading of EWMS Self-Serve Forms

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg?logo=python&logoColor=white)](https://www.python.org/downloads/)

`ewms_form_loader.py` is a Python utility that automates EWMS Self-Serve Forms directly in a tenant database (aims to replicate UI flow CLI style).

## Setup

Download the latest forms package from [NetworkXPRESS](https://univeris.atlassian.net/wiki/spaces/CP/pages/73402166/NetworkXPRESS).

```powershell
pip install -r requirements.txt   # one-time

python ewms_form_loader.py load-package-forms `
  --forms-package "forms_package_input\forms_package.json" `
  --tenants "forms_package_input\tenants.json" `
  --db-user "<DB_USER>" `
  --db-password '<PASSWORD_PLACEHOLDER>' `
  --db-driver "ODBC Driver 17 for SQL Server"
```

> [!TIP]
> Every run is a dry run unless you pass `--apply`.
>
> Refresh the EWMS cache once the changes are applied.

## Loader Parameters

| Parameter                         | Type | Required | Notes                                                                   |
| --------------------------------- | ---- | -------- | ----------------------------------------------------------------------- |
| `--forms-package`               | Path | Yes      | Path to forms_package.json                                              |
| `--tenants`                     | Path | Yes      | Path to tenants.json                                                    |
| `--db-user`                     | Text | Yes      | SQL Server login name                                                   |
| `--db-password`                 | Text | Yes      | SQL Server login password                                               |
| `--db-driver`                   | Text | No       | Installed pyodbc driver                                                 |
| `--apply`                       | Flag | No       | Commit the transactions; otherwise dry run with rollback                |
| `--yes`                         | Flag | No       | Skip the update confirmation prompt on`--apply`, for unattended runs. |

---

`forms_package.json` (one entry per form):

```text
{
  "base_dir": "Univeris-Network-Express-Forms-Library-May-15-2026",
  "forms": [
    {
      "form_code": "AIM-NAAF-INVESTMENT",
      "english_name": "AIM NAAF INVESTMENT",
      "french_name": "AIM NAAF INVESTMENT",
      "eng_pdf": "AIM/AIM-NAAF-INVESTMENT-EN-0225.pdf",
      "fre_pdf": "AIM/AIM-NAAF-INVESTMENT-FR-0225.pdf",
      "status": "A",
      "start_date": "2026-08-17",
      "end_date": "2030-12-31",
      "tags": ["NAAF", "AIM"]
    },
    ...
  ]
}
```

## Forms Package Parameters

| Parameter                          | Required | Default (at creation)                       | On update (existing form)                                                     | Description                                                        |
| ---------------------------------- | -------- | ------------------------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `form_code`                      | Yes      | –                                          | Lookup key (never changed)                                                    | SSF_ID, globally unique in the database (max 30 chars)             |
| `english_name` / `french_name` | Yes      | –                                          | Replaced when content differs                                                 | Template display names (max 150 chars)                             |
| `eng_pdf` / `fre_pdf`          | Yes      | –                                          | Replaced when content (SHA-256) differs                                       | Paths to the English and French PDF files, relative to`base_dir` |
| `tags`                           | No       | No tags                                     | Full replace when supplied (`[]` removes all tags); omit to leave untouched | Tag names; each must already exist in`S_SSF_TAG`                 |
| `status`                         | No       | A                                           | Synced when supplied; omit to leave unchanged                                 | A (Active), I (Inactive), E (Expired)                              |
| `start_date` / `end_date`      | No       | `start_date` = today, `end_date` = none | Synced when supplied; omit to leave unchanged                                 | YYYY-MM-DD                                                         |

---

`tenants.json` (one entry per target tenant database):

```text
{
  "FLEX_TEST": {
    "db_host": "example-uat-db.flex.univeris.com",
    "db_name": "uvs_ba"
  },
  ...
}
```

## Additional Information

There are several validations executed as part of the process e.g.

- Duplicate form codes
- Missing PDF files
- One PDF shared by two forms
- Identical English/French files
- Tags in the package are checked against `S_SSF_TAG`

## Time Saved (Forms + related business Rules)

| Task                        | Manual | Automated  | Reduction |
| --------------------------- | ------ | ---------- | --------- |
| 1 form, 1 tenant            | 2 h    | ~1 min     | 99%       |
| 60 form release, 1 tenant   | 120 h  | ~15 min    | 99%       |
| 60 form release, 15 tenants | 1800 h | under 12 h | 99%       |
