# Printy API — Clean DigitalOcean Deployment Guide
> Django + PostgreSQL + Gunicorn + Nginx on Ubuntu LTS  
> Root-only deployment under `/root/apps/printy_api`

---

## FIXED PATHS — NEVER DEVIATE

| Item | Path |
|---|---|
| Project root | `/root/apps/printy_api` |
| Virtualenv | `/root/apps/printy_api/env` |
| Gunicorn socket | `/root/apps/printy_api/gunicorn.sock` |
| Static files | `/root/apps/printy_api/staticfiles` |
| Media files | `/root/apps/printy_api/media` |
| .env file | `/root/apps/printy_api/.env` |
| Systemd service | `/etc/systemd/system/gunicorn.service` |
| Nginx site config | `/etc/nginx/sites-available/printy_api` |
| Nginx enabled symlink | `/etc/nginx/sites-enabled/printy_api` |

---

## PHASE 1 — Create the Droplet

### DigitalOcean UI choices (exact)

1. Go to **Droplets → Create Droplet**
2. **Region:** Choose **Johannesburg** (`fra1` is next best; Johannesburg has the lowest latency to Nairobi)
3. **OS:** Ubuntu 24.04 LTS (x64)
4. **Plan:** Basic → **Regular** → **$6/mo** (1 vCPU, 1 GB RAM, 25 GB SSD) — sufficient for initial deployment
5. **Authentication:** Password → set a strong root password and save it
6. **Hostname:** `printy-api` (or anything — this is cosmetic)
7. **IPv6:** Enable
8. **Monitoring:** Enable (free, useful)
9. Click **Create Droplet**

### Get the public IP

Once created, copy the **IPv4 address** shown on the droplet card. This is `YOUR_DROPLET_IP`. Save it. You'll use it everywhere below.

### Open the web console (if SSH isn't set up yet)

Droplet page → **Access** tab → **Launch Droplet Console** → log in as `root` with your password.

---

## PHASE 2 — First Login and Base Setup

### Login

```bash
ssh root@YOUR_DROPLET_IP
```

Accept the host key fingerprint if prompted.

### Update the system

```bash
apt update && apt upgrade -y
```

This may take 2–5 minutes. Let it finish completely.

### Set the timezone

```bash
timedatectl set-timezone Africa/Nairobi
```

### Install all system dependencies in one shot

```bash
apt install -y \
  git curl wget build-essential \
  software-properties-common \
  libpq-dev \
  postgresql postgresql-contrib \
  nginx \
  ufw \
  python3-venv python3-pip python3-dev
```

### Install Python 3.13

Ubuntu 24.04 ships with Python 3.12. Add the deadsnakes PPA for 3.13:

```bash
add-apt-repository ppa:deadsnakes/ppa -y
apt update
apt install -y python3.13 python3.13-venv python3.13-dev
```

### Verify Python 3.13

```bash
python3.13 --version
```

Expected output: `Python 3.13.x`

---

### CHECKPOINT — Phase 2

```bash
python3.13 --version && psql --version && nginx -v && git --version
```

All four commands should return version strings.

**Recovery — python3.13 not found:**
```bash
add-apt-repository ppa:deadsnakes/ppa -y
apt update
apt install -y python3.13 python3.13-venv python3.13-dev
which python3.13
```

---

## PHASE 3 — Project Checkout and Python Environment

### Create the apps directory

```bash
mkdir -p /root/apps
cd /root/apps
```

### Clone the repo

```bash
git clone https://github.com/WillieIlus/printy_api.git
```

Verify:

```bash
ls /root/apps/printy_api
```

You should see your Django project files (`manage.py`, `config/`, `requirements.txt`, etc.).

### Create the virtualenv using Python 3.13 exactly

```bash
python3.13 -m venv /root/apps/printy_api/env
```

### Activate it

```bash
source /root/apps/printy_api/env/bin/activate
```

Your prompt should change to show `(env)`.

### Verify you are using the correct Python and pip

```bash
which python
which pip
python --version
```

Expected:
```
/root/apps/printy_api/env/bin/python
/root/apps/printy_api/env/bin/pip
Python 3.13.x
```

### Install requirements

```bash
cd /root/apps/printy_api
pip install --upgrade pip
pip install -r requirements.txt
```

> `requirements.txt` already includes `gunicorn` and `psycopg2-binary`. No extra installs needed.

---

### CHECKPOINT — Phase 3

```bash
source /root/apps/printy_api/env/bin/activate
python -c "import django; print(django.__version__)"
python -c "import gunicorn; print(gunicorn.__version__)"
python -c "import psycopg2; print(psycopg2.__version__)"
```

All three should print version numbers.

---

## PHASE 4 — PostgreSQL Setup

### Open the postgres shell

```bash
sudo -u postgres psql
```

### Run these SQL commands exactly

```sql
CREATE USER printy_user WITH PASSWORD 'choose_a_strong_password_here';
CREATE DATABASE printy_db OWNER printy_user;
GRANT ALL PRIVILEGES ON DATABASE printy_db TO printy_user;
\c printy_db
GRANT ALL ON SCHEMA public TO printy_user;
ALTER SCHEMA public OWNER TO printy_user;
\q
```

**What each line does:**
- `CREATE USER` — creates the DB user with a password
- `CREATE DATABASE ... OWNER` — creates the DB owned by that user
- `GRANT ALL PRIVILEGES ON DATABASE` — grants full DB-level access
- `\c printy_db` — connects into the new DB
- `GRANT ALL ON SCHEMA public` — lets the user create tables (required on Postgres 15+)
- `ALTER SCHEMA public OWNER` — fixes "permission denied for schema public" on PG15+

### Verify the connection works

```bash
psql -U printy_user -d printy_db -h 127.0.0.1 -W
```

Enter your password. You should get a `printy_db=>` prompt. Type `\q` to exit.

---

### CHECKPOINT — Phase 4

```bash
psql -U printy_user -d printy_db -h 127.0.0.1 -c "SELECT current_user, current_database();"
```

Expected output:
```
 current_user | current_database
--------------+-----------------
 printy_user  | printy_db
```

**Recovery — permission denied for schema public:**
```bash
sudo -u postgres psql -d printy_db
```
```sql
GRANT ALL ON SCHEMA public TO printy_user;
ALTER SCHEMA public OWNER TO printy_user;
\q
```

---

## PHASE 5 — .env and Django Setup

### Create the .env file

```bash
cat > /root/apps/printy_api/.env << 'EOF'
# Django core
SECRET_KEY=replace-this-with-a-real-50-char-secret-key
DEBUG=False
ALLOWED_HOSTS=YOUR_DROPLET_IP,api.printy.ke,localhost,127.0.0.1

# CORS / CSRF
CORS_ALLOWED_ORIGINS=https://printy.ke
CSRF_TRUSTED_ORIGINS=http://YOUR_DROPLET_IP,https://api.printy.ke,https://printy.ke

# Database
DB_NAME=printy_db
DB_USER=printy_user
DB_PASSWORD=choose_a_strong_password_here
DB_HOST=127.0.0.1
DB_PORT=5432

# Security — KEEP THESE False until SSL is working
SECURE_SSL_REDIRECT=False
SESSION_COOKIE_SECURE=False
CSRF_COOKIE_SECURE=False
EOF
```

**Replace `YOUR_DROPLET_IP` with your actual IP**, e.g. `165.22.46.12`.

**Generate a real SECRET_KEY:**
```bash
source /root/apps/printy_api/env/bin/activate
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Copy the output and paste it as the `SECRET_KEY` value.

### Secure the .env file

```bash
chmod 600 /root/apps/printy_api/.env
```

### Verify the .env was written correctly

```bash
cat /root/apps/printy_api/.env
```

### Why the security flags matter

`SECURE_SSL_REDIRECT=False` keeps Django from redirecting all HTTP → HTTPS during initial setup.
Once SSL is confirmed working (Phase 11), flip them all to `True`.

| Setting | Value now | Change to after SSL |
|---|---|---|
| `SECURE_SSL_REDIRECT` | `False` | `True` |
| `SESSION_COOKIE_SECURE` | `False` | `True` |
| `CSRF_COOKIE_SECURE` | `False` | `True` |

### Run Django management commands

```bash
cd /root/apps/printy_api
source env/bin/activate

python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

---

### CHECKPOINT — Phase 5

```bash
cd /root/apps/printy_api
source env/bin/activate
python manage.py check --deploy 2>&1 | head -20
ls staticfiles/
```

`manage.py check --deploy` will warn about SSL settings — that is expected and intentional at this stage. Confirm that `staticfiles/` exists and is not empty.

---

## PHASE 6 — Manual Gunicorn Test

### Kill any old gunicorn processes first

```bash
pkill -f gunicorn || true
sleep 2
```

### Check nothing is on port 8000

```bash
ss -tlnp | grep 8000
```

If anything shows up, kill it:

```bash
fuser -k 8000/tcp
```

### Run gunicorn manually bound to all interfaces

```bash
cd /root/apps/printy_api
source env/bin/activate
gunicorn --bind 0.0.0.0:8000 --workers 2 config.wsgi:application
```

**Leave this running in this terminal.**

### Test from inside the droplet (second terminal or after Ctrl+C)

```bash
curl -I http://127.0.0.1:8000/
curl -I http://127.0.0.1:8000/admin/
```

You should get `HTTP/1.1 200 OK` or a redirect — NOT a connection refused.

### Test from your browser

Open: `http://YOUR_DROPLET_IP:8000/admin/`

**CRITICAL:**
- `127.0.0.1` = loopback, only reachable from inside the droplet
- `YOUR_DROPLET_IP` = public IP, reachable from your laptop browser
- Never test from a browser using `127.0.0.1` — you are on a different machine

### Stop gunicorn

Press `Ctrl+C` then verify:
```bash
pkill -f gunicorn || true
ss -tlnp | grep 8000
```

The second command should return nothing.

---

### CHECKPOINT — Phase 6

```bash
ss -tlnp | grep 8000
ps aux | grep gunicorn | grep -v grep
```

Both should return empty.

**Recovery — address already in use:**
```bash
pkill -f gunicorn || true
fuser -k 8000/tcp
sleep 3
ss -tlnp | grep 8000
```

---

## PHASE 7 — Gunicorn Systemd Service

### Create the service file

```bash
cat > /etc/systemd/system/gunicorn.service << 'EOF'
[Unit]
Description=Gunicorn daemon for Printy API
After=network.target

[Service]
Type=notify
User=root
Group=root
WorkingDirectory=/root/apps/printy_api
ExecStart=/root/apps/printy_api/env/bin/gunicorn \
    --access-logfile - \
    --workers 3 \
    --bind unix:/root/apps/printy_api/gunicorn.sock \
    config.wsgi:application
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
TimeoutStopSec=5
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
```

### Enable and start the service

```bash
systemctl daemon-reload
systemctl start gunicorn
systemctl enable gunicorn
systemctl status gunicorn
```

Expected status output should show `Active: active (running)`.

### Verify the socket was created

```bash
ls -la /root/apps/printy_api/gunicorn.sock
```

### View logs if status shows failed

```bash
journalctl -u gunicorn -n 50 --no-pager
```

---

### CHECKPOINT — Phase 7

```bash
systemctl is-active gunicorn
ls /root/apps/printy_api/gunicorn.sock
```

Both should return `active` and show the socket file path.

**Recovery — service failed to start:**
```bash
journalctl -u gunicorn -n 100 --no-pager
# Look for: ModuleNotFoundError, ImportError, address already in use, permission denied
```

**Recovery — socket permission denied (nginx can't read it):**
```bash
chmod 755 /root
chmod 755 /root/apps
chmod 755 /root/apps/printy_api
systemctl restart gunicorn
systemctl restart nginx
```

---

## PHASE 8 — Nginx Configuration

### Remove the default nginx site

```bash
rm -f /etc/nginx/sites-enabled/default
ls /etc/nginx/sites-enabled/
```

The directory should now be empty.

### Create the Nginx site config

```bash
cat > /etc/nginx/sites-available/printy_api << 'EOF'
server {
    listen 80;
    server_name YOUR_DROPLET_IP api.printy.ke;

    client_max_body_size 20M;

    location = /favicon.ico { access_log off; log_not_found off; }

    location /static/ {
        alias /root/apps/printy_api/staticfiles/;
    }

    location /media/ {
        alias /root/apps/printy_api/media/;
    }

    location / {
        include proxy_params;
        proxy_pass http://unix:/root/apps/printy_api/gunicorn.sock;
    }
}
EOF
```

**Replace `YOUR_DROPLET_IP` with the actual IP.**

### Enable the site

```bash
ln -s /etc/nginx/sites-available/printy_api /etc/nginx/sites-enabled/printy_api
```

### Test nginx syntax

```bash
nginx -t
```

Expected:
```
nginx: the configuration file /etc/nginx/nginx.conf syntax is ok
nginx: configuration file /etc/nginx/nginx.conf test is successful
```

### Restart nginx

```bash
systemctl restart nginx
systemctl status nginx
```

---

### CHECKPOINT — Phase 8

```bash
nginx -t && curl -I http://127.0.0.1/
```

`nginx -t` should pass. `curl` should return a Django response (200, 301, or 302), NOT 502.

**Recovery — 502 Bad Gateway:**
```bash
systemctl status gunicorn
ls /root/apps/printy_api/gunicorn.sock
# If socket missing:
systemctl restart gunicorn
```

---

## PHASE 9 — Firewall and Network Testing

### Configure UFW

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
ufw status verbose
```

### Test from inside the droplet

```bash
curl -I http://127.0.0.1/
curl -I http://127.0.0.1/admin/
```

### Test from your laptop browser

```
http://YOUR_DROPLET_IP/admin/
```

You should see the Django admin login page.

---

### CHECKPOINT — Phase 9

```bash
ufw status verbose
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1/admin/
```

UFW should show OpenSSH and Nginx Full allowed. curl should return `200` or `301`.

---

## PHASE 10 — Final Health Checks

```bash
echo "=== FAILED SERVICES ===" && systemctl --failed
echo ""
echo "=== GUNICORN STATUS ===" && systemctl status gunicorn --no-pager -l
echo ""
echo "=== NGINX STATUS ===" && systemctl status nginx --no-pager -l
echo ""
echo "=== GUNICORN JOURNAL (last 30 lines) ===" && journalctl -u gunicorn -n 30 --no-pager
echo ""
echo "=== NGINX ERROR LOG ===" && tail -20 /var/log/nginx/error.log
echo ""
echo "=== LISTENING PORTS ===" && ss -tlnp
echo ""
echo "=== GUNICORN SOCKET ===" && ls -la /root/apps/printy_api/gunicorn.sock
echo ""
echo "=== UFW STATUS ===" && ufw status verbose
```

| What you see | What it means |
|---|---|
| `gunicorn.service: active (running)` | Gunicorn is healthy |
| `0 loaded units listed` under FAILED SERVICES | No failed services |
| `srwxrwxrwx ... gunicorn.sock` | Socket exists |
| `:80` in LISTENING PORTS | Nginx listening for public traffic |
| `502 Bad Gateway` in nginx error log | Gunicorn is down or socket path mismatch |
| `connect() to unix:... failed (13: Permission denied)` | Nginx can't read socket (see Phase 7 recovery) |

---

## PHASE 11 — Domain and SSL

### Step 1: Point api.printy.ke to the droplet

Add an **A record** in your DNS provider (Cloudflare, etc.):
- Name: `api`
- Value: `YOUR_DROPLET_IP`
- TTL: 300
- Proxy: **Off** (DNS only) — until SSL is confirmed

Test:
```bash
dig api.printy.ke +short
```

Should return your droplet IP.

### Step 2: Install Certbot and get SSL

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d api.printy.ke
```

### Step 3: Verify auto-renewal

```bash
certbot renew --dry-run
```

### Step 4: Enable SSL security settings in .env

Once `https://api.printy.ke/admin/` loads correctly:

```bash
nano /root/apps/printy_api/.env
```

Change:
```ini
SECURE_SSL_REDIRECT=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
```

Then restart:
```bash
systemctl restart gunicorn
```

---

## FINAL FILES

### 1. `/etc/systemd/system/gunicorn.service`

```ini
[Unit]
Description=Gunicorn daemon for Printy API
After=network.target

[Service]
Type=notify
User=root
Group=root
WorkingDirectory=/root/apps/printy_api
ExecStart=/root/apps/printy_api/env/bin/gunicorn \
    --access-logfile - \
    --workers 3 \
    --bind unix:/root/apps/printy_api/gunicorn.sock \
    config.wsgi:application
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
TimeoutStopSec=5
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### 2. `/etc/nginx/sites-available/printy_api`

```nginx
server {
    listen 80;
    server_name YOUR_DROPLET_IP api.printy.ke;

    client_max_body_size 20M;

    location = /favicon.ico { access_log off; log_not_found off; }

    location /static/ {
        alias /root/apps/printy_api/staticfiles/;
    }

    location /media/ {
        alias /root/apps/printy_api/media/;
    }

    location / {
        include proxy_params;
        proxy_pass http://unix:/root/apps/printy_api/gunicorn.sock;
    }
}
```

### 3. `/root/apps/printy_api/.env` (initial IP-based testing)

```ini
# Django core
SECRET_KEY=your-generated-secret-key-here
DEBUG=False
ALLOWED_HOSTS=YOUR_DROPLET_IP,api.printy.ke,localhost,127.0.0.1

# CORS / CSRF
CORS_ALLOWED_ORIGINS=https://printy.ke
CSRF_TRUSTED_ORIGINS=http://YOUR_DROPLET_IP,https://api.printy.ke,https://printy.ke

# Database
DB_NAME=printy_db
DB_USER=printy_user
DB_PASSWORD=your_strong_db_password
DB_HOST=127.0.0.1
DB_PORT=5432

# Security — ALL False until SSL is working
SECURE_SSL_REDIRECT=False
SESSION_COOKIE_SECURE=False
CSRF_COOKIE_SECURE=False
```

---

## DO NOT MAKE THESE MISTAKES

- **Don't browse to `127.0.0.1` from your laptop.** That connects to your local machine, not the droplet. Always use the public droplet IP in your browser.
- **Don't leave old gunicorn processes running.** Before every test, run `pkill -f gunicorn || true && sleep 2`.
- **Don't mix socket paths.** The socket is ALWAYS `/root/apps/printy_api/gunicorn.sock`. This path must be identical in the systemd service AND the nginx config.
- **Don't leave the nginx default site active.** Always `rm -f /etc/nginx/sites-enabled/default` before enabling your own config.
- **Don't force HTTPS before SSL exists.** Keep `SECURE_SSL_REDIRECT=False` in `.env` until Certbot has issued a certificate.
- **Don't skip `systemctl daemon-reload`** after creating or modifying the service file.
- **Don't forget `GRANT ALL ON SCHEMA public`** on PostgreSQL 15+. Without it, migrations fail with "permission denied for schema public".
