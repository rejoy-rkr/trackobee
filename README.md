# Trackobee
Know who is working where, **from any website**. Trackobee links the people who sign in to *your* website or app with the computers they sit at,
and gives you live availability by facility, screen-on time, and a login/logoff history for every endpoint.
Backend = **Supabase** (Postgres + row-level security + two small Edge Functions). Website = static files on **GitHub Pages**. No server to run.

```
website backend ──login/logout──► tb-events API ─┐
PC agent ──heartbeats / session events──────────► Postgres (RLS per organization) ◄── dashboard (GitHub Pages)
website page (embed.js, optional) ► local agent ─┘            owner/admin actions ─► tb-manage-users
```

## For your customers (any website, any sign-in)
1. **Create a workspace** on the sign-up page (instant; the platform owner can optionally require approval). They become the Admin and invite their team.
2. **Install the agent** on PCs (generated Windows/macOS/Linux installer, or push it with GPO/Intune/Jamf).
3. **Send login/logout events** from their website with one server-side call:
   ```bash
   curl -X POST "https://<project>.supabase.co/functions/v1/tb-events" \
     -H "Authorization: Bearer <org API key>" -H "Content-Type: application/json" \
     -d '{"event":"login","user":"priya","ip":"203.0.113.5"}'      # and {"event":"logout","user":"priya"} on sign-out
   ```
   `ip` is the user's public IP (usually `X-Forwarded-For`). Optional: `host` (computer name) and `ts` (unix seconds). JSON or form-encoded; aliases like `signin`/`signout` work.
   Trackobee pairs the user with the one active PC reporting from that IP. Many PCs behind one IP? Pass `host`, or add the optional **embed** (one `<script>` + a signed token) for exact pairing.

## Employee tracking (what the dashboard shows)
- **Dashboard:** live status per employee (Available / Away / Locked / Offline, current application, battery, approximate location) and **Today**: worked and away time.
- **Reports:** any date range, worked hours per employee per day, totals and averages, time per application, CSV export.
  *Worked* = screen unlocked and keyboard/mouse used in the last **2 minutes**; *Away* = no activity for more than 2 minutes, or screen locked.
- **History:** login/logoff, lock/unlock and online/offline events for any date range.
- **Devices:** serial number, model, OS, CPU/RAM/disks, MACs, battery health, Wi-Fi, agent version, location. The agent sends this as free-form JSON
  (`tb_devices.info` / `telemetry` / `location`), so new fields need only an agent update, never a migration or a new installer.
- **Agents update themselves** from `<site>/agent/trackobee_agent.py` (tested, then swapped in and restarted), so installers are one-time.
- Application tracking records the application **name only** (never window titles, keystrokes, screenshots or content). Location is approximate (public IP, city level).
  Tell your employees what is collected.

## What is recorded and for how long
- **Login/logoff history for every endpoint:** PC sign-in / sign-out (clean `logoff` on shutdown), lock/unlock, offline/online (detected automatically when a PC stops reporting), and website login/logout (API or embed), each with source and detail.
- **Retention is configurable per workspace, minimum 3 days.** Defaults: login/logoff history **30 days**, activity data (reports) **14 days**. Cleanup runs nightly.

## Layout
| Path | What |
|---|---|
| `supabase/migrations/` | schema, RLS policies, RPC functions (all objects prefixed `tb_`) |
| `supabase/functions/tb-events/` | public events API (authenticates with the organization API key) |
| `supabase/functions/tb-manage-users/` | create/delete users, reset passwords, create workspaces (JWT-verified) |
| `site/` | static website: landing, sign-up, sign-in, dashboard, owner console, `embed.js`, installer generator |
| `agent/`, `packaging/` | the endpoint agent and its PyInstaller build scripts |
| `.github/workflows/deploy.yml` | applies migrations, deploys functions, builds agents, publishes Pages |
| `app.py`, `serve.py`, `manage.py`, `static/`, `deploy/` | the earlier self-hosted Flask backend (not used by the Supabase setup) |

## Roles
**Admin** everything · **Sub-admin** facilities, integration, installers, origins, view · **Read-only** view. Enforced by Postgres RLS, not the page.
The **platform owner** (you) has an owner console: toggle "require approval for new workspaces", approve/reject, suspend, delete.

## One-time setup (platform owner)
1. **Supabase project.** Authentication → Providers → Email: set minimum password length **10**.
   - **Confirm email:** leave ON if you configure SMTP (Authentication → SMTP; Supabase's built-in mailer only emails your own team and is rate-limited). New users confirm their email, then their workspace is created on first sign-in. Turn it OFF only if you want instant sign-up without email verification.
   - Database → Extensions: enable **pg_cron** (offline detection and retention cleanup).
2. **Apply schema + functions:** CI (secrets `SUPABASE_ACCESS_TOKEN`, `SUPABASE_DB_PASSWORD`, variable `SUPABASE_PROJECT_REF`) or locally:
   `supabase link --project-ref <ref> && supabase db push && supabase functions deploy tb-manage-users && supabase functions deploy tb-events --no-verify-jwt`.
3. **Create your owner login:** Authentication → Users → *Add user*, then in the SQL editor: `select tb_make_owner('you@example.com');`
4. **Publish the site:** repo variables `SUPABASE_URL`, `SUPABASE_ANON_KEY` (public values), optional `SITE_URL`; Settings → Pages → Source: *GitHub Actions*; push.
   (Pages from a private repo needs a paid GitHub plan; otherwise make the repo public: it contains no secrets.)

## Security notes
RLS on every table (no anonymous table access); `security definer` functions with explicit role checks, fixed `search_path`, execute revoked from `public`;
organization API key / agent token / embed secret are per organization, admin-only and rotatable; the agent's local port accepts only your allowed origins;
website users are linked to PCs either by an HMAC-signed token (exact) or by the API (by IP / machine name). Heartbeat and event writes are only possible through the ingest functions.
Tell people what is collected (computer name, IPs, OS user, lock/idle state, the website user you report; no screenshots, keystrokes or content) and get consent where the law requires it.

## Tests
SQL: 125 checks (roles, RLS isolation, ingest, report math, owner flow, self-serve claim, events API pairing, retention, offline/online) run against Postgres.
Edge Functions: 30 + 33 checks under Deno with a fake Supabase client. Live end-to-end and real-browser suites were run against a Supabase project.
