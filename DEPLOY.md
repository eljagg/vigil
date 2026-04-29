# Deploying Vigil to Railway

This walkthrough assumes you have a Railway account and a GitHub repo containing this code.

---

## 1 · Create the Railway project

1. Sign in to [railway.com](https://railway.com)
2. **New Project** → **Deploy from GitHub repo** → pick your Vigil repo
3. Railway detects Python via Nixpacks and starts an initial build. The first build will fail at runtime because there's no `DATABASE_URL` yet — that's expected.

---

## 2 · Add the Postgres plugin

1. From the project canvas, click **+ New** → **Database** → **Add PostgreSQL**
2. Once provisioned, click the new Postgres service → **Variables** tab → confirm `DATABASE_URL` is exported
3. Go back to the Vigil web service → **Variables** tab → click **Add Reference** → pick the Postgres service's `DATABASE_URL`. Railway will inject it into the web service automatically.

The `startCommand` in `railway.json` runs `flask --app app db upgrade` before booting gunicorn on every container start, so migrations apply automatically on every deploy. (Migrations are kept out of the build phase because Railway's internal DNS — `postgres.railway.internal` — is only reachable at runtime, not during builds.)

---

## 3 · Set required environment variables

On the Vigil web service, **Variables** tab, set:

| Variable          | Value                                                                   |
|-------------------|-------------------------------------------------------------------------|
| `SECRET_KEY`      | Generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `COMPANY_NAME`    | Your company name (admin can override later in the UI)                  |
| `TAGLINE`         | `Backup integrity check.` (or whatever)                                 |
| `FOOTER_CREDIT`   | `Designed by Omar McLeod - 2026`                                        |

Optional but recommended:

| Variable                  | Value                                              |
|---------------------------|----------------------------------------------------|
| `SESSION_TIMEOUT_MINUTES` | `60` (idle timeout)                                |
| `LOGIN_RATE_LIMIT`        | `5 per minute` (per-IP; tighten for stricter envs) |

Railway sets `RAILWAY_ENVIRONMENT` automatically, which makes Vigil flip `SESSION_COOKIE_SECURE` on and emit HSTS. No manual flag needed.

---

## 4 · Object storage for the company logo (one-time)

Railway containers have ephemeral filesystems, so logos must live in S3-compatible object storage. **Cloudflare R2** is recommended because the free tier is generous and there are no egress fees.

1. Sign in to Cloudflare → **R2** → **Create bucket** (e.g. `vigil-assets`)
2. Make the bucket publicly readable, or set up a public **R2.dev** subdomain (Cloudflare → R2 bucket → Settings → Public access)
3. Create an API token (R2 → Manage API Tokens → Create API token, scope: Object Read & Write on this bucket)
4. Set these env vars on the Vigil web service:

| Variable             | Value                                                              |
|----------------------|--------------------------------------------------------------------|
| `S3_ENDPOINT_URL`    | `https://<account-id>.r2.cloudflarestorage.com`                    |
| `S3_BUCKET`          | `vigil-assets`                                                     |
| `S3_ACCESS_KEY`      | (from token creation)                                              |
| `S3_SECRET_KEY`      | (from token creation)                                              |
| `S3_REGION`          | `auto`                                                             |
| `S3_PUBLIC_BASE_URL` | `https://pub-<id>.r2.dev` (or your bucket's public URL)            |

(For AWS S3 instead, leave `S3_ENDPOINT_URL` blank and use a real region like `us-east-1`.)

If you don't configure object storage, the logo upload form will still appear to work in dev but logos won't survive a Railway redeploy. Either configure storage or skip uploading a logo and let Vigil show the placeholder.

---

## 5 · Generate a domain

Vigil web service → **Settings** → **Networking** → **Generate Domain**. Railway gives you a `*.up.railway.app` hostname. You can also bring your own custom domain on the same screen.

---

## 6 · Create the first admin user

Railway lets you open a shell into a deployed service. From the Vigil web service:

**Settings** → ⋯ menu → **Open shell** (or use `railway run` from your local CLI), then:

```bash
flask --app app create-admin
```

It'll prompt for username, full name, email (optional), and a password (12+ chars).

If shell access isn't available, an alternative is to run the same command locally pointed at the Railway database — copy `DATABASE_URL` from Railway, set it locally, then `flask --app app create-admin`.

---

## 7 · First-run sanity check

1. Visit your Railway domain — you should see the Vigil sign-in page
2. Sign in with the admin you just created
3. Visit `/admin/settings` → upload a logo, confirm the tagline, save
4. Visit `/admin/users` → create operators (or set their auth source to `ldap` if you've configured that)
5. Visit `/admin/rotation` → click **Generate** to lay out a 12-week rotation
6. Visit `/scan/new` from a Chrome desktop, click **Browse**, pick a folder, click **Run scan** — you should be redirected to the scan view with the three reports

The `/healthz` endpoint returns `{"status":"ok"}` when the app + DB are healthy. Railway's healthcheck path is already pointed at it.

---

## 8 · Ongoing operations

- **Logs** — Railway → service → **Deployments** → click the active deploy → **View logs**
- **Database backups** — Railway Postgres has automatic daily backups; you can also `pg_dump` from a local shell using `DATABASE_URL`
- **Migrations** — when you change a model, run `flask --app app db migrate -m "describe change"` locally, commit the new file in `migrations/versions/`, and push. Railway will run `db upgrade` automatically on the next deploy.
- **Roll back a bad deploy** — Railway → service → **Deployments** → pick a previous successful deploy → **Redeploy**

---

## Troubleshooting

**"DATABASE_URL is not set" on first deploy**
The Postgres plugin isn't linked. Go to the Vigil service → Variables → Add Reference → Postgres → `DATABASE_URL`. Then redeploy.

**"500 Internal Server Error" on first request**
Check Railway logs — usually means the migration step failed. Most common cause is `psycopg` couldn't connect (DATABASE_URL malformed or Postgres still booting). Wait 30s and retry.

**Logo uploads don't show up after a redeploy**
S3 isn't configured. See section 4 above.

**Browser says "Vigil's folder picker requires the File System Access API"**
The user is on Firefox / Safari / mobile, or in a stripped-down browser shell. Open the same URL in Chrome or Edge desktop.

**CSRF errors on the scan page**
The session expired mid-scan. Refresh the page and try again. If it persists, check `SECRET_KEY` is set and stable across replicas.

---

Good luck, Omar.
