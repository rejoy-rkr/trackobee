-- Trackobee v11: force-update a computer's agent from the dashboard. Admin/sub-admin sets a one-shot flag; the next
-- heartbeat from that computer gets told to check for an update right now (instead of waiting for its normal few-hour
-- schedule), and the flag clears itself as soon as it has been delivered once. Idempotent.
alter table public.tb_devices add column if not exists force_update_at bigint;

drop function if exists public.tb_ingest_heartbeat(text, text, text, text, text, int, text, jsonb, text, jsonb, jsonb, jsonb, text, text, text);
create or replace function public.tb_ingest_heartbeat(
  p_agent_token text, p_host text, p_ip text, p_os_user text, p_state text, p_idle int,
  p_token text default null, p_events jsonb default '[]',
  p_app text default null, p_sys jsonb default null, p_telemetry jsonb default null, p_location jsonb default null, p_title text default null, p_site text default null, p_acct text default null) returns jsonb
language plpgsql security definer set search_path = public, extensions as $$
declare v_org uuid; v_crm text; v_hdr jsonb; v_pub text; v_fac text; v_ovr text; v_now bigint := extract(epoch from now())::bigint;
        e jsonb; n int := 0; v_user text; v_last text; v_type text; v_ts bigint; v_ver text; v_acct text; v_force boolean;
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
  v_acct := nullif(left(lower(btrim(coalesce(p_acct, ''))), 100), '');
  if v_crm is null then v_crm := v_acct; end if;                                                   -- else: the Chrome sign-in
  begin v_hdr := coalesce(nullif(current_setting('request.headers', true), '')::jsonb, '{}'); exception when others then v_hdr := '{}'; end;
  v_pub := nullif(btrim(coalesce(nullif(v_hdr->>'cf-connecting-ip', ''), split_part(coalesce(v_hdr->>'x-forwarded-for', ''), ',', 1))), '');
  begin perform v_pub::inet; exception when others then v_pub := null; end;
  select d.facility_override into v_ovr from tb_devices d where d.org_id = v_org and d.host = p_host;
  v_fac := case when v_ovr = '__none__' then null when v_ovr is not null then v_ovr else tb_facility_for(v_org, p_ip, v_pub) end;
  insert into tb_hb(org_id, ts, host, ip, pub_ip, os_user, crm_user, facility, state, idle, app, title, site, acct)
    values (v_org, v_now, p_host, left(p_ip, 64), v_pub, p_os_user, v_crm, v_fac, p_state, greatest(coalesce(p_idle, 0), 0),
            nullif(left(btrim(coalesce(p_app, '')), 100), ''), nullif(left(btrim(coalesce(p_title, '')), 150), ''), nullif(left(btrim(coalesce(p_site, '')), 100), ''), v_acct);

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
      updated_at = v_now
    returning (tb_devices.force_update_at is not null) into v_force;
  if v_force then update tb_devices set force_update_at = null where org_id = v_org and host = p_host; end if;

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
  return jsonb_build_object('ok', true, 'interval', 15, 'force_update', coalesce(v_force, false));
end $$;

-- ============================================================ ask a computer's agent to check for an update right now
--   p_host: a specific computer  |  NULL = every computer in the organization
create or replace function public.tb_force_update(p_host text default null) returns int
language plpgsql security definer set search_path = public as $$
declare v_role text := tb_me_role(); v_org uuid := tb_me_org(); v_now bigint := extract(epoch from now())::bigint; v_n int;
begin
  if v_role is null or v_role not in ('admin', 'sub_admin') then raise exception 'forbidden'; end if;
  update tb_devices set force_update_at = v_now where org_id = v_org and (p_host is null or host = p_host);
  get diagnostics v_n = row_count;
  if p_host is not null and v_n = 0 then raise exception 'computer not found'; end if;
  return v_n;
end $$;

revoke all on function public.tb_ingest_heartbeat(text, text, text, text, text, int, text, jsonb, text, jsonb, jsonb, jsonb, text, text, text),
  public.tb_force_update(text) from public, anon, authenticated;
grant execute on function public.tb_ingest_heartbeat(text, text, text, text, text, int, text, jsonb, text, jsonb, jsonb, jsonb, text, text, text) to anon, authenticated;
grant execute on function public.tb_force_update(text) to authenticated;
