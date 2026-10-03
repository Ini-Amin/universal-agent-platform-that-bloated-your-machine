// Search surface. It works without any server configuration:
//   - "Search the web" opens the chosen engine in a new tab. Search engines send
//     X-Frame-Options, so their results can never be shown inside a card.
//   - "Search knowledge" filters the verified knowledge items the server holds.
// The status line says whether the server has an MCP search tool, which is what
// research runs use for real web evidence (see README, "Real search & fetch providers").
//
// Data access is injected (`deps`) so this module stays free of fetch/state.

export const SEARCH_ENGINES = [
  { id: 'duckduckgo', label: 'DuckDuckGo', url: 'https://duckduckgo.com/?q=' },
  { id: 'google', label: 'Google', url: 'https://www.google.com/search?q=' },
  { id: 'bing', label: 'Bing', url: 'https://www.bing.com/search?q=' },
];

const ENGINE_KEY = 'uap.search.engine';
const MAX_RESULTS = 50;

export function searchUrl(engineId, query) {
  const engine = SEARCH_ENGINES.find((e) => e.id === engineId) || SEARCH_ENGINES[0];
  return engine.url + encodeURIComponent(String(query || '').trim());
}

/** Case-insensitive match of every word in `query` against statement/domain/tags. */
export function filterKnowledge(items, query) {
  const words = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
  const list = Array.isArray(items) ? items : [];
  if (words.length === 0) return list;
  return list.filter((item) => {
    const hay = [item && item.statement, item && item.domain, ...((item && item.tags) || [])]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function readEngine() {
  try {
    const saved = localStorage.getItem(ENGINE_KEY);
    if (SEARCH_ENGINES.some((e) => e.id === saved)) return saved;
  } catch (_e) { /* storage unavailable */ }
  return SEARCH_ENGINES[0].id;
}

function saveEngine(id) {
  try { localStorage.setItem(ENGINE_KEY, id); } catch (_e) { /* ignore */ }
}

/**
 * Fill `body` with the search UI.
 * deps: { getKnowledge(): Promise<item[]>, getTools(): Promise<{name}[]>, openKnowledge?(item) }
 */
export function buildSearchBody(body, spec, deps = {}) {
  body.classList.add('stage-search');

  const form = el('form', 'stage-search-form');
  form.setAttribute('role', 'search');

  const input = el('input', 'stage-search-input');
  input.type = 'search';
  input.placeholder = 'Search the web or your knowledge…';
  input.setAttribute('aria-label', 'Search query');
  input.autocomplete = 'off';

  const select = el('select', 'stage-search-engine');
  select.setAttribute('aria-label', 'Search engine');
  for (const e of SEARCH_ENGINES) {
    const opt = el('option', '', e.label);
    opt.value = e.id;
    select.appendChild(opt);
  }
  select.value = readEngine();
  select.addEventListener('change', () => saveEngine(select.value));

  const btnWeb = el('button', 'btn btn-accent btn-sm stage-search-web', 'Search the web ↗');
  btnWeb.type = 'submit';
  btnWeb.title = 'Opens the results in a new tab';

  const btnKnowledge = el('button', 'btn btn-sm stage-search-knowledge', 'Search knowledge');
  btnKnowledge.type = 'button';

  const row = el('div', 'stage-search-row');
  row.append(input, select);
  const actions = el('div', 'stage-search-actions');
  actions.append(btnWeb, btnKnowledge);
  form.append(row, actions);

  const message = el('div', 'stage-search-message');
  message.setAttribute('role', 'status');
  const results = el('ul', 'stage-search-results');
  results.setAttribute('aria-live', 'polite');
  const status = el('div', 'stage-search-status');

  body.append(form, message, results, status);

  function setMessage(text) {
    message.textContent = text || '';
  }

  function query() {
    return input.value.trim();
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (!query()) {
      setMessage('Type something to search for.');
      input.focus();
      return;
    }
    setMessage('');
    // An anchor click inside the user's gesture is not treated as a pop-up.
    const a = document.createElement('a');
    a.href = searchUrl(select.value, query());
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.click();
  });

  btnKnowledge.addEventListener('click', async () => {
    const q = query();
    results.textContent = '';
    if (typeof deps.getKnowledge !== 'function') {
      setMessage('Knowledge search is not available here.');
      return;
    }
    setMessage('Searching knowledge…');
    btnKnowledge.disabled = true;
    try {
      const all = await deps.getKnowledge();
      const hits = filterKnowledge(all, q);
      if (!Array.isArray(all) || all.length === 0) {
        setMessage('No verified knowledge yet. Research runs add items here once they are verified.');
        return;
      }
      if (hits.length === 0) {
        setMessage(`No knowledge matches “${q}”.`);
        return;
      }
      setMessage(hits.length > MAX_RESULTS
        ? `${hits.length} matches — showing the first ${MAX_RESULTS}.`
        : `${hits.length} ${hits.length === 1 ? 'match' : 'matches'}.`);
      for (const item of hits.slice(0, MAX_RESULTS)) {
        const li = el('li', 'stage-search-result');
        const btn = el('button', 'stage-search-result-btn');
        btn.type = 'button';
        btn.appendChild(el('span', 'stage-search-result-text', item.statement || '(no statement)'));
        if (item.domain) btn.appendChild(el('span', 'stage-search-result-badge', item.domain));
        btn.addEventListener('click', () => {
          if (typeof deps.openKnowledge === 'function') deps.openKnowledge(item);
        });
        li.appendChild(btn);
        results.appendChild(li);
      }
    } catch (err) {
      setMessage(`Could not load knowledge: ${(err && err.message) || err}`);
    } finally {
      btnKnowledge.disabled = false;
    }
  });

  // Status of in-app web search: does the server have an MCP search tool?
  if (typeof deps.getTools === 'function') {
    Promise.resolve(deps.getTools()).then((tools) => {
      const found = (Array.isArray(tools) ? tools : [])
        .map((t) => String((t && t.name) || ''))
        .find((n) => /search/i.test(n));
      status.textContent = found
        ? `Research runs use “${found}” for live web evidence.`
        : 'No search provider is set up, so research runs use clearly labelled sample evidence. '
          + 'To enable live web research, register an MCP search tool (UAP_MCP_ENABLED=1) or set UAP_SEARCH_URL — see the README, “Real search & fetch providers”.';
    }).catch(() => { /* status is optional */ });
  }

  return body;
}
