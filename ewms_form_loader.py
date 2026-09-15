"""EWMS SSF form loader.

This tool loads bilingual (English and French) SSF forms into the EWMS
SQL Server database. For each form, it reads the two PDF files, extracts
the PDF field names, and inserts or updates the form rows in one
database transaction. It verifies the written rows before commit.

Commands:
  load-single-form  Load one form from two PDF paths into one tenant.
  load-package      Load every form in forms_package.json into every
                    tenant in tenants.json (runs load-single-form in a loop)

Safety:
  The default run executes the full transaction and then rolls it back.
  Supply --apply to commit. The tool warns before it changes existing
  rows, and it reports all warnings again at the end of the run.
  Supply --yes to skip the update confirmation prompt (unattended runs).

Note:
  A cache refresh in EWMS may be necessary before the changes show in
  the UI.
"""


import argparse
import difflib
import hashlib
import io
import json
import os
import re
import sys
import pyodbc
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree
from pypdf import PdfReader


LANGUAGES = ("ENG", "FRE")
STATUS_CODES = {"A", "I", "E"}
MAX_LENGTHS = {
    "ssf_id": 30,
    "name": 150,
    "tag": 20,
}
CACHE_NOTE = (
    "NOTE: you may need to clear/refresh EWMS cache to see the changes "
    "reflect in the UI."
)

_RESOLVED_DRIVERS: dict[str, str] = {}

WARNINGS: list[str] = []
CONFIRM_ALL = False

class LoaderError(RuntimeError):
    """An expected validation or loading failure."""


def _warn(message: str) -> None:
    """Record the warning for the end-of-run report and print it to stderr."""
    WARNINGS.append(message)
    print(f"WARNING: {message}", file=sys.stderr)


def read_json_file(path: str | Path, label: str) -> Any:
    """Read a JSON file and return the parsed data.

    Fail when the file does not exist or does not contain valid JSON.
    """
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise LoaderError(f"{label} file not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoaderError(f"{label} file is not valid JSON ({p}): {exc}")


def sha256(data: bytes) -> str:
    """Return the SHA-256 digest of the data as a lowercase hex string."""
    return hashlib.sha256(data).hexdigest()


def _has_field_children(field: Any) -> bool:
    """Return True when the PDF field has a child that is itself a named or
    typed field. Such a field is a parent node, not a terminal field."""
    for child_ref in field.get("/Kids", []):
        child = child_ref.get_object()
        if child.get("/T") is not None or child.get("/FT") is not None:
            return True
    return False


def extract_pdf_metadata(data: bytes) -> dict[str, Any]:
    """Extract the terminal AcroForm field names from the PDF bytes.

    Return the sorted field names and an XML document that lists them.
    """
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        fields = reader.get_fields() or {}
        terminal_names = sorted(
            name
            for name, field in fields.items()
            if field.get("/FT") is not None and not _has_field_children(field)
        )
    except Exception as exc:
        raise LoaderError(f"cannot parse PDF: {exc}") from exc
    root = ElementTree.Element("fields")
    for name in terminal_names:
        ElementTree.SubElement(root, "field", {"name": name})
    return {
        "field_names": terminal_names,
        "fields_xml": ElementTree.tostring(root, encoding="unicode"),
    }


def _iso_date(value: str | None, field: str) -> str | None:
    """Validate a YYYY-MM-DD string and return it in ISO format.

    Return None when the value is empty.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise LoaderError(f"{field} must use YYYY-MM-DD") from exc


def _db_date(value: Any) -> str | None:
    """Convert a date or datetime value from the database to an ISO string.

    Return None when the value is None.
    """
    if value is None:
        return None
    if hasattr(value, "date"):
        value = value.date()
    return value.isoformat()


def _check_length(value: str, key: str, label: str) -> None:
    """Fail when the value is longer than the column limit in MAX_LENGTHS."""
    if len(value) > MAX_LENGTHS[key]:
        raise LoaderError(f"{label} exceeds {MAX_LENGTHS[key]} characters")


def validate_form_args(args: argparse.Namespace) -> list[str] | None:
    """Validate the form arguments and return the cleaned tag list.

    Return None when the user did not supply tags.
    """
    for name, key in (
        ("--form-code", "ssf_id"),
        ("--english-name", "name"),
        ("--french-name", "name"),
    ):
        attr = name.lstrip("-").replace("-", "_")
        value = getattr(args, attr)
        if not value.strip():
            raise LoaderError(f"{name} must be a non-empty string")
        _check_length(value, key, name)
    _iso_date(args.start_date, "--start-date")
    _iso_date(args.end_date, "--end-date")
    if args.tags is None:
        return None
    tags = [tag.strip() for tag in args.tags if tag.strip()]
    for tag in tags:
        _check_length(tag, "tag", f"tag {tag!r}")
    if len({tag.casefold() for tag in tags}) != len(tags):
        raise LoaderError("--tags contains duplicates")
    return tags


def _read_pdf(path_value: str, label: str) -> tuple[bytes, str]:
    """Read the PDF file and return its bytes and its file name.

    Fail when the file does not exist.
    """
    pdf_path = Path(path_value).expanduser().resolve()
    if not pdf_path.is_file():
        raise LoaderError(f"{label} does not exist: {pdf_path}")
    return pdf_path.read_bytes(), pdf_path.name


def build_manifest(
    args: argparse.Namespace, tags: list[str] | None
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Read the two PDF files and build the form manifest.

    Return the manifest and the PDF bytes for each language.
    """
    templates: dict[str, Any] = {}
    pdfs: dict[str, bytes] = {}
    for lang, path_value, name, label in (
        ("ENG", args.eng_pdf, args.english_name, "--eng-pdf"),
        ("FRE", args.fre_pdf, args.french_name, "--fre-pdf"),
    ):
        data, file_name = _read_pdf(path_value, label)
        metadata = extract_pdf_metadata(data)
        if not metadata["field_names"]:
            _warn(
                f"{file_name} has no fillable AcroForm fields "
                "(flattened or scanned PDF?)"
            )
        pdfs[lang] = data
        templates[lang] = {
            "file_name": file_name,
            "name": name,
            "sha256": sha256(data),
            "byte_length": len(data),
            **metadata,
        }
    manifest = {
        "form": {
            "ssf_id": args.form_code,
            "dlr_sysid": args.dlr_sysid,
            "status_cd": args.status,
            "edit_cd": "NM",
            "start_date": _iso_date(args.start_date, "--start-date"),
            "expiry_date": _iso_date(args.end_date, "--end-date"),
            "doc_entity_cd": None,
            "doc_type_cd": None,
            "page_entity": None,
            "bf_blank_process": 4095,
            "bf_field_opt": 0,
            "bf_consolidation_process": 0,
            "e_signature_eligible": False,
            "tags": tags,
            "display_orders": [{"process_cd": "AO", "display_order": None}],
        },
        "templates": templates,
    }
    return manifest, pdfs


def resolve_db_driver(requested: str) -> str:
    """Return the name of an installed SQL Server ODBC driver.

    When the requested driver is absent, use the newest installed
    "ODBC Driver NN for SQL Server", or fail with install guidance.
    """
    cached = _RESOLVED_DRIVERS.get(requested)
    if cached:
        return cached
    installed = pyodbc.drivers()
    if requested in installed:
        resolved = requested
    else:
        sql_drivers = sorted(
            (d for d in installed if re.fullmatch(r"ODBC Driver \d+ for SQL Server", d)),
            key=lambda d: int(d.split()[2]), reverse=True)
        if not sql_drivers:
            raise LoaderError(
                f"no Microsoft ODBC Driver for SQL Server is installed on this machine "
                f"(requested '{requested}'; installed ODBC drivers: "
                f"{', '.join(installed) or 'none'}).\n"
                f"  -> one-time install (needs admin):  winget install -e --id Microsoft.msodbcsql.18\n"
                f"  -> or download the MSI from "
                f"https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server\n"
                f"  -> then re-run this script; no other arguments need to change")
        resolved = sql_drivers[0]
        print(f"note: ODBC driver '{requested}' is not installed; "
              f"using '{resolved}' instead", flush=True)
    _RESOLVED_DRIVERS[requested] = resolved
    return resolved


def _connection_string(args: argparse.Namespace) -> str:
    """Build the pyodbc connection string from the arguments or from the
    EWMS_DB_CONNECTION environment variable. Fail when no credentials are supplied."""
    value = args.connection_string or os.environ.get("EWMS_DB_CONNECTION")
    if value:
        return value
    if bool(args.db_user) != bool(args.db_password):
        raise LoaderError("--db-user and --db-password must be supplied together")
    if args.db_user and args.db_password:
        if not args.db_host or not args.db_name:
            raise LoaderError(
                "--db-host and --db-name are required when using a SQL login"
            )
        escape = lambda item: "{" + str(item).replace("}", "}}") + "}"
        return (
            f"DRIVER={escape(resolve_db_driver(args.db_driver))};"
            f"SERVER={escape(args.db_host)};"
            f"DATABASE={escape(args.db_name)};"
            f"UID={escape(args.db_user)};"
            f"PWD={escape(args.db_password)};"
            "Encrypt=yes;TrustServerCertificate=yes"
        )
    raise LoaderError(
        "Set EWMS_DB_CONNECTION or pass --db-user and --db-password; "
        "credentials are never stored"
    )


def _resolve_dlr_sysid(cursor: pyodbc.Cursor, requested: int | None) -> int:
    """Return the dealer SYSID for the form.

    When no value is requested, use the single SSF_LIBRARY candidate, or fail.
    """
    if requested is not None:
        return requested
    candidates = [
        row[0]
        for row in cursor.execute(
            "SELECT DLR_SYSID FROM SSF_LIBRARY WHERE DLR_SYSID <> 0 ORDER BY DLR_SYSID"
        ).fetchall()
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise LoaderError(
        f"Cannot auto-resolve DLR_SYSID; SSF_LIBRARY candidates are {candidates}. "
        "Pass --dlr-sysid explicitly."
    )


def _tag_dictionary(cursor: pyodbc.Cursor) -> dict[str, tuple[int, str]]:
    """Read all tags from S_SSF_TAG and return them by normalized name.

    Each value holds the tag code and the display name.
    """
    by_name: dict[str, tuple[int, str]] = {}
    for tag_cd, name in cursor.execute("SELECT TAG_CD, NAME FROM S_SSF_TAG").fetchall():
        key = name.strip().casefold()
        if key in by_name:
            raise LoaderError(f"Duplicate normalized tag name in target: {name.strip()}")
        by_name[key] = (tag_cd, name.strip())
    return by_name


def _diff_tags(
    cursor: pyodbc.Cursor, ssf_id: str, desired: list[str] | None
) -> tuple[dict[str, int], dict[str, int], list[str]]:
    """Compare the desired tags with the form's current tags.

    Return the tags to add, the tags to remove (each as {name: tag_cd}), and
    the unchanged names. desired=None means tags were not supplied, so the
    function reports no changes.
    """
    rows = cursor.execute(
        "SELECT t.TAG_CD, s.NAME FROM SSF_TAG t "
        "JOIN S_SSF_TAG s ON s.TAG_CD = t.TAG_CD WHERE t.SSF_ID = ?",
        ssf_id,
    ).fetchall()
    current = {
        name.strip().casefold(): (tag_cd, name.strip()) for tag_cd, name in rows
    }
    if desired is None:
        return {}, {}, sorted(entry[1] for entry in current.values())
    dictionary = _tag_dictionary(cursor)
    to_add: dict[str, int] = {}
    unchanged: list[str] = []
    desired_keys: set[str] = set()
    for name in desired:
        key = name.strip().casefold()
        desired_keys.add(key)
        if key in current:
            unchanged.append(current[key][1])
        elif key in dictionary:
            to_add[dictionary[key][1]] = dictionary[key][0]
        else:
            raise LoaderError(f"Tag {name!r} does not exist in target S_SSF_TAG")
    to_remove = {
        entry[1]: entry[0] for key, entry in current.items() if key not in desired_keys
    }
    return to_add, to_remove, sorted(unchanged)


def _preflight(cursor: pyodbc.Cursor, manifest: dict[str, Any]) -> dict[str, int]:
    """Check the target database before a new-form insert.

    Fail when the form already exists, or when the library row, a process
    code, or a tag is absent. Return the resolved tag IDs.
    """
    form = manifest["form"]
    if cursor.execute(
        "SELECT COUNT(*) FROM SSF_REGISTER WHERE SSF_ID = ?", form["ssf_id"]
    ).fetchval():
        raise LoaderError(f"SSF_ID already exists globally: {form['ssf_id']}")
    if not cursor.execute(
        "SELECT 1 FROM SSF_LIBRARY WHERE DLR_SYSID = ?", form["dlr_sysid"]
    ).fetchone():
        raise LoaderError(f"No SSF_LIBRARY row for DLR_SYSID {form['dlr_sysid']}")
    target_processes = {
        item["process_cd"] for item in form["display_orders"]
    }
    known_processes = {
        row[0]
        for row in cursor.execute(
            "SELECT SSF_PROCESS_CD FROM S_SSF_PROCESS"
        ).fetchall()
    }
    unknown_processes = sorted(target_processes - known_processes)
    if unknown_processes:
        raise LoaderError(
            f"Unknown process codes: {', '.join(unknown_processes)}"
        )
    by_name = _tag_dictionary(cursor)
    resolved: dict[str, int] = {}
    for name in form["tags"] or []:
        key = name.strip().casefold()
        if key not in by_name:
            raise LoaderError(f"Tag {name!r} does not exist in target S_SSF_TAG")
        resolved[name] = by_name[key][0]
    return resolved


def _allocate_binary_ids(cursor: pyodbc.Cursor, count: int = 2) -> tuple[int, ...]:
    """Allocate sequential BINARY_STORE_DATA IDs with the NTX allocator
    procedure and return them."""
    row = cursor.execute(
        """
        DECLARE @sysid int;
        EXEC mp_MPS_CFN_GET_SYSID_NTX @count = ?, @sysid = @sysid OUTPUT;
        SELECT @sysid;
        """,
        count,
    ).fetchone()
    if not row:
        raise LoaderError("NTX allocator returned no ID")
    return tuple(row[0] + offset for offset in range(count))


def _insert_and_verify(
    cursor: pyodbc.Cursor,
    manifest: dict[str, Any],
    pdfs: dict[str, bytes],
    tag_ids: dict[str, int],
) -> tuple[tuple[int, int], list[dict[str, int | str]]]:
    """Insert all rows and PDF binaries for a new form, then verify the row
    counts and the stored PDF hashes.

    Return the allocated binary IDs and the resolved display orders.
    """
    form = manifest["form"]
    binary_ids = _allocate_binary_ids(cursor)
    for lang, binary_id in zip(LANGUAGES, binary_ids):
        cursor.execute(
            "INSERT INTO BINARY_STORE_DATA (BINARY_STORE_DATA_ID, BD_ID, DATA) "
            "VALUES (?, ?, ?)",
            binary_id,
            f"TEMPLATE_{form['dlr_sysid']}{form['ssf_id']}{lang}",
            pyodbc.Binary(pdfs[lang]),
        )
    cursor.execute(
        """
        INSERT INTO SSF_REGISTER (
          SSF_ID, DLR_SYSID, START_DATE, EXPIRY_DATE, CREATE_DATE, UPDATE_DATE,
          EDIT_CD, STATUS_CD, DD_EXT_XSLT_BD_ID, SUMMARY, DOC_ENTITY_CD,
          DOC_TYPE_CD, PAGE_ENTITY, BF_BLANK_PROCESS, BF_FIELD_OPT,
          BF_CONSOLIDATION_PROCESS, E_SIGNATURE_ELIGIBLE
        ) VALUES (
          ?, ?, ?, ?, GETDATE(), GETDATE(), ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        form["ssf_id"],
        form["dlr_sysid"],
        form.get("start_date"),
        form.get("expiry_date"),
        form["edit_cd"],
        form["status_cd"],
        form.get("doc_entity_cd"),
        form.get("doc_type_cd"),
        form.get("page_entity"),
        form["bf_blank_process"],
        form["bf_field_opt"],
        form["bf_consolidation_process"],
        form["e_signature_eligible"],
    )
    for lang, binary_id in zip(LANGUAGES, binary_ids):
        template = manifest["templates"][lang]
        cursor.execute(
            """
            INSERT INTO SSF_TEMPLATE (
              SSF_ID, DLR_SYSID, LANG_CD, NAME, PDF_TEMPLATE_BD_ID,
              PDF_TEMPLATE_FIELDS, FIELD_VALIDATION_STATUS, FIELD_VALIDATION_DTL,
              LOGO_POS_TOP, LOGO_POS_LEFT, LOGO_HEIGHT, LOGO_IND, FILE_NAME
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, 0, ?)
            """,
            form["ssf_id"],
            form["dlr_sysid"],
            lang,
            template["name"],
            binary_id,
            template["fields_xml"],
            template["file_name"],
        )
    for tag_id in tag_ids.values():
        cursor.execute(
            "INSERT INTO SSF_TAG (SSF_ID, TAG_CD) VALUES (?, ?)",
            form["ssf_id"],
            tag_id,
        )
    resolved_display_orders = []
    for item in form["display_orders"]:
        process_cd = item["process_cd"]
        display_order = item.get("display_order")
        if display_order is None:
            display_order = cursor.execute(
                """
                SELECT COALESCE(MAX(DISPLAY_ORDER), 0) + 1
                FROM SSF_REGISTER_DISPLAY_ORDER WITH (UPDLOCK, HOLDLOCK)
                WHERE SSF_PROCESS_CD = ?
                """,
                process_cd,
            ).fetchval()
        cursor.execute(
            """
            INSERT INTO SSF_REGISTER_DISPLAY_ORDER
              (SSF_ID, DISPLAY_ORDER, SSF_PROCESS_CD)
            VALUES (?, ?, ?)
            """,
            form["ssf_id"],
            display_order,
            process_cd,
        )
        resolved_display_orders.append(
            {"process_cd": process_cd, "display_order": display_order}
        )
    counts = cursor.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM SSF_REGISTER WHERE SSF_ID = ?),
          (SELECT COUNT(*) FROM SSF_TEMPLATE WHERE SSF_ID = ?),
          (SELECT COUNT(*) FROM SSF_TAG WHERE SSF_ID = ?),
          (SELECT COUNT(*) FROM SSF_REGISTER_DISPLAY_ORDER WHERE SSF_ID = ?)
        """,
        *([form["ssf_id"]] * 4),
    ).fetchone()
    expected = (1, 2, len(tag_ids), len(resolved_display_orders))
    if tuple(counts) != expected:
        raise LoaderError(f"Post-insert row counts {tuple(counts)} != {expected}")
    hashes = cursor.execute(
        """
        SELECT BINARY_STORE_DATA_ID,
               LOWER(CONVERT(varchar(64),
                 HASHBYTES('SHA2_256', CONVERT(varbinary(max), DATA)), 2))
        FROM BINARY_STORE_DATA
        WHERE BINARY_STORE_DATA_ID IN (?, ?)
        """,
        *binary_ids,
    ).fetchall()
    actual_hashes = {row[0]: row[1] for row in hashes}
    for lang, binary_id in zip(LANGUAGES, binary_ids):
        if actual_hashes.get(binary_id) != manifest["templates"][lang]["sha256"]:
            raise LoaderError(f"Stored {lang} PDF hash mismatch")
    return binary_ids, resolved_display_orders


def _filename_similarity(old_name: str, new_name: str) -> float:
    """Return a similarity ratio (0 to 1) for two file names.

    The comparison ignores the extension, digits, and language tokens.
    """

    def normalize(name: str) -> str:
        stem = Path(name).stem.casefold()
        tokens = re.split(r"[^a-z]+", stem)
        return "".join(t for t in tokens if t and t not in ("en", "fr", "eng", "fre"))

    return difflib.SequenceMatcher(None, normalize(old_name), normalize(new_name)).ratio()


def _update_existing_form(
    connection: pyodbc.Connection,
    cursor: pyodbc.Cursor,
    manifest: dict[str, Any],
    pdfs: dict[str, bytes],
    apply_changes: bool,
) -> str:
    """Update an existing form (PDFs, names, tags, status, dates) after review
    and confirmation. Change only the values that differ from the stored rows,
    and return 'unchanged', 'update', 'tags_only', or 'aborted'.
    """
    form = manifest["form"]
    rows = cursor.execute(
        """
        SELECT t.LANG_CD, t.PDF_TEMPLATE_BD_ID, t.FILE_NAME, t.NAME,
               LOWER(CONVERT(varchar(64),
                 HASHBYTES('SHA2_256', CONVERT(varbinary(max), b.DATA)), 2))
        FROM SSF_TEMPLATE t
        LEFT JOIN BINARY_STORE_DATA b ON b.BINARY_STORE_DATA_ID = t.PDF_TEMPLATE_BD_ID
        WHERE t.SSF_ID = ?
        """,
        form["ssf_id"],
    ).fetchall()
    existing = {
        row[0]: {
            "binary_id": row[1],
            "file_name": row[2],
            "name": row[3],
            "sha256": row[4],
        }
        for row in rows
    }
    register = cursor.execute(
        "SELECT STATUS_CD, START_DATE, EXPIRY_DATE FROM SSF_REGISTER "
        "WHERE SSF_ID = ?",
        form["ssf_id"],
    ).fetchone()
    # Sync register fields and template names only when supplied (None means
    # "leave untouched", matching the tags convention).
    register_updates: dict[str, dict[str, Any]] = {}
    for column, supplied, stored in (
        ("STATUS_CD", form["status_cd"], (register[0] or "").strip()),
        ("START_DATE", form.get("start_date"), _db_date(register[1])),
        ("EXPIRY_DATE", form.get("expiry_date"), _db_date(register[2])),
    ):
        if supplied is not None and supplied != stored:
            register_updates[column] = {"old": stored, "new": supplied}
    # Template names are required arguments, so diff them the same way.
    name_updates: dict[str, dict[str, str]] = {}
    for lang in LANGUAGES:
        if lang not in existing:
            continue
        supplied_name = manifest["templates"][lang]["name"].strip()
        stored_name = (existing[lang]["name"] or "").strip()
        if supplied_name != stored_name:
            name_updates[lang] = {"old": stored_name, "new": supplied_name}
    # missing: no SSF_TEMPLATE row at all; broken: template row whose stored
    # PDF binary is gone (e.g. removed via the UI). Both get fresh binaries.
    missing = [lang for lang in LANGUAGES if lang not in existing]
    broken = [
        lang
        for lang in LANGUAGES
        if lang in existing and existing[lang]["sha256"] is None
    ]
    changed = [
        lang
        for lang in LANGUAGES
        if lang in existing
        and existing[lang]["sha256"] is not None
        and manifest["templates"][lang]["sha256"] != existing[lang]["sha256"]
    ]
    tags_to_add, tags_to_remove, tags_unchanged = _diff_tags(
        cursor, form["ssf_id"], form["tags"]
    )
    tags_changed = bool(tags_to_add or tags_to_remove)
    if tags_to_remove:
        _warn(
            f"{form['ssf_id']}: existing tag(s) not in the supplied list will "
            f"be removed: {', '.join(sorted(tags_to_remove))}"
        )
    restored = missing + broken
    metadata_changed = bool(register_updates or name_updates)
    if not changed and not restored and not tags_changed and not metadata_changed:
        print(
            json.dumps(
                {
                    "mode": "UPDATE",
                    "ssf_id": form["ssf_id"],
                    "result": "supplied values match stored values; nothing to do",
                },
                indent=2,
            )
        )
        connection.rollback()
        return "unchanged"
    filename_mismatches = [
        lang
        for lang in changed
        if _filename_similarity(
            existing[lang]["file_name"], manifest["templates"][lang]["file_name"]
        )
        < 0.6
    ]
    for lang in filename_mismatches:
        _warn(
            f"{form['ssf_id']} {lang}: stored file "
            f"{existing[lang]['file_name']!r} looks unrelated to new file "
            f"{manifest['templates'][lang]['file_name']!r}; verify the form "
            "code is not crossed with another form"
        )
    # Confirm BEFORE writing anything: once the UPDATEs/INSERTs below run,
    # the open transaction holds locks on live tenant tables until commit,
    # and an unattended input() prompt would block the EWMS app.
    global CONFIRM_ALL
    if apply_changes and not CONFIRM_ALL:
        actions = []
        if changed:
            actions.append(f"replace its {', '.join(changed)} PDF(s)")
        if restored:
            actions.append(f"load its missing {', '.join(restored)} PDF(s)")
        if tags_to_add:
            actions.append(f"add tag(s) {', '.join(sorted(tags_to_add))}")
        if tags_to_remove:
            actions.append(f"remove tag(s) {', '.join(sorted(tags_to_remove))}")
        for column, change in register_updates.items():
            actions.append(
                f"set {column} {change['old']!r} -> {change['new']!r}"
            )
        for lang, change in name_updates.items():
            actions.append(
                f"rename {lang} {change['old']!r} -> {change['new']!r}"
            )
        try:
            answer = input(
                f"Form {form['ssf_id']} already exists. {'; '.join(actions)}? "
                "[y = yes / a = yes to all remaining / anything else = skip]: "
            ).strip().lower()
        except EOFError:
            connection.rollback()
            raise LoaderError(
                "non-interactive session: cannot show the update confirmation "
                "prompt; re-run with --yes to skip it, or run from an "
                "interactive terminal"
            )
        if answer == "a":
            CONFIRM_ALL = True
        elif answer not in ("y", "yes"):
            connection.rollback()
            print("Skipped: no changes were made to this form")
            return "aborted"
    for lang in changed:
        template = manifest["templates"][lang]
        cursor.execute(
            "UPDATE BINARY_STORE_DATA SET DATA = ? WHERE BINARY_STORE_DATA_ID = ?",
            pyodbc.Binary(pdfs[lang]),
            existing[lang]["binary_id"],
        )
        cursor.execute(
            "UPDATE SSF_TEMPLATE SET PDF_TEMPLATE_FIELDS = ?, FILE_NAME = ? "
            "WHERE SSF_ID = ? AND LANG_CD = ?",
            template["fields_xml"],
            template["file_name"],
            form["ssf_id"],
            lang,
        )
    new_binary_ids: dict[str, int] = {}
    if restored:
        for lang, binary_id in zip(
            restored, _allocate_binary_ids(cursor, len(restored))
        ):
            new_binary_ids[lang] = binary_id
            template = manifest["templates"][lang]
            cursor.execute(
                "INSERT INTO BINARY_STORE_DATA (BINARY_STORE_DATA_ID, BD_ID, DATA) "
                "VALUES (?, ?, ?)",
                binary_id,
                f"TEMPLATE_{form['dlr_sysid']}{form['ssf_id']}{lang}",
                pyodbc.Binary(pdfs[lang]),
            )
            if lang in broken:
                cursor.execute(
                    "UPDATE SSF_TEMPLATE SET PDF_TEMPLATE_BD_ID = ?, "
                    "PDF_TEMPLATE_FIELDS = ?, FILE_NAME = ? "
                    "WHERE SSF_ID = ? AND LANG_CD = ?",
                    binary_id,
                    template["fields_xml"],
                    template["file_name"],
                    form["ssf_id"],
                    lang,
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO SSF_TEMPLATE (
                      SSF_ID, DLR_SYSID, LANG_CD, NAME, PDF_TEMPLATE_BD_ID,
                      PDF_TEMPLATE_FIELDS, FIELD_VALIDATION_STATUS,
                      FIELD_VALIDATION_DTL, LOGO_POS_TOP, LOGO_POS_LEFT,
                      LOGO_HEIGHT, LOGO_IND, FILE_NAME
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, 0, ?)
                    """,
                    form["ssf_id"],
                    form["dlr_sysid"],
                    lang,
                    template["name"],
                    binary_id,
                    template["fields_xml"],
                    template["file_name"],
                )
    for name, tag_cd in tags_to_add.items():
        cursor.execute(
            "INSERT INTO SSF_TAG (SSF_ID, TAG_CD) VALUES (?, ?)",
            form["ssf_id"],
            tag_cd,
        )
    for name, tag_cd in tags_to_remove.items():
        cursor.execute(
            "DELETE FROM SSF_TAG WHERE SSF_ID = ? AND TAG_CD = ?",
            form["ssf_id"],
            tag_cd,
        )
    if tags_changed:
        tag_count = cursor.execute(
            "SELECT COUNT(*) FROM SSF_TAG WHERE SSF_ID = ?", form["ssf_id"]
        ).fetchval()
        expected_tags = len(tags_unchanged) + len(tags_to_add)
        if tag_count != expected_tags:
            raise LoaderError(
                f"Post-update tag count {tag_count} != expected {expected_tags}"
            )
    for column, change in register_updates.items():
        # column names come from the fixed tuple above, never from input
        cursor.execute(
            f"UPDATE SSF_REGISTER SET {column} = ? WHERE SSF_ID = ?",
            change["new"],
            form["ssf_id"],
        )
    for lang, change in name_updates.items():
        cursor.execute(
            "UPDATE SSF_TEMPLATE SET NAME = ? WHERE SSF_ID = ? AND LANG_CD = ?",
            change["new"],
            form["ssf_id"],
            lang,
        )
    cursor.execute(
        "UPDATE SSF_REGISTER SET UPDATE_DATE = GETDATE() WHERE SSF_ID = ?",
        form["ssf_id"],
    )
    for lang in changed + restored:
        binary_id = (
            new_binary_ids[lang] if lang in new_binary_ids
            else existing[lang]["binary_id"]
        )
        stored_hash = cursor.execute(
            """
            SELECT LOWER(CONVERT(varchar(64),
              HASHBYTES('SHA2_256', CONVERT(varbinary(max), DATA)), 2))
            FROM BINARY_STORE_DATA WHERE BINARY_STORE_DATA_ID = ?
            """,
            binary_id,
        ).fetchval()
        if stored_hash != manifest["templates"][lang]["sha256"]:
            raise LoaderError(f"Updated {lang} PDF hash mismatch")
    summary = {
        "mode": "UPDATE " + ("APPLY" if apply_changes else "DRY RUN"),
        "ssf_id": form["ssf_id"],
        "changed_languages": changed,
        "restored_languages": restored,
        "pdfs": {
            lang: {
                "old_file_name": (
                    existing[lang]["file_name"]
                    if lang in existing
                    else "(template row was missing)"
                ),
                "old_pdf": (
                    "(stored PDF binary was missing)" if lang in broken else None
                ),
                "new_file_name": manifest["templates"][lang]["file_name"],
                "old_sha256": (
                    existing[lang]["sha256"] if lang in existing else None
                ),
                "new_sha256": manifest["templates"][lang]["sha256"],
                "new_field_count": len(manifest["templates"][lang]["field_names"]),
            }
            for lang in changed + restored
        },
        "tags": (
            "not supplied; left untouched"
            if form["tags"] is None
            else {
                "add": sorted(tags_to_add),
                "remove": sorted(tags_to_remove),
                "unchanged": tags_unchanged,
            }
        ),
        "unchanged": {
            "tags": not tags_changed,
            "status": "STATUS_CD" not in register_updates,
            "names": not name_updates,
            "dates": not (
                set(register_updates) & {"START_DATE", "EXPIRY_DATE"}
            ),
        },
        "pdf_hashes_verified": True,
    }
    if register_updates:
        summary["register_updates"] = register_updates
    if name_updates:
        summary["name_updates"] = name_updates
    if filename_mismatches:
        summary["filename_mismatch_warning"] = filename_mismatches
    print(json.dumps(summary, indent=2))
    if not apply_changes:
        connection.rollback()
        print("DRY RUN: update rolled back; re-run with --apply to commit")
        return (
            "update"
            if changed or restored or metadata_changed
            else "tags_only"
        )
    connection.commit()
    print("Committed.")
    return "update" if changed or restored or metadata_changed else "tags_only"


def load_single_form(args: argparse.Namespace) -> str:
    """Create a new form, or update the form when it already exists.

    Return 'create', 'update', 'tags_only', 'unchanged', or 'aborted'.
    """
    tags = validate_form_args(args)
    connection = pyodbc.connect(_connection_string(args), autocommit=False)
    try:
        cursor = connection.cursor()
        existing_dlr = cursor.execute(
            "SELECT DLR_SYSID FROM SSF_REGISTER WHERE SSF_ID = ?", args.form_code
        ).fetchval()
        if existing_dlr is not None:
            args.dlr_sysid = existing_dlr
            manifest, pdfs = build_manifest(args, tags)
            return _update_existing_form(connection, cursor, manifest, pdfs, args.apply)
        args.dlr_sysid = _resolve_dlr_sysid(cursor, args.dlr_sysid)
        if args.status is None:
            args.status = "A"
        if args.start_date is None:
            args.start_date = date.today().isoformat()
        manifest, pdfs = build_manifest(args, tags)
        form = manifest["form"]
        tag_ids = _preflight(cursor, manifest)
        binary_ids, display_orders = _insert_and_verify(
            cursor, manifest, pdfs, tag_ids
        )
        summary = {
            "mode": "APPLY" if args.apply else "DRY RUN",
            "ssf_id": form["ssf_id"],
            "dlr_sysid": form["dlr_sysid"],
            "binary_ids": binary_ids,
            "tag_ids": tag_ids,
            "display_orders": display_orders,
            "pdfs": {
                lang: {
                    "file_name": manifest["templates"][lang]["file_name"],
                    "byte_length": manifest["templates"][lang]["byte_length"],
                    "sha256": manifest["templates"][lang]["sha256"],
                    "field_count": len(
                        manifest["templates"][lang]["field_names"]
                    ),
                }
                for lang in LANGUAGES
            },
            "row_counts_verified": True,
            "pdf_hashes_verified": True,
        }
        if args.apply:
            connection.commit()
        else:
            connection.rollback()
            summary["database_changes"] = "rolled back"
        print(json.dumps(summary, indent=2))
        return "create"
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _load_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines from a .env file and return them as a dictionary.

    Return an empty dictionary when the file does not exist.
    """
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def _package_preflight(forms: list[dict[str, Any]], base_dir: Path) -> None:
    """Validate every form entry and PDF in the package manifest.

    Fail with the full problem list before the tool contacts a tenant.
    """
    problems: list[str] = []
    seen_codes: dict[str, str] = {}
    seen_paths: dict[str, str] = {}
    for form in forms:
        code = form["form_code"]
        key = code.strip().casefold()
        if key in seen_codes:
            problems.append(f"duplicate form_code: {code} and {seen_codes[key]}")
        seen_codes[key] = code
        status = form.get("status")
        if status is not None and status not in STATUS_CODES:
            problems.append(
                f"{code}: invalid status {status!r} "
                f"(must be one of {', '.join(sorted(STATUS_CODES))})"
            )
        for date_key in ("start_date", "end_date"):
            value = form.get(date_key)
            if value is not None:
                try:
                    date.fromisoformat(value)
                except (TypeError, ValueError):
                    problems.append(
                        f"{code}: {date_key} must use YYYY-MM-DD, got {value!r}"
                    )
        hashes: dict[str, str] = {}
        for pdf_key in ("eng_pdf", "fre_pdf"):
            rel = form[pdf_key]
            path = base_dir / rel
            if not path.is_file():
                problems.append(f"{code}: {pdf_key} does not exist: {rel}")
                continue
            if rel in seen_paths and seen_paths[rel] != code:
                problems.append(
                    f"PDF used by multiple forms: {rel} ({seen_paths[rel]}, {code})"
                )
            seen_paths.setdefault(rel, code)
            data = path.read_bytes()
            hashes[pdf_key] = sha256(data)
            try:
                extract_pdf_metadata(data)
            except LoaderError as exc:
                problems.append(f"{code}: {pdf_key}: {exc}")
        if len(hashes) == 2 and hashes["eng_pdf"] == hashes["fre_pdf"]:
            problems.append(f"{code}: eng_pdf and fre_pdf are identical files")
    if problems:
        raise LoaderError(
            "package preflight failed:\n  " + "\n  ".join(problems)
        )


def load_package_forms(args: argparse.Namespace) -> int:
    """Run load_single_form for every form in the package on every tenant.

    Print a package summary and return exit code 0, or 2 when a load failed.
    """
    if os.environ.get("EWMS_DB_CONNECTION"):
        raise LoaderError(
            "Unset EWMS_DB_CONNECTION for package loads; it would send every "
            "tenant's forms to one database"
        )
    package_path = Path(args.forms_package).expanduser().resolve()
    package = read_json_file(package_path, "forms package")
    tenants = read_json_file(args.tenants, "tenants")
    forms = package.get("forms")
    if not forms:
        raise LoaderError("forms package contains no forms")
    if not tenants:
        raise LoaderError("tenants file contains no tenants")
    for tenant_key, tenant in tenants.items():
        missing = [k for k in ("db_host", "db_name") if not tenant.get(k)]
        if missing:
            raise LoaderError(
                f"tenant '{tenant_key}' in the tenants file is missing required "
                f"key(s): {', '.join(missing)}"
            )
    if "base_dir" not in package:
        raise LoaderError("forms package is missing the 'base_dir' key")
    base_dir = (package_path.parent / package["base_dir"]).resolve()
    _package_preflight(forms, base_dir)
    print(f"package preflight OK: {len(forms)} forms validated")
    env = {**_load_env_file(package_path.parent / ".env"), **os.environ}
    db_user = args.db_user or env.get("EWMS_DB_USER")
    db_password = args.db_password or env.get("EWMS_DB_PWD")
    if not (db_user and db_password):
        raise LoaderError(
            "Set EWMS_DB_USER and EWMS_DB_PWD (environment or .env beside "
            "the forms package) or pass --db-user and --db-password"
        )
    outcomes: dict[str, list[str]] = {
        "create": [],
        "update": [],
        "tags_only": [],
        "unchanged": [],
        "aborted": [],
        "failed": [],
    }
    total = len(tenants) * len(forms)
    item = 0
    review_warnings: list[str] = []
    for tenant_key, tenant in tenants.items():
        probe = argparse.Namespace(
            connection_string=None,
            db_user=db_user,
            db_password=db_password,
            db_host=tenant["db_host"],
            db_name=tenant["db_name"],
            db_driver=args.db_driver,
        )
        try:
            probe_connection = pyodbc.connect(_connection_string(probe), timeout=15)
            try:
                known_tags = {
                    row[0].strip().casefold()
                    for row in probe_connection.cursor()
                    .execute("SELECT NAME FROM S_SSF_TAG")
                    .fetchall()
                }
            finally:
                probe_connection.close()
        except pyodbc.Error as exc:
            outcomes["failed"].append(f"{tenant_key}: connection failed: {exc}")
            print(
                f"ERROR: {tenant_key} connection failed; skipping its "
                f"{len(forms)} forms: {exc}",
                file=sys.stderr,
            )
            item += len(forms)
            continue
        tenant_warnings_before = len(WARNINGS)
        for form in forms:
            for tag in form.get("tags") or []:
                if tag.strip() and tag.strip().casefold() not in known_tags:
                    _warn(
                        f"{form['form_code']}: tag {tag!r} not found in "
                        "S_SSF_TAG; this form load will fail"
                    )
        review_warnings.extend(
            f"{tenant_key}: {message}"
            for message in WARNINGS[tenant_warnings_before:]
        )
        for form in forms:
            item += 1
            print(f"\n=== [{item}/{total}] {tenant_key} :: {form['form_code']} ===")
            warnings_before = len(WARNINGS)
            single = argparse.Namespace(
                form_code=form["form_code"],
                english_name=form["english_name"],
                french_name=form["french_name"],
                eng_pdf=str(base_dir / form["eng_pdf"]),
                fre_pdf=str(base_dir / form["fre_pdf"]),
                dlr_sysid=None,
                tags=list(form["tags"]) if form.get("tags") is not None else None,
                status=form.get("status"),
                start_date=form.get("start_date"),
                end_date=form.get("end_date"),
                apply=args.apply,
                connection_string=None,
                db_user=db_user,
                db_password=db_password,
                db_host=tenant["db_host"],
                db_name=tenant["db_name"],
                db_driver=args.db_driver,
            )
            try:
                outcome = load_single_form(single)
                outcomes[outcome].append(f"{tenant_key}/{form['form_code']}")
            except (LoaderError, OSError, pyodbc.Error) as exc:
                outcomes["failed"].append(f"{tenant_key}/{form['form_code']}: {exc}")
                print(f"ERROR: {exc}", file=sys.stderr)
            review_warnings.extend(
                f"{tenant_key}: {message}" for message in WARNINGS[warnings_before:]
            )
    labels = {
        "create": "new forms (will be created)",
        "update": "existing, PDFs or metadata differ (will be updated)",
        "tags_only": "existing, only tags differ (tags will be updated)",
        "unchanged": "existing, everything matches (no action)",
        "aborted": "update declined at prompt",
        "failed": "failed",
    }
    verb = "APPLIED" if args.apply else "DRY RUN, all changes rolled back"
    print(f"\n=== PACKAGE SUMMARY ({verb}): {total} form loads ===")
    for key, label in labels.items():
        items = outcomes[key]
        if not items:
            continue
        print(f"{label}: {len(items)}")
        for entry in items:
            print(f"  - {entry}")
    if review_warnings:
        print(f"\nwarnings for review: {len(review_warnings)}")
        for entry in review_warnings:
            print(f"  - {entry}")
    if args.apply and (
        outcomes["create"] or outcomes["update"] or outcomes["tags_only"]
    ):
        print(f"\n{CACHE_NOTE}")
    actions = ", ".join(
        f"{label}: {len(outcomes[key])}"
        for key, label in (
            ("create", "New Forms Created"),
            ("update", "Forms Updated"),
            ("tags_only", "Tags Updated (Forms)"),
        )
        if outcomes[key]
    )
    mode = "APPLIED" if args.apply else "DRY RUN (planned)"
    print(f"\n{datetime.now().astimezone().isoformat(timespec='seconds')}: "
          f"{mode}: {actions or 'No data changes'}")
    return 2 if outcomes["failed"] else 0


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser with the two load subcommands."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser(
        "load-single-form",
        help="Load one form; rolls back unless --apply is supplied",
    )
    load_parser.add_argument("--form-code", required=True)
    load_parser.add_argument("--english-name", required=True)
    load_parser.add_argument("--french-name", required=True)
    load_parser.add_argument("--eng-pdf", required=True, help="Path to the English PDF")
    load_parser.add_argument("--fre-pdf", required=True, help="Path to the French PDF")
    load_parser.add_argument(
        "--dlr-sysid",
        type=int,
        help="Dealer SYSID; auto-resolved from SSF_LIBRARY when omitted",
    )
    load_parser.add_argument(
        "--tags",
        nargs="*",
        default=None,
        help="Desired tag set (full replace on update); omit to leave tags untouched",
    )
    load_parser.add_argument(
        "--status",
        choices=sorted(STATUS_CODES),
        help="Defaults to A at creation; omit on update to leave unchanged",
    )
    load_parser.add_argument(
        "--start-date",
        help="YYYY-MM-DD; defaults to today at creation; "
        "omit on update to leave unchanged",
    )
    load_parser.add_argument(
        "--end-date",
        help="YYYY-MM-DD; omit on update to leave unchanged",
    )
    load_parser.add_argument("--apply", action="store_true")
    load_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the update confirmation prompt on --apply (for unattended runs)",
    )
    load_parser.add_argument(
        "--connection-string",
        help="pyodbc connection string; prefer EWMS_DB_CONNECTION",
    )
    load_parser.add_argument("--db-user", help="SQL login name")
    load_parser.add_argument(
        "--db-password", help="SQL password (visible in shell history)"
    )
    load_parser.add_argument(
        "--db-host",
        help="SQL Server host (required when using a SQL login)",
    )
    load_parser.add_argument(
        "--db-name", help="SQL database (required when using a SQL login)"
    )
    load_parser.add_argument(
        "--db-driver",
        default="ODBC Driver 17 for SQL Server",
        help="Installed pyodbc driver",
    )

    package_parser = subparsers.add_parser(
        "load-package-forms",
        help="Load every form in a package JSON on every tenant in a tenants "
        "JSON; rolls back unless --apply is supplied",
    )
    package_parser.add_argument(
        "--forms-package", required=True, help="Path to forms_package.json"
    )
    package_parser.add_argument(
        "--tenants", required=True, help="Path to tenants.json"
    )
    package_parser.add_argument("--apply", action="store_true")
    package_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the per-form update confirmation prompt on --apply "
        "(for unattended runs)",
    )
    package_parser.add_argument(
        "--db-user", help="SQL login; default EWMS_DB_USER (environment or .env)"
    )
    package_parser.add_argument(
        "--db-password", help="SQL password; default EWMS_DB_PWD (environment or .env)"
    )
    package_parser.add_argument(
        "--db-driver",
        default="ODBC Driver 17 for SQL Server",
        help="Installed pyodbc driver",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    """Parse the arguments, run the selected command, and return the exit code.

    Expected failures print an ERROR line and return 2.
    """
    args = build_parser().parse_args(argv)
    if args.yes:
        global CONFIRM_ALL
        CONFIRM_ALL = True
    try:
        if args.command == "load-package-forms":
            return load_package_forms(args)
        outcome = load_single_form(args)
        if args.apply and outcome in ("create", "update", "tags_only"):
            print(f"\n{CACHE_NOTE}")
        return 0
    except (LoaderError, OSError, pyodbc.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
