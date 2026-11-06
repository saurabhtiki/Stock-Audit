# Putting Stock Audit on a real server

> **Not yet tried live.** The app side (`manage.py serve`, `check --deploy`, `collectstatic`) was
> tested on a development PC. The Windows-service, Caddy and cloud steps below follow the tools'
> official instructions but have not been run end to end yet. Tick them off in
> `docs/pilot-checklist.md` as you go.

## How it fits together

```
 phone / PC browser ──HTTPS──▶  Caddy (port 443)  ──▶  Waitress: manage.py serve (127.0.0.1:8000)  ──▶  SQLite + media folder
```

- **Waitress** runs the app. It's started with `uv run python manage.py serve`, the same command
  on Windows and Linux.
- **Caddy** sits in front. It handles HTTPS (the padlock) and passes each request to Waitress.
  It renews certificates by itself.
- Data stays in the project folder: `data/production.db` and `media/production/`.
  Back it up with **Admin → Backup** (see the end of this guide).

Pick **one** of the two paths:
- **A. Office Windows PC / server**: data stays in the office.
- **B. Cloud VPS** (DigitalOcean droplet, Hostinger **VPS**, etc.): reachable from anywhere.
  Hostinger's cheap "shared web hosting" plans **cannot** run this app. You need a VPS plan.
  1 CPU / 1–2 GB RAM / 25 GB disk is enough for the pilot.

---

## A. Office Windows PC or server

### A1. Install the tools (once)

1. **uv** (runs Python and the app's packages). Open PowerShell and run:
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
   Close and reopen PowerShell, then check: `uv --version`.
2. **Caddy**: download the Windows amd64 `caddy.exe` from <https://caddyserver.com/download>,
   and put it in `C:\caddy\caddy.exe`.
3. **NSSM** (turns a command into a Windows service that starts after a reboot): download from
   <https://nssm.cc/download>, and copy `win64\nssm.exe` to `C:\nssm\nssm.exe`.

### A2. Copy the app and set it up

1. Copy the project folder to the server, e.g. `C:\StockAudit` (zip it on your PC without the
   `.venv`, `data`, `media` and `staticfiles` folders, then unzip on the server, or `git clone` if
   the code is on GitHub). **Don't** put it in a OneDrive / Dropbox folder: syncing a live
   database can damage it.
2. In PowerShell:
   ```powershell
   cd C:\StockAudit
   uv sync --no-dev
   copy .env.production.example .env.production
   uv run python -c "import secrets; print(secrets.token_urlsafe(50))"
   notepad .env.production
   ```
   In Notepad, fill in:
   - `SECRET_KEY=` the long random text printed above (keep it secret, never share it)
   - `ALLOWED_HOSTS=` the address people will type, e.g. `audit.yourco.com` or, office only,
     `stockaudit.local,192.168.1.50`
   - `CSRF_TRUSTED_ORIGINS=` the same with `https://` in front, e.g. `https://audit.yourco.com`
   - `TIME_ZONE=Asia/Kolkata`
3. Create the database, collect CSS/JS, make the first Admin:
   ```powershell
   $env:DJANGO_ENV="production"
   uv run python manage.py migrate
   uv run python manage.py collectstatic --no-input
   uv run python manage.py create_admin
   uv run python manage.py check --deploy      # should say "no issues"
   ```

### A3. Run the app as a Windows service

```powershell
C:\nssm\nssm.exe install StockAudit "$env:USERPROFILE\.local\bin\uv.exe" "run --no-dev python manage.py serve --port 8000"
C:\nssm\nssm.exe set StockAudit AppDirectory C:\StockAudit
C:\nssm\nssm.exe set StockAudit AppEnvironmentExtra DJANGO_ENV=production
C:\nssm\nssm.exe set StockAudit AppStdout C:\StockAudit\data\app.log
C:\nssm\nssm.exe set StockAudit AppStderr C:\StockAudit\data\app.log
C:\nssm\nssm.exe start StockAudit
```

(If `uv.exe` is somewhere else, find it with `(Get-Command uv).Source`.)
Check: open `http://127.0.0.1:8000/health/` on the server. It should show `{"status": "ok"}`.

### A4. HTTPS with Caddy

Create `C:\caddy\Caddyfile` (no file extension) with **one** of these:

**If you have a web address** (e.g. `audit.yourco.com` pointing to the office's public IP, with
ports 80 and 443 forwarded to this PC by the router). Caddy gets a free certificate automatically:
```
audit.yourco.com {
    reverse_proxy 127.0.0.1:8000
}
```

**Office network only** (no web address). Caddy makes its own certificate:
```
stockaudit.local, 192.168.1.50 {
    tls internal
    reverse_proxy 127.0.0.1:8000
}
```
With `tls internal`, each PC/phone must trust Caddy's certificate once, otherwise browsers show a
warning. On the server run `C:\caddy\caddy.exe trust`, then copy
`%AppData%\Caddy\pki\authorities\local\root.crt` to the other PCs and install it as a
"Trusted Root Certification Authority" (double-click → Install Certificate → Local Machine).
For `stockaudit.local`, also add a line `192.168.1.50 stockaudit.local` to the office DNS / router,
or to each PC's hosts file.

Run Caddy as a service too:
```powershell
C:\nssm\nssm.exe install Caddy C:\caddy\caddy.exe "run --config C:\caddy\Caddyfile"
C:\nssm\nssm.exe set Caddy AppDirectory C:\caddy
C:\nssm\nssm.exe start Caddy
```
Allow HTTPS through Windows Firewall (port 8000 stays closed to other PCs):
```powershell
New-NetFirewallRule -DisplayName "Stock Audit HTTPS" -Direction Inbound -Protocol TCP -LocalPort 80,443 -Action Allow
```
Open `https://audit.yourco.com` (or `https://stockaudit.local`) from another PC and log in.

### A5. Updating the app later (Windows)

```powershell
C:\nssm\nssm.exe stop StockAudit
# copy the new code over C:\StockAudit (keep .env.production, data\ and media\)
cd C:\StockAudit
$env:DJANGO_ENV="production"
uv sync --no-dev
uv run python manage.py migrate
uv run python manage.py collectstatic --no-input
C:\nssm\nssm.exe start StockAudit
```
Download a backup **before** every update.

---

## B. Cloud VPS (Ubuntu 24.04: DigitalOcean, Hostinger VPS, …)

You need: the server's IP, a web address (e.g. `audit.yourco.com`) with an **A record** pointing to
that IP (set at your domain provider), and an SSH login (`ssh root@<server IP>`).

### B1. Server basics (once)

```bash
apt update && apt upgrade -y
adduser --disabled-password --gecos "" stockaudit
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
timedatectl set-timezone Asia/Kolkata
```

### B2. Install Caddy (official package)

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

### B3. Copy the app and set it up (as the `stockaudit` user)

```bash
su - stockaudit
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env
# copy the code to /home/stockaudit/app (git clone, or scp a zip and unzip it)
cd ~/app
uv sync --no-dev
cp .env.production.example .env.production
uv run python -c "import secrets; print(secrets.token_urlsafe(50))"
nano .env.production        # SECRET_KEY, ALLOWED_HOSTS=audit.yourco.com, CSRF_TRUSTED_ORIGINS=https://audit.yourco.com
export DJANGO_ENV=production
uv run python manage.py migrate
uv run python manage.py collectstatic --no-input
uv run python manage.py create_admin
uv run python manage.py check --deploy
exit
```

### B4. Run the app as a service (systemd)

As root, create `/etc/systemd/system/stockaudit.service`:
```ini
[Unit]
Description=Stock Audit (Waitress)
After=network.target

[Service]
User=stockaudit
WorkingDirectory=/home/stockaudit/app
Environment=DJANGO_ENV=production
ExecStart=/home/stockaudit/.local/bin/uv run --no-dev python manage.py serve --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```
Then:
```bash
systemctl daemon-reload
systemctl enable --now stockaudit
curl http://127.0.0.1:8000/health/        # {"status": "ok"}
```
Logs: `journalctl -u stockaudit -f`.

### B5. HTTPS with Caddy

Replace `/etc/caddy/Caddyfile` with:
```
audit.yourco.com {
    reverse_proxy 127.0.0.1:8000
}
```
```bash
systemctl reload caddy
```
Caddy gets the free certificate within a minute. Open `https://audit.yourco.com` and log in.

### B6. Updating the app later (VPS)

```bash
su - stockaudit
cd ~/app
# copy / git pull the new code (keep .env.production, data/ and media/)
export DJANGO_ENV=production
uv sync --no-dev && uv run python manage.py migrate && uv run python manage.py collectstatic --no-input
exit
systemctl restart stockaudit
```

---

## Backups (both paths)

- **Admin → Backup → Database + media** downloads a zip. The app reminds Admins after 7 days.
- Keep copies **off the server** (your PC, a USB disk, cloud drive). A backup on the same machine
  is lost with the machine.
- Test a restore now and then into the **test** environment (`docs/environments.md`).

## Something wrong?

| What you see | Likely cause |
|---|---|
| "Bad Request (400)" | The address you typed isn't in `ALLOWED_HOSTS`. |
| "CSRF verification failed" on login | `CSRF_TRUSTED_ORIGINS` missing or without `https://`. |
| Page without styles / `serve` says "CSS / JS files are not collected yet" | Run `collectstatic --no-input`, then restart the service. |
| Endless redirect loop | Something other than Caddy is in front and doesn't send `X-Forwarded-Proto`; or set `SECURE_SSL_REDIRECT=false` only if that proxy forces HTTPS itself. |
| "SECRET_KEY is missing or too short" | `.env.production` missing or the key is under 32 characters. |
