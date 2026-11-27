-- Trackobee v7: employee display names (rename) and "first seen" per employee (for the New tag). Idempotent.
create table if not exists public.tb_people (
  org_id uuid not null references public.tb_orgs(id) on delete cascade,
  usr text not null,                   -- the employee key: website (CRM) user if paired, else the OS user
  name text,                           -- display name chosen by an admin (NULL = show the key)
  first_seen bigint not null,
  primary key (org_id, usr));
alter table public.tb_people enable row level security;
revoke all on public.tb_people from anon, authenticated;
grant select on public.tb_people to authenticated;
drop policy if exists tb_people_sel on public.tb_people;
create policy tb_people_sel on public.tb_people for select to authenticated using (org_id = tb_me_org());

-- every employee is registered the first time a heartbeat names them
create or replace function public.tb_people_seen() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into tb_people(org_id, usr, first_seen) values (new.org_id, coalesce(new.crm_user, new.os_user), new.ts) on conflict do nothing;
  return null;
end $$;
drop trigger if exists tb_people_seen_trg on public.tb_hb;
create trigger tb_people_seen_trg after insert on public.tb_hb for each row execute function public.tb_people_seen();

-- existing employees: first heartbeat still on record (or the computer's first appearance, whichever is earlier)
insert into public.tb_people(org_id, usr, first_seen)
  select org_id, coalesce(crm_user, os_user), min(ts) from public.tb_hb group by 1, 2 on conflict do nothing;

-- rename an employee (admin / sub-admin). An empty name removes the rename.
create or replace function public.tb_set_person_name(p_usr text, p_name text) returns void
language plpgsql security definer set search_path = public as $$
declare v_role text := tb_me_role(); v_org uuid := tb_me_org(); v_name text := nullif(left(btrim(coalesce(p_name, '')), 80), '');
begin
  if v_role is null or v_role not in ('admin', 'sub_admin') then raise exception 'forbidden'; end if;
  insert into tb_people(org_id, usr, name, first_seen) values (v_org, p_usr, v_name, extract(epoch from now())::bigint)
    on conflict (org_id, usr) do update set name = v_name;
end $$;

revoke all on function public.tb_people_seen(), public.tb_set_person_name(text, text) from public, anon, authenticated;
grant execute on function public.tb_set_person_name(text, text) to authenticated;
