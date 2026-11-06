# The three environments

The app has three separate copies of its data. Each has its own database and media folder,
so work in one never touches the others. You choose one with `DJANGO_ENV`.

| Environment | What it is for | Database | Media (photos, files) | Looks like |
|---|---|---|---|---|
| **development** | writing code; trying new screens | `data/development.db` | `media/development/` | debug pages on, Django admin panel on, yellow **development** badge |
| **test** | your **dress rehearsal**: try anything with fake data before doing it for real | `data/test.db` | `media/test/` | works like production (debug off, real login rules), yellow **test** badge |
| **production** | real data only | `data/production.db` | `media/production/` | no badge |

The **automatic tests** (`uv run pytest`) are separate again: every run gets a throw-away database
and a temporary media folder that are deleted at the end. They never touch the three above.

Example: you want to see what happens when Admin reopens a locked audit. Do it in **test** first,
check the log and the PDF, then do it in production.

All commands below are for **PowerShell** on Windows, run from the project folder
(the one with `manage.py`). Linux commands are in `docs/deploy.md`.

> `$env:DJANGO_ENV="test"` lasts until you close that PowerShell window. Open a new window
> (or run `$env:DJANGO_ENV="development"`) to go back.

---

## Development (default)

```powershell
uv sync                                   # install / update packages (first time and after updates)
uv run python manage.py migrate           # create / update the database
uv run python manage.py seed_demo         # demo companies + users (logins: admin, ravi / demo-pass-123)
uv run python manage.py runserver         # open http://127.0.0.1:8000
```

## Test (dress rehearsal)

```powershell
$env:DJANGO_ENV="test"
uv run python manage.py migrate
uv run python manage.py seed_demo         # optional: demo data (logins: admin, ravi / demo-pass-123)
uv run python manage.py create_admin      # optional: your own Admin login instead
uv run python manage.py serve             # open http://127.0.0.1:8000 — same web server as production
```

Want other PCs in the office to try it? Start it with `--host 0.0.0.0` and open
`http://<this PC's IP>:8000` on the other PC. (No HTTPS here, so use it for fake data only.)

### Reset test to empty

Stop the server (Ctrl+C), then:

```powershell
$env:DJANGO_ENV="test"
Remove-Item data\test.db*, media\test -Recurse -Force -ErrorAction SilentlyContinue
uv run python manage.py migrate
uv run python manage.py seed_demo
```

### Try a backup → restore round trip

1. In **production** (or development), log in as Admin → **Admin → Backup** → download
   **Database + media**. You get a file like `stock-audit-production-full-20261106-1610.zip`.
2. Restore it into **test**:

```powershell
$env:DJANGO_ENV="test"
uv run python manage.py restore_backup C:\Users\you\Downloads\stock-audit-production-full-20261106-1610.zip
uv run python manage.py migrate           # only needed if the app is newer than the backup
uv run python manage.py serve
```

3. Log in to test with the **same usernames and passwords as the backup**, and check a few audits,
   photos and the activity log. The old test database is kept as `data\test.db.before-restore-<time>`.

`restore_backup` refuses to run on production unless you add `--force`.

## Production

Set up once by following `docs/deploy.md` (secret key, web address, HTTPS). Day to day:

```powershell
$env:DJANGO_ENV="production"
uv run python manage.py migrate                   # after every app update
uv run python manage.py collectstatic --no-input  # after every app update
uv run python manage.py serve                     # usually runs as a Windows service instead
```

`seed_demo` refuses to run on production, so demo data can never end up in live data.

## Automatic tests

```powershell
uv run pytest                    # all tests (about 1 minute)
uv run pytest tests\test_acceptance.py   # only the 10 pilot acceptance checks
```

`DJANGO_ENV` doesn't matter here: pytest always uses its own throw-away copy.

## After changing page designs (developers)

The page styles are one ready-made file, `static/css/tailwind.css`. After adding or changing
classes in templates, JS or Python, rebuild it:

```powershell
.\tools\build_css.ps1
```

The first time, download the Tailwind tool (about 130 MB, not stored in git):

```powershell
curl.exe -fL -o tools\tailwindcss.exe https://github.com/tailwindlabs/tailwindcss/releases/download/v4.1.11/tailwindcss-windows-x64.exe
```
