-- Trackobee v2: self-serve workspaces, website events API, endpoint login/logoff history, per-organization retention (min 3 days).
-- Idempotent: safe to run more than once.

-- ============================================================ columns / tables
alter table public.tb_orgs add column if not exists event_retention_days int not null default 30;   -- login/logoff history
alter table public.tb_orgs add column if not exists hb_retention_days int not null default 14;      -- heartbeats (reports)
do $$ begin alter table public.tb_orgs add constraint tb_orgs_event_ret check (event_retention_days between 3 and 365);
  exception when duplicate_object then null; end $$;
do $$ begin alter table public.tb_orgs add constraint tb_orgs_hb_ret check (hb_retention_days between 3 and 90);
  exception when duplicate_object then null; end $$;

alter table public.tb_org_secrets add column if not exists api_key text;                              -- website events API key
update public.tb_org_secrets set api_key = public.tb_rand(24) where api_key is null;
create unique index if not exists tb_org_secrets_api_key on public.tb_org_secrets(api_key);

alter table public.tb_ev add column if not exists source text;     -- agent | embed | api | system
alter table public.tb_ev add column if not exists detail text;
create index if not exists tb_ev_oh on public.tb_ev(org_id, host, id desc);

create table if not exists public.tb_settings (key text primary key, value jsonb not null);
insert into public.tb_settings(key, value) values ('require_approval', 'false') on conflict (key) do nothing;

create table if not exists public.tb_pairings (                   -- website user <-> machine, from the events API
  org_id uuid not null references public.tb_orgs(id) on delete cascade,
  host text not null, crm_user text not null, source text, paired_at bigint not null,
  primary key (org_id, host));

alter table public.tb_settings enable row level security;
alter table public.tb_pairings enable row level security;
revoke all on public.tb_settings, public.tb_pairings from anon, authenticated;

-- ============================================================ self-serve workspace creation
create or replace function public.tb_create_workspace(p_user uuid, p_org text, p_name text, p_email text) returns uuid
language plpgsql security definer set search_path = public, extensions as $$
declare v uuid;
begin
  insert into tb_orgs(name, public_id) values (left(btrim(p_org), 80), tb_rand(6)) returning id into v;
  insert into tb_org_secrets(org_id, agent_token, crm_secret, api_key) values (v, tb_rand(32), tb_rand(32), tb_rand(24));
  insert into tb_profiles(user_id, org_id, email, name, role) values (p_user, v, p_email, left(btrim(p_name), 80), 'admin');
  return v;
end $$;

-- Called by the website right after sign-up / first sign-in. Reads org name + full name from the signup metadata.
-- Creates the workspace immediately, or a pending request when the platform owner requires approval.
create or replace function public.tb_claim_workspace() returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_uid uuid := auth.uid(); v_meta jsonb; v_email text; v_org text; v_name text; v_req boolean;
begin
  if v_uid is null then return jsonb_build_object('status', 'anon'); end if;
  if not exists (select 1 from tb_profiles where user_id = v_uid) and not exists (select 1 from tb_signup_requests where user_id = v_uid) then
    select u.email, u.raw_user_meta_data into v_email, v_meta from auth.users u where u.id = v_uid;
    v_org := left(btrim(coalesce(v_meta->>'org_name', '')), 80);
    v_name := left(btrim(coalesce(nullif(v_meta->>'full_name', ''), split_part(v_email, '@', 1))), 80);
    if v_org <> '' then
      select coalesce((value)::boolean, false) into v_req from tb_settings where key = 'require_approval';
      if coalesce(v_req, false) then
        insert into tb_signup_requests(user_id, org_name, name, email) values (v_uid, v_org, v_name, v_email) on conflict (user_id) do nothing;
      else
        perform tb_create_workspace(v_uid, v_org, coalesce(nullif(v_name, ''), 'Admin'), v_email);
      end if;
    end if;
  end if;
  return tb_my_status();
end $$;

create or replace function public.tb_public_settings() returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object('require_approval', coalesce((select (value)::boolean from tb_settings where key = 'require_approval'), false)) $$;

create or replace function public.tb_owner_set_setting(p_key text, p_value jsonb) returns void
language plpgsql security definer set search_path = public as $$
begin
  if tb_me_role() is distinct from 'owner' then raise exception 'forbidden'; end if;
  if p_key <> 'require_approval' then raise exception 'unknown setting'; end if;
  insert into tb_settings(key, value) values (p_key, p_value) on conflict (key) do update set value = excluded.value;
end $$;

create or replace function public.tb_owner_summary() returns jsonb
language plpgsql stable security definer set search_path = public as $$
begin
  if tb_me_role() is distinct from 'owner' then raise exception 'forbidden'; end if;
  return jsonb_build_object(
    'settings', coalesce((select jsonb_object_agg(key, value) from tb_settings), '{}'),
    'requests', coalesce((select jsonb_agg(jsonb_build_object('id', id, 'org', org_name, 'name', name, 'email', email,
                 'created', extract(epoch from created_at)::bigint) order by created_at) from tb_signup_requests), '[]'),
    'orgs', coalesce((select jsonb_agg(jsonb_build_object('id', o.id, 'name', o.name, 'public_id', o.public_id, 'active', o.active,
                 'created', extract(epoch from o.created_at)::bigint,
                 'users', (select count(*) from tb_profiles p where p.org_id = o.id),
                 'machines_24h', (select count(distinct h.host) from tb_hb h where h.org_id = o.id and h.ts >= extract(epoch from now())::bigint - 86400),
                 'admin', coalesce((select p.email from tb_profiles p where p.org_id = o.id and p.role = 'admin' order by p.created_at limit 1), ''))
                 order by o.created_at) from tb_orgs o), '[]'));
end $$;

-- ============================================================ settings: retention + API key
create or replace function public.tb_update_retention(p_events int, p_hb int) returns void
language plpgsql security definer set search_path = public as $$
begin
  if tb_me_role() is distinct from 'admin' then raise exception 'forbidden'; end if;
  if p_events is null or p_events < 3 or p_events > 365 then raise exception 'login/logoff history must be kept between 3 and 365 days'; end if;
  if p_hb is null or p_hb < 3 or p_hb > 90 then raise exception 'activity data must be kept between 3 and 90 days'; end if;
  update tb_orgs set event_retention_days = p_events, hb_retention_days = p_hb where id = tb_me_org();
end $$;

create or replace function public.tb_rotate_key(p_what text) returns void
language plpgsql security definer set search_path = public, extensions as $$
begin
  if tb_me_role() is distinct from 'admin' then raise exception 'forbidden'; end if;
  if p_what = 'agent' then update tb_org_secrets set agent_token = tb_rand(32) where org_id = tb_me_org();
  elsif p_what = 'crm' then update tb_org_secrets set crm_secret = tb_rand(32) where org_id = tb_me_org();
  elsif p_what = 'api' then update tb_org_secrets set api_key = tb_rand(24) where org_id = tb_me_org();
  else raise exception 'unknown key'; end if;
end $$;

-- ============================================================ website events API (called by the tb-events edge function with the service role)
-- A website backend reports "user X logged in / out" (+ the user's public IP). We pair the user with the machine
-- reporting from that IP when exactly one active machine matches; otherwise the event is still recorded.
create or replace function public.tb_website_event(p_api_key text, p_event text, p_user text, p_ip text,
                                                   p_host text default null, p_ts bigint default null) returns jsonb
language plpgsql security definer set search_path = public, extensions as $$
declare v_org uuid; v_now bigint := extract(epoch from now())::bigint; v_ev text; v_ip inet; v_host text; v_n int := 0;
        v_detail text; v_user text := btrim(coalesce(p_user, '')); v_ts bigint;
begin
  select o.id into v_org from tb_orgs o join tb_org_secrets s on s.org_id = o.id where s.api_key = p_api_key and o.active;
  if v_org is null then return jsonb_build_object('ok', false, 'error', 'invalid api key'); end if;
  v_ev := case lower(coalesce(p_event, '')) when 'login' then 'crm_login' when 'logout' then 'crm_logout' else null end;
  if v_ev is null then return jsonb_build_object('ok', false, 'error', 'event must be "login" or "logout"'); end if;
  if v_user = '' or length(v_user) > 100 then return jsonb_build_object('ok', false, 'error', 'user is required (max 100 characters)'); end if;
  if (select count(*) from tb_ev where org_id = v_org and source = 'api' and ts > v_now - 60) > 600 then
    return jsonb_build_object('ok', false, 'error', 'rate limited'); end if;
  begin v_ip := nullif(btrim(coalesce(p_ip, '')), '')::inet; exception when others then
    return jsonb_build_object('ok', false, 'error', 'invalid ip'); end;
  v_ts := case when p_ts is not null and p_ts between v_now - 86400 and v_now + 300 then p_ts else v_now end;

  if v_ev = 'crm_login' then
    if coalesce(p_host, '') <> '' then
      if exists (select 1 from tb_hb where org_id = v_org and host = p_host and ts > v_now - 86400) then v_host := p_host; v_n := 1; v_detail := 'paired by machine name';
      else v_detail := 'unknown machine name'; end if;
    elsif v_ip is not null then
      select count(*), min(x.host) into v_n, v_host
        from (select distinct on (h.host) h.host, h.pub_ip from tb_hb h where h.org_id = v_org and h.ts > v_now - 120 order by h.host, h.id desc) x
        where x.pub_ip is not null and x.pub_ip::inet = v_ip;
      if v_n = 1 then v_detail := 'paired by IP (one machine at this address)';
      elsif v_n = 0 then v_host := null; v_detail := 'no active machine at this IP';
      else v_host := null; v_detail := v_n || ' machines share this IP; use the embed script for exact pairing'; end if;
    else v_detail := 'no ip or machine given';
    end if;
    if v_host is not null then
      insert into tb_pairings(org_id, host, crm_user, source, paired_at) values (v_org, v_host, v_user, 'api', v_now)
        on conflict (org_id, host) do update set crm_user = excluded.crm_user, source = 'api', paired_at = v_now;
    end if;
  else
    delete from tb_pairings where org_id = v_org and crm_user = v_user and (coalesce(p_host, '') = '' or host = p_host);
    v_detail := 'website session ended';
  end if;

  insert into tb_ev(org_id, ts, host, pub_ip, os_user, crm_user, facility, type, source, detail)
    values (v_org, v_ts, v_host, host(v_ip), null, v_user, case when v_ip is null then null else tb_facility_for(v_org, host(v_ip)) end, v_ev, 'api', v_detail);
  return jsonb_build_object('ok', true, 'paired', v_host is not null, 'host', v_host, 'detail', v_detail);
end $$;

-- ============================================================ agent ingest (adds: pairings, logoff, online events)
create or replace function public.tb_ingest_heartbeat(
  p_agent_token text, p_host text, p_ip text, p_os_user text, p_state text, p_idle int,
  p_token text default null, p_events jsonb default '[]') returns jsonb
language plpgsql security definer set search_path = public, extensions as $$
declare v_org uuid; v_crm text; v_hdr jsonb; v_pub text; v_fac text; v_now bigint := extract(epoch from now())::bigint; e jsonb; n int := 0; v_user text; v_last text; v_type text;
begin
  select o.id into v_org from tb_orgs o join tb_org_secrets s on s.org_id = o.id
    where s.agent_token = p_agent_token and o.active;
  if v_org is null then return jsonb_build_object('ok', false); end if;
  if p_host is null or p_os_user is null or length(p_host) > 100 or length(p_os_user) > 100 or p_state not in ('active', 'locked') then
    return jsonb_build_object('ok', false);
  end if;
  select t.crm_user into v_crm from tb_verify_crm_token(p_token) t where t.org_id = v_org;          -- exact: embed token
  if v_crm is null then                                                                              -- else: events-API pairing (12 h)
    select pr.crm_user into v_crm from tb_pairings pr where pr.org_id = v_org and pr.host = p_host and pr.paired_at > v_now - 43200;
  end if;
  begin v_hdr := coalesce(nullif(current_setting('request.headers', true), '')::jsonb, '{}'); exception when others then v_hdr := '{}'; end;
  v_pub := nullif(btrim(coalesce(nullif(v_hdr->>'cf-connecting-ip', ''), split_part(coalesce(v_hdr->>'x-forwarded-for', ''), ',', 1))), '');
  begin perform v_pub::inet; exception when others then v_pub := null; end;
  v_fac := tb_facility_for(v_org, v_pub, p_ip);
  insert into tb_hb(org_id, ts, host, ip, pub_ip, os_user, crm_user, facility, state, idle)
    values (v_org, v_now, p_host, left(p_ip, 64), v_pub, p_os_user, v_crm, v_fac, p_state, greatest(coalesce(p_idle, 0), 0));
  select ev.type into v_last from tb_ev ev where ev.org_id = v_org and ev.host = p_host and ev.type in ('online', 'offline') order by ev.id desc limit 1;
  if v_last = 'offline' then
    insert into tb_ev(org_id, ts, host, pub_ip, os_user, crm_user, facility, type, source, detail)
      values (v_org, v_now, p_host, v_pub, p_os_user, v_crm, v_fac, 'online', 'system', 'machine reporting again');
  end if;
  for e in select * from jsonb_array_elements(coalesce(p_events, '[]'::jsonb)) loop
    n := n + 1; exit when n > 50;
    v_type := e->>'type';
    if v_type in ('login', 'logoff', 'lock', 'unlock', 'crm_login', 'crm_logout') then
      v_user := left(coalesce(nullif(e->>'user', ''), v_crm), 100);
      insert into tb_ev(org_id, ts, host, pub_ip, os_user, crm_user, facility, type, source)
        values (v_org, coalesce(nullif(e->>'ts', '')::bigint, v_now), p_host, v_pub, p_os_user, v_user, v_fac, v_type,
                case when v_type like 'crm\_%' then 'embed' else 'agent' end);
    end if;
  end loop;
  return jsonb_build_object('ok', true, 'interval', 15);
end $$;

-- ============================================================ maintenance (pg_cron)
-- machines that stop reporting get one 'offline' event (and 'online' when they return, see ingest)
create or replace function public.tb_mark_offline() returns void
language sql security definer set search_path = public as $$
  insert into tb_ev(org_id, ts, host, pub_ip, os_user, crm_user, facility, type, source, detail)
  select h.org_id, h.ts, h.host, h.pub_ip, h.os_user, h.crm_user, h.facility, 'offline', 'system', 'machine stopped reporting'
  from (select distinct on (x.org_id, x.host) x.* from tb_hb x where x.ts > extract(epoch from now())::bigint - 172800 order by x.org_id, x.host, x.id desc) h
  join tb_orgs o on o.id = h.org_id and o.active
  where h.ts < extract(epoch from now())::bigint - 90
    and coalesce((select e.type from tb_ev e where e.org_id = h.org_id and e.host = h.host and e.type in ('online', 'offline') order by e.id desc limit 1), 'online') <> 'offline' $$;

create or replace function public.tb_purge() returns void language sql security definer set search_path = public as $$
  delete from tb_hb h using tb_orgs o where o.id = h.org_id and h.ts < extract(epoch from now())::bigint - o.hb_retention_days * 86400;
  delete from tb_ev e using tb_orgs o where o.id = e.org_id and e.ts < extract(epoch from now())::bigint - o.event_retention_days * 86400;
  delete from tb_pairings where paired_at < extract(epoch from now())::bigint - 86400 $$;

-- ============================================================ privileges (re-applied for ALL tb_ functions)
do $$ declare f record; begin
  for f in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'public' and p.proname like 'tb\_%' loop
    execute format('revoke all on function %s from public, anon, authenticated', f.sig);
  end loop; end $$;

grant execute on function public.tb_me_org(), public.tb_me_role() to authenticated;
grant execute on function public.tb_ingest_heartbeat(text, text, text, text, text, int, text, jsonb) to anon, authenticated;
grant execute on function public.tb_public_installer_info(text), public.tb_public_settings() to anon, authenticated;
grant execute on function public.tb_installer_info(), public.tb_live_status(), public.tb_daily_report(bigint, bigint), public.tb_my_status(),
  public.tb_claim_workspace(), public.tb_request_workspace(text, text), public.tb_update_org(text, text), public.tb_update_retention(int, int),
  public.tb_rotate_key(text), public.tb_set_user(uuid, text, boolean), public.tb_approve_request(bigint), public.tb_reject_request(bigint),
  public.tb_owner_summary(), public.tb_owner_set_active(uuid, boolean), public.tb_owner_delete_org(uuid), public.tb_owner_set_setting(text, jsonb) to authenticated;
grant execute on function public.tb_create_workspace(uuid, text, text, text), public.tb_website_event(text, text, text, text, text, bigint) to service_role;

do $$ begin
  create extension if not exists pg_cron;
  perform cron.schedule('tb_offline', '* * * * *', 'select public.tb_mark_offline()');
  perform cron.schedule('tb_purge', '17 3 * * *', 'select public.tb_purge()');
exception when others then raise notice 'pg_cron not enabled: enable it for offline detection and retention cleanup';
end $$;
