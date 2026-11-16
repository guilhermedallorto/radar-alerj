# Especificação — Radar Legislativo ALERJ

## 1. Objetivo

Aplicação privada para acompanhar todas as publicações da **Parte II — Poder Legislativo** do Diário Oficial do Estado do Rio de Janeiro. O site é a interface principal. O banco guarda metadados e texto extraído, mas **não guarda cópias dos PDFs**.

## 2. Requisitos funcionais

- Abrangência: todas as publicações da Parte II, tanto da edição regular quanto de eventual edição extra.
- Coleta regular: todos os dias às 9h05 (`America/Sao_Paulo`).
- Checagem de edição extra: todos os dias às 22h (`America/Sao_Paulo`), restrita à data corrente.
- Temas: cada usuário autenticado mantém temas e critérios próprios; nenhuma classificação temática ou revisão humana é compartilhada entre contas.
- Análise: a segmentação documental é feita uma vez por edição. As edições extras novas são classificadas na rotina noturna, separadamente para cada conta com temas cadastrados; edições regulares e históricas não analisadas são classificadas quando o usuário as abre.
- Acesso: privado, com autenticação e políticas de Row Level Security.
- Fonte: calendário/página de seleção de data da IOERJ → link Parte II (Poder Legislativo), distinguindo o rótulo `EDIÇÃO EXTRA`.
- PDFs: extraídos diretamente do visualizador e mantidos apenas em memória/temporariamente no runner; nunca enviados ao Storage do Radar.

## 3. Arquitetura

| Camada | Serviço | Responsabilidade |
|---|---|---|
| Interface | HTML/CSS/JS estático | Leitura, busca, fichas, gestão de temas, acompanhamento de edições |
| Hospedagem | GitHub Pages | Entrega dos arquivos estáticos |
| Autenticação | Supabase Auth | Login de contas previamente criadas pelo administrador |
| Banco | Supabase PostgreSQL | Edições, publicações, temas por usuário, análises particulares e logs |
| Coleta | GitHub Actions | Execução às 9h05 e 22h, além de execução manual |
| Extração | Python, Playwright/Chromium, PyMuPDF e Tesseract | Abertura do PDF.js, extração por página e OCR quando necessário |
| IA | Gemini API | Segmentação comum, classificação personalizada e pesquisa |
| Funções | Supabase Edge Functions | Análise temática autenticada por usuário e pesquisa contextual |

### Fluxo de dados

1. O workflow da manhã consulta o calendário e seleciona a Parte II regular mais recente.
2. O workflow das 22h consulta o dia corrente e recolhe todos os links da Parte II identificados como `EDIÇÃO EXTRA`, podendo processar vários cadernos na mesma data.
3. O worker abre o visualizador PDF.js, recupera os bytes do documento, valida a assinatura, a identificação da Parte II e a data impressa.
4. PyMuPDF extrai texto por página; Tesseract OCR é aplicado quando necessário.
5. O Gemini segmenta o texto em publicações individuais. Nesta etapa, não há classificação por temas de gabinete.
6. O acervo de publicações é gravado no Supabase, com chave própria para cada caderno, permitindo várias extras na mesma data.
7. Para edições extras, após a segmentação o worker classifica o documento separadamente para cada conta com temas cadastrados e grava o resultado sob o respectivo `user_id`. Para edições regulares e históricas ainda não analisadas, `analyze-user-themes` executa a classificação quando o usuário abre a edição, usando somente os temas permitidos pela sessão.
8. A análise individual é armazenada e reutilizada enquanto o conjunto de temas e o conteúdo da edição permanecerem iguais.
9. `ask-radar` pesquisa o acervo e utiliza as correspondências temáticas do usuário autenticado, quando existentes.

## 4. Modelo de dados

### `editions`
Metadados da edição, incluindo `edition_date` e `edition_type` (`regular` ou `extra`), rótulo, URLs oficiais, hash do PDF, estado e contagem de publicações. A chave única é a combinação de data e `edition_key`: a edição regular usa `regular`, e cada edição extra recebe uma chave própria derivada do PDF. Assim, duas ou mais extras podem coexistir na mesma data.

### `publications`
Publicações identificadas, vinculadas a uma edição, com páginas, tipo, título, texto integral, órgão, pessoas, municípios, valores e fonte. Os campos históricos de relevância global permanecem por compatibilidade, mas não são utilizados para produzir destaques personalizados.

### `themes`
Temas, descrição, palavras-chave, critérios e prioridade, vinculados obrigatoriamente a `user_id`. A RLS permite ao usuário acessar apenas as próprias linhas.

### `user_edition_analyses`
Estado e síntese da análise por combinação usuário/edição, incluindo assinatura dos temas e da versão do conteúdo analisado.

### `user_publication_matches`
Classificação por usuário/publicação, temas correspondentes, resumo, justificativa e eventual revisão humana particular.

### `ingestion_runs`
Histórico operacional, incluindo data, tipo de edição verificada (regular/extra), chave opcional do caderno, estado e mensagem. A verificação das 22h grava também um resumo consolidado do resultado da busca extra.

## 5. Experiência do usuário

- **Leitura diária:** mostra as edições regulares e extras em grupos separados, com dois indicadores de execução: coleta regular e busca de edição extra.
- **Destaques:** cada conta recebe classificação, temas correspondentes, resumo e justificativa próprios.
- **Publicações:** o acervo integral permanece consultável. Edições regulares/históricas que o usuário ainda não abriu aparecem como `Ainda não analisada para seus temas`, sem serem tratadas como irrelevantes. Edições extras são pré-analisadas para as contas com temas cadastrados na checagem das 22h.
- **Temas monitorados:** CRUD de temas por conta, com descrição, critérios, prioridade e ativação/desativação.
- **Pesquisa:** resposta fundamentada no acervo, com indicação das fontes, data, página e eventual marca de edição extra.
- **Histórico:** edição extra aparece em grupo próprio e recebe identificação explícita.
- **Revisão humana:** a marcação de relevante/irrelevante e a observação pertencem apenas ao usuário que as registrou.

## 6. Segurança e privacidade

- Nenhuma chave administrativa ou chave Gemini é incluída no frontend.
- Usuários são criados manualmente; o cadastro público deve permanecer desabilitado.
- RLS restringe `themes`, `user_edition_analyses` e `user_publication_matches` ao `user_id` autenticado.
- Classificações e revisões particulares não são gravadas na linha compartilhada de `publications`.
- O worker utiliza a chave de serviço apenas no ambiente GitHub Actions.
- Edge Functions de análise e pesquisa exigem JWT válido.
- O `config.js` contém apenas URL e chave publicável do Supabase.

## 7. Personalização e consumo da API

A migração transforma os temas existentes em cópias individuais para cada conta já cadastrada. A partir daí, alterações em uma conta não afetam outra. Novas contas começam sem temas configurados.

Para edições regulares e históricas, a análise temática é feita quando o usuário abre uma edição, evitando chamadas Gemini para usuários que não consultam determinado caderno. Para edições extras novas, a rotina das 22h percorre as contas com temas cadastrados e calcula a classificação individualmente antes de gravá-la sob o respectivo `user_id`. Se uma análise individual falhar, a edição documental continua processada, a análise dessa conta fica em erro e pode ser refeita quando o usuário abrir a edição. Alterações em temas recalculam a edição atualmente aberta; edições anteriores são recalculadas quando forem selecionadas. O cache reduz chamadas repetidas, mas o consumo da API depende da quantidade de usuários, temas e publicações.

## 8. Limitações

- A checagem extra é prospectiva e consulta a data corrente; não importa automaticamente edições extras históricas.
- Se o portal IOERJ alterar HTML, links ou o visualizador, o coletor poderá exigir ajuste. Ele interrompe a execução quando não consegue validar o documento, em vez de processar outro caderno silenciosamente.
- OCR, segmentação documental e classificação temática por IA devem ser conferidos em publicações importantes no documento oficial.
- A pesquisa é baseada em busca textual do PostgreSQL e síntese com IA; não é uma busca vetorial semântica.
- O número de atos e os resumos não substituem o Diário Oficial original.
