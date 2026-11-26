-- Trackobee on Supabase: schema, row-level security and RPC functions.
-- Every object is prefixed tb_ so it cannot clash with anything else in the project.
create extension if not exists pgcrypto with schema extensions;

-- ============================================================ tables
create table if not exists public.tb_orgs (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  public_id text not null unique,
  allowed_origins text not null default '',
  active boolean not null default true,
  created_at timestamptz not null default now());

create table if not exists public.tb_org_secrets (            -- readable by the workspace admin only
  org_id uuid primary key references public.tb_orgs(id) on delete cascade,
  agent_token text not null unique,
  crm_secret text not null);

create table if not exists public.tb_profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  org_id uuid references public.tb_orgs(id) on delete cascade,
  email text not null, name text not null,
  role text not null check (role in ('owner','admin','sub_admin','read_only')),
  active boolean not null default true,
  created_at timestamptz not null default now(),
  check ((role = 'owner') = (org_id is null)));

create table if not exists public.tb_signup_requests (
  id bigint generated always as identity primary key,
  user_id uuid not null unique references auth.users(id) on delete cascade,
  org_name text not null, name text not null, email text not null,
  created_at timestamptz not null default now());

create table if not exists public.tb_facilities (
  id bigint generated always as identity primary key,
  org_id uuid not null references public.tb_orgs(id) on delete cascade,
  name text not null, cidr cidr not null, unique (org_id, cidr));

create table if not exists public.tb_hb (
  id bigint generated always as identity primary key,
  org_id uuid not null references public.tb_orgs(id) on delete cascade,
  ts bigint not null, host text, ip text, pub_ip text, os_user text, crm_user text,
  facility text, state text, idle int);
create index if not exists tb_hb_o on public.tb_hb(org_id, ts);
create index if not exists tb_hb_oh on public.tb_hb(org_id, host, id desc);

create table if not exists public.tb_ev (
  id bigint generated always as identity primary key,
  org_id uuid not null references public.tb_orgs(id) on delete cascade,
  ts bigint not null, host text, pub_ip text, os_user text, crm_user text, facility text, type text);
create index if not exists tb_ev_o on public.tb_ev(org_id, ts);

-- ============================================================ who am I (used by the policies)
create or replace function public.tb_me_org() returns uuid
language sql stable security definer set search_path = public as $$
  select p.org_id from tb_profiles p left join tb_orgs o on o.id = p.org_id
  where p.user_id = auth.uid() and p.active and (p.role = 'owner' or o.active) $$;

create or replace function public.tb_me_role() returns text
language sql stable security definer set search_path = public as $$
  select p.role from tb_profiles p left join tb_orgs o on o.id = p.org_id
  where p.user_id = auth.uid() and p.active and (p.role = 'owner' or o.active) $$;

-- ============================================================ row-level security
alter table public.tb_orgs enable row level security;
alter table public.tb_org_secrets enable row level security;
alter table public.tb_profiles enable row level security;
alter table public.tb_signup_requests enable row level security;
alter table public.tb_facilities enable row level security;
alter table public.tb_hb enable row level security;
alter table public.tb_ev enable row level security;

revoke all on public.tb_orgs, public.tb_org_secrets, public.tb_profiles, public.tb_signup_requests,
              public.tb_facilities, public.tb_hb, public.tb_ev from anon, authenticated;
grant select on public.tb_orgs, public.tb_org_secrets, public.tb_profiles, public.tb_signup_requests,
                public.tb_facilities, public.tb_hb, public.tb_ev to authenticated;
grant insert, update, delete on public.tb_facilities to authenticated;

drop policy if exists tb_orgs_sel on public.tb_orgs;
create policy tb_orgs_sel on public.tb_orgs for select to authenticated
  using (id = tb_me_org() or tb_me_role() = 'owner');
drop policy if exists tb_secrets_sel on public.tb_org_secrets;
create policy tb_secrets_sel on public.tb_org_secrets for select to authenticated
  using (org_id = tb_me_org() and tb_me_role() = 'admin');
drop policy if exists tb_prof_sel on public.tb_profiles;
create policy tb_prof_sel on public.tb_profiles for select to authenticated
  using (user_id = auth.uid() or org_id = tb_me_org() or tb_me_role() = 'owner');
drop policy if exists tb_req_sel on public.tb_signup_requests;
create policy tb_req_sel on public.tb_signup_requests for select to authenticated using (tb_me_role() = 'owner');
drop policy if exists tb_fac_sel on public.tb_facilities;
create policy tb_fac_sel on public.tb_facilities for select to authenticated using (org_id = tb_me_org());
drop policy if exists tb_fac_ins on public.tb_facilities;
create policy tb_fac_ins on public.tb_facilities for insert to authenticated
  with check (org_id = tb_me_org() and tb_me_role() in ('admin','sub_admin'));
drop policy if exists tb_fac_upd on public.tb_facilities;
create policy tb_fac_upd on public.tb_facilities for update to authenticated
  using (org_id = tb_me_org() and tb_me_role() in ('admin','sub_admin'))
  with check (org_id = tb_me_org() and tb_me_role() in ('admin','sub_admin'));
drop policy if exists tb_fac_del on public.tb_facilities;
create policy tb_fac_del on public.tb_facilities for delete to authenticated
  using (org_id = tb_me_org() and tb_me_role() in ('admin','sub_admin'));
drop policy if exists tb_hb_sel on public.tb_hb;
create policy tb_hb_sel on public.tb_hb for select to authenticated using (org_id = tb_me_org());
drop policy if exists tb_ev_sel on public.tb_ev;
create policy tb_ev_sel on public.tb_ev for select to authenticated using (org_id = tb_me_org());
-- hb / ev have NO insert policy: only tb_ingest_heartbeat (security definer) can write them.

-- ============================================================ helpers (internal)
create or replace function public.tb_rand(n int) returns text language sql volatile set search_path = public, extensions as $$
  select translate(encode(gen_random_bytes(n), 'base64'), E'+/=\n', '-_') $$;

create or replace function public.tb_b64url_decode(s text) returns text language sql immutable as $$
  select convert_from(decode(translate(s, '-_', '+/') || repeat('=', (4 - length(s) % 4) % 4), 'base64'), 'utf8') $$;

create or replace function public.tb_facility_for(p_org uuid, variadic p_ips text[]) returns text
language plpgsql stable security definer set search_path = public as $$
declare ip text; v inet; r text;
begin
  foreach ip in array p_ips loop
    begin v := ip::inet; exception when others then continue; end;
    select f.name into r from tb_facilities f
      where f.org_id = p_org and family(f.cidr) = family(v) and v <<= f.cidr
      order by masklen(f.cidr) desc limit 1;
    if r is not null then return r; end if;
  end loop;
  return null;
end $$;

-- CRM-signed pairing token: <org_public_id>.<base64url("user:expiry")>.<hmac_sha256_hex(org_secret, "<public_id>.<payload>")>
create or replace function public.tb_verify_crm_token(p_token text) returns table(org_id uuid, crm_user text)
language plpgsql stable security definer set search_path = public, extensions as $$
declare parts text[]; v_org uuid; v_secret text; payload text; usr text; exp bigint; expected text;
begin
  if p_token is null or length(p_token) > 600 then return; end if;
  parts := string_to_array(p_token, '.');
  if array_length(parts, 1) <> 3 then return; end if;
  select o.id, s.crm_secret into v_org, v_secret
    from tb_orgs o join tb_org_secrets s on s.org_id = o.id where o.public_id = parts[1] and o.active;
  if v_org is null then return; end if;
  expected := encode(hmac(parts[1] || '.' || parts[2], v_secret, 'sha256'), 'hex');
  if encode(hmac(expected, 'cmp', 'sha256'), 'hex') <> encode(hmac(parts[3], 'cmp', 'sha256'), 'hex') then return; end if;
  payload := tb_b64url_decode(parts[2]);
  usr := substring(payload from '^(.*):[0-9]+$'); exp := substring(payload from ':([0-9]+)$')::bigint;
  if usr is null or exp <= extract(epoch from now()) then return; end if;
  return query select v_org, usr;
exception when others then return;
end $$;

-- ============================================================ agent + embed entry points (callable by anon)
create or replace function public.tb_ingest_heartbeat(
  p_agent_token text, p_host text, p_ip text, p_os_user text, p_state text, p_idle int,
  p_token text default null, p_events jsonb default '[]') returns jsonb
language plpgsql security definer set search_path = public, extensions as $$
declare v_org uuid; v_crm text; v_hdr jsonb; v_pub text; v_fac text; v_now bigint := extract(epoch from now())::bigint; e jsonb; n int := 0; v_user text;
begin
  select o.id into v_org from tb_orgs o join tb_org_secrets s on s.org_id = o.id
    where s.agent_token = p_agent_token and o.active;
  if v_org is null then return jsonb_build_object('ok', false); end if;
  if p_host is null or p_os_user is null or length(p_host) > 100 or length(p_os_user) > 100 or p_state not in ('active', 'locked') then
    return jsonb_build_object('ok', false);
  end if;
  select t.crm_user into v_crm from tb_verify_crm_token(p_token) t where t.org_id = v_org;   -- must belong to the same org
  begin v_hdr := coalesce(nullif(current_setting('request.headers', true), '')::jsonb, '{}'); exception when others then v_hdr := '{}'; end;
  v_pub := nullif(btrim(coalesce(nullif(v_hdr->>'cf-connecting-ip', ''), split_part(coalesce(v_hdr->>'x-forwarded-for', ''), ',', 1))), '');
  begin perform v_pub::inet; exception when others then v_pub := null; end;
  v_fac := tb_facility_for(v_org, v_pub, p_ip);
  insert into tb_hb(org_id, ts, host, ip, pub_ip, os_user, crm_user, facility, state, idle)
    values (v_org, v_now, p_host, left(p_ip, 64), v_pub, p_os_user, v_crm, v_fac, p_state, greatest(coalesce(p_idle, 0), 0));
  for e in select * from jsonb_array_elements(coalesce(p_events, '[]'::jsonb)) loop
    n := n + 1; exit when n > 50;
    if e->>'type' in ('login', 'lock', 'unlock', 'crm_login', 'crm_logout') then
      v_user := left(coalesce(nullif(e->>'user', ''), v_crm), 100);
      insert into tb_ev(org_id, ts, host, pub_ip, os_user, crm_user, facility, type)
        values (v_org, coalesce(nullif(e->>'ts', '')::bigint, v_now), p_host, v_pub, p_os_user, v_user, v_fac, e->>'type');
    end if;
  end loop;
  return jsonb_build_object('ok', true, 'interval', 15);
end $$;

-- the embedded website client uses this (with the CRM-signed token) to build the endpoint installer
create or replace function public.tb_public_installer_info(p_token text) returns jsonb
language plpgsql stable security definer set search_path = public as $$
declare v record;
begin
  select * into v from tb_verify_crm_token(p_token);
  if v.org_id is null then return jsonb_build_object('ok', false); end if;
  return (select jsonb_build_object('ok', true, 'agent_token', s.agent_token, 'allowed_origins', o.allowed_origins)
          from tb_orgs o join tb_org_secrets s on s.org_id = o.id where o.id = v.org_id);
end $$;

-- admins AND sub-admins may build endpoint installers (which carry the agent token); sub-admins still can't read the secrets table
create or replace function public.tb_installer_info() returns jsonb
language plpgsql stable security definer set search_path = public as $$
declare v_role text := tb_me_role();
begin
  if v_role is null or v_role not in ('admin', 'sub_admin') then raise exception 'forbidden'; end if;
  return (select jsonb_build_object('agent_token', s.agent_token, 'allowed_origins', o.allowed_origins, 'public_id', o.public_id)
          from tb_orgs o join tb_org_secrets s on s.org_id = o.id where o.id = tb_me_org());
end $$;

-- ============================================================ dashboard reads (security invoker => row-level security applies)
create or replace function public.tb_live_status() returns table(
  host text, ip text, pub_ip text, os_user text, crm_user text, facility text, state text, idle int, last_seen bigint, mismatch boolean)
language sql stable set search_path = public as $$
  select h.host, h.ip, h.pub_ip, h.os_user, h.crm_user, coalesce(h.facility, 'Unmapped'),
    case when extract(epoch from now())::bigint - h.ts > 50 then 'offline'
         when h.state <> 'active' then 'locked' when h.idle > 300 then 'away' else 'available' end,
    h.idle, h.ts, (h.crm_user is not null and lower(h.crm_user) <> lower(h.os_user))
  from (select distinct on (x.host) x.* from tb_hb x where x.org_id = tb_me_org() order by x.host, x.id desc) h
  order by h.host $$;

create or replace function public.tb_daily_report(p_start bigint, p_end bigint) returns table(
  usr text, host text, facility text, ips text[], first_ts bigint, last_ts bigint, screen_on bigint, idle bigint, locked bigint)
language sql stable set search_path = public as $$
  with x as (
    select coalesce(h.crm_user, '(unpaired) ' || h.os_user) as u, h.host, h.facility, h.pub_ip, h.ts, h.state, h.idle,
           least(h.ts - coalesce(lag(h.ts) over (partition by coalesce(h.crm_user, '(unpaired) ' || h.os_user), h.host order by h.ts), h.ts - 15), 45) as gap
    from tb_hb h where h.org_id = tb_me_org() and h.ts >= p_start and h.ts < p_end)
  select u, x.host, (array_agg(x.facility order by x.ts desc) filter (where x.facility is not null))[1],
         coalesce(array_agg(distinct x.pub_ip) filter (where x.pub_ip is not null), '{}'), min(x.ts), max(x.ts),
         coalesce(sum(gap) filter (where x.state = 'active' and x.idle <= 300), 0)::bigint,
         coalesce(sum(gap) filter (where x.state = 'active' and x.idle > 300), 0)::bigint,
         coalesce(sum(gap) filter (where x.state <> 'active'), 0)::bigint
  from x group by u, x.host order by u, x.host $$;

-- ============================================================ account + workspace management
create or replace function public.tb_my_status() returns jsonb
language plpgsql stable security definer set search_path = public as $$
declare p tb_profiles; o tb_orgs;
begin
  if auth.uid() is null then return jsonb_build_object('status', 'anon'); end if;
  select * into p from tb_profiles where user_id = auth.uid();
  if not found then
    return jsonb_build_object('status', case when exists (select 1 from tb_signup_requests where user_id = auth.uid()) then 'pending' else 'none' end);
  end if;
  if not p.active then return jsonb_build_object('status', 'disabled'); end if;
  if p.role <> 'owner' then
    select * into o from tb_orgs where id = p.org_id;
    if not o.active then return jsonb_build_object('status', 'suspended'); end if;
  end if;
  return jsonb_build_object('status', 'ok', 'id', p.user_id, 'role', p.role, 'name', p.name, 'email', p.email,
                            'org', coalesce(o.name, 'Platform owner'));
end $$;

create or replace function public.tb_request_workspace(p_org text, p_name text) returns void
language plpgsql security definer set search_path = public as $$
declare v_email text;
begin
  if auth.uid() is null then raise exception 'login required'; end if;
  if exists (select 1 from tb_profiles where user_id = auth.uid()) then raise exception 'you already have a workspace'; end if;
  if btrim(coalesce(p_org, '')) = '' or btrim(coalesce(p_name, '')) = '' then raise exception 'organization and name are required'; end if;
  select email into v_email from auth.users where id = auth.uid();
  insert into tb_signup_requests(user_id, org_name, name, email) values (auth.uid(), left(btrim(p_org), 80), left(btrim(p_name), 80), v_email)
    on conflict (user_id) do nothing;
end $$;

create or replace function public.tb_update_org(p_name text, p_origins text) returns void
language plpgsql security definer set search_path = public as $$
declare v_role text := tb_me_role(); v_org uuid := tb_me_org(); o text; list text[] := '{}';
begin
  if v_role is null or v_role not in ('admin', 'sub_admin') then raise exception 'forbidden'; end if;
  for o in select btrim(regexp_replace(x, '/+$', '')) from regexp_split_to_table(coalesce(p_origins, ''), '[,\n]') x loop
    if o = '' then continue; end if;
    if o !~ '^https?://[A-Za-z0-9.\-]+(:[0-9]+)?$' then raise exception 'invalid origin: % (use https://host[:port])', o; end if;
    list := list || o;
  end loop;
  update tb_orgs set allowed_origins = array_to_string(list, ',') where id = v_org;
  if v_role = 'admin' and btrim(coalesce(p_name, '')) <> '' then update tb_orgs set name = left(btrim(p_name), 80) where id = v_org; end if;
end $$;

create or replace function public.tb_rotate_key(p_what text) returns void
language plpgsql security definer set search_path = public, extensions as $$
begin
  if tb_me_role() is distinct from 'admin' then raise exception 'forbidden'; end if;
  if p_what = 'agent' then update tb_org_secrets set agent_token = tb_rand(32) where org_id = tb_me_org();
  elsif p_what = 'crm' then update tb_org_secrets set crm_secret = tb_rand(32) where org_id = tb_me_org();
  else raise exception 'unknown key'; end if;
end $$;

create or replace function public.tb_set_user(p_user uuid, p_role text, p_active boolean) returns void
language plpgsql security definer set search_path = public as $$
declare t tb_profiles; v_org uuid := tb_me_org();
begin
  if tb_me_role() is distinct from 'admin' then raise exception 'forbidden'; end if;
  select * into t from tb_profiles where user_id = p_user and org_id = v_org;
  if not found then raise exception 'user not found'; end if;
  if p_role not in ('admin', 'sub_admin', 'read_only') then raise exception 'bad role'; end if;
  if t.role = 'admin' and (p_role <> 'admin' or not p_active)
     and not exists (select 1 from tb_profiles where org_id = v_org and role = 'admin' and active and user_id <> p_user) then
    raise exception 'the organization needs at least one active admin';
  end if;
  update tb_profiles set role = p_role, active = p_active where user_id = p_user;
end $$;

-- internal: used by approval and by the manage-users edge function
create or replace function public.tb_create_workspace(p_user uuid, p_org text, p_name text, p_email text) returns uuid
language plpgsql security definer set search_path = public, extensions as $$
declare v uuid;
begin
  insert into tb_orgs(name, public_id) values (left(btrim(p_org), 80), tb_rand(6)) returning id into v;
  insert into tb_org_secrets(org_id, agent_token, crm_secret) values (v, tb_rand(32), tb_rand(32));
  insert into tb_profiles(user_id, org_id, email, name, role) values (p_user, v, p_email, left(btrim(p_name), 80), 'admin');
  return v;
end $$;

create or replace function public.tb_approve_request(p_id bigint) returns void
language plpgsql security definer set search_path = public as $$
declare r tb_signup_requests;
begin
  if tb_me_role() is distinct from 'owner' then raise exception 'forbidden'; end if;
  select * into r from tb_signup_requests where id = p_id;
  if not found then raise exception 'request not found'; end if;
  perform tb_create_workspace(r.user_id, r.org_name, r.name, r.email);
  delete from tb_signup_requests where id = p_id;
end $$;

create or replace function public.tb_reject_request(p_id bigint) returns void
language plpgsql security definer set search_path = public as $$
declare r tb_signup_requests;
begin
  if tb_me_role() is distinct from 'owner' then raise exception 'forbidden'; end if;
  select * into r from tb_signup_requests where id = p_id;
  if not found then raise exception 'request not found'; end if;
  delete from auth.users where id = r.user_id;          -- cascades to the request row
end $$;

create or replace function public.tb_owner_summary() returns jsonb
language plpgsql stable security definer set search_path = public as $$
begin
  if tb_me_role() is distinct from 'owner' then raise exception 'forbidden'; end if;
  return jsonb_build_object(
    'requests', coalesce((select jsonb_agg(jsonb_build_object('id', id, 'org', org_name, 'name', name, 'email', email,
                 'created', extract(epoch from created_at)::bigint) order by created_at) from tb_signup_requests), '[]'),
    'orgs', coalesce((select jsonb_agg(jsonb_build_object('id', o.id, 'name', o.name, 'public_id', o.public_id, 'active', o.active,
                 'created', extract(epoch from o.created_at)::bigint,
                 'users', (select count(*) from tb_profiles p where p.org_id = o.id),
                 'machines_24h', (select count(distinct h.host) from tb_hb h where h.org_id = o.id and h.ts >= extract(epoch from now())::bigint - 86400),
                 'admin', coalesce((select p.email from tb_profiles p where p.org_id = o.id and p.role = 'admin' order by p.created_at limit 1), ''))
                 order by o.created_at) from tb_orgs o), '[]'));
end $$;

create or replace function public.tb_owner_set_active(p_org uuid, p_active boolean) returns void
language plpgsql security definer set search_path = public as $$
begin
  if tb_me_role() is distinct from 'owner' then raise exception 'forbidden'; end if;
  update tb_orgs set active = p_active where id = p_org;
end $$;

create or replace function public.tb_owner_delete_org(p_org uuid) returns void
language plpgsql security definer set search_path = public as $$
begin
  if tb_me_role() is distinct from 'owner' then raise exception 'forbidden'; end if;
  delete from auth.users where id in (select user_id from tb_profiles where org_id = p_org);
  delete from tb_orgs where id = p_org;                 -- cascades data, secrets, facilities
end $$;

-- run once in the SQL editor to create your platform-owner profile:  select tb_make_owner('you@example.com');
create or replace function public.tb_make_owner(p_email text) returns text
language plpgsql security definer set search_path = public as $$
declare u uuid;
begin
  select id into u from auth.users where lower(email) = lower(p_email);
  if u is null then raise exception 'no auth user with that email: create it first (Authentication -> Users)'; end if;
  insert into tb_profiles(user_id, org_id, email, name, role) values (u, null, lower(p_email), 'Platform owner', 'owner')
    on conflict (user_id) do update set role = 'owner', org_id = null, active = true;
  return 'owner ready: ' || p_email;
end $$;

create or replace function public.tb_purge() returns void language sql security definer set search_path = public as $$
  delete from tb_hb where ts < extract(epoch from now())::bigint - 60 * 86400;
  delete from tb_ev where ts < extract(epoch from now())::bigint - 365 * 86400 $$;

-- ============================================================ function privileges (Supabase grants execute to everyone by default)
do $$ declare f record; begin
  for f in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'public' and p.proname like 'tb\_%' loop
    execute format('revoke all on function %s from public, anon, authenticated', f.sig);
  end loop; end $$;

grant execute on function public.tb_me_org(), public.tb_me_role() to authenticated;
grant execute on function public.tb_ingest_heartbeat(text, text, text, text, text, int, text, jsonb) to anon, authenticated;
grant execute on function public.tb_public_installer_info(text) to anon, authenticated;
grant execute on function public.tb_installer_info() to authenticated;
grant execute on function public.tb_live_status(), public.tb_daily_report(bigint, bigint), public.tb_my_status(),
  public.tb_request_workspace(text, text), public.tb_update_org(text, text), public.tb_rotate_key(text),
  public.tb_set_user(uuid, text, boolean), public.tb_approve_request(bigint), public.tb_reject_request(bigint),
  public.tb_owner_summary(), public.tb_owner_set_active(uuid, boolean), public.tb_owner_delete_org(uuid) to authenticated;
grant execute on function public.tb_create_workspace(uuid, text, text, text) to service_role;

-- nightly retention (needs the pg_cron extension: Database -> Extensions -> pg_cron). Safe to skip if unavailable.
do $$ begin
  create extension if not exists pg_cron;
  perform cron.schedule('tb_purge', '17 3 * * *', 'select public.tb_purge()');
exception when others then raise notice 'pg_cron not enabled: enable it to auto-delete old heartbeats (select tb_purge())';
end $$;
