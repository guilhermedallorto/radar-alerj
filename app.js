(() => {
  const cfg = window.RADAR_CONFIG || {};
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));

  const SITE_TIME_ZONE = 'America/Sao_Paulo';

  const fmtDate = (
    value,
    options = { day:'2-digit', month:'short', year:'numeric' }
  ) => {
    if (!value) return 'Data não identificada';

    const raw = String(value).trim();
    const isDateOnly = /^\d{4}-\d{2}-\d{2}$/.test(raw);
    const dateToParse = isDateOnly ? `${raw}T12:00:00Z` : raw;
    const d = new Date(dateToParse);

    if (Number.isNaN(d.getTime())) return raw;

    return new Intl.DateTimeFormat('pt-BR', {
      ...options,
      timeZone: SITE_TIME_ZONE,
      hourCycle: 'h23'
    }).format(d);
  };

  const safeUrl = (value) => {
    try {
      const u = new URL(value);
      return ['https:','http:'].includes(u.protocol) ? u.href : '#';
    } catch {
      return '#';
    }
  };

  let client = null;
  let currentUser = null;
  let editions = [];
  let themes = [];
  let currentEdition = null;
  let currentPubs = [];
  let selectedTheme = '';
  let currentView = 'daily';
  let latestRun = null;
  let ingestionRuns = [];
  let currentAnalysis = null;
  let publicationOffset = 0;
  let publicationPageSize = 100;

  // Controle da análise temática assíncrona.
  let analysisPollTimer = null;
  let analysisDispatchError = '';

  function toast(message) {
    const node = $('toast');
    node.textContent = message;
    node.classList.remove('hidden');

    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => node.classList.add('hidden'), 4200);
  }

  function isRelevant(pub) {
    if (pub.manual_relevance === true) return true;
    if (pub.manual_relevance === false) return false;
    return !!pub.user_relevant;
  }

  function mergeUserMatch(pub, match, analysisStatus = null) {
    return {
      ...pub,
      user_relevant: !!match?.relevant,
      user_matched_themes: Array.isArray(match?.matched_themes) ? match.matched_themes : [],
      user_summary: match?.summary || '',
      user_justification: match?.justification || '',
      manual_relevance: match?.manual_relevance ?? null,
      manual_note: match?.manual_note || '',
      reviewed_at: match?.reviewed_at || null,
      theme_signature: match?.theme_signature || '',
      user_analysis_status: analysisStatus || (match ? 'processed' : null)
    };
  }

  async function attachUserMatches(publications) {
    const ids = publications.map(p => p.id).filter(Boolean);

    if (!ids.length) {
      return publications.map(p => mergeUserMatch(p, null));
    }

    const editionIds = [...new Set(publications.map(p => p.edition_id).filter(Boolean))];

    const [matchResult, analysisResult] = await Promise.all([
      client.from('user_publication_matches')
        .select('publication_id,relevant,matched_themes,summary,justification,manual_relevance,manual_note,reviewed_at,theme_signature')
        .in('publication_id', ids),

      editionIds.length
        ? client.from('user_edition_analyses')
            .select('edition_id,status')
            .in('edition_id', editionIds)
        : Promise.resolve({ data: [], error: null })
    ]);

    if (matchResult.error) throw matchResult.error;
    if (analysisResult.error) throw analysisResult.error;

    const byId = new Map((matchResult.data || []).map(m => [m.publication_id, m]));
    const statusByEdition = new Map((analysisResult.data || []).map(a => [a.edition_id, a.status]));

    return publications.map(p => {
      const status = statusByEdition.get(p.edition_id) || null;
      const candidate = byId.get(p.id);

      const hasManualReview =
        candidate?.manual_relevance !== null &&
        candidate?.manual_relevance !== undefined;

      const match = status === 'processed' || hasManualReview
        ? candidate
        : null;

      return mergeUserMatch(p, match, status);
    });
  }

  function pageText(p) {
    if (p.page_start && p.page_end && p.page_start !== p.page_end) {
      return `pp. ${p.page_start}–${p.page_end}`;
    }

    if (p.page_start) return `p. ${p.page_start}`;

    return 'página não identificada';
  }

  function publicationTags(pub) {
    const names = Array.isArray(pub.user_matched_themes)
      ? pub.user_matched_themes
      : [];

    return names.map(name => `<span class="tag">${esc(name)}</span>`).join(' ');
  }

  function renderCard(pub) {
    const rel = isRelevant(pub);
    const itemSummary = pub.user_summary;

    const summary = rel && itemSummary
      ? itemSummary
      : (pub.body_text || '').slice(0, 300) +
        ((pub.body_text || '').length > 300 ? '…' : '');

    const auth = pub.organization
      ? `Órgão: ${esc(pub.organization)}`
      : 'Órgão não identificado';

    const people = Array.isArray(pub.people) && pub.people.length
      ? ` · ${esc(pub.people.slice(0,4).join(', '))}`
      : '';

    return `<article class="highlight-card">
      <div class="card-top">
        <span class="tag ${rel?'green':'neutral'}">${rel ? 'Destaque dos seus temas' : esc(pub.kind || 'Publicação')}</span>
        <span class="page-ref">${esc(pageText(pub))}</span>
      </div>
      <h3>${esc(pub.title || 'Publicação sem título')}</h3>
      <p class="summary">${esc(summary)}</p>
      <div class="metadata">${auth}${people} · ${esc(fmtDate(pub.editions?.edition_date || currentEdition?.edition_date))}</div>
      ${rel && pub.user_justification ? `<div class="why-box"><strong>Por que importa</strong>${esc(pub.user_justification)}</div>` : ''}
      <div class="card-actions">
        <button data-open-pub="${esc(pub.id)}">Ler publicação →</button>
        <button data-review="${esc(pub.id)}">Revisar classificação</button>
      </div>
    </article>`;
  }

  function switchView(view) {
    currentView = view;

    document.querySelectorAll('.nav-tab').forEach(b => {
      b.classList.toggle('active', b.dataset.view === view);
    });

    document.querySelectorAll('.view').forEach(s => {
      s.classList.toggle('hidden', s.id !== `view-${view}`);
    });

    if (view === 'publications') loadPublications();
    if (view === 'themes') renderThemes();
    if (view === 'history') renderHistory();
  }

  async function init() {
    if (!cfg.SUPABASE_URL || !cfg.SUPABASE_PUBLISHABLE_KEY || !window.supabase?.createClient) {
      $('login-error').textContent = 'Configure config.js com a URL e a chave pública do Supabase. Consulte o README.';
      $('login-error').classList.remove('hidden');
      return;
    }

    client = window.supabase.createClient(
      cfg.SUPABASE_URL,
      cfg.SUPABASE_PUBLISHABLE_KEY,
      {
        auth: {
          persistSession: true,
          autoRefreshToken: true,
          detectSessionInUrl: true
        }
      }
    );

    const { data: { session } } = await client.auth.getSession();

    if (session) await enterApp(session.user);

    client.auth.onAuthStateChange(async (_event, session) => {
      if (session?.user && $('app').classList.contains('hidden')) {
        await enterApp(session.user);
      }

      if (!session) {
        stopAnalysisPolling();
        currentUser = null;
        currentEdition = null;
        currentAnalysis = null;
        $('app').classList.add('hidden');
        $('login-screen').classList.remove('hidden');
      }
    });
  }

  async function enterApp(user) {
    currentUser = user;

    $('login-screen').classList.add('hidden');
    $('app').classList.remove('hidden');

    $('header-date').textContent = new Intl.DateTimeFormat('pt-BR', {
      dateStyle: 'full',
      timeZone: SITE_TIME_ZONE
    }).format(new Date());

    try {
      await loadDashboard();
    } catch (e) {
      toast(`Não foi possível carregar o Radar: ${e.message}`);
      console.error(e);
    }
  }

  async function login(event) {
    event.preventDefault();

    $('login-error').classList.add('hidden');

    const { error } = await client.auth.signInWithPassword({
      email: $('email').value.trim(),
      password: $('password').value
    });

    if (error) {
      $('login-error').textContent = error.message;
      $('login-error').classList.remove('hidden');
    }
  }

  async function loadDashboard() {
    const [edRes, themeRes, runRes] = await Promise.all([
      client.from('editions')
        .select('*')
        .order('edition_date', { ascending:false })
        .limit(30),

      client.from('themes')
        .select('*')
        .order('priority', { ascending:true })
        .order('name'),

      client.from('ingestion_runs')
        .select('*')
        .order('started_at', { ascending:false })
        .limit(30)
    ]);

    if (edRes.error) throw edRes.error;
    if (themeRes.error) throw themeRes.error;
    if (runRes.error) throw runRes.error;

    editions = (edRes.data || []).sort((a,b) =>
      String(b.edition_date).localeCompare(String(a.edition_date)) ||
      ((a.edition_type === 'extra' ? 1 : 0) - (b.edition_type === 'extra' ? 1 : 0))
    );

    themes = themeRes.data || [];
    ingestionRuns = runRes.data || [];
    latestRun = ingestionRuns[0] || null;

    $('last-read').textContent = latestRun
      ? `Última verificação: ${fmtDate(latestRun.started_at,{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'})} · ${latestRun.status}`
      : 'Aguardando a primeira coleta';

    renderTopicRow();
    renderEditionTabs();
    renderEditionChecks();
    renderHistory(latestRun);

    if (editions.length) {
      await selectEdition(editions[0].id);
    } else {
      renderEmptyDashboard();
    }
  }

  function renderEmptyDashboard() {
    $('edition-title').textContent = 'Nenhuma edição processada';
    $('edition-subtitle').textContent = 'A primeira coleta será iniciada pelo fluxo de automação do projeto.';
    $('daily-summary').textContent = 'Assim que o robô localizar a Parte II do Diário Oficial, a síntese da edição aparecerá aqui.';

    $('edition-stats').innerHTML = [
      '0|Publicações identificadas',
      '0|Destaques temáticos',
      '0|Temas ativos',
      '—|Última edição'
    ].map(x => {
      const [n,l] = x.split('|');
      return `<div class="stat-card"><span class="stat-number">${n}</span><span class="stat-label">${l}</span></div>`;
    }).join('');

    $('highlights').innerHTML = '<div class="empty-state">O acervo ainda não possui publicações processadas.</div>';
  }

  function renderTopicRow() {
    const active = themes.filter(t => t.active);
    const counts = {};

    currentPubs.forEach(p => {
      (Array.isArray(p.user_matched_themes) ? p.user_matched_themes : [])
        .forEach(n => counts[n] = (counts[n] || 0) + 1);
    });

    $('topic-row').innerHTML = `
      <span class="topic-label">MONITORAMENTO</span>
      ${
        active.length
          ? active.map(t => `
              <button class="topic-pill" data-topic-filter="${esc(t.name)}">
                ${esc(t.name)} <strong>${counts[t.name] || 0}</strong>
              </button>
            `).join('')
          : '<span class="muted tiny">Nenhum tema ativo. Cadastre temas para personalizar o Radar.</span>'
      }
    `;
  }

  function renderEditionChecks() {
    const latestByType = {};

    for (const run of ingestionRuns) {
      const type = run.edition_type || 'regular';
      if (!latestByType[type]) latestByType[type] = run;
    }

    const renderCheck = (type, title, timeLabel) => {
      const run = latestByType[type];

      if (!run) {
        return `
          <div class="edition-check">
            <span class="edition-check-title">${title}</span>
            <span class="status-pill">Ainda não verificada</span>
          </div>
        `;
      }

      const statusLabel =
        run.status === 'success' ? 'Concluída' :
        run.status === 'warning' ? 'Concluída com avisos' :
        run.status === 'running' ? 'Em andamento' :
        run.status === 'error' ? 'Erro' :
        run.status;

      const detail = run.message ||
        (run.publications_found
          ? `${run.publications_found} publicações identificadas.`
          : 'Verificação registrada.');

      return `
        <div class="edition-check">
          <span class="edition-check-title">${title}</span>
          <span class="edition-check-time">
            ${timeLabel} · ${esc(fmtDate(run.started_at,{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}))}
          </span>
          <span class="status-pill ${run.status==='error'?'error':run.status==='warning'?'warning':''}">
            ${esc(statusLabel)}
          </span>
          <small>${esc(detail)}</small>
        </div>
      `;
    };

    const node = $('edition-check-status');

    if (node) {
      node.innerHTML =
        renderCheck('regular','Edição regular','Checagem diária') +
        renderCheck('extra','Edição extra','Verificação adicional');
    }
  }

  function renderEditionTabs() {
    const regular = editions
      .filter(e => (e.edition_type || 'regular') === 'regular')
      .slice(0, 10);

    const extra = editions
      .filter(e => e.edition_type === 'extra')
      .slice(0, 10);

    const renderGroup = (items, label, type) => `
      <div class="edition-tabs-group">
        <div class="edition-tabs-label">${label}</div>
        ${
          items.length
            ? items.map(e => `
                <button class="edition-tab ${currentEdition?.id === e.id ? 'active' : ''}" data-edition="${esc(e.id)}">
                  <strong>${esc(fmtDate(e.edition_date,{day:'2-digit',month:'short'}))}</strong>
                  <small>
                    ${type === 'extra'
                      ? `<span class="edition-extra-tag">${esc((e.edition_label || '').match(/EDIÇÃO EXTRA\s+\d+/i)?.[0] || 'EDIÇÃO EXTRA')}</span> · `
                      : ''
                    }
                    ${Number(e.publication_count || 0)} atos
                  </small>
                </button>
              `).join('')
            : `<span class="muted tiny">${type === 'extra' ? 'Nenhuma edição extra localizada.' : 'Nenhuma edição regular localizada.'}</span>`
        }
      </div>
    `;

    $('edition-tabs').innerHTML =
      renderGroup(regular, 'EDIÇÕES REGULARES', 'regular') +
      renderGroup(extra, 'EDIÇÕES EXTRAS', 'extra');
  }

  // ------------------------------------------------------------
  // ANÁLISE TEMÁTICA ASSÍNCRONA
  // ------------------------------------------------------------

  function stopAnalysisPolling() {
    if (analysisPollTimer) {
      clearTimeout(analysisPollTimer);
    }

    analysisPollTimer = null;
  }

  async function refreshEditionAnalysis(editionId) {
    const [matchResult, analysisResult] = await Promise.all([
      client.from('user_publication_matches')
        .select('*')
        .eq('edition_id', editionId),

      client.from('user_edition_analyses')
        .select('*')
        .eq('edition_id', editionId)
        .maybeSingle()
    ]);

    if (matchResult.error) throw matchResult.error;
    if (analysisResult.error) throw analysisResult.error;

    // Evita atualizar a tela caso o usuário já tenha selecionado outra edição.
    if (currentEdition?.id !== editionId) return;

    currentAnalysis = analysisResult.data || null;

    if (currentAnalysis?.status === 'processed') {
      analysisDispatchError = '';
    }

    // Enquanto a análise automática estiver pendente, somente as revisões
    // manuais são exibidas. Os resultados automáticos só são apresentados
    // depois que a análise da edição for concluída.
    const matchRows = (matchResult.data || []).filter(m => {
      const hasManualReview =
        m.manual_relevance !== null &&
        m.manual_relevance !== undefined;

      return currentAnalysis?.status === 'processed' || hasManualReview;
    });

    const matchByPublication = new Map(
      matchRows.map(m => [m.publication_id, m])
    );

    const editionAnalysisStatus = currentAnalysis?.status || null;

    currentPubs = currentPubs.map(p =>
      mergeUserMatch(
        p,
        matchByPublication.get(p.id),
        editionAnalysisStatus
      )
    );

    renderEditionTabs();
    renderTopicRow();
    renderDaily();
  }

  function scheduleAnalysisPolling(editionId, attempt = 0) {
    const maxAttempts = 90;
    const intervalMs = 8000;

    if (attempt >= maxAttempts) {
      if (
        currentEdition?.id === editionId &&
        ['queued', 'processing'].includes(currentAnalysis?.status)
      ) {
        $('daily-summary').textContent =
          'A análise continua em processamento no servidor. Você pode continuar utilizando o Radar; os resultados serão atualizados quando abrir novamente esta edição.';
      }

      return;
    }

    analysisPollTimer = setTimeout(async () => {
      analysisPollTimer = null;

      if (currentEdition?.id !== editionId) return;

      try {
        await refreshEditionAnalysis(editionId);
      } catch (error) {
        console.error(
          'Erro ao atualizar o andamento da análise:',
          error
        );
      }

      if (
        currentEdition?.id === editionId &&
        ['queued', 'processing'].includes(currentAnalysis?.status)
      ) {
        scheduleAnalysisPolling(editionId, attempt + 1);
      }
    }, intervalMs);
  }

  async function selectEdition(id) {
    stopAnalysisPolling();

    currentEdition = editions.find(e => e.id === id) || null;
    currentAnalysis = null;
    analysisDispatchError = '';

    if (!currentEdition) return;

    const { data, error } = await client.from('publications')
      .select('*')
      .eq('edition_id', id)
      .order('page_start', { ascending:true })
      .limit(3000);

    if (error) throw error;

    // Inicialmente, as publicações são carregadas sem classificação automática.
    currentPubs = (data || []).map(p => mergeUserMatch(p, null));

    renderEditionTabs();
    renderTopicRow();

    $('highlights').innerHTML =
      '<div class="empty-state">Preparando a análise temática desta edição…</div>';

    $('daily-summary').textContent =
      'O Radar está solicitando a análise personalizada desta edição.';

    // A Edge Function agora apenas registra a solicitação na fila e aciona
    // o workflow do GitHub Actions. A classificação propriamente dita é
    // realizada pelo worker Python, fora do limite de execução da Edge Function.
    try {
      const {
        data: analysisResponse,
        error: functionError
      } = await client.functions.invoke(
        'analyze-user-themes',
        {
          body: {
            edition_id: id
          }
        }
      );

      if (functionError) throw functionError;

      if (analysisResponse?.error) {
        throw new Error(analysisResponse.error);
      }
    } catch (analysisError) {
      console.error(
        'Falha ao solicitar a análise temática do usuário:',
        analysisError
      );

      analysisDispatchError =
        analysisError.message || String(analysisError);
    }

    // Consulta o estado persistido no Supabase, independentemente de a
    // solicitação à Edge Function ter retornado sucesso ou erro.
    try {
      await refreshEditionAnalysis(id);
    } catch (refreshError) {
      console.error(
        'Falha ao consultar o estado da análise temática:',
        refreshError
      );

      if (currentEdition?.id === id) {
        analysisDispatchError = analysisDispatchError
          ? `${analysisDispatchError}. Também não foi possível consultar o status da análise: ${refreshError.message || refreshError}`
          : `Não foi possível consultar o status da análise: ${refreshError.message || refreshError}`;

        renderDaily();
      }

      toast('Não foi possível consultar o andamento da análise temática.');
      return;
    }

    // Mantém a consulta periódica enquanto a análise estiver na fila
    // ou sendo processada pelo worker.
    if (
      currentEdition?.id === id &&
      ['queued', 'processing'].includes(currentAnalysis?.status)
    ) {
      scheduleAnalysisPolling(id);
    }
  }

  // ------------------------------------------------------------
  // PAINEL DIÁRIO
  // ------------------------------------------------------------

  function renderDaily() {
    const e = currentEdition;

    $('edition-weekday').textContent = e
      ? fmtDate(e.edition_date,{weekday:'long'})
      : 'EDIÇÃO SELECIONADA';

    $('edition-title').textContent = e
      ? fmtDate(e.edition_date,{weekday:'long',day:'numeric',month:'long',year:'numeric'})
      : 'Leitura diária';

    const extraLabel =
      e?.edition_type === 'extra' &&
      !String(e?.edition_label || '').toUpperCase().includes('EDIÇÃO EXTRA')
        ? 'EDIÇÃO EXTRA · '
        : '';

    $('edition-subtitle').textContent = e
      ? `${extraLabel}${e.edition_label || 'Parte II · Poder Legislativo'} · ${
          e.status === 'processed'
            ? 'edição processada'
            : e.status === 'error'
              ? 'falha no processamento'
              : 'em processamento'
        }`
      : '';

    $('edition-source').href = safeUrl(
      e?.source_pdf_url || e?.source_page_url || '#'
    );

    const analysisPending = ['queued', 'processing'].includes(
      currentAnalysis?.status
    );

    if (!currentAnalysis && analysisDispatchError) {
      $('daily-summary').textContent =
        `Não foi possível confirmar o início da análise dos seus temas nesta edição: ${analysisDispatchError}. O texto integral das publicações continua disponível no acervo. Abra novamente esta edição para tentar iniciar a análise.`;

    } else if (analysisPending) {
      const pendingMessage = currentAnalysis.status === 'queued'
        ? 'A análise dos seus temas foi registrada e está aguardando processamento.'
        : 'A análise dos seus temas está em processamento pelo servidor.';

      $('daily-summary').textContent =
        `${pendingMessage} O Radar atualizará os resultados automaticamente quando a classificação for concluída.` +
        (
          analysisDispatchError
            ? ` Houve também uma falha na confirmação da solicitação: ${analysisDispatchError}.`
            : ''
        );

    } else if (currentAnalysis?.status === 'error') {
      $('daily-summary').textContent =
        `Não foi possível concluir a análise dos seus temas nesta edição: ${currentAnalysis.error || 'erro não detalhado'}. O texto integral das publicações continua disponível no acervo. Abra novamente esta edição para tentar realizar a análise outra vez.`;

    } else {
      $('daily-summary').textContent =
        currentAnalysis?.summary ||
        (
          e?.status === 'error'
            ? `A coleta falhou: ${e.error || 'erro não detalhado'}`
            : 'A análise personalizada desta edição ainda não foi concluída para sua conta.'
        );
    }

    const relevant = currentPubs.filter(isRelevant);

    $('edition-stats').innerHTML = [
      [currentPubs.length,'Publicações identificadas'],
      [relevant.length,'Destaques dos seus temas'],
      [themes.filter(t => t.active).length,'Temas ativos'],
      [
        e?.status === 'processed'
          ? 'Concluída'
          : (e?.status || '—'),
        'Situação da coleta'
      ]
    ].map(([n,l]) => `
      <div class="stat-card">
        <span class="stat-number">${esc(n)}</span>
        <span class="stat-label">${esc(l)}</span>
      </div>
    `).join('');

    // Enquanto a análise estiver na fila ou em processamento, não apresentar
    // como definitivos os resultados automáticos ainda incompletos.
    if (analysisPending) {
      $('highlights').innerHTML = `
        <div class="empty-state">
          A classificação das publicações de acordo com os seus temas está em andamento.
          Os destaques aparecerão automaticamente nesta área assim que a análise for concluída.
        </div>
      `;

      $('highlight-count').textContent =
        'Aguardando a conclusão da análise temática desta edição.';

      $('other-publications').innerHTML = `
        <div class="empty-state">
          A classificação ainda está em andamento. Você já pode consultar o texto integral
          de todas as publicações na aba Publicações.
        </div>
      `;

      return;
    }

    $('highlights').innerHTML = relevant.length
      ? relevant.slice(0,12).map(renderCard).join('')
      : `
          <div class="empty-state">
            ${
              themes.some(t => t.active)
                ? 'Nenhuma publicação foi classificada como relevante para os seus temas ativos nesta edição. O acervo completo permanece disponível abaixo.'
                : 'Você ainda não possui temas ativos. Cadastre seus temas para receber destaques personalizados.'
            }
          </div>
        `;

    $('highlight-count').textContent =
      `${relevant.length} publicação(ões) relevantes para os seus temas nesta edição`;

    const others = currentPubs.filter(p => !isRelevant(p));

    const grouped = others.reduce((acc,p) => {
      const k = p.kind || 'Outras publicações';
      (acc[k] = acc[k] || []).push(p);
      return acc;
    }, {});

    const groups = Object.entries(grouped)
      .sort((a,b) => b[1].length - a[1].length);

    $('other-publications').innerHTML = groups.length
      ? groups.map(([kind,items]) => `
          <section class="other-group">
            <div class="other-group-head">
              <span>${esc(kind)}</span>
              <span>${items.length}</span>
            </div>
            ${items.slice(0,8).map(p => `
              <div class="other-item">
                <div>
                  <strong>${esc(p.title)}</strong>
                  <small>${esc(pageText(p))} · ${esc(p.organization || 'Órgão não identificado')}</small>
                </div>
                <button data-open-pub="${esc(p.id)}">Abrir →</button>
              </div>
            `).join('')}
            ${
              items.length > 8
                ? `
                    <div class="other-item">
                      <small>Mais ${items.length-8} publicação(ões) nesta categoria.</small>
                      <button data-go="publications">Ver tudo →</button>
                    </div>
                  `
                : ''
            }
          </section>
        `).join('')
      : '<div class="empty-state">Não há outras publicações nesta edição.</div>';
  }

  // ------------------------------------------------------------
  // ACERVO DE PUBLICAÇÕES
  // ------------------------------------------------------------

  async function loadPublications(append = false) {
    if (!append) publicationOffset = 0;

    const query = $('publication-query').value.trim();
    const status = $('publication-filter').value;
    const edition = $('publication-date').value;

    let req = client.from('publications')
      .select('*, editions(edition_date,edition_type,edition_label)')
      .order('created_at',{ascending:false})
      .range(publicationOffset, publicationOffset + publicationPageSize - 1);

    if (edition !== 'all') {
      req = req.eq('edition_id', edition);
    }

    if (query) {
      req = req.textSearch(
        'search_vector',
        query,
        {
          type:'websearch',
          config:'portuguese'
        }
      );
    }

    const { data, error } = await req;

    if (error) {
      toast(`Falha na pesquisa: ${error.message}`);
      return;
    }

    const page = await attachUserMatches(data || []);

    publicationOffset += page.length;

    let items = page;

    if (status === 'relevant') {
      items = items.filter(isRelevant);
    }

    if (status === 'other') {
      items = items.filter(p =>
        p.user_analysis_status === 'processed' && !isRelevant(p)
      );
    }

    if (selectedTheme) {
      items = items.filter(p =>
        (p.user_matched_themes || []).includes(selectedTheme)
      );
    }

    const html = items.map(p => {
      const relevanceLabel = isRelevant(p)
        ? 'Relevante para você'
        : p.user_analysis_status === 'processed'
          ? (themes.some(t => t.active)
              ? 'Não corresponde aos seus temas'
              : 'Nenhum tema ativo')
          : 'Ainda não analisada para seus temas';

      const tagClass = isRelevant(p) ? 'green' : 'neutral';
      const excerpt = p.user_summary || p.body_text || '';

      return `
        <article class="publication-row">
          <div class="pub-date">
            ${esc(fmtDate(p.editions?.edition_date))}
            ${p.editions?.edition_type === 'extra'
              ? `<br><span class="edition-extra-tag">${esc((p.editions?.edition_label || '').match(/EDIÇÃO EXTRA\s+\d+/i)?.[0] || 'EDIÇÃO EXTRA')}</span>`
              : ''
            }
            <br>${esc(pageText(p))}
          </div>
          <div>
            <h3>${esc(p.title)}</h3>
            <p>${esc(excerpt.slice(0,280))}${excerpt.length > 280 ? '…' : ''}</p>
            <div class="pub-meta">
              <span class="tag ${tagClass}">${relevanceLabel}</span>
              ${publicationTags(p)}
              <span class="tiny muted">${esc(p.kind || '')}</span>
            </div>
          </div>
          <button class="text-button" data-open-pub="${esc(p.id)}">Abrir →</button>
        </article>
      `;
    }).join('');

    if (append) {
      $('publication-results').insertAdjacentHTML('beforeend', html);
    } else {
      $('publication-results').innerHTML =
        html || '<div class="empty-state">Nenhuma publicação encontrada com esses filtros.</div>';
    }

    $('pub-load-more').classList.toggle(
      'hidden',
      page.length < publicationPageSize
    );
  }

  // ------------------------------------------------------------
  // TEMAS PERSONALIZADOS
  // ------------------------------------------------------------

  function renderThemes() {
    $('theme-list').innerHTML = themes.length
      ? themes.map(t => `
          <article class="theme-item">
            <div>
              <h3>
                ${esc(t.name)}
                ${t.active
                  ? '<span class="tag green">Ativo</span>'
                  : '<span class="tag neutral">Inativo</span>'
                }
              </h3>
              <p>${esc(t.description || 'Sem descrição')} · Prioridade ${t.priority || 3}</p>
              <p>Palavras-chave: ${esc((t.keywords || []).join(', ') || 'não cadastradas')}</p>
            </div>
            <div class="theme-actions">
              <button data-edit-theme="${esc(t.id)}">Editar</button>
              <button data-toggle-theme="${esc(t.id)}">${t.active ? 'Desativar' : 'Ativar'}</button>
            </div>
          </article>
        `).join('')
      : '<div class="empty-state">Cadastre o primeiro tema que o Radar deverá monitorar.</div>';
  }

  function clearThemeForm() {
    $('theme-form').reset();
    $('theme-id').value = '';
    $('theme-priority').value = '3';
    $('theme-active').checked = true;
    $('theme-cancel').classList.add('hidden');
  }

  async function saveTheme(event) {
    event.preventDefault();

    const id = $('theme-id').value;

    const payload = {
      user_id: currentUser.id,
      name: $('theme-name').value.trim(),
      description: $('theme-description').value.trim(),
      keywords: $('theme-keywords').value.split(',').map(x => x.trim()).filter(Boolean),
      include_criteria: $('theme-include').value.trim(),
      exclude_criteria: $('theme-exclude').value.trim(),
      priority: Number($('theme-priority').value),
      active: $('theme-active').checked
    };

    if (!payload.name) return;

    const result = id
      ? await client.from('themes').update(payload).eq('id',id)
      : await client.from('themes').insert(payload);

    if (result.error) {
      toast(`Não foi possível salvar o tema: ${result.error.message}`);
      return;
    }

    const refreshed = await client.from('themes')
      .select('*')
      .order('priority');

    themes = refreshed.data || [];

    clearThemeForm();
    renderThemes();
    renderTopicRow();

    if (currentEdition) {
      await selectEdition(currentEdition.id);
    }

    toast('Tema salvo. A análise desta edição foi solicitada novamente para sua conta.');
  }

  async function editTheme(id) {
    const t = themes.find(x => x.id === id);
    if (!t) return;

    $('theme-id').value = t.id;
    $('theme-name').value = t.name;
    $('theme-description').value = t.description || '';
    $('theme-keywords').value = (t.keywords || []).join(', ');
    $('theme-include').value = t.include_criteria || '';
    $('theme-exclude').value = t.exclude_criteria || '';
    $('theme-priority').value = String(t.priority || 3);
    $('theme-active').checked = !!t.active;
    $('theme-cancel').classList.remove('hidden');

    window.scrollTo({
      top:0,
      behavior:'smooth'
    });
  }

  async function toggleTheme(id) {
    const t = themes.find(x => x.id === id);
    if (!t) return;

    const { error } = await client.from('themes')
      .update({ active:!t.active })
      .eq('id',id);

    if (error) {
      toast(error.message);
      return;
    }

    t.active = !t.active;

    renderThemes();
    renderTopicRow();

    if (currentEdition) {
      await selectEdition(currentEdition.id);
    }

    toast(`Tema ${t.active ? 'ativado' : 'desativado'}.`);
  }

  // ------------------------------------------------------------
  // LEITURA E REVISÃO DE PUBLICAÇÕES
  // ------------------------------------------------------------

  async function openPublication(id) {
    const { data, error } = await client.from('publications')
      .select('*, editions(edition_date,edition_type,edition_label,source_pdf_url)')
      .eq('id',id)
      .single();

    if (error) {
      toast(error.message);
      return;
    }

    const {
      data: match,
      error: matchError
    } = await client.from('user_publication_matches')
      .select('*')
      .eq('publication_id',id)
      .maybeSingle();

    if (matchError) {
      toast(matchError.message);
      return;
    }

    const p = mergeUserMatch(data,match);
    const rel = isRelevant(p);
    const editUrl = safeUrl(p.editions?.source_pdf_url || p.source_url);
    const extra = p.editions?.edition_type === 'extra'
      ? ' · EDIÇÃO EXTRA'
      : '';

    $('modal-content').innerHTML = `
      <div class="section-kicker">
        ${esc(p.kind || 'PUBLICAÇÃO')} · ${esc(fmtDate(p.editions?.edition_date))}${extra}
      </div>
      <h2 id="modal-title">${esc(p.title)}</h2>
      <p class="muted tiny">
        ${esc(pageText(p))} · ${esc(p.organization || 'Órgão não identificado')}
      </p>
      <div class="pub-meta">
        ${rel
          ? '<span class="tag green">Relevante para você</span>'
          : '<span class="tag neutral">Demais atos</span>'
        }
        ${publicationTags(p)}
      </div>
      ${p.user_summary
        ? `<div class="why-box"><strong>Resumo para seus temas</strong>${esc(p.user_summary)}</div>`
        : ''
      }
      ${p.user_justification
        ? `<div class="why-box"><strong>Por que importa</strong>${esc(p.user_justification)}</div>`
        : ''
      }
      <h3 style="font-size:12px;margin:20px 0 8px">Texto da publicação</h3>
      <div class="full-text">${esc(p.body_text)}</div>
      <p style="margin-top:14px">
        <a class="btn btn-outline" href="${editUrl}" target="_blank" rel="noopener">
          Abrir / baixar Diário Oficial ↗
        </a>
      </p>
      <div class="card-actions">
        <button data-review="${esc(p.id)}">Revisar classificação</button>
        <span class="muted tiny">
          Classificação automática personalizada: ${isRelevant(p) ? 'relevante' : 'não relevante'}
        </span>
      </div>
    `;

    $('publication-modal').classList.remove('hidden');
  }

  async function reviewPublication(id) {
    const selected = window.prompt(
      'Classificação final para sua conta: digite R para relevante ou I para irrelevante. Deixe em branco para cancelar.'
    );

    if (!selected) return;

    const normalized = selected.trim().toUpperCase();

    if (!['R','I'].includes(normalized)) {
      toast('Digite R ou I.');
      return;
    }

    const note = window.prompt('Observação da revisão (opcional):') || '';

    const {
      data: publication,
      error: publicationError
    } = await client.from('publications')
      .select('id,edition_id')
      .eq('id',id)
      .single();

    if (publicationError) {
      toast(publicationError.message);
      return;
    }

    const {
      data: existingMatch,
      error: matchError
    } = await client.from('user_publication_matches')
      .select('theme_signature,relevant,matched_themes,confidence,justification,summary')
      .eq('publication_id',id)
      .maybeSingle();

    if (matchError) {
      toast(matchError.message);
      return;
    }

    const { error } = await client.from('user_publication_matches')
      .upsert({
        user_id: currentUser.id,
        edition_id: publication.edition_id,
        publication_id: id,
        theme_signature: existingMatch?.theme_signature || currentAnalysis?.theme_signature || 'manual-review',
        relevant: existingMatch?.relevant ?? false,
        matched_themes: existingMatch?.matched_themes || [],
        confidence: existingMatch?.confidence ?? null,
        justification: existingMatch?.justification || null,
        summary: existingMatch?.summary || null,
        manual_relevance: normalized === 'R',
        manual_note: note,
        reviewed_at: new Date().toISOString()
      }, {
        onConflict:'user_id,publication_id'
      });

    if (error) {
      toast(`Falha ao salvar revisão: ${error.message}`);
      return;
    }

    $('publication-modal').classList.add('hidden');

    if (currentEdition?.id === publication.edition_id) {
      await selectEdition(publication.edition_id);
    } else {
      await loadPublications(false);
    }

    toast('Revisão particular da sua conta registrada.');
  }

  // ------------------------------------------------------------
  // PESQUISA NO ACERVO COM IA
  // ------------------------------------------------------------

  async function runResearch(event) {
    event.preventDefault();

    const question = $('research-question').value.trim();
    if (!question) return;

    const btn = $('research-submit');

    btn.disabled = true;
    btn.textContent = 'Pesquisando…';

    $('research-answer').classList.remove('hidden');
    $('research-answer').innerHTML =
      '<p class="muted">Consultando publicações e preparando a resposta com referências...</p>';

    try {
      const { data, error } = await client.functions.invoke(
        'ask-radar',
        {
          body: {
            question
          }
        }
      );

      if (error) throw error;
      if (data.error) throw new Error(data.error);

      const sources = data.sources || [];

      $('research-answer').innerHTML = `
        <h3>Resultado da pesquisa</h3>
        <div class="answer-text">${esc(data.answer || 'Não foi possível elaborar uma resposta.')}</div>
        <div class="source-list">
          <h4>Fontes consultadas</h4>
          ${sources.map(s => `
            <a class="source-link"
               href="${safeUrl(s.sourceUrl)}"
               target="_blank"
               rel="noopener">
              [${s.ref}] ${esc(s.title)} · ${esc(fmtDate(s.editionDate))}${s.editionType === 'extra' ? ' · EDIÇÃO EXTRA' : ''} · ${esc(s.pageStart ? `p. ${s.pageStart}` : 'página não identificada')} ↗
            </a>
          `).join('')}
        </div>
      `;
    } catch (e) {
      $('research-answer').innerHTML =
        `<p class="error">Não foi possível concluir a pesquisa: ${esc(e.message || String(e))}</p>`;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Pesquisar no acervo';
    }
  }

  // ------------------------------------------------------------
  // HISTÓRICO
  // ------------------------------------------------------------

  function renderHistory(lastRun = latestRun) {
    const ordered = [...editions].sort((a,b) =>
      String(b.edition_date).localeCompare(String(a.edition_date)) ||
      ((a.edition_type === 'extra' ? 1 : 0) - (b.edition_type === 'extra' ? 1 : 0))
    );

    $('edition-history').innerHTML = ordered.length
      ? ordered.map(e => `
          <div class="history-item">
            <strong>${esc(fmtDate(e.edition_date))}</strong>
            <div>
              <small>
                ${e.edition_type === 'extra' ? '<span class="edition-extra-tag">EDIÇÃO EXTRA</span> · ' : ''}
                ${esc(e.edition_label || 'Parte II · Poder Legislativo')} · ${Number(e.publication_count || 0)} atos
              </small>
            </div>
            <span class="status-pill ${e.status === 'error' ? 'error' : ''}">
              ${esc(e.status === 'processed' ? 'Processada' : e.status === 'error' ? 'Erro' : 'Pendente')}
            </span>
            <a href="${safeUrl(e.source_pdf_url || e.source_page_url)}" target="_blank" rel="noopener">
              Abrir Diário ↗
            </a>
          </div>
        `).join('')
      : '<div class="empty-state">Nenhuma edição cadastrada.</div>';

    const run = lastRun;

    $('run-status').textContent = run
      ? `Última execução: ${fmtDate(run.started_at,{day:'2-digit',month:'long',year:'numeric',hour:'2-digit',minute:'2-digit'})} · ${run.status} · ${run.edition_type === 'extra' ? 'verificação de edição extra · ' : ''}${run.message || ''}`
      : 'Ainda não há execuções registradas.';

    const date = $('publication-date');

    if (date) {
      const current = date.value || 'all';

      date.innerHTML =
        '<option value="all">Todas as edições</option>' +
        ordered.map(e => `
          <option value="${esc(e.id)}">
            ${esc(fmtDate(e.edition_date))}${e.edition_type === 'extra'
              ? ` — ${esc((e.edition_label || '').match(/EDIÇÃO EXTRA\s+\d+/i)?.[0] || 'EDIÇÃO EXTRA')}`
              : ''
            }
          </option>
        `).join('');

      date.value = [...date.options].some(o => o.value === current)
        ? current
        : 'all';
    }
  }

  // ------------------------------------------------------------
  // EVENTOS DA INTERFACE
  // ------------------------------------------------------------

  document.addEventListener('click', async (event) => {
    const tab = event.target.closest('[data-view]');

    if (tab) {
      switchView(tab.dataset.view);
      return;
    }

    const ed = event.target.closest('[data-edition]');

    if (ed) {
      await selectEdition(ed.dataset.edition);
      return;
    }

    const open = event.target.closest('[data-open-pub]');

    if (open) {
      await openPublication(open.dataset.openPub);
      return;
    }

    const rev = event.target.closest('[data-review]');

    if (rev) {
      await reviewPublication(rev.dataset.review);
      return;
    }

    const edit = event.target.closest('[data-edit-theme]');

    if (edit) {
      editTheme(edit.dataset.editTheme);
      return;
    }

    const toggle = event.target.closest('[data-toggle-theme]');

    if (toggle) {
      await toggleTheme(toggle.dataset.toggleTheme);
      return;
    }

    const topic = event.target.closest('[data-topic-filter]');

    if (topic) {
      selectedTheme = topic.dataset.topicFilter;
      $('publication-filter').value = 'all';
      $('publication-query').value = '';
      switchView('publications');
      return;
    }

    const go = event.target.closest('[data-go]');

    if (go) {
      switchView(go.dataset.go);
      return;
    }

    if (event.target.closest('[data-close-modal]')) {
      $('publication-modal').classList.add('hidden');
      return;
    }
  });

  $('login-form').addEventListener('submit', login);

  $('logout-btn').addEventListener('click', async () => {
    stopAnalysisPolling();
    await client.auth.signOut();
  });

  $('global-search-btn').addEventListener('click', () => {
    $('publication-query').value = $('global-search').value;
    selectedTheme = '';
    $('publication-filter').value = 'all';
    $('publication-date').value = 'all';
    switchView('publications');
  });

  $('global-search').addEventListener('keydown', e => {
    if (e.key === 'Enter') $('global-search-btn').click();
  });

  $('publication-search-btn').addEventListener('click', () => {
    loadPublications(false);
  });

  $('pub-load-more').addEventListener('click', () => {
    loadPublications(true);
  });

  $('publication-query').addEventListener('keydown', e => {
    if (e.key === 'Enter') loadPublications(false);
  });

  $('theme-form').addEventListener('submit', saveTheme);
  $('theme-cancel').addEventListener('click', clearThemeForm);
  $('research-form').addEventListener('submit', runResearch);

  init();
})();
