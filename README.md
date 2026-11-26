# Trackobee
Multi-tenant work-time and availability tracker: an embeddable website client, endpoint agents and a live dashboard.
**Backend = Supabase** (Postgres + row-level security + one Edge Function). **Website = static files on GitHub Pages.** No server to run.

```
PC agent ──► Supabase RPC tb_ingest_heartbeat ──► Postgres (RLS per organization) ◄── dashboard (GitHub Pages, supabase-js)
CRM page (embed.js) ─► local agent (127.0.0.1:47821)        owner/admin actions that need secrets ─► Edge Function tb-manage-users
```

## Layout
| Path | What |
|---|---|
| `supabase/migrations/` | schema, RLS policies, RPC functions (all objects prefixed `tb_`) |
| `supabase/functions/tb-manage-users/` | Edge Function: create/delete users, reset passwords, create workspaces |
| `site/` | static website (login, dashboard, owner console, `embed.js`, installer generator) |
| `agent/`, `packaging/` | the endpoint agent and its PyInstaller build scripts |
| `.github/workflows/deploy.yml` | applies migrations + deploys the function, builds agents, publishes Pages |
| `app.py`, `serve.py`, `manage.py`, `static/`, `deploy/` | the earlier self-hosted Flask backend (still works; not used by the Supabase setup) |

## How access works
- **Workspaces are approved manually.** A visitor requests a workspace (creates a Supabase Auth user with no access). The platform **owner** approves in `owner.html`; only then is an organization created and that user becomes its **Admin**.
- Roles: **Admin** (everything), **Sub-admin** (facilities, deploy, origins, view), **Read-only** (view). Enforced by Postgres RLS, not by the page.
- Agents authenticate with a per-organization **agent token**; website users are linked to PCs with a short-lived **HMAC-signed token** that your CRM backend creates (secret never leaves your server).
- Secrets (`tb_org_secrets`) are readable only by the workspace admin. Heartbeat tables can only be written by the ingest function.

## One-time setup
1. **Supabase project** (existing or new). Dashboard settings:
   - Authentication → Providers → Email: **turn OFF "Confirm email"**, set minimum password length **10**. (Workspace requests create the account immediately; the owner's approval is the gate.)
   - Database → Extensions: enable **pg_cron** (auto-deletes heartbeats older than 60 days; optional).
2. **Apply the schema + function** (either of):
   - CI: add repo secrets `SUPABASE_ACCESS_TOKEN`, `SUPABASE_DB_PASSWORD` and repo variable `SUPABASE_PROJECT_REF`, then push (or run the workflow), or
   - local: `supabase link --project-ref <ref> && supabase db push && supabase functions deploy tb-manage-users`.
3. **Create the platform owner:** Authentication → Users → *Add user* (email + password), then in the SQL editor: `select tb_make_owner('you@example.com');`
4. **Website:** repo variables `SUPABASE_URL`, `SUPABASE_ANON_KEY` (public values; Project Settings → API), optional `SITE_URL` (custom domain). Settings → Pages → Source: **GitHub Actions**. Push → the workflow publishes the site.
   *GitHub Pages from a **private** repo needs a paid GitHub plan; on the free plan the repo must be public (it contains no secrets), or host `site/` on Cloudflare Pages / Netlify instead.*

## Using it
1. Owner signs in (`/login.html` → redirected to the owner console), approves workspace requests.
2. Workspace admin: **Facilities** (map public IP/CIDR → site), **Settings** (add your website origin, e.g. `https://customercentral.co`), **Deploy**:
   - *Website embed*: paste the `<script>` on pages for logged-in users + add the token-signing function (Python/PHP/Node, pre-filled with your org id and secret) to your backend.
   - *Endpoint installer*: download the Windows/macOS/Linux installer (built in the browser from your org settings) and run it, or push it with GPO/Intune/Jamf.
3. **Users** (admin): add sub-admins / read-only users with a temporary password.

## Agents
Python agent works everywhere with Python 3. The deploy workflow builds Windows and Linux binaries (no Python needed) and serves them under `/installers/`; the generated installers use them automatically. macOS uses the system Python until you run the manual "Build macOS agent" workflow. Sign them to avoid SmartScreen/Gatekeeper warnings.

## Tests
`supabase/` SQL is covered by a 74-check suite (roles, RLS isolation, ingest, report math, owner flow) run against Postgres; the Edge Function by a 30-check Deno suite with a fake Supabase client.

## Security notes
RLS on every table (no anonymous table access), `security definer` functions with explicit role checks and fixed search_path, execute privileges revoked from `public`, privileged actions re-verified server-side in the Edge Function, agent port restricted to your origins.
Tell employees what is collected (host, IPs, OS user, lock/idle state, CRM user; no screenshots/keystrokes/content) and get consent where the law requires it.
