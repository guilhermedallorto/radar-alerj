-- Return the deployment to the stable Part II-only Radar.
-- Keep the original shared limiter used by the stable worker and Edge Functions.
begin;

drop function if exists public.reserve_gemini_model_slot(text, numeric);
drop table if exists public.gemini_model_request_gate;

revoke all on function public.reserve_gemini_slot(numeric) from public, anon;
grant execute on function public.reserve_gemini_slot(numeric)
  to authenticated, service_role;

-- These are only operational records created by the experimental Part I worker.
-- No editions, publications, themes, or user-specific analyses are removed.
delete from public.ingestion_runs
where edition_key like 'executive-%';

commit;
