# Instalação rápida — Radar Legislativo ALERJ v2

## Antes de começar

Você precisará de contas no Supabase, GitHub e OpenAI. O pacote não exige Google Sheets nem Google Drive.

## 1. Supabase

1. Crie um projeto no Supabase, preferencialmente na região São Paulo.
2. Abra **SQL Editor → New query**.
3. Cole e execute todo o arquivo `supabase/schema.sql`.
4. Em **Authentication**, deixe o login por e-mail habilitado e desative o cadastro público.
5. Em **Authentication → Users**, crie manualmente os usuários autorizados.
6. Em **Project Settings → API Keys**, copie o Project URL, a Publishable key (`sb_publishable_...`) e a Secret key (`sb_secret_...`).

## 2. Configurar o site

1. Copie `config.example.js` para um novo arquivo chamado `config.js`.
2. Preencha nele somente o Project URL e a Publishable key.
3. Crie um repositório GitHub e envie os arquivos do pacote, incluindo `config.js`.
4. No repositório, abra **Settings → Pages**, escolha **Deploy from a branch**, selecione `main` e `/ (root)`.
5. Copie a URL gerada pelo GitHub Pages.
6. No Supabase, em **Authentication → URL Configuration**, informe a URL do site.
7. Abra o site e faça login.

## 3. Cadastrar os temas antes da primeira coleta

1. No site, abra **Temas monitorados**.
2. Cadastre os temas, critérios de inclusão e exclusão e palavras-chave.
3. Salve os temas que devem estar ativos.

Faça isso antes de disparar a primeira coleta. A versão 2 aplica os critérios no processamento da edição e não refaz automaticamente a classificação do histórico quando um tema é alterado.

## 4. Configurar a coleta diária

1. No repositório GitHub, abra **Settings → Secrets and variables → Actions**.
2. Cadastre como **Repository secrets**:
   - `SUPABASE_URL`
   - `SUPABASE_SECRET_KEY`
   - `OPENAI_API_KEY`
3. Opcional: em **Variables**, cadastre `OPENAI_MODEL` com um modelo disponível na sua conta. O padrão é `gpt-4.1-mini`.
4. Abra **Actions** e habilite os workflows.
5. Selecione **Radar ALERJ — coleta diária** e clique em **Run workflow**.
6. Confira o log da execução. Verifique no Supabase as tabelas `editions`, `publications` e `ingestion_runs`.

O workflow está programado para executar todos os dias às 9h15 em `America/Sao_Paulo`. A execução manual também permanece disponível.

## 5. Ativar a pesquisa com IA

Com o Supabase CLI instalado, na pasta do projeto:

```bash
supabase login
supabase link --project-ref SEU_PROJECT_REF
supabase secrets set OPENAI_API_KEY=SUA_CHAVE_OPENAI
supabase secrets set OPENAI_MODEL=gpt-4.1-mini
supabase functions deploy ask-radar
```

## 6. O que cada serviço guarda

- **Supabase:** texto de cada publicação, metadados, temas, resumos, classificações e links oficiais.
- **GitHub:** código do site e rotina de coleta.
- **OpenAI:** recebe o texto dos lotes para segmentar/classificar e gerar resumos/respostas.
- **IOERJ:** continua sendo a única hospedagem dos PDFs originais.

O PDF é baixado para memória durante o processamento e descartado no fim da execução. Nenhuma cópia vai para Supabase Storage, Drive ou GitHub. O Radar guarda apenas a URL oficial de consulta/baixa.
