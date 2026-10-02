// Stage — the centre pane hosts real tool views, not only the node graph.
//
// A "view" is one card: a title bar, a close button, and a content area that
// renders one of the supported kinds. The stage is a pure host: it owns no
// domain state and never fetches. Callers hand it a spec; it renders it.
//
//   createStage(containerEl) -> { showView, closeView, listViews, clear, onEvent }
//
// Every kind is handled explicitly. An unknown kind is rendered as a
// `placeholder` that names the kind — a view never fails silently.

const KIND_TITLES = {
  html: 'HTML',
  markdown: 'Markdown',
  image: 'Image',
  video: 'Video',
  iframe: 'Embedded page',
  code: 'Code',
  whiteboard: 'Whiteboard',
  placeholder: 'View',
};

// Verified by loading it in a real browser before wiring it here: the page
// returns two <canvas> elements (static + interactive) and sends no
// X-Frame-Options / CSP frame-ancestors, so it renders inside an <iframe>.
export const EXCALIDRAW_URL = 'https://excalidraw.com/';

const DEFAULT_IFRAME_SANDBOX =
  'allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals';

export function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str).replace(/[&<>"']/g, (m) => {
    switch (m) {
      case '&': return '&amp;';
      case '<': return '&lt;';
      case '>': return '&gt;';
      case '"': return '&quot;';
      case "'": return '&#39;';
      default: return m;
    }
  });
}

// ---------------------------------------------------------------------------
// Markdown — a small, dependency-free renderer. Not CommonMark-complete; it
// covers the constructs a tool view actually emits (headings, emphasis, code,
// lists, quotes, links, images, rules). Everything is HTML-escaped first, so a
// document can never inject markup through its text.
// ---------------------------------------------------------------------------
export function renderMarkdown(markdown) {
  const src = String(markdown === null || markdown === undefined ? '' : markdown)
    .replace(/\r\n?/g, '\n');
  const lines = src.split('\n');

  function inline(text) {
    let s = escapeHtml(text);
    s = s.replace(/`([^`]+)`/g, (_m, c) => `<code>${c}</code>`);
    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_m, alt, url) => `<img src="${url}" alt="${alt}" class="md-img">`);
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, txt, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${txt}</a>`);
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    s = s.replace(/(^|[^_\w])_([^_\n]+)_/g, '$1<em>$2</em>');
    s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    return s;
  }

  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    const fence = line.match(/^\s*```\s*(\S*)\s*$/);
    if (fence) {
      const lang = fence[1] || '';
      const buf = [];
      i += 1;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i += 1; }
      i += 1; // consume closing fence (or run off the end)
      out.push(`<pre class="md-pre"><code class="md-code" data-lang="${escapeHtml(lang)}">${escapeHtml(buf.join('\n'))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }

    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { out.push('<hr>'); i += 1; continue; }

    if (/^\s*>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ''));
        i += 1;
      }
      out.push(`<blockquote>${renderMarkdown(buf.join('\n'))}</blockquote>`);
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*[-*+]\s+/, ''));
        i += 1;
      }
      out.push('<ul>' + buf.map((x) => `<li>${inline(x)}</li>`).join('') + '</ul>');
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*\d+\.\s+/, ''));
        i += 1;
      }
      out.push('<ol>' + buf.map((x) => `<li>${inline(x)}</li>`).join('') + '</ol>');
      continue;
    }

    if (/^\s*$/.test(line)) { i += 1; continue; }

    const para = [line];
    i += 1;
    while (
      i < lines.length &&
      !/^\s*$/.test(lines[i]) &&
      !/^(#{1,6}\s|```|\s*[-*+]\s|\s*\d+\.\s|\s*>)/.test(lines[i])
    ) {
      para.push(lines[i]);
      i += 1;
    }
    out.push(`<p>${inline(para.join(' '))}</p>`);
  }
  return out.join('\n');
}

// ---------------------------------------------------------------------------

export function createStage(containerEl) {
  if (!containerEl) throw new Error('createStage: a container element is required');

  containerEl.classList.add('stage-root');
  const grid = document.createElement('div');
  grid.className = 'stage-grid';
  containerEl.appendChild(grid);

  const views = new Map(); // id -> { spec, cardEl }
  const listeners = new Set();
  let seq = 0;

  function emit(event) {
    for (const cb of listeners) {
      try { cb(event); } catch (err) { console.error('stage listener error:', err); }
    }
  }

  function nextId() {
    seq += 1;
    return `view-${seq}-${Math.random().toString(36).slice(2, 8)}`;
  }

  function titleFor(spec) {
    if (spec.title) return String(spec.title);
    if (spec.kind === 'placeholder') return 'View';
    return KIND_TITLES[spec.kind] || String(spec.kind || 'View');
  }

  // --- per-kind body builders -------------------------------------------

  function buildBody(spec) {
    const body = document.createElement('div');
    body.className = 'stage-card-body';

    switch (spec.kind) {
      case 'html': {
        if (typeof spec.html !== 'string') return missing(body, 'html', '`html` (string)');
        // Trusted fragment: the caller owns it, so it is inserted as markup.
        body.classList.add('stage-html');
        body.innerHTML = spec.html;
        return body;
      }

      case 'markdown': {
        const text = spec.markdown !== undefined ? spec.markdown : spec.text;
        if (typeof text !== 'string') return missing(body, 'markdown', '`markdown` (string)');
        body.classList.add('stage-markdown');
        body.innerHTML = renderMarkdown(text);
        return body;
      }

      case 'image': {
        if (!spec.url) return missing(body, 'image', '`url`');
        const fig = document.createElement('figure');
        fig.className = 'stage-figure';
        const img = document.createElement('img');
        img.className = 'stage-image';
        img.src = spec.url;
        img.alt = spec.alt || spec.title || '';
        img.loading = 'lazy';
        img.addEventListener('error', () => {
          fig.innerHTML = `<div class="stage-media-error">Image failed to load: ${escapeHtml(spec.url)}</div>`;
          emit({ type: 'error', id: spec.__id, kind: 'image', message: 'image failed to load' });
        });
        fig.appendChild(img);
        if (spec.caption) {
          const cap = document.createElement('figcaption');
          cap.className = 'stage-caption';
          cap.textContent = spec.caption;
          fig.appendChild(cap);
        }
        body.appendChild(fig);
        return body;
      }

      case 'video': {
        if (!spec.url) return missing(body, 'video', '`url`');
        const fig = document.createElement('figure');
        fig.className = 'stage-figure';
        const video = document.createElement('video');
        video.className = 'stage-video';
        video.controls = true;
        video.setAttribute('controls', '');
        video.preload = 'metadata';
        if (spec.poster) video.poster = spec.poster;
        video.src = spec.url;
        video.addEventListener('error', () => {
          emit({ type: 'error', id: spec.__id, kind: 'video', message: 'video failed to load' });
        });
        fig.appendChild(video);
        if (spec.caption) {
          const cap = document.createElement('figcaption');
          cap.className = 'stage-caption';
          cap.textContent = spec.caption;
          fig.appendChild(cap);
        }
        body.appendChild(fig);
        return body;
      }

      case 'iframe': {
        if (!spec.url) return missing(body, 'iframe', '`url`');
        const frame = document.createElement('iframe');
        frame.className = 'stage-iframe';
        frame.src = spec.url;
        frame.title = spec.title || 'Embedded view';
        frame.setAttribute('loading', 'lazy');
        frame.setAttribute('referrerpolicy', 'no-referrer');
        if (spec.sandbox === false) {
          // explicit opt-out: no sandbox attribute at all
        } else if (typeof spec.sandbox === 'string') {
          frame.setAttribute('sandbox', spec.sandbox);
        } else if (Array.isArray(spec.sandbox)) {
          frame.setAttribute('sandbox', spec.sandbox.join(' '));
        } else {
          frame.setAttribute('sandbox', DEFAULT_IFRAME_SANDBOX);
        }
        if (spec.allow) frame.setAttribute('allow', spec.allow);
        body.appendChild(frame);
        return body;
      }

      case 'code': {
        const text = spec.text !== undefined ? spec.text : spec.code;
        if (typeof text !== 'string') return missing(body, 'code', '`text` (string)');
        const wrap = document.createElement('div');
        wrap.className = 'stage-code-wrap';
        const bar = document.createElement('div');
        bar.className = 'stage-code-bar';
        const lang = document.createElement('span');
        lang.className = 'stage-code-lang';
        lang.textContent = spec.language || 'text';
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.className = 'stage-code-copy';
        copy.textContent = 'Copy';
        copy.addEventListener('click', async () => {
          const ok = await copyText(text);
          copy.textContent = ok ? 'Copied' : 'Copy failed';
          emit({ type: ok ? 'copy' : 'error', id: spec.__id, kind: 'code', message: ok ? 'copied' : 'copy failed' });
          setTimeout(() => { copy.textContent = 'Copy'; }, 1500);
        });
        bar.appendChild(lang);
        bar.appendChild(copy);
        const pre = document.createElement('pre');
        pre.className = 'stage-pre';
        const codeEl = document.createElement('code');
        codeEl.className = 'stage-code';
        codeEl.dataset.lang = spec.language || 'text';
        codeEl.textContent = text;
        pre.appendChild(codeEl);
        wrap.appendChild(bar);
        wrap.appendChild(pre);
        body.appendChild(wrap);
        return body;
      }

      case 'whiteboard': {
        const frame = document.createElement('iframe');
        frame.className = 'stage-iframe stage-whiteboard';
        frame.src = spec.url || EXCALIDRAW_URL;
        frame.title = spec.title || 'Excalidraw whiteboard';
        frame.setAttribute('allow', 'clipboard-read; clipboard-write; fullscreen');
        frame.setAttribute('referrerpolicy', 'no-referrer');
        body.appendChild(frame);
        return body;
      }

      case 'placeholder':
      default: {
        const box = document.createElement('div');
        box.className = 'stage-placeholder';
        const unknown = spec.kind && spec.kind !== 'placeholder';
        box.innerHTML = `
          <div class="stage-placeholder-icon">${unknown ? '?' : '◻'}</div>
          <div class="stage-placeholder-title">${escapeHtml(titleFor(spec))}</div>
          <div class="stage-placeholder-message">${escapeHtml(
            spec.message ||
              (unknown
                ? `Unsupported view kind "${spec.kind}". Nothing was rendered — this placeholder stands in its place.`
                : 'No content for this view yet.')
          )}</div>
        `;
        body.appendChild(box);
        return body;
      }
    }
  }

  function missing(body, kind, field) {
    const box = document.createElement('div');
    box.className = 'stage-placeholder stage-placeholder-error';
    box.innerHTML = `
      <div class="stage-placeholder-icon">!</div>
      <div class="stage-placeholder-title">${escapeHtml(KIND_TITLES[kind] || kind)} view has no content</div>
      <div class="stage-placeholder-message">A "${escapeHtml(kind)}" view needs ${escapeHtml(field)}.</div>
    `;
    body.appendChild(box);
    return body;
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_e) { /* fall through to legacy path */ }
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch (_e) {
      return false;
    }
  }

  function buildCard(spec) {
    const card = document.createElement('section');
    card.className = 'stage-card';
    card.dataset.viewId = spec.__id;
    card.dataset.kind = spec.kind || 'placeholder';

    const titlebar = document.createElement('header');
    titlebar.className = 'stage-card-titlebar';

    const kindBadge = document.createElement('span');
    kindBadge.className = 'stage-card-kind';
    kindBadge.textContent = spec.kind || 'placeholder';

    const title = document.createElement('span');
    title.className = 'stage-card-title';
    title.textContent = titleFor(spec);

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'stage-card-close';
    close.setAttribute('aria-label', 'Close view');
    close.title = 'Close view';
    close.textContent = '\u00D7';
    close.addEventListener('click', () => closeView(spec.__id));

    titlebar.appendChild(kindBadge);
    titlebar.appendChild(title);
    titlebar.appendChild(close);

    card.appendChild(titlebar);
    card.appendChild(buildBody(spec));
    return card;
  }

  function normalize(spec) {
    const s = { ...(spec || {}) };
    if (!s.kind) s.kind = 'placeholder';
    if (s.id === undefined || s.id === null || s.id === '') s.id = nextId();
    s.__id = String(s.id);
    return s;
  }

  function showView(spec) {
    const s = normalize(spec);
    const existing = views.get(s.__id);
    if (existing) {
      const replacement = buildCard(s);
      existing.cardEl.replaceWith(replacement);
      views.set(s.__id, { spec: s, cardEl: replacement });
      emit({ type: 'update', id: s.__id, kind: s.kind, spec: s });
      return s.__id;
    }
    const card = buildCard(s);
    grid.appendChild(card);
    views.set(s.__id, { spec: s, cardEl: card });
    emit({ type: 'open', id: s.__id, kind: s.kind, spec: s });
    return s.__id;
  }

  function closeView(id) {
    const entry = views.get(String(id));
    if (!entry) return false;
    entry.cardEl.remove();
    views.delete(String(id));
    emit({ type: 'close', id: String(id) });
    return true;
  }

  function listViews() {
    return Array.from(views.values()).map(({ spec }) => ({
      id: spec.__id,
      kind: spec.kind,
      title: titleFor(spec),
    }));
  }

  function clear() {
    const ids = Array.from(views.keys());
    for (const id of ids) {
      const entry = views.get(id);
      if (entry) entry.cardEl.remove();
    }
    views.clear();
    emit({ type: 'clear', ids });
    return ids.length;
  }

  function onEvent(cb) {
    if (typeof cb !== 'function') return () => {};
    listeners.add(cb);
    return () => listeners.delete(cb);
  }

  return { showView, closeView, listViews, clear, onEvent };
}

export default createStage;
