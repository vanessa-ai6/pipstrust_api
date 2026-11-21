# Deploying PipsTrust

The Django **backend** on Render (free or paid), the **website** on any host you like, and email sent through
**Brevo, Resend or another provider over HTTPS**. Follow the steps in order.
Dashboards change now and then, so a button may be labelled slightly differently.

---

## 1. Choose your path

| | **Free** Render plan | **Paid** Render plan (about $20-25/month with the website) |
|---|---|---|
| Good for | Rehearsing, showing a demo | Real customers and real money |
| Email | Works, **with an HTTPS provider** (this package): Render's free plan blocks SMTP | Works with an HTTPS provider (recommended) or Gmail SMTP |
| Sleeps when idle | **Yes**: after about 15 minutes without visitors; the next visit waits roughly a minute | No |
| Uploaded files (deposit proofs) | **Lost at every restart and deploy** (no disk) | Kept, on a persistent disk |
| Database | Render's free database expires; use an outside free Postgres (Neon, Supabase, ...) or pay | Render PostgreSQL, paid |

**Please do not take real money on the free plan.** Deposit proofs would vanish, and the site sleeps. Use it to
test, then move to the paid plan (or ask for cloud file storage to be added, which removes the disk problem).

---

## 2. What you will end up with

```
Visitor's browser
      |
      v
Website  (any host: Vercel, Netlify, Cloudflare Pages, Render, ...)     https://yourdomain.com
      |  calls
      v
API      (Render web service, Django + gunicorn)                        https://api.yourdomain.com
      |                    \                  \
      v                     v                  v
PostgreSQL database      Disk (paid only)     Email provider API (Brevo / Resend / ...) over HTTPS
```

The API only needs to know the website's address (for security rules and for the links inside emails),
so you can move the website to another host at any time.

---

## 3. Before you start

- [ ] A **GitHub** account and a **Render** account.
- [ ] An account at an **email provider** (Brevo or Resend; see Step 7) and a sender address you can verify there.
- [ ] Optional: a domain name. Without one you can test on the free `*.onrender.com` addresses.
- [ ] Two folders: the **backend** (contains `manage.py`) and the **frontend** (contains `package.json`).

---

## 4. Step by step

### Step 1 - Add the hosting files to the backend
Copy everything from the `backend/` folder of this package into your backend folder (the one with `manage.py`).
**Copy all of it, including `pipstrust/health.py` and `pipstrust/urls_prod.py`**: the production settings use them,
and Render's health check fails without them. Your normal `settings.py` is not touched.

### Step 2 - Remove the secrets from git
Your old `.env` was committed to git (secret key, database password, mail password). Anyone who sees the history can
read it, and the secret key also signs login tokens. Start a **fresh git history**. In PowerShell, inside the backend folder:

```powershell
Remove-Item -Recurse -Force .git
git init
git add .
git status
```

In the list printed by `git status`, **`.env` and `db.sqlite3` must NOT appear** (the `.gitignore` keeps them out). Then:

```powershell
git commit -m "Initial commit"
```

Create fresh secrets, never the old ones:
- **SECRET_KEY**: `python -c "import secrets; print(secrets.token_urlsafe(64))"` (the production settings refuse the old key).
- **Mail password / API keys**: new ones (Step 7). Delete the old Gmail App Password.

### Step 3 - Put the code on GitHub (private repositories)
Create a **Private** repository `pipstrust-backend` on github.com (leave it empty), then:

```powershell
git remote add origin https://github.com/YOUR-NAME/pipstrust-backend.git
git branch -M main
git push -u origin main
```

Do the same for the website with a second private repository, `pipstrust-frontend`
(its `.gitignore` should contain `.env*.local`; a normal Next.js project already does).

### Step 4 - The database
- **Free path:** create a free PostgreSQL database at an outside provider (for example Neon or Supabase; check their
  current free limits). Copy its connection string; if it does not already end with `?sslmode=require`, add it.
- **Paid path:** Render dashboard > **New +** > **PostgreSQL**, a paid plan, same region as the API
  (for West Africa, Frankfurt is usually the nearest; check the list). Copy the **Internal Database URL**.

### Step 5 - Create the backend on Render
1. **New +** > **Web Service** > connect GitHub > pick `pipstrust-backend`.
2. Fill in:

| Field | Value |
|---|---|
| Language / Runtime | Python 3 |
| Branch | `main` |
| Build Command | `bash build.sh` |
| Start Command | `gunicorn pipstrust.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 120` |
| Instance Type | Free (to test) or Starter (real use) |
| Health Check Path (Settings > Health Checks) | `/healthz/` |

3. **Paid plans only:** add a Disk (name `media`, mount path `/var/data`, 1 GB) and set `MEDIA_ROOT=/var/data/media`.
4. **Environment variables.** Add these (`.env.production.example` lists everything):

| Variable | Value |
|---|---|
| `DJANGO_SETTINGS_MODULE` | `pipstrust.settings_prod` - **must be set**, otherwise the site runs with the development settings |
| `PYTHON_VERSION` | `3.12.3` - without it Render may pick a newer Python than Django 5.0 supports |
| `SECRET_KEY` | the **new** key from Step 2 |
| `ALLOWED_HOSTS` | your API address, e.g. `pipstrust-api.onrender.com` or `api.yourdomain.com` (several: comma-separated) |
| `FRONTEND_URL` | the website's address: `https://...`, **no slash at the end** (placeholder for now if unknown) |
| `DATABASE_URL` | the connection string from Step 4 |
| `CELERY_TASK_ALWAYS_EAGER` | `True` - **required** unless you run Redis |
| `EMAIL_PROVIDER` | `brevo` or `resend` (Step 7) |
| `BREVO_API_KEY` or `RESEND_API_KEY` | the key from the provider (Step 7) |
| `DEFAULT_FROM_EMAIL` | `PipsTrust <noreply@yourdomain.com>` - an address **verified at the provider** |
| `ADMIN_EMAIL` | a real inbox you read (admin alerts go here, and to every staff user) |
| `JWT_ACCESS_TOKEN_LIFETIME_MINUTES` / `JWT_REFRESH_TOKEN_LIFETIME_DAYS` | `60` / `7` |
| `OTP_EXPIRY_MINUTES`, `REFERRAL_BONUS_AMOUNT` | `10`, `5.00` |
| `DJANGO_SUPERUSER_EMAIL`, `DJANGO_SUPERUSER_PASSWORD`, `DJANGO_SUPERUSER_FIRST_NAME`, `DJANGO_SUPERUSER_LAST_NAME` | your first admin (Step 6); **delete these four after the first deploy** |

   Render adds its own `*.onrender.com` address to the allowed hosts automatically. **Never** set `DEBUG=True`.

5. Create the service and watch the **Logs**. When it is live, open `https://YOUR-SERVICE.onrender.com/healthz/`: you should see `ok`.

### Step 6 - Your first admin account
`build.sh` creates it for you from the four `DJANGO_SUPERUSER_*` variables (Step 5), which works on plans that have no Shell.
It runs once, later deploys skip it. **After the first successful deploy, delete those four variables** (the password should not stay there).
(On a paid plan you can instead open the service's **Shell** tab and run `python manage.py createsuperuser`.)
Superusers are verified automatically, so you can log in straight away. The Django admin is at `https://YOUR-API-ADDRESS/admin/`.

### Step 7 - Email through Brevo, Resend or another provider
The site calls the provider's **HTTPS API** (port 443), so it works on any host, including Render's free plan.
You switch provider with environment variables only, no code changes.

**Brevo**
1. Create an account. Under *Senders, domains & dedicated IPs*, add and **verify** your sender address (or domain).
2. *SMTP & API* > *API keys* > create a key. Use the **API key (v3)**, not the SMTP key.
3. Set `EMAIL_PROVIDER=brevo`, `BREVO_API_KEY=...`, and `DEFAULT_FROM_EMAIL=PipsTrust <your-verified-sender>`.
4. Free plan: 300 emails per day, shared by everything you send.

**Resend**
1. Create an account. Add your **domain** and create the DNS records it shows you until it says *verified*.
   (Its test address `onboarding@resend.dev` only reaches your own account's email; check Resend's docs.)
2. Create an API key.
3. Set `EMAIL_PROVIDER=resend`, `RESEND_API_KEY=...`, and `DEFAULT_FROM_EMAIL=PipsTrust <noreply@your-verified-domain>`.
4. Free plan: 100 emails per day and 3,000 per month.

**Others.** `EMAIL_PROVIDER` also accepts `sendgrid` (`SENDGRID_API_KEY`), `mailgun` (`MAILGUN_API_KEY`,
`MAILGUN_SENDER_DOMAIN`) and `postmark` (`POSTMARK_SERVER_TOKEN`). Any provider supported by django-anymail
(Mailjet, MailerSend, Amazon SES, ...): leave `EMAIL_PROVIDER` out and set
`EMAIL_BACKEND=anymail.backends.<name>.EmailBackend` plus `ANYMAIL_<SETTING>=...`
(for example `ANYMAIL_MAILJET_API_KEY` and `ANYMAIL_MAILJET_SECRET_KEY`). List: https://anymail.dev/en/stable/esps/
Gmail SMTP (`EMAIL_PROVIDER=smtp`, or unset) still works on hosts that allow SMTP, but not on Render's free plan.

**Icons in the emails.** Brevo and the other APIs cannot embed pictures, so the small icons are loaded from your website.
Copy the folder `frontend/public/email-icons/` of this package into your website's `public/` folder, so that
`https://yourdomain.com/email-icons/check.png` opens. To host them elsewhere set `EMAIL_ICON_BASE_URL`;
to send emails without icons set `EMAIL_ICON_BASE_URL=none`. If the website's address is not public yet, icons are left out cleanly.

**Check it.** In your website's admin page open the **Email** tab: it shows the provider, warns about problems
(missing key, unverified sender address, ...) and has **Send test email**. Failed emails stay in the log with the provider's
own error message, and **Retry** re-sends them.

### Step 8 - The website, hosted anywhere
Build it like any Next.js site and set **one** variable before the build:
`NEXT_PUBLIC_API_URL=https://YOUR-API-ADDRESS/api` (Next.js bakes it in, so after changing it, redeploy with a clean build).

| Host | How |
|---|---|
| Vercel | Import the repo (Next.js is detected). The free Hobby plan is for non-commercial use; a business site needs Pro. |
| Netlify, Cloudflare Pages | Import the repo and use their Next.js support. Check the terms of their free plans for commercial use. |
| Render | Web Service, Build `npm install && npm run build`, Start `npm start`. Add `NODE_VERSION=20` if the build asks for it. |
| Any server with Node | `npm install && npm run build && npm start` |

(This assumes the usual `build` and `start` scripts in `package.json`.)
Then tell the API where the website lives, in the API's environment: `FRONTEND_URL=https://yourdomain.com`
(exact address, https, no trailing slash). Other addresses of the same site go in `EXTRA_CORS_ORIGINS` (comma-separated, e.g. the `www.` version).
Hosts that make a new address for every preview deployment (Vercel previews, for example) can be allowed with a pattern:
`CORS_ALLOWED_ORIGIN_REGEXES=^https://pipstrust-[a-z0-9-]+\.vercel\.app$`.
Moving the website to another host later = change `NEXT_PUBLIC_API_URL` there and `FRONTEND_URL` on the API, then redeploy both.

### Step 9 - Custom domains (optional)
In each service open **Settings > Custom Domains**, add the domain, and create the DNS records Render shows at your registrar.
Certificates are created automatically. If the API gets a custom domain, add it to `ALLOWED_HOSTS`.

### Step 10 - Test the whole journey
- [ ] `https://YOUR-API-ADDRESS/healthz/` shows `ok`.
- [ ] Register on the website and receive the verification email (check spam too).
- [ ] Log in, submit a deposit with a proof image.
- [ ] Log in as admin and approve it: the wallet goes up and the user gets an email.
- [ ] Invest, request a withdrawal.
- [ ] Admin page > **Email** tab: no failed emails.
- [ ] `https://YOUR-API-ADDRESS/admin/` is styled (not plain text).

---

## 5. When something goes wrong

Look at the service's **Logs** tab first: the production settings print errors and tracebacks there.

| What you see | Likely cause | Fix |
|---|---|---|
| Logs repeat `Not Found: /healthz/` (404), the deploy fails after about 15 minutes or the service restarts every minute | The running code has no `/healthz/` address: `pipstrust/health.py` is missing from the repo, or `settings_prod.py` is not the version from this package, or `DJANGO_SETTINGS_MODULE` is not `pipstrust.settings_prod` | **Right now:** Render > service > Settings > Health Checks > set the path to `/admin/login/` and save. **Permanently:** copy `pipstrust/health.py`, `pipstrust/urls_prod.py` and `pipstrust/settings_prod.py` from this package, commit and push, check the variable, then you may set the path back to `/healthz/` |
| Logs repeat `DisallowedHost: Invalid HTTP_HOST header: 'xxx.onrender.com'` (400) | That address is not in `ALLOWED_HOSTS` | Add it. (`settings_prod` adds Render's own address by itself; if it does not, you are not running `settings_prod`) |
| The logs show `python3.14` (or another version you did not choose) | `PYTHON_VERSION` is not set | Set `PYTHON_VERSION=3.12.3` and redeploy (the package also contains a `.python-version` file) |
| Build fails with `pg_config executable not found` | An old `requirements.txt` (plain `psycopg2`, or `psycopg2-binary` 2.9.9 on Python 3.14) | Use the `requirements.txt` from this package |
| Start fails with `SECRET_KEY is missing, too short, or is the old key...` | No new key, or the old one reused | Set a new `SECRET_KEY` |
| Test email says `API key` / `401` | The provider rejected the key, or none is set | Check `BREVO_API_KEY` / `RESEND_API_KEY` for typos and spaces; Brevo needs the **API** key, not the SMTP key |
| Test email says the sender/domain is not verified | `DEFAULT_FROM_EMAIL` is not a verified sender | Verify the sender (Brevo) or the domain (Resend) and use exactly that address |
| Test email says `sending limit` / `429` | You used up the provider's free quota | Wait for it to reset, or upgrade |
| Emails arrive without the little icons | Icons are not on your website, or `FRONTEND_URL` is not public | Copy `email-icons/` into the website's `public/` folder, or set `EMAIL_ICON_BASE_URL` |
| Emails time out with SMTP | Render's free plan blocks SMTP | Set `EMAIL_PROVIDER=brevo` (or `resend`) |
| Browser console shows a **CORS** error | `FRONTEND_URL` differs from the real website address | Must match exactly: `https://`, same spelling, no trailing slash; add `www` to `EXTRA_CORS_ORIGINS`; previews: `CORS_ALLOWED_ORIGIN_REGEXES` |
| `/admin/` login says **CSRF verification failed** | The API's domain is missing from `ALLOWED_HOSTS` | Add it |
| `/admin/` has no colours | `DJANGO_SETTINGS_MODULE` is not `pipstrust.settings_prod`, or the build stopped before "collectstatic" | Check the variable and the build log |
| Website still calls `localhost` | `NEXT_PUBLIC_API_URL` was set after the build | Set it, then redeploy with a clean build |
| First visit after a quiet period takes a minute | Free plan: the service was asleep | Normal on the free plan |
| Deposit proofs vanish | Free plan (no disk), or `MEDIA_ROOT` is not on the disk | Paid plan with the disk, or cloud file storage |
| The database stopped working after some weeks | Render's free database expired | Use an outside free Postgres or a paid database |

---

## 6. Updating later
Commit and `git push`. Render redeploys automatically; `build.sh` installs packages, collects static files and
applies database migrations each time.

## 7. Good to know
- **Background jobs.** With `CELERY_TASK_ALWAYS_EAGER=True` there is no Redis and no worker; matured investments are settled
  when a user or an admin opens the app, so the "Investment Completed" email can arrive late. For timed settlement: add Redis and two
  more paid services (`celery -A pipstrust worker -l info` and `celery -A pipstrust beat -l info`), set `REDIS_URL`, switch
  `CELERY_TASK_ALWAYS_EAGER` off and run `python setup_beat.py` once.
- **Backups.** The database holds money records. Check what your plan includes and take your own copies, e.g. `pg_dump "<connection string>" > backup.sql`.
- **Uploads are public by URL.** Anyone with the exact address of a deposit proof can open it. Later, move uploads to private storage with signed links.
- **HSTS.** It starts at 1 hour. Once everything works over https, set `SECURE_HSTS_SECONDS=31536000`.
- **API docs** (`/api/docs/`) are hidden in production; `ENABLE_API_DOCS=True` shows them.
- **Legal.** A site that takes deposits and pays fixed returns is regulated in many countries. Check the licensing rules where your
  customers live and speak to a lawyer before accepting real money.

## 8. What was tested, and what was not
Tested here: the unit tests (provider switch, Brevo and Resend request formats against a local stand-in server, failures and hints,
icons, diagnostics); and the production settings against a real PostgreSQL with gunicorn (HTTPS headers simulated) on Python 3.12 and 3.14:
build and migrations, `DATABASE_URL` parsing, https redirect, host protection, admin styling and login, secure cookies, uploads,
path-traversal protection, CORS (including patterns for preview addresses), hidden API docs, the health check with Render's hostname,
crash logging, the secret-key guard, first-admin creation, and a real email sent through the Brevo setup.
Not tested: a real deployment on Render, real Brevo/Resend accounts (their servers were replaced by a local stand-in that follows
their documented API), your Next.js project, and the free plan's file-storage limitation (not solved yet).

## 9. Where to check prices and rules
- Render pricing and free-plan limits: https://render.com/pricing
- Render health checks: https://render.com/docs/health-checks
- Render SMTP restriction: https://render.com/changelog/free-web-services-will-no-longer-allow-outbound-traffic-to-smtp-ports
- Brevo pricing: https://www.brevo.com/pricing/
- Resend pricing: https://resend.com/pricing
- Supported email providers and settings: https://anymail.dev/en/stable/esps/
