# Especificação — Radar Legislativo ALERJ v2.0

## 1. Objetivo

Aplicação privada para acompanhar exclusivamente a Parte II — Poder Legislativo do Diário Oficial do Estado do Rio de Janeiro. A experiência principal é o site; a planilha deixa de ser dependência. O banco guarda o acervo textual e seus metadados, mas **não guarda cópias de PDFs**.

## 2. Requisitos confirmados

- Abrangência: todas as publicações identificadas na Parte II.
- Resumos por IA: resumos individuais apenas para publicações relacionadas aos temas ativos.
- Acesso: privado, com autenticação e bloqueio de consultas anônimas no banco.
- Fonte: página de última edição da IOERJ → link Parte II (Poder Legislativo).
- PDFs: consulta/download direto da IOERJ; não são armazenados pelo Radar.
- Temas: cadastro editável no próprio site, com descrição, palavras-chave, critérios de inclusão/exclusão, prioridade e ativação.

## 3. Arquitetura

| Camada | Serviço | Responsabilidade |
|---|---|---|
| Interface | HTML/CSS/JS estático | Boletim diário, filtros, fichas, pesquisa, gestão de temas |
| Hospedagem | GitHub Pages | Entrega dos arquivos do site |
| Autenticação | Supabase Auth | Login de usuários previamente criados pelo administrador |
| Banco | Supabase PostgreSQL | Edições, atos, temas, resumos, revisões e logs |
| Coleta programada | GitHub Actions | Execução diária, com botão de execução manual |
| Extração | Python + PyMuPDF + Tesseract | Extrai texto por página; aplica OCR quando necessário |
| IA | Gemini API (`generateContent`) | Segmentação, classificação, resumos e respostas de pesquisa |
| Pesquisa | PostgreSQL FTS + Edge Function | Recupera atos e responde com fontes citadas |

### Fluxo de dados

1. Workflow agenda e inicia `worker/ingest.py`.
2. Coletor acessa o portal da IOERJ, segue o redirecionamento de última edição e localiza a Parte II.
3. PDF é baixado para memória no runner, não é escrito no Supabase Storage, Drive ou repositório.
4. PyMuPDF extrai texto por página; Tesseract OCR é acionado nas páginas sem texto útil.
5. Texto é dividido em lotes e enviado à IA para segmentação e classificação.
6. Todas as publicações são gravadas em `publications`; resumo individual/justificativa só ficam preenchidos quando `ai_relevant=true`.
7. Uma síntese geral da edição é gravada em `editions.daily_summary`.
8. PDF bytes deixam de existir quando a execução termina; o banco preserva apenas o link oficial e o hash do documento.
9. O site lê o banco após login e apresenta links oficiais para abrir/baixar o PDF.

## 4. Modelo de dados

### `editions`
Data da edição (chave única), rótulo/ano/número, URL da página da Parte II, URL do PDF oficial, hash, situação da coleta, timestamps, contagem de atos, contagem de relevantes, resumo geral e eventual erro.

### `publications`
Chave estável de deduplicação, edição, páginas, tipo, título, texto integral extraído, órgão, pessoas, municípios, valores, classificação automática, temas correspondentes, confiança, justificativa, resumo, fonte, revisão humana e observações.

### `themes`
Nome, ativo/inativo, descrição, palavras-chave, critérios de inclusão/exclusão e prioridade.

### `ingestion_runs`
Histórico de início/fim, situação, data da edição, mensagem e totais processados.

## 5. Experiência do usuário

### Leitura diária
Masthead editorial; navegação horizontal; pesquisa global; temas monitorados com contadores; seletor das últimas edições; data e link oficial; síntese geral da edição; indicadores; destaques individuais em cartões contendo resumo, tipo, páginas, órgão, autoria, temas e “por que importa”; demais atos agrupados por tipo.

### Publicações
Consulta de todas as publicações, busca full-text em português, filtros de relevância e edição, ficha individual e link oficial.

### Temas monitorados
CRUD de temas; critérios semânticos e palavras-chave; prioridade; ativação/desativação.

### Pesquisa no acervo
Pergunta em linguagem natural, recuperação textual no PostgreSQL, resposta da IA baseada exclusivamente nas publicações recuperadas, links e páginas das fontes.

### Edições
Histórico de edições, totais, estado de processamento, mensagem de falha e link oficial.

### Revisão humana
Usuário autenticado pode marcar ato como relevante/irrelevante e registrar nota. A revisão prevalece na exibição sobre o julgamento automático.

## 6. Segurança e privacidade

- Frontend não contém segredo administrativo nem chave da API Gemini.
- Usuários são criados manualmente; cadastro público deve permanecer desabilitado.
- RLS bloqueia qualquer leitura anônima e permite leitura a usuários autenticados.
- Apenas o backend de coleta possui a service role key.
- A Edge Function de pesquisa exige JWT.
- Repositório contém `config.example.js` e `config.js`; este último contém somente o Project URL e a chave publicável do Supabase, que pode ser exposta no frontend quando as políticas RLS estão corretamente configuradas. Nenhuma chave administrativa ou da API Gemini pode ser incluída no repositório.
- GitHub Pages torna os arquivos estáticos e a tela de login públicos. O conteúdo do banco continua protegido por Auth/RLS. Para ocultar também os arquivos estáticos, acrescentar uma camada de controle de acesso na hospedagem.

## 7. Retenção

- Sem retenção de PDF ou arquivo de imagem do PDF.
- Retenção de texto de cada ato, necessário para pesquisa e consulta histórica.
- Armazenamento de URL oficial e hash do PDF para rastreabilidade.
- Histórico de edições e execuções mantido no Supabase.

## 8. Rotina

Workflow programado diariamente às 09h15 no fuso `America/Sao_Paulo`. O horário é aproximado porque o GitHub pode atrasar jobs de cron. Execução manual disponível por `workflow_dispatch`.

## 9. Limitações v2.0

- A primeira coleta manual deve confirmar o caminho de navegação e o PDF atual da IOERJ.
- Mudanças no HTML/visualizador da IOERJ podem demandar ajuste do coletor.
- OCR depende da qualidade do PDF.
- Segmentação/identificação de páginas pela IA precisa de amostragem e revisão humana.
- A troca de critérios de tema não reprocessa automaticamente o acervo histórico nesta entrega; os novos critérios serão usados nas coletas seguintes.
- A busca é full-text lexical em português, não busca vetorial.
- O número de atos e o resumo não substituem o documento original.


## 10. Configuração do Gemini

A coleta (GitHub Actions) e a pesquisa (Supabase Edge Function `ask-radar`) utilizam a mesma chave Gemini, cadastrada separadamente em cada ambiente como `GEMINI_API_KEY`. O modelo é configurável por `GEMINI_MODEL`; padrão: `gemini-3.8-flash`. A migração do provedor não altera o schema PostgreSQL.

A modalidade gratuita do Gemini está sujeita a limites de taxa e pode utilizar conteúdo enviado para aprimoramento dos produtos do Google. O projeto deve evitar o envio de dados pessoais ou conteúdo interno sigiloso.
