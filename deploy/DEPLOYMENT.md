# Deploying Healthchecks on Your Own Linux Server

This guide deploys the full application (web UI + 24/7 probe/alert worker +
housekeeping) on a self-hosted, always-on Linux server using
**Gunicorn + Nginx + PostgreSQL + systemd**, with free HTTPS.

It uses the artifacts already in this `deploy/` folder:

| File | Purpose |
|---|---|
| `healthchecks-web.service` | systemd unit: Gunicorn web app (port 8001) |
| `healthchecks-worker.service` | systemd unit: `sendalerts` worker (probes, alerts, retries, watchdog) |
| `healthchecks-housekeeping.service` | systemd unit: nightly reports + pruning loop |
| `nginx.conf` | reverse proxy (HTTP -> Gunicorn; TLS block for certbot) |
| `backup.sh` | DB backups (auto-detects SQLite vs PostgreSQL) |
| `migrate_sqlite_to_postgres.sh` | one-time data cutover from a dev SQLite DB |

---

## 0. Prerequisites

- A Linux server that is up 24/7, with **root/sudo** access.
- **Ubuntu 24.04 / Debian 12+** recommended (needs **Python 3.12+** for Django 6.1).
- A **domain name** pointing at the server (for free Let's Encrypt HTTPS), **or**
  just the server's IP (internal/self-signed HTTPS — see Step 8, Option B).
- Outbound internet access from the server (it must reach the APIs you monitor
  and `pip`/`apt` mirrors).
- An SMTP account to send alert emails (e.g. Gmail App Password, Mailgun, SES).

> Replace these placeholders throughout:
> - `APP_USER` = the Linux user that runs the app (e.g. `healthchecks`)
> - `APP_DIR` = install directory (e.g. `/home/healthchecks/healthchecks`)
> - `hc.example.com` = your domain
> - `STRONG_DB_PASSWORD` = a strong Postgres password

---

## 1. Install system packages

Ubuntu/Debian:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git nginx postgresql certbot python3-certbot-nginx \
    python3-venv python3-dev build-essential pkg-config \
    libpq-dev libcurl4-openssl-dev libssl-dev
```

RHEL/Rocky/Alma:
```bash
sudo dnf install -y git nginx postgresql-server certbot python3-certbot-nginx \
    python3-devel gcc pkgconf-pkg-config libpq-devel libcurl-devel openssl-devel
sudo postgresql-setup --initdb && sudo systemctl enable --now postgresql
```

> `libpq-dev` builds `psycopg`; `libcurl4-openssl-dev` builds `pycurl`
> (both are in `requirements.txt`).

---

## 2. Create the app user, clone the code, build the virtualenv

```bash
sudo adduser --disabled-password --gecos "" healthchecks     # or use your user
sudo -u healthchecks bash -c '
  cd ~ &&
  git clone https://github.com/abdulhanan8527/healthchecks.git &&
  cd healthchecks &&
  python3 -m venv .venv &&
  .venv/bin/pip install --upgrade pip &&
  .venv/bin/pip install -r requirements.txt
'
```
So `APP_DIR=/home/healthchecks/healthchecks` and the venv is at
`APP_DIR/.venv`.

---

## 3. Create the PostgreSQL database

```bash
sudo -u postgres createuser hc
sudo -u postgres createdb -O hc hc
sudo -u postgres psql -c "ALTER USER hc WITH PASSWORD 'STRONG_DB_PASSWORD';"
```

---

## 4. Create the environment file `/etc/healthchecks/env`

```bash
sudo mkdir -p /etc/healthchecks
sudo nano /etc/healthchecks/env
```

```ini
# --- Core ---
SECRET_KEY=REPLACE_WITH_A_LONG_RANDOM_STRING
DEBUG=False
SITE_ROOT=https://hc.example.com
SITE_NAME=Mychecks
ALLOWED_HOSTS=hc.example.com
SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https
DEFAULT_FROM_EMAIL=alerts@example.com
ADMINS=you@example.com
REGISTRATION_OPEN=True

# --- Database (PostgreSQL) ---
DB=postgres
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=hc
DB_USER=hc
DB_PASSWORD=STRONG_DB_PASSWORD

# --- Email (SMTP) ---
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=you@gmail.com
EMAIL_HOST_PASSWORD=your-app-password

# --- Integrations you use ---
MATTERMOST_ENABLED=True
SLACK_ENABLED=True
```

Generate the secret key:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

Lock the file down (it contains secrets):
```bash
sudo chown root:healthchecks /etc/healthchecks/env
sudo chmod 640 /etc/healthchecks/env
```

> Full list of supported variables: `templates/docs/self_hosted_configuration.md`
> and `docker/.env.example`.

---

## 5. Initialize the app (schema, static files, admin user)

```bash
cd /home/healthchecks/healthchecks
sudo -u healthchecks bash -c '
  set -a; . /etc/healthchecks/env; set +a
  .venv/bin/python manage.py migrate
  .venv/bin/python manage.py collectstatic --noinput
  .venv/bin/python manage.py createsuperuser
'
```
> `healthchecks` needs read access to `/etc/healthchecks/env` (done in Step 4).

---

## 6. Install the systemd services

The `deploy/*.service` files reference the original author's paths/user. Point
them at your `APP_USER` and `APP_DIR`, and switch them to the env file:

```bash
cd /home/healthchecks/healthchecks
APP_USER=healthchecks
APP_DIR=/home/healthchecks/healthchecks

sed -i "s#User=muhammad-ans#User=${APP_USER}#" deploy/*.service
sed -i "s#WorkingDirectory=/home/muhammad-ans/webapps/healthchecks#WorkingDirectory=${APP_DIR}#" deploy/*.service
sed -i "s#/home/muhammad-ans/webapps/.venv#${APP_DIR}/.venv#g" deploy/*.service
# Use the env file, drop the hardcoded example.com placeholders:
sed -i 's#^# EnvironmentFile=/etc/healthchecks/env#EnvironmentFile=/etc/healthchecks/env#' deploy/*.service
sed -i '/^Environment="SITE_ROOT=/d; /^Environment="ALLOWED_HOSTS=/d; /^Environment="DEBUG=/d' deploy/*.service
```

Open each file and confirm: `User=healthchecks`, correct `WorkingDirectory`,
`EnvironmentFile=/etc/healthchecks/env` is **uncommented**, and `ExecStart`
points at `APP_DIR/.venv/bin/...`. Then install:

```bash
sudo cp deploy/healthchecks-web.service \
        deploy/healthchecks-worker.service \
        deploy/healthchecks-housekeeping.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now healthchecks-web healthchecks-worker healthchecks-housekeeping
systemctl status healthchecks-web --no-pager
```

Gunicorn now serves the app on `127.0.0.1:8001`.

---

## 7. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'     # opens 80 + 443
sudo ufw enable
```
(Or open TCP 80/443 with `firewalld`/cloud security groups as appropriate.)

---

## 8. Nginx reverse proxy + HTTPS

### Option A — You have a domain (recommended, free HTTPS)

```bash
cd /home/healthchecks/healthchecks
sudo cp deploy/nginx.conf /etc/nginx/sites-available/healthchecks
sudo sed -i 's/healthchecks.example.com/hc.example.com/g' /etc/nginx/sites-available/healthchecks
sudo ln -sf /etc/nginx/sites-available/healthchecks /etc/nginx/sites-enabled/healthchecks
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# Free certificate from Let's Encrypt (auto-edits nginx + auto-renews):
sudo certbot --nginx -d hc.example.com
```
Then uncomment the HTTP->HTTPS redirect in
`/etc/nginx/sites-available/healthchecks`:
```nginx
return 301 https://$host$request_uri;
```
and reload: `sudo nginx -t && sudo systemctl reload nginx`.

> RHEL-family: put the config in `/etc/nginx/conf.d/healthchecks.conf`
> (no `sites-available`/`sites-enabled`).

### Option B — IP address only / internal network (no domain)

You cannot get a Let's Encrypt cert for a bare IP. Either:
- serve plain HTTP on the internal network (edit `nginx.conf`: keep the
  `listen 80;` block, set `SITE_ROOT=http://<SERVER_IP>`, and remove
  `SECURE_PROXY_SSL_HEADER`), **or**
- generate a self-signed cert:
```bash
sudo openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout /etc/ssl/private/hc.key -out /etc/ssl/certs/hc.crt \
  -subj "/CN=<SERVER_IP>"
```
  and add an `listen 443 ssl;` server block referencing those files. Browsers
  will show a warning until you trust the certificate.

---

## 9. (Optional) Import data from an existing dev SQLite database

If you already have checks/history in a local `hc.sqlite`, copy it to the
server and load it into Postgres:

```bash
# On the machine that has hc.sqlite:
python manage.py dumpdata --natural-foreign --natural-primary \
  -e contenttypes -e auth.permission -e admin.logentry -e sessions.session \
  --indent 2 > hc-data.json
scp hc-data.json healthchecks@SERVER:/home/healthchecks/

# On the server (DB must be freshly migrated / empty of app rows):
cd /home/healthchecks/healthchecks
sudo -u healthchecks bash -c '
  set -a; . /etc/healthchecks/env; set +a
  .venv/bin/python manage.py loaddata /home/healthchecks/hc-data.json
'
sudo systemctl restart healthchecks-web healthchecks-worker
```

If your source DB is a live SQLite you want to cut over in one step, use
`deploy/migrate_sqlite_to_postgres.sh` (dumps, migrates, loads).

---

## 10. Automated backups

```bash
sudo cp deploy/backup.sh /usr/local/bin/hc-backup.sh
sudo chmod +x /usr/local/bin/hc-backup.sh
sudo mkdir -p /var/backups/healthchecks
sudo crontab -e
```
Add (daily at 03:30, keeps 14 days):
```cron
30 3 * * * set -a; . /etc/healthchecks/env; set +a; BACKUP_DIR=/var/backups/healthchecks /usr/local/bin/hc-backup.sh >> /var/log/hc-backup.log 2>&1
```
`backup.sh` detects Postgres and uses `pg_dump`. Test it once:
```bash
sudo BACKUP_DIR=/var/backups/healthchecks /usr/local/bin/hc-backup.sh
```

---

## 11. Watchdog ("monitor the monitor") + smoke tests

1. In the web UI, create a **passive** check (leave the API URL empty) and give
   it the tag `watchdog` (with spaces around it in the tag list). Set its period
   to ~3 minutes. The worker pings all `watchdog`-tagged passive checks once a
   minute; if the worker dies, the check times out and alerts you normally.
2. Assign an integration (email/Mattermost/Slack) to it.

Smoke tests:
```bash
curl -I https://hc.example.com/                          # 200 or 302
curl -s https://hc.example.com/api/v3/agent/checks/      # {"error":"missing api key"}
journalctl -u healthchecks-worker -n 30 --no-pager       # running, no tracebacks
```

---

## 12. Day-2 operations

View logs:
```bash
sudo journalctl -u healthchecks-web -f
sudo journalctl -u healthchecks-worker -f
sudo journalctl -u healthchecks-housekeeping -f
```

Restart:
```bash
sudo systemctl restart healthchecks-web healthchecks-worker healthchecks-housekeeping
```

Deploy a code update:
```bash
cd /home/healthchecks/healthchecks
sudo -u healthchecks bash -c '
  git pull &&
  .venv/bin/pip install -r requirements.txt &&
  set -a; . /etc/healthchecks/env; set +a &&
  .venv/bin/python manage.py migrate &&
  .venv/bin/python manage.py collectstatic --noinput
'
sudo systemctl restart healthchecks-web healthchecks-worker
```

> The `sendalerts` worker does **not** auto-reload code. Always restart
> `healthchecks-worker` after a deploy, or it keeps running old code.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `502 Bad Gateway` | Gunicorn not running: `systemctl status healthchecks-web`; check it binds `127.0.0.1:8001`. |
| Checks never probe | Worker down: `systemctl status healthchecks-worker`. Probing happens **only** in the worker. |
| `DisallowedHost` error | `ALLOWED_HOSTS` / `SITE_ROOT` don't match the host you used. Fix `/etc/healthchecks/env`. |
| No alert emails | SMTP settings wrong, or `DEFAULT_FROM_EMAIL` domain rejected. Check `EMAIL_*` and the worker log. |
| Static/CSS missing (DEBUG=False) | Run `manage.py collectstatic`; ensure WhiteNoise middleware is active (it is by default). |
| `psycopg`/`pycurl` build fails | Install `libpq-dev` + `libcurl4-openssl-dev` + `build-essential` (Step 1). |
| HTTPS redirect loop | `SECURE_PROXY_SSL_HEADER` set but nginx not sending `X-Forwarded-Proto`. The provided `nginx.conf` sends it. |
| Permission denied on env file | App user must read `/etc/healthchecks/env`: `chown root:APP_USER` + `chmod 640`. |

---

## Quick config reference

| Variable | Meaning |
|---|---|
| `SECRET_KEY` | Cryptographic signing key (unique, secret). |
| `DEBUG` | Must be `False` in production. |
| `SITE_ROOT` | Public base URL (scheme + host). Drives generated links. |
| `ALLOWED_HOSTS` | Comma-separated hostnames accepted. |
| `SECURE_PROXY_SSL_HEADER` | `HTTP_X_FORWARDED_PROTO,https` when behind nginx TLS. |
| `DB=postgres` + `DB_*` | Switch to PostgreSQL and its connection settings. |
| `EMAIL_*` | SMTP for alert/report emails. |
| `DEFAULT_FROM_EMAIL` / `ADMINS` | Sender address / error recipients. |
| `*_ENABLED` | Toggle individual integrations (Mattermost, Slack, ...). |

For the full feature/architecture reference, see `PROJECT_DOCUMENTATION.md`.
