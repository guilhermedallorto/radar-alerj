-- Radar ALERJ: temas por usuário e suporte a edições extras.
-- Executar uma única vez no SQL Editor do Supabase, antes de publicar o novo frontend/workflow.

begin;

-- 1) Uma data pode ter uma edição regular e uma edição extra da Parte II.
alter table public.editions
  add column if not exists edition_type text not null default 'regular';

-- edition_key identifica o caderno específico dentro de uma data: regular
-- usa a chave fixa "regular"; cada extra usa um prefixo do hash do PDF.
alter table public.editions
  add column if not exists edition_key text not null default 'regular';

-- Compatibilidade com uma implantação preliminar que permitia apenas uma
-- extra por data: transforma a chave antiga em hash de conteúdo.
update public.editions
set edition_key = case
  when edition_type = 'regular' then 'regular'
  else 'extra-' || left(coalesce(nullif(pdf_sha256, ''), md5(coalesce(source_pdf_url, source_page_url, id::text))), 20)
end;

alter table public.editions
  drop constraint if exists editions_edition_date_key;

alter table public.editions
  drop constraint if exists editions_edition_type_check;

alter table public.editions
  add constraint editions_edition_type_check
  check (edition_type in ('regular', 'extra'));

alter table public.editions
  drop constraint if exists editions_date_type_key;

alter table public.editions
  drop constraint if exists editions_date_key;

drop index if exists public.editions_date_type_idx;

alter table public.editions
  add constraint editions_date_key unique (edition_date, edition_key);

-- 2) Registrar se a execução verificou a edição regular ou a extra.
alter table public.ingestion_runs
  add column if not exists edition_type text not null default 'regular';

alter table public.ingestion_runs
  add column if not exists edition_key text;

alter table public.ingestion_runs
  drop constraint if exists ingestion_runs_edition_type_check;

alter table public.ingestion_runs
  add constraint ingestion_runs_edition_type_check
  check (edition_type in ('regular', 'extra'));

-- Revisões manuais deixam de alterar a linha compartilhada de publications.
-- A versão personalizada é armazenada em user_publication_matches.
drop policy if exists publications_review_authenticated on public.publications;
revoke update on public.publications from authenticated;
revoke update (manual_relevance, manual_note, reviewed_by, reviewed_at)
  on public.publications from authenticated;

-- 3) Converter os temas antigos (globais) em cópias individuais para cada
-- usuário existente, preservando o ponto de partida de todos os gabinetes.
alter table public.themes
  add column if not exists user_id uuid references auth.users(id) on delete cascade;

-- A versão anterior impunha unicidade global apenas ao nome do tema.
alter table public.themes
  drop constraint if exists themes_name_key;

alter table public.themes
  drop constraint if exists themes_user_name_key;

alter table public.themes
  add constraint themes_user_name_key unique (user_id, name);

create index if not exists themes_user_active_priority_idx
  on public.themes (user_id, active, priority);

do $$
declare
  old_theme_count integer;
  user_count integer;
begin
  select count(*) into old_theme_count
  from public.themes
  where user_id is null;

  if old_theme_count > 0 then
    select count(*) into user_count from auth.users;
    if user_count = 0 then
      raise exception 'Há temas antigos sem usuário e nenhuma conta em auth.users. Crie ao menos um usuário e execute novamente a migração.';
    end if;

    create temporary table radar_legacy_themes on commit drop as
      select active, name, description, keywords, include_criteria,
             exclude_criteria, priority
      from public.themes
      where user_id is null;

    insert into public.themes (
      user_id, active, name, description, keywords,
      include_criteria, exclude_criteria, priority
    )
    select
      u.id, t.active, t.name, t.description, t.keywords,
      t.include_criteria, t.exclude_criteria, t.priority
    from auth.users u
    cross join radar_legacy_themes t
    on conflict (user_id, name) do nothing;

    delete from public.themes where user_id is null;
  end if;
end $$;

alter table public.themes
  alter column user_id set not null;

-- 4) Resultado da análise temática particular de cada usuário e edição.
create table if not exists public.user_edition_analyses (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  edition_id uuid not null references public.editions(id) on delete cascade,
  theme_signature text not null,
  status text not null default 'processing'
    check (status in ('processing', 'processed', 'error')),
  publication_count integer not null default 0,
  relevant_count integer not null default 0,
  summary text,
  error text,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  updated_at timestamptz not null default now(),
  constraint user_edition_analyses_user_edition_key unique (user_id, edition_id)
);

-- 5) Correspondência publicação-tema privada por usuário. Inclui revisão humana
-- privada, para que a avaliação de um usuário não altere a de outro.
create table if not exists public.user_publication_matches (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  edition_id uuid not null references public.editions(id) on delete cascade,
  publication_id uuid not null references public.publications(id) on delete cascade,
  theme_signature text not null,
  relevant boolean not null default false,
  matched_themes jsonb not null default '[]'::jsonb,
  confidence numeric(4,3),
  justification text,
  summary text,
  manual_relevance boolean,
  manual_note text,
  reviewed_at timestamptz,
  updated_at timestamptz not null default now(),
  constraint user_publication_matches_user_publication_key unique (user_id, publication_id)
);

create index if not exists user_edition_analyses_user_idx
  on public.user_edition_analyses (user_id, updated_at desc);
create index if not exists user_publication_matches_user_edition_idx
  on public.user_publication_matches (user_id, edition_id);
create index if not exists user_publication_matches_publication_idx
  on public.user_publication_matches (publication_id);

-- 6) RLS: cada usuário só pode ler ou modificar seus próprios temas/análises.
alter table public.themes enable row level security;
alter table public.user_edition_analyses enable row level security;
alter table public.user_publication_matches enable row level security;

drop policy if exists themes_all_authenticated on public.themes;
drop policy if exists themes_owner_only on public.themes;
create policy themes_owner_only on public.themes
  for all to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

drop policy if exists user_edition_analyses_owner_only on public.user_edition_analyses;
create policy user_edition_analyses_owner_only on public.user_edition_analyses
  for all to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

drop policy if exists user_publication_matches_owner_only on public.user_publication_matches;
create policy user_publication_matches_owner_only on public.user_publication_matches
  for all to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

revoke all on public.themes, public.user_edition_analyses, public.user_publication_matches from anon;
revoke all on public.themes, public.user_edition_analyses, public.user_publication_matches from authenticated;
grant select, insert, update, delete on public.themes to authenticated;
grant select, insert, update, delete on public.user_edition_analyses to authenticated;
grant select, insert, update, delete on public.user_publication_matches to authenticated;
grant all on public.themes, public.user_edition_analyses, public.user_publication_matches to service_role;

-- Trigger de atualização para as tabelas novas.
drop trigger if exists user_edition_analyses_set_updated_at on public.user_edition_analyses;
create trigger user_edition_analyses_set_updated_at
  before update on public.user_edition_analyses
  for each row execute function public.set_updated_at();

drop trigger if exists user_publication_matches_set_updated_at on public.user_publication_matches;
create trigger user_publication_matches_set_updated_at
  before update on public.user_publication_matches
  for each row execute function public.set_updated_at();

comment on table public.themes is 'Temas particulares de cada usuário; RLS impede acesso entre usuários.';
comment on table public.user_edition_analyses is 'Resumo e estado da análise temática específica de cada usuário e edição.';
comment on table public.user_publication_matches is 'Classificação e revisão privada por usuário e publicação.';
comment on column public.editions.edition_type is 'regular = caderno diário normal; extra = caderno identificado pela IOERJ como EDIÇÃO EXTRA.';
comment on column public.editions.edition_key is 'Identificador estável do caderno dentro da data; permite múltiplas edições extras no mesmo dia.';

commit;
