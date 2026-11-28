// Trackobee browser client for Supabase. Exposes window.TB and a route-style api() used by the pages.
(function () {
  let cfg, sb, ME = null, ORG_ID = null, USERS = [], DEVC = null, DEVT = 0;
  const fail = (e) => { throw new Error(String((e && e.message) || e || "Request failed").replace(/^Error:\s*/, "")); };
  const ok = (r) => { if (r.error) fail(r.error); return r.data; };
  const dayRange = (d) => { const s = new Date(d + "T00:00:00"); return [Math.floor(s / 1000), Math.floor(s / 1000) + 86400]; };
  const siteUrl = () => (cfg.siteUrl || new URL(".", location.href).href).replace(/\/$/, "");

  async function init() {
    if (sb) return TB;
    cfg = await (await fetch("config.json", { cache: "no-store" })).json();
    if (!cfg.supabaseUrl || cfg.supabaseUrl.includes("YOUR-PROJECT")) throw new Error("config.json is not set up yet (supabaseUrl / anonKey).");
    sb = window.supabase.createClient(cfg.supabaseUrl, cfg.anonKey, { auth: { persistSession: true, autoRefreshToken: true } });
    return TB;
  }
  async function status() {                         // no session (or an expired one) => "anon", never an error page
    const s = await sb.auth.getSession(); if (!s.data || !s.data.session) return { status: "anon" };
    const r = await sb.rpc("tb_my_status");
    if (r.error) { if (/jwt|permission denied|not authenticated/i.test(r.error.message || "")) return { status: "anon" }; fail(r.error); }
    if (r.data && r.data.status === "none") return ok(await sb.rpc("tb_claim_workspace"));   // signed up with org details, workspace not created yet
    return r.data;
  }
  async function fn(body) {                       // privileged actions run in the tb-manage-users edge function
    const r = await sb.functions.invoke("tb-manage-users", { body });
    if (r.error) { let msg = r.error.message; try { msg = (await r.error.context.json()).error || msg; } catch (e) {} fail(msg); }
    return r.data;
  }
  async function listUsers() {
    const rows = ok(await sb.from("tb_profiles").select("user_id,email,name,role,active,created_at").not("org_id", "is", null).order("created_at"));
    USERS = rows.map(u => ({ id: u.user_id, email: u.email, name: u.name, role: u.role, active: u.active ? 1 : 0 })); return USERS;
  }
  async function facilities() { return ok(await sb.from("tb_facilities").select("id,name,cidr").order("name")); }
  async function ownerSummary() { return ok(await sb.rpc("tb_owner_summary")); }
  async function packaged() {
    const out = {}; const f = { windows: "trackobee-setup.exe", mac: "trackobee-agent-macos", linux: "trackobee-agent-linux" };
    await Promise.all(Object.entries(f).map(async ([k, v]) => { try { out[k] = (await fetch(`${siteUrl()}/installers/${v}`, { method: "HEAD" })).ok; } catch (e) { out[k] = false; } }));
    return out;
  }

  // route-style facade so the page code can stay simple
  async function api(path, method = "GET", body) {
    const [p, qs] = path.split("?"); const q = new URLSearchParams(qs || ""); let m;
    if (p === "/api/me") { const s = await status(); if (s.status !== "ok") { location.href = "login.html"; fail("not signed in"); }
      ME = s; if (s.role !== "owner") { const pr = ok(await sb.from("tb_profiles").select("org_id").eq("user_id", s.id).maybeSingle()); ORG_ID = pr && pr.org_id; } return s; }
    if (p === "/api/live") {                                       // live status + each computer's inventory (serial number, model ...), inventory cached 60 s
      const rows = ok(await sb.rpc("tb_live_status"));
      if (!DEVC || Date.now() - DEVT > 60000) { try { DEVC = ok(await sb.from("tb_devices").select("host,info,telemetry,location,agent_version,updated_at,facility_override,first_seen")); DEVT = Date.now(); } catch (e) {} }
      const m = {}; (DEVC || []).forEach(d => { m[d.host] = d; });
      return rows.map(r => { const d = m[r.host], i = (d && d.info) || {}; return { ...r, serial: i.serial_number || i.board_serial || "", dev: d || null }; }); }
    if (p === "/api/report") { const [a, b] = dayRange(q.get("date"));
      return ok(await sb.rpc("tb_daily_report", { p_start: a, p_end: b })).map(r => ({ user: r.usr, host: r.host, facility: r.facility, ips: r.ips, first: r.first_ts, last: r.last_ts, screen_on: r.screen_on, idle: r.idle, locked: r.locked })); }
    if (p === "/api/events") {                                   // ?from=YYYY-MM-DD&to=YYYY-MM-DD (local days) or ?days=N
      let from, to;
      if (q.get("since")) { from = Number(q.get("since")); to = Number(q.get("until") || Math.floor(Date.now() / 1000) + 60); }
      else if (q.get("from")) { from = dayRange(q.get("from"))[0]; to = dayRange(q.get("to") || q.get("from"))[1]; }
      else { const days = Number(q.get("days") || 0), now = Math.floor(Date.now() / 1000);
        from = days > 0 ? now - days * 86400 : Math.floor(new Date(new Date().toDateString()) / 1000); to = now + 60; }
      const out = [];
      for (let off = 0; off < 10000; off += 1000) {            // the API returns at most 1000 rows per request
        const r = ok(await sb.from("tb_ev").select("*").gte("ts", from).lt("ts", to).order("ts", { ascending: false }).range(off, off + 999));
        out.push(...r); if (r.length < 1000) break; }
      return out; }
    if (p === "/api/checkins") { const a = dayRange(q.get("from"))[0], b = dayRange(q.get("to") || q.get("from"))[1];     // first working moment of each day
      const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "UTC";
      const r = await sb.rpc("tb_day_checkins", { p_start: a, p_end: b, p_tz: tz }); if (r.error) return [];
      return r.data.map(x => ({ day: x.day, user: x.usr, host: x.host, checkin: x.checkin, lastwork: x.lastwork })); }
    if (p === "/api/activity") { const a = dayRange(q.get("from"))[0], b = dayRange(q.get("to") || q.get("from"))[1];
      const r = await sb.rpc("tb_activity_usage", { p_start: a, p_end: b }); if (r.error) return [];
      return r.data.map(x => ({ user: x.usr, host: x.host, app: x.app, site: x.site, title: x.title, seconds: x.seconds })); }
    if (p === "/api/work") { const a = dayRange(q.get("from"))[0], b = dayRange(q.get("to") || q.get("from"))[1];
      const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "UTC";
      return ok(await sb.rpc("tb_work_summary", { p_start: a, p_end: b, p_tz: tz })).map(r => ({ day: r.day, user: r.usr, hosts: r.hosts || [], worked: r.worked, away: r.away, locked: r.locked, first: r.first_ts, last: r.last_ts })); }
    if (p === "/api/apps") { const a = dayRange(q.get("from"))[0], b = dayRange(q.get("to") || q.get("from"))[1];
      return ok(await sb.rpc("tb_app_usage", { p_start: a, p_end: b })).map(r => ({ user: r.usr, app: r.app, seconds: r.seconds })); }
    if (p === "/api/nicks") {                                    // nicknames per computer (serial number)
      if (method === "POST") ok(await sb.rpc("tb_set_nickname", { p_key: body.key, p_name: body.name || "" }));
      const r = await sb.from("tb_nicknames").select("key,name"); if (r.error) return {};
      const m = {}; r.data.forEach(x => { m[x.key] = x.name; }); return m; }
    if (p === "/api/people") {                                   // display names + first-seen per employee
      const r = await sb.from("tb_people").select("usr,name,first_seen"); if (r.error) return {};      // older database: no renames yet
      const m = {}; r.data.forEach(x => { m[x.usr] = { name: x.name, first: x.first_seen }; }); return m; }
    if (p === "/api/sites") { const a = dayRange(q.get("from"))[0], b = dayRange(q.get("to") || q.get("from"))[1];
      const r = await sb.rpc("tb_site_usage", { p_start: a, p_end: b }); if (r.error) return [];      // older database: no website data yet
      return r.data.map(x => ({ user: x.usr, site: x.site, seconds: x.seconds })); }
    if (p === "/api/titles") { const a = dayRange(q.get("from"))[0], b = dayRange(q.get("to") || q.get("from"))[1];
      const r = await sb.rpc("tb_title_usage", { p_start: a, p_end: b }); if (r.error) return [];      // older database: no page titles yet
      return r.data.map(x => ({ user: x.usr, app: x.app, title: x.title, seconds: x.seconds })); }
    if (p === "/api/devices") {                                     // inventory + each computer's current facility
      const [devs, live] = await Promise.all([sb.from("tb_devices").select("*").order("host"), sb.rpc("tb_live_status")]);
      const m = {}; (live.data || []).forEach(r => { m[r.host] = r; });
      return ok(devs).map(d => ({ ...d, fac: m[d.host] ? m[d.host].facility : undefined, fac_src: m[d.host] ? m[d.host].facility_source : undefined })); }
    if (p === "/api/device/facility" && method === "POST") { ok(await sb.rpc("tb_set_device_facility", { p_host: body.host, p_facility: body.facility })); return { ok: true }; }
    if (p === "/api/facilities") {
      if (method === "POST") { const list = (body.cidrs || [body.cidr]).filter(Boolean).map(c => ({ org_id: ORG_ID, name: (body.name || "").trim().slice(0, 80), cidr: String(c).trim() }));
        const r = await sb.from("tb_facilities").upsert(list, { onConflict: "org_id,cidr" });
        if (r.error) fail(/cidr|inet|network/i.test(r.error.message) ? "invalid IP or CIDR" : r.error.message); }
      if (method === "DELETE") ok(await sb.from("tb_facilities").delete().eq("id", Number(q.get("id"))));
      return facilities(); }
    if (p === "/api/org") {
      if (method === "POST") { ok(await sb.rpc("tb_update_org", { p_name: body.name || null, p_origins: body.allowed_origins || "" })); return { ok: true }; }
      const o = ok(await sb.from("tb_orgs").select("name,public_id,allowed_origins,event_retention_days,hb_retention_days").limit(1).maybeSingle());
      const s = ok(await sb.from("tb_org_secrets").select("agent_token,crm_secret,api_key").maybeSingle());
      return { ...o, agent_token: s && s.agent_token, crm_secret: s && s.crm_secret, api_key: s && s.api_key }; }
    if (p === "/api/retention") { ok(await sb.rpc("tb_update_retention", { p_events: body.events, p_hb: body.hb })); return { ok: true }; }
    if ((m = p.match(/^\/api\/org\/rotate\/(agent|crm|api)$/))) { ok(await sb.rpc("tb_rotate_key", { p_what: m[1] })); return { ok: true }; }
    if (p === "/api/me/password") { const r = await sb.auth.signInWithPassword({ email: ME.email, password: body.old }); if (r.error) fail("current password is wrong");
      if (!body.new || body.new.length < 10) fail("password must be at least 10 characters"); ok(await sb.auth.updateUser({ password: body.new })); return { ok: true }; }
    if (p === "/api/users") { if (method === "POST") await fn({ action: "create_user", name: body.name, email: body.email, role: body.role, password: body.password }); return listUsers(); }
    if ((m = p.match(/^\/api\/users\/([0-9a-f-]{36})$/))) { const id = m[1];
      if (method === "DELETE") await fn({ action: "delete_user", user_id: id });
      else { if (body.password) await fn({ action: "set_password", user_id: id, password: body.password });
        if (body.role !== undefined || body.active !== undefined) { const cur = USERS.find(u => u.id === id) || {};
          ok(await sb.rpc("tb_set_user", { p_user: id, p_role: body.role ?? cur.role, p_active: body.active !== undefined ? !!body.active : !!cur.active })); } }
      return listUsers(); }
    if (p === "/api/deploy/info") { const o = ok(await sb.from("tb_orgs").select("public_id,allowed_origins").limit(1).maybeSingle());
      const s = ok(await sb.from("tb_org_secrets").select("crm_secret,api_key").maybeSingle());
      return { server: siteUrl(), events_url: `${cfg.supabaseUrl}/functions/v1/tb-events`, public_id: o.public_id, crm_secret: s && s.crm_secret, api_key: s && s.api_key, origins: o.allowed_origins, packaged: await packaged() }; }
    if (p === "/api/owner/summary") return ownerSummary();
    if (p === "/api/owner/settings") { ok(await sb.rpc("tb_owner_set_setting", { p_key: "require_approval", p_value: !!body.require_approval })); return ownerSummary(); }
    if ((m = p.match(/^\/api\/owner\/requests\/(\d+)\/(approve|reject)$/))) { ok(await sb.rpc(m[2] === "approve" ? "tb_approve_request" : "tb_reject_request", { p_id: Number(m[1]) })); return ownerSummary(); }
    if (p === "/api/owner/orgs" && method === "POST") { await fn({ action: "create_workspace", org: body.org, name: body.name, email: body.email, password: body.password }); return ownerSummary(); }
    if ((m = p.match(/^\/api\/owner\/orgs\/([0-9a-f-]{36})$/))) {
      if (method === "DELETE") ok(await sb.rpc("tb_owner_delete_org", { p_org: m[1] })); else ok(await sb.rpc("tb_owner_set_active", { p_org: m[1], p_active: !!body.active }));
      return ownerSummary(); }
    fail("unknown route " + path);
  }

  async function extInfo() { try { const r = await fetch(`${siteUrl()}/extension/info.json`); return r.ok ? await r.json() : null; } catch (e) { return null; } }
  async function downloadInstaller(os, withExt) {          // admins and sub-admins; the agent token never touches the page source
    const info = ok(await sb.rpc("tb_installer_info")); const { makeInstaller, saveFile } = await import("./installers.js?v=20261205b");
    const r = makeInstaller(os, { siteUrl: siteUrl(), ingestUrl: `${cfg.supabaseUrl}/rest/v1/rpc/tb_ingest_heartbeat`, anonKey: cfg.anonKey, agentToken: info.agent_token, allowedOrigins: info.allowed_origins }, await packaged(), withExt ? { ext: await extInfo() || {} } : null);
    saveFile(r.name, r.body);
  }
  async function exportCsv(date) {
    const rows = await api("/api/report?date=" + date); const f = t => new Date(t * 1000).toLocaleTimeString();
    const q = v => '"' + String(v ?? "").replace(/"/g, '""') + '"';
    const csv = ["user,host,facility,ips,first_seen,last_seen,screen_on_min,idle_min,locked_min", ...rows.map(a => [a.user, a.host, a.facility || "", a.ips.join(";"), f(a.first), f(a.last), Math.floor(a.screen_on / 60), Math.floor(a.idle / 60), Math.floor(a.locked / 60)].map(q).join(","))].join("\n");
    (await import("./installers.js?v=20261205b")).saveFile(`trackobee-${date}.csv`, csv);
  }

  async function exportEvents(from, to) {
    const rows = await api(`/api/events?from=${from}&to=${to}`); const q = v => '"' + String(v ?? "").replace(/"/g, '""') + '"';
    const csv = ["time,event,website_user,os_user,computer,facility,public_ip,source,detail", ...rows.map(e => [new Date(e.ts * 1000).toISOString(), e.type, e.crm_user, e.os_user, e.host, e.facility, e.pub_ip, e.source, e.detail].map(q).join(","))].join("\n");
    (await import("./installers.js?v=20261205b")).saveFile(`trackobee-events-${from}_to_${to}.csv`, csv);
  }
  async function exportWork(from, to) {
    const rows = await api(`/api/work?from=${from}&to=${to}`); const q = v => '"' + String(v ?? "").replace(/"/g, '""') + '"'; const h = s => (s / 3600).toFixed(2);
    const csv = ["date,employee,computers,worked_hours,away_hours,locked_hours,first_seen,last_seen", ...rows.map(r => [r.day, r.user, r.hosts.join(";"), h(r.worked), h(r.away + r.locked), h(r.locked), new Date(r.first * 1000).toLocaleTimeString(), new Date(r.last * 1000).toLocaleTimeString()].map(q).join(","))].join("\n");
    (await import("./installers.js?v=20261205b")).saveFile(`trackobee-work-${from}_to_${to}.csv`, csv);
  }

  const TB = {
    init, api, downloadInstaller, exportCsv, exportEvents, exportWork, status, siteUrl,
    publicSettings: async () => { const r = await sb.rpc("tb_public_settings"); return r.error ? { require_approval: false } : r.data; },
    claim: async () => ok(await sb.rpc("tb_claim_workspace")),
    sb: () => sb, cfg: () => cfg,
    signIn: async (email, password) => ok(await sb.auth.signInWithPassword({ email, password })),
    signOut: async () => { await sb.auth.signOut(); },
    signUp: async (email, password, meta) => ok(await sb.auth.signUp({ email, password, options: { data: meta || {} } })),
    requestWorkspace: async (org, name) => ok(await sb.rpc("tb_request_workspace", { p_org: org, p_name: name })),
  };
  window.TB = TB;
})();
