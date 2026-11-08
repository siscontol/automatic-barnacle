# ems_swap: swap a Global Experience elective on LBS EMS when a seat opens

This script watches the LBS EMS portal (`https://ems.london.edu/Student/Home`) for a free seat in
**Global Experience – Hong Kong** or **Global Experience – Vietnam**. When one opens, it enrols you in
that course and then drops **Global Experience – Helsinki**.

It follows these rules:

| Situation | What the script does |
|---|---|
| You're already enrolled in Hong Kong or Vietnam | Does nothing and exits. It never touches Helsinki in this case. |
| EMS has its own Swap/Replace control | Uses that first, because the swap happens in one step on EMS's side. |
| No native swap | Adds the new course first. It drops Helsinki only after your enrolments page shows the new course as **Enrolled**. |
| The add is blocked by a clash or credit cap | Tries the other target first. If that's blocked too, it **stops and asks you**. Drop-then-add only happens if you type `DROP HELSINKI`. |
| The add only puts you on a waitlist, or leaves you pending | Stops without dropping anything. Waitlisted is not the same as enrolled. |
| A confirmation dialog mentions an unexpected course | Cancels the dialog and stops to ask you. |
| The swap finishes | Reads your enrolments page again and prints the final state. |

> ### ⚠️ Status: the selectors are untested on the real EMS site
> The script was built and tested against a **mock** EMS site (`tests/mock_ems.py`). I couldn't reach
> `ems.london.edu` or get past your MFA, so I haven't seen the real pages. Everything that depends on how
> the pages look is in `ems_config.toml`. The script fails safe: if it can't find something, or finds two
> candidates, it does nothing.
> **Before running it for real, do steps 3–5 below** (`inspect`, then tune the config, then `--dry-run`).

---

## 1. Install (once)

You need Python 3.9 or later.

```bash
cd automatic-barnacle
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## 2. Set your credentials (every new terminal)

The script reads only the `LBS_USER` and `LBS_PASS` environment variables. It never writes them to disk
and never logs them. Log lines are also filtered so neither value can appear. The browser profile has
password saving turned off.

**macOS / Linux (zsh or bash).** `read -s` keeps the password out of your shell history:

```bash
export LBS_USER="your.name@london.edu"
read -s "?LBS password: " LBS_PASS && export LBS_PASS     # zsh
# bash:  read -s -p "LBS password: " LBS_PASS && export LBS_PASS
```

**Windows PowerShell:**

```powershell
$env:LBS_USER = "your.name@london.edu"
$env:LBS_PASS = Read-Host "LBS password" -MaskInput     # PowerShell 7+
```

If you skip this step, the script still works. You just type your credentials into the login page yourself.

## 3. First run: sign in and capture the real pages

```bash
python -m ems_swap inspect
```

- A browser window opens. The script fills in your username and password on the Microsoft sign-in page.
  **You approve the MFA prompt.** If it asks "Stay signed in?", the script clicks Yes so the session lasts
  longer.
- Your session is saved in a browser profile at `~/.lbs-ems-swap/profile`. It holds cookies only, not your
  password. Later runs reuse it, so you won't need MFA again until the session expires.
- Inspect mode never clicks anything itself. In the browser, go to:
  1. the page that lists the Global Experience electives and their seat availability
  2. your **enrolments** page
  3. if you can, open the Add (or Enrol), Drop and Swap confirmation dialogs. **Don't press the final
     confirm button.**

  After each page, come back to the terminal, type a label (for example `courses`) and press Enter.
  Type `q` when you're done.
- Each capture goes into `inspect_output/<time>_<label>/`. You get `page.html` (anti-forgery tokens
  removed), `aria.yml` (the accessibility tree), `screenshot.png` and `summary.txt`. `summary.txt` shows
  what the current selectors find on that page: course rows, parsed seat status, enrolment status, and the
  Add, Drop and Swap buttons in each row.

**Send me `summary.txt` and `aria.yml` from the course list and enrolments captures.** With those I can
set the selectors exactly. Check the files first: they contain your enrolment details.

## 4. Tune `ems_config.toml`

The settings you're most likely to change:

- `pages.availability` and `pages.enrolments`: paste the URLs of the two pages you captured.
- `courses.targets` and `courses.drop`: if EMS spells the names differently, use
  `{ name = "...", keywords = ["global experience", "hong kong"] }`. A row matches when it contains all
  the keywords.
- `selectors.enrolled_region`: a CSS selector for the "my enrolments" area. Set it if courses appear in
  more than one place on the page.
- `text.seats_available` and `buttons.*`: only needed if the summary shows the wrong seat status or no
  buttons.

Run `inspect` again and check the "Course rows found" section until every course shows exactly one row,
the correct seat status, and the correct buttons.

## 5. Dry run

```bash
python -m ems_swap --dry-run
```

Checks every 10 seconds, plus 1–3 seconds of random jitter. Every check is logged with a timestamp:

```
2026-11-08 14:41:21  INFO    check #12 | Hong Kong: FULL (0 seats) | Vietnam: FULL | next check in 11.4s
```

When a seat opens, it plays a sound and logs the exact plan, for example: "Native swap found → click …",
or "ADD: click 'Enrol' in the row '…' → DROP: click 'Drop' in the row '…'". Nothing is clicked.

Use `--once` to run a single check and exit.

## 6. Live run

```bash
python -m ems_swap                 # visible browser window
python -m ems_swap --headless      # no window; one opens automatically if you need to sign in again
```

Keep the computer awake. On macOS, use `caffeinate -i python -m ems_swap`.

**When the session expires:** monitoring pauses. You get a sound and a console banner, repeated every
minute. If you were running headless, a visible window opens. The script fills in your credentials once;
you approve MFA. Monitoring then resumes on its own (and goes back to headless if you started that way).

**Errors, HTTP 429 and CAPTCHAs:** the wait between checks doubles each time (20 s, 40 s, 80 s, up to
120 s). It returns to 10 s after the next successful check. The script never tries to solve a CAPTCHA. If
the window is visible you can solve it yourself.

**When it finishes,** you get a banner and a sound for success or failure, plus the final enrolment state
read fresh from EMS. Exit codes:

| Code | Meaning |
|---|---|
| 0 | Swapped successfully, or you were already in Hong Kong or Vietnam |
| 1 | Stopped safely and needs your attention (see the message) |
| 2 | Config error |
| 3 | You declined drop-then-add |
| 4 | **Critical:** Helsinki was lost and the new course wasn't obtained. Check EMS now. |
| 130 | You pressed Ctrl-C |

## Other options

```
--config FILE        another config file (default: ./ems_config.toml)
--profile-dir DIR    where the browser session is saved (default: ~/.lbs-ems-swap/profile)
--channel chrome     use your installed Chrome or Edge (msedge) instead of Playwright's Chromium.
                     Try this if Microsoft sign-in complains about the browser.
--no-autofill        type your credentials yourself
--no-sound           console alerts only
--log-file FILE      default ems_swap.log; pass '' to turn off file logging
-v                   debug logging (includes the row text the parser saw)
python -m ems_swap sound-test     check that you can hear the alerts
```

## Safety notes

- **Helsinki is only dropped when** your enrolments page shows Hong Kong or Vietnam as **Enrolled**. This
  is checked again immediately before the Drop click. Waitlisted, pending, ambiguous or missing does not
  count. The one exception is the drop-then-add path, which needs you to type `DROP HELSINKI`. Even then,
  the script checks that the seat is still open before dropping. If the add then fails, it immediately
  tries to re-add Helsinki.
- After every action, the script reads your enrolments page again and decides from that, not from the
  site's success message.
- In `--dry-run`, nothing that changes your enrolment can be clicked.
- Don't run the script with `DEBUG=pw:api`. Playwright's debug output would print the values it types.
- The browser profile folder contains your EMS session cookies. Treat it like a logged-in laptop. Delete
  the folder to log out.
- Please check LBS's rules on automated access to EMS before running this. The polling rate is kept
  modest on purpose, and the minimum allowed interval is 5 seconds.

## Tests

```bash
pip install pytest
pytest                      # browser tests run headless against the mock site
xvfb-run -a pytest          # Linux: also runs the two tests that need a visible window (login)
```

The tests cover: dry-run never clicks, add before drop, the native swap being preferred, already enrolled
→ no action, clash → ask → decline or consent, clash on Hong Kong → Vietnam tried first, a confirmation
dialog that mentions Helsinki → cancel, an add the enrolments page doesn't confirm → never drop, waitlisted
≠ enrolled, back-off on 429, CAPTCHA, session expiry → re-login with autofill, credentials never logged,
and the drop guard.
