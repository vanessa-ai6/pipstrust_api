# PipsTrust backend implementation notes

## What changed

- Matured investments are settled atomically and become `completed` once they reach their end time/100% progress.
- Dashboard and investment reads also run the settlement check, so an investment cannot stay visually active just because Celery Beat is delayed.
- Investment settlement is idempotent and protected with database row locks to prevent duplicate ROI credits.
- Investment creation now locks and re-checks the wallet balance before deducting funds.
- Deposit approval updates `total_deposited`; starting an investment no longer incorrectly increases that total.
- Withdrawal requests reserve funds immediately, use row locks, validate the configured payout address, support BTC/ETH/USDT TRC-20/USDT ERC-20, and refund safely on cancel/reject.
- If withdrawal OTP delivery fails, the held amount is automatically returned.
- Withdrawal OTP can be resent.
- Admin withdrawal serialization no longer references deleted bank fields.
- Added support-ticket API for users plus admin reply/close endpoints and Django admin registration.
- Added migrations for ERC-20 USDT support and support tickets.
- Added regression tests for investment settlement, withdrawal reservation/refund, ERC-20 payout selection and support tickets.

## Deploy

1. Keep your existing production `.env` file private.
2. Install requirements: `pip install -r requirements.txt`
3. Run migrations: `python manage.py migrate`
4. Run Django checks/tests: `python manage.py check` and `python manage.py test`
5. Re-run `python setup_beat.py` so matured investments are checked every minute.
6. Restart Django, Celery worker and Celery Beat.

The API also settles matured investments when the user opens the dashboard or investment list, so the UI does not depend exclusively on Celery.

---

## Update: deposits credit the wallet, and email is now observable

### The wallet bug
A confirmed deposit must raise **wallet_balance** (spendable) *and* **total_deposited** (history).
Before, the admin-API approval crashed on a misspelt field (`total_deposit`), and the Django admin let
staff set a deposit's status to "confirmed" by hand, which counted it in Total Deposited without ever
crediting the wallet. Now:

- `apps/investments/services.py` has one `confirm_deposit()` / `reject_deposit()`. The admin API **and** the
  Django admin actions both call it, in one transaction, with row locks (cannot double-credit).
- The Deposit and Withdrawal `status` fields are read-only in the Django admin; use the actions instead.
- Starting an investment no longer touches `total_deposited`.
- `python manage.py reconcile_wallets` finds wallets that do not match the ledger (dry run by default);
  add `--apply` (optionally `--email user@x.com`) to repair them. Run it once after deploying.

### Email
- Old behaviour: `fail_silently=True`, so any SMTP error vanished. Now every email is stored in `EmailLog`
  (sent/failed, real error text) and printed to the server log.
- **Admin**: Django admin > Notifications > Email logs (filter Status = Failed, action "Retry selected failed emails"),
  or `GET /api/admin/email-logs/`, `POST /api/admin/email-logs/<id>/retry/`, `POST /api/admin/email-test/`.
  Frontend: the "Email" tab in `app/admin/page.tsx` (red badge + Overview banner when emails fail), built from
  `components/admin/EmailHealthPanel.tsx`; the same panel is also served at `/admin/email-logs`.
- **User**: `GET /api/notifications/email-log/` (status only, no technical detail); the dashboard shows a notice if
  recent account emails failed. Every email is also an in-app notification.
- **Command line**: `python manage.py check_email --to you@example.com` prints the config, flags likely problems and sends a real test.
- Admin alerts go to `ADMIN_EMAIL` (comma-separated list allowed) **and** every active staff user.
- Emails are now sent for: deposit submitted (user + admins), deposit confirmed/rejected, wallet credited by admin,
  withdrawal requested/OTP-confirmed (admins)/processed/rejected/cancelled, investment started/completed,
  referral bonus, support ticket opened/replied/closed, broadcast, and all OTPs.
- OTP codes are never stored in `EmailLog` and OTP emails are not retryable (the user requests a fresh code).
- Money-moving code sends its emails *after* the database commit, so a slow mail server cannot hold locks and
  an email is never sent for a transaction that rolled back.

### Other fixes found on the way
- `api_response()` ignored its `http_status` argument, so every error was returned as HTTP 200. It now returns real codes.
- Celery was never loaded (`pipstrust/__init__.py` was empty and `celery.py` pointed at `config.settings`).
- Referral-bonus signal never fired (its "previous state" logic was inverted) and the task crashed on float+Decimal.
- Withdrawal OTP-failure recovery crashed (`withdrawal.objects`); Django-admin withdrawal actions referenced a deleted field.
- `JWT_ACCESS_TOKEN_LIFETIME_MINUTES` was read under the wrong env name; `FRONTEND_URL` was missing from settings;
  added `EMAIL_TIMEOUT`, `EMAIL_USE_SSL` and logging.
- Completing an investment credited the wallet correctly but then crashed (`TypeError` in the completion-email
  callback): the "Investment Completed" email never went out and the first request after maturity failed. Fixed.
- The web app created support tickets at `/support/tickets/create/`, which does not exist (404). The frontend now uses
  `/support/tickets/` and the backend also accepts the old URL.
- `apps/__init__.py` added so `python manage.py test` discovers the tests.

### Deploy
1. `pip install -r requirements.txt` (no new packages)
2. `python manage.py migrate`  (adds the `email_logs` table)
3. `python manage.py check_email --to <your real inbox>`
4. `python manage.py reconcile_wallets` (review) then `... --apply`
5. Restart Django, Celery worker and Celery Beat.

