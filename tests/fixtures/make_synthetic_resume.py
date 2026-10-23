"""Write the synthetic five-page test resume for a fictional candidate, Alex Example.

Every name, employer, school and contact detail here is invented. The layout mirrors what
the code expects of a designated resume (reply_resume.context and answering_agent's dated
sections): five pages, an uppercase name, PROFESSIONAL SUMMARY and CORE SKILLS, employer
lines followed by "Title | Month Year - Month Year" lines, then undated EDUCATION and
CERTIFICATIONS. The three designated_claims sentences in tests/fixtures/owner-seed.json appear verbatim on
pages 2, 3 and 4, as seed_designated_claims checks.

The PDF is hand-written with the standard library (Courier, WinAnsiEncoding, no dates), so
the output is byte-for-byte reproducible:

    python3.13 tests/fixtures/make_synthetic_resume.py
"""

from pathlib import Path

OUTPUT = Path(__file__).resolve().parent / "resume" / "alex-example-resume.pdf"

PAGES = [
    """\
                                ALEX EXAMPLE
    Staff Platform Engineer | Data Pipelines & Developer Tooling
        Springfield, Illinois, USA | alex.example@example.com | +1-555-010-0100
         LinkedIn: https://www.linkedin.com/in/alex-example/



PROFESSIONAL SUMMARY
Platform engineer with 9+ years of experience in freight and warehouse software at
Tailspin Freight and Fabrikam Logistics. Enjoys turning slow, fragile batch jobs into
observable streaming services, and writing the runbooks that let a small team sleep
through the night.

CORE SKILLS
Languages: Go, Java, TypeScript, SQL
Infrastructure: Kubernetes, Terraform, Helm, Linux
Data: Postgres, ClickHouse, RabbitMQ
Practices: Incident Review • Contract Testing • Technical Writing

Fabrikam Logistics Platform

Staff Platform Engineer | March 2021 – August 2025

Warehouse Data Platform

   •   Rebuilt the nightly inventory reconciliation job as a streaming service in Go,
       so a pallet scanned on the dock showed up on store dashboards within seconds.
   •   Introduced a schema registry and contract tests for forty producer teams, so a
       breaking field change failed in code review instead of at three in the morning.
""",
    """\
   •   Designed a sharded job scheduler with lease-based ownership that let route planning
       workers fail over between data centers without dropping queued work.
   •   Wrote the on-call handbook and a weekly failure review that halved repeat pages
       over two quarters.

Developer Tooling

   •   Built an internal command-line tool that creates a new service with logging,
       metrics and deployment manifests already wired in.
   •   Moved build caching to a shared remote store, bringing typical pull request
       checks from twenty minutes to under six.
""",
    """\
LANTERNWORKS
Founding Engineer and Technical Advisor | May 2023 – Present

Product Engineering
   •   Started a volunteer scheduling app for community gardens with two friends.
   •   Wrote a TypeScript and Postgres backend that matches volunteers to garden shifts,
       sends reminders, and keeps a history of every plot

Operations

   •   Set up error tracking, nightly backups and a simple status page on a single
       low-cost virtual machine.
   •   Interviewed garden coordinators every month and turned their requests into a
       short public roadmap.
""",
    """\
TAILSPIN
Backend Engineer II | September 2016 – February 2021

Freight Booking Services
   •   Maintained the Java booking service that priced and confirmed truckload orders
       for regional carriers.
   •   Wrote Kubernetes operators that restarted stuck pricing workers and posted each
       restart to the team channel.

Database Migration
   •   Moved the booking database from a single primary to a replicated cluster in
       stages, keeping carriers able to book throughout each cutover
""",
    """\
Testing & Release
   •   Added contract tests and staged rollouts to the release process for the booking
       and invoicing services



EDUCATION
Bachelor of Science in Information Systems
Example State University



CERTIFICATIONS
HashiCorp Certified: Terraform Associate
Linux Foundation Certified System Administrator
""",
]

FONT_SIZE = 9
CHAR_WIDTH = FONT_SIZE * 0.6          # Courier advance width
LEADING = 12
LEFT, TOP = 36, 756                   # US Letter, 612 x 792


def escape(text):
    raw = text.encode("cp1252")       # WinAnsiEncoding: bullet and en dash included
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def content(page):
    out = [b"BT", b"/F1 %d Tf" % FONT_SIZE]
    for number, line in enumerate(page.splitlines()):
        stripped = line.lstrip(" ")
        if not stripped:
            continue
        x = LEFT + CHAR_WIDTH * (len(line) - len(stripped))
        y = TOP - LEADING * number
        out.append(b"1 0 0 1 %.1f %d Tm (" % (x, y) + escape(stripped) + b") Tj")
    out.append(b"ET")
    return b"\n".join(out) + b"\n"


def build():
    objects = []
    kids = " ".join("%d 0 R" % (4 + 2 * index) for index in range(len(PAGES)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [" + kids.encode() + b"] /Count %d >>" % len(PAGES))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
    for index, page in enumerate(PAGES):
        stream = content(page)
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (5 + 2 * index))
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream")
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(output)
    output += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        output += b"%010d 00000 n \n" % offset
    output += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(output)


if __name__ == "__main__":
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(build())
    print(OUTPUT)
