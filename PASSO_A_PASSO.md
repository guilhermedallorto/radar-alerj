# Passo a passo — atualização de temas por usuário e edições extras

Este roteiro é destinado à instalação existente do Radar. **Faça backup do Supabase antes de começar.**

## 1. Supabase: migrar o banco

1. Abra **SQL Editor** no Supabase.
2. Abra `supabase/migrations/20261116_personal_themes_and_extra_editions.sql`.
3. Copie o SQL completo e execute uma única vez.
4. Confirme que terminou sem erro.

Não execute `supabase/schema.sql` em uma base que já contém dados. A migração copia os temas antigos para cada usuário já cadastrado, transforma o armazenamento de temas em individual, cria as tabelas de classificação pessoal e habilita RLS.

Se o banco não tiver usuários cadastrados, não tente contornar o erro da migração removendo os dados: confirme primeiro as contas em **Authentication → Users**.

## 2. Supabase: publicar funções

No terminal, dentro da pasta raiz do projeto:

```bash
supabase login
supabase link --project-ref SEU_PROJECT_REF
supabase functions deploy analyze-user-themes
supabase functions deploy ask-radar
```

Confira em **Edge Functions → Secrets** que `GEMINI_API_KEY` e `GEMINI_MODEL` já estão configurados. Não copie chaves secretas para arquivos do frontend ou para o GitHub Pages.

## 3. GitHub: atualizar os arquivos

No repositório, substitua os arquivos existentes pelos correspondentes deste pacote:

**Raiz do site:**
- `index.html`
- `app.js`
- `styles.css`

**Coletor:**
- `worker/ingest.py`
- `worker/requirements.txt`

**Workflow:**
- `.github/workflows/daily-ingest.yml`

**Supabase:**
- `supabase/config.toml`
- `supabase/functions/analyze-user-themes/index.ts`
- `supabase/functions/ask-radar/index.ts`

Mantenha o seu `config.js` atual se os dados publicáveis tiverem mudado desde o ZIP enviado. Não inclua secrets no commit. Faça commit e push no branch utilizado pelo GitHub Pages e pelo agendamento do Actions (normalmente `main`).

## 4. Confirmar o workflow

O workflow contém duas rotinas no mesmo arquivo:

| Horário de Brasília | Ação |
|---|---|
| 09h05 | Coleta da Parte II regular mais recente |
| 22h00 | Busca da Parte II identificada como EDIÇÃO EXTRA para a data corrente |

Em **Actions → Radar ALERJ — coleta e verificação de edições → Run workflow**, é possível escolher `regular`, `extra` ou `both`. Para o primeiro teste após a atualização, escolha `both`.

## 5. Conferir as duas contas

1. Entre no Radar com a primeira conta e confira os temas existentes.
2. Adicione ou altere um tema e confirme que ele fica salvo.
3. Saia e entre com outra conta.
4. Confirme que a segunda conta só enxerga os próprios temas e que pode alterá-los sem modificar a primeira conta.
5. Abra uma edição regular na segunda conta. A análise pessoal será calculada na primeira abertura. Para uma edição extra nova, confira também se a análise noturna foi gravada separadamente para cada conta.
6. Volte à primeira conta e confirme que seus destaques permanecem independentes.

## 6. Conferir a edição extra

Na página inicial, em **Acompanhamento das edições**, existem dois grupos: `EDIÇÕES REGULARES` e `EDIÇÕES EXTRAS`. Também aparecem dois indicadores de verificação, que mostram o último resultado da rotina regular e da busca adicional.

Se o portal não disponibilizar edição extra, a checagem das 22h deve aparecer como concluída, com a mensagem de que nenhuma edição extra foi localizada. Se houver uma ou mais, cada caderno aparece como linha própria da mesma data no grupo `EDIÇÕES EXTRAS`, marcada como `EDIÇÃO EXTRA` e numerada quando houver mais de uma.

A busca noturna começa a valer após a implantação. O dia 29/10/2026 foi utilizado para verificar a existência dos dois links distintos no portal, mas não é importado automaticamente pelo job noturno.

## 7. Como a análise personalizada é recalculada

- Ao abrir uma edição pela primeira vez, o usuário recebe uma análise baseada nos seus temas ativos.
- Ao salvar, editar, ativar ou desativar um tema, a edição que estiver aberta é recalculada.
- Para recalcular outra edição antiga, basta selecioná-la no grupo de edições.
- Para publicações de uma edição regular/histórica ainda não aberta pelo usuário, o acervo exibe `Ainda não analisada para seus temas`, sem tratá-las como irrelevantes. As edições extras novas são pré-analisadas na rotina das 22h para as contas com temas cadastrados.
- O cache evita chamadas Gemini quando os temas e o conteúdo da edição permanecem iguais.

A busca noturna extrai e segmenta a(s) edição(ões) extra(s) automaticamente e as disponibiliza no acervo. Em seguida, o worker calcula os destaques separadamente para cada conta que tenha temas cadastrados e grava os resultados com o respectivo `user_id`. Se uma análise de usuário falhar, a coleta do caderno permanece válida; a falha aparece na verificação e pode ser tentada novamente quando a conta abrir a edição. A análise de edições regulares e históricas não analisadas continua ocorrendo quando o usuário as seleciona. A análise personalizada pode aumentar o uso da API Gemini conforme o número de contas, temas e publicações. Não existe classificação temática única compartilhada entre os usuários.
