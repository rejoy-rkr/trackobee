-- Trackobee v8: nicknames belong to a COMPUTER (its serial number), not to a user name, so two people with the same user name keep separate nicknames.
-- Idempotent.
create table if not exists public.tb_nicknames (
  org_id uuid not null references public.tb_orgs(id) on delete cascade,
  key text not null,                  -- the computer's serial number, or 'host:<computer name>' when it has none
  name text not null,
  primary key (org_id, key));
alter table public.tb_nicknames enable row level security;
revoke all on public.tb_nicknames from anon, authenticated;
grant select on public.tb_nicknames to authenticated;
drop policy if exists tb_nick_sel on public.tb_nicknames;
create policy tb_nick_sel on public.tb_nicknames for select to authenticated using (org_id = tb_me_org());

create or replace function public.tb_set_nickname(p_key text, p_name text) returns void
language plpgsql security definer set search_path = public as $$
declare v_role text := tb_me_role(); v_org uuid := tb_me_org(); v_name text := nullif(left(btrim(coalesce(p_name, '')), 80), '');
begin
  if v_role is null or v_role not in ('admin', 'sub_admin') then raise exception 'forbidden'; end if;
  if p_key is null or btrim(p_key) = '' then raise exception 'missing computer'; end if;
  if v_name is null then delete from tb_nicknames where org_id = v_org and key = p_key;
  else insert into tb_nicknames(org_id, key, name) values (v_org, p_key, v_name)
         on conflict (org_id, key) do update set name = excluded.name; end if;
end $$;
revoke all on function public.tb_set_nickname(text, text) from public, anon, authenticated;
grant execute on function public.tb_set_nickname(text, text) to authenticated;

-- carry over the few user-level renames made before: attach each to that user's most recent computer
insert into public.tb_nicknames(org_id, key, name)
  select p.org_id, coalesce(nullif(btrim(coalesce(d.info->>'serial_number', d.info->>'board_serial', '')), ''), 'host:' || l.host), p.name
  from public.tb_people p
  join lateral (select h.host from public.tb_hb h where h.org_id = p.org_id and coalesce(h.crm_user, h.os_user) = p.usr order by h.id desc limit 1) l on true
  left join public.tb_devices d on d.org_id = p.org_id and d.host = l.host
  where p.name is not null
  on conflict do nothing;
update public.tb_people set name = null where name is not null;
