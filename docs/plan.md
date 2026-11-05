# Plan

## How to run
```
uv sync
uv run python manage.py migrate
uv run python manage.py seed_demo        # development only: demo companies + users
uv run python manage.py create_admin     # real first Admin (any environment)
uv run python manage.py runserver
uv run pytest                             # tests
```
Pick the environment with `DJANGO_ENV` (default `development`).
Production: copy `.env.production.example` → `.env.production`, set a long random `SECRET_KEY`,
then `uv run python manage.py collectstatic`.

## Phase 0 — Project setup in Django (DONE)
The first setup was done in FastAPI by mistake (typo in CLAUDE.md). Redone in Django:
- Django 5.2 LTS, uv, HTMX + Alpine.js + DaisyUI (CDN for now), pytest (pytest-django)
- One settings file per environment, chosen by `DJANGO_ENV`:
  - `development` → `data/development.db`, `media/development/`
  - `test` → `data/test.db`, `media/test/`
  - `production` → `data/production.db`, `media/production/` (secret key from `.env.production`)
- `/health` page checks app + database
- Remove the FastAPI files

## Phase 1 — Login, companies, branches, users, rights (DONE — 41 tests pass)

### Phase 1.1 — polish (DONE — 53 tests pass)
- Light / dark theme switch (top bar and login page; remembered in the browser, follows the computer's setting the first time).
- Coloured buttons (blue = main, teal = secondary, amber = careful, green = turn on, red = remove), blue top bar, coloured table headings.
- Every list table: click a heading to sort, filter box, CSV download icon (`static/js/table_tools.js`). The Activity log CSV holds the rows on the page only (50 per page).
- Users: **department** (Admin sets it, with suggestions from existing ones) and **photo** (each user adds, changes or removes their own on "My profile"; shrunk to 400 px; PNG/JPG up to 5 MB; photo changes are logged).

Left for later (from code review): merge the 4 similar "activate/deactivate" views into one
helper; load company/role in the top bar lazily. Before going live: keep a local copy of
the CDN files (HTMX, Alpine, DaisyUI, font) so the app works without internet.

Goal: Admin can set up the group and give one user different rights in two Branches
(acceptance criterion 1).

### Decisions (from grilling)
| # | Decision |
|---|---|
| 1 | Django (not FastAPI) |
| 2 | Admin is group-wide: sees and manages all companies and Branches |
| 5 | A user is assigned to 1 or more companies, with **one role per company** |
| 6 | Password ≥ 8 chars; must change at first login and after reset; logout after 8 h idle; 5 wrong tries → locked 15 min |
| 7 | Activity log starts in this phase |
| 8 | **Username is unique across the whole group.** Login = username + password (no company dropdown) |
| 9 | User works in **one company at a time**; switcher in the top bar (only if they have 2+ companies) |
| 10 | "Copy rights from user X" copies only within the chosen company |
| 11 | Own DaisyUI screens for Admin; Django admin panel for developer only, off in production |
| 12 | Django 5.2, settings per environment via `DJANGO_ENV`, pytest |
| 13 | Deactivating a company makes its Branches inactive and removes users' access to it (nothing deleted; reactivating restores) |
| 14 | Email required and unique across the group |

Example: Ravi is Auditor in Company A and Manager in Company B. He logs in with "ravi",
lands in Company A (Auditor rights), switches to B and sees only B's Branches with Manager rights.

### What gets built
1. **Login / logout / change password** — clear messages, e.g. *"Username or password is wrong."*,
   *"Too many wrong tries. Try again in 15 minutes."*
2. **First Admin** — created with `uv run python manage.py create_admin`.
3. **Companies** — add, edit, deactivate. Fields: name, address, logo (PNG/JPG, max 1 MB), status.
4. **Branches** — add, edit, deactivate. Fields: company, name, code (unique in company), status.
5. **Users** — Admin adds, edits, deactivates (never deletes), resets password.
   Fields: username (unique), full name, email (unique), status, Admin flag.
6. **Company membership** — per user: list of companies, each with one role.
7. **Rights grid** (per user, per company) — rows = that company's Branches, columns = 9 rights.
   - Ticks start from the role's defaults.
   - "Apply to all Branches of this company" button.
   - "Copy rights from another user" (same company only).
8. **Company switcher** in top bar; the user sees only Branches where they have at least one right.
9. **Activity log** — every add / edit / deactivate / rights change / login lock:
   user, time, record, old value, new value. Read-only Admin screen; no one can edit or delete it.

### The 9 rights
upload_erp · set_tolerance_freeze · count_and_remarks · upload_media · reply_queries ·
manage_actions · verify_close_queries · lock_audit · view_reports

### Default rights by role
| Role | Default ticks |
|---|---|
| Admin | all (plus Admin screens) |
| Manager | set_tolerance_freeze, manage_actions, verify_close_queries, lock_audit, view_reports |
| User | upload_erp, reply_queries, manage_actions, view_reports |
| Auditor | count_and_remarks, upload_media, view_reports |
| Viewer | view_reports |

### Not in this phase
Items, audits, ERP upload, counts, queries, dashboards, backup.

### Tests
- login ok / wrong password / deactivated user blocked / lock after 5 tries / forced password change
- username and email unique across the group
- user in 2 companies has a different role in each; switcher shows only their companies
- rights in Branch A only → cannot see Branch B
- apply-to-all and copy-rights (same company only) work
- deactivating a company hides its Branches and blocks access; reactivating restores it
- every change writes a log row; the log cannot be edited or deleted

---

## Phase 2 — Items, audits, ERP upload, tolerance & freeze (DONE — 85 tests pass)

Goal: acceptance criteria 2 and 3 — a user creates an audit, uploads an ERP file with errors,
fixes it, replaces the draft and confirms; a Manager sets tolerance and freezes ERP stock.

Example: Priya (User, Branch A) creates "Aug26" dated 30 Aug, type Scheduled, stock as on 29 Aug.
She uploads `erp.xlsx` → preview shows *"Row 14: ERP Qty 'abc' is not a number"* → she fixes it,
uploads again, ticks *"I've checked the 3 negative-quantity lines"*, confirms. Ravi (Manager) sets
tolerance qty 2 / 5 %, sees the summary, ticks the box and freezes. The lines are now locked.

### Decisions (from grilling)
| # | Decision |
|---|---|
| 1 | Tolerance + freeze are in this phase; counting starts in Phase 3 |
| 2 | Upload accepts .xlsx and .csv; template downloads as .xlsx (`openpyxl` added) |
| 3 | Items: Admin + users with `set_tolerance_freeze` in any Branch of the company can add / edit / mark "New" items as Reviewed; others view only |
| 4 | Audit line keeps a copy of the file's description, UoM and rate; the Items list text is not changed |
| 5 | Audit = Branch + **Title** (≤ 60 chars, unique within Branch, cancelled audits ignored). Same Branch + same date allowed. Title pre-filled from date (e.g. "Aug26"), editable |
| 6 | New item codes are added (marked "New") only on confirm; they stay even if the draft is replaced |
| 7 | Row checks: blank rows skipped; missing code / qty / rate = error; zero rate ok; qty ≤ 3 decimals, rate ≤ 2 decimals; columns matched by heading name (any order / case); missing heading = error; duplicate item code = error; negative qty = warning |
| 8 | Limit 20,000 rows / 5 MB per file |
| 9 | "Stock as on" ≤ audit date; future audit dates allowed |
| 10 | Every uploaded file (incl. replaced) kept on disk and listed on the audit page |
| 11 | Log: audit created / edited / cancelled, file uploaded or replaced (name, lines, total value), tolerance set, frozen — not each line |
| 12 | While ERP Draft, the creator or Admin can edit title, date, type, "stock as on" |
| 13 | Cancel (reason required, nothing deleted): Admin at Draft or Frozen if nothing counted; creator their own Draft |
| 14 | Freeze screen shows lines, total value, negative lines, new items, tolerance + tick to confirm |
| 15 | Audit list: title, Branch, date, type, stage, lines, total value; filters Branch / stage / type / date; visible to anyone with any right in the Branch |
| 16 | One tick accepts all negative-quantity warnings |

### What gets built (new app `audits`)
1. **Items** — list per company (search, "New" filter), add / edit / deactivate, "Mark reviewed".
2. **Audit create / edit / cancel** — Branch, title, audit date, type, stock as on.
3. **Template download** — .xlsx with headings: Item No, Description, UoM, ERP Qty, Rate p.u.
4. **Upload → preview → confirm** — preview table with errors (red) and warnings (amber), summary
   (lines, total value, errors, warnings). Nothing saved until confirm. Re-upload replaces the draft.
5. **Audit page** — details, stage badge, ERP lines (search, paginated, total value), uploaded files list.
6. **Tolerance + freeze** — qty tolerance (default 0) and % tolerance (optional); summary + tick; stage → ERP Frozen.
7. **Audit list** with filters; menu links for Items and Audits; tooltips on all actions.

### Not in this phase
Counting, offline count sheet, Checking Done, queries, actions, media, locking, reports, dashboards.

### Tests
- template downloads with the right headings
- upload with errors → blocked; negative qty needs the tick; nothing saved before confirm
- csv and xlsx both work; heading order / case doesn't matter; 20,000-row / 5 MB limit
- confirm saves lines, total = qty × rate, new items added as "New"; replace swaps the lines
- title unique within Branch; same date allowed; "stock as on" after audit date rejected
- users without upload right can't create / upload; other-Branch users can't see the audit
- set tolerance + freeze → lines, tolerance and upload blocked afterwards
- cancel rules (Admin vs creator, reason required); every step writes a log row

### Left for Phase 3
- Admin cancel must also check "nothing counted yet" once counts exist.
- Uploaded ERP files sit under `media/` with random names; serve all media through a
  permission check before going live.

---

## Phase 2.1 — Fixes before Phase 3 (DONE — 97 tests pass)

Small fixes the user asked for after trying Phase 2. Confirmed in chat (grilled point by point).

### Decisions
| # | Decision |
|---|---|
| 1 | **Logout bug first**: the whole "Log out" menu row is the button; one click logs out, the button shows "Logging out…" and can't be clicked twice; user lands on the login page with "You are logged out." |
| 2 | **UoM is required** in the ERP file. Blank UoM = error, e.g. *"Row 7: UoM is missing."* |
| 3 | **Too many decimals = warning, not error.** Limits stay (qty 3, rate 2). Extra decimals are rounded (half up), e.g. *"ERP Qty 12.34567 will be saved as 12.346."* The same tick box accepts all warnings ("I've checked the 4 warning lines"). Or the user uploads a new file. |
| 4 | **Uploaded file is not stored or shown.** File has errors → nothing saved, the error list is shown, user fixes and uploads again. No errors → only the checked lines are kept until Confirm, then the summary + Confirm button. No download link. (Replaces Phase 2 decision 10.) |
| 5 | **Mark many New items Reviewed at once.** Tick rows by hand, tick "all on this page", or after a search/filter (e.g. "rm") pick "Select all 38 matching". Button "Mark N reviewed" asks *"38 items will be marked Reviewed. Continue?"* Only items still New change; each change is logged. |

### What gets built
1. Navbar logout fix (template + small Alpine/JS guard).
2. `erp_import`: blank UoM error; decimals rounded with a warning; general warning text.
3. `ErpUpload`: drop the file field, keep the checked lines (JSON) until Confirm; errors shown straight after "Check file" without saving; remove the download link/URL.
4. Items list: checkboxes, select-all-on-page, select-all-matching, bulk "Mark reviewed" with confirm.

### Tests
- logout: one POST logs out and redirects to login with the message; GET is refused
- blank UoM → error; 12.34567 qty → warning and saved as 12.346; rate 1.239 → 1.24
- warnings (decimals or negative) need the tick
- file with errors → no ErpUpload row, errors shown; clean file → no file on disk, confirm works
- bulk review: by ids, by filter ("rm"), only New items change, other company's items untouched, no right → refused, one log row per item

### Left for later (from code review)
- Bulk review writes one log row per item (fine for hundreds; batch it if thousands become common).
- Refreshing the error page asks to resend the old file (nothing is saved, so harmless).

---

## Phase 2.2 — Reusable table component (DONE — 116 tests pass)

One table component for the whole app: sort, a filter on each column, CSV download and pages.
Confirmed in chat (grilled point by point).

### Decisions (from grilling)
| # | Decision |
|---|---|
| 1 | **Everything runs on the server** (Django), for every table. Each click asks for fresh rows (~0.1–0.3 sec), and this works the same for 50 or 50,000 rows. The quick search box waits ~0.3 sec after typing stops; a column pop-up applies on Enter or **Apply**. |
| 2 | **Column types are set in code**: `text`, `choice`, `number`, `date`. No guessing from the data. |
| 3 | **A funnel icon on each column header** opens a small pop-up for that column. Text = "contains" (ignores case). Choice = tick list. Number / date = from–to range. |
| 4 | **Tick list**: search box at the top, Select all / Clear, a count next to each value, e.g. `KG (1,240)`, and a **(Blank)** option. Only columns marked `choice` get a tick list. |
| 5 | **Range**: one end can be left empty. "Qty from 10" means ≥ 10. |
| 6 | **Combining**: AND across columns, OR inside one tick list. E.g. UoM = KG **or** PCS, **and** Qty 10–50. |
| 7 | **Active filters**: chips above the table (`UoM: KG, PCS ✕`), `3 filters on · Clear all`, and a filled funnel on filtered columns. |
| 8 | **Sort**: one column at a time. Click for A→Z, again for Z→A, a third time for the normal order. Each table sets its own normal order (Items = item code, Activity log = newest first). |
| 9 | **Pages**: 25 / 50 / 100 / 200 rows, **default 25**. `« 1 2 3 … 25 »` and "Showing 26–50 of 1,240". |
| 10 | **URL keeps the state**, e.g. `?uom=KG,PCS&qty_min=10&sort=-erp_value&page=2`. Refresh, Back and shared links all work. |
| 11 | **CSV = all rows that match the filters and sort**, across every page. The tooltip says so: *"Downloads all 1,240 rows that match your filters, not just this page."* Plain numbers (`12500.50`), dates as `2026-11-05`, streamed (no row limit, server doesn't freeze), protected against Excel formulas. |
| 12 | **Items page keeps** its quick search box (searches all text columns) and its New / Active / Inactive buttons. They work together with the column filters. Ticking items for bulk review stays as it is. |
| 13 | **On a phone**: the table scrolls sideways, the first column stays fixed, and pop-ups open full-width. |
| 14 | **Our own small component**, no new packages (not django-tables2 / django-filter). |
| 15 | **First tables to move**: Items and Audit lines. Other screens keep `table_tools.js` until a later step. |

### What gets built
1. `core/datatable.py`: `Column(name, label, type, choices=None, sortable=True, filterable=True, csv=True, align_right=False)` and a `DataTable` class. You give it a queryset and columns. It reads the URL, then filters, sorts, makes pages, counts the tick-list values and streams the CSV.
2. `templates/partials/datatable.html` (+ header / filter pop-up / pager partials): DaisyUI table, funnel dropdowns, chips, page-size select and pager. Everything reloads with HTMX (`hx-get`, `hx-push-url`), and Alpine.js handles the pop-ups.
3. Tooltips on every control (funnel, sort, page size, CSV, Clear all).
4. Items list and Audit lines (audit detail) moved onto the component. Each gets a `?export=csv` link.
   Items' old `show=` select became the shortcut buttons All / New / Active / Inactive (they set the Status filter,
   e.g. `?status=new`). "Select all N matching" for bulk review now sends the full filter string.
5. Files: `core/datatable.py`, `templates/partials/datatable/{toolbar,head_cells,pager}.html`,
   `static/js/datatable.js`, tests in `tests/test_datatable.py`.

### Not in this phase
- Moving the other ~10 list screens (Companies, Branches, Users, Activity log…).
- Sorting by several columns, showing / hiding columns, saved filters.

### Tests
- text "contains" ignores case; tick list with 2 values = OR; (Blank) finds empty values
- number / date range with both ends, only "from", only "to"; a bad value (e.g. `qty_min=abc`) is ignored with a message, no crash
- two filters together = AND; sort asc / desc / normal; unknown sort column ignored
- page size 25 default, 100 allowed, 999 falls back to 25; a page past the end shows the last page
- tick-list counts are right
- CSV has every matching row (not just the page), plain numbers, ISO dates, formula guard, skips columns marked `csv=False`
- Items: quick search + New/Active buttons + column filters work together; bulk review still works
- another company's rows never show up, in the table or in the CSV
