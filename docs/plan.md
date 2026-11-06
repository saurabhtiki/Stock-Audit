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

Restore a backup zip (Admin → Backup) on a test PC — the old database and media are kept as
`*.before-restore-<time>`; refused on production unless `--force`:
```
DJANGO_ENV=test uv run python manage.py restore_backup stock-audit-production-full-20261106-1610.zip
uv run python manage.py migrate      # only if the app is newer than the backup
```
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

---

## Phase 3 — Physical count, offline count sheet, remarks, Checking Done (DONE — 170 tests pass)

Goal: acceptance criteria 4 and 5. The auditor counts in the app and through the Excel count
sheet, some lines are left Not counted, and at Checking Done queries are created only for
Excess / Shortage lines. After that, counts can't be changed or rolled back.

Example: Audit "Aug26" is frozen with tolerance qty 2. Anil (Auditor) downloads the count sheet
(no ERP Qty in it), counts the store, then uploads it. The preview says *"Row 9: item ZZ-99 is not
in this audit"*. He fixes the row and uploads again. He ticks *"12 lines already counted will be
changed"* and confirms. In the app he types 0 for ITM-007 (status Shortage) and adds the remark
"bin mismatch". 40 lines are still blank. He opens Checking Done, sees *"960 counted · 40 not
counted · 25 Excess (₹12,400) · 31 Shortage (₹18,900) · 56 queries will be created"*, ticks
the box and confirms. Counts are now fixed, and remarks stay editable until the audit is locked.

### Decisions (from grilling)
| # | Decision |
|---|---|
| 1 | In scope: counting in the app, offline sheet, line and general remarks, Checking Done. Checking Done **creates query records** (status Open) for each Excess / Shortage line. Query and action screens come in Phase 4. Media comes later. |
| 2 | Variance within tolerance → status **OK**, with the variance still shown and no query. Over either tolerance → **Excess** / **Shortage**. If ERP Qty is 0 and the count isn't 0, the % test counts as exceeded. |
| 3 | The app shows the auditor ERP Qty, variance and status (filters Not counted / Excess / Shortage / OK). Only the Excel sheet is blind. |
| 4 | Physical Qty box on each row. It saves on Enter or when you click out of the box (HTMX) and shows "Saved ✓". Status and variance update right away, and it works on a phone. Blank = Not counted; clearing a count resets the line. |
| 5 | A count is 0 or more with at most 3 decimals. More decimals or a negative number is an error with a clear message. |
| 6 | Several auditors can count at once and the last save wins. Each line shows who counted it and when, and every count change is logged (old → new). |
| 7 | Count sheet upload: .xlsx / .csv; columns matched by heading. An item not in the audit or a duplicate code = error, and a bad number = error. Blank cell = skipped (never overwrites). A filled cell that changes an existing count = **warning that needs a tick**. A sheet remark is **added** as a new line remark. Preview first; nothing is saved until Confirm, and the file isn't stored. |
| 8 | Remarks: many per line and many per audit, each with who and when. Only the writer edits or deletes, until Locked. Delete is a soft delete and is logged. |
| 9 | Checking Done: anyone with `count_and_remarks` in the Branch. A summary screen + tick; **blocked if nothing is counted**. It can't be undone by anyone. |
| 10 | Admin cancel of a frozen audit only if **no line has a count**. Otherwise the button is hidden and a tooltip explains why. |
| 11 | Before Checking Done, counts and remarks can be changed freely (app + sheet). **After Checking Done, counts are fixed** (app and sheet upload blocked); remarks stay editable until Locked. |
| 12 | The downloaded sheet's Physical Qty column is **always empty** (blind, simple). |
| 13 | Moving the other list screens onto the table component is left for a later small phase. |

### What gets built
1. **AuditLine** gets: `physical_qty` (blank = not counted), `variance_qty`, `variance_value`, `count_status`
   (Not counted / OK / Excess / Shortage, stored so the table can filter and sort), `counted_by`, `counted_at`.
   One function works out variance + status using the audit's tolerance.
2. **Count screen** (audit detail, stage ERP Frozen): audit-lines table (Phase 2.2 component) with
   the Physical Qty box, variance, status badge, counted by/at, filters by status, and counters
   (counted / not counted / Excess / Shortage). Row remark button opens a small remark panel.
3. **Count sheet download** (.xlsx): Item No, Description, UoM, Physical Qty (empty), Remark.
4. **Count sheet upload → preview → confirm** (like the ERP upload): errors in red, warnings in
   amber with one tick, then a summary + Confirm. Each changed count is logged.
5. **Remarks**: `Remark` model (audit, optional line, text, who, when, edited at, deleted flag).
   General remarks section on the audit page, line remarks on each row. Add / edit / delete with log rows.
6. **Checking Done**: summary screen + tick → stage Checking Done, `checking_done_by/at`, and one
   `Query` (status Open, linked to the line and item) for each Excess / Shortage line, all in one transaction.
7. **Admin cancel** checks "nothing counted yet".
8. Tooltips on every new button and box.

### Not in this phase
Query replies, actions, carry-forward, item history, media, locking / reopening, PDF, dashboards,
moving the other list screens to the table component.

### Tests
- status math: within tolerance = OK; over qty or % tolerance = Excess / Shortage; ERP 0 + count 5 = flagged; blank = Not counted; 0 is a valid count
- save a count (HTMX): negative or 4 decimals rejected; clearing resets; who/when stored; log row with old → new
- no `count_and_remarks` right or another Branch → refused; Draft or Checking Done audit → count refused
- sheet download has no ERP Qty and an empty Physical Qty column
- sheet upload: unknown item / duplicate / bad number = error, nothing saved; blank never overwrites;
  changing an existing count needs the tick; remark added, not replaced; blocked after Checking Done
- remarks: only the writer edits / deletes; soft delete; blocked after Locked; logged
- Checking Done: blocked with 0 counted; queries only for Excess / Shortage (not OK, not Not counted);
  counts can't change afterwards; remarks still can
- Admin cancel refused once anything is counted

---

## Phase 4 — Query replies and actions (DONE — 193 tests pass)

Built notes: no new rights were needed (Reply / Actions / Verify & close already exist in the rights grid).
Action owner = any active member of the company, or an Admin. Queries show in a "Queries" tab on each
audit (own + carried) and in a new top-bar "Queries" list. Logic: `audits/queries.py`, screens: `audits/query_views.py`.

Goal: acceptance criteria 6 and 7 — a query goes Open → Reply Submitted → Action Pending → Closed,
with an action owner and a verifier; an open query carries into the next audit and shows the item history.

Example: Anil's audit "Aug26" raised a Shortage query on ITM-007 (ERP 50, counted 42). Priya (User,
Branch A, reply right) writes "Two cartons sent to Branch B on 28 Aug, no slip raised." The query moves
to **Reply Submitted**. Ravi (Manager) adds an action: "Raise transfer slip for 8 pcs", owner Priya,
due 15 Sep. Priya marks it **Done**. Ravi (verify right) checks it and marks it **Verified**. The query
moves to **Closed** after Ravi confirms it. If Ravi had not closed it, it would show up in the
"Aug26" list and in the next audit's list, with its reply and actions still there.

### Decisions (all recommended options confirmed by user in chat)
Confirmed: verifier may be the same person who marked Done (decision 5) · a query can close with no action, with a short note (decision 4) ·
carry-forward is the same record (decision 6) · reopen by anyone with close right, reason required (decision 9).
| # | Decision |
|---|---|
| 1 | Replying uses the existing `reply_queries` right, per Branch. Admin decides who has it. |
| 2 | Adding actions uses `manage_actions`. Marking Done uses `manage_actions` too (the owner or any user with the right). Verifying uses `verify_close_queries`. |
| 3 | One reply per query. Later updates are added as dated entries under it (the reply itself is not overwritten). Writer can edit their own reply until the query is Closed. |
| 4 | Status flow: a reply sets **Reply Submitted**. Adding the first action sets **Action Pending**. When all actions are Verified, a user with `verify_close_queries` clicks **Close**. If no action is needed, that same user can close straight from Reply Submitted, with a short note. |
| 5 | Action status: Open → Done → Verified. Done can be undone by the owner before Verified. |
| 6 | **Carry forward = same record**, not a copy. A query not Closed shows in the next audit's query list for that Branch (labelled with the audit it came from). No duplicate queries. |
| 7 | **Item history** on each query page: one row per audit for that item (date, ERP, Physical, Variance, query status). |
| 8 | Due date is optional. Overdue = due date passed and not Verified; shown in red on the list. |
| 9 | Reopen a Closed query: user with `verify_close_queries`, reason required, status back to Open, logged. (Confirmed.) |
| 10 | Queries are read-only once the audit is Locked (locking itself comes in a later phase). |
| 11 | Each user's home page gets a simple "My pending items" list (replies, actions to do, actions to verify). |

### What gets built
1. **Models**: `QueryReply` (query, text, by, at, edited at), `QueryUpdate` (reply, text, by, at), `Action`
   (query, description, owner, due date, status, done by/at, verified by/at). Query gets `closed_by/at`,
   `reopen_reason`. No hard deletes.
2. **Query list** per Branch (table component, Phase 2.2): query no., item, audit, variance, status,
   open actions, overdue flag. Filters by status and Branch. Includes queries carried forward.
3. **Query page**: line details, item history table, reply box (or the reply + updates), action list
   with add / mark done / verify buttons, Close and Reopen buttons. Each button shows only if the user has the right.
4. **Actions**: add (description, owner dropdown of users in the Branch, due date), mark Done, mark Verified.
5. **Status logic** in one place (`audits/query_status.py`) so views only call it.
6. **Admin screen per Branch**: who can reply, who creates / marks done / verifies actions, who closes queries.
   (Rights grid already exists in Phase 1 — this just adds the 3 action-related rights if missing.)
7. **My pending items** on the home page.
8. Tooltips on every new button and box.
9. Every change is logged (reply, update, action add / done / verified, close, reopen).

### Not in this phase
Media attached to queries or actions, locking, PDF, full dashboards (ageing, overdue charts), reports export.

### Tests
- status flow: Open → reply → Reply Submitted → action → Action Pending → all verified → Close
- close straight from Reply Submitted with no actions; close blocked while any action is not Verified
- reply needs `reply_queries` in that Branch; action add needs `manage_actions`; verify needs `verify_close_queries`
- other Branch users cannot see or act on the query; Viewer sees but cannot act
- one reply per query; updates are added, the reply text is kept; writer edits only before Closed
- carry forward: a not-Closed query shows in the next audit's list; a Closed one does not; no duplicate created
- item history shows one row per audit for that item
- overdue flag: due date passed and not Verified; no due date = never overdue
- reopen needs a reason and right; status back to Open; log row written
- "My pending items" shows only what the user can act on
- every step writes a log row

---

## Phase 5 — Locking, media, reports (DONE — 238 tests pass)

Built notes: lock / reopen in `audits/lock_views.py`; media checks in `audits/media.py`, screens and the
safe file view in `audits/media_views.py`; Excel + PDF in `audits/reports.py` (`report_views.py`).
New rights checks are in `audits/permissions.py` (`can_lock`, `can_reopen_audit`, `can_add_media`,
`can_remove_media`, `can_view_reports`). Line photos sit in each line's remarks window; query and action
photos on the query page. Videos support “Range” requests so iPhones can play them.
New packages: `reportlab`, `pillow-heif`. Tests: `tests/test_lock_media_reports.py`.

Goal: acceptance criteria 8 and 9 — lock and reopen audits with a logged reason, attach photos / videos /
files, and export Excel and PDF that match the screen. Also closes a security gap: `/media/` is now served
to anyone with the link (`config/urls.py`); after this phase media is served only after a permission check.

Example: Anil attaches a photo of a damaged carton to ITM-007 from his phone camera and ticks
*"include in report"*. Ravi (Manager, lock right) opens Lock and sees *"1,000 lines · 40 not counted ·
56 queries: 50 Closed, 6 still open (these will carry forward) · 12 photos"*, ticks and locks. Priya can
still reply to the 6 open queries. Later Admin reopens "Aug26" with the reason *"Count sheet was wrong
for 3 lines"*; it goes back to Checking Done and the log shows who and why. Ravi downloads the PDF with
the photo and the signature block.

### Decisions (from grilling — all recommended answers confirmed)
| # | Decision |
|---|---|
| 1 | Phase 5 = lock / reopen + media + reports. **Dashboards and backup move to Phase 6.** |
| 2 | **Lock**: needs `lock_audit` in that Branch, only from **Checking Done**. Confirm screen with summary (lines, not counted, queries Closed / still open, photos) + tick. |
| 3 | Lock freezes **remarks, media, Closed queries (with their actions)** and blocks **cancel**. Counts are already fixed since Checking Done. |
| 4 | **Open queries stay fully workable after lock** (reply, updates, actions, close), from the old audit and the next one. Replaces Phase 4 decision 10. A Closed query of a locked audit can't be reopened until the audit is reopened. |
| 5 | **Reopen**: **Admin only**, reason required, back to **Checking Done**. Counts stay fixed; remarks and media editable again; Closed queries can be reopened again. Lock / reopen can repeat; every one logged with reason. |
| 6 | Media attaches to the **audit, a count line, a query, or an action**. Audit / line media need `upload_media`. Query / action media: also anyone with `reply_queries` or `manage_actions` in that Branch. Viewers see, can't upload. |
| 7 | **Photos**: JPG / PNG / **HEIC** (new package `pillow-heif`), 10 MB each, shrunk to max 1600 px and saved as JPEG. "Include in report" tick, **off** by default. Phone: picker offers Camera or Gallery; several at once. |
| 8 | **Videos**: MP4 / MOV / WEBM, 100 MB. **Files**: PDF / DOCX / XLSX, 5 MB. Kept as uploaded. File type checked by extension **and** content. |
| 9 | **Remove media**: uploader or Admin, until Locked. Soft delete (hidden, file stays on disk), logged. |
| 10 | Disk: `media/<env>/attachments/<year>/<month>/<random name>`; database keeps path, type, size, original name. |
| 11 | **Viewing**: photo thumbnail → full size; video plays in browser (🎬 icon, no thumbnail); PDF / DOCX / XLSX download. |
| 12 | **Serving media**: only through a permission-checked view (any right in that Branch); no right → 404. Public `/media/` route removed. Company logos and user photos: any logged-in user. |
| 13 | **Excel export** per audit (.xlsx): sheets Lines, Remarks, Queries, Actions, Activity log (that audit). All rows. Needs `view_reports`. |
| 14 | **PDF** per audit (A4, `reportlab`): summary incl. Not counted, variances, line + general remarks, query + action status, selected photos, signature block. Needs `view_reports`. Allowed from **ERP Frozen** on; not Locked → *"DRAFT – not locked"* on every page. |
| 15 | PDF photos: only ticked ones, at the end, 2 per row, caption = item code or "General", uploader, date. Max 100, with a note if more were ticked. |
| 16 | **Signature block**: Auditor (who did Checking Done), Stockkeeper (optional name typed on the Download PDF form, else blank line), Manager (who locked, else blank). |

### What gets built
1. **Lock / reopen**: views + confirm screens on the audit page; stage → Locked / back to Checking Done; logged.
   One central check (`audits/permissions.py`) used by every edit view (remarks, media, closed-query reopen, cancel).
   Phase 4 query rules changed so open queries keep working after lock.
2. **`Attachment` model**: audit, optional line / query / action, kind (photo / video / file), original name,
   stored path, size, include-in-report, uploaded by / at, deleted flag. No hard deletes.
3. **Upload + list** (HTMX) on the audit page and the line / query / action panels; thumbnails for photos;
   include-in-report toggle; remove button.
4. **Media serve view** with permission check; replaces `/media/` in `config/urls.py` (logos / profile photos keep a login-only route).
5. **Excel export** (`openpyxl`) — button on the audit page.
6. **PDF export** (`reportlab`, new) in `audits/reports.py` — "Download PDF" form with optional Stockkeeper name.
7. New packages: `reportlab`, `pillow-heif`.
8. Tooltips on every new button and box; every change logged.

### Not in this phase
Dashboards (status grid, ageing, overdue, trends, repeat items), backup zip, moving the other list screens
to the table component.

### Tests
- lock: only from Checking Done; needs `lock_audit` in that Branch; Draft / Frozen refused; log row
- reopen: Admin only; reason required; back to Checking Done; counts still fixed; log row
- locked: remark add / edit / delete, media add / remove / tick, closed-query reopen, cancel → refused
- locked: open query can still be replied to, get actions, be closed; still carries forward
- media: wrong type / fake extension refused; size limits (10 MB / 100 MB / 5 MB); photo stored as JPEG ≤ 1600 px; HEIC converted
- media: random name in year/month folder; soft delete hides it, file stays; logged
- upload rights: audit / line need `upload_media`; query / action also allow reply / action rights; Viewer refused
- serve: no Branch right → 404; old `/media/attachments/...` URL no longer works; logo still loads
- Excel: 5 sheets; row counts match the database; values match the screen
- PDF: valid PDF; "DRAFT" when not locked, not when locked; Not counted total present; only ticked photos; signature names; needs `view_reports`

---

## Phase 6 — Dashboards and backup (DONE — 302 tests pass)

Built notes: numbers in `audits/dashboards.py`, screen in `audits/dashboard_views.py` (URL `/dashboard/<tab>/`),
charts drawn by `static/js/dashboard.js` (Chart.js from CDN; if it can't load, the tables still show).
The table component got a `keep=` option so the status grid keeps the dashboard filters in its links.
Backup: `core/backup.py`, `core/backup_views.py` (Admin → Backup), `BackupLog` model, banner via
`core.context_processors.backup_reminder`. Restore: `core/management/commands/restore_backup.py`.
Open queries / ageing count queries raised by audits inside the month range. Trend opens on the Branch
of the latest counted audit. Tests: `tests/test_dashboards_backup.py`.

Goal: section 5 dashboards and acceptance criterion 10 — Admin downloads a backup zip and it restores
correctly on a test server.

Example: Ravi (view rights in Branches A and B) opens **Dashboard → Variance**, picks Apr–Oct and type
Scheduled, and sees Branch A: Excess ₹12,400 · Shortage ₹18,900 · Net −₹6,500 for Aug. On **Repeat items**
he sees ITM-007 short in 3 audits in a row at Branch A. Admin sees the amber banner *"No backup for 9 days"*,
clicks **Download backup (database + media)**, and later restores the zip on a test PC with
`uv run python manage.py restore_backup backup.zip`.

### Decisions (from grilling — all recommended answers confirmed)
| # | Decision |
|---|---|
| 1 | Phase 6 = dashboards + backup only. Moving other list screens to the table component and local CDN copies wait for a later small phase. |
| 2 | Dashboards need `view_reports`; data only from Branches where the user has it, in the current company. Admin sees all Branches of that company. |
| 3 | One **Dashboard** page (top bar) with 5 tabs: Audit status · Queries & actions · Variance · Repeat items · Trend. "My pending items" stays on home. |
| 4 | One filter bar for all tabs: Branch (tick list), month range (default last 12 months, by audit date), type All / Scheduled / Surprise. |
| 5 | Charts with **Chart.js** (CDN); every chart has a table under it with CSV download. |
| 6 | **Audit status grid**: Branch, title, date, type, stage, % counted, open queries; row click opens the audit; cancelled hidden unless "Show cancelled"; uses the Phase 2.2 table. |
| 7 | **Ageing** of open (not Closed) queries per Branch: 0–7, 8–30, 31–60, 61–90, 90+ days since raised. **Overdue actions** list: action, owner, due date, days late. |
| 8 | **Variance**: all counted lines (incl. within tolerance); Not counted left out; only audits at Checking Done or Locked. Excess, Shortage, Net = Excess − Shortage, by Branch and month. |
| 9 | **Repeat items**: Excess / Shortage in 2+ audits in a row in the same Branch; shows item, Branch, streak, total variance value; click → item history. |
| 10 | **Trend**: pick a Branch or an Item → line chart of excess / shortage / net per audit over time, with queries raised. |
| 11 | **Backup**: Admin only. Two buttons: database only / database + media. Zip includes `backup_info.txt` (date, environment, app version). Database copied with SQLite's safe backup (app keeps running). |
| 12 | Zip built in a temp folder on disk, streamed to the browser, then deleted. Screen shows expected size first. |
| 13 | Backup screen shows last backup (when, who, which kind). No backup in 7 days (or never) → amber banner on every page, Admins only. Each download logged. |
| 14 | **Restore**: `manage.py restore_backup <zip>` command only (no button). Allowed in development / test; production needs `--force`. Steps written below. |

### What gets built
1. `audits/dashboards.py` — one function per tab (status grid rows, ageing buckets, overdue actions,
   variance by Branch × month, repeat-item streaks, trend series), all taking the user + filters and
   respecting Branch rights.
2. `audits/dashboard_views.py` + `templates/audits/dashboard.html` — tabs load by HTMX, shared filter bar
   kept in the URL, Chart.js charts + tables with CSV, tooltips on every control.
3. `core/backup.py` + Admin "Backup" screen — two download buttons, size estimate, last-backup info.
   `BackupLog` model (who, when, kind, size). Banner through a context processor (Admins only).
4. `core/management/commands/restore_backup.py` — checks the zip, stops if environment is production
   without `--force`, replaces the database and (if present) media folder, keeps a copy of the old database.
5. Restore steps added to "How to run" above.

### Not in this phase
Moving the other list screens to the table component, local CDN copies, scheduled/automatic backups,
email alerts.

### Tests
- dashboards: no `view_reports` → refused; another Branch's / company's data never shows
- status grid: % counted and open-query counts right; cancelled hidden by default
- ageing buckets correct at the edges (7 / 8, 30 / 31 …); overdue = due passed and not Verified
- variance: includes within-tolerance lines, excludes Not counted and Draft / Frozen audits; net = excess − shortage
- repeat items: 2 in a row found, a gap breaks the streak, other Branch doesn't count
- filters (Branch, months, type) change every tab; CSV matches the table
- backup: Admin only; db-only zip has the database + info file; db+media zip also has media; log row + last-backup shown
- banner: shows after 7 days / never, hidden after a backup, never shown to non-Admins
- restore: zip restores into the test environment and the data matches; production refused without `--force`; bad zip refused with a clear message

### Added after Phase 6 (user request — 317 tests pass)
- **My pending items** (home page): 4 tabs — Replies to write / My actions / To verify / To close —
  each with its count and a full table (search, column filters, sort, pages, CSV). No 50-row limit.
  The last opened tab is remembered. Code: `audits/pending_views.py`, `partials/pending_items.html`, `pending_table.html`.
- **Dashboard drilldown**: ▸ on a row opens the detail under it, loaded on first click.
  Variance row → its items (biggest value first, top 50); Repeat row → every audit of that item in the Branch;
  Trend row → that audit's biggest variance items. Ageing numbers link to the Queries list with Branch,
  open statuses, audit dates and "Raised on" range filled in (new "Raised on" column on the Queries list).
  Code: `dashboard_views.dashboard_detail`, `partials/dashboard_detail.html`, `dashboard_expander.html`.
- Tests: `tests/test_pending_drilldown.py`.
- Code review fixes (328 tests pass):
  - Drilldown messages (400/403) now show inside the ▸ box, via `dashboard.js`.
  - Restore refuses drive-letter or backslash names and anything that would land outside the media folder.
  - The backup temp folder is always deleted, even on errors or a cancelled download.
  - A backup is recorded only after the whole zip was sent.
  - Ageing links carry the audit type, and the Queries list gained a Type column.
  - Logged 404s, a shared rights helper, and fewer session writes.
