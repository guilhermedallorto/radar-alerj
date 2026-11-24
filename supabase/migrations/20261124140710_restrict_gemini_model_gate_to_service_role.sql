-- Restrict Gemini rate-gate administration to backend service credentials.
-- Enforce the interval policy in the database instead of trusting callers.
create or replace function public.reserve_gemini_model_slot(
  p_model text,
  p_interval_seconds numeric default 6.0
)
returns timestamptz
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
  v_next timestamptz;
  v_now timestamptz;
  v_required_interval numeric;
begin
  if p_model is null or length(p_model) > 128
     or p_model !~ '^[A-Za-z0-9._-]{1,128}$' then
    raise exception 'Nome de modelo Gemini inválido.';
  end if;

  v_required_interval := case
    when p_model in ('gemini-3.1-flash-lite', 'gemini-3.5-flash-lite') then 5.5
    else 13.0
  end;

  if p_interval_seconds is null
     or p_interval_seconds <> v_required_interval then
    raise exception 'O intervalo de reserva é definido pelo projeto para cada modelo.';
  end if;

  insert into public.gemini_model_request_gate (model_name)
  values (p_model)
  on conflict (model_name) do nothing;

  select next_allowed_at
    into v_next
    from public.gemini_model_request_gate
   where model_name = p_model
   for update;

  v_now := clock_timestamp();

  if v_next > v_now then
    perform pg_sleep(extract(epoch from (v_next - v_now)));
  end if;

  v_now := clock_timestamp();

  update public.gemini_model_request_gate
     set next_allowed_at = v_now
       + make_interval(secs => v_required_interval::double precision)
   where model_name = p_model;

  return v_now;
end;
$function$;

revoke all on function public.reserve_gemini_model_slot(text, numeric) from public, anon, authenticated;
grant execute on function public.reserve_gemini_model_slot(text, numeric) to service_role;

-- The former global limiter is no longer called by authenticated Edge Functions.
revoke all on function public.reserve_gemini_slot(numeric) from public, anon, authenticated;
grant execute on function public.reserve_gemini_slot(numeric) to service_role;
