> **Nota histórica:** documento referente à correção anterior do coletor. Para a arquitetura atual, a implantação e a migração de banco, siga `README.md`, `PASSO_A_PASSO.md` e `IMPLEMENTACAO_TEMAS_PERSONALIZADOS_E_EDICOES_EXTRAS.md`.

# Correção da coleta da Parte II — 13/11/2026

## O que foi corrigido

1. **Descoberta diária da edição:** o coletor continua entrando pelo calendário da IOERJ, identifica a edição mais recente disponível e seleciona o link cujo rótulo indica expressamente **Parte II (Poder Legislativo)**. Se a página da edição não tiver esse link, ou se a data da página divergir da data do calendário, a coleta é interrompida.
2. **Extração pelo visualizador:** para links `mostra_edicao.php`, o coletor abre o visualizador com Chromium/Playwright e recupera os bytes do documento carregado em `PDFViewerApplication`, usando `pdf.getData()`. Não tenta mais encontrar um PDF por links HTML genéricos nem aceita o texto da página HTML como substituto.
3. **Validação antes do Gemini:** confere a assinatura `%PDF`, abre o arquivo, verifica na primeira página os marcadores `PARTE II`, `PODER LEGISLATIVO` e `ASSEMBLEIA LEGISLATIVA`, e compara a data impressa com a data da edição selecionada. Também compara a contagem de páginas informada pelo PDF.js com a contagem do PyMuPDF.
4. **Texto na ordem das colunas:** as páginas são extraídas em três colunas, da esquerda para a direita, preservando a leitura vertical dentro de cada coluna.
5. **JSON do Gemini:** a classificação aceita tanto o objeto `{"items": [...]}` quanto a lista de publicações diretamente na raiz (`[...]`). A síntese diária continua esperando um objeto com `summary` e usa uma mensagem de contingência se o modelo não produzir esse campo.
6. **Proteção contra perda de dados:** o coletor não apaga publicações existentes antes da análise. Primeiro conclui a extração, a classificação e a gravação dos novos itens; só então remove registros antigos que não constem na versão validada da edição.

## Arquivos alterados

- `worker/ingest.py`
- `worker/requirements.txt` — inclui Playwright.
- `.github/workflows/daily-ingest.yml` — instala Chromium e suas dependências; limite do job elevado para 60 minutos.

Não há alteração no esquema do Supabase, nas funções Edge, nas credenciais ou na configuração do Gemini.

## Conferência com o PDF enviado

O arquivo fornecido foi aberto e validado localmente: 15 páginas; data impressa em 12/11/2026; metadados do autor indicando a Imprensa Oficial do Estado do Rio de Janeiro; primeira página identificada como Parte II — Poder Legislativo. A extração por colunas produziu 24 lotes para essa edição.

Também foram testados localmente: seleção do link da Parte II em uma página que contém também a Parte I; decodificação da data Base64 do calendário; aceitação do PDF enviado; rejeição de HTML/falso PDF e de PDF identificado como Parte I; aceitação de resposta JSON do Gemini tanto como lista na raiz quanto como objeto com `items`.

**Limite do teste:** o ambiente desta correção não conseguiu resolver o endereço externo da IOERJ por DNS. Portanto, a navegação real do Chromium no portal e uma execução com as credenciais do GitHub/Supabase/Gemini ainda precisam ser confirmadas na primeira execução do GitHub Actions. O fluxo está preparado para falhar de forma explícita se o visualizador não disponibilizar o documento ou se a validação do caderno não passar.

## Aplicação no repositório

Substitua no repositório do GitHub os três arquivos listados acima pelos arquivos desta versão e faça commit/push. Depois, execute manualmente o workflow **Radar ALERJ — coleta diária** em *Actions* → *Run workflow*.

Na execução, confira especialmente as mensagens:

- `Link identificado como Parte II — Poder Legislativo`;
- `PDF extraído do visualizador`;
- `Validação do PDF aprovada: Parte II — Poder Legislativo`;
- `Analisando lote ...`.

Para o PDF de 12/11/2026 enviado nesta conversa, o resultado local foi de 15 páginas e 24 lotes. Edições futuras podem naturalmente ter quantidade diferente de páginas e lotes.
