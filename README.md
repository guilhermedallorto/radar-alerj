# Radar Legislativo ALERJ

Aplicação privada para acompanhar a **Parte II — Poder Legislativo** do Diário Oficial do Estado do Rio de Janeiro.

## Funcionalidades

- Coleta automática da edição regular, diariamente às **9h05** (`America/Sao_Paulo`).
- Segunda verificação diária, às **22h**, para localizar e processar todas as edições extras da Parte II; mais de uma edição extra na mesma data é aceita e identificada separadamente.
- Extração dos documentos diretamente do visualizador oficial PDF.js; PDFs mantidos apenas temporariamente no workflow.
- Segmentação do conteúdo em publicações individuais e armazenamento do texto integral para consulta.
- Temas monitorados particulares de cada usuário autenticado.
- Classificação, destaques, resumos e revisões separados por usuário, com proteção por Supabase RLS. As edições extras são pré-analisadas para cada conta que tenha temas cadastrados.
- Histórico e acompanhamento visual separados para edições regulares e extras.
- Pesquisa assistida por Gemini no acervo, com referências aos documentos oficiais.

## Arquitetura

- **Frontend:** HTML, CSS e JavaScript estático, publicado pelo GitHub Pages.
- **Banco e autenticação:** Supabase Postgres, Auth e Row Level Security.
- **Coleta:** GitHub Actions + Python + Playwright/Chromium + PyMuPDF.
- **IA:** Google Gemini API, chamada pelo worker e por Edge Functions autenticadas.

## Estrutura

```text
.github/workflows/daily-ingest.yml
worker/ingest.py
worker/requirements.txt
supabase/schema.sql
supabase/migrations/20261116_personal_themes_and_extra_editions.sql
supabase/functions/analyze-user-themes/index.ts
supabase/functions/ask-radar/index.ts
index.html
app.js
styles.css
config.js
```

## Implantação

Para atualizar uma instalação existente, siga **[IMPLEMENTACAO_TEMAS_PERSONALIZADOS_E_EDICOES_EXTRAS.md](IMPLEMENTACAO_TEMAS_PERSONALIZADOS_E_EDICOES_EXTRAS.md)**, na ordem indicada. Em especial:

1. Faça backup do Supabase e execute a migração SQL indicada (não reexecute `schema.sql` em uma base existente).
2. Publique as Edge Functions `analyze-user-themes` e `ask-radar`.
3. Atualize o frontend, o worker e o workflow no branch publicado pelo GitHub Pages.
4. Execute um teste manual do workflow escolhendo `both`.

`supabase/schema.sql` é destinado somente à criação de uma instalação nova. A migração em `supabase/migrations/` é o caminho para a base existente.

## Configuração e segredos

O frontend utiliza `config.js` apenas com a URL do Supabase e a chave publicável. Não coloque no frontend, no repositório ou no histórico do Git:

- `SUPABASE_SECRET_KEY` / `SUPABASE_SERVICE_ROLE_KEY`;
- `GEMINI_API_KEY`.

Esses valores devem permanecer nos secrets do GitHub Actions ou do Supabase Edge Functions, conforme o componente que os utiliza. O nome do modelo é configurado pela variável `GEMINI_MODEL`; o valor utilizado neste projeto é `gemini-3.1-flash-lite`.

Mantenha o cadastro público desativado no Supabase Auth. Crie e gerencie as contas autorizadas manualmente.

## Privacidade dos destaques

Os textos das publicações são compartilhados entre as contas porque provêm do mesmo Diário Oficial. Já temas, correspondências temáticas, resumos personalizados e revisões humanas pertencem a cada usuário e são protegidos por RLS. As edições extras novas são analisadas separadamente para cada conta com temas cadastrados na rotina das 22h. Edições regulares e históricas são analisadas na primeira abertura do usuário e reutilizadas enquanto os temas e o conteúdo da edição não mudarem.

## Limitações e operação

- A disponibilidade e a estrutura do portal IOERJ podem mudar. O coletor valida data, conteúdo e identificação da Parte II antes de encaminhar o texto à segmentação.
- A edição extra é procurada na página da data corrente. A checagem noturna não realiza importação retroativa automática.
- A segmentação e a classificação temática são feitas por IA e devem ser conferidas no documento original em casos relevantes.
- A análise personalizada gera consumo adicional da API Gemini para cada usuário/edição não analisado ou atualizado; o cache reduz repetições.
- Verifique as cotas, preços e condições atuais do Gemini, Supabase e GitHub Actions.
