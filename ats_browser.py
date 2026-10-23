import resources
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import urllib.parse

import applications
import user_profile
import paths


ROOT = Path(__file__).resolve().parent
LABEL_SEMANTICS = {
    "first name": "given_name", "last name": "family_name",
    "full name": "legal_or_resume_full_name", "name": "legal_or_resume_full_name",
    "email": "contact_email", "email address": "contact_email",
    "phone": "contact_phone", "phone number": "contact_phone",
    "linkedin profile": "linkedin_profile", "linkedin url": "linkedin_profile",
}
FIELDS_SCRIPT = """() => Array.from(document.querySelectorAll('input,textarea,select')).flatMap(element => {
  const type = element.type || element.tagName.toLowerCase();
  if (['hidden','submit','button','reset'].includes(type) || element.disabled) return [];
  if (type !== 'file' && element.closest('[aria-hidden="true"]')) return [];
  if (!element.getClientRects().length && type !== 'file') return [];
  const labels = Array.from(element.labels || []).map(label => label.innerText.trim());
  const labelledBy = (element.getAttribute('aria-labelledby') || '').split(' ').filter(Boolean).map(id => document.getElementById(id)?.innerText || '').join(' ');
  const label = labels.join(' ') || element.getAttribute('aria-label') || labelledBy;
  return [{dom_id:element.id, name:element.name, label, type,
    required:element.required || element.getAttribute('aria-required') === 'true' || label.includes('*'),
    role:element.getAttribute('role'), value:type === 'file' ? '' : element.value,
    options:element.tagName === 'SELECT' ? Array.from(element.options).map(option => ({label:option.text, value:option.value})) : []}];
})"""


class Blocked(RuntimeError):
    def __init__(self, code, action):
        self.code, self.action = code, action
        super().__init__(code)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def destination(job):
    role = json.loads(job["payload"])
    parsed = urllib.parse.urlsplit(role["url"])
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise Blocked("UNSUPPORTED_ATS_DESTINATION", "Resolve an official HTTPS employer application form")
    paths = [part for part in parsed.path.split("/") if part]
    if parsed.hostname in {"job-boards.greenhouse.io", "boards.greenhouse.io"} and len(paths) == 3 and paths[1] == "jobs" and paths[2].isdigit():
        provider, requisition = "greenhouse", paths[2]
    elif parsed.hostname == "jobs.ashbyhq.com" and len(paths) in {2, 3} and re.fullmatch(r"[a-fA-F0-9-]{36}", paths[1]):
        if len(paths) == 3 and paths[2] != "application":
            raise Blocked("UNSUPPORTED_ATS_DESTINATION", "Inspect the official Ashby application URL")
        provider, requisition = "ashby", paths[1]
    else:
        raise Blocked("UNSUPPORTED_ATS_DESTINATION", "Add and test this ATS provider; do not invent a submission endpoint")
    if requisition != job["requisition"]:
        raise Blocked("REQUISITION_MISMATCH", "Reconcile employer/requisition identity before opening the application")
    path = "/".join(paths[:2] if provider == "ashby" else paths)
    if provider == "ashby":
        path += "/application"
    return provider, urllib.parse.urlunsplit(("https", parsed.netloc, "/" + path, "", ""))


def normalize_fields(raw):
    result = []
    seen = set()
    for field in raw:
        label = " ".join(field["label"].replace("*", "").split())
        if field["type"] == "file" and field["dom_id"] == "resume" and label == "Attach":
            label = "Resume/CV"
        if not label or not (field["dom_id"] or field["name"]):
            raise Blocked("FORM_FIELD_UNIDENTIFIED", "Inspect an unlabeled or unstable application field")
        identifier = field["dom_id"] or field["name"]
        if identifier in seen:
            raise Blocked("FORM_FIELD_AMBIGUOUS", "Inspect repeated field identity before filling the form")
        seen.add(identifier)
        semantic = LABEL_SEMANTICS.get(label.casefold(), "exact_question:" + fingerprint(label))
        result.append(dict(field, id=identifier, label=label, semantic=semantic,
                           answer_type="choice" if field["options"] else "text",
                           choices=[option["label"] for option in field["options"] if option["value"]]))
    return result


def form_revision(fields):
    return fingerprint([{key: value for key, value in field.items() if key != "value"} for field in fields])


class BrowserPage:
    def __init__(self, root, key, url, headless=True):
        self.root, self.key, self.url, self.headless = root, key, url, headless

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        base = paths.state(self.root) / "ats-browser"
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(base / "browsers")
        self.runtime = sync_playwright().start()
        self.context = self.runtime.chromium.launch_persistent_context(
            str(base / "profiles" / fingerprint(self.key)), headless=self.headless,
            chromium_sandbox=True, accept_downloads=False)
        try:
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(15000)
            self.page.on("dialog", lambda dialog: dialog.dismiss())
            self.page.goto(self.url, wait_until="domcontentloaded", timeout=45000)
            self.page.locator("body").wait_for()
            self.check_access()
            self.page.locator("form").first.wait_for(timeout=15000)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        try:
            self.context.close()
        finally:
            self.runtime.stop()

    def check_access(self):
        if urllib.parse.urlsplit(self.page.url).netloc != urllib.parse.urlsplit(self.url).netloc:
            raise Blocked("ATS_LOGIN_OR_REDIRECT", "Complete the required sign-in in the dedicated browser; do not bypass it")
        if self.page.locator('input[type="password"]').count():
            raise Blocked("ATS_LOGIN_REQUIRED", "Complete employer login in the dedicated browser")
        text = self.page.locator("body").inner_text()
        if re.search(r"verify (?:that )?you (?:are|are a) human|complete the captcha|security verification|access denied", text, re.I):
            raise Blocked("ATS_ACCESS_CHALLENGE", "User must handle the site's access challenge; no automated solving or bypass")
        challenges = self.page.locator('iframe[title*="challenge" i],iframe[title*="captcha" i]')
        if any(challenges.nth(index).is_visible() for index in range(challenges.count())):
            raise Blocked("ATS_CAPTCHA_REQUIRES_USER", "User must handle CAPTCHA in the dedicated browser")

    def fields(self):
        self.check_access()
        fields = normalize_fields(self.page.evaluate(FIELDS_SCRIPT))
        for field in fields:
            if field["role"] == "combobox" and field["required"]:
                control = self.control(field)
                control.click()
                self.page.get_by_role("option").first.wait_for()
                options = self.page.get_by_role("option").all_text_contents()
                control.press("Escape")
                if not options:
                    raise Blocked("FORM_OPTIONS_UNAVAILABLE", "Inspect required dropdown options before supplying an answer")
                field.update(options=[{"label": option.strip(), "value": option.strip()} for option in options], choices=[option.strip() for option in options], answer_type="choice")
        return fields

    def control(self, field):
        if field["dom_id"]:
            selector = '[id=' + json.dumps(field["dom_id"]) + ']'
        else:
            selector = '[name=' + json.dumps(field["name"]) + ']'
        locator = self.page.locator(selector)
        if locator.count() != 1:
            raise Blocked("FORM_FIELD_AMBIGUOUS", "Reinspect the current application form")
        return locator

    def fill(self, fields, answers, resume):
        for field in fields:
            if field["type"] == "file":
                if re.fullmatch(r"resume(?:/cv)?|resume/cv|cv", field["label"], re.I):
                    self.control(field).set_input_files(str(resume))
                elif field["required"]:
                    raise Blocked("ADDITIONAL_DOCUMENT_REQUIRED", "Supply the exact required document; do not substitute the resume")
                continue
            if field["id"] not in answers:
                if field["required"]:
                    raise Blocked("UNKNOWN_REQUIRED_ANSWERS", "Resolve the newly required answer before submitting")
                continue
            if re.search(r"agree|consent|certify|signature|terms|privacy policy", field["label"], re.I):
                raise Blocked("ATS_LEGAL_ACKNOWLEDGMENT_REQUIRED", "User must review the specific acknowledgment at action time")
            value = answers[field["id"]]
            control = self.control(field)
            if field["role"] == "combobox":
                if str(value) not in field["choices"]:
                    raise Blocked("FORM_CHOICE_MISMATCH", "Verify the exact choice rather than mapping an ambiguous answer")
                control.click()
                option = self.page.get_by_role("option", name=str(value), exact=True)
                if option.count() != 1:
                    raise Blocked("FORM_CHOICE_MISMATCH", "The chosen option no longer resolves uniquely")
                option.click()
            elif field["options"]:
                if str(value) not in field["choices"]:
                    raise Blocked("FORM_CHOICE_MISMATCH", "Verify the exact choice rather than mapping an ambiguous answer")
                control.select_option(label=str(value))
            elif field["type"] in {"text", "email", "tel", "url", "textarea", "number"} and field["role"] != "combobox":
                control.fill(str(value))
            else:
                raise Blocked("UNSUPPORTED_FORM_CONTROL", "Add and fixture-test this field type before submitting")

    def submit(self):
        self.check_access()
        text = self.page.locator("body").inner_text()
        if re.search(r"by (?:clicking|submitting).{0,180}(?:agree|consent|certify)", text, re.I | re.S):
            raise Blocked("ATS_LEGAL_ACKNOWLEDGMENT_REQUIRED", "User must review the submission agreement at action time")
        button = self.page.get_by_role("button", name=re.compile(r"^(?:Submit application|Submit my application|Submit Application|Apply)$"))
        if button.count() != 1 or not button.is_enabled():
            raise Blocked("SUBMIT_CONTROL_UNAVAILABLE", "Inspect the current official submit control")
        button.click()

    def observation(self):
        return {"url": self.page.url, "title": self.page.title(), "text": self.page.locator("body").inner_text()[:120000]}


class Adapter:
    def __init__(self, root=None, page_factory=BrowserPage):
        root = paths.data(root)
        self.root, self.page_factory = root, page_factory
        self.config = resources.read_json("config.json")
        self.policy = user_profile.load()
        self.directory = paths.state(root) / "ats-browser"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def save(self, key, suffix, payload):
        path = self.directory / (fingerprint(key) + "-" + suffix + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2))
        temporary.chmod(0o600)
        temporary.replace(path)

    def review(self, job):
        path = paths.state(self.root) / "ats-evidence" / (fingerprint(job["job_key"]) + ".json")
        if not path.is_file():
            raise Blocked("SOURCED_ELIGIBILITY_REVIEW_REQUIRED", "Verify dated role/company/compensation/authorization and application/referral history; record evidence for this exact requisition")
        review = json.loads(path.read_text())
        now = applications.stamp()
        if not applications.fresh(review, now) or review.get("employer") != job["employer"] or review.get("requisition") != job["requisition"]:
            raise Blocked("STALE_OR_MISMATCHED_ELIGIBILITY_REVIEW", "Refresh sourced evidence for this exact employer/requisition")
        return review

    def inspect(self, job):
        provider, url = destination(job)
        with self.page_factory(self.root, job["job_key"], url) as page:
            fields = page.fields()
            if not fields:
                raise Blocked("APPLICATION_FORM_UNAVAILABLE", "Inspect the official application form; no required-answer assumptions")
            revision = form_revision(fields)
            observation = dict(page.observation(), provider=provider, observed_at=applications.stamp(), fields=fields, form_revision=revision)
        self.save(job["job_key"], "inspect", observation)
        review = self.review(job)
        review.update(questions=[{key: field[key] for key in ["id", "label", "semantic", "required", "answer_type", "choices"]}
                                 for field in fields if field["type"] != "file"],
                      all_required_questions_enumerated=True, form_revision=revision,
                      browser_observed_at=observation["observed_at"], browser_source=url)
        return review

    def receipt(self, job):
        path = paths.state(self.root) / "ats-receipts" / (fingerprint(job["job_key"]) + ".json")
        if path.is_file():
            receipt = json.loads(path.read_text())
            if applications.verified_receipt(receipt, job, applications.stamp()) and receipt.get("evidence_kind") in {"employer_confirmation_email", "authenticated_ats_application_record"}:
                return receipt
        return {"status": "UNKNOWN", "source": "independent employer receipt not yet reconciled", "job_key": job["job_key"]}

    def submit(self, payload):
        job = payload["job"]
        if not self.config.get("execution_ownership_verified") or not self.policy.get("preapproved_applications"):
            raise Blocked("EXECUTION_OWNERSHIP_UNVERIFIED", "Reconcile this project's cloud/Mac ownership before submitting")
        with contextlib.closing(sqlite3.connect(paths.database(self.root))) as database:
            database.row_factory = sqlite3.Row
            claim = database.execute("SELECT * FROM application_attempts WHERE job_key=? AND state='SUBMITTING' ORDER BY started_at DESC LIMIT 1", (job["job_key"],)).fetchone()
            current = database.execute("SELECT state FROM application_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
            if not claim or not current or current[0] != "SUBMITTING" or payload.get("idempotency_key") != job["job_key"]:
                raise Blocked("DURABLE_SUBMISSION_CLAIM_REQUIRED", "Only the existing application worker may execute a claimed submission")
            evidence = json.loads(claim["evidence"])
            # An answer rendered to an attachment legitimately leaves `answers` (a file control
            # cannot be typed into) while the evidence, recorded before that split, still holds
            # it. Restore each attachment's field before comparing, exactly as desktop_adapter
            # does; an attachment for a field the evidence never held still mismatches.
            submitted = dict(payload["answers"])
            for field in (payload.get("attachments") or {}):
                if field in evidence["answers"] and field not in submitted:
                    submitted[field] = evidence["answers"][field]
            if evidence["answers"] != submitted or evidence["review"].get("form_revision") != payload.get("form_revision"):
                raise Blocked("SUBMISSION_PAYLOAD_MISMATCH", "Use the exact verified answers and form revision saved by the worker")
            now = applications.stamp()
            # The history freshness window is gone here too (owner, 2026-09-22). This is the
            # same guard as the desktop gate's number 8, duplicated on the non-desktop path;
            # leaving one copy behind is how a fix at one layer keeps getting defeated at the
            # next. The eligibility re-run stays, exactly as it does there.
            if applications.assess(evidence["review"], job, self.policy, now):
                raise Blocked("ELIGIBILITY_EVIDENCE_EXPIRED", "Refresh eligibility and duplicate/referral checks before submitting")
            resume = self.root / self.policy["resume"]["path"]
            if Path(payload["resume"]).resolve() != resume.resolve() or hashlib.sha256(resume.read_bytes()).hexdigest() != self.policy["resume"]["sha256"]:
                raise Blocked("DESIGNATED_RESUME_MISMATCH", "Use only the designated unchanged resume")
            database.execute("CREATE TABLE IF NOT EXISTS browser_submission_claims (job_key TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, state TEXT NOT NULL, observed_at TEXT NOT NULL)")
            with database:
                inserted = database.execute("INSERT OR IGNORE INTO browser_submission_claims VALUES (?,?,'INTENT',?)", (job["job_key"], claim["attempt_id"], now))
            if inserted.rowcount != 1:
                return {"status": "UNKNOWN", "reason": "BROWSER_CLAIM_ALREADY_CONSUMED"}
            provider, url = destination(job)
            try:
                with self.page_factory(self.root, job["job_key"], url) as page:
                    fields = page.fields()
                    if form_revision(fields) != payload["form_revision"]:
                        raise Blocked("APPLICATION_FORM_CHANGED", "Reinspect the changed form and resolve every new required answer")
                    page.fill(fields, payload["answers"], resume)
                    if form_revision(page.fields()) != payload["form_revision"]:
                        raise Blocked("APPLICATION_FORM_CHANGED", "Resolve conditional questions revealed after filling; do not submit")
                    with database:
                        database.execute("UPDATE browser_submission_claims SET state='SUBMIT_POSSIBLE' WHERE job_key=?", (job["job_key"],))
                    page.submit()
                    self.save(job["job_key"], "post-submit-observation", dict(page.observation(), provider=provider, observed_at=applications.stamp(), independent_receipt=False))
                return {"acknowledged": True, "verified": False, "requires_independent_receipt": True}
            except Blocked as error:
                self.save(job["job_key"], "submit-blocker", {"code": error.code, "next_action": error.action, "observed_at": applications.stamp()})
                return {"status": "UNKNOWN", "blocker": error.code, "next_action": error.action}

    def call(self, operation, payload):
        try:
            if operation == "inspect":
                return self.inspect(payload["job"])
            if operation == "submit":
                return self.submit(payload)
            if operation in {"verify", "reconcile"}:
                return self.receipt(payload["job"])
            raise Blocked("UNSUPPORTED_ADAPTER_OPERATION", "Use inspect, submit, verify or reconcile only")
        except Blocked as error:
            return {"blocked": {"code": error.code, "next_action": error.action}}


def main():
    os.umask(0o077)
    try:
        result = Adapter().call(sys.argv[1], json.load(sys.stdin))
        # Machine contract (E2.T6): the adapter's JSON result line, read by its caller; stays print.
        print(json.dumps(result))
    except Exception as error:
        # Machine contract (E2.T6): the adapter's JSON result line, read by its caller; stays print.
        print(json.dumps({"adapter_error": type(error).__name__, "outcome": "UNKNOWN"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
