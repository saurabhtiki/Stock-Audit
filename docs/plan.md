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
