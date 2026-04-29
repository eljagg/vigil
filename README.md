# Vigil — Backup integrity check

Vigil audits a folder of backup files and produces three reports per scan:

1. **Size distribution** (largest first) with size deltas vs. the previous scan
2. **Encryption-name audit** flagging files without `_encrypted` in the name
3. **Timestamp ordering** with day-of-week labels to spot weekend / holiday gaps

Each scan is diffed against the previous scan of the same folder, so on a typical week you see at a glance which backups grew, shrunk, or appeared new. A weekly Mon–Sun rotation tracks who's on duty, the dashboard nags about missed weeks, and an immutable audit log records every action.

This is **Vigil 2.0** — a Railway-hosted Flask + Postgres web app with browser-side scanning. There is no on-prem component; the operator's own browser walks the backup folder and POSTs file metadata (names, sizes, modification times only) to the hosted app.

---

## How it actually works

Browsers can't reach SMB / UNC paths directly, so Vigil uses the **File System Access API** to read whatever folder the operator picks on their workstation. That means:

- The operator opens Vigil in **Chrome** or **Edge** on a desktop / laptop
- They click **Browse** and pick the folder (typically a mapped drive — `Z:\Backup Logs\Archive` or similar)
- The browser walks the folder client-side, builds a list of `(filename, size, mtime)`, and posts it to the hosted Vigil
- Vigil stores the manifest in Postgres, computes the diff against the previous scan, and renders the three reports

**Browser support**

|                         | Run a scan? | View results? |
|-------------------------|:-----------:|:-------------:|
| Chrome / Edge / Brave / Opera (desktop) | ✅ | ✅ |
| Firefox (desktop)       | ❌          | ✅            |
| Safari (desktop)        | ❌          | ✅            |
| Mobile (any)            | ❌          | ✅            |

File **contents** are never read by the browser — only the metadata listed above. That means no backup data crosses the wire to the hosted app.

---

## Features

- **Browser-side scanning** via File System Access API — no agent, no cifs mount, no on-prem component
- **Three reports per scan** with per-file size delta vs. previous scan
- **Weekly rotation** with auto-generated round-robin and manual override
- **Missed-week / partial-week alerts** on the dashboard
- **Per-path roll-up** showing the most recent scan status for every folder
- **Light / dark mode** with system, light, dark options per user; remembers across sessions
- **Mobile-responsive** dashboard (scans require desktop, but everything else works on a phone)
- **Immutable audit log** of every login, scan, export, and admin action
- **CSV + PDF export** with color-coded rows
- **Configurable branding** — company name, tagline, logo, footer credit
- **Local + LDAP authentication** with optional **TOTP 2FA** per user
- **Login rate limiting** and standard security headers (CSP, HSTS, X-Frame-Options, etc.)

---

## Quickstart in GitHub Codespaces

1. Push this repository to GitHub
2. Click **Code → Create codespace on main**
3. Wait for the postCreateCommand to finish (installs Postgres, creates the dev DB, runs migrations)
4. In the Codespace terminal:
   ```bash
   flask --app app create-admin
   ```
   Pick a username, full name, password (12+ chars). This is your first administrator.
5. Start the dev server:
   ```bash
   flask --app app --debug run --host 0.0.0.0 --port 8080
   ```
6. Open the forwarded port 8080 from the Codespace **Ports** tab

Sign in with the admin you created. Visit `/admin/users` to add operators, `/admin/rotation` to set the weekly rotation, and `/admin/settings` to upload your company logo and set the tagline.

To run a real scan, you must open the forwarded port URL **in Chrome or Edge** — Codespaces' default in-browser preview can be a stripped-down WebView that doesn't expose the File System Access API.

---

## Project layout

```
vigil2/
├── app/                 Flask app (factory pattern)
│   ├── __init__.py      create_app() + CLI commands
│   ├── config.py        env-driven config (Railway-aware)
│   ├── extensions.py    db, migrate, csrf, limiter
│   ├── models.py        SQLAlchemy models
│   ├── auth.py          argon2, TOTP, LDAP, decorators
│   ├── audit.py         immutable audit log helper
│   ├── scanner.py       diff logic for incoming scans
│   ├── rotation.py      weekly rotation + missed-week analytics
│   ├── storage.py       S3 / R2 abstraction for logos
│   ├── exports.py       CSV + PDF generation
│   ├── routes_auth.py   login, 2FA, profile, theme
│   ├── routes_main.py   dashboard, scan flow, exports
│   ├── routes_admin.py  users, rotation, settings, audit
│   ├── routes_api.py    /api/scan/submit (browser → backend)
│   ├── static/          CSS, JS (theme, mobile nav, scanner)
│   └── templates/       Jinja2 templates with light/dark theming
├── migrations/          Alembic migrations (committed)
├── .devcontainer/       Codespaces config + setup script
├── .env.example         Local development env template
├── Procfile             Railway / Heroku-style entrypoint
├── railway.json         Railway-specific deploy config
├── requirements.txt     Python dependencies (pinned ranges)
└── DEPLOY.md            Railway deployment walkthrough
```

---

## Daily use

Once everything's set up:

1. **Operator on duty** opens Vigil, clicks **New scan**
2. Clicks **Browse**, picks the backup folder on their mapped drive
3. Optionally types a label ("Mars · daily archive")
4. Clicks **Run scan** — browser walks the folder, posts to backend, redirects to the scan view
5. Reviews the three reports; exports CSV or PDF if needed
6. Dashboard updates: this week's coverage bar moves up, the path's roll-up reflects the new state

Admins manage users, rotation, branding, and review the audit log under `/admin/`.

---

## CLI commands

```bash
flask --app app create-admin            # Create the first admin user
flask --app app reset-password <user>   # Reset a local user's password
flask --app app db migrate -m "..."     # Generate a new schema migration
flask --app app db upgrade              # Apply pending migrations
flask --app app db downgrade            # Roll back one migration
```

---

## Security notes

- Passwords use **argon2id** (argon2-cffi default parameters)
- Optional **TOTP 2FA** per user; admins should enable it for themselves
- **Login rate limit** defaults to 5/min/IP (configurable via `LOGIN_RATE_LIMIT`)
- **CSRF protection** on every state-changing form
- **Security headers**: CSP, X-Frame-Options DENY, X-Content-Type-Options nosniff, Referrer-Policy same-origin
- **HSTS** enabled when `SESSION_COOKIE_SECURE` is on (auto-on under Railway)
- **Audit log** is append-only; no UI deletes entries

For a production financial-institution deployment, also:

- Confirm with InfoSec that filenames + paths transiting to a cloud-hosted app is acceptable (file *contents* never transit, but path strings do)
- Set `SECRET_KEY` from a strong random source in Railway env vars
- Enable per-admin TOTP from `/profile`
- Review the audit log periodically (`/admin/audit`)

---

## License

Internal tool — not licensed for redistribution.

Designed by Omar McLeod · 2026
