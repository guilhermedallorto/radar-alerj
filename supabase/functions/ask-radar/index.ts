import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  if (req.method !== "POST") return json({ error: "Método não permitido." }, 405);

  const authHeader = req.headers.get("Authorization");
  if (!authHeader) return json({ error: "Autenticação obrigatória." }, 401);

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  let publishableKey = Deno.env.get("SUPABASE_PUBLISHABLE_KEY") || Deno.env.get("SUPABASE_ANON_KEY");
  // Projetos Supabase mais novos disponibilizam as chaves publicáveis em um mapa JSON.
  if (!publishableKey) {
    const publishableKeysJson = Deno.env.get("SUPABASE_PUBLISHABLE_KEYS");
    if (publishableKeysJson) {
      try {
        const publishableKeys = JSON.parse(publishableKeysJson);
        publishableKey = publishableKeys.default;
      } catch {
        return json({ error: "Não foi possível ler a chave publicável do Supabase no ambiente da função." }, 503);
      }
    }
  }
  if (!supabaseUrl || !publishableKey) {
    return json({ error: "A configuração de conexão/autenticação do Supabase está incompleta na Edge Function." }, 503);
  }
  const supabase = createClient(supabaseUrl, publishableKey, {
    global: { headers: { Authorization: authHeader } },
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const { data: userData, error: userError } = await supabase.auth.getUser();
  if (userError || !userData.user) return json({ error: "Sessão inválida ou expirada." }, 401);

  let body: { question?: string };
  try { body = await req.json(); } catch { return json({ error: "Corpo da requisição inválido." }, 400); }
  const question = String(body.question || "").trim();
  if (question.length < 3 || question.length > 1500) return json({ error: "A pergunta deve ter entre 3 e 1.500 caracteres." }, 400);

  // Remove termos funcionais e combina os termos substantivos com OR: uma pergunta
  // em linguagem natural não deve exigir que todas as palavras apareçam no mesmo ato.
  const stopwords = new Set(["sobre", "quais", "qual", "como", "quando", "onde", "houve", "foram", "publicado", "publicada", "publicacoes", "publicacao", "atos", "diario", "oficial", "ultimas", "ultimos", "edicoes", "edicao", "dias", "meses", "anos", "para", "pelo", "pela", "entre", "mais", "menos", "esta", "esse", "essa", "isso", "com", "sem", "dos", "das", "que", "uma", "uns", "umas", "nas", "nos", "de", "da", "do", "em", "e", "ou", "a", "o"]);
  const searchTerms = question.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().match(/[a-z0-9./-]+/g) || [];
  const searchExpression = [...new Set(searchTerms.filter((w) => w.length > 1 && !stopwords.has(w)))].slice(0, 8).join(" OR ") || question;
  const { data: sources, error: searchError } = await supabase.rpc("search_publications", {
    search_query: searchExpression,
    result_limit: 18,
  });
  if (searchError) return json({ error: `Falha na pesquisa do acervo: ${searchError.message}` }, 500);
  if (!sources?.length) {
    return json({ answer: "Não encontrei publicações que correspondam aos termos da pergunta no acervo processado. Experimente indicar um município, número de projeto, órgão, autor ou expressão mais específica.", sources: [] });
  }

  const compactSources = sources.map((s: any, i: number) => ({
    ref: i + 1,
    date: s.edition_id,
    title: s.title,
    kind: s.kind,
    page_start: s.page_start,
    page_end: s.page_end,
    summary: s.summary,
    themes: s.matched_themes,
    excerpt: String(s.body_text || "").slice(0, 1700),
    source_url: s.source_url,
  }));

  const geminiKey = Deno.env.get("GEMINI_API_KEY");
  if (!geminiKey) return json({ error: "A chave da API Gemini ainda não foi configurada no Supabase." }, 503);
  const geminiModel = Deno.env.get("GEMINI_MODEL") || "gemini-3.8-flash";

  // Recupera datas das edições pelo identificador, sempre dentro do conjunto visível ao usuário.
  const editionIds = [...new Set(sources.map((s: any) => s.edition_id))];
  const { data: editions } = await supabase.from("editions").select("id,edition_date,edition_label").in("id", editionIds);
  const dates = new Map<string, any>((editions || []).map((e: any) => [e.id, e]));
  const groundedSources = compactSources.map((s: any) => {
    const edition = dates.get(s.date);
    return { ...s, edition_date: edition?.edition_date || "data não identificada", edition_label: edition?.edition_label || "" };
  });

  const prompt = `Você é o assistente de pesquisa documental do Radar Legislativo ALERJ. Responda à pergunta usando EXCLUSIVAMENTE as publicações fornecidas abaixo. Não use conhecimento externo para completar lacunas. Se as fontes não permitirem responder, diga isso claramente. Não invente datas, números, autores, resultados, relações ou conclusões. Distinga publicação de projeto apresentado, parecer, votação, sanção e promulgação. Sempre cite as fontes no corpo como [1], [2] etc., correspondendo exatamente ao campo ref. Ao final, não crie bibliografia separada; a interface mostrará as fontes abaixo.

PERGUNTA: ${question}

PUBLICAÇÕES ENCONTRADAS:
${JSON.stringify(groundedSources)}`;

  const aiResponse = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(geminiModel)}:generateContent`,
    {
      method: "POST",
      headers: { "x-goog-api-key": geminiKey, "Content-Type": "application/json" },
      body: JSON.stringify({
        contents: [{ role: "user", parts: [{ text: prompt }] }],
        generationConfig: { maxOutputTokens: 2200 },
      }),
    },
  );
  if (!aiResponse.ok) {
    const detail = (await aiResponse.text()).slice(0, 700);
    return json({ error: `A API Gemini retornou HTTP ${aiResponse.status}: ${detail}` }, 502);
  }
  const aiData = await aiResponse.json();
  const answer = (aiData.candidates || [])
    .flatMap((candidate: any) => candidate.content?.parts || [])
    .filter((part: any) => typeof part.text === "string" && !part.thought)
    .map((part: any) => part.text)
    .join("\n")
    .trim();
  if (!answer) {
    return json({ error: "O Gemini não retornou uma resposta textual utilizável." }, 502);
  }
  return json({ answer, sources: groundedSources.map((s: any) => ({ ref: s.ref, title: s.title, kind: s.kind, editionDate: s.edition_date, pageStart: s.page_start, pageEnd: s.page_end, sourceUrl: s.source_url, themes: s.themes })) });
});

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { ...corsHeaders, "Content-Type": "application/json; charset=utf-8" } });
}
