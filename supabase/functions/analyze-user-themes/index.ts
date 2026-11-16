import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json; charset=utf-8" },
  });
}

function cleanText(value: unknown, max = 4000): string {
  return String(value ?? "").replace(/\s+/g, " ").trim().slice(0, max);
}

async function sha256(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function callGemini(prompt: string, maxOutputTokens = 6000, jsonMode = true): Promise<string> {
  const apiKey = Deno.env.get("GEMINI_API_KEY");
  if (!apiKey) throw new Error("A chave GEMINI_API_KEY não está configurada nas Edge Functions do Supabase.");
  const model = Deno.env.get("GEMINI_MODEL") || "gemini-3.1-flash-lite";
  const endpoint = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`;
  const generationConfig: Record<string, unknown> = { maxOutputTokens };
  if (jsonMode) generationConfig.responseMimeType = "application/json";

  for (let attempt = 1; attempt <= 5; attempt++) {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "x-goog-api-key": apiKey, "Content-Type": "application/json" },
      body: JSON.stringify({
        contents: [{ role: "user", parts: [{ text: prompt }] }],
        generationConfig,
      }),
    });

    if (response.ok) {
      const payload = await response.json();
      const content = (payload.candidates || [])
        .flatMap((candidate: any) => candidate.content?.parts || [])
        .filter((part: any) => typeof part.text === "string" && !part.thought)
        .map((part: any) => part.text)
        .join("\n")
        .trim();
      if (!content) throw new Error("O Gemini não retornou texto utilizável para a análise temática.");
      return content;
    }

    const detail = (await response.text()).slice(0, 600);
    if (![429, 500, 502, 503, 504].includes(response.status) || attempt === 5) {
      throw new Error(`Gemini HTTP ${response.status}: ${detail}`);
    }
    const retryAfter = Number(response.headers.get("Retry-After"));
    const delay = Number.isFinite(retryAfter) && retryAfter > 0
      ? Math.min(retryAfter * 1000, 15000)
      : Math.min(1500 * (2 ** (attempt - 1)), 12000);
    await sleep(delay);
  }
  throw new Error("O Gemini não respondeu após as tentativas de repetição.");
}

function parseJsonResponse(text: string): any {
  const cleaned = text.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
  const parsed = JSON.parse(cleaned);
  if (Array.isArray(parsed)) return parsed;
  if (parsed && typeof parsed === "object" && Array.isArray(parsed.items)) return parsed.items;
  throw new Error("O Gemini não retornou uma lista JSON de classificações.");
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  if (req.method !== "POST") return json({ error: "Método não permitido." }, 405);

  const authorization = req.headers.get("Authorization");
  if (!authorization) return json({ error: "Autenticação obrigatória." }, 401);

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  let publishableKey = Deno.env.get("SUPABASE_PUBLISHABLE_KEY") || Deno.env.get("SUPABASE_ANON_KEY");
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
    return json({ error: "A configuração de conexão/autenticação da Edge Function está incompleta." }, 503);
  }

  const supabase = createClient(supabaseUrl, publishableKey, {
    global: { headers: { Authorization: authorization } },
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const { data: authData, error: authError } = await supabase.auth.getUser();
  if (authError || !authData.user) return json({ error: "Sessão inválida ou expirada." }, 401);
  const userId = authData.user.id;

  let body: { edition_id?: string };
  try { body = await req.json(); } catch { return json({ error: "Corpo da requisição inválido." }, 400); }
  const editionId = String(body.edition_id || "").trim();
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(editionId)) {
    return json({ error: "Informe um identificador válido de edição." }, 400);
  }

  let analysisStarted = false;
  try {
    const { data: edition, error: editionError } = await supabase
      .from("editions")
      .select("id,edition_date,edition_type,edition_label,daily_summary")
      .eq("id", editionId)
      .maybeSingle();
    if (editionError) throw editionError;
    if (!edition) return json({ error: "Edição não encontrada ou sem acesso." }, 404);

    const { data: themes, error: themesError } = await supabase
      .from("themes")
      .select("id,name,description,keywords,include_criteria,exclude_criteria,priority,active,updated_at")
      .eq("active", true)
      .order("priority", { ascending: true })
      .order("name", { ascending: true })
      .order("id", { ascending: true });
    if (themesError) throw themesError;

    let publications: any[] = [];
    const pageSize = 200;
    for (let offset = 0; ; offset += pageSize) {
      const { data, error } = await supabase
        .from("publications")
        .select("id,edition_id,page_start,page_end,kind,title,body_text,organization,people,municipalities,amounts,updated_at")
        .eq("edition_id", editionId)
        .order("page_start", { ascending: true })
        .range(offset, offset + pageSize - 1);
      if (error) throw error;
      const page = data || [];
      publications = publications.concat(page);
      if (page.length < pageSize) break;
      if (publications.length > 3000) throw new Error("A edição excedeu o limite de 3.000 publicações para análise em uma única execução.");
    }

    // A assinatura considera os temas particulares e a versão do acervo da edição.
    // Assim, uma republicação corrigida é reanalisada mesmo que mantenha a mesma
    // quantidade de publicações.
    const signatureSource = JSON.stringify({
      themes: [...(themes || [])].sort((a: any, b: any) => String(a.id).localeCompare(String(b.id))).map((t: any) => ({
        id: t.id,
        name: t.name,
        description: t.description || "",
        keywords: t.keywords || [],
        include_criteria: t.include_criteria || "",
        exclude_criteria: t.exclude_criteria || "",
        priority: t.priority || 3,
        updated_at: t.updated_at,
      })),
      publications: [...publications].sort((a: any, b: any) => String(a.id).localeCompare(String(b.id))).map((p: any) => [p.id, p.updated_at || ""]),
    });
    const themeSignature = await sha256(signatureSource);

    const { data: priorAnalysis, error: priorAnalysisError } = await supabase
      .from("user_edition_analyses")
      .select("id,status,theme_signature,publication_count,relevant_count,summary")
      .eq("user_id", userId)
      .eq("edition_id", editionId)
      .maybeSingle();
    if (priorAnalysisError) throw priorAnalysisError;

    if (priorAnalysis?.status === "processed" && priorAnalysis.theme_signature === themeSignature && priorAnalysis.publication_count === publications.length) {
      return json({
        ok: true,
        cached: true,
        status: "processed",
        summary: priorAnalysis.summary || "",
        publication_count: publications.length,
        relevant_count: priorAnalysis.relevant_count || 0,
        has_active_themes: (themes || []).length > 0,
      });
    }

    const { error: analysisUpsertError } = await supabase
      .from("user_edition_analyses")
      .upsert({
        user_id: userId,
        edition_id: editionId,
        theme_signature: themeSignature,
        status: "processing",
        publication_count: publications.length,
        relevant_count: 0,
        error: null,
        started_at: new Date().toISOString(),
        finished_at: null,
      }, { onConflict: "user_id,edition_id" });
    if (analysisUpsertError) throw analysisUpsertError;
    analysisStarted = true;

    if (!(themes || []).length) {
      // Limpa apenas as classificações automáticas antigas, preservando eventuais
      // revisões manuais que pertencem a este usuário.
      const { error: clearAutomaticError } = await supabase
        .from("user_publication_matches")
        .update({
          theme_signature: themeSignature,
          relevant: false,
          matched_themes: [],
          confidence: null,
          justification: null,
          summary: null,
        })
        .eq("user_id", userId)
        .eq("edition_id", editionId)
        .is("manual_relevance", null);
      if (clearAutomaticError) throw clearAutomaticError;
      const summary = "Nenhum tema ativo está configurado para este usuário. Cadastre seus temas em ‘Temas monitorados’ para gerar destaques personalizados nesta edição.";
      const { error: finishError } = await supabase
        .from("user_edition_analyses")
        .update({ status: "processed", summary, publication_count: publications.length, relevant_count: 0, error: null, finished_at: new Date().toISOString() })
        .eq("user_id", userId)
        .eq("edition_id", editionId);
      if (finishError) throw finishError;
      return json({ ok: true, cached: false, status: "processed", summary, publication_count: publications.length, relevant_count: 0, has_active_themes: false });
    }

    const { data: previousMatches, error: previousError } = await supabase
      .from("user_publication_matches")
      .select("publication_id,manual_relevance,manual_note,reviewed_at")
      .eq("user_id", userId)
      .eq("edition_id", editionId);
    if (previousError) throw previousError;
    const previousByPublication = new Map((previousMatches || []).map((m: any) => [m.publication_id, m]));

    const themesForPrompt = (themes || []).map((t: any) => ({
      id: t.id,
      name: t.name,
      description: t.description || "",
      keywords: t.keywords || [],
      include_criteria: t.include_criteria || "",
      exclude_criteria: t.exclude_criteria || "",
      priority: t.priority || 3,
    }));
    const classified = new Map<string, any>();
    const batchSize = 10;

    for (let offset = 0; offset < publications.length; offset += batchSize) {
      const batch = publications.slice(offset, offset + batchSize);
      const compact = batch.map((p: any) => ({
        publication_id: p.id,
        type: p.kind,
        title: p.title,
        organization: p.organization,
        page_start: p.page_start,
        page_end: p.page_end,
        text: cleanText(p.body_text, 2600),
      }));
      const prompt = `Você é um analista documental de um gabinete parlamentar. Classifique cada publicação exclusivamente em relação aos temas particulares deste usuário.

REGRAS OBRIGATÓRIAS:
- Avalie o conteúdo substantivo, não apenas coincidências de palavras.
- Considere palavras-chave como pistas; aplique os critérios de inclusão e exclusão.
- Uma publicação é relevante somente quando houver relação clara com pelo menos um tema ativo.
- Use os nomes exatos dos temas fornecidos em matched_themes.
- Não invente relações, fatos, pessoas, valores, municípios ou consequências.
- Para cada publicação, devolva exatamente uma classificação e preserve o publication_id sem alteração.
- Para itens irrelevantes, use matched_themes=[], justification="" e summary="".
- Para itens relevantes, dê uma justificativa específica e um resumo factual de 2 a 4 frases.
- Não omita nenhuma publicação do lote.

TEMAS PARTICULARES DO USUÁRIO:
${JSON.stringify(themesForPrompt)}

PUBLICAÇÕES DO LOTE:
${JSON.stringify(compact)}

Responda somente JSON no formato {"items":[{"publication_id":"UUID","relevant":true,"matched_themes":["Tema exato"],"confidence":0.0,"justification":"...","summary":"..."}]}.`;

      const raw = await callGemini(prompt, 6000, true);
      const items = parseJsonResponse(raw);
      const batchMap = new Map<string, any>();
      for (const item of items) {
        const id = String(item.publication_id || "");
        if (!id || !batch.some((p: any) => p.id === id) || batchMap.has(id)) {
          throw new Error("O Gemini retornou um identificador ausente, repetido ou estranho ao lote atual.");
        }
        batchMap.set(id, item);
      }
      for (const publication of batch) {
        const item = batchMap.get(publication.id);
        if (!item) throw new Error(`O Gemini não classificou a publicação ${publication.id}. A análise foi interrompida para evitar resultados incompletos.`);
        const allowedNames = new Set(themesForPrompt.map((t: any) => t.name));
        const matchedThemes = Array.isArray(item.matched_themes)
          ? [...new Set(item.matched_themes.map((name: unknown) => String(name)).filter((name: string) => allowedNames.has(name)))]
          : [];
        const relevant = Boolean(item.relevant) && matchedThemes.length > 0;
        classified.set(publication.id, {
          user_id: userId,
          edition_id: editionId,
          publication_id: publication.id,
          theme_signature: themeSignature,
          relevant,
          matched_themes: relevant ? matchedThemes : [],
          confidence: Number.isFinite(Number(item.confidence)) ? Math.max(0, Math.min(1, Number(item.confidence))) : null,
          justification: relevant ? cleanText(item.justification, 1000) : null,
          summary: relevant ? cleanText(item.summary, 1800) : null,
          manual_relevance: previousByPublication.get(publication.id)?.manual_relevance ?? null,
          manual_note: previousByPublication.get(publication.id)?.manual_note ?? null,
          reviewed_at: previousByPublication.get(publication.id)?.reviewed_at ?? null,
        });
      }
      if (offset + batchSize < publications.length) await sleep(1200);
      console.log(`Análise temática do usuário: lote ${Math.floor(offset / batchSize) + 1}/${Math.ceil(publications.length / batchSize)} concluído.`);
    }

    const matchRows = [...classified.values()];
    // Um único upsert mantém a classificação do usuário separada da classificação
    // global (que deixou de existir na camada de coleta compartilhada).
    if (matchRows.length) {
      const { error: matchUpsertError } = await supabase
        .from("user_publication_matches")
        .upsert(matchRows, { onConflict: "user_id,publication_id" });
      if (matchUpsertError) throw matchUpsertError;
    } else {
      const { error: clearError } = await supabase
        .from("user_publication_matches")
        .delete()
        .eq("user_id", userId)
        .eq("edition_id", editionId);
      if (clearError) throw clearError;
    }

    const relevantRows = matchRows.filter((m: any) => m.relevant);
    let summary = "";
    if (relevantRows.length) {
      const topHighlights = relevantRows.slice(0, 35).map((m: any) => {
        const pub = publications.find((p: any) => p.id === m.publication_id);
        return { title: pub?.title, kind: pub?.kind, themes: m.matched_themes, summary: m.summary };
      });
      const summaryPrompt = `Elabore uma síntese personalizada da edição ${edition.edition_date} (${edition.edition_type === "extra" ? "EDIÇÃO EXTRA" : "edição regular"}) do Diário Oficial, Parte II — Poder Legislativo, para este usuário, considerando exclusivamente os temas e correspondências abaixo. Indique os assuntos que mais aparecem, sem inventar fatos. Escreva um único parágrafo institucional de 90 a 150 palavras.\n\nTEMAS DO USUÁRIO: ${JSON.stringify(themesForPrompt.map((t: any) => t.name))}\n\nDESTAQUES: ${JSON.stringify(topHighlights)}`;
      try {
        summary = cleanText(await callGemini(summaryPrompt, 1100, false), 2600);
      } catch (summaryError) {
        console.warn("A classificação terminou, mas a síntese personalizada não foi gerada:", summaryError);
      }
    }
    if (!summary) {
      summary = relevantRows.length
        ? `Foram identificadas ${relevantRows.length} publicações relacionadas aos seus temas nesta edição, entre ${publications.length} publicações analisadas. Consulte os destaques para ver os temas correspondentes e os resumos individuais.`
        : `Foram analisadas ${publications.length} publicações. Nenhuma apresentou correspondência suficientemente clara com os seus temas ativos nesta edição.`;
    }

    const relevantCount = relevantRows.length;
    const { error: finishError } = await supabase
      .from("user_edition_analyses")
      .update({
        status: "processed",
        summary,
        publication_count: publications.length,
        relevant_count: relevantCount,
        error: null,
        finished_at: new Date().toISOString(),
      })
      .eq("user_id", userId)
      .eq("edition_id", editionId);
    if (finishError) throw finishError;

    return json({
      ok: true,
      cached: false,
      status: "processed",
      summary,
      publication_count: publications.length,
      relevant_count: relevantCount,
      has_active_themes: true,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error("Falha na análise temática personalizada:", message);
    if (analysisStarted) {
      await supabase
        .from("user_edition_analyses")
        .update({ status: "error", error: message.slice(0, 1800), finished_at: new Date().toISOString() })
        .eq("user_id", userId)
        .eq("edition_id", editionId);
    }
    return json({ error: message }, 502);
  }
<<<<<<< HEAD
});
=======
});
>>>>>>> e9ef30a828e48c2cc4e1e61e4b19a53a25b8e35a
