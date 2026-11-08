"""A small stateful fake of an EMS-like portal, used only by the tests.

It is NOT a copy of the real LBS EMS (which we can't see from CI); it just exercises
the same kinds of things: SSO redirect + MFA wait, a course table with seat counts,
Add with an HTML modal, Drop with a native confirm(), an optional native Swap,
clash rules, 429s, CAPTCHAs and session expiry.

Portal is served on 127.0.0.1:<port>; the fake identity provider on localhost:<port>,
so the portal and the "SSO" really are different hosts.
"""

from __future__ import annotations

import html
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Set
from urllib.parse import parse_qs, quote, urlparse

HK = "Global Experience – Hong Kong"
VN = "Global Experience – Vietnam"
HEL = "Global Experience – Helsinki"
LIS = "Global Experience – Lisbon"


@dataclass
class Scenario:
    seats: Dict[str, int] = field(default_factory=lambda: {HK: 0, VN: 0, HEL: 0, LIS: 4})
    enrolled: Set[str] = field(default_factory=lambda: {HEL})
    waitlisted: Set[str] = field(default_factory=set)
    open_after: Dict[str, int] = field(default_factory=dict)  # course -> electives view no. when 1 seat opens
    native_swap: bool = False
    clash_with: Dict[str, str] = field(default_factory=dict)  # adding key clashes with enrolled value
    add_confirm: str = "modal"  # "modal" | "native" | "none"
    add_confirm_text: Optional[str] = None
    add_puts_on_waitlist: bool = False
    add_fake_success: bool = False  # says "enrolled" but doesn't enrol
    rate_limit_on: Set[int] = field(default_factory=set)  # electives view numbers -> 429
    captcha_on: Set[int] = field(default_factory=set)
    expire_on: Set[int] = field(default_factory=set)
    user: str = "student@london.edu"
    password: str = "correct horse"


class MockEMS:
    def __init__(self, scenario: Scenario) -> None:
        self.sc = scenario
        self.lock = threading.Lock()
        self.sessions: Set[str] = set()
        self.tickets: Set[str] = set()
        self.views = 0
        self.actions: List[str] = []  # e.g. "add:HK", "drop:HEL", "swap:HEL->HK"
        self.logins = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def portal(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def idp(self) -> str:
        return f"http://localhost:{self.port}"

    def new_session(self) -> str:
        with self.lock:
            sid = secrets.token_hex(8)
            self.sessions.add(sid)
            return sid

    def start(self) -> "MockEMS":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------------ html
    @staticmethod
    def page(title: str, body: str, extra_head: str = "") -> str:
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<meta name="csrf-token" content="SECRET-CSRF-TOKEN">{extra_head}
<style>.modal{{display:none;position:fixed;top:20%;left:30%;background:#fff;border:1px solid #333;padding:1em}}
.modal.show{{display:block}}</style></head><body>
<nav><ul><li><a href="/Student/Home">Home</a></li><li><a href="/Student/Electives">Electives</a></li>
<li><a href="/Student/Enrolments">My enrolments</a></li></ul></nav>
<main>{body}</main></body></html>"""

    def flash(self, q: Dict[str, List[str]]) -> str:
        msg = q.get("msg", [""])[0]
        kind = q.get("kind", ["info"])[0]
        return f'<div class="alert alert-{kind}" role="alert">{html.escape(msg)}</div>' if msg else ""

    def electives_html(self, q) -> str:
        sc = self.sc
        rows = []
        for name, seats in sc.seats.items():
            esc = html.escape(name)
            if name in sc.enrolled:
                seat_txt, action = "Enrolled", ""
            else:
                seat_txt = f"{seats} seats available" if seats else "Full"
                if seats > 0:
                    if sc.add_confirm == "modal":
                        text = html.escape(sc.add_confirm_text or f"Confirm enrolment in {name}?")
                        action = (f'<form method="post" action="/Student/Add" id="f-{abs(hash(name))}">'
                                  f'<input type="hidden" name="course" value="{esc}">'
                                  f'<input type="hidden" name="__RequestVerificationToken" value="SECRET-RVT">'
                                  f'<button type="button" class="btn" onclick="openAdd(this.form, \'{text}\')">Enrol</button>'
                                  f'</form>')
                    elif sc.add_confirm == "native":
                        text = html.escape(sc.add_confirm_text or f"Enrol in {name}?")
                        action = (f'<form method="post" action="/Student/Add" onsubmit="return confirm(\'{text}\')">'
                                  f'<input type="hidden" name="course" value="{esc}">'
                                  f'<input type="submit" value="Enrol"></form>')
                    else:
                        action = (f'<form method="post" action="/Student/Add">'
                                  f'<input type="hidden" name="course" value="{esc}">'
                                  f'<button type="submit">Enrol</button></form>')
                else:
                    action = '<button type="button" disabled>Enrol</button> <button type="button">Join waitlist</button>'
            rows.append(f"<tr><td>{esc}</td><td>Week 7</td><td>{seat_txt}</td><td>{action}</td></tr>")
        modal = """
<div class="modal" role="dialog" aria-modal="true" id="addModal"><p id="addText"></p>
<button type="button" id="addOk">Confirm</button> <button type="button" onclick="closeM()">Cancel</button></div>
<script>
let pending = null;
function openAdd(form, text) { pending = form; document.getElementById('addText').textContent = text;
  document.getElementById('addModal').classList.add('show'); }
function closeM() { document.getElementById('addModal').classList.remove('show'); pending = null; }
document.getElementById('addOk').onclick = () => { const f = pending; closeM(); f.submit(); };
</script>"""
        return self.page("Electives", f"""<h1>Global Experience electives</h1>{self.flash(q)}
<table class="table"><thead><tr><th>Course</th><th>Dates</th><th>Seats</th><th>Action</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>{modal}""")

    def enrolments_html(self, q) -> str:
        sc = self.sc
        rows = []
        for name in list(sc.enrolled) + list(sc.waitlisted):
            esc = html.escape(name)
            status = "Enrolled" if name in sc.enrolled else "Waitlisted"
            actions = ""
            if name in sc.enrolled:
                actions = (f'<form method="post" action="/Student/Drop" '
                           f'onsubmit="return confirm(\'Are you sure you want to drop {esc}?\')">'
                           f'<input type="hidden" name="course" value="{esc}"><button type="submit">Drop</button></form>')
                if sc.native_swap:
                    actions += f' <button type="button" onclick="openSwap(\'{esc}\')">Swap</button>'
            rows.append(f"<tr><td>{esc}</td><td>{status}</td><td>{actions}</td></tr>")
        opts = "".join(f"<option>{html.escape(n)}</option>" for n, s in sc.seats.items()
                       if s > 0 and n not in sc.enrolled)
        swap_modal = f"""
<div class="modal" role="dialog" id="swapModal"><form method="post" action="/Student/Swap">
<p>Swap <span id="swapFrom"></span> for:</p><input type="hidden" name="from" id="swapFromIn">
<select name="to"><option value="">-- choose --</option>{opts}</select>
<button type="submit">Confirm swap</button> <button type="button" onclick="document.getElementById('swapModal').classList.remove('show')">Cancel</button>
</form></div>
<script>function openSwap(n) {{ document.getElementById('swapFrom').textContent = n;
document.getElementById('swapFromIn').value = n; document.getElementById('swapModal').classList.add('show'); }}</script>""" if sc.native_swap else ""
        empty = "" if rows else "<tr><td colspan=3>No enrolments</td></tr>"
        return self.page("My enrolments", f"""<h1>My enrolments</h1>{self.flash(q)}
<table id="mine"><thead><tr><th>Course</th><th>Status</th><th></th></tr></thead>
<tbody>{''.join(rows)}{empty}</tbody></table>{swap_modal}""")

    # ------------------------------------------------------------------ handler
    def _handler(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code: int, body: str = "", headers: Optional[Dict[str, str]] = None) -> None:
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _redirect(self, url: str, cookie: Optional[str] = None) -> None:
                h = {"Location": url}
                if cookie:
                    h["Set-Cookie"] = cookie
                self._send(302, "", h)

            def _form(self) -> Dict[str, str]:
                n = int(self.headers.get("Content-Length", 0))
                return {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}

            def _host(self) -> str:
                return (self.headers.get("Host") or "").split(":")[0]

            def _authed(self) -> bool:
                cookie = self.headers.get("Cookie") or ""
                for part in cookie.split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "ems_session" and v in mock.sessions:
                        return True
                return False

            def _to_login(self) -> None:
                self._redirect(f"{mock.idp}/idp/login?return={quote(self.path)}")

            # ---------------------------------------------------------- GET
            def do_GET(self) -> None:
                u = urlparse(self.path)
                q = parse_qs(u.query)
                with mock.lock:
                    if self._host() == "localhost":
                        return self._idp_get(u.path, q)
                    if u.path == "/sso/callback":
                        t = q.get("ticket", [""])[0]
                        if t in mock.tickets:
                            mock.tickets.discard(t)
                            sid = secrets.token_hex(8)
                            mock.sessions.add(sid)
                            return self._redirect(q.get("return", ["/Student/Home"])[0],
                                                  f"ems_session={sid}; Path=/; Max-Age=3600; HttpOnly")
                        return self._send(403, "bad ticket")
                    if not self._authed():
                        return self._to_login()
                    if u.path == "/Student/Home":
                        return self._send(200, mock.page("EMS Home", "<h1>Welcome to EMS</h1>"))
                    if u.path == "/Student/Electives":
                        mock.views += 1
                        v = mock.views
                        for course, at in mock.sc.open_after.items():
                            if v == at and course not in mock.sc.enrolled:
                                mock.sc.seats[course] = max(mock.sc.seats[course], 1)
                        if v in mock.sc.expire_on:
                            mock.sessions.clear()
                            return self._to_login()
                        if v in mock.sc.rate_limit_on:
                            return self._send(429, "<h1>Too Many Requests</h1>", {"Retry-After": "1"})
                        if v in mock.sc.captcha_on:
                            return self._send(200, mock.page("Checking your browser",
                                                             '<div class="g-recaptcha"></div><p>Verify you are human</p>'))
                        return self._send(200, mock.electives_html(q))
                    if u.path == "/Student/Enrolments":
                        return self._send(200, mock.enrolments_html(q))
                    return self._send(404, "not found")

            def _idp_get(self, path: str, q) -> None:
                ret = q.get("return", ["/Student/Home"])[0]
                if path == "/idp/login":
                    return self._send(200, mock.page("Sign in", f"""<h1>Sign in</h1>
<form method="post" action="/idp/user?return={quote(ret)}"><input type="email" name="loginfmt" placeholder="Email">
<input type="submit" value="Next"></form>"""))
                if path == "/idp/mfa":
                    return self._send(200, mock.page("Approve sign in", "<p>Approve the request on your phone</p>",
                                                     f'<meta http-equiv="refresh" content="1;url=/idp/kmsi?return={quote(ret)}">'))
                if path == "/idp/kmsi":
                    return self._send(200, mock.page("Stay signed in?", f"""<h1>Stay signed in?</h1>
<form method="post" action="/idp/done?return={quote(ret)}"><button type="submit">Yes</button></form>"""))
                return self._send(404, "idp 404")

            # ---------------------------------------------------------- POST
            def do_POST(self) -> None:
                u = urlparse(self.path)
                q = parse_qs(u.query)
                form = self._form()
                with mock.lock:
                    if self._host() == "localhost":
                        return self._idp_post(u.path, q, form)
                    if not self._authed():
                        return self._to_login()
                    sc = mock.sc
                    c = form.get("course", "")
                    if u.path == "/Student/Add":
                        mock.actions.append(f"add:{c}")
                        if c in sc.enrolled:
                            return self._redirect("/Student/Electives?kind=warning&msg=" + quote("Already enrolled"))
                        if sc.seats.get(c, 0) <= 0:
                            return self._redirect("/Student/Electives?kind=danger&msg=" + quote("Sorry, this course is full"))
                        other = sc.clash_with.get(c)
                        if other and other in sc.enrolled:
                            return self._redirect("/Student/Electives?kind=danger&msg=" + quote(
                                f"Unable to enrol: timetable clash with {other}"))
                        if sc.add_fake_success:
                            return self._redirect("/Student/Electives?kind=success&msg=" + quote(
                                "You have been enrolled"))
                        if sc.add_puts_on_waitlist:
                            sc.waitlisted.add(c)
                            return self._redirect("/Student/Electives?kind=info&msg=" + quote("Request received"))
                        sc.seats[c] -= 1
                        sc.enrolled.add(c)
                        return self._redirect("/Student/Electives?kind=success&msg=" + quote(
                            f"You have been enrolled in {c}"))
                    if u.path == "/Student/Drop":
                        mock.actions.append(f"drop:{c}")
                        if c in sc.enrolled:
                            sc.enrolled.discard(c)
                            sc.seats[c] = sc.seats.get(c, 0) + 1
                        return self._redirect("/Student/Enrolments?kind=success&msg=" + quote(f"{c} has been dropped"))
                    if u.path == "/Student/Swap":
                        f, t = form.get("from", ""), form.get("to", "")
                        mock.actions.append(f"swap:{f}->{t}")
                        if f in sc.enrolled and t and sc.seats.get(t, 0) > 0 and t not in sc.enrolled:
                            sc.enrolled.discard(f)
                            sc.seats[f] += 1
                            sc.enrolled.add(t)
                            sc.seats[t] -= 1
                            return self._redirect("/Student/Enrolments?kind=success&msg=" + quote("Courses swapped"))
                        return self._redirect("/Student/Enrolments?kind=danger&msg=" + quote("Swap failed"))
                    return self._send(404, "not found")

            def _idp_post(self, path: str, q, form) -> None:
                ret = q.get("return", ["/Student/Home"])[0]
                if path == "/idp/user":
                    if form.get("loginfmt") != mock.sc.user:
                        return self._send(200, mock.page("Sign in", "<p>Unknown user</p>"))
                    return self._send(200, mock.page("Enter password", f"""<h1>Enter password</h1>
<form method="post" action="/idp/pass?return={quote(ret)}"><input type="password" name="passwd">
<input type="submit" value="Sign in"></form>"""))
                if path == "/idp/pass":
                    if form.get("passwd") != mock.sc.password:
                        return self._send(200, mock.page("Enter password", """<p>Your password is incorrect</p>
<form method="post" action="/idp/pass"><input type="password" name="passwd"><input type="submit" value="Sign in"></form>"""))
                    return self._redirect(f"/idp/mfa?return={quote(ret)}")
                if path == "/idp/done":
                    mock.logins += 1
                    t = secrets.token_hex(8)
                    mock.tickets.add(t)
                    return self._redirect(f"{mock.portal}/sso/callback?ticket={t}&return={quote(ret)}")
                return self._send(404, "idp 404")

        return H
