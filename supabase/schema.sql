-- Radar Legislativo ALERJ — Supabase schema v2
-- Execute no SQL Editor de um projeto Supabase novo.

create extension if not exists pgcrypto;

create table if not exists public.editions (
  id uuid primary key default gen_random_uuid(),
  edition_date date not null unique,
  edition_label text,
  source_page_url text not null,
  source_pdf_url text,
  pdf_sha256 text,
  status text not null default 'found' check (status in ('found','processed','error','no_pdf')),
  collected_at timestamptz,
  processed_at timestamptz,
  publication_count integer not null default 0,
  relevant_count integer not null default 0,
  daily_summary text,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.themes (
  id uuid primary key default gen_random_uuid(),
  active boolean not null default true,
  name text not null unique,
  description text not null default '',
  keywords text[] not null default '{}',
  include_criteria text not null default '',
  exclude_criteria text not null default '',
  priority integer not null default 3 check (priority between 1 and 5),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.publications (
  id uuid primary key default gen_random_uuid(),
  edition_id uuid not null references public.editions(id) on delete cascade,
  external_key text not null unique,
  page_start integer,
  page_end integer,
  kind text not null default 'Não identificado',
  title text not null default 'Publicação sem título',
  body_text text not null,
  organization text,
  people jsonb not null default '[]'::jsonb,
  municipalities jsonb not null default '[]'::jsonb,
  amounts jsonb not null default '[]'::jsonb,
  ai_relevant boolean not null default false,
  matched_themes jsonb not null default '[]'::jsonb,
  confidence numeric(4,3),
  justification text,
  summary text,
  source_url text not null,
  manual_relevance boolean,
  manual_note text,
  reviewed_by uuid references auth.users(id) on delete set null,
  reviewed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  search_vector tsvector generated always as (
    to_tsvector('portuguese', coalesce(title,'') || ' ' || coalesce(kind,'') || ' ' ||
      coalesce(organization,'') || ' ' || coalesce(body_text,''))
  ) stored
);

create index if not exists editions_date_idx on public.editions (edition_date desc);
create index if not exists publications_edition_idx on public.publications (edition_id);
create index if not exists publications_relevance_idx on public.publications (ai_relevant, manual_relevance);
create index if not exists publications_search_idx on public.publications using gin (search_vector);
create index if not exists publications_created_idx on public.publications (created_at desc);
create index if not exists themes_active_idx on public.themes (active, priority);

create table if not exists public.ingestion_runs (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null check (status in ('running','success','warning','error')),
  edition_date date,
  message text,
  publications_found integer not null default 0,
  publications_relevant integer not null default 0
);

-- Atualização automática do updated_at.
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists editions_set_updated_at on public.editions;
create trigger editions_set_updated_at before update on public.editions
for each row execute function public.set_updated_at();

drop trigger if exists themes_set_updated_at on public.themes;
create trigger themes_set_updated_at before update on public.themes
for each row execute function public.set_updated_at();

drop trigger if exists publications_set_updated_at on public.publications;
create trigger publications_set_updated_at before update on public.publications
for each row execute function public.set_updated_at();

-- Pesquisa textual em português. A RLS da tabela continua sendo respeitada.
create or replace function public.search_publications(search_query text, result_limit integer default 30)
returns setof public.publications
language sql stable security invoker set search_path = public
as $$
  select p.*
  from public.publications p
  where p.search_vector @@ websearch_to_tsquery('portuguese', coalesce(search_query,''))
  order by ts_rank(p.search_vector, websearch_to_tsquery('portuguese', coalesce(search_query,''))) desc,
           p.created_at desc
  limit least(greatest(coalesce(result_limit,30),1),50);
$$;

-- Acesso privado: nenhum visitante anônimo pode consultar as tabelas.
alter table public.editions enable row level security;
alter table public.themes enable row level security;
alter table public.publications enable row level security;
alter table public.ingestion_runs enable row level security;

revoke all on public.editions, public.themes, public.publications, public.ingestion_runs from anon;
revoke all on public.editions, public.themes, public.publications, public.ingestion_runs from authenticated;

grant select on public.editions, public.ingestion_runs to authenticated;
grant select, insert, update, delete on public.themes to authenticated;
grant select on public.publications to authenticated;
grant update (manual_relevance, manual_note, reviewed_by, reviewed_at) on public.publications to authenticated;
grant all on public.editions, public.themes, public.publications, public.ingestion_runs to service_role;
grant execute on function public.search_publications(text, integer) to authenticated;

-- Qualquer conta criada manualmente pelo administrador pode acessar os dados.
-- Não habilite cadastro público no Supabase Auth.
drop policy if exists editions_read_authenticated on public.editions;
create policy editions_read_authenticated on public.editions for select to authenticated
using ((select auth.uid()) is not null);

drop policy if exists runs_read_authenticated on public.ingestion_runs;
create policy runs_read_authenticated on public.ingestion_runs for select to authenticated
using ((select auth.uid()) is not null);

drop policy if exists themes_all_authenticated on public.themes;
create policy themes_all_authenticated on public.themes for all to authenticated
using ((select auth.uid()) is not null)
with check ((select auth.uid()) is not null);

drop policy if exists publications_read_authenticated on public.publications;
create policy publications_read_authenticated on public.publications for select to authenticated
using ((select auth.uid()) is not null);

drop policy if exists publications_review_authenticated on public.publications;
create policy publications_review_authenticated on public.publications for update to authenticated
using ((select auth.uid()) is not null)
with check ((select auth.uid()) is not null);

comment on table public.editions is 'Metadados e resumo das edições da Parte II. Não armazena arquivos PDF.';
comment on column public.editions.source_pdf_url is 'URL oficial do PDF na IOERJ; o arquivo não é copiado para o Radar.';
comment on table public.publications is 'Atos individualizados; body_text guarda texto extraído, não o PDF original.';
