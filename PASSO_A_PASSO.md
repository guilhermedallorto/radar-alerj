# Instalação rápida — Radar Legislativo ALERJ v2 (Gemini)

## 1. Supabase

1. Crie ou utilize o projeto Supabase já configurado para o Radar.
2. **Não execute novamente `supabase/schema.sql` se as tabelas `editions`, `publications`, `themes` e `ingestion_runs` já existem.** A migração para Gemini não altera o banco.
3. Mantenha o login por e-mail habilitado e o cadastro público desativado.
4. Confirme que os usuários autorizados conseguem entrar no Radar.

## 2. Configurar o site

1. Extraia o ZIP dentro de uma cópia local do repositório GitHub, de modo que `index.html` e `app.js` continuem diretamente na raiz do repositório.
2. Mantenha o `config.js` atual: ele contém somente a URL e a chave publicável do Supabase. O ZIP inclui a cópia que estava no arquivo que você enviou; preserve a versão do repositório caso tenha atualizado esses dados depois do envio.
3. Mantenha a estrutura das pastas `.github/`, `worker/` e `supabase/` exatamente como está no ZIP.
4. Remova as cópias antigas que estavam soltas na raiz do repositório: `daily-ingest.yml`, `ingest.py`, `requirements.txt`, `index.ts`, `schema.sql`, `config.toml` e `config.js.js`. Os arquivos correspondentes agora estão nas pastas corretas.
5. Faça commit e push das alterações para a branch principal. No GitHub Pages, mantenha a publicação a partir de `main` e `/ (root)`.

## 3. Cadastrar ou conferir os temas

Antes da primeira coleta, entre no Radar e confira os temas ativos em **Temas monitorados**. Eles serão usados para classificar a próxima edição.

## 4. Criar a chave Gemini

1. Acesse [Google AI Studio — API keys](https://aistudio.google.com/api-keys).
2. Entre com sua conta Google e crie uma chave de API.
3. Copie a chave e guarde-a em local seguro. Não a coloque em `config.js`, no site ou no repositório.
4. Para usar o plano gratuito, confira no AI Studio se o projeto está no nível gratuito e consulte os limites ativos do modelo. O Google informa que o conteúdo enviado no plano gratuito pode ser utilizado para melhorar os produtos; não envie informações pessoais ou confidenciais.

Modelo inicial utilizado neste pacote: `gemini-3.8-flash`.

## 5. Configurar os secrets e variables no GitHub

No repositório GitHub, abra **Settings → Secrets and variables → Actions**.

Em **Repository secrets**, crie ou mantenha:

- `SUPABASE_URL` — Project URL do Supabase.
- `SUPABASE_SECRET_KEY` — Secret key do Supabase (`sb_secret_...`).
- `GEMINI_API_KEY` — chave criada no Google AI Studio.

Em **Repository variables**, crie:

- `GEMINI_MODEL` — valor `gemini-3.8-flash`.

`GEMINI_API_KEY` deve ser um Secret. `GEMINI_MODEL` é uma Variable e pode ser alterada sem editar o código. Não cadastre a chave Gemini como variável pública.

Depois de confirmar que o workflow novo está funcionando, `OPENAI_API_KEY` e `OPENAI_MODEL` podem ser removidos dos Secrets/Variables do GitHub, pois o código atualizado não os utiliza.

## 6. Configurar a chave no Supabase para a pesquisa do acervo

A aba **Pesquisar no acervo** usa uma Edge Function, separada da coleta do GitHub Actions. Por isso, a mesma chave Gemini precisa ser cadastrada também no Supabase.

### Pelo Dashboard

1. Abra o projeto Supabase.
2. Entre em **Edge Functions → Secrets** (em algumas versões, a área aparece como **Project Settings → Edge Functions → Secrets**).
3. Adicione `GEMINI_API_KEY` com a mesma chave criada no Google AI Studio.
4. Adicione `GEMINI_MODEL` com o valor `gemini-3.8-flash`.
5. Salve.

### Alternativa pelo Supabase CLI

Na pasta raiz deste projeto, execute:

```bash
supabase login
supabase link --project-ref SEU_PROJECT_REF
supabase secrets set GEMINI_API_KEY=SUA_CHAVE_GEMINI
supabase secrets set GEMINI_MODEL=gemini-3.8-flash
supabase functions deploy ask-radar
```

Se você já havia cadastrado `OPENAI_API_KEY` e `OPENAI_MODEL` no Supabase, poderá removê-los depois de confirmar que a pesquisa com Gemini está funcionando. Não é necessário remover nem modificar os demais segredos do Supabase.

## 7. Executar a coleta de teste

1. Abra **Actions** no GitHub.
2. Selecione **Radar ALERJ — coleta diária**.
3. Clique em **Run workflow**.
4. Confira se a execução termina com sucesso.
5. No Supabase, confira a edição em `editions`, as publicações em `publications` e o registro em `ingestion_runs`.

A coleta está programada diariamente às 9h15 em `America/Sao_Paulo`, além da execução manual. O worker aguarda alguns segundos entre chamadas para reduzir o risco de exceder os limites gratuitos e faz novas tentativas em erros temporários de limite ou disponibilidade.

## 8. Estrutura de pastas necessária

O pacote já está organizado para que os serviços encontrem seus arquivos:

- `.github/workflows/daily-ingest.yml` — automação diária do GitHub Actions.
- `worker/ingest.py` e `worker/requirements.txt` — coletor e dependências Python.
- `supabase/config.toml` — configuração da função com autenticação JWT.
- `supabase/functions/ask-radar/index.ts` — pesquisa do acervo com Gemini.
- `supabase/schema.sql` — esquema do banco, apenas para referência/instalação inicial.

Não mova esses arquivos para a raiz. O frontend (`index.html`, `app.js`, `styles.css` e `config.js`) permanece na raiz do repositório para o GitHub Pages.
