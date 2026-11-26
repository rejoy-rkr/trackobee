# Trackobee
Multi-tenant work-time and availability tracker. Each organization signs up, becomes its own workspace, and gets
an embeddable website client plus endpoint installers. Agents report host, IPs, OS user, lock/idle state and the
website (CRM) user; the dashboard shows live availability by facility, daily screen-on time and login/logout events.

## Run
    pip install flask
    TB_PUBLIC_URL=https://trackobee.yourco.com TB_TRUST_PROXY=1 TB_SECURE_COOKIES=1 python3 app.py   # PORT=8090
Open the site → **Create a workspace** → you are the Admin. Data lives in `./data` (set `TB_DATA`). Put it behind HTTPS.
`TB_ALLOW_SIGNUP=0` closes public signup after you have created your workspace(s).

## Roles
| Role | Can do |
|---|---|
| Admin | everything: users, keys (agent token, CRM secret), settings, facilities, deploy, view |
| Sub-admin | facilities, deploy (embed + installers), allowed origins, view |
| Read-only | view dashboards, reports, facilities |

## After login
1. **Facilities** → map public IP / CIDR ranges to sites.
2. **Settings** → add your website origin(s), e.g. `https://customercentral.co`.
3. **Deploy**
   - *Website embed*: paste the `<script>` tag on pages for logged-in users; add the token-signing function (Python/PHP/Node, pre-filled with your org id and secret) to your backend.
     The page detects the OS, offers the install if the agent is missing, and links the website user to the PC.
   - *Endpoint installer*: download the per-organization script for Windows / macOS / Linux and run it (or push it by GPO / Intune / Jamf).
4. **Users** (admin) → add sub-admins and read-only users.

## Packaged agents (optional, removes the Python requirement)
Binaries are generic (no secrets); organization settings come from `org.json` written by the setup script.
Build on CI (`.github/workflows/build-agent.yml`, no secrets needed) or locally with `packaging/build_windows.bat` / `packaging/build_macos.sh`,
then put in `./installers/`: `trackobee-setup.exe`, `trackobee-agent-macos`, `trackobee-agent-linux`. Generated installers use them automatically,
otherwise they fall back to the Python agent. `trackobee.pkg` is for MDM; deliver `/Library/Trackobee/org.json` with it.
Sign the exe / sign + notarize the pkg to avoid SmartScreen / Gatekeeper warnings.

## Security notes
Passwords are hashed (scrypt), sessions are HttpOnly cookies, state-changing calls require a custom header (CSRF), logins are throttled,
all data is scoped per organization, agent tokens and CRM secrets are per organization and rotatable, the agent's local port only
accepts your origins, and CRM user links are HMAC-signed so they can't be spoofed from the browser.
Tell employees what is collected (no screenshots, keystrokes or content) and get consent where the law requires it.
