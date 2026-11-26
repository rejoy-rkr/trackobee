# Trackobee
Multi-tenant work-time and availability tracker: embeddable website client + endpoint agents + live dashboard.
Hosted on your own laptop (Cloudflare Tunnel for public HTTPS) with the database in Supabase (Postgres).

## Architecture
`PCs (agent) / CRM website (embed.js)` → `https://your-hostname (Cloudflare)` → `laptop: waitress on 127.0.0.1:8090` → `Supabase Postgres`

## Setup on the laptop
    git clone <repo> && cd trackobee
    sh deploy/setup.sh                       # venv + deps + .env
    nano .env                                # DATABASE_URL (Supabase), TB_PUBLIC_URL
    .venv/bin/python manage.py check         # connects to Supabase, creates tables (RLS enabled)
    .venv/bin/python manage.py create-owner you@yourco.com
    sh deploy/install-services.sh            # auto-start Trackobee (+ tunnel)

### Supabase
1. Create a project. **Project Settings → Database → Connection string → Transaction pooler** (works over IPv4). Put it in `.env` as `DATABASE_URL`.
2. Tables are created on first start with Row Level Security **enabled and no policies**, so Supabase's public REST API can't read them; only this server can.
3. Heartbeats are pruned after `TB_RETENTION_DAYS` (default 60) to stay within Supabase's storage limits.

### Cloudflare Tunnel (public HTTPS to the laptop; needed because the CRM is HTTPS)
You need a domain on Cloudflare (free plan is fine). Install `cloudflared` from
https://github.com/cloudflare/cloudflared/releases (or `apt`), then:

    cloudflared tunnel login
    cloudflared tunnel create trackobee            # prints TUNNEL-UUID, writes ~/.cloudflared/TUNNEL-UUID.json
    cloudflared tunnel route dns trackobee trackobee.yourco.com
    cp deploy/cloudflared-config.yml.example ~/.cloudflared/config.yml   # edit hostname, user, UUID
    sh deploy/install-services.sh

Set `TB_PUBLIC_URL=https://trackobee.yourco.com` in `.env` and restart: `systemctl --user restart trackobee`.
Quick test without a domain: `cloudflared tunnel --url http://127.0.0.1:8090` (random URL that changes on restart: not for real deployments).

### Keep the laptop awake
The server is down while the laptop sleeps (agents retry; the gap shows as offline). Disable sleep/lid-suspend:
`sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target` and set
`HandleLidSwitch=ignore` + `HandleLidSwitchExternalPower=ignore` in `/etc/systemd/logind.conf`, then `sudo systemctl restart systemd-logind`.

## Workspaces are approved manually
- Visitors use **Request a workspace** on the sign-in page. Nothing is created and nobody can sign in until you approve.
- Sign in as the owner (`create-owner`) → **owner console** (`/owner`): approve/reject requests, create workspaces manually,
  suspend/activate (suspended = users logged out, agents rejected), delete (removes all data). `TB_ALLOW_SIGNUP=0` hides the request form.
- Approving makes the requester the **Admin** of a new workspace with its own agent token + CRM secret.

## Roles inside a workspace
| Role | Can do |
|---|---|
| Admin | everything: users, keys, settings, facilities, deploy, view |
| Sub-admin | facilities, deploy (embed + installers), allowed origins, view |
| Read-only | view dashboards, reports, facilities |

## After a workspace admin signs in
1. **Facilities**: map public IP / CIDR to sites. 2. **Settings**: add website origins (e.g. `https://customercentral.co`).
3. **Deploy**: website embed (`<script>` + token-signing code for Python/PHP/Node) and per-organization endpoint installers (Windows/macOS/Linux).
4. **Users** (admin): add sub-admins and read-only users.

## Packaged agents (optional, removes the Python requirement on PCs)
Build generic binaries via `.github/workflows/build-agent.yml` (no secrets) or `packaging/build_windows.bat` / `packaging/build_macos.sh`;
put `trackobee-setup.exe`, `trackobee-agent-macos`, `trackobee-agent-linux` in `./installers/`. Sign them to avoid SmartScreen/Gatekeeper warnings.

## Development
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python app.py` (SQLite in `./data` when `DATABASE_URL` is empty).

## Security notes
scrypt password hashes, HttpOnly sessions, CSRF header on state-changing calls, login/signup throttling, per-organization data scoping and keys,
HMAC-signed CRM user links, agent port restricted to your origins, server bound to localhost (only the tunnel reaches it).
Tell employees what is collected (no screenshots, keystrokes or content) and get consent where the law requires it.
