#!/usr/bin/env bash
set -Eeuo pipefail

# Printy API bootstrap for a fresh Ubuntu droplet
# Run as a sudo-capable NON-root user, e.g.:
#   chmod +x printyapi_bootstrap.sh
#   ./printyapi_bootstrap.sh
#
# What it does:
# - installs system packages
# - installs Python 3.13 if available (falls back to deadsnakes PPA)
# - clones/pulls the repo
# - creates venv inside project
# - creates PostgreSQL DB/user
# - writes project .env from your answers
# - runs migrate / collectstatic / createsuperuser
# - creates gunicorn systemd service
# - creates nginx site config
# - enables ufw rules
#
# It does NOT create the DigitalOcean droplet itself.

trap 'echo; echo "Failed at line $LINENO. Scroll up for the exact command/error."; exit 1' ERR

if [[ $EUID -eq 0 ]]; then
  echo "Please run this script as your sudo-capable user, not as root."
  exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required."
  exit 1
fi

SUDO_USER_NAME="${USER}"
HOME_DIR="/home/${SUDO_USER_NAME}"
APP_BASE="${HOME_DIR}/apps"
PROJECT_NAME="printy_api"
PROJECT_DIR="${APP_BASE}/${PROJECT_NAME}"
VENV_DIR="${PROJECT_DIR}/env"
SOCK_PATH="${PROJECT_DIR}/gunicorn.sock"
SERVICE_NAME="gunicorn"
NGINX_SITE="printy_api"

echo "=== Printy API bootstrap ==="
read -rp "Git repo URL [https://github.com/WillieIlus/printy_api.git]: " REPO_URL
REPO_URL="${REPO_URL:-https://github.com/WillieIlus/printy_api.git}"

read -rp "Server public IP (example 206.81.x.x): " SERVER_IP
if [[ -z "${SERVER_IP}" ]]; then
  echo "Server public IP is required."
  exit 1
fi

read -rp "Domain for API [api.printy.ke]: " API_DOMAIN
API_DOMAIN="${API_DOMAIN:-api.printy.ke}"

read -rp "Postgres DB name [printy_db]: " DB_NAME
DB_NAME="${DB_NAME:-printy_db}"

read -rp "Postgres DB user [printy_user]: " DB_USER
DB_USER="${DB_USER:-printy_user}"

read -rsp "Postgres DB password: " DB_PASSWORD
echo
if [[ -z "${DB_PASSWORD}" ]]; then
  echo "Database password is required."
  exit 1
fi

read -rp "Frontend origin for CORS [https://printy.ke]: " FRONTEND_ORIGIN
FRONTEND_ORIGIN="${FRONTEND_ORIGIN:-https://printy.ke}"

read -rp "Run create superuser at end? [y/N]: " CREATE_SU
CREATE_SU="${CREATE_SU:-N}"

echo "=== 1) System packages ==="
sudo apt update
sudo apt upgrade -y
sudo apt install -y software-properties-common ca-certificates curl gnupg lsb-release \
  git nginx postgresql postgresql-contrib libpq-dev build-essential ufw

echo "=== 2) Python 3.13 ==="
if ! command -v python3.13 >/dev/null 2>&1; then
  sudo add-apt-repository -y ppa:deadsnakes/ppa || true
  sudo apt update
  sudo apt install -y python3.13 python3.13-venv python3.13-dev
fi
python3.13 --version

echo "=== 3) Clone or update repo ==="
mkdir -p "${APP_BASE}"
if [[ -d "${PROJECT_DIR}/.git" ]]; then
  git -C "${PROJECT_DIR}" pull --ff-only
else
  git clone "${REPO_URL}" "${PROJECT_DIR}"
fi

cd "${PROJECT_DIR}"

echo "=== 4) Virtualenv ==="
python3.13 -m venv "${VENV_DIR}"
source "${VENV_DIR}/bin/activate"
python --version
pip install --upgrade pip
pip install -r requirements.txt

echo "=== 5) PostgreSQL ==="
sudo systemctl enable --now postgresql

sudo -u postgres psql <<SQL
DO \$\$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '${DB_USER}') THEN
      CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASSWORD}';
   ELSE
      ALTER ROLE ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';
   END IF;
END
\$\$;
SQL

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"
fi

sudo -u postgres psql -d "${DB_NAME}" <<SQL
GRANT ALL ON SCHEMA public TO ${DB_USER};
ALTER SCHEMA public OWNER TO ${DB_USER};
SQL

echo "=== 6) Django .env ==="
SECRET_KEY="$(python - <<'PY'
from django.core.management.utils import get_random_secret_key
print(get_random_secret_key())
PY
)"

cat > "${PROJECT_DIR}/.env" <<ENVFILE
DEBUG=False
SECRET_KEY=${SECRET_KEY}
ALLOWED_HOSTS=${SERVER_IP},${API_DOMAIN},localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=http://${SERVER_IP},https://${API_DOMAIN},https://printy.ke
CORS_ALLOWED_ORIGINS=${FRONTEND_ORIGIN}
DB_NAME=${DB_NAME}
DB_USER=${DB_USER}
DB_PASSWORD=${DB_PASSWORD}
DB_HOST=localhost
DB_PORT=5432
SECURE_SSL_REDIRECT=False
SESSION_COOKIE_SECURE=False
CSRF_COOKIE_SECURE=False
ENVFILE

chmod 600 "${PROJECT_DIR}/.env"

echo "=== 7) Migrate / collectstatic ==="
python manage.py migrate
python manage.py collectstatic --noinput

if [[ "${CREATE_SU}" =~ ^[Yy]$ ]]; then
  echo "=== 8) Create superuser ==="
  python manage.py createsuperuser
fi

echo "=== 9) Gunicorn service ==="
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<SERVICE
[Unit]
Description=gunicorn daemon for ${PROJECT_NAME}
After=network.target

[Service]
User=${SUDO_USER_NAME}
Group=www-data
WorkingDirectory=${PROJECT_DIR}
Environment="PATH=${VENV_DIR}/bin"
ExecStart=${VENV_DIR}/bin/gunicorn --workers 2 --bind unix:${SOCK_PATH} --umask 007 config.wsgi:application

[Install]
WantedBy=multi-user.target
SERVICE

sudo chmod 755 "${HOME_DIR}" "${APP_BASE}" "${PROJECT_DIR}"

sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}"

echo "=== 10) Nginx ==="
sudo tee "/etc/nginx/sites-available/${NGINX_SITE}" > /dev/null <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name ${SERVER_IP} ${API_DOMAIN};

    client_max_body_size 20M;

    location = /favicon.ico { access_log off; log_not_found off; }

    location /static/ {
        alias ${PROJECT_DIR}/staticfiles/;
    }

    location /media/ {
        alias ${PROJECT_DIR}/media/;
    }

    location / {
        include proxy_params;
        proxy_pass http://unix:${SOCK_PATH};
    }
}
NGINX

sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf "/etc/nginx/sites-available/${NGINX_SITE}" "/etc/nginx/sites-enabled/${NGINX_SITE}"

sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl restart nginx

echo "=== 11) Firewall ==="
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw --force enable || true

echo
echo "=== Done ==="
echo "Useful checks:"
echo "  sudo systemctl status gunicorn --no-pager"
echo "  sudo systemctl status nginx --no-pager"
echo "  curl -I http://127.0.0.1/"
echo "  curl -I http://127.0.0.1/admin/"
echo "  Open in browser: http://${SERVER_IP}/admin/"
echo
echo "After your domain points to this IP, add SSL with:"
echo "  sudo apt install -y certbot python3-certbot-nginx"
echo "  sudo certbot --nginx -d ${API_DOMAIN}"
echo
echo "After SSL is confirmed working, update ${PROJECT_DIR}/.env:"
echo "  SECURE_SSL_REDIRECT=True"
echo "  SESSION_COOKIE_SECURE=True"
echo "  CSRF_COOKIE_SECURE=True"
echo "Then restart: sudo systemctl restart gunicorn"
