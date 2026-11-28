-- Trackobee v9: the first login of the day, and activity per computer. Idempotent.

-- First check-in of each local day, per employee and computer = the first heartbeat of that day where the screen is unlocked AND the
-- person is really working (activity within the away limit). So a PC left on overnight and unlocked the next morning counts as that
-- morning's login, and a PC left unlocked but untouched does not count until someone uses it.
create or replace function public.tb_day_checkins(p_start bigint, p_end bigint, p_tz text default 'UTC') returns table(
  day date, usr text, host text, checkin bigint, lastwork bigint)
language sql stable set search_path = public as $$
  with z as (select case when exists (select 1 from pg_timezone_names n where n.name = p_tz) then p_tz else 'UTC' end as tz)
  select (to_timestamp(h.ts) at time zone z.tz)::date, coalesce(h.crm_user, h.os_user), h.host, min(h.ts), max(h.ts)
  from tb_hb h cross join z
  where h.org_id = tb_me_org() and h.ts >= p_start and h.ts < p_end and h.state = 'active' and h.idle <= tb_away_secs()
  group by 1, 2, 3 order by 1 desc, 2, 3 $$;

-- time per application / website / page, per employee AND computer (so it can be searched by serial number)
create or replace function public.tb_activity_usage(p_start bigint, p_end bigint) returns table(
  usr text, host text, app text, site text, title text, seconds bigint)
language sql stable set search_path = public as $$
  with x as (
    select coalesce(h.crm_user, h.os_user) as u, h.host, h.app, h.site, h.title, h.state, h.idle,
           least(h.ts - coalesce(lag(h.ts) over (partition by coalesce(h.crm_user, h.os_user), h.host order by h.ts), h.ts - 15), 45) as gap
    from tb_hb h where h.org_id = tb_me_org() and h.ts >= p_start and h.ts < p_end)
  select u, host, coalesce(app, 'Unknown'), site, title, sum(gap)::bigint from x
  where state = 'active' and idle <= tb_away_secs()
  group by 1, 2, 3, 4, 5 order by 1, 2, 6 desc $$;

revoke all on function public.tb_day_checkins(bigint, bigint, text), public.tb_activity_usage(bigint, bigint) from public, anon, authenticated;
grant execute on function public.tb_day_checkins(bigint, bigint, text), public.tb_activity_usage(bigint, bigint) to authenticated;
