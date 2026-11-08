"""`inspect` mode: capture the real EMS DOM so selectors can be tuned to it.

You drive the browser; each time you press Enter in the terminal the current page is
saved (HTML, accessibility tree, screenshot, and a summary of what the script's
current selectors find on it). Nothing is clicked by the script.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import List

from playwright.sync_api import Error as PWError, Page

from .config import Config, normalise
from .portal import Portal
from .session import BrowserSession

log = logging.getLogger("ems_swap")

# Hidden form values that are session-ish (anti-forgery tokens, view state). Scrubbed
# from saved HTML so the capture is safer to share.
_TAG = re.compile(r"<(?:input|meta)\b[^>]*>", re.IGNORECASE)
_SENSITIVE_NAME = re.compile(
    r"""\bname\s*=\s*["'][^"']*(?:token|csrf|xsrf|viewstate|eventvalidation|nonce|session|saml|wresult|code)""",
    re.IGNORECASE)
_VALUE_ATTR = re.compile(r"""(\b(?:value|content)\s*=\s*)(["'])[^"']*\2""", re.IGNORECASE)


def scrub_html(html: str) -> str:
    def fix(m: "re.Match[str]") -> str:
        tag = m.group(0)
        if not _SENSITIVE_NAME.search(tag):
            return tag
        return _VALUE_ATTR.sub(lambda v: f"{v.group(1)}{v.group(2)}[scrubbed]{v.group(2)}", tag)
    return _TAG.sub(fix, html)


_JS_DESCRIBE = r"""
(root) => {
  const out = [];
  const cssPath = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 6) {
      let p = el.tagName.toLowerCase();
      if (el.id) { p += '#' + el.id; parts.unshift(p); break; }
      const cls = [...el.classList].slice(0, 3).join('.');
      if (cls) p += '.' + cls;
      parts.unshift(p);
      el = el.parentElement;
    }
    return parts.join(' > ');
  };
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
  let n;
  while ((n = walker.nextNode())) {
    const own = [...n.childNodes].filter(c => c.nodeType === 3).map(c => c.textContent).join(' ');
    if (/global\s+experience|helsinki|hong\s*kong|vietnam/i.test(own)) {
      out.push(cssPath(n) + '  ::  ' + own.replace(/\s+/g, ' ').trim().slice(0, 160));
    }
  }
  return out.slice(0, 200);
}
"""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "page"


def summarise(page: Page, portal: Portal, cfg: Config) -> str:
    out: List[str] = [f"URL:   {page.url}", f"Title: {page.title()}", ""]

    dlg = portal.visible_dialog()
    out.append("== Visible dialog ==")
    out.append(portal.text_of(dlg)[:1000] if dlg else "(none)")
    if dlg:
        for el in dlg.get_by_role("button").all():
            if el.is_visible():
                out.append(f"   dialog button: {portal.label_of(el)!r}")
    out.append("")
    out.append("== Messages / alerts ==")
    out.append(portal.messages() or "(none)")
    out.append("")

    out.append("== Course rows found with the CURRENT selectors ==")
    body = page.locator("body")
    for course in cfg.all_courses:
        rows = portal.rows(course, body)
        out.append(f"-- {course.name}  (keywords {course.keywords}) -> {len(rows)} row(s)")
        for i, row in enumerate(rows[:5]):
            text = portal.text_of(row)
            tag = row.evaluate("e => e.tagName.toLowerCase() + (e.className ? '.' + [...e.classList].join('.') : '')")
            out.append(f"   [{i}] <{tag}> {text[:300]}")
            out.append(f"       seat status  : {portal.parse_seats(course, text, row)}")
            out.append(f"       enrol status : {portal.row_enrol_state(row).value}")
            for kind in ("add", "drop", "swap"):
                labels = [portal.label_of(b) for b in portal.buttons(row, kind)]
                out.append(f"       {kind:<5} buttons: {labels or '-'}")
    out.append("")

    out.append("== All visible buttons & links (accessible-name approximation) ==")
    seen = set()
    for role in ("button", "link", "tab", "menuitem"):
        for el in page.get_by_role(role).all()[:400]:
            try:
                if not el.is_visible():
                    continue
                label = portal.label_of(el)
                key = (role, label)
                if key in seen or not label:
                    continue
                seen.add(key)
                disabled = "" if el.is_enabled() else "  [disabled]"
                href = el.get_attribute("href") if role == "link" else None
                out.append(f"   {role:<8} {label[:80]!r}{disabled}{'  -> ' + href if href else ''}")
            except PWError:
                continue
    out.append("")

    out.append("== Elements whose own text mentions the courses (CSS path :: text) ==")
    try:
        for line in page.evaluate(_JS_DESCRIBE, page.locator("body").element_handle()):
            out.append("   " + line)
    except PWError as e:
        out.append(f"   (failed: {type(e).__name__})")
    out.append("")

    out.append("== Tables (header cells) ==")
    for i, table in enumerate(page.locator("table").all()[:10]):
        try:
            headers = [normalise(h) for h in table.locator("th").all_inner_texts()]
            out.append(f"   table[{i}] rows={table.locator('tr').count()} headers={headers}")
        except PWError:
            continue
    out.append("")
    out.append("== Frames ==")
    for f in page.frames:
        out.append(f"   {f.url}")
    return "\n".join(out)


def capture(page: Page, session: BrowserSession, portal: Portal, cfg: Config, out_dir: Path, label: str) -> None:
    if session.on_login_page(page):
        log.warning("This is a login page - not capturing it (keeps credentials out of the capture).")
        return
    folder = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{_slug(label)}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "page.html").write_text(scrub_html(page.content()), encoding="utf-8")
    for i, frame in enumerate(page.frames[1:], start=1):
        try:
            (folder / f"frame{i}.html").write_text(scrub_html(frame.content()), encoding="utf-8")
        except PWError:
            pass
    try:
        (folder / "aria.yml").write_text(page.locator("body").aria_snapshot(), encoding="utf-8")
    except (PWError, AttributeError) as e:
        log.debug("aria snapshot unavailable: %s", e)
    try:
        page.screenshot(path=str(folder / "screenshot.png"), full_page=True)
    except PWError:
        pass
    summary = summarise(page, portal, cfg)
    (folder / "summary.txt").write_text(summary, encoding="utf-8")
    course_part = summary.split("== Course rows found")[1].split("== All visible buttons")[0]
    print("\n== Course rows found" + course_part)
    log.info("Saved capture to %s", folder)


def run_inspect(cfg: Config, session: BrowserSession, out_dir: Path) -> int:
    portal = Portal(session, cfg, dry_run=True)  # dry_run: script never clicks enrolment buttons
    session.ensure_logged_in()
    out_dir.mkdir(parents=True, exist_ok=True)
    assert session.page is not None and session.context is not None
    print(
        "\nINSPECT MODE - the script will not click anything.\n"
        "1. In the browser, go to a page you want captured: the course list with seat counts,\n"
        "   your enrolments page, and (if you can) open an Add / Drop / Swap confirmation\n"
        "   dialog but DO NOT press its final confirm button.\n"
        "2. Come back here, type a short label (e.g. 'courses') and press Enter.\n"
        "3. Type q and Enter when done.\n"
        "Native browser confirm() boxes are auto-cancelled in this mode; their text is printed here.\n"
        f"Captures go to: {out_dir.resolve()}\n"
    )
    capture(session.page, session, portal, cfg, out_dir, "home")
    while True:
        try:
            label = input("label (or q to quit)> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if label.lower() in ("q", "quit", "exit"):
            break
        # Capture the most recently used portal tab (EMS may open things in new tabs).
        pages = [p for p in session.context.pages if not p.is_closed()]
        page = pages[-1] if pages else session.page
        if page is not session.page:
            session.page = page
        capture(page, session, portal, cfg, out_dir, label or "page")
    print(f"\nDone. Review the files in {out_dir.resolve()} before sharing them "
          "(they contain your enrolment details, though tokens are scrubbed).")
    return 0
