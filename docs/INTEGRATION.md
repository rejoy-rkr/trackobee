# Trackobee documentation

Trackobee shows **who is working, on which computer, and for how long**: live status, worked and away time per employee per day, applications used, and a hardware inventory of every PC. This guide covers installation, every API, the data model and operations.

**Contents**
1. [How it works](#1-how-it-works)
2. [Key concepts](#2-key-concepts)
3. [Quick start](#3-quick-start)
4. [Installing the agent](#4-installing-the-agent)
5. [What the agent collects](#5-what-the-agent-collects)
6. [Website integration](#6-website-integration)
7. [Agent ingest API](#7-agent-ingest-api)
8. [Reading your data (REST API)](#8-reading-your-data-rest-api)
9. [Data model](#9-data-model)
10. [Retention and storage](#10-retention-and-storage)
11. [Roles and permissions](#11-roles-and-permissions)
12. [Administration](#12-administration)
13. [Self-hosting and deployment](#13-self-hosting-and-deployment)
14. [Troubleshooting](#14-troubleshooting)
15. [Limits and platform notes](#15-limits-and-platform-notes)

---

## 1. How it works

```
 Employee PC                                  Your Supabase project                         You
 ┌────────────────────┐   heartbeat every 15s ┌───────────────────────────────┐
 │ Trackobee agent    │──────────────────────►│ Postgres + row-level security │◄──── Dashboard (GitHub Pages)
 │ (runs as the user) │   RPC tb_ingest_...   │  tb_hb  tb_ev  tb_devices ... │      sign in as admin / read-only
 └─────────▲──────────┘                       │                               │
           │ pair (optional)                  │  Edge Function tb-events      │◄──── Your website backend
 Your website page (embed.js)                 │  Edge Function tb-manage-users│      login / logout events
                                              └───────────────────────────────┘
```

* **Agent**: a small program on each PC. It reports every 15 seconds and updates itself. Windows, macOS and Linux.
* **Backend**: your own Supabase project. All data stays in your database. Access is controlled by Postgres row-level security, not by the web page.
* **Dashboard**: static files on GitHub Pages. It talks to Supabase directly.
* **Website integration** (optional): tells Trackobee which *website user* is sitting at which PC.

## 2. Key concepts

| Term | Meaning |
|---|---|
| **Workspace** | One organization's isolated data. Created from the sign-up page; the creator becomes its Admin. |
| **Employee** | A person, identified by the **website user** if the website integration is used, otherwise by the PC's **OS username**. |
| **Computer / host** | The machine name reported by the agent. One employee can use several computers. |
| **Worked** | Screen unlocked **and** keyboard/mouse used within the last **2 minutes**. |
| **Away** | Screen unlocked but **no activity for more than 2 minutes**. The dashboard's "Away" figure also includes locked time. |
| **Locked** | Screen locked. |
| **Offline** | The computer has not reported for more than 50 seconds. |
| **Available** | Online, unlocked, active within 2 minutes. |

Time is measured by heartbeats: each heartbeat is worth the time since the previous one for that employee and computer, capped at 45 seconds, so gaps while a PC is off are never counted as work.

The 2-minute rule lives in one database function, `tb_away_secs()`.

## 3. Quick start

1. **Create a workspace** at `<your site>/signup.html` (organization name, your name, email, password of at least 10 characters). You become the Admin.
2. Open **Install** and download the installer for each operating system.
3. Run the installer on each employee PC (no administrator rights needed). It confirms when the first report reaches the server.
4. Open **Dashboard**: each PC appears within about 15 seconds.
5. Open **Reports** for worked hours per day.
6. *(Optional)* connect your website, see [section 6](#6-website-integration).

## 4. Installing the agent

The installer is generated for your organization (it contains your organization's agent token), so download it from **Install** while signed in as Admin or Sub-admin. It only: writes `~/.trackobee/org.json`, downloads the agent, makes it start at every login, and waits until the first report reaches the server. Everything else ships through the agent's own self-update, so installers are one-time.

### Windows
Run `trackobee-setup.bat` (double-click). It downloads `trackobee-setup.exe`, which installs itself to `%LOCALAPPDATA%\Trackobee`, registers a per-user startup entry and starts. If no packaged `.exe` is available, the script falls back to Python 3 and a scheduled task.
Remove: run `trackobee-agent.exe --uninstall`.

### macOS
Run `sh trackobee-setup.command`. It installs a per-user LaunchAgent (`global.trackobee.agent`). Uses `/usr/bin/python3` unless a packaged binary is available.
Remove: `sh trackobee-setup.command uninstall`.

### Linux
Run `sh trackobee-setup.sh`. It installs a systemd **user** service (`trackobee.service`), or falls back to desktop autostart. It asks for `sudo` once, optionally, to read the hardware serial number (readable only by root on Linux).
Remove: `sh trackobee-setup.sh uninstall`.

### Silent / managed deployment (GPO, Intune, Jamf, scripts)
Put the organization settings in a system-wide file and deploy the agent binary; no per-user script is needed:

| OS | Settings file |
|---|---|
| Windows | `%PROGRAMDATA%\Trackobee\org.json` |
| macOS | `/Library/Trackobee/org.json` |
| Linux | `/etc/trackobee/org.json` |

Contents (copy from a downloaded installer):
```json
{ "server": "https://<you>.github.io/<repo>",
  "ingest": "https://<project>.supabase.co/rest/v1/rpc/tb_ingest_heartbeat",
  "apikey": "<Supabase anon (public) key>",
  "agent_token": "<organization agent token>",
  "allowed_origins": "https://your-website.com" }
```
The agent searches, in order: a baked-in `config.json`, `~/.trackobee/org.json`, then the system-wide file above.

### Files and ports
| Path / port | Purpose |
|---|---|
| `~/.trackobee/org.json` | organization settings |
| `~/.trackobee/config.json` | embed pairing token (mode 600) |
| `~/.trackobee/state.json` | `last_beat` timestamp, used by installers to verify |
| `~/.trackobee/hw.json`, `extra.json` | optional extra inventory fields merged as-is (see below) |
| `~/.trackobee/agent.py` (or `agent_update.py` for the packaged binary) | the agent / its self-updated copy |
| `127.0.0.1:47821` | local pairing API, loopback only (section 6.3) |

Adding custom inventory: write any JSON object into `~/.trackobee/extra.json` (for example `{"asset_tag":"EQL-0042","department":"Support"}`); it is merged into the device record on the next hourly inventory and appears in **Devices**.

### Self-update
Every 6 hours (first check one minute after start) the agent downloads `<server>/agent/trackobee_agent.py`. It installs it only if the version is newer, it compiles, and (for the script form) it passes its own `--selftest`. It then restarts itself. A restart or update does **not** create a sign-in/sign-out event. The packaged binary runs the downloaded code in place of its bundled copy.

## 5. What the agent collects

**Every heartbeat (15 s)**

| Field | Notes |
|---|---|
| host, OS username, LAN IP | public IP is seen by the server |
| state | `active` or `locked` |
| idle seconds | seconds since the last keyboard/mouse input |
| foreground application | the **name**, e.g. `Chrome`, `Excel`. For web browsers only, also the **page title** (e.g. `Inbox - Gmail`), never the URL; titles of other apps are not collected |
| telemetry | battery percent / charging / status, AC power, uptime, memory use %, free disk, Wi-Fi network, agent version |
| session events | sign-in, sign-out, lock, unlock (and website login/logout via embed) |

**Hourly (and at start): inventory** (stored in `tb_devices.info`)
manufacturer, model, **serial number**, board serial, UUID, BIOS version/date, OS and version, CPU, cores, RAM, disks (size, model, serial), GPU, MAC addresses, timezone, battery model/serial/health/cycles, monitors, antivirus (Windows), domain (Windows), and any fields from `extra.json`.

**Hourly: approximate location** (stored in `tb_devices.location`): city, region, country, latitude/longitude, ISP, derived from the public IP by a public geolocation service (`ipwho.is`, falling back to `ipapi.co`). City-level only; it is **not** GPS.

**Never collected:** keystrokes, screenshots, window titles, file contents, browsing history, clipboard, microphone, camera.

> Tell your employees what is collected. Employee monitoring is regulated in many places (for example GDPR in the EU, DPDP Act in India); a written notice and, where required, consent are your responsibility.

## 6. Website integration

Optional. It links a **website user** to the PC they are using, so the dashboard shows the person's website identity instead of the OS username. Two methods, usable together.

### 6.1 Events API (server to server)

Your website backend calls this when a user signs in or out.

```
POST https://<project>.supabase.co/functions/v1/tb-events
Authorization: Bearer <organization API key>        (or header  x-api-key: <key>,  or body field api_key)
Content-Type: application/json                      (form-encoded also accepted)
```
Find the endpoint and your API key under **Install** (Admin only). Keep the key server-side; never put it in browser code.

**Body**

| Field | Required | Aliases | Notes |
|---|---|---|---|
| `event` | yes | `type`, `action` | `login` or `logout`. Also accepted: `signin`, `sign_in`, `sign-in`, `logon`, `session_start`, `user.login`, `logged_in` / `signout`, `sign_out`, `sign-out`, `logoff`, `session_end`, `user.logout`, `logged_out` |
| `user` | yes | `username`, `email`, `user_id`, `userid` | max 100 characters |
| `ip` | recommended | `ip_address`, `client_ip` | the user's **public** IP. Pairing needs it unless `host` is given. Must be a single address |
| `host` | no | `machine`, `computer` | the PC's machine name: exact pairing, even when many PCs share one public IP |
| `ts` | no | `timestamp` | unix seconds (milliseconds also accepted); must be within the last 24 h and not more than 5 min ahead, else the server time is used |

**Pairing rules (`login`)**: with `host`, the user is paired to that PC if it reported in the last 24 h. Otherwise, with `ip`, the user is paired to the **one** machine currently reporting from that public IP; if zero or several machines match, the event is recorded unpaired with an explanation in `detail`. Pairings last 12 hours or until `logout`.

**Responses**
```json
{ "ok": true, "paired": true, "host": "PC-12", "detail": "paired by IP (one machine at this address)" }
```
| Status | Meaning |
|---|---|
| 200 | recorded (check `paired`) |
| 400 | invalid event, missing user, invalid ip, bad body |
| 401 | missing or invalid API key |
| 413 | body larger than 8 KB |
| 429 | rate limited (about 600 events per minute per organization) |
| 500 | could not record; retry |

**Examples**
```bash
curl -X POST "https://<project>.supabase.co/functions/v1/tb-events" \
  -H "Authorization: Bearer <API key>" -H "Content-Type: application/json" \
  -d '{"event":"login","user":"priya","ip":"203.0.113.5"}'
# on sign-out
curl -X POST ... -d '{"event":"logout","user":"priya"}'
```
```python
import requests
def trackobee(event, user, ip=None, host=None):      # event: "login" or "logout"
    requests.post("https://<project>.supabase.co/functions/v1/tb-events", timeout=5,
        headers={"Authorization": "Bearer <API key>"},
        json={"event": event, "user": user, "ip": ip, "host": host})
```
```js
async function trackobee(event, user, ip) {
  await fetch("https://<project>.supabase.co/functions/v1/tb-events", { method: "POST",
    headers: { Authorization: "Bearer <API key>", "Content-Type": "application/json" },
    body: JSON.stringify({ event, user, ip }) });
}
```
```php
function trackobee($event, $user, $ip = null) {
  $ch = curl_init("https://<project>.supabase.co/functions/v1/tb-events");
  curl_setopt_array($ch, [CURLOPT_POST => 1, CURLOPT_RETURNTRANSFER => 1, CURLOPT_TIMEOUT => 5,
    CURLOPT_HTTPHEADER => ["Authorization: Bearer <API key>", "Content-Type: application/json"],
    CURLOPT_POSTFIELDS => json_encode(["event" => $event, "user" => $user, "ip" => $ip])]);
  curl_exec($ch);
}
```
Tip: behind a proxy, pass the **first** address of `X-Forwarded-For` as `ip`.

### 6.2 Embed script (exact pairing)

Add to pages shown to signed-in users:
```html
<script src="https://<your site>/embed.js" data-token="SIGNED_TOKEN" async></script>
<!-- on sign-out, optionally: Trackobee.unpair() -->
```
The script detects the OS, offers the agent installer if the agent is missing, and hands the signed token to the local agent, which attaches it to its heartbeats. This pairs the user to **that exact PC**.

**Signed token format**
```
<organization public_id> . <base64url("user:expiry_unix")> . <hex HMAC-SHA256(secret, "<public_id>.<payload>")>
```
`public_id` and the signing secret are under **Install** (Admin only). Generate the token on your server; never expose the secret.
```python
import base64, hmac, hashlib, time
ORG_ID, SECRET = "<public_id>", "<embed signing secret>"
def trackobee_token(username, ttl=86400):
    p = base64.urlsafe_b64encode(f"{username}:{int(time.time())+ttl}".encode()).decode().rstrip("=")
    return f"{ORG_ID}.{p}." + hmac.new(SECRET.encode(), f"{ORG_ID}.{p}".encode(), hashlib.sha256).hexdigest()
```
```js
const crypto = require("crypto");
function trackobeeToken(user, ttl = 86400) {
  const p = Buffer.from(user + ":" + (Math.floor(Date.now()/1000) + ttl)).toString("base64url");
  return ORG_ID + "." + p + "." + crypto.createHmac("sha256", SECRET).update(ORG_ID + "." + p).digest("hex");
}
```
```php
function trackobee_token($user, $ttl = 86400) {
  $p = rtrim(strtr(base64_encode($user . ":" . (time() + $ttl)), "+/", "-_"), "=");
  return ORG_ID . "." . $p . "." . hash_hmac("sha256", ORG_ID . "." . $p, SECRET);
}
```
The page's origin (for example `https://your-website.com`) must be listed under **Settings → allowed origins**, and the agent installed after that (origins are written into the agent's settings at install time).

### 6.3 Local agent API
The agent listens on `127.0.0.1:47821` (loopback only; Host header and Origin allow-list enforced).

| Method, path | Purpose |
|---|---|
| `GET /status` | `{agent, version, paired, last_beat}` |
| `POST /pair` `{"token": "..."}` | attach a signed token (allowed origins only) |
| `POST /unpair` | detach |

## 7. Agent ingest API

The agent calls a Postgres function through Supabase's REST interface. You normally never call this yourself, but you can send data from other tools (for example a scripted device) the same way.

```
POST https://<project>.supabase.co/rest/v1/rpc/tb_ingest_heartbeat
apikey: <Supabase anon key>
Authorization: Bearer <Supabase anon key>
Content-Type: application/json
```
| Parameter | Type | Notes |
|---|---|---|
| `p_agent_token` | text | **required.** organization agent token (Install, Admin only) |
| `p_host` | text | **required.** machine name, max 100 |
| `p_ip` | text | LAN IP |
| `p_os_user` | text | **required.** OS username, max 100 |
| `p_state` | text | **required.** `active` or `locked` |
| `p_idle` | int | seconds since last input (0 or more) |
| `p_token` | text | optional embed token (section 6.2) |
| `p_events` | json array | up to 50 of `{"type","ts","user"}`; types: `login`, `logoff`, `lock`, `unlock`, `crm_login`, `crm_logout` |
| `p_app` | text | foreground application name |
| `p_sys` | json object | inventory, merged into `tb_devices.info` (max 30 KB) |
| `p_telemetry` | json object | latest readings, merged into `tb_devices.telemetry` (max 4 KB) |
| `p_location` | json object | stored in `tb_devices.location` (max 2 KB) |

Response: `{"ok": true, "interval": 15}`, or `{"ok": false}` for a bad token or invalid input.

```bash
curl -X POST "https://<project>.supabase.co/rest/v1/rpc/tb_ingest_heartbeat" \
  -H "apikey: <anon key>" -H "Authorization: Bearer <anon key>" -H "Content-Type: application/json" \
  -d '{"p_agent_token":"<token>","p_host":"PC-12","p_ip":"192.168.1.20","p_os_user":"priya","p_state":"active","p_idle":3,
       "p_app":"Excel","p_telemetry":{"battery_percent":80,"charging":true},"p_sys":{"serial_number":"ABC123","department":"Support"}}'
```
**Extensibility:** `p_sys`, `p_telemetry` and `p_location` are free-form. Any new key you send is stored and shown under **Devices** with no database change. Unknown keys never cause an error; `null` and empty values are ignored by the agent so they cannot erase stored data.

## 8. Reading your data (REST API)

Everything the dashboard shows is available to your own tools through the same Supabase REST API.

### Authenticate
Sign in as a dashboard user and use the returned access token (valid about an hour; refresh with the refresh token). **Create a dedicated Read-only user** (Team → Add user) for integrations.
```bash
curl -X POST "https://<project>.supabase.co/auth/v1/token?grant_type=password" \
  -H "apikey: <anon key>" -H "Content-Type: application/json" \
  -d '{"email":"reports@yourcompany.com","password":"..."}'
# -> {"access_token":"<JWT>", ...}
```
Send `apikey: <anon key>` and `Authorization: Bearer <JWT>` on every call. Row-level security limits you to your own organization's data.

### Functions (POST `/rest/v1/rpc/<name>`)

**`tb_work_summary`**: worked/away/locked per employee per day
```bash
curl -X POST ".../rest/v1/rpc/tb_work_summary" -H "apikey: ..." -H "Authorization: Bearer <JWT>" -H "Content-Type: application/json" \
  -d '{"p_start":1795700000,"p_end":1796300000,"p_tz":"Asia/Kolkata"}'
```
`p_start` inclusive and `p_end` exclusive are unix seconds; `p_tz` is an IANA zone used to cut days (default `UTC`).
Returns rows: `day` (date), `usr`, `hosts` (array), `worked`, `away`, `locked` (seconds; "Away" in the dashboard = `away + locked`), `first_ts`, `last_ts`.

**`tb_app_usage`** `{p_start, p_end}`: rows `usr`, `app`, `seconds` (only time while working).

**`tb_live_status`** `{}`: one row per computer: `host, ip, pub_ip, os_user, crm_user, facility, state` (`available`/`away`/`locked`/`offline`), `idle, last_seen, mismatch, app, battery, charging, model, location, facility_source` (`auto` or `manual`).

**`tb_set_device_facility`** `{p_host, p_facility}`: move a computer to a facility (Admin / Sub-admin only; see section 12).

**`tb_daily_report`** `{p_start, p_end}`: per user and computer: `usr, host, facility, ips, first_ts, last_ts, screen_on, idle, locked`.

### Tables (GET `/rest/v1/<table>`)
`tb_devices`, `tb_ev` (events), `tb_hb` (heartbeats), `tb_facilities`, `tb_profiles` are readable; PostgREST filters apply.
```bash
# serial numbers and models of all computers
curl ".../rest/v1/tb_devices?select=host,info->>serial_number,info->>model,telemetry->>battery_percent" -H "apikey: ..." -H "Authorization: Bearer <JWT>"
# one employee's events on a day
curl ".../rest/v1/tb_ev?crm_user=eq.priya&ts=gte.1795700000&ts=lt.1795786400&order=ts.desc" -H "apikey: ..." -H "Authorization: Bearer <JWT>"
```
**Limits:** responses are capped at **1000 rows** per request; page with `Range: 0-999`, `1000-1999`, … (or `limit`/`offset`).

## 9. Data model

| Table | One row per | Key columns |
|---|---|---|
| `tb_orgs` | workspace | `name`, `public_id`, `allowed_origins`, `active`, `event_retention_days` (3-365), `hb_retention_days` (3-90) |
| `tb_org_secrets` | workspace (Admin only) | `agent_token`, `api_key`, `crm_secret` |
| `tb_profiles` | dashboard user | `user_id`, `org_id`, `email`, `name`, `role` (`owner`/`admin`/`sub_admin`/`read_only`), `active` |
| `tb_hb` | heartbeat | `org_id, ts, host, ip, pub_ip, os_user, crm_user, facility, state, idle, app` |
| `tb_ev` | event | `org_id, ts, host, pub_ip, os_user, crm_user, facility, type, source, detail` |
| `tb_devices` | computer | `host`, `info` (json), `telemetry` (json), `location` (json), `agent_version`, `facility_override` (`NULL` automatic, `__none__` no facility, or a facility name), `first_seen`, `updated_at` |
| `tb_facilities` | site | `name`, `cidr` |
| `tb_pairings` | website user to PC | `host`, `crm_user`, `paired_at` (kept 12 h) |

Event `type` values: `login`, `logoff` (PC session), `lock`, `unlock`, `online`, `offline` (system-detected), `crm_login`, `crm_logout` (website). `source`: `agent`, `embed`, `api`, `system`.

All timestamps are unix seconds. All objects are prefixed `tb_`.

## 10. Retention and storage

* **Activity data** (`tb_hb`, drives reports): 14 days by default, configurable 3 to 90 days (**Settings, Data retention**).
* **Login/logoff history** (`tb_ev`): 30 days by default, configurable 3 to 365 days.
* Cleanup runs nightly (03:17 UTC) with `pg_cron`; offline detection runs every minute. Enable the `pg_cron` extension in Supabase (Database, Extensions), otherwise nothing is deleted and no offline events are created.
* Storage estimate: one computer sends about 5,800 heartbeats per day, roughly 1.5 to 2 MB per day including indexes. Longer retention multiplies this; check your Supabase plan's database size.
* Reports cannot go back further than the activity-data retention.

## 11. Roles and permissions

| Capability | Admin | Sub-admin | Read-only |
|---|:-:|:-:|:-:|
| Dashboard, reports, devices, per-employee history | yes | yes | yes |
| Facilities (add/remove segments) and moving computers between facilities | yes | yes | no |
| Download installers, see integration page | yes | yes | no |
| Allowed website origins | yes | yes | no |
| Organization name, retention | yes | no | no |
| See and rotate keys (agent token, API key, embed secret) | yes | no | no |
| Add, disable, delete users, reset passwords, change roles | yes | no | no |

The **platform owner** (a separate role with no workspace) manages workspaces: approve or reject sign-ups, suspend, delete, and toggle "require approval". At least one active Admin is always kept per workspace.

## 12. Administration

* **Keys**: Settings shows the agent token, website API key and embed signing secret. *Rotate* issues a new one immediately. Rotating the agent token disconnects installed agents until they are reinstalled; the API key and embed secret need your backend updated.
* **Users**: Team tab (Admin). Roles: Admin, Sub-admin, Read-only. Passwords need 10 or more characters.
* **Facilities** (Facilities tab): a facility is a site. Choose how it is identified, then add as many entries as you need under the same name:
  **Local network (LAN)**: a segment such as `192.168.6.*` or `192.168.6.0/24`; **IP range**: a start and end address (for example `192.168.6.10` to `192.168.6.50`), converted automatically to the exact network blocks; **Public IP**: the office's internet address (`103.249.205.154`), covering every computer behind it. The form rejects a public address under "Local network" and the reverse.
  Accepted LAN forms: `192.168.6.0/24`, `192.168.6.*` (same thing), `192.168.6` (three octets mean /24), `10.*` (/8), or a single IP (/32, matches one computer only; you are asked to confirm).
  Host bits are cleaned up for you (`192.168.6.5/24` is stored as `192.168.6.0/24`).
  **Matching order:** the computer's **LAN segment** first, then its **public IP**; within one IP the most specific (longest) range wins. Matching is evaluated live, so adding or editing a facility re-maps every computer immediately.
  The Facilities tab lists **unmapped computers grouped by network**; name a group and click *Add* to create its facility.
* **Moving a computer by hand**: on the Dashboard click an employee, or open the Devices tab, and use the **Facility** drop-down:
  *Automatic (by network)* (default), any facility, or *No facility*. Manual assignments are marked ✎ on the Dashboard and survive reinstalls; removing the last segment of a facility returns its computers to Automatic.
  Admins and Sub-admins can move computers. Via API: `rpc/tb_set_device_facility {"p_host":"PC-12","p_facility":"Branch 6"}` (`null` = automatic, `""` = no facility).
* **Searching devices** (Devices tab) works on **any attribute** the agent reports, including ones you add yourself:

  | You type | Meaning |
  |---|---|
  | `lenovo` | any attribute contains `lenovo` |
  | `os:windows` | an attribute whose name contains `os` contains `windows` |
  | `serial=PF123` | exact match |
  | `ram_gb>8`, `battery_percent<20` | numeric comparison (`>`, `<`, `>=`, `<=`) |
  | `-os:windows` | exclude |
  | `manufacturer:lenovo serial:PF` | several terms: all must match |

  The *in all attributes* drop-down limits plain words to one attribute; its list is built from what your computers actually report. The facility drop-down filters by site. The Dashboard has its own search plus facility and status filters.
* **Allowed origins**: websites allowed to talk to the local agent (one per line, `https://host[:port]`). Written into agents at install time.
* **Approval of new workspaces** (platform owner): when on, sign-ups wait for approval.

## 13. Self-hosting and deployment

1. **Supabase project.** Authentication, Providers, Email: set minimum password length **10**. Database, Extensions: enable **pg_cron**.
2. **Schema and functions**: `supabase link --project-ref <ref>`, then `supabase db push --remote` and `supabase functions deploy tb-manage-users` and `supabase functions deploy tb-events --no-verify-jwt`.
3. **Owner login**: Authentication, Users, add a user, then in the SQL editor `select tb_make_owner('you@example.com');`
4. **Site**: GitHub repository variables `SUPABASE_URL`, `SUPABASE_ANON_KEY` (both public values), optional `SITE_URL`. Settings, Pages, Source: *GitHub Actions*. Push to `main`.
5. **Automatic migrations from CI**: variable `SUPABASE_PROJECT_REF` and secrets `SUPABASE_ACCESS_TOKEN` (create at supabase.com/dashboard/account/tokens) and `SUPABASE_DB_PASSWORD`. Without the secrets the database job fails (the site still publishes).

The workflow (`.github/workflows/deploy.yml`) applies migrations, deploys the functions, builds the Windows and Linux agent binaries and publishes the site. A macOS binary can be built on demand with the *Build macOS agent* workflow.

## 14. Troubleshooting

| Symptom | Check |
|---|---|
| Installer says no report reached the server | The PC must reach the site URL and `*.supabase.co` over HTTPS. Check proxy or firewall. Windows: `%USERPROFILE%\.trackobee\state.json`; Linux: `journalctl --user -u trackobee`. |
| Computer shows **Offline** | No heartbeat for 50 seconds: PC off, asleep, no internet, or the agent stopped. |
| **Away** or **Locked** while the person works | Idle detection per OS, section 15. On Linux/Wayland desktops idle is estimated from input interrupts; USB-only input on some systems may be missed. |
| Application column empty | Windows and macOS: should always work. Linux: KDE Wayland and X11 are supported; GNOME on Wayland does not expose the active window to other programs. |
| Serial number empty on Linux | Re-run the installer and enter your sudo password when asked, or write `{"serial_number":"..."}` into `~/.trackobee/extra.json`. |
| Computer shows **Unmapped** | Facilities are matched by network segment. Click the employee on the Dashboard to see the computer's Local IP, then add that segment, for example `192.168.6.0/24`. A single IP like `192.168.6.5` only matches that one computer. The Facilities tab suggests segments for every unmapped computer, or move the computer by hand with the Facility drop-down. |
| Computer is in the wrong facility | A home network using the same private range as an office (both `192.168.1.x`, say) looks identical to the LAN check. Move that computer by hand. |
| Wrong or empty location | Location comes from the public IP and is city level; VPNs and mobile networks shift it. |
| Website user not shown | Events API: pass the PC's `host`, or make sure only one PC reports from that public IP. Embed: the origin must be in Allowed origins and the agent reinstalled afterwards. |
| Events API returns 401 | The key was rotated or copied incorrectly. Look under Install (Admin). |
| Dashboard says "config.json is not set up" | The repository variables `SUPABASE_URL` and `SUPABASE_ANON_KEY` are missing; redeploy. |
| Reports empty for old days | Beyond the activity-data retention (default 14 days). |

## 15. Limits and platform notes

| | Windows | macOS | Linux |
|---|---|---|---|
| Lock detection | yes | yes | GNOME, KDE and most desktops (D-Bus / logind) |
| Idle detection | exact | exact | GNOME exact, X11 exact, KDE/other Wayland estimated |
| Foreground application | yes | yes | KDE (Wayland and X11), X11; not GNOME-Wayland |
| Serial number | yes | yes | needs one-time sudo at install |
| Battery | yes | yes | yes |
| Wi-Fi name | yes | yes | NetworkManager or `iwgetid` |

* Heartbeat interval: 15 seconds. Offline after 50 seconds without one.
* The agent runs as the signed-in user, needs no administrator rights, and uses only the language's standard library.
* Several people using one computer at the same time (terminal servers, fast user switching): the agent listens on one local port per machine, so only the first user's agent runs. Per-session tracking for shared servers is not supported yet.
* Heartbeat history and reports are kept per employee identity; if an employee's website username changes, they appear as a new employee.
