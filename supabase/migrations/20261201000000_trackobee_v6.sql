-- Trackobee v6: the real website (domain) of the active browser tab, reported by the Trackobee browser extension. Idempotent.
alter table public.tb_hb add column if not exists site text;

drop function if exists public.tb_ingest_heartbeat(text, text, text, text, text, int, text, jsonb, text, jsonb, jsonb, jsonb, text);
create or replace function public.tb_ingest_heartbeat(
  p_agent_token text, p_host text, p_ip text, p_os_user text, p_state text, p_idle int,
  p_token text default null, p_events jsonb default '[]',
  p_app text default null, p_sys jsonb default null, p_telemetry jsonb default null, p_location jsonb default null, p_title text default null, p_site text default null) returns jsonb
language plpgsql security definer set search_path = public, extensions as $$
declare v_org uuid; v_crm text; v_hdr jsonb; v_pub text; v_fac text; v_ovr text; v_now bigint := extract(epoch from now())::bigint;
        e jsonb; n int := 0; v_user text; v_last text; v_type text; v_ts bigint; v_ver text;
begin
  select o.id into v_org from tb_orgs o join tb_org_secrets s on s.org_id = o.id
    where s.agent_token = p_agent_token and o.active;
  if v_org is null then return jsonb_build_object('ok', false); end if;
  if p_host is null or p_os_user is null or length(p_host) > 100 or length(p_os_user) > 100
     or p_state is null or p_state not in ('active', 'locked') then
    return jsonb_build_object('ok', false);
  end if;
  select t.crm_user into v_crm from tb_verify_crm_token(p_token) t where t.org_id = v_org;          -- exact: embed token
  if v_crm is null then                                                                              -- else: events-API pairing (12 h)
    select pr.crm_user into v_crm from tb_pairings pr where pr.org_id = v_org and pr.host = p_host and pr.paired_at > v_now - 43200;
  end if;
  begin v_hdr := coalesce(nullif(current_setting('request.headers', true), '')::jsonb, '{}'); exception when others then v_hdr := '{}'; end;
  v_pub := nullif(btrim(coalesce(nullif(v_hdr->>'cf-connecting-ip', ''), split_part(coalesce(v_hdr->>'x-forwarded-for', ''), ',', 1))), '');
  begin perform v_pub::inet; exception when others then v_pub := null; end;
  select d.facility_override into v_ovr from tb_devices d where d.org_id = v_org and d.host = p_host;
  v_fac := case when v_ovr = '__none__' then null when v_ovr is not null then v_ovr else tb_facility_for(v_org, p_ip, v_pub) end;
  insert into tb_hb(org_id, ts, host, ip, pub_ip, os_user, crm_user, facility, state, idle, app, title, site)
    values (v_org, v_now, p_host, left(p_ip, 64), v_pub, p_os_user, v_crm, v_fac, p_state, greatest(coalesce(p_idle, 0), 0),
            nullif(left(btrim(coalesce(p_app, '')), 100), ''), nullif(left(btrim(coalesce(p_title, '')), 150), ''), nullif(left(btrim(coalesce(p_site, '')), 100), ''));

  v_ver := nullif(left(coalesce(p_sys->>'agent_version', ''), 40), '');
  insert into tb_devices(org_id, host, info, telemetry, location, agent_version, first_seen, updated_at)
    values (v_org, p_host,
            case when jsonb_typeof(p_sys) = 'object' and length(p_sys::text) <= 30000 then p_sys else '{}'::jsonb end,
            case when jsonb_typeof(p_telemetry) = 'object' and length(p_telemetry::text) <= 4000 then p_telemetry else '{}'::jsonb end,
            case when jsonb_typeof(p_location) = 'object' and length(p_location::text) <= 2000 then p_location else null end,
            v_ver, v_now, v_now)
    on conflict (org_id, host) do update set
      info = tb_devices.info || case when jsonb_typeof(p_sys) = 'object' and length(p_sys::text) <= 30000 then p_sys else '{}'::jsonb end,
      telemetry = tb_devices.telemetry || case when jsonb_typeof(p_telemetry) = 'object' and length(p_telemetry::text) <= 4000 then p_telemetry else '{}'::jsonb end,
      location = case when jsonb_typeof(p_location) = 'object' and length(p_location::text) <= 2000 then p_location else tb_devices.location end,
      agent_version = coalesce(v_ver, tb_devices.agent_version),
      updated_at = v_now;

  select ev.type into v_last from tb_ev ev where ev.org_id = v_org and ev.host = p_host and ev.type in ('online', 'offline') order by ev.id desc limit 1;
  if v_last = 'offline' then
    insert into tb_ev(org_id, ts, host, pub_ip, os_user, crm_user, facility, type, source, detail)
      values (v_org, v_now, p_host, v_pub, p_os_user, v_crm, v_fac, 'online', 'system', 'machine reporting again');
  end if;
  for e in select * from jsonb_array_elements(case when jsonb_typeof(p_events) = 'array' then p_events else '[]'::jsonb end) loop
    n := n + 1; exit when n > 50;
    v_type := e->>'type';
    if v_type in ('login', 'logoff', 'lock', 'unlock', 'crm_login', 'crm_logout') then
      v_user := left(coalesce(nullif(e->>'user', ''), v_crm), 100);
      v_ts := case when (e->>'ts') ~ '^[0-9]{1,12}$' and (e->>'ts')::bigint between v_now - 86400 and v_now + 300 then (e->>'ts')::bigint else v_now end;
      insert into tb_ev(org_id, ts, host, pub_ip, os_user, crm_user, facility, type, source)
        values (v_org, v_ts, p_host, v_pub, p_os_user, v_user, v_fac, v_type,
                case when v_type like 'crm\_%' then 'embed' else 'agent' end);
    end if;
  end loop;
  return jsonb_build_object('ok', true, 'interval', 15);
end $$;



drop function if exists public.tb_live_status();
create or replace function public.tb_live_status() returns table(
  host text, ip text, pub_ip text, os_user text, crm_user text, facility text, state text, idle int, last_seen bigint, mismatch boolean,
  app text, battery int, charging boolean, model text, location text, facility_source text, title text, site text)
language sql stable security definer set search_path = public as $$
  select h.host, h.ip, h.pub_ip, h.os_user, h.crm_user,
    case when d.facility_override = '__none__' then 'Unmapped'
         when d.facility_override is not null then d.facility_override
         else coalesce(tb_facility_for(d.org_id, h.ip, h.pub_ip), 'Unmapped') end,
    case when extract(epoch from now())::bigint - h.ts > 50 then 'offline'
         when h.state <> 'active' then 'locked' when h.idle > tb_away_secs() then 'away' else 'available' end,
    h.idle, h.ts, (h.crm_user is not null and lower(h.crm_user) <> lower(h.os_user)),
    h.app,
    case when (d.telemetry->>'battery_percent') ~ '^[0-9]+$' then (d.telemetry->>'battery_percent')::int end,
    case when d.telemetry ? 'charging' then (d.telemetry->>'charging')::boolean end,
    nullif(btrim(coalesce(d.info->>'manufacturer', '') || ' ' || coalesce(d.info->>'model', '')), ''),
    nullif(btrim(coalesce(d.location->>'city', '') || ', ' || coalesce(d.location->>'country', ''), ', '), ''),
    case when d.facility_override is not null then 'manual' else 'auto' end,
    h.title,
    h.site
  from tb_devices d
  cross join lateral (select x.* from tb_hb x where x.org_id = d.org_id and x.host = d.host order by x.id desc limit 1) h
  where d.org_id = tb_me_org()
  order by h.host $$;



-- time per website (domain), only while working: unlocked and not away
create or replace function public.tb_site_usage(p_start bigint, p_end bigint) returns table(usr text, site text, seconds bigint)
language sql stable set search_path = public as $$
  with x as (
    select coalesce(h.crm_user, h.os_user) as u, h.site, h.state, h.idle,
           least(h.ts - coalesce(lag(h.ts) over (partition by coalesce(h.crm_user, h.os_user), h.host order by h.ts), h.ts - 15), 45) as gap
    from tb_hb h where h.org_id = tb_me_org() and h.ts >= p_start and h.ts < p_end)
  select u, site, sum(gap)::bigint from x
  where state = 'active' and idle <= tb_away_secs() and site is not null
  group by 1, 2 order by 1, 3 desc $$;

revoke all on function public.tb_live_status(), public.tb_site_usage(bigint, bigint),
  public.tb_ingest_heartbeat(text, text, text, text, text, int, text, jsonb, text, jsonb, jsonb, jsonb, text, text) from public, anon, authenticated;
grant execute on function public.tb_live_status(), public.tb_site_usage(bigint, bigint) to authenticated;
grant execute on function public.tb_ingest_heartbeat(text, text, text, text, text, int, text, jsonb, text, jsonb, jsonb, jsonb, text, text) to anon, authenticated;
