-- Model-specific, project-wide pacing for Gemini API requests.
-- Limits are based on the AI Studio values verified on 2026-11-24.
create table if not exists public.gemini_model_request_gate (
  model_name text primary key,
  next_allowed_at timestamptz not null default '-infinity'::timestamptz,
  constraint gemini_model_request_gate_model_name_check
    check (model_name ~ '^[A-Za-z0-9._-]{1,128}$')
);

alter table public.gemini_model_request_gate enable row level security;
revoke all on table public.gemini_model_request_gate from anon, authenticated;
grant all on table public.gemini_model_request_gate to service_role;

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
begin
  if p_model is null or length(p_model) > 128
     or p_model !~ '^[A-Za-z0-9._-]{1,128}$' then
    raise exception 'Nome de modelo Gemini inválido.';
  end if;

  if p_interval_seconds is null
     or p_interval_seconds < 1
     or p_interval_seconds > 60 then
    raise exception 'Intervalo de reserva inválido.';
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
       + make_interval(secs => p_interval_seconds::double precision)
   where model_name = p_model;

  return v_now;
end;
$function$;

revoke all on function public.reserve_gemini_model_slot(text, numeric) from public, anon;
grant execute on function public.reserve_gemini_model_slot(text, numeric) to authenticated, service_role;
