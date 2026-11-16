# Radar Legislativo ALERJ — temas personalizados e edições extras

## O que esta atualização faz

1. **Temas por usuário:** cada conta autenticada tem sua própria tabela de temas. Uma pessoa pode cadastrar, editar, ativar ou desativar temas sem alterar a configuração de outra conta.
2. **Análise individual:** a coleta central identifica e armazena todas as publicações, sem aplicar temas de um usuário a todos. Quando o usuário abre uma edição, a função autenticada `analyze-user-themes` classifica as publicações segundo os temas ativos daquela conta e grava os resultados em tabelas protegidas por RLS.
3. **Cache individual:** o resultado é reutilizado enquanto os temas e o conteúdo da edição permanecerem iguais. Se os temas forem alterados, a edição aberta é reanalisada. As edições anteriores são analisadas para aquela conta quando forem abertas.
4. **Edição extra:** o workflow realiza a coleta regular às 9h05 e uma checagem adicional às 22h, ambas em `America/Sao_Paulo`. A checagem noturna consulta a data local corrente e procura todos os links da Parte II identificados pela IOERJ como `EDIÇÃO EXTRA`, inclusive quando há mais de uma extra na mesma data. Cada PDF recebe uma chave própria derivada do conteúdo, evitando sobrescrita.
5. **Acompanhamento separado:** a página inicial passa a mostrar grupos distintos de edições regulares e extras, além do estado mais recente das duas rotinas. Edições extras aparecem com identificação explícita.
6. **Pesquisa do acervo:** `ask-radar` continua respeitando o usuário autenticado e usa apenas resumos/classificações pessoais. Os resumos antigos guardados em campos compartilhados não são apresentados como se fossem personalizados.

O exemplo oficial de 29/10/2026 apresenta dois links distintos na página da data: `Parte II (Poder Legislativo)` e `Parte II (Poder Legislativo) EDIÇÃO EXTRA`. O seletor do coletor diferencia esses rótulos; não classifica a Parte I (Poder Executivo) como documento legislativo. A conferência complementar de 04/09/2026 também foi usada como teste de múltiplas extras, pois a página oficial lista dois links de Parte II EDIÇÃO EXTRA nessa data.

## Ordem de implantação no projeto existente

> Faça backup do banco antes da migração. Não execute `supabase/schema.sql` sobre um projeto que já contém dados. Para uma base existente, use apenas a migração indicada abaixo.

### 1. Atualizar o banco Supabase

1. Abra o projeto Supabase e entre em **SQL Editor**.
2. Abra o arquivo `supabase/migrations/20261116_personal_themes_and_extra_editions.sql` deste pacote.
3. Copie todo o conteúdo, cole no SQL Editor e execute uma única vez.
4. Confirme que a execução terminou sem erro.

A migração:
- adiciona `edition_type` às edições e às tentativas de coleta (`regular` ou `extra`);
- troca a unicidade de data por unicidade de data/identificador do caderno, permitindo mais de uma edição extra no mesmo dia;
- cria as tabelas de análise e classificação por usuário;
- adiciona `user_id` aos temas e aplica políticas RLS;
- copia os temas legados para cada conta já existente, como ponto de partida individual;
- remove a permissão de revisão global em `publications`, pois as revisões passam a ser guardadas por usuário.

**Importante sobre os temas existentes:** as contas que já existem receberão uma cópia inicial dos temas que antes eram compartilhados. Depois da migração, as alterações de uma conta não afetam as demais. Uma conta criada posteriormente começa sem temas e deve cadastrá-los em seu próprio acesso.

Se o SQL indicar que existem temas antigos mas nenhuma conta em `auth.users`, interrompa a implantação e verifique a configuração das contas antes de tentar novamente. Não remova nem substitua a tabela manualmente.

### 2. Publicar as Edge Functions

A análise individual precisa rodar no Supabase sob a sessão autenticada do usuário. É necessário publicar a função nova e atualizar a função de pesquisa.

Com o Supabase CLI instalado e o diretório do projeto aberto no terminal:

```bash
supabase login
supabase link --project-ref SEU_PROJECT_REF
supabase functions deploy analyze-user-themes
supabase functions deploy ask-radar
```

O arquivo `supabase/config.toml` já configura JWT obrigatório para as duas funções.

A função utiliza os secrets já usados pela pesquisa do acervo. Confirme no Supabase em **Edge Functions → Secrets** que existem `GEMINI_API_KEY` e `GEMINI_MODEL`. O modelo atualmente esperado no projeto é `gemini-3.1-flash-lite`. Não coloque a chave no frontend nem no repositório. Se esses secrets já existem e a pesquisa `ask-radar` funciona, não é necessário cadastrar outra chave.

### 3. Atualizar o site

Substitua no repositório os arquivos da raiz pelos desta versão:

- `index.html`
- `app.js`
- `styles.css`

Mantenha o `config.js` atual do seu repositório, caso tenha atualizado a URL/chave publicável desde o ZIP enviado. A chave publicável pode permanecer no frontend; a chave secreta do Supabase e a chave Gemini, não.

Faça commit e push no branch publicado pelo GitHub Pages (normalmente `main`, diretório `/`).

### 4. Atualizar o coletor e o agendamento

Substitua no repositório:

- `worker/ingest.py`
- `worker/requirements.txt`
- `.github/workflows/daily-ingest.yml`

Mantenha os secrets e variables existentes do GitHub Actions. O workflow usa `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `GEMINI_API_KEY` e a variável `GEMINI_MODEL`.

O agendamento fica assim:

- **09h05:** descobre e processa a edição regular mais recente da Parte II.
- **22h00:** consulta a data corrente de Brasília e procura uma eventual edição extra da Parte II. Se não houver link de edição extra, registra a checagem como concluída sem criar uma edição falsa.

Ambas as rotinas utilizam o fuso `America/Sao_Paulo`. Não crie um segundo workflow separado: substitua o `daily-ingest.yml` existente.

O gatilho manual do mesmo workflow permite escolher `regular`, `extra` ou `both`. Use `both` para um teste manual completo no dia da implantação.

### 5. Conferir o primeiro teste

Depois do commit:

1. Abra **Actions** no GitHub.
2. Execute manualmente `Radar ALERJ — coleta e verificação de edições`, escolhendo `both`.
3. Confira se o job identifica separadamente a Parte II regular e se a checagem extra encontra o link correto ou registra que não havia edição extra naquele dia.
4. No Supabase, confira as linhas em `editions`, `ingestion_runs` e `user_edition_analyses`.
5. Abra o site com cada conta autorizada. Os temas e destaques devem ser individuais; na primeira abertura da edição regular, a análise pessoal é calculada, enquanto as edições extras são pré-analisadas na rotina noturna.

Na página inicial, a seção **Acompanhamento das edições** mostra dois grupos — `EDIÇÕES REGULARES` e `EDIÇÕES EXTRAS` — e dois indicadores, um para cada rotina. Quando houver edições extras, cada PDF terá registro próprio — inclusive duas ou mais extras na mesma data — com a marca `EDIÇÃO EXTRA` e uma numeração visual quando houver múltiplas.

## Como a personalização funciona

- Cada usuário consulta e altera somente as linhas de `themes` cujo `user_id` corresponde à sua sessão.
- A segmentação dos atos é comum, porque o texto do Diário Oficial é a fonte pública compartilhada.
- A pertinência temática, os nomes dos temas correspondentes, os resumos e as revisões manuais ficam em `user_publication_matches`, sempre vinculados ao usuário.
- O estado e a síntese personalizados de cada usuário/edição ficam em `user_edition_analyses`.
- Ao abrir uma edição pela primeira vez, o sistema chama o Gemini para classificá-la segundo os temas daquela conta. Reaberturas com os mesmos temas e o mesmo conteúdo reutilizam o resultado armazenado.
- Se os temas forem alterados, a edição que estiver aberta é recalculada. Para reclassificar outra edição histórica, basta selecioná-la na lista de edições. As edições extras novas são pré-analisadas para cada conta que tenha temas cadastrados na rotina das 22h.
- No acervo, uma publicação antiga sem análise pessoal aparece como `Ainda não analisada para seus temas`, em vez de ser tratada indevidamente como irrelevante.

A coleta das 22h localiza, extrai e segmenta automaticamente cada edição extra. Depois de salvar as publicações, o worker consulta os temas e executa a classificação separadamente para cada conta que tenha temas cadastrados (inclusive contas em que todos os temas estejam inativos, que recebem o estado informativo correspondente). As classificações, correspondências, resumos e sínteses são gravados com o `user_id` de cada conta; não há resultado temático global. Assim, os destaques das edições extras são calculados na própria rotina noturna, antes de o usuário abrir o site. O cache por conta/edição evita repetir a classificação quando o conteúdo e os temas não mudaram. Se a análise de uma conta falhar, a coleta documental da edição continua válida, a falha fica registrada naquela análise e a abertura da edição pelo usuário permite nova tentativa. Para as edições regulares e para edições históricas ainda não analisadas, a classificação particular é executada quando o usuário seleciona a edição no site. A coleta central segmenta o PDF uma única vez para todos; as chamadas Gemini de classificação são individuais e seu consumo cresce conforme o número de contas com temas e de publicações.

## Edições extras e o exemplo de 29/10/2026

O coletor não usa uma busca genérica por `PDF` para descobrir a edição. Ele abre a página oficial de seleção da data e procura o link textual da Parte II. Para a rotina noturna, exige também o rótulo `EDIÇÃO EXTRA` e recolhe todos os links correspondentes, não apenas o primeiro. Cada PDF é identificado por hash de conteúdo, extraído do visualizador PDF.js, validado como Parte II e conferido com a data impressa. Depois de segmentado e armazenado, o conteúdo é classificado separadamente para cada usuário que tenha temas cadastrados.

O exemplo de 29/10/2026 foi usado para testar que o seletor não confunda o link regular com o link `EDIÇÃO EXTRA`. A checagem de 22h é prospectiva: ela procura todas as edições extras da data corrente após a implantação; não importa automaticamente edições extras históricas de 29/10/2026.

## Armazenamento e segurança

- Os PDFs não são armazenados no Supabase, no GitHub ou no Drive. O arquivo é lido temporariamente pelo workflow.
- O Supabase guarda metadados da edição, link oficial, texto de cada publicação e as classificações necessárias ao Radar.
- `themes`, `user_edition_analyses` e `user_publication_matches` têm políticas RLS por usuário.
- As tabelas públicas de publicações/edições continuam compartilhando apenas o conteúdo documental, não as classificações ou revisões particulares.
- Mantenha o cadastro público desativado no Supabase Auth e crie as contas de acesso manualmente.
