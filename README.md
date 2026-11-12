# Radar Legislativo ALERJ — versão 2.0 (Supabase)

Esta versão substitui Google Sheets/Drive por um banco PostgreSQL no Supabase. O painel é um site estático com autenticação. A coleta diária é executada pelo GitHub Actions.

## Regra de armazenamento dos PDFs

**Os PDFs da IOERJ não são arquivados pelo Radar.** O robô consulta a página da Parte II, baixa o PDF para memória durante a execução, extrai o texto, processa os atos e descarta a cópia temporária. O banco guarda somente:

- metadados da edição e da publicação;
- texto extraído de cada ato, necessário para consulta e pesquisa;
- classificação temática, justificativa e resumo das publicações relevantes;
- URL oficial da página/PDF da IOERJ e hash de verificação do arquivo.

O botão “Abrir Diário Oficial” abre o endereço original para que o usuário consulte ou baixe o PDF diretamente da IOERJ. A disponibilidade do link depende do portal oficial.

---

# PARTE 1 — Criar o banco e o acesso privado

## 1. Criar projeto no Supabase

1. Acesse [https://supabase.com](https://supabase.com) e crie uma conta/projeto.
2. Crie um projeto chamado `radar-legislativo-alerj` (ou outro nome de sua preferência).
3. Selecione a região **South America (São Paulo)**, caso esteja disponível na lista de regiões da sua conta.
4. Defina uma senha forte para o banco e guarde-a em local seguro.
5. Aguarde a criação do projeto.

## 2. Criar as tabelas

1. No painel do Supabase, abra **SQL Editor**.
2. Escolha **New query**.
3. Abra o arquivo `supabase/schema.sql` deste pacote.
4. Copie todo o conteúdo e cole no SQL Editor.
5. Execute a consulta.
6. Confirme em **Table Editor** que existem as tabelas `editions`, `publications`, `themes` e `ingestion_runs`.

O script ativa Row Level Security (RLS) e bloqueia a leitura anônima. Apenas usuários autenticados terão acesso às tabelas.

## 3. Configurar usuários autorizados

1. No Supabase, abra **Authentication → Providers → Email**.
2. Mantenha habilitado o login por e-mail e senha.
3. Desative **Allow new users to sign up / Enable sign ups**. Não permita cadastro público.
4. Abra **Authentication → Users** e crie manualmente os usuários que poderão acessar o Radar (por exemplo, o e-mail do responsável pelo painel e de servidores autorizados).
5. Envie/defina a senha por meio do fluxo do próprio Supabase.

As políticas RLS desta versão permitem que usuários autenticados consultem o acervo compartilhado e administrem temas/revisões. Por isso, somente devem ser criadas contas para pessoas autorizadas.

## 4. Obter os dados de conexão

No painel do Supabase, abra **Project Settings → API Keys** (ou use o botão **Connect**) e copie:

- **Project URL**;
- **Publishable key** (ou a chave pública `anon`, se a interface ainda a apresentar);
- **Secret key** (`sb_secret_...`), para uso exclusivo no GitHub Actions. Ela concede acesso administrativo e nunca pode ser colocada no navegador. O worker também aceita a chave legada `service_role` como alternativa de compatibilidade.

**Nunca coloque a secret key ou a chave da API Gemini no HTML, em `config.js`, em um repositório público ou em mensagens.** A publishable key (`sb_publishable_...`) pode estar no frontend porque o banco está protegido por RLS. A Supabase recomenda o par publishable/secret e está descontinuando as chaves legadas `anon` e `service_role` ao final de 2026. ([documentação oficial](https://supabase.com/docs/guides/getting-started/api-keys))

---

# PARTE 2 — Publicar o site

## 5. Configurar o frontend

1. Na pasta do projeto, faça uma cópia de `config.example.js` e nomeie a cópia `config.js`.
2. Abra `config.js` e preencha:

```js
window.RADAR_CONFIG = {
  SUPABASE_URL: "https://SEU-PROJETO.supabase.co",
  SUPABASE_PUBLISHABLE_KEY: "SUA_CHAVE_PUBLICAVEL"
};
```

3. Não inclua outras chaves nesse arquivo. `config.js` contém somente URL do projeto e chave publicável; por isso **deve ser enviado ao repositório** junto ao site. A chave pública não substitui as políticas RLS.

## 6. Criar o repositório e publicar no GitHub Pages

1. Crie um repositório GitHub para o projeto.
2. Envie os arquivos e pastas deste pacote, incluindo o `config.js` que você acabou de criar. Ele contém somente a chave publicável; não envie `SUPABASE_SERVICE_ROLE_KEY` nem `GEMINI_API_KEY` em nenhum arquivo.
3. No GitHub, abra **Settings → Pages**.
4. Em **Build and deployment**, escolha **Deploy from a branch**.
5. Selecione a branch `main` e a pasta `/ (root)`; salve.
6. O GitHub exibirá o endereço do site, normalmente `https://SEU-USUARIO.github.io/NOME-DO-REPOSITORIO/`.

**Privacidade:** o GitHub Pages entrega os arquivos estáticos do site publicamente; entretanto, a aplicação exige login e as tabelas não respondem a visitantes anônimos por causa da autenticação/RLS. O código do frontend não contém credenciais administrativas. Se a exigência for ocultar até mesmo a tela de login e os arquivos estáticos, será necessário hospedar o frontend em um serviço com controle de acesso na camada web (por exemplo, Cloudflare Access), além da proteção do banco.

## 7. Configurar a URL autorizada no Supabase

1. No Supabase, abra **Authentication → URL Configuration**.
2. Em **Site URL**, informe a URL do GitHub Pages.
3. Em **Redirect URLs**, adicione a URL do site e, se usar caminho de repositório, a URL completa com a barra final.
4. Salve.

O frontend utiliza e-mail e senha, sem cadastro público. Essas URLs servem também para a gestão da sessão/autenticação.

---

# PARTE 3 — Instalar a coleta automática

## 8. Criar segredos no GitHub

No repositório GitHub:

1. Abra **Settings → Secrets and variables → Actions**.
2. Em **Secrets**, escolha **New repository secret** e crie:
   - `SUPABASE_URL` — URL do projeto Supabase;
   - `SUPABASE_SECRET_KEY` — secret key administrativa do Supabase (`sb_secret_...`);
   - `GEMINI_API_KEY` — chave da API Gemini.
3. Em **Variables**, crie opcionalmente:
   - `GEMINI_MODEL` — modelo Gemini disponível na sua conta (o padrão do projeto é `gemini-3.8-flash`).

As três primeiras são secrets, não variables. Não as coloque em arquivos do repositório. A variável do workflow já está configurada para ler `SUPABASE_SECRET_KEY`.

## 9. Ativar GitHub Actions

1. **Antes da primeira coleta**, abra o site, entre com o usuário autorizado e cadastre os temas iniciais em **Temas monitorados**. Isso é importante: os temas são usados no momento do processamento e esta versão não reclassifica automaticamente edições antigas.
2. No repositório, abra **Actions**.
3. Se solicitado, habilite workflows.
4. Abra o workflow **Radar ALERJ — coleta diária**.
5. Clique em **Run workflow** para a primeira execução manual.
6. Abra o job e confira o log. Ao final, o esperado é uma mensagem semelhante a `Concluído: N atos, M relevantes. PDF descartado...`.
7. Abra o Supabase → **Table Editor** e confira se a edição foi registrada em `editions` e os atos em `publications`.

A rotina está programada para todos os dias às **9h15 no fuso `America/Sao_Paulo`**. O GitHub informa que execuções agendadas podem sofrer atrasos em períodos de alta demanda; portanto, não é uma garantia de execução no minuto exato. O workflow também pode ser iniciado manualmente. Em repositórios públicos sem atividade por 60 dias, o GitHub pode desativar workflows agendados; confira periodicamente a aba Actions.


### Migração para Gemini

A versão atual utiliza `gemini-3.8-flash` na coleta e na pesquisa assistida. A troca do provedor de IA **não exige alterações no schema do banco** nem uma nova execução de `supabase/schema.sql`. Ela exige `GEMINI_API_KEY` e `GEMINI_MODEL` no GitHub Actions e também nos segredos das Edge Functions do Supabase, pois são dois ambientes de execução independentes.

O plano gratuito do Gemini tem limites de requisições e tokens, que variam por modelo e projeto. A documentação do Google informa que, na modalidade gratuita, o conteúdo enviado pode ser utilizado para melhorar os produtos. Como o Radar envia textos de publicações oficiais e também os critérios dos temas monitorados, avalie se esses critérios podem revelar informação interna antes de utilizá-lo. Consulte os limites e termos atuais no [preçário oficial](https://ai.google.dev/gemini-api/docs/pricing) e nos [limites de taxa](https://ai.google.dev/gemini-api/docs/rate-limits).

## 10. Instalar a função de pesquisa com IA

A aba “Pesquisar no acervo” usa uma Edge Function do Supabase para recuperar documentos e formular uma resposta com referências. Essa função não expõe a chave da API Gemini ao navegador.

Pré-requisitos: instalar o [Supabase CLI](https://supabase.com/docs/guides/cli) e autenticar-se na conta.

No terminal, na pasta raiz do projeto (a função está em `supabase/functions/ask-radar/index.ts`):

```bash
supabase login
supabase link --project-ref SEU_PROJECT_REF
supabase secrets set GEMINI_API_KEY=SUA_CHAVE_GEMINI
supabase secrets set GEMINI_MODEL=gemini-3.8-flash
supabase functions deploy ask-radar
```

O `SUPABASE_URL` e a chave pública de autenticação são disponibilizados no ambiente da função pelo Supabase. O arquivo `supabase/config.toml` exige JWT válido para invocar a função.

---

# PARTE 4 — Primeiro uso

## 11. Cadastrar os temas

1. Entre no site com um dos usuários criados no Supabase.
2. Abra **Temas monitorados**.
3. Cadastre o nome, a descrição, as palavras-chave e os critérios de inclusão/exclusão.
4. Deixe ativo o tema que deve ser considerado na próxima coleta.

Não pré-cadastrei temas porque o gabinete deve definir o escopo do monitoramento. Exemplos possíveis: Magé; Baixada Fluminense; segurança pública; saúde; educação; TEA e mães atípicas; infraestrutura; transportes; projetos de autoria ou interesse do gabinete.

Os temas salvos passam a valer na coleta seguinte. Nesta versão, a alteração de temas não reclassifica automaticamente o histórico inteiro; as publicações anteriores mantêm a classificação registrada na data do processamento. Uma rotina específica de reclassificação retroativa pode ser acrescentada depois.

## 12. Conferir o primeiro boletim

Na página **Leitura diária**, confira:

- a data e o número da edição, quando identificados;
- o resumo geral da edição;
- o total de publicações identificadas;
- os destaques para os temas ativos;
- as demais publicações agrupadas por tipo;
- o botão para abrir o Diário Oficial original.

Use **Revisar classificação** para marcar uma publicação como relevante ou irrelevante. A revisão humana prevalece na apresentação do Radar.

## 13. Pesquisa dirigida

Na aba **Pesquisar no acervo**, escreva a pergunta em linguagem natural. A ferramenta busca atos indexados no banco e gera uma resposta com referências às publicações encontradas. A resposta não deve ser tratada como substituta do documento original; os links oficiais aparecem junto das fontes.

---

# O que esta versão armazena e o que não armazena

| Informação | Armazenamento |
|---|---|
| Data, ano/número e link da edição | Supabase |
| Link oficial para o PDF | Supabase (apenas URL) |
| Arquivo PDF original | **Não armazenado** |
| Texto extraído de cada publicação | Supabase |
| Resumos e classificações | Supabase |
| Temas e critérios do gabinete | Supabase |
| Histórico de execuções | Supabase |
| Cópia do PDF em Drive/Storage/repositório | **Não criada** |

O arquivo é baixado em memória durante o workflow. Se a extração falhar ou a IOERJ mudar o visualizador, a execução é marcada como erro e fica registrada no histórico; não se deve considerar uma edição processada apenas porque o link foi localizado.

## Limitações conhecidas da primeira versão

- O portal da IOERJ pode alterar HTML, redirecionamentos ou o visualizador. O coletor procura a página “Última edição”, segue o redirecionamento e identifica o link “Parte II (Poder Legislativo)”, mas a primeira execução real é necessária para validar a rotina do dia.
- OCR é aplicado quando uma página do PDF não contém camada textual suficiente; a qualidade depende da resolução e do layout.
- A segmentação em atos é feita por IA e pode precisar de revisão em relações extensas, tabelas ou atos que atravessam páginas.
- A pesquisa textual usa o índice de texto do PostgreSQL e a IA sintetiza os resultados recuperados; não é, nesta primeira versão, uma busca vetorial semântica.
- Resumos individuais são gerados somente para publicações classificadas como relevantes. O resumo geral da edição é gerado uma vez por edição para sustentar a página de leitura diária.
- O link de download aponta para a IOERJ. Se o órgão alterar a disponibilidade/endereço, o link poderá precisar de atualização.

## Custos e serviços

Supabase, GitHub Actions e API Gemini têm planos, franquias e/ou custos que dependem do volume e das condições da conta. A coleta diária consome a API Gemini ao segmentar/classificar os atos e gerar resumos. A pesquisa assistida por IA também consome API. Verifique os planos e limites vigentes antes de colocar em uso contínuo.
