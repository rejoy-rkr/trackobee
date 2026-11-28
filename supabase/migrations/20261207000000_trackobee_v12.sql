-- Trackobee v12: restricted websites (content filter, reporting only — the agent never blocks anything). Admin/sub-admin keeps a
-- list of domains or keywords; tb_restricted_hits reports who visited one (matched against both the recorded domain and the
-- recorded page title, by substring — "facebook" also catches "m.facebook.com"). Matching is by DOMAIN/TITLE, the same units the
-- agent/extension already record (never a full URL, path or query string, per its own disclosure). WhatsApp (regular or
-- Business — both show "WhatsApp" in the domain or the tab title) is always flagged, independent of the admin's own list.
-- Idempotent.
create table if not exists public.tb_restricted_sites (
  id bigint generated always as identity primary key,
  org_id uuid not null references public.tb_orgs(id) on delete cascade,
  domain text not null, created_at bigint not null default extract(epoch from now())::bigint,
  unique (org_id, domain));
alter table public.tb_restricted_sites enable row level security;
revoke all on public.tb_restricted_sites from anon, authenticated;
grant select, insert, update, delete on public.tb_restricted_sites to authenticated;
drop policy if exists tb_rs_sel on public.tb_restricted_sites;
create policy tb_rs_sel on public.tb_restricted_sites for select to authenticated using (org_id = tb_me_org());
drop policy if exists tb_rs_ins on public.tb_restricted_sites;
create policy tb_rs_ins on public.tb_restricted_sites for insert to authenticated
  with check (org_id = tb_me_org() and tb_me_role() in ('admin','sub_admin'));
drop policy if exists tb_rs_upd on public.tb_restricted_sites;
create policy tb_rs_upd on public.tb_restricted_sites for update to authenticated
  using (org_id = tb_me_org() and tb_me_role() in ('admin','sub_admin'))
  with check (org_id = tb_me_org() and tb_me_role() in ('admin','sub_admin'));
drop policy if exists tb_rs_del on public.tb_restricted_sites;
create policy tb_rs_del on public.tb_restricted_sites for delete to authenticated
  using (org_id = tb_me_org() and tb_me_role() in ('admin','sub_admin'));

-- who visited a restricted site, and how much, within a date range. "matched" says why: the admin's list, the built-in
-- WhatsApp rule, or both.
create or replace function public.tb_restricted_hits(p_start bigint, p_end bigint) returns table(
  usr text, host text, site text, hits bigint, last_ts bigint, matched text)
language sql stable security definer set search_path = public as $$
  with x as (
    select coalesce(h.crm_user, h.os_user) as u, h.host, h.site, h.ts,
      exists (select 1 from tb_restricted_sites r where r.org_id = tb_me_org()
              and (h.site ilike '%' || r.domain || '%' or h.title ilike '%' || r.domain || '%')) as hit_list,
      (h.site ilike '%whatsapp%' or h.title ilike '%whatsapp%') as hit_wa
    from tb_hb h
    where h.org_id = tb_me_org() and h.ts >= p_start and h.ts < p_end and (h.site is not null or h.title is not null))
  select u, host, coalesce(site, ''), count(*)::bigint, max(ts)::bigint,
    case when bool_or(hit_list) and bool_or(hit_wa) then 'list, whatsapp'
         when bool_or(hit_list) then 'list' else 'whatsapp' end
  from x where hit_list or hit_wa
  group by 1, 2, 3 order by 5 desc $$;

revoke all on function public.tb_restricted_hits(bigint, bigint) from public, anon, authenticated;
grant execute on function public.tb_restricted_hits(bigint, bigint) to authenticated;
