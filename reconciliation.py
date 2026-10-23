import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile

import applications
import paths


def workbook_rows(path):
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ["".join(item.itertext()) for item in ET.fromstring(archive.read("xl/sharedStrings.xml"))]
        sheets = ET.fromstring(archive.read("xl/workbook.xml")).find("main:sheets", namespace)
        relations = {item.attrib["Id"]: item.attrib["Target"] for item in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        for sheet in sheets:
            target = relations[sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]]
            target = target.lstrip("/") if target.startswith("/") else "xl/" + target
            for row in ET.fromstring(archive.read(target)).findall("main:sheetData/main:row", namespace):
                cells = {}
                for cell in row:
                    value = cell.find("main:v", namespace)
                    text = value.text if value is not None else "".join(cell.findtext("main:is/main:t", "", namespace))
                    if cell.attrib.get("t") == "s" and text:
                        text = strings[int(text)]
                    if text:
                        cells[cell.attrib["r"]] = text
                if cells:
                    yield {"sheet": sheet.attrib["name"], "row": int(row.attrib["r"]), "cells": cells}


# The imported personal-history folder (tracker workbook, outreach history, requirements), under
# the project root. Read only when present; a new install has none.
HANDOFF_DIR = paths.HANDOFF   # under the data folder


def merge(database, root, profile_key, name_key):
    folder = root / HANDOFF_DIR
    if not folder.exists():
        return {}
    applications.initialize(database)
    database.executescript("""
        CREATE TABLE IF NOT EXISTS reconciled_sources (
            source_key TEXT PRIMARY KEY, source TEXT NOT NULL, payload TEXT NOT NULL,
            imported_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS company_contact_baselines (
            company TEXT PRIMARY KEY, minimum_count INTEGER NOT NULL);
    """)
    history = json.loads((folder / "outreach-history.json").read_text())
    now = applications.stamp()
    added = 0
    with database:
        for contact in history["contacts"]:
            identity = "name:" + name_key(contact["recipient"])
            profile = contact.get("profile")
            existing = database.execute("SELECT * FROM history WHERE identity=?", (identity,)).fetchone()
            if not existing and profile:
                existing = next((row for row in database.execute("SELECT * FROM history WHERE profile IS NOT NULL") if profile_key(row["profile"]) == profile_key(profile)), None)
            if not existing:
                database.execute("INSERT INTO history VALUES (?,?,?,?,?)", (identity, contact["recipient"], contact["company"], profile, json.dumps(contact)))
                added += 1
            for suppressed in [identity] + ([profile_key(profile)] if profile else []):
                database.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?)", (suppressed, "Handoff historical outreach; never repeat initial contact"))
            raw = json.dumps(contact, sort_keys=True)
            database.execute("INSERT OR IGNORE INTO reconciled_sources VALUES (?,?,?,?)", (hashlib.sha256(raw.encode()).hexdigest(), "outreach-history.json:contacts", raw, now))
        for interview in history["interviews"]:
            database.execute("INSERT OR IGNORE INTO application_history VALUES (?, '*', 'INTERVIEW', ?)", (applications.normalized(interview["company"]), json.dumps(interview)))
        for row in workbook_rows(folder / "originals/linkedin_interview_pipeline.xlsx"):
            raw = json.dumps(row, sort_keys=True)
            database.execute("INSERT OR IGNORE INTO reconciled_sources VALUES (?,?,?,?)", (hashlib.sha256(raw.encode()).hexdigest(), "originals/linkedin_interview_pipeline.xlsx", raw, now))
            if row["sheet"] != "Tracker" or row["row"] < 8:
                continue
            cells = {re.sub(r"[0-9]", "", key): value for key, value in row["cells"].items()}
            company = cells.get("A")
            if not company:
                continue
            count = int(cells.get("M", 0))
            database.execute("INSERT INTO company_contact_baselines VALUES (?,?) ON CONFLICT(company) DO UPDATE SET minimum_count=MAX(minimum_count,excluded.minimum_count)", (name_key(company), count))
            links = re.findall(r"https://[^\s]+", cells.get("L", ""))
            for link in links:
                role = {"company": company, "role": cells.get("B", "Unknown"), "url": link,
                        "rationale": "Historical tracker role; current eligibility and application/referral history unverified", "source": "handoff spreadsheet Tracker row " + str(row["row"])}
                database.execute("INSERT OR IGNORE INTO opportunities VALUES (?,?,?)", (link, json.dumps(role), now))
        for opportunity in database.execute("SELECT payload FROM opportunities"):
            applications.ingest(database, json.loads(opportunity[0]), now)
    return {"at": now, "added_contacts": added, "existing_history_overwritten": False,
            "preserved_contacts": database.execute("SELECT COUNT(*) FROM history").fetchone()[0],
            "historical_rows": database.execute("SELECT COUNT(*) FROM reconciled_sources").fetchone()[0],
            "interview_holds": database.execute("SELECT COUNT(*) FROM application_history WHERE kind='INTERVIEW'").fetchone()[0],
            "conflicts": ["Spreadsheet invitation dates 09/17 disagree with later reconciled 09/16 evidence; existing local history retained",
                          "Spreadsheet details-pending rows are superseded by handoff interviews and existing sent campaigns",
                          "Spreadsheet follow-up template is stale; resolved 3-business-day then 5-business-day policy prevails; automatic follow-ups remain disabled",
                          "Original YAML is historical, not executable configuration"],
            "cloud_migration": "Sites management is available without include_mcp_connection. HTTPS transport does not require Site MCP. Verify the transport deployment and duplicate cloud schedule retirement separately."}


def apply_baselines(database, pipeline, name_key):
    if not database.execute("SELECT 1 FROM sqlite_master WHERE name='company_contact_baselines'").fetchone():
        return
    for row in database.execute("SELECT * FROM company_contact_baselines"):
        existing = next((company for company in pipeline["companies"] if name_key(company["company"]) == row["company"]), None)
        if existing:
            existing["conservative_known_contact_count"] = max(existing["conservative_known_contact_count"], row["minimum_count"])
        else:
            pipeline["companies"].append({"company": row["company"], "conservative_known_contact_count": row["minimum_count"]})
