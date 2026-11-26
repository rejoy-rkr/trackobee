// Trackobee events API: a website's backend reports "user X logged in / logged out" (and the user's public IP).
//   POST https://<project>.supabase.co/functions/v1/tb-events
//   Authorization: Bearer <organization API key>          (or header  x-api-key: <key>)
//   { "event": "login" | "logout", "user": "priya", "ip": "203.0.113.5", "host": "optional machine name", "ts": optional unix seconds }
// JSON or form-encoded. Server-to-server: never put the API key in browser code.
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-api-key, content-type, apikey, x-client-info",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const json = (o: unknown, status = 200) =>
  new Response(JSON.stringify(o), { status, headers: { ...CORS, "Content-Type": "application/json" } });

const LOGIN = new Set(["login", "signin", "sign_in", "sign-in", "logon", "session_start", "session.start", "user.login", "user_login", "logged_in"]);
const LOGOUT = new Set(["logout", "signout", "sign_out", "sign-out", "logoff", "session_end", "session.end", "user.logout", "user_logout", "logged_out"]);
const pick = (b: Record<string, unknown>, ...keys: string[]) => { for (const k of keys) { const v = b[k]; if (v !== undefined && v !== null && v !== "") return v; } return undefined; };
const text = (v: unknown, max: number) => (v === undefined ? "" : String(v).trim().slice(0, max));

// deno-lint-ignore no-explicit-any
export async function handle(req: Request, admin: any): Promise<Response> {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ ok: false, error: "use POST" }, 405);

  const raw = await req.text();
  if (raw.length > 8192) return json({ ok: false, error: "payload too large" }, 413);
  let b: Record<string, unknown> = {};
  try {
    const ct = req.headers.get("content-type") ?? "";
    b = ct.includes("application/x-www-form-urlencoded") ? Object.fromEntries(new URLSearchParams(raw)) : JSON.parse(raw || "{}");
    if (typeof b !== "object" || b === null || Array.isArray(b)) throw new Error("bad");
  } catch { return json({ ok: false, error: "body must be a JSON object (or form-encoded)" }, 400); }

  const bearer = (req.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "").trim();
  const key = bearer || req.headers.get("x-api-key")?.trim() || text(pick(b, "api_key", "apikey"), 200);
  if (!key) return json({ ok: false, error: "missing API key (Authorization: Bearer <key>)" }, 401);

  const ev = text(pick(b, "event", "type", "action"), 40).toLowerCase();
  const event = LOGIN.has(ev) ? "login" : LOGOUT.has(ev) ? "logout" : "";
  if (!event) return json({ ok: false, error: 'event must be "login" or "logout"' }, 400);
  const user = text(pick(b, "user", "username", "email", "user_id", "userid"), 100);
  if (!user) return json({ ok: false, error: "user is required" }, 400);

  let ts: number | null = Number(pick(b, "ts", "timestamp") ?? NaN);
  if (!Number.isFinite(ts)) ts = null; else { if (ts > 1e12) ts = ts / 1000; ts = Math.floor(ts); }
  const { data, error } = await admin.rpc("tb_website_event", {
    p_api_key: key, p_event: event, p_user: user, p_ip: text(pick(b, "ip", "ip_address", "client_ip"), 64) || null,
    p_host: text(pick(b, "host", "machine", "computer"), 100) || null, p_ts: ts,
  });
  if (error) return json({ ok: false, error: "could not record the event" }, 500);
  if (data?.ok) return json(data);
  const status = data?.error === "invalid api key" ? 401 : data?.error === "rate limited" ? 429 : 400;
  return json(data ?? { ok: false }, status);
}
