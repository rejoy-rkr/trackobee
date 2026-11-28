-- Trackobee v13: drop the built-in always-on WhatsApp rule from tb_restricted_hits. A site is now flagged only when it (or its
-- recorded page title) matches an entry the admin added to tb_restricted_sites -- nothing is flagged by default. Idempotent.
create or replace function public.tb_restricted_hits(p_start bigint, p_end bigint) returns table(
  usr text, host text, site text, hits bigint, last_ts bigint, matched text)
language sql stable security definer set search_path = public as $$
  select coalesce(h.crm_user, h.os_user) as u, h.host, coalesce(h.site, ''), count(*)::bigint, max(h.ts)::bigint, 'list'
  from tb_hb h
  where h.org_id = tb_me_org() and h.ts >= p_start and h.ts < p_end and (h.site is not null or h.title is not null)
    and exists (select 1 from tb_restricted_sites r where r.org_id = tb_me_org()
                and (h.site ilike '%' || r.domain || '%' or h.title ilike '%' || r.domain || '%'))
  group by 1, 2, 3 order by 5 desc $$;
