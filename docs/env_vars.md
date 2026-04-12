# Environment Variables - printy_api

All required and optional environment variables. Use a local `.env` file or deployment secret management.

---

## Core

| Variable | Required | Default | Description |
|----------|----------|---------|--------------|
| `SECRET_KEY` | Yes | (none) | Secret for signing; set this in local `.env` and deployment secrets |
| `DEBUG` | No | `false` | `true`/`1`/`yes` enables debug mode |
| `ALLOWED_HOSTS` | No | `localhost,127.0.0.1,testserver` | Comma-separated Django hosts |

---

## Database

| Variable | Required | Default | Description |
|----------|----------|---------|--------------|
| `DB_NAME` | Yes | `printy_db` | Database name |
| `DB_USER` | Yes | `printy_user` | Database user |
| `DB_PASSWORD` | Yes | (empty) | Database password |
| `DB_HOST` | No | `127.0.0.1` | Database host |
| `DB_PORT` | No | `5432` | Database port |
| `ENV_DEBUG` | No | `false` | When `true`, logs whether key env vars are present without printing secret values |

---

## Frontend, CORS, and CSRF

| Variable | Required | Default | Description |
|----------|----------|---------|--------------|
| `FRONTEND_URL` | No | `http://localhost:3000` | Frontend base URL for email links and redirects |
| `CORS_ALLOWED_ORIGINS` | No | local dev origins only | Comma-separated frontend origins to allow |
| `CSRF_TRUSTED_ORIGINS` | No | local dev origins only | Comma-separated trusted origins for Django forms/admin |

---

## JWT

| Variable | Required | Default | Description |
|----------|----------|---------|--------------|
| (uses `SECRET_KEY`) | - | - | JWT signing uses `SECRET_KEY` |
| Access token lifetime | - | 15 min | Set in `config/settings.py` |
| Refresh token lifetime | - | 30 days | Set in `config/settings.py` |

No extra env vars are required for JWT beyond `SECRET_KEY`.

---

## Email

| Variable | Required | Default | Description |
|----------|----------|---------|--------------|
| `DEFAULT_FROM_EMAIL` | No | `noreply@example.com` | From address for emails |
| `EMAIL_BACKEND` | No | `django.core.mail.backends.console.EmailBackend` | Override for SMTP in production |

---

## OAuth

| Variable | Required | Default | Description |
|----------|----------|---------|--------------|
| `GOOGLE_CLIENT_ID` | If Google login is enabled | (empty) | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | If Google login is enabled | (empty) | Google OAuth client secret |
| `GITHUB_CLIENT_ID` | If GitHub login is enabled | (empty) | GitHub OAuth client ID |
| `GITHUB_CLIENT_SECRET` | If GitHub login is enabled | (empty) | GitHub OAuth client secret |

---

## M-Pesa

| Variable | Required | Default | Description |
|----------|----------|---------|--------------|
| `MPESA_BASE_URL` | No | `https://sandbox.safaricom.co.ke` | Daraja API base URL |
| `MPESA_CONSUMER_KEY` | Yes when enabled | (empty) | Daraja API consumer key |
| `MPESA_CONSUMER_SECRET` | Yes when enabled | (empty) | Daraja API consumer secret |
| `MPESA_SHORTCODE` | Yes when enabled | (empty) | Paybill or till number |
| `MPESA_PASSKEY` | Yes when enabled | (empty) | Lipa Na M-Pesa passkey |
| `MPESA_STK_CALLBACK_URL` | Yes when enabled | `https://api.example.com/api/payments/mpesa/callback/` | Public HTTPS callback URL |
| `MPESA_INITIATOR_NAME` | If B2C is enabled | (empty) | B2C initiator name |
| `MPESA_SECURITY_CREDENTIAL` | If B2C is enabled | (empty) | B2C security credential |
| `MPESA_TIMEOUT_URL` | No | `https://api.example.com/api/mpesa/timeout/` | Timeout callback URL |
| `MPESA_RESULT_URL` | No | `https://api.example.com/api/mpesa/result/` | Result callback URL |

Callback URLs must be HTTPS and publicly reachable.

---

## Example `.env`

```env
SECRET_KEY=replace-with-a-unique-secret-key
DEBUG=false
ALLOWED_HOSTS=api.example.com

DB_NAME=replace-with-database-name
DB_USER=replace-with-database-user
DB_PASSWORD=replace-with-database-password
DB_HOST=127.0.0.1
DB_PORT=5432

FRONTEND_URL=https://www.example.com
CORS_ALLOWED_ORIGINS=https://www.example.com
CSRF_TRUSTED_ORIGINS=https://www.example.com

MPESA_CONSUMER_KEY=replace-with-mpesa-consumer-key
MPESA_CONSUMER_SECRET=replace-with-mpesa-consumer-secret
MPESA_SHORTCODE=replace-with-mpesa-shortcode
MPESA_PASSKEY=replace-with-mpesa-passkey
MPESA_STK_CALLBACK_URL=https://api.example.com/api/payments/mpesa/callback/
```
