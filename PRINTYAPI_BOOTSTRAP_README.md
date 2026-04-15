# Printy API bootstrap

This script automates most of the Ubuntu droplet setup for your Django backend.

## Quick start on a fresh droplet

```bash
# Option A: download directly from GitHub (no need to clone first)
curl -O https://raw.githubusercontent.com/WillieIlus/printy_api/main/printyapi_bootstrap.sh
chmod +x printyapi_bootstrap.sh
./printyapi_bootstrap.sh

# Option B: after cloning the repo
chmod +x printyapi_bootstrap.sh
./printyapi_bootstrap.sh
```

Run as your normal sudo-capable user, **not root**.

## What it asks for

- Git repo URL (default: `https://github.com/WillieIlus/printy_api.git`)
- Server public IP
- API domain (default: `api.printy.ke`)
- PostgreSQL DB name / user / password
- Frontend origin for CORS (default: `https://printy.ke`)

## What it creates

- Project folder: `/home/<your-user>/apps/printy_api`
- Virtualenv: `/home/<your-user>/apps/printy_api/env`
- Gunicorn socket: `/home/<your-user>/apps/printy_api/gunicorn.sock`
- Systemd service: `/etc/systemd/system/gunicorn.service`
- Nginx site: `/etc/nginx/sites-available/printy_api`
- `.env` file with generated `SECRET_KEY`

## Notes

- SSL redirect is OFF in the generated `.env` so you can test over HTTP first using the droplet IP.
- After `certbot` issues an SSL cert and HTTPS works, flip in `.env`:
  ```
  SECURE_SSL_REDIRECT=True
  SESSION_COOKIE_SECURE=True
  CSRF_COOKIE_SECURE=True
  ```
  Then `sudo systemctl restart gunicorn`.
- It does not create the DigitalOcean droplet itself.
- For a full step-by-step walkthrough see `printy_api_deployment_guide.md`.
