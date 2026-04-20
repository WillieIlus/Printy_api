# Environment Variables - printy_api

All required and optional environment variables. Use a local `.env` file or deployment secret management.

## Core

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SECRET_KEY` | Yes | (none) | Django secret key |
| `DEBUG` | No | `false` | `true`/`1`/`yes` enables debug mode |
| `ALLOWED_HOSTS` | No | `api.printy.ke,178.128.206.240,localhost,127.0.0.1,testserver` | Comma-separated Django hosts |
| `ENV_DEBUG` | No | `false` | Logs env presence only, without printing secret values |

## Database

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DB_NAME` | Yes | `printy_db` | PostgreSQL database name |
| `DB_USER` | Yes | `printy_user` | PostgreSQL user |
| `DB_PASSWORD` | Yes | (empty) | PostgreSQL password |
| `DB_HOST` | No | `127.0.0.1` | PostgreSQL host |
| `DB_PORT` | No | `5432` | PostgreSQL port |

## Frontend, CORS, and CSRF

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FRONTEND_URL` | No | `http://localhost:3000` | Frontend base URL for links and redirects |
| `SITE_DOMAIN` | No | `localhost:8000` in debug, `printy.ke` otherwise | Domain stored in Django Sites; run `python manage.py configure_site` after changing it |
| `SITE_NAME` | No | `Printyke` | Display name stored in Django Sites |
| `CORS_ALLOWED_ORIGINS` | No | local dev + printy domains | Comma-separated allowed frontend origins |
| `CSRF_TRUSTED_ORIGINS` | No | local dev + printy domains | Comma-separated trusted origins |

## Email and OAuth

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DEFAULT_FROM_EMAIL` | No | `Printyke <hello.printyke@gmail.com>` | From address for emails |
| `EMAIL_BACKEND` | No | console backend | SMTP backend override for production |
| `EMAIL_HOST` | No | `smtp.gmail.com` | SMTP server host |
| `EMAIL_PORT` | No | `587` | SMTP server port |
| `EMAIL_USE_TLS` | No | `true` | Enable STARTTLS |
| `EMAIL_HOST_USER` | Yes in production SMTP | (empty) | SMTP username |
| `EMAIL_HOST_PASSWORD` | Yes in production SMTP | (empty) | SMTP password or app password |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | If Google login is enabled | (empty) | Google OAuth credentials |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | If GitHub login is enabled | (empty) | GitHub OAuth credentials |

Email verification flow notes:
- Registration and resend emails generate links to `${FRONTEND_URL}/auth/confirm-email?key=...`.
- Production should use `FRONTEND_URL=https://printy.ke` and API origin `https://api.printy.ke`.
- After changing `SITE_DOMAIN` / `SITE_NAME`, run `python manage.py configure_site` so allauth metadata matches the deployed site.

## JWT

JWT uses `SECRET_KEY` for signing. No separate JWT env vars are required.

## M-Pesa / Daraja

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MPESA_ENV` | Yes when enabled | `sandbox` | `sandbox` or `production` |
| `MPESA_BASE_URL` | No | derived from `MPESA_ENV` | Override Daraja API base URL explicitly |
| `MPESA_CONSUMER_KEY` | Yes when enabled | (empty) | Daraja consumer key |
| `MPESA_CONSUMER_SECRET` | Yes when enabled | (empty) | Daraja consumer secret |
| `MPESA_SHORTCODE` | Yes when enabled | (empty) | Paybill or till number |
| `MPESA_PASSKEY` | Yes when enabled | (empty) | Lipa Na M-Pesa Online passkey |
| `MPESA_CALLBACK_URL` | Yes for STK push | (empty) | Public HTTPS billing callback URL |
| `MPESA_TIMEOUT_SECONDS` | No | `30` | HTTP timeout for Daraja requests |
| `MPESA_ACCOUNT_REFERENCE_DEFAULT` | No | `PRINTY` | Default account reference prefix |
| `MPESA_TRANSACTION_DESC_DEFAULT` | No | `Printy payment` | Default STK transaction description prefix |
| `MPESA_INITIATOR_NAME` | Only for future B2C flows | (empty) | Daraja initiator name |
| `MPESA_INITIATOR_PASSWORD` | Only for future B2C flows | (empty) | Initiator password before encryption |
| `MPESA_SECURITY_CREDENTIAL` | Only for future B2C flows | (empty) | Encrypted security credential |
| `MPESA_TIMEOUT_URL` | Only for timeout/result callback flows | (empty) | Public timeout callback URL |
| `MPESA_RESULT_URL` | Only for timeout/result callback flows | (empty) | Public result callback URL |

Notes:
- In production, `MPESA_CALLBACK_URL` must be a public HTTPS URL and must not point to `localhost`.
- The code keeps the legacy `MPESA_STK_CALLBACK_URL` name as a fallback alias, but `MPESA_CALLBACK_URL` is the canonical variable going forward.

## Billing retries

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BILLING_GRACE_PERIOD_DAYS` | No | `3` | Grace period before suspension |
| `BILLING_RETRY_SCHEDULE_HOURS` | No | `6,24,48` | Comma-separated retry delays in hours |
