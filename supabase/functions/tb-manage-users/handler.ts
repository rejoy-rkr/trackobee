// Privileged user management for Trackobee. Runs with the service role, so every action re-checks the caller itself.
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const json = (o: unknown, status = 200) =>
  new Response(JSON.stringify(o), { status, headers: { ...CORS, "Content-Type": "application/json" } });

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const okPw = (p: unknown) => typeof p === "string" && p.length >= 10 && p.length <= 200;
const str = (v: unknown, max = 80) => (typeof v === "string" ? v.trim().slice(0, max) : "");

// deno-lint-ignore no-explicit-any
export async function handle(req: Request, admin: any): Promise<Response> {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  const jwt = (req.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "");
  const { data: ud, error: ue } = await admin.auth.getUser(jwt);
  if (ue || !ud?.user) return json({ error: "not signed in" }, 401);

  const { data: me } = await admin.from("tb_profiles").select("user_id,org_id,role,active").eq("user_id", ud.user.id).maybeSingle();
  if (!me || !me.active) return json({ error: "forbidden" }, 403);
  if (me.role !== "owner") {
    const { data: org } = await admin.from("tb_orgs").select("active").eq("id", me.org_id).maybeSingle();
    if (!org?.active) return json({ error: "forbidden" }, 403);
  }

  let b: Record<string, unknown>;
  try { b = await req.json(); } catch { return json({ error: "bad json" }, 400); }

  // target must be in the caller's organization (owner may target anyone), and never an owner for an org admin
  const target = async (id: unknown) => {
    if (typeof id !== "string") return null;
    const { data: t } = await admin.from("tb_profiles").select("user_id,org_id,role,active").eq("user_id", id).maybeSingle();
    if (!t) return null;
    if (me.role === "owner") return t;
    return t.org_id === me.org_id && t.role !== "owner" ? t : null;
  };

  switch (b.action) {
    case "create_user": {
      if (me.role !== "admin") return json({ error: "forbidden" }, 403);
      const email = str(b.email, 200).toLowerCase(), name = str(b.name), role = b.role;
      if (!EMAIL.test(email) || !name || !["admin", "sub_admin", "read_only"].includes(role as string)) return json({ error: "name, valid email and role are required" }, 400);
      if (!okPw(b.password)) return json({ error: "password must be at least 10 characters" }, 400);
      const { data: cu, error: ce } = await admin.auth.admin.createUser({ email, password: b.password, email_confirm: true });
      if (ce || !cu?.user) return json({ error: /already|registered|exists/i.test(ce?.message ?? "") ? "email already registered" : "could not create user" }, /already|registered|exists/i.test(ce?.message ?? "") ? 409 : 500);
      const { error: pe } = await admin.from("tb_profiles").insert({ user_id: cu.user.id, org_id: me.org_id, email, name, role });
      if (pe) { await admin.auth.admin.deleteUser(cu.user.id); return json({ error: "could not create profile" }, 500); }
      return json({ ok: true });
    }
    case "set_password": {
      if (me.role !== "admin" && me.role !== "owner") return json({ error: "forbidden" }, 403);
      const t = await target(b.user_id);
      if (!t) return json({ error: "user not found" }, 404);
      if (!okPw(b.password)) return json({ error: "password must be at least 10 characters" }, 400);
      const { error } = await admin.auth.admin.updateUserById(t.user_id, { password: b.password });
      return error ? json({ error: "could not update password" }, 500) : json({ ok: true });
    }
    case "delete_user": {
      if (me.role !== "admin") return json({ error: "forbidden" }, 403);
      const t = await target(b.user_id);
      if (!t) return json({ error: "user not found" }, 404);
      if (t.user_id === me.user_id) return json({ error: "you cannot delete yourself" }, 400);
      if (t.role === "admin") {
        const { count } = await admin.from("tb_profiles").select("user_id", { count: "exact", head: true })
          .eq("org_id", me.org_id).eq("role", "admin").eq("active", true).neq("user_id", t.user_id);
        if (!count) return json({ error: "the organization needs at least one active admin" }, 400);
      }
      const { error } = await admin.auth.admin.deleteUser(t.user_id);   // cascades to the profile
      return error ? json({ error: "could not delete user" }, 500) : json({ ok: true });
    }
    case "create_workspace": {                                          // owner creates a workspace + its first admin by hand
      if (me.role !== "owner") return json({ error: "forbidden" }, 403);
      const org = str(b.org), name = str(b.name), email = str(b.email, 200).toLowerCase();
      if (!org || !name || !EMAIL.test(email)) return json({ error: "organization, admin name and valid email required" }, 400);
      if (!okPw(b.password)) return json({ error: "password must be at least 10 characters" }, 400);
      const { data: cu, error: ce } = await admin.auth.admin.createUser({ email, password: b.password, email_confirm: true });
      if (ce || !cu?.user) return json({ error: /already|registered|exists/i.test(ce?.message ?? "") ? "email already has an account" : "could not create user" }, /already|registered|exists/i.test(ce?.message ?? "") ? 409 : 500);
      const { error: re } = await admin.rpc("tb_create_workspace", { p_user: cu.user.id, p_org: org, p_name: name, p_email: email });
      if (re) { await admin.auth.admin.deleteUser(cu.user.id); return json({ error: "could not create workspace" }, 500); }
      return json({ ok: true });
    }
    default:
      return json({ error: "unknown action" }, 400);
  }
}
