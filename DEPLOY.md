# Deploying PipsTrust

This guide puts the **Django backend** and the **Next.js website** on [Render](https://render.com), with a
PostgreSQL database and a small disk for uploaded files. Follow the steps in order.
Render's screens change now and then, so a button may be labelled slightly differently.

---

## 1. What you will end up with

```
Visitor's browser
      |
      v
Website  (Render web service, Next.js)      https://yourdomain.com
      |  calls
      v
API      (Render web service, Django)       https://api.yourdomain.com
      |                \
      v                 v
PostgreSQL database    Disk (deposit proofs, avatars)
      
Emails go out through Gmail SMTP (port 587).
```

**Approximate monthly cost** (confirm on https://render.com/pricing before you pay):

| Part | Plan | About |
|---|---|---|
| API web service | Starter | $7 |
| PostgreSQL | Basic (smallest paid) | $6-7 |
| Website web service | Starter | $7 |
| Disk for uploads | 1 GB | $0.25 |
| **Total** | | **about $20-25** |

**Why not a free plan?** Render's free web services block outgoing email (SMTP ports 25, 465 and 587), so your
Gmail emails would never be sent, and free services cannot have a disk. Free databases expire. Use paid plans.
(Railway also blocks SMTP on its cheaper plans.)

---

## 2. Before you start

- [ ] A **GitHub** account.
- [ ] A **Render** account with a payment card that works for USD payments (if your local card is declined,
      use a virtual USD card).
- [ ] A **new Gmail App Password** (Google Account > Security > 2-Step Verification > App passwords).
      Delete the old one: it was in your `.env`, which was committed to git.
- [ ] Optional: a domain name. Without one you can test on the free `*.onrender.com` addresses.
- [ ] Two folders on your computer: the **backend** (contains `manage.py`) and the **frontend** (contains `package.json`).

---

## 3. Step by step

### Step 1 - Add the hosting files to the backend
Copy everything from the `backend/` folder of this package into your backend folder (the one with `manage.py`).
It adds `settings_prod.py`, `urls_prod.py`, `health.py`, `build.sh`, `.gitignore`, `.gitattributes`,
`.env.production.example` and replaces `requirements.txt`. Your normal `settings.py` is not touched,
so nothing changes on your own computer.

### Step 2 - Remove the secrets from git
Your old `.env` was committed to git, with the secret key, database password and Gmail password inside.
Anyone who sees the git history can read them, and the secret key also signs login tokens.
The simplest fix is to start a **fresh git history**. In PowerShell, inside the backend folder:

```powershell
Remove-Item -Recurse -Force .git
git init
git add .
git status
```

Look at the list printed by `git status`: **`.env` and `db.sqlite3` must NOT be in it**
(the new `.gitignore` keeps them out). Then:

```powershell
git commit -m "Initial commit"
```

Also create fresh secrets (never reuse the old ones):

- **SECRET_KEY**: run `python -c "import secrets; print(secrets.token_urlsafe(64))"` and keep the result for Step 5.
  The production settings refuse to start with the old key or a short one.
- **Gmail App Password**: the new one from Step 2's checklist.

### Step 3 - Put the code on GitHub (private repositories)
On github.com create a **Private** repository named `pipstrust-backend` (leave it empty), then:

```powershell
git remote add origin https://github.com/YOUR-NAME/pipstrust-backend.git
git branch -M main
git push -u origin main
```

Do the same for the website folder with a second private repository, `pipstrust-frontend`.
Check that its `.gitignore` contains `.env*.local` (a normal Next.js project already does) so no secrets are pushed.

### Step 4 - Create the database (Render)
1. Render dashboard > **New +** > **PostgreSQL**.
2. Name: `pipstrust-db`. Choose a **paid** plan (the smallest is fine to start).
3. Region: the one closest to your customers (for West Africa this is usually Frankfurt; check the list).
   **Remember the region: the API service must use the same one.**
4. Create it, then open it and copy the **Internal Database URL** (you need it in Step 5).

### Step 5 - Create the backend (Render)
1. **New +** > **Web Service** > connect your GitHub account > pick `pipstrust-backend`.
2. Fill in:

| Field | Value |
|---|---|
| Language / Runtime | Python 3 |
| Region | **same as the database** |
| Branch | `main` |
| Build Command | `bash build.sh` |
| Start Command | `gunicorn pipstrust.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 120` |
| Instance Type | **Starter** (a paid type is required for the disk and for email) |
| Health Check Path (Advanced) | `/healthz/` |

3. **Disk** (Advanced > Add Disk): name `media`, mount path `/var/data`, size 1 GB.
   Without it, uploaded deposit proofs are erased every time you deploy.
4. **Environment variables.** Add these (the file `.env.production.example` lists them all):

| Variable | Value |
|---|---|
| `DJANGO_SETTINGS_MODULE` | `pipstrust.settings_prod` |
| `PYTHON_VERSION` | `3.12.3` (the version this was tested on; Django 5.0 is not built for newer Pythons) |
| `SECRET_KEY` | the **new** key from Step 2 |
| `ALLOWED_HOSTS` | your API address, e.g. `api.yourdomain.com` (comma-separate several). Render's own `*.onrender.com` address is added automatically. |
| `FRONTEND_URL` | the website's address, starting with `https://`, **no slash at the end**. If you don't know it yet, put a placeholder and fix it in Step 8. |
| `EXTRA_CORS_ORIGINS` | optional, e.g. `https://www.yourdomain.com` |
| `DATABASE_URL` | the **Internal Database URL** from Step 4 |
| `MEDIA_ROOT` | `/var/data/media` |
| `CELERY_TASK_ALWAYS_EAGER` | `True` - **required** unless you run Redis; otherwise background tasks try to reach a Redis server that does not exist |
| `EMAIL_HOST_USER` | your Gmail address |
| `EMAIL_HOST_PASSWORD` | the new 16-character App Password |
| `DEFAULT_FROM_EMAIL` | `PipsTrust <your-gmail-address@gmail.com>` |
| `ADMIN_EMAIL` | a real inbox you read (admin alerts go here, and to every staff user) |
| `JWT_ACCESS_TOKEN_LIFETIME_MINUTES` | `60` |
| `JWT_REFRESH_TOKEN_LIFETIME_DAYS` | `7` |
| `OTP_EXPIRY_MINUTES` | `10` |
| `REFERRAL_BONUS_AMOUNT` | `5.00` |

   Email host, port and TLS already default to Gmail on port 587, so you do not need to set them.
   **Never** set `DEBUG=True` in production (the production settings force it off anyway).

5. Click **Create Web Service**. Watch the **Logs**: the build installs packages, collects static files and
   creates the database tables. When it says the service is live, open
   `https://YOUR-SERVICE.onrender.com/healthz/` - you should see `ok`.

### Step 6 - Create your admin account
Open the service's **Shell** tab and run:

```
python manage.py createsuperuser
```

Enter your email, first name, last name and a strong password. Superusers are verified automatically,
so you can log straight in. The Django admin is at `https://YOUR-API-ADDRESS/admin/`.

### Step 7 - Deploy the website (Render)
1. **New +** > **Web Service** > pick `pipstrust-frontend`.
2. Runtime: Node. Build Command: `npm install && npm run build`. Start Command: `npm start`. Instance: Starter.
   (This assumes the usual `build` and `start` scripts in `package.json`. If the build complains about the Node
   version, add an environment variable `NODE_VERSION` = `20`.)
3. Environment variable: `NEXT_PUBLIC_API_URL` = `https://YOUR-API-ADDRESS/api`
   (for example `https://api.yourdomain.com/api`, or `https://pipstrust-api.onrender.com/api` for a first test).

   **Important:** Next.js bakes this value into the site when it is *built*. If you change it later, redeploy with
   **Manual Deploy > Clear build cache & deploy**.
4. Create the service and wait until it is live.

### Step 8 - Connect the two and add your domains
1. Go back to the **API** service > Environment and set `FRONTEND_URL` to the website's final address
   (`https://yourdomain.com` or its `onrender.com` address). Add the `www` version to `EXTRA_CORS_ORIGINS` if you use it.
   Save - Render redeploys the API.
2. Custom domains (optional): in each service open **Settings > Custom Domains**, add `api.yourdomain.com` to the API
   and `yourdomain.com` (and `www.`) to the website, then create the DNS records Render shows you at your domain
   registrar. HTTPS certificates are created automatically. If you add a custom API domain, also add it to
   `ALLOWED_HOSTS`, and update `NEXT_PUBLIC_API_URL` on the website (then redeploy it).

### Step 9 - Test the whole journey
Open the website and go through it like a customer:

- [ ] Register, and check the verification code arrives by email (look in spam too).
- [ ] Log in.
- [ ] Submit a deposit with a proof image.
- [ ] Log in as admin (`/admin` page of the website) and open the deposit: **the proof image must display**.
- [ ] Approve the deposit: the wallet balance goes up and the user gets an email.
- [ ] Invest, and try a withdrawal request.
- [ ] Admin panel > **Email** tab: no failed emails. Use **Send test email** if in doubt.
- [ ] Log into `https://YOUR-API-ADDRESS/admin/`: the page should be styled (not plain text).

---

## 4. When something goes wrong

Always look at the service's **Logs** tab first. The production settings print errors and tracebacks there.

| What you see | Likely cause | Fix |
|---|---|---|
| Build fails with `pg_config executable not found` | The old `requirements.txt` (it listed `psycopg2`) is still in use | Use the `requirements.txt` from this package |
| Start fails with `SECRET_KEY is missing, too short, or is the old key...` | No new key set, or the old one was reused | Set a new `SECRET_KEY` (Step 2) |
| Page shows `Bad Request (400)` | The address is not in `ALLOWED_HOSTS` | Add the exact domain |
| Browser console shows a **CORS** error | `FRONTEND_URL` differs from the real website address | Must match exactly: `https://`, same spelling, no trailing slash; add `www` to `EXTRA_CORS_ORIGINS`; redeploy the API |
| `/admin/` login says **CSRF verification failed** | The API's domain is missing from `ALLOWED_HOSTS` | Add it (the production settings build the trusted list from it) |
| `/admin/` has no colours or layout | `DJANGO_SETTINGS_MODULE` is not `pipstrust.settings_prod`, or the build failed at "collectstatic" | Check the variable and the build log |
| Website still calls `localhost` | `NEXT_PUBLIC_API_URL` was set after the build | Set it, then **Clear build cache & deploy** |
| Deposit proofs vanish after a deploy | No disk, or `MEDIA_ROOT` is not on the disk | Add the disk at `/var/data` and set `MEDIA_ROOT=/var/data/media` |
| Emails never arrive | Free Render plan (SMTP blocked), wrong App Password, or Gmail limits | Use a paid instance; check the Admin **Email** tab for the exact error; check spam |
| Background tasks hang or fail | `CELERY_TASK_ALWAYS_EAGER` is not `True` | Set it to `True` (or run Redis + a worker) |
| Service keeps restarting as "unhealthy" | Health Check Path is wrong | Set it to `/healthz/` |
| Build log says it cannot connect to the database during `migrate` | Database and API are in different regions, or the wrong URL was used | Same region; use the **Internal** Database URL. If it still fails, remove the `migrate` line from `build.sh` and put `python manage.py migrate --noinput` in Render's **Pre-Deploy Command** |

---

## 5. Updating the site later
Commit and `git push` to GitHub. Render redeploys automatically; `build.sh` installs new packages, collects static
files and applies database migrations every time.

---

## 6. Good to know / next steps

- **Background jobs.** With `CELERY_TASK_ALWAYS_EAGER=True` there is no Redis and no worker. Matured investments are
  settled when a user or an admin opens the app, so the "Investment Completed" email can arrive late. For settlement
  on a timer, add a Redis instance and two more paid services (a Background Worker running
  `celery -A pipstrust worker -l info` and one running `celery -A pipstrust beat -l info`), set `REDIS_URL`, turn
  `CELERY_TASK_ALWAYS_EAGER` off, and run `python setup_beat.py` once.
- **Backups.** The database holds money records. Check what your database plan includes, and also take your own
  copies, for example `pg_dump "<External Database URL>" > backup.sql`.
- **Uploads are public by URL.** Anyone who has the exact address of a deposit proof can open it. Fine to start;
  later move uploads to private storage (S3 / Cloudflare R2) with signed links.
- **Gmail** limits how many emails you can send per day and rewrites the From address. A transactional email service
  (Brevo, Resend, Mailgun) with your own domain is more reliable once you have many users.
- **HSTS.** It starts at 1 hour. After everything works over https, set `SECURE_HSTS_SECONDS=31536000` (1 year).
- **API docs** (`/api/docs/`) are hidden in production. Set `ENABLE_API_DOCS=True` only if you need them.
- **A cheaper alternative** is a single VPS that you manage yourself (Docker, HTTPS, updates and backups are then your job).
- **Legal.** A site that takes deposits and pays fixed returns is regulated in many countries. Check the licensing
  rules where your customers live and speak to a lawyer before accepting real money.

---

## 7. What was tested, and what was not

Tested: the production settings were run against a real PostgreSQL database with gunicorn (with the HTTPS headers a
host adds simulated). All checks passed: build and 72 migrations, `DATABASE_URL` parsing (including a password with
special characters), https redirect, host protection, admin styling and login with CSRF, secure cookies, HSTS,
uploaded files, path-traversal protection, CORS (your site allowed, others refused), hidden API docs, the health
check, crash logging, and the secret-key guard.

Not tested: an actual deployment on Render (I cannot reach it from here), your Next.js project (only its
`package.json` scripts are assumed), and real Gmail delivery from Render.

## 8. Where to check prices and rules
- Render pricing: https://render.com/pricing
- Render SMTP restriction: https://render.com/changelog/free-web-services-will-no-longer-allow-outbound-traffic-to-smtp-ports
- Vercel Hobby plan (not for commercial use, if you prefer Vercel for the website): https://vercel.com/docs/plans/hobby
