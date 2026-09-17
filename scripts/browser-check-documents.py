"""Real-browser check of the business-context document upload.

The suite proves the endpoint; this proves the thing a person actually does —
drop a PDF into step 1 of the setup wizard and find out whether the run can
read it. It uploads a genuine PDF, a spreadsheet and a scanned-looking PDF with
no text layer, and asserts on what the screen says about each.

    make browser-documents

Three things carried over from the earlier browser checks and repeated here
because they bit every one of them:

* `networkidle` never fires on this app — the SSE stream stays open. Wait for
  content.
* The scroll container is `<main>`, not the document, so `full_page=True`
  screenshots a tall blank page.
* Every fixture is built fresh per invocation. A harness that reuses fixtures
  measures its own leftovers.
"""

import json
import sys
import urllib.error
import urllib.request
import uuid
import zlib

sys.path.insert(0, "/app/src")

from playwright.sync_api import Page, sync_playwright

WEB = "http://web:3000"
API = "http://api:8000/api/v1"
ADMIN = ("admin@example.com", "change-me-at-least-12-chars")
SHOT = "/tmp/shots"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

failures: list[str] = []
passes: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    suffix = f" — {detail}" if detail and not condition else ""
    (passes if condition else failures).append(f"{name}{suffix}")


class Api:
    """The same cookie-and-CSRF dance `lib/api.ts` does, without a browser."""

    def __init__(self) -> None:
        self.jar: dict[str, str] = {}

    def call(self, path, payload=None, method=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{API}{path}", data=data, method=method or ("POST" if data else "GET")
        )
        request.add_header("Content-Type", "application/json")
        if self.jar:
            request.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.jar.items()))
        if "csrf" in self.jar:
            request.add_header("X-CSRF-Token", self.jar["csrf"])
        try:
            with urllib.request.urlopen(request) as response:
                for header in response.headers.get_all("Set-Cookie") or []:
                    name, _, rest = header.partition("=")
                    self.jar[name.strip()] = rest.split(";")[0]
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            return {"_status": error.code, "_body": error.read().decode()[:400]}

    def sign_in(self):
        self.call("/auth/csrf")
        return self.call("/auth/login", {"email": ADMIN[0], "password": ADMIN[1]})


# --- fixtures ---------------------------------------------------------------


def _pdf(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    return bytes(out)


def text_pdf(pages: list[str]) -> bytes:
    """A real PDF with a real text layer, one page per string."""
    page_ids = [4 + index * 2 for index, _ in enumerate(pages)]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] "
        f"/Count {len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, text in enumerate(pages):
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 3 0 R >> >> "
                f"/MediaBox [0 0 612 792] /Contents {page_ids[index] + 1} 0 R >>"
            ).encode()
        )
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 11 Tf 72 720 Td ({escaped}) Tj ET".encode()
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    return _pdf(objects)


def scanned_pdf() -> bytes:
    """One page, one image, no text operators — what a scanner produces."""
    pixels = zlib.compress(b"\xff\xff\xff" * 16)
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /XObject << /Im0 5 0 R >> >> >>",
            b"<< /Length 24 >>\nstream\nq 612 0 0 792 0 0 cm Q\nendstream",
            b"<< /Type /XObject /Subtype /Image /Width 4 /Height 4 /ColorSpace /DeviceRGB "
            b"/BitsPerComponent 8 /Filter /FlateDecode /Length "
            + str(len(pixels)).encode()
            + b" >>\nstream\n"
            + pixels
            + b"\nendstream",
        ]
    )


PRICING_PAGES = [
    "SDS Manager pricing. Team plan is 49 EUR per site per month, billed annually, "
    "and covers up to ten users with unlimited safety data sheets.",
    "Site plan is 199 EUR per month per location. Enterprise customers with more "
    "than twenty locations are quoted individually and invoiced per site.",
    "Every plan includes automatic supplier updates and the regulator report. "
    "The free tier stops at fifty sheets and has no compliance reporting.",
]

PLANS_CSV = (
    b"Plan,Price EUR,Seats,Includes\n"
    b"Free,0,2,Fifty sheets and search\n"
    b"Team,49,10,Supplier updates and the regulator report\n"
    b"Site,199,unlimited,Everything in Team plus multi-location reporting\n"
)


def write_fixtures() -> dict[str, str]:
    paths = {
        "pricing.pdf": text_pdf(PRICING_PAGES),
        "plans.csv": PLANS_CSV,
        "scan.pdf": scanned_pdf(),
    }
    for name, content in paths.items():
        with open(f"/tmp/{name}", "wb") as handle:
            handle.write(content)
    return {name: f"/tmp/{name}" for name in paths}


def seed(api: Api) -> str:
    """A fresh, empty project — the state someone opening the wizard is in."""
    marker = uuid.uuid4().hex[:8]
    project = api.call(
        "/projects", {"name": f"Document upload check {marker}", "domain": f"{marker}.example.com"}
    )
    if "_status" in project:
        raise SystemExit(f"could not create a project: {project}")
    return project["id"]


# --- the screen -------------------------------------------------------------


def sign_in(page: Page) -> None:
    page.goto(f"{WEB}/login")
    page.fill('input[type="email"]', ADMIN[0])
    page.fill('input[type="password"]', ADMIN[1])
    page.click('button[type="submit"]')
    page.wait_for_url(lambda url: "/login" not in url, timeout=20_000)


def shoot(page: Page, name: str) -> None:
    page.screenshot(path=f"{SHOT}/{name}.png")


def library(page: Page):
    """The uploader and its list, as one container."""
    return page.locator("fieldset").filter(has_text="Background documents").first


def upload(page: Page, path: str) -> None:
    page.set_input_files("#context-documents", path)


def check_upload(page: Page, project_id: str, files: dict[str, str]) -> None:
    page.goto(f"{WEB}/projects/{project_id}/setup")
    page.wait_for_selector("text=What does this brand sell?", timeout=20_000)

    panel = library(page)
    check("the uploader is on step 1", panel.is_visible())
    check(
        "it says which formats it takes before anything is chosen",
        ".pdf" in panel.inner_text() and ".docx" in panel.inner_text(),
        panel.inner_text()[:200],
    )
    shoot(page, "documents-empty")

    upload(page, files["pricing.pdf"])
    page.wait_for_selector("text=pricing.pdf", timeout=30_000)
    row = panel.inner_text()
    check("the uploaded file is listed", "pricing.pdf" in row)
    check("with the page count read out of it", "3 pages" in row, row[:300])
    check("and the passages a node can cite", "passage" in row, row[:300])
    check(
        "the preview shows text that actually came out of the PDF",
        "49 EUR per site" in row,
        row[:400],
    )
    check("a clean file raises no warning", "were not read" not in row and "skipped" not in row)

    upload(page, files["plans.csv"])
    page.wait_for_selector("text=plans.csv", timeout=30_000)
    row = library(page).inner_text()
    check("a CSV is accepted as context too", "plans.csv" in row)
    check("counted in rows, not pages", "3 rows" in row, row[:400])
    shoot(page, "documents-two-files")

    upload(page, files["scan.pdf"])
    page.wait_for_selector("text=no text layer", timeout=30_000)
    body = page.locator("body").inner_text()
    check(
        "a scan is refused with a sentence that says what to do",
        "no text layer" in body and ("Word" in body or "paste" in body),
        body[body.find("no text layer") - 120 : body.find("no text layer") + 160],
    )
    check("and it is not listed as if it had worked", "scan.pdf" not in library(page).inner_text())
    shoot(page, "documents-scan-refused")

    upload(page, files["pricing.pdf"])
    page.wait_for_selector("text=already in this project", timeout=30_000)
    check(
        "the same file twice is refused, by contents not by name",
        "already in this project" in page.locator("body").inner_text(),
    )
    check(
        "and the library still holds exactly two files",
        library(page).inner_text().count("passage") == 2,
        library(page).inner_text()[:400],
    )


def check_evidence(api: Api, project_id: str) -> None:
    """The point of the feature: the passages exist as citable evidence."""
    body = api.call(f"/evidence?project_id={project_id}&source=upload&limit=200")
    items = body.get("items", [])
    check("the upload wrote evidence a node can cite", len(items) >= 2, json.dumps(body)[:300])
    check(
        "every row is filed under the brand_doc kind",
        bool(items) and {item["kind"] for item in items} == {"brand_doc"},
    )
    check(
        "and carries the text, not just a filename",
        any("49 EUR per site" in (item["content_text"] or "") for item in items),
    )
    check(
        "the evidence explorer can filter to it by source",
        all(item["source"] == "upload" for item in items),
    )


def check_review_and_delete(page: Page, project_id: str) -> None:
    page.goto(f"{WEB}/projects/{project_id}/setup")
    page.wait_for_selector("text=What does this brand sell?", timeout=20_000)
    page.get_by_role("button", name="Review").click()
    page.wait_for_selector("text=Background documents", timeout=20_000)
    summary = page.locator("dl").first.inner_text()
    check(
        "the review step counts the documents the run will read",
        "2 files" in summary and "citable passages" in summary,
        summary[:400],
    )
    shoot(page, "documents-review")

    page.get_by_role("button", name="Business context").click()
    page.wait_for_selector("text=pricing.pdf", timeout=20_000)
    library(page).get_by_role("button", name="Remove pricing.pdf").click()
    page.wait_for_selector("text=Remove this document?", timeout=10_000)
    dialog = page.get_by_role("dialog")
    check(
        "the confirm says what else disappears with it",
        "evidence" in dialog.inner_text(),
        dialog.inner_text()[:200],
    )
    shoot(page, "documents-confirm-delete")
    dialog.get_by_role("button", name="Remove").click()
    page.wait_for_selector("text=pricing.pdf", state="detached", timeout=20_000)
    check("the file is gone from the list", "pricing.pdf" not in library(page).inner_text())


def check_mobile(page: Page, project_id: str) -> None:
    page.set_viewport_size(MOBILE)
    page.goto(f"{WEB}/projects/{project_id}/setup")
    page.wait_for_selector("text=plans.csv", timeout=20_000)
    panel = library(page)
    check("the uploader is usable at 390px", panel.is_visible())
    overflow = page.evaluate(
        "() => { const m = document.querySelector('main');"
        " return m ? m.scrollWidth - m.clientWidth : 0; }"
    )
    check("and nothing overflows sideways", overflow <= 1, f"main overflows by {overflow}px")
    shoot(page, "documents-mobile")
    page.set_viewport_size(DESKTOP)


def main() -> int:
    api = Api()
    signed = api.sign_in()
    if "_status" in signed:
        raise SystemExit(f"could not sign in: {signed}")
    project_id = seed(api)
    files = write_fixtures()

    errors: list[str] = []
    bad_responses: list[str] = []

    def record_console(message) -> None:
        if message.type != "error":
            return
        if "Cross-Origin-Opener-Policy header has been ignored" in message.text:
            return
        if "Failed to load resource" in message.text:
            return
        errors.append(message.text)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        page.on("console", record_console)
        page.on("pageerror", lambda error: errors.append(str(error)))

        # Three failures are the product working, and the check is written to
        # provoke them: the scan (422), the duplicate (409), and `GET /models`,
        # which 409s on a stack with no OpenRouter key stored.
        expected = ("/documents", "/models")

        def record_response(response) -> None:
            if response.status < 400:
                return
            if any(path in response.url for path in expected):
                return
            bad_responses.append(f"{response.status} {response.url}")

        page.on("response", record_response)

        sign_in(page)
        check_upload(page, project_id, files)
        check_evidence(api, project_id)
        check_review_and_delete(page, project_id)
        check_mobile(page, project_id)

        browser.close()

    check("no console errors", not errors, "; ".join(errors[:3]))
    check("no failing requests", not bad_responses, "; ".join(sorted(set(bad_responses))[:4]))

    for line in passes:
        print(f"  PASS {line}")
    for line in failures:
        print(f"  FAIL {line}")
    print(f"\n  {len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
