# Putting Stock Audit on PythonAnywhere

> **Not yet tried live.** Steps follow PythonAnywhere's own Django guide. Tick them off as you go.

Your site will be: **https://tikitar.pythonanywhere.com**
Code comes from: **https://github.com/saurabhtiki/Stock-Audit**

PythonAnywhere runs the app for you (no Caddy, no Waitress, no service to set up) and gives
HTTPS for free on `*.pythonanywhere.com`.

**Free plan limits to know:** 512 MB disk (photos/videos fill this fast; the app allows 100 MB
videos), the site must be "extended" once every 3 months (one click on the **Web** tab), and
outside internet is limited to a whitelist (PyPI and GitHub are on it, so the steps below work).

---

## 1. Get the code and install packages (Bash console)

On PythonAnywhere: **Consoles → Bash**. Then:

```bash
cd ~
git clone https://github.com/saurabhtiki/Stock-Audit.git
cd Stock-Audit
pip install --user uv
python3.13 -m venv .venv            # app needs Python 3.12 or newer
source .venv/bin/activate
uv sync --no-dev --active
```

If `python3.13` is "not found", use `python3.12`. If the GitHub repo is **private**, `git clone`
asks for a login: use your GitHub username and a **Personal access token** (GitHub → Settings →
Developer settings → Tokens) as the password.

## 2. Settings file `.env.production`

```bash
cp .env.production.example .env.production
python -c "import secrets; print(secrets.token_urlsafe(50))"
nano .env.production
```

Fill it like this (paste your own long key):

```
SECRET_KEY=<the long random text printed above>
ALLOWED_HOSTS=tikitar.pythonanywhere.com
CSRF_TRUSTED_ORIGINS=https://tikitar.pythonanywhere.com
TIME_ZONE=Asia/Kolkata
LOG_LEVEL=INFO
SECURE_SSL_REDIRECT=false
```

`SECURE_SSL_REDIRECT=false` because PythonAnywhere itself forces HTTPS (step 4, "Force HTTPS").
Save in nano: `Ctrl+O`, `Enter`, `Ctrl+X`.

## 3. Database, CSS/JS, first Admin

```bash
export DJANGO_ENV=production
python manage.py migrate
python manage.py collectstatic --no-input
python manage.py create_admin
python manage.py check --deploy
```

## 4. Create the web app (Web tab)

1. **Web → Add a new web app → Next → Manual configuration** (not "Django") → pick the
   **same Python version** as in step 1 (3.13 or 3.12).
2. On the Web page that opens, set:
   - **Source code:** `/home/tikitar/Stock-Audit`
   - **Working directory:** `/home/tikitar/Stock-Audit`
   - **Virtualenv:** `/home/tikitar/Stock-Audit/.venv`
3. **Static files** → add one row:
   | URL | Directory |
   |---|---|
   | `/static/` | `/home/tikitar/Stock-Audit/staticfiles` |

   Do **not** add a row for `/media/`. Media (photos, attachments) must go through the app so
   the login and Branch checks apply.
4. **Security → Force HTTPS:** turn **on**.
5. Click the **WSGI configuration file** link (`/var/www/tikitar_pythonanywhere_com_wsgi.py`),
   delete everything in it, and paste:

   ```python
   import os
   import sys

   project_dir = "/home/tikitar/Stock-Audit"
   if project_dir not in sys.path:
       sys.path.insert(0, project_dir)
   os.chdir(project_dir)

   os.environ["DJANGO_ENV"] = "production"
   os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.production"

   from django.core.wsgi import get_wsgi_application

   application = get_wsgi_application()
   ```
   **Save**.
6. Back on **Web**, click the green **Reload** button.
7. Open <https://tikitar.pythonanywhere.com/health/>. It should show `{"status": "ok"}`.
   Then open the home page and log in with the Admin from step 3.

## 5. Updating the app later

Push your changes to GitHub from your PC first, then in a PythonAnywhere Bash console:

```bash
cd ~/Stock-Audit
source .venv/bin/activate
export DJANGO_ENV=production
git pull
uv sync --no-dev --active
python manage.py migrate
python manage.py collectstatic --no-input
```
Then **Web → Reload**. Download a backup (**Admin → Backup**) **before** every update.

`.env.production`, `data/` and `media/` are not in git, so `git pull` never touches them.

---

## Something wrong?

Look at the **Error log** link on the Web tab first (newest lines at the bottom).

| What you see | Likely cause |
|---|---|
| "Something went wrong :-(" page | Read the error log. Usually a typo in the WSGI file or wrong virtualenv path. |
| "Bad Request (400)" | `ALLOWED_HOSTS` in `.env.production` isn't `tikitar.pythonanywhere.com`. |
| "CSRF verification failed" on login | `CSRF_TRUSTED_ORIGINS` missing `https://`. |
| "SECRET_KEY is missing or too short" | `.env.production` not found or key under 32 characters. |
| Page without styles | Run `collectstatic --no-input`, check the `/static/` row, Reload. |
| "Too many redirects" | Set `SECURE_SSL_REDIRECT=false` and use **Force HTTPS** on the Web tab instead. |
| `disk I/O error` or `database is locked` in the log | SQLite's WAL mode may not like PythonAnywhere's shared disk. Ask for a small settings switch to turn WAL off. |
| `ModuleNotFoundError` | The Web tab's virtualenv path is wrong, or `uv sync` was not run. |
| "Disk quota exceeded" | Free plan 512 MB is full: delete old attachments/backups or upgrade. |
