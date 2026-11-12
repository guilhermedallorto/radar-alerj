(() => {
  const cfg = window.RADAR_CONFIG || {};
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const fmtDate = (value, options = { day:'2-digit', month:'short', year:'numeric' }) => {
    if (!value) return 'Data não identificada';
    const d = new Date(`${String(value).slice(0,10)}T12:00:00-03:00`);
    return Number.isNaN(d.getTime()) ? String(value) : new Intl.DateTimeFormat('pt-BR', options).format(d);
  };
  const safeUrl = (value) => {
    try { const u = new URL(value); return ['https:','http:'].includes(u.protocol) ? u.href : '#'; } catch { return '#'; }
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
  let publicationOffset = 0;
  let publicationPageSize = 100;

  function toast(message) {
    const node = $('toast'); node.textContent = message; node.classList.remove('hidden');
    clearTimeout(toast.timer); toast.timer = setTimeout(() => node.classList.add('hidden'), 4200);
  }
  function isRelevant(pub) {
    if (pub.manual_relevance === true) return true;
    if (pub.manual_relevance === false) return false;
    return !!pub.ai_relevant;
  }
  function pageText(p) {
    if (p.page_start && p.page_end && p.page_start !== p.page_end) return `pp. ${p.page_start}–${p.page_end}`;
    if (p.page_start) return `p. ${p.page_start}`;
    return 'página não identificada';
  }
  function publicationTags(pub) {
    const names = Array.isArray(pub.matched_themes) ? pub.matched_themes : [];
    return names.map(name => `<span class="tag">${esc(name)}</span>`).join(' ');
  }
  function renderCard(pub) {
    const rel = isRelevant(pub);
    const summary = rel && pub.summary ? pub.summary : (pub.body_text || '').slice(0, 300) + ((pub.body_text || '').length > 300 ? '…' : '');
    const auth = pub.organization ? `Órgão: ${esc(pub.organization)}` : 'Órgão não identificado';
    const people = Array.isArray(pub.people) && pub.people.length ? ` · ${esc(pub.people.slice(0,4).join(', '))}` : '';
    return `<article class="highlight-card">
      <div class="card-top"><span class="tag ${rel?'green':'neutral'}">${rel ? 'Destaque para o gabinete' : esc(pub.kind || 'Publicação')}</span><span class="page-ref">${esc(pageText(pub))}</span></div>
      <h3>${esc(pub.title || 'Publicação sem título')}</h3>
      <p class="summary">${esc(summary)}</p>
      <div class="metadata">${auth}${people} · ${esc(fmtDate(pub.editions?.edition_date || currentEdition?.edition_date))}</div>
      ${rel && pub.justification ? `<div class="why-box"><strong>Por que importa</strong>${esc(pub.justification)}</div>` : ''}
      <div class="card-actions"><button data-open-pub="${esc(pub.id)}">Ler publicação →</button><button data-review="${esc(pub.id)}">Revisar classificação</button></div>
    </article>`;
  }
  function switchView(view) {
    currentView = view;
    document.querySelectorAll('.nav-tab').forEach(b => b.classList.toggle('active', b.dataset.view === view));
    document.querySelectorAll('.view').forEach(s => s.classList.toggle('hidden', s.id !== `view-${view}`));
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
    client = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_PUBLISHABLE_KEY, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true }
    });
    const { data: { session } } = await client.auth.getSession();
    if (session) await enterApp(session.user);
    client.auth.onAuthStateChange(async (_event, session) => {
      if (session?.user && $('app').classList.contains('hidden')) await enterApp(session.user);
      if (!session) {
        currentUser = null; $('app').classList.add('hidden'); $('login-screen').classList.remove('hidden');
      }
    });
  }
  async function enterApp(user) {
    currentUser = user;
    $('login-screen').classList.add('hidden'); $('app').classList.remove('hidden');
    $('header-date').textContent = new Intl.DateTimeFormat('pt-BR',{dateStyle:'full'}).format(new Date());
    try { await loadDashboard(); }
    catch (e) { toast(`Não foi possível carregar o Radar: ${e.message}`); console.error(e); }
  }
  async function login(event) {
    event.preventDefault();
    $('login-error').classList.add('hidden');
    const { error } = await client.auth.signInWithPassword({ email: $('email').value.trim(), password: $('password').value });
    if (error) { $('login-error').textContent = error.message; $('login-error').classList.remove('hidden'); }
  }
  async function loadDashboard() {
    const [edRes, themeRes, runRes] = await Promise.all([
      client.from('editions').select('*').order('edition_date', { ascending:false }).limit(30),
      client.from('themes').select('*').order('priority', { ascending:true }).order('name'),
      client.from('ingestion_runs').select('*').order('started_at', { ascending:false }).limit(1)
    ]);
    if (edRes.error) throw edRes.error;
    if (themeRes.error) throw themeRes.error;
    editions = edRes.data || []; themes = themeRes.data || []; latestRun = runRes.data?.[0] || null;
    $('last-read').textContent = runRes.data?.[0]?.finished_at ? `Última coleta: ${fmtDate(runRes.data[0].finished_at)} · ${esc(runRes.data[0].status)}` : 'Aguardando a primeira coleta';
    renderTopicRow(); renderEditionTabs(); renderHistory(latestRun);
    if (editions.length) await selectEdition(editions[0].id);
    else renderEmptyDashboard();
  }
  function renderEmptyDashboard() {
    $('edition-title').textContent = 'Nenhuma edição processada';
    $('edition-subtitle').textContent = 'A primeira coleta será iniciada pelo fluxo de automação do projeto.';
    $('daily-summary').textContent = 'Assim que o robô localizar a Parte II do Diário Oficial, a síntese da edição aparecerá aqui.';
    $('edition-stats').innerHTML = ['0|Publicações identificadas','0|Destaques temáticos','0|Temas ativos','—|Última edição'].map(x=>{const [n,l]=x.split('|');return `<div class="stat-card"><span class="stat-number">${n}</span><span class="stat-label">${l}</span></div>`}).join('');
    $('highlights').innerHTML = '<div class="empty-state">O acervo ainda não possui publicações processadas.</div>';
  }
  function renderTopicRow() {
    const active = themes.filter(t => t.active);
    const counts = {};
    currentPubs.forEach(p => (Array.isArray(p.matched_themes)?p.matched_themes:[]).forEach(n => counts[n]=(counts[n]||0)+1));
    $('topic-row').innerHTML = `<span class="topic-label">MONITORAMENTO</span>${active.length ? active.map(t=>`<button class="topic-pill" data-topic-filter="${esc(t.name)}">${esc(t.name)} <strong>${counts[t.name]||0}</strong></button>`).join('') : '<span class="muted tiny">Nenhum tema ativo. Cadastre temas para personalizar o Radar.</span>'}`;
  }
  function renderEditionTabs() {
    $('edition-tabs').innerHTML = editions.slice(0,10).map(e=>`<button class="edition-tab ${currentEdition?.id===e.id?'active':''}" data-edition="${esc(e.id)}"><strong>${esc(fmtDate(e.edition_date,{day:'2-digit',month:'short'}))}</strong><small>${Number(e.publication_count||0)} atos · ${Number(e.relevant_count||0)} destaques</small></button>`).join('');
  }
  async function selectEdition(id) {
    currentEdition = editions.find(e => e.id === id) || null;
    if (!currentEdition) return;
    const { data, error } = await client.from('publications').select('*').eq('edition_id', id).order('page_start', {ascending:true}).limit(1200);
    if (error) throw error;
    currentPubs = data || [];
    renderEditionTabs(); renderTopicRow(); renderDaily();
  }
  function renderDaily() {
    const e = currentEdition;
    $('edition-weekday').textContent = e ? fmtDate(e.edition_date,{weekday:'long'}) : 'EDIÇÃO SELECIONADA';
    $('edition-title').textContent = e ? fmtDate(e.edition_date,{weekday:'long',day:'numeric',month:'long',year:'numeric'}) : 'Leitura diária';
    $('edition-subtitle').textContent = e ? `${e.edition_label || 'Parte II · Poder Legislativo'} · ${e.status==='processed'?'edição processada':e.status==='error'?'falha no processamento':'em processamento'}` : '';
    $('edition-source').href = safeUrl(e?.source_pdf_url || e?.source_page_url || '#');
    $('daily-summary').textContent = e?.daily_summary || (e?.status==='error' ? `A última tentativa de processamento falhou: ${e.error || 'erro não detalhado'}` : 'A síntese ainda não foi gerada para esta edição.');
    const relevant = currentPubs.filter(isRelevant);
    const counts = themes.filter(t=>t.active).map(t=>({name:t.name,count:currentPubs.filter(p=>isRelevant(p)&&(p.matched_themes||[]).includes(t.name)).length}));
    $('edition-stats').innerHTML = [
      [currentPubs.length,'Publicações identificadas'],[relevant.length,'Destaques para o gabinete'],[themes.filter(t=>t.active).length,'Temas ativos'],[e?.status==='processed'?'Concluída':(e?.status||'—'),'Situação da coleta']
    ].map(([n,l])=>`<div class="stat-card"><span class="stat-number">${esc(n)}</span><span class="stat-label">${esc(l)}</span></div>`).join('');
    $('highlights').innerHTML = relevant.length ? relevant.slice(0,12).map(renderCard).join('') : '<div class="empty-state">Nenhuma publicação foi classificada como relevante para os temas ativos nesta edição. O acervo completo permanece disponível abaixo.</div>';
    $('highlight-count').textContent = `${relevant.length} publicação(ões) relevantes nesta edição`;
    const others = currentPubs.filter(p=>!isRelevant(p));
    const grouped = others.reduce((acc,p)=>{const k=p.kind||'Outras publicações';(acc[k]=acc[k]||[]).push(p);return acc;},{});
    const groups = Object.entries(grouped).sort((a,b)=>b[1].length-a[1].length);
    $('other-publications').innerHTML = groups.length ? groups.map(([kind,items])=>`<section class="other-group"><div class="other-group-head"><span>${esc(kind)}</span><span>${items.length}</span></div>${items.slice(0,8).map(p=>`<div class="other-item"><div><strong>${esc(p.title)}</strong><small>${esc(pageText(p))} · ${esc(p.organization||'Órgão não identificado')}</small></div><button data-open-pub="${esc(p.id)}">Abrir →</button></div>`).join('')}${items.length>8?`<div class="other-item"><small>Mais ${items.length-8} publicação(ões) nesta categoria.</small><button data-go="publications">Ver tudo →</button></div>`:''}</section>`).join('') : '<div class="empty-state">Não há outras publicações nesta edição.</div>';
  }
  async function loadPublications(append = false) {
    if (!append) publicationOffset = 0;
    const query = $('publication-query').value.trim();
    const status = $('publication-filter').value;
    const edition = $('publication-date').value;
    let req = client.from('publications').select('*, editions(edition_date,edition_label)').order('created_at',{ascending:false}).range(publicationOffset, publicationOffset + publicationPageSize - 1);
    if (edition !== 'all') req = req.eq('edition_id', edition);
    if (query) req = req.textSearch('search_vector', query, {type:'websearch',config:'portuguese'});
    const {data,error} = await req;
    if (error) {toast(`Falha na pesquisa: ${error.message}`);return;}
    const page = data || [];
    publicationOffset += page.length;
    let items = page;
    if (status==='relevant') items = items.filter(isRelevant);
    if (status==='other') items = items.filter(p=>!isRelevant(p));
    if (selectedTheme) items = items.filter(p=>(p.matched_themes||[]).includes(selectedTheme));
    const html = items.map(p=>`<article class="publication-row"><div class="pub-date">${esc(fmtDate(p.editions?.edition_date))}<br>${esc(pageText(p))}</div><div><h3>${esc(p.title)}</h3><p>${esc((p.summary || p.body_text || '').slice(0,280))}${(p.summary||p.body_text||'').length>280?'…':''}</p><div class="pub-meta"><span class="tag ${isRelevant(p)?'green':'neutral'}">${isRelevant(p)?'Relevante':'Demais atos'}</span>${publicationTags(p)}<span class="tiny muted">${esc(p.kind||'')}</span></div></div><button class="text-button" data-open-pub="${esc(p.id)}">Abrir →</button></article>`).join('');
    if (append) $('publication-results').insertAdjacentHTML('beforeend', html);
    else $('publication-results').innerHTML = html || '<div class="empty-state">Nenhuma publicação encontrada com esses filtros.</div>';
    $('pub-load-more').classList.toggle('hidden', page.length < publicationPageSize);
  }
  function renderThemes() {
    $('theme-list').innerHTML = themes.length ? themes.map(t=>`<article class="theme-item"><div><h3>${esc(t.name)} ${t.active?'<span class="tag green">Ativo</span>':'<span class="tag neutral">Inativo</span>'}</h3><p>${esc(t.description||'Sem descrição')} · Prioridade ${t.priority||3}</p><p>Palavras-chave: ${esc((t.keywords||[]).join(', ')||'não cadastradas')}</p></div><div class="theme-actions"><button data-edit-theme="${esc(t.id)}">Editar</button><button data-toggle-theme="${esc(t.id)}">${t.active?'Desativar':'Ativar'}</button></div></article>`).join('') : '<div class="empty-state">Cadastre o primeiro tema que o Radar deverá monitorar.</div>';
  }
  function clearThemeForm() {
    $('theme-form').reset(); $('theme-id').value=''; $('theme-priority').value='3'; $('theme-active').checked=true; $('theme-cancel').classList.add('hidden');
  }
  async function saveTheme(event) {
    event.preventDefault();
    const id = $('theme-id').value;
    const payload = {name:$('theme-name').value.trim(),description:$('theme-description').value.trim(),keywords:$('theme-keywords').value.split(',').map(x=>x.trim()).filter(Boolean),include_criteria:$('theme-include').value.trim(),exclude_criteria:$('theme-exclude').value.trim(),priority:Number($('theme-priority').value),active:$('theme-active').checked};
    if (!payload.name) return;
    const result = id ? await client.from('themes').update(payload).eq('id',id) : await client.from('themes').insert(payload);
    if (result.error) {toast(`Não foi possível salvar o tema: ${result.error.message}`);return;}
    const refreshed = await client.from('themes').select('*').order('priority');
    themes = refreshed.data || []; clearThemeForm(); renderThemes(); renderTopicRow(); toast('Tema salvo. Ele será considerado na próxima coleta automática.');
  }
  async function editTheme(id) {
    const t=themes.find(x=>x.id===id); if(!t)return;
    $('theme-id').value=t.id;$('theme-name').value=t.name;$('theme-description').value=t.description||'';$('theme-keywords').value=(t.keywords||[]).join(', ');$('theme-include').value=t.include_criteria||'';$('theme-exclude').value=t.exclude_criteria||'';$('theme-priority').value=String(t.priority||3);$('theme-active').checked=!!t.active;$('theme-cancel').classList.remove('hidden');
    window.scrollTo({top:0,behavior:'smooth'});
  }
  async function toggleTheme(id) {
    const t=themes.find(x=>x.id===id); if(!t)return;
    const {error}=await client.from('themes').update({active:!t.active}).eq('id',id);
    if(error){toast(error.message);return;}
    t.active=!t.active;renderThemes();renderTopicRow();toast(`Tema ${t.active?'ativado':'desativado'}.`);
  }
  async function openPublication(id) {
    const {data,error}=await client.from('publications').select('*, editions(edition_date,edition_label,source_pdf_url)').eq('id',id).single();
    if(error){toast(error.message);return;}
    const p=data; const rel=isRelevant(p); const editUrl=safeUrl(p.editions?.source_pdf_url||p.source_url);
    $('modal-content').innerHTML=`<div class="section-kicker">${esc(p.kind||'PUBLICAÇÃO')} · ${esc(fmtDate(p.editions?.edition_date))}</div><h2 id="modal-title">${esc(p.title)}</h2><p class="muted tiny">${esc(pageText(p))} · ${esc(p.organization||'Órgão não identificado')}</p><div class="pub-meta">${rel?'<span class="tag green">Relevante</span>':'<span class="tag neutral">Demais atos</span>'}${publicationTags(p)}</div>${p.summary?`<div class="why-box"><strong>Resumo</strong>${esc(p.summary)}</div>`:''}${p.justification?`<div class="why-box"><strong>Por que importa</strong>${esc(p.justification)}</div>`:''}<h3 style="font-size:12px;margin:20px 0 8px">Texto da publicação</h3><div class="full-text">${esc(p.body_text)}</div><p style="margin-top:14px"><a class="btn btn-outline" href="${editUrl}" target="_blank" rel="noopener">Abrir / baixar Diário oficial ↗</a></p><div class="card-actions"><button data-review="${esc(p.id)}">Revisar classificação</button><span class="muted tiny">Classificação automática: ${p.ai_relevant?'relevante':'não relevante'}</span></div>`;
    $('publication-modal').classList.remove('hidden');
  }
  async function reviewPublication(id) {
    const selected=window.prompt('Classificação final: digite R para relevante ou I para irrelevante. Deixe em branco para cancelar.');
    if(!selected)return;
    const normalized=selected.trim().toUpperCase();
    if(!['R','I'].includes(normalized)){toast('Digite R ou I.');return;}
    const note=window.prompt('Observação da revisão (opcional):')||'';
    const {error}=await client.from('publications').update({manual_relevance:normalized==='R',manual_note:note,reviewed_by:currentUser.id,reviewed_at:new Date().toISOString()}).eq('id',id);
    if(error){toast(`Falha ao salvar revisão: ${error.message}`);return;}
    $('publication-modal').classList.add('hidden'); await loadDashboard(); toast('Revisão humana registrada.');
  }
  async function runResearch(event) {
    event.preventDefault();
    const question=$('research-question').value.trim(); if(!question)return;
    const btn=$('research-submit');btn.disabled=true;btn.textContent='Pesquisando…';$('research-answer').classList.remove('hidden');$('research-answer').innerHTML='<p class="muted">Consultando publicações e preparando a resposta com referências...</p>';
    try{
      const {data,error}=await client.functions.invoke('ask-radar',{body:{question}});
      if(error)throw error;
      if(data.error)throw new Error(data.error);
      const sources=data.sources||[];
      $('research-answer').innerHTML=`<h3>Resultado da pesquisa</h3><div class="answer-text">${esc(data.answer||'Não foi possível elaborar uma resposta.')}</div><div class="source-list"><h4>Fontes consultadas</h4>${sources.map(s=>`<a class="source-link" href="${safeUrl(s.sourceUrl)}" target="_blank" rel="noopener">[${s.ref}] ${esc(s.title)} · ${esc(fmtDate(s.editionDate))} · ${esc(s.pageStart?`p. ${s.pageStart}`:'página não identificada')} ↗</a>`).join('')}</div>`;
    }catch(e){$('research-answer').innerHTML=`<p class="error">Não foi possível concluir a pesquisa: ${esc(e.message||String(e))}</p>`;}
    finally{btn.disabled=false;btn.textContent='Pesquisar no acervo';}
  }
  function renderHistory(lastRun = latestRun) {
    $('edition-history').innerHTML = editions.length ? editions.map(e=>`<div class="history-item"><strong>${esc(fmtDate(e.edition_date))}</strong><div><small>${esc(e.edition_label||'Parte II · Poder Legislativo')} · ${Number(e.publication_count||0)} atos · ${Number(e.relevant_count||0)} destaques</small></div><span class="status-pill ${e.status==='error'?'error':''}">${esc(e.status==='processed'?'Processada':e.status==='error'?'Erro':'Pendente')}</span><a href="${safeUrl(e.source_pdf_url||e.source_page_url)}" target="_blank" rel="noopener">Abrir Diário ↗</a></div>`).join('') : '<div class="empty-state">Nenhuma edição cadastrada.</div>';
    const run=lastRun;
    $('run-status').textContent=run?`Última execução: ${fmtDate(run.started_at,{day:'2-digit',month:'long',year:'numeric',hour:'2-digit',minute:'2-digit'})} · ${run.status} · ${run.message||''}`:'Ainda não há execuções registradas.';
    const date=$('publication-date');
    if(date && date.options.length<=1) date.innerHTML='<option value="all">Todas as edições</option>'+editions.map(e=>`<option value="${esc(e.id)}">${esc(fmtDate(e.edition_date))}</option>`).join('');
  }

  document.addEventListener('click', async (event) => {
    const tab=event.target.closest('[data-view]'); if(tab){switchView(tab.dataset.view);return;}
    const ed=event.target.closest('[data-edition]'); if(ed){await selectEdition(ed.dataset.edition);return;}
    const open=event.target.closest('[data-open-pub]'); if(open){await openPublication(open.dataset.openPub);return;}
    const rev=event.target.closest('[data-review]'); if(rev){await reviewPublication(rev.dataset.review);return;}
    const edit=event.target.closest('[data-edit-theme]'); if(edit){editTheme(edit.dataset.editTheme);return;}
    const toggle=event.target.closest('[data-toggle-theme]'); if(toggle){await toggleTheme(toggle.dataset.toggleTheme);return;}
    const topic=event.target.closest('[data-topic-filter]'); if(topic){selectedTheme=topic.dataset.topicFilter;$('publication-filter').value='all';$('publication-query').value='';switchView('publications');return;}
    const go=event.target.closest('[data-go]'); if(go){switchView(go.dataset.go);return;}
    if(event.target.closest('[data-close-modal]')){$('publication-modal').classList.add('hidden');return;}
  });
  $('login-form').addEventListener('submit', login);
  $('logout-btn').addEventListener('click', async()=>{await client.auth.signOut();});
  $('global-search-btn').addEventListener('click',()=>{$('publication-query').value=$('global-search').value;selectedTheme='';$('publication-filter').value='all';$('publication-date').value='all';switchView('publications');});
  $('global-search').addEventListener('keydown',e=>{if(e.key==='Enter')$('global-search-btn').click();});
  $('publication-search-btn').addEventListener('click',()=>loadPublications(false));
  $('pub-load-more').addEventListener('click',()=>loadPublications(true));
  $('publication-query').addEventListener('keydown',e=>{if(e.key==='Enter')loadPublications(false);});
  $('theme-form').addEventListener('submit',saveTheme);
  $('theme-cancel').addEventListener('click',clearThemeForm);
  $('research-form').addEventListener('submit',runResearch);
  init();
})();
