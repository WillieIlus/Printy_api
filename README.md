# Printy API

Backend for a print-shop SaaS and marketplace focused on Kenyan print businesses. The project is a Django 5 + DRF API that covers shop onboarding, catalog and pricing setup, quote drafting and messaging, public shop discovery, billing with M-Pesa, production tracking, artwork PDF analysis, analytics ingestion, and lead capture.

## What is in this repo

- Multi-tenant shop management: shops, memberships, opening hours, ratings, favorites, nearby search.
- Catalog and pricing: products, categories, machines, papers, materials, printing rates, finishing rates, volume discounts, rate cards, and setup wizards.
- Quote workflows: customer quote requests, seller responses, draft quotes, attachments, share links, inbox-style messaging, and calculator previews.
- Public marketplace features: public shops, public products, SEO location/product endpoints, match-shops calculator flows, and guest quote submission.
- Billing: subscription plans, entitlements, usage counters, M-Pesa STK push, callbacks, renewal retries, grace periods, and admin support actions.
- Production and jobs: production orders, job processes, operators, price cards, and overflow job requests/claims.
- Artwork analysis: uploaded PDF analysis with size detection, booklet hints, preview generation, and product suggestions.
- Platform support: JWT auth, allauth email verification, Google social login, analytics event ingestion, i18n, and Django admin tooling.

## Stack

- Python 3
- Django 5.2
- Django REST Framework
- SimpleJWT
- django-allauth
- PostgreSQL
- PyMuPDF and Pillow for artwork/PDF processing
- pytest + pytest-django

## Main apps

| App | Responsibility |
| --- | --- |
| `accounts` | Custom user model, JWT auth, registration, email verification, Google social login, roles |
| `api` | Main API views/serializers for shops, quotes, public marketplace, calculator, analytics, SEO, workflow endpoints |
| `artwork` | Artwork upload, PDF analysis, preview generation |
| `billing` | Current subscription/billing system, plans, transactions, renewals, entitlements |
| `catalog` | Product catalog models and validation |
| `common` | Shared models, middleware, request/meta helpers, analytics event model |
| `core` | Shared permissions and querysets |
| `feedback` | Feedback intake endpoints and throttling |
| `gallery` | Gallery categories/products for shop storefronts |
| `inventory` | Machines, paper sizes, papers |
| `jobs` | Overflow job marketplace requests and claims |
| `leads` | Early-access and demo lead capture |
| `locations` | SEO/discovery locations |
| `notifications` | User notifications API |
| `pricing` | Finishing categories, printing/finishing/material/service rates, discounts |
| `production` | Production orders, processes, customers, operators, dashboard |
| `quotes` | Quote domain models, pricing helpers, summaries, draft/share artifacts |
| `services` | Pricing engines and lower-level calculator services |
| `setup` | Shop setup-status endpoints and seed helpers |
| `shops` | Shop model, memberships, hours, ratings, related admin |
| `subscriptions` | Legacy subscription/M-Pesa module still referenced by some API routes |

## API shape

Top-level routing is defined in `config/urls.py` and `api/urls.py`.

Important route groups:

- `/api/auth/`: register, login, token refresh, profile/me, email verify/resend, Google social login
- `/api/setup/`: setup status endpoints
- `/api/billing/`: plans, subscription lifecycle, usage, payments, M-Pesa callbacks
- `/api/leads/`: early-access spots, applications, demo actions
- `/api/artwork/`: artwork upload and detail endpoints
- `/api/public/...`: public shops, products, calculators, match-shops flows
- `/api/seo/...`: SEO-oriented location/product route data
- `/api/shops/...`: seller shop resources, pricing resources, hours, gallery, products, rate cards
- `/api/quote-requests`, `/api/quote-drafts`, `/api/sent-quotes`, `/api/quotes`: quoting workflows
- `/api/calculator/...`: calculator config, previews, draft creation/sending
- `/api/dashboard/...`: shop dashboard and calculator preview endpoints
- `/api/jobs`, `/api/job-processes`, `/api/customers`, etc.: production tracking resources

The API is primarily JWT-protected. Public discovery and some guest quoting endpoints are open by design.

## Auth and user flow

- Authentication uses JWT bearer tokens only for the API.
- Registration and email confirmation are implemented with `django-allauth`.
- Verification links are generated against `FRONTEND_URL` through `accounts.adapters.AccountAdapter`.
- Authenticated language preference is stored on the user profile; unauthenticated requests can use `Accept-Language`.
- Google social login is implemented; GitHub provider is installed but not exposed in `accounts/urls.py`.

## Pricing and quoting

- Shop-scoped pricing is the dominant pattern: machines, papers, materials, finishing rates, discounts, and products belong to a shop.
- The codebase includes multiple calculator entry points:
  - standard quote preview
  - booklet preview
  - large-format preview
  - public calculator/match-shops preview
  - shop onboarding rate-wizard and MVP rate-card preview/setup
- Quote workflows include draft creation, item attachments, customer/shop messaging, response accept/reject flows, and public share links.
- Pricing logic lives across `quotes/`, `services/pricing/`, and `services/engine/`.

## Billing and payments

- The active billing domain is in `billing/`.
- Plans are seeded as code-based tiers such as `FREE`, `BIASHARA`, `BIASHARA_PLUS`, and `BIASHARA_MAX`.
- Billing supports activation, upgrades, downgrades, cancellations, manual renewals, and retry/grace-period handling.
- M-Pesa Daraja STK push and callback handling are implemented.
- `subscriptions/` still exists as an older module and some `/api/...` routes still reference it, so both domains currently coexist.

## Artwork analysis

Artwork upload and PDF analysis live in `artwork/`.

Current analysis behavior includes:

- opening PDFs with PyMuPDF
- extracting page size and page count
- detecting mixed page sizes
- generating a JPEG preview
- inferring likely product types such as flyer, booklet, business card, or large-format poster
- normalizing booklet page counts to multiples of 4 for production hints

## Local setup

1. Create and activate a virtual environment.
2. Install dependencies.
3. Copy `.env.example` to `.env` and fill in required values.
4. Ensure PostgreSQL is available and the configured database exists.
5. Run migrations.
6. Configure the Django site metadata.
7. Seed any required domain data.
8. Start the development server.

Example:

```powershell
python -m venv env
.\env\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python manage.py migrate
python manage.py configure_site
python manage.py runserver
```

## Environment

The project loads environment variables from `.env` via `python-dotenv`.

Core variables:

- `SECRET_KEY`
- `DEBUG`
- `ALLOWED_HOSTS`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_HOST`
- `DB_PORT`
- `FRONTEND_URL`
- `CORS_ALLOWED_ORIGINS`
- `CSRF_TRUSTED_ORIGINS`

Email/auth variables:

- `ACCOUNT_EMAIL_VERIFICATION`
- `EMAIL_BACKEND`
- `EMAIL_HOST`
- `EMAIL_PORT`
- `EMAIL_USE_TLS`
- `EMAIL_HOST_USER`
- `EMAIL_HOST_PASSWORD`
- `DEFAULT_FROM_EMAIL`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GITHUB_CLIENT_ID`
- `GITHUB_CLIENT_SECRET`

Billing variables:

- `MPESA_ENV`
- `MPESA_BASE_URL`
- `MPESA_CONSUMER_KEY`
- `MPESA_CONSUMER_SECRET`
- `MPESA_SHORTCODE`
- `MPESA_PASSKEY`
- `MPESA_CALLBACK_URL`
- `MPESA_TIMEOUT_SECONDS`
- `BILLING_GRACE_PERIOD_DAYS`
- `BILLING_RETRY_SCHEDULE_HOURS`

See `docs/env_vars.md` and `.env.example` for the fuller reference.

## Useful management commands

- `python manage.py migrate`
- `python manage.py createsuperuser`
- `python manage.py configure_site`
- `python manage.py seed_billing_plans`
- `python manage.py seed_large_format`
- `python manage.py seed_shop_pricing`
- `python manage.py queue_due_renewals`
- `python manage.py process_due_renewals`
- `python manage.py expire_grace_periods`
- `python manage.py backfill_usage_counters`
- `python manage.py analyze_pdf_sample <path>`

## Testing

The repo contains Django tests and pytest-based execution.

```powershell
pytest
```

Targeted examples:

```powershell
pytest api/tests.py
pytest billing/tests/test_payments.py
pytest tests/test_engine_services.py
```

## Project notes

- Database configuration is PostgreSQL-first in `config/settings.py`; SQLite is not the active default.
- Static files are served with WhiteNoise.
- API 404/500 responses are customized to return JSON instead of default Django HTML.
- The project contains substantial implementation notes under `docs/`.
- There are older and newer implementations in a few domains, especially billing/subscriptions and some quote flows, so route-level behavior should be checked against the current view modules when making changes.
