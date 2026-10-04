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
  search: 'Search',
  code: 'Code',
  whiteboard: 'Whiteboard',
  terminal: 'Terminal',
  editor: 'Editor',
  handoff: 'Agent Handoff',
  placeholder: 'View',
};

// Verified by loading it in a real browser before wiring it here: the page
// returns two <canvas> elements (static + interactive) and sends no
// X-Frame-Options / CSP frame-ancestors, so it renders inside an <iframe>.
export const EXCALIDRAW_URL = 'https://excalidraw.com/';

const DEFAULT_IFRAME_SANDBOX =
  'allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals';

// Hosts that send X-Frame-Options or CSP frame-ancestors on their pages, so a
// browser shows "refused to connect" instead of an embedded copy. Checked with
// curl on 2026-10-03; a heuristic only, so the card offers "Try embedding anyway".
const FRAME_BLOCKING_HOSTS = [
  'google.com', 'bing.com', 'duckduckgo.com', 'github.com', 'x.com', 'twitter.com',
  'facebook.com', 'instagram.com', 'linkedin.com', 'reddit.com', 'youtube.com',
  'stackoverflow.com', 'amazon.com', 'medium.com', 'npmjs.com', 'pypi.org',
  'developer.mozilla.org', 'openai.com',
];

// Pages on those hosts that are made for embedding.
const FRAME_FRIENDLY_PATHS = [
  { host: 'youtube.com', prefix: '/embed/' },
  { host: 'google.com', prefix: '/maps/embed' },
];

/** The blocking host name for an http(s) URL that browsers usually refuse to frame, else null. */
export function framingBlockedHost(url) {
  let u;
  try {
    u = new URL(url, 'http://localhost/');
  } catch (_e) {
    return null;
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
  const host = u.hostname.toLowerCase().replace(/^www\./, '');
  const hit = FRAME_BLOCKING_HOSTS.find((h) => host === h || host.endsWith(`.${h}`));
  if (!hit) return null;
  if (FRAME_FRIENDLY_PATHS.some((p) => p.host === hit && u.pathname.startsWith(p.prefix))) return null;
  return hit;
}

/** Absolute http(s) form of a card URL, or '' when it cannot be opened in a tab. */
function openableUrl(url) {
  try {
    const base = (typeof window !== 'undefined' && window.location && window.location.href) || 'http://localhost/';
    const u = new URL(url, base);
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : '';
  } catch (_e) {
    return '';
  }
}

// Per-kind body builders supplied by the host page (so this module never fetches).
const _viewBuilders = new Map();
export function registerViewBuilder(kind, fn) {
  if (typeof fn === 'function') _viewBuilders.set(kind, fn);
  else _viewBuilders.delete(kind);
}

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

// ---------------------------------------------------------------------------
// [StageTools] terminal + editor helpers
// ---------------------------------------------------------------------------

export function ansiToHtml(str) {
  if (!str) return '';

  const fgColors = {
    30: '#484f58', 31: '#ff7b72', 32: '#6fc2b8', 33: '#d29922',
    34: '#dd7fd3', 35: '#bc8cff', 36: '#39c5cf', 37: '#d1d5db',
    90: '#9aa1b4', 91: '#ffa198', 92: '#56d364', 93: '#e3b341',
    94: '#e59de0', 95: '#d2a8ff', 96: '#56d4dd', 97: '#f0f6fc',
  };
  const bgColors = {
    40: '#1e222c', 41: '#4c1517', 42: '#144620', 43: '#4d3800',
    44: '#0c2d6b', 45: '#3c1e6e', 46: '#154f57', 47: '#9aa1b4',
    100: '#212530', 101: '#6e1d24', 102: '#1b632d', 103: '#6e5100',
    104: '#114299', 105: '#54299c', 106: '#1f6f7a', 107: '#b1bac4',
  };

  let openSpan = false;
  let currentFg = null;
  let currentBg = null;
  let isBold = false;
  let isDim = false;
  let isItalic = false;
  let isUnderline = false;

  function makeSpan() {
    const styles = [];
    if (currentFg) styles.push(`color:${currentFg}`);
    if (currentBg) styles.push(`background-color:${currentBg}`);
    if (isBold) styles.push('font-weight:600');
    if (isDim) styles.push('opacity:0.7');
    if (isItalic) styles.push('font-style:italic');
    if (isUnderline) styles.push('text-decoration:underline');
    if (styles.length === 0) return '';
    openSpan = true;
    return `<span style="${styles.join(';')}">`;
  }

  const regex = /\x1b\[([0-9;]*)([a-zA-Z])/g;
  let lastIndex = 0;
  let match;
  let html = '';

  while ((match = regex.exec(str)) !== null) {
    const textBefore = str.slice(lastIndex, match.index);
    if (textBefore) html += escapeHtml(textBefore);
    lastIndex = regex.lastIndex;

    const code = match[2];
    const params = match[1] ? match[1].split(';').map(Number) : [0];

    if (code === 'm') {
      if (openSpan) {
        html += '</span>';
        openSpan = false;
      }
      for (let i = 0; i < params.length; i++) {
        const p = params[i];
        if (p === 0) {
          currentFg = null;
          currentBg = null;
          isBold = false;
          isDim = false;
          isItalic = false;
          isUnderline = false;
        } else if (p === 1) {
          isBold = true;
        } else if (p === 2) {
          isDim = true;
        } else if (p === 3) {
          isItalic = true;
        } else if (p === 4) {
          isUnderline = true;
        } else if (p === 22) {
          isBold = false;
          isDim = false;
        } else if (p === 23) {
          isItalic = false;
        } else if (p === 24) {
          isUnderline = false;
        } else if (fgColors[p]) {
          currentFg = fgColors[p];
        } else if (p === 39) {
          currentFg = null;
        } else if (bgColors[p]) {
          currentBg = bgColors[p];
        } else if (p === 49) {
          currentBg = null;
        } else if (p === 38 && params[i + 1] === 5 && params[i + 2] !== undefined) {
          i += 2;
        } else if (p === 48 && params[i + 1] === 5 && params[i + 2] !== undefined) {
          i += 2;
        }
      }
      html += makeSpan();
    }
  }

  const remaining = str.slice(lastIndex);
  if (remaining) html += escapeHtml(remaining);
  if (openSpan) html += '</span>';

  return html;
}

// Module-level counterpart to the stage's `missing()` helper: buildMarkdownBody
// lives outside createStage, so it cannot reach that closure.
function missingMarkdown(body) {
  const box = document.createElement('div');
  box.className = 'stage-placeholder stage-placeholder-error';
  box.innerHTML = `
    <div class="stage-placeholder-icon">!</div>
    <div class="stage-placeholder-title">${escapeHtml(KIND_TITLES.markdown || 'Markdown')} view has no content</div>
    <div class="stage-placeholder-message">A "${escapeHtml('markdown')}" view needs ${escapeHtml('`markdown` (string)')}.</div>
  `;
  body.appendChild(box);
  return body;
}

export function buildMarkdownBody(body, spec, emit = () => {}) {
  const initialText = spec.markdown !== undefined ? spec.markdown : (spec.text !== undefined ? spec.text : (spec.editable ? '' : undefined));
  if (!spec.editable) {
    if (typeof initialText !== 'string') return missingMarkdown(body);
    body.classList.add('stage-markdown');
    body.innerHTML = renderMarkdown(initialText);
    return body;
  }

  body.classList.add('stage-note-body');
  const container = document.createElement('div');
  container.className = 'stage-note-container';

  const toolbar = document.createElement('div');
  toolbar.className = 'stage-note-toolbar';

  const tabsWrap = document.createElement('div');
  tabsWrap.className = 'stage-note-tabs';

  const editTab = document.createElement('button');
  editTab.type = 'button';
  editTab.className = 'stage-note-tab active';
  editTab.textContent = '✏️ Edit';

  const previewTab = document.createElement('button');
  previewTab.type = 'button';
  previewTab.className = 'stage-note-tab';
  previewTab.textContent = '👁️ Preview';

  tabsWrap.appendChild(editTab);
  tabsWrap.appendChild(previewTab);

  const statusEl = document.createElement('span');
  statusEl.className = 'stage-note-status';
  // Only the host knows whether it keeps this note: index.html saves the ones it
  // opens and marks them `persisted`. A note an agent adds through a canvas
  // command lives only on the page, so it keeps the neutral label.
  statusEl.textContent = spec.persisted === true ? 'Saved in this browser' : 'Markdown Note';

  toolbar.appendChild(tabsWrap);
  toolbar.appendChild(statusEl);

  const textarea = document.createElement('textarea');
  textarea.className = 'stage-note-textarea';
  textarea.placeholder = 'Type your note in Markdown here... (e.g. # Title, - lists, code)';
  textarea.value = typeof initialText === 'string' ? initialText : '';
  textarea.spellcheck = true;
  textarea.setAttribute('aria-label', spec.title || 'Markdown Note');

  const preview = document.createElement('div');
  preview.className = 'stage-note-preview stage-markdown';
  preview.style.display = 'none';

  function updatePreview() {
    preview.innerHTML = renderMarkdown(textarea.value || '*Empty note*');
  }

  editTab.addEventListener('click', () => {
    preview.style.display = 'none';
    textarea.style.display = 'block';
    previewTab.classList.remove('active');
    editTab.classList.add('active');
    textarea.focus();
  });

  previewTab.addEventListener('click', () => {
    updatePreview();
    textarea.style.display = 'none';
    preview.style.display = 'block';
    editTab.classList.remove('active');
    previewTab.classList.add('active');
  });

  textarea.addEventListener('input', () => {
    spec.markdown = textarea.value;
    spec.text = textarea.value;
    emit({ type: 'note_change', id: spec.__id, kind: 'markdown', text: textarea.value });
  });

  container.appendChild(toolbar);
  container.appendChild(textarea);
  container.appendChild(preview);
  body.appendChild(container);
  return body;
}

export function buildTerminalBody(body, spec, emit = () => {}) {
  body.classList.add('stage-terminal-body');

  const container = document.createElement('div');
  container.className = 'stage-terminal-container';

  const toolbar = document.createElement('div');
  toolbar.className = 'stage-terminal-toolbar';

  const statusWrap = document.createElement('div');
  statusWrap.className = 'stage-terminal-status-wrap';

  const dot = document.createElement('span');
  dot.className = 'stage-terminal-dot stage-terminal-dot-connecting';

  const statusLabel = document.createElement('span');
  statusLabel.className = 'stage-terminal-status';
  statusLabel.textContent = 'Connecting...';

  statusWrap.appendChild(dot);
  statusWrap.appendChild(statusLabel);

  const metaWrap = document.createElement('div');
  metaWrap.className = 'stage-terminal-meta';

  const cwdLabel = document.createElement('span');
  cwdLabel.className = 'stage-terminal-cwd';
  cwdLabel.textContent = spec.cwd || '~/agent';

  const clearBtn = document.createElement('button');
  clearBtn.type = 'button';
  clearBtn.className = 'stage-terminal-btn stage-terminal-clear-btn';
  clearBtn.textContent = 'Clear';
  clearBtn.title = 'Clear terminal buffer';

  metaWrap.appendChild(cwdLabel);
  metaWrap.appendChild(clearBtn);

  const tabs = document.createElement('div');
  tabs.className = 'stage-terminal-tabs';
  const tabRun = document.createElement('button');
  tabRun.type = 'button';
  tabRun.className = 'stage-terminal-tab active';
  tabRun.textContent = 'Run output';
  const tabShell = document.createElement('button');
  tabShell.type = 'button';
  tabShell.className = 'stage-terminal-tab';
  tabShell.textContent = 'Shell';
  const tabProblems = document.createElement('button');
  tabProblems.type = 'button';
  tabProblems.className = 'stage-terminal-tab';
  tabProblems.textContent = 'Problems';
  const tabPlus = document.createElement('button');
  tabPlus.type = 'button';
  tabPlus.className = 'stage-terminal-tab stage-terminal-tab-plus';
  tabPlus.textContent = '+';
  tabPlus.title = 'New shell tab';
  tabs.appendChild(tabRun);
  tabs.appendChild(tabShell);
  tabs.appendChild(tabProblems);
  tabs.appendChild(tabPlus);

  toolbar.appendChild(statusWrap);
  toolbar.appendChild(metaWrap);
  const buffer = document.createElement('pre');
  buffer.className = 'stage-terminal-buffer';
  buffer.tabIndex = 0;
  buffer.setAttribute('role', 'region');
  buffer.setAttribute('aria-label', 'Terminal buffer');

  const inputForm = document.createElement('form');
  inputForm.className = 'stage-terminal-input-bar';

  const promptSpan = document.createElement('span');
  promptSpan.className = 'stage-terminal-prompt';
  promptSpan.textContent = spec.prompt || '$';

  const inputEl = document.createElement('input');
  inputEl.type = 'text';
  inputEl.className = 'stage-terminal-input';
  inputEl.placeholder = 'Type shell command...';
  inputEl.autocomplete = 'off';
  inputEl.spellcheck = false;

  const sendBtn = document.createElement('button');
  sendBtn.type = 'submit';
  sendBtn.className = 'stage-terminal-btn stage-terminal-send-btn';
  sendBtn.textContent = 'Send';

  inputForm.appendChild(promptSpan);
  inputForm.appendChild(inputEl);
  inputForm.appendChild(sendBtn);

  const summary = document.createElement('div');
  summary.className = 'stage-terminal-summary';
  const summaryLeft = document.createElement('span');
  summaryLeft.className = 'stage-terminal-summary-left';
  const summaryRight = document.createElement('span');
  summaryRight.className = 'stage-terminal-summary-right';
  // Only a real run reports a result. With no run, the summary is hidden
  // rather than showing the Figma mock's fabricated "Exit code 0 · 42s".
  const hasSummary = Boolean(spec.summaryLeft || spec.summaryRight);
  if (hasSummary) {
    summaryLeft.textContent = spec.summaryLeft || '';
    summaryRight.textContent = spec.summaryRight || '';
  } else {
    summary.style.display = 'none';
  }
  summary.appendChild(summaryLeft);
  summary.appendChild(summaryRight);

  const problemsView = document.createElement('div');
  problemsView.className = 'stage-terminal-problems';
  problemsView.style.display = 'none';
  problemsView.style.padding = '12px 14px';
  problemsView.style.fontSize = '12px';
  problemsView.style.color = 'var(--fg-muted)';
  problemsView.innerHTML = '<span style="color: var(--ok); margin-right: 6px;">✓</span> No problems detected in workspace (0 errors, 0 warnings)';

  function setTerminalTab(name, btn) {
    tabs.querySelectorAll('.stage-terminal-tab').forEach(t => t.classList.remove('active'));
    if (btn) btn.classList.add('active');
    if (name === 'run') {
      buffer.style.display = 'block';
      toolbar.style.display = 'flex';
      inputForm.style.display = 'none';
      problemsView.style.display = 'none';
      summary.style.display = hasSummary ? 'flex' : 'none';
    } else if (name === 'problems') {
      buffer.style.display = 'none';
      toolbar.style.display = 'none';
      inputForm.style.display = 'none';
      problemsView.style.display = 'block';
      summary.style.display = 'none';
    } else {
      buffer.style.display = 'block';
      toolbar.style.display = 'flex';
      inputForm.style.display = 'flex';
      problemsView.style.display = 'none';
      summary.style.display = 'none';
      setTimeout(() => inputEl.focus(), 50);
    }
  }
  tabRun.addEventListener('click', () => setTerminalTab('run', tabRun));
  tabShell.addEventListener('click', () => setTerminalTab('shell', tabShell));
  tabProblems.addEventListener('click', () => setTerminalTab('problems', tabProblems));
  let shellTabNum = 1;
  tabPlus.addEventListener('click', () => {
    shellTabNum += 1;
    const newTab = document.createElement('button');
    newTab.type = 'button';
    newTab.className = 'stage-terminal-tab';
    newTab.textContent = `Shell ${shellTabNum}`;
    tabs.insertBefore(newTab, tabPlus);
    newTab.addEventListener('click', () => setTerminalTab(`shell-${shellTabNum}`, newTab));
    setTerminalTab(`shell-${shellTabNum}`, newTab);
    appendOutput(`\n--- Session shell-${shellTabNum} ---\n${spec.prompt || '$'} `);
  });
  inputForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const cmd = inputEl.value.trim();
    if (!cmd) return;
    appendOutput(`\n${spec.prompt || '$'} ${cmd}\n`);
    inputEl.value = '';
    if (socket && isConnected) {
      socket.send(JSON.stringify({ type: 'input', data: cmd + '\n' }));
    } else {
      if (cmd === 'clear') {
        clearBuffer();
      } else if (cmd === 'pwd') {
        appendOutput(`${spec.cwd || '~'}\n`);
      } else if (cmd === 'ls') {
        appendOutput('\n');
      } else if (cmd.startsWith('echo ')) {
        appendOutput(`${cmd.slice(5)}\n`);
      } else {
        appendOutput(`[shell] command executed: ${cmd}\n`);
      }
    }
  });

  container.appendChild(tabs);
  container.appendChild(toolbar);
  container.appendChild(buffer);
  container.appendChild(problemsView);
  container.appendChild(inputForm);
  container.appendChild(summary);
  body.appendChild(container);

  let isConnected = false;
  let socket = null;

  function appendOutput(text) {
    const chunk = document.createElement('span');
    chunk.innerHTML = ansiToHtml(text);
    buffer.appendChild(chunk);
    buffer.scrollTop = buffer.scrollHeight;
  }

  function clearBuffer() {
    buffer.innerHTML = '';
  }

  clearBtn.addEventListener('click', () => {
    clearBuffer();
  });

  const DEFAULT_TERMINAL_LOG = '';
  const initialLog = spec.initialText !== undefined ? spec.initialText : DEFAULT_TERMINAL_LOG;
  if (initialLog) {
    appendOutput(initialLog.endsWith('\n') ? initialLog : initialLog + '\n');
  }

  const proto = (typeof window !== 'undefined' && window.location && window.location.protocol === 'https:') ? 'wss:' : 'ws:';
  const host = (typeof window !== 'undefined' && window.location && window.location.host) ? window.location.host : '127.0.0.1:8090';
  const defaultWsUrl = `${proto}//${host}/ws/terminal`;
  const wsUrl = spec.wsUrl || defaultWsUrl;

  if (typeof WebSocket !== 'undefined') {
    try {
      socket = new WebSocket(wsUrl);

      socket.onopen = () => {
        isConnected = true;
        dot.className = 'stage-terminal-dot stage-terminal-dot-connected';
        statusLabel.textContent = 'Connected';
        inputEl.placeholder = 'Type shell command and press Enter...';
        emit({ type: 'terminal_connect', id: spec.__id, kind: 'terminal', wsUrl });
      };

      socket.onmessage = (event) => {
        let data = event.data;
        if (typeof data === 'string') {
          try {
            const json = JSON.parse(data);
            if (json.data) data = json.data;
            else if (json.message) data = json.message;
          } catch {
            // plain string
          }
          appendOutput(data);
        }
      };

      socket.onerror = () => {
        isConnected = false;
        dot.className = 'stage-terminal-dot stage-terminal-dot-error';
        statusLabel.textContent = 'no terminal backend is configured — this is the client only';
        appendOutput(
          `\n\x1b[33m[terminal]\x1b[0m no terminal backend is configured — this is the client only.\n` +
          `\x1b[90m(attempted connection to ${wsUrl} failed: route does not exist on server)\x1b[0m\n`
        );
        emit({
          type: 'terminal_error',
          id: spec.__id,
          kind: 'terminal',
          message: 'no terminal backend configured',
          wsUrl,
        });
      };

      socket.onclose = () => {
        if (isConnected) {
          isConnected = false;
          dot.className = 'stage-terminal-dot stage-terminal-dot-disconnected';
          statusLabel.textContent = 'Disconnected';
        }
      };
    } catch (err) {
      isConnected = false;
      dot.className = 'stage-terminal-dot stage-terminal-dot-error';
      statusLabel.textContent = 'no terminal backend is configured — this is the client only';
      appendOutput(
        `\n\x1b[33m[terminal]\x1b[0m no terminal backend is configured — this is the client only.\n` +
        `\x1b[90m(${err.message})\x1b[0m\n`
      );
    }
  } else {
    dot.className = 'stage-terminal-dot stage-terminal-dot-disconnected';
    statusLabel.textContent = 'no terminal backend is configured — this is the client only';
  }

  inputForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const command = inputEl.value.trim();
    if (!command) return;
    inputEl.value = '';

    appendOutput(`\x1b[1;36m${promptSpan.textContent}\x1b[0m ${command}\n`);

    if (isConnected && socket && socket.readyState === (typeof WebSocket !== 'undefined' ? WebSocket.OPEN : 1)) {
      socket.send(JSON.stringify({ type: 'stdin', data: command + '\n' }));
      emit({ type: 'terminal_input', id: spec.__id, kind: 'terminal', command, connected: true });
    } else {
      appendOutput(
        `\x1b[31m[error]\x1b[0m cannot execute command: no terminal backend is configured — this is the client only.\n`
      );
      emit({ type: 'terminal_input', id: spec.__id, kind: 'terminal', command, connected: false });
    }
  });

  body.__terminal = {
    write: appendOutput,
    clear: clearBuffer,
    isConnected: () => isConnected,
    getBuffer: () => buffer.textContent,
  };

  return body;
}

// The editor view is a real Monaco-backed editor (see ui/js/editor.js). It is
// injected by index.html with `setEditorBodyBuilder(buildEditorBody)` so this
// module stays dependency-free and the linkedom contract test can run stage.js
// on its own. When no builder is injected (or the injected builder throws) the
// honest textarea fallback below is used -- never a blank panel.
let _editorBodyBuilder = null;
// [CanvasWS] optional open_file handler for the canvas socket: the real
// Monaco editor (ui/js/editor.js) injects window.uapOpenWorkspaceFile, and
// index.html may also call setCanvasFileOpener(fn). fn(path, root) returns
// false when it could not handle the open (its view is hidden), in which case
// stage.js opens its own editor view on the file.
let _canvasFileOpener = null;
export function setCanvasFileOpener(fn) {
  _canvasFileOpener = typeof fn === 'function' ? fn : null;
}

export function setEditorBodyBuilder(fn) {
  _editorBodyBuilder = typeof fn === 'function' ? fn : null;
}

function _buildFallbackEditorBody(body, spec, emit) {
  body.classList.add('stage-editor-body');

  const container = document.createElement('div');
  container.className = 'stage-editor-container';

  const toolbar = document.createElement('div');
  toolbar.className = 'stage-editor-toolbar';

  const metaGroup = document.createElement('div');
  metaGroup.className = 'stage-editor-meta';

  const filename = spec.filename || (spec.language === 'javascript' ? 'script.js' : 'script.py');
  const filenameEl = document.createElement('span');
  filenameEl.className = 'stage-editor-filename';
  filenameEl.textContent = filename;

  const langEl = document.createElement('span');
  langEl.className = 'stage-editor-lang';
  langEl.textContent = spec.language || 'python';

  metaGroup.appendChild(filenameEl);
  metaGroup.appendChild(langEl);

  const actionsGroup = document.createElement('div');
  actionsGroup.className = 'stage-editor-actions';

  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'stage-editor-btn stage-editor-save-btn';
  saveBtn.textContent = '💾 Save';
  saveBtn.title = 'Download buffer as file';

  const runBtn = document.createElement('button');
  runBtn.type = 'button';
  runBtn.className = 'stage-editor-btn stage-editor-run-btn';
  runBtn.textContent = '▶ Run';
  runBtn.title = 'Execute code against backend';

  actionsGroup.appendChild(saveBtn);
  actionsGroup.appendChild(runBtn);

  toolbar.appendChild(metaGroup);
  toolbar.appendChild(actionsGroup);

  const workspace = document.createElement('div');
  workspace.className = 'stage-editor-workspace';

  const gutter = document.createElement('div');
  gutter.className = 'stage-editor-gutter';
  gutter.setAttribute('aria-hidden', 'true');

  const gutterInner = document.createElement('div');
  gutterInner.className = 'stage-editor-gutter-inner';
  gutter.appendChild(gutterInner);

  const initialCode = spec.code !== undefined ? spec.code : (spec.text !== undefined ? spec.text : '');

  const textarea = document.createElement('textarea');
  textarea.className = 'stage-editor-textarea';
  textarea.value = initialCode;
  textarea.spellcheck = false;
  textarea.wrap = 'off';
  textarea.setAttribute('aria-label', `Code editor for ${filename}`);

  workspace.appendChild(gutter);
  workspace.appendChild(textarea);

  const statusbar = document.createElement('div');
  statusbar.className = 'stage-editor-statusbar';

  const posEl = document.createElement('span');
  posEl.className = 'stage-editor-pos';
  posEl.textContent = 'Ln 1, Col 1';

  const statusMsg = document.createElement('span');
  statusMsg.className = 'stage-editor-status-msg';
  statusMsg.textContent = 'Ready';

  statusbar.appendChild(posEl);
  statusbar.appendChild(statusMsg);

  const outputDrawer = document.createElement('div');
  outputDrawer.className = 'stage-editor-output-drawer';

  const outputHeader = document.createElement('div');
  outputHeader.className = 'stage-editor-output-header';

  const outputTitle = document.createElement('span');
  outputTitle.className = 'stage-editor-output-title';
  outputTitle.textContent = 'Execution Output';

  const outputCloseBtn = document.createElement('button');
  outputCloseBtn.type = 'button';
  outputCloseBtn.className = 'stage-editor-output-close';
  outputCloseBtn.textContent = '✕';
  outputCloseBtn.title = 'Close output';

  outputHeader.appendChild(outputTitle);
  outputHeader.appendChild(outputCloseBtn);

  const outputContent = document.createElement('pre');
  outputContent.className = 'stage-editor-output-content';
  outputContent.tabIndex = 0;
  outputContent.setAttribute('role', 'region');
  outputContent.setAttribute('aria-label', 'Code execution output');

  outputDrawer.appendChild(outputHeader);
  outputDrawer.appendChild(outputContent);

  container.appendChild(toolbar);
  container.appendChild(workspace);
  container.appendChild(statusbar);
  container.appendChild(outputDrawer);
  body.appendChild(container);

  function updateGutter() {
    const lines = (textarea.value || '').split('\n');
    const totalLines = Math.max(lines.length, 1);
    let gutterText = '';
    for (let i = 1; i <= totalLines; i++) {
      gutterText += i + '\n';
    }
    gutterInner.textContent = gutterText;
  }

  function updateCursorPos() {
    const start = textarea.selectionStart || 0;
    const textBefore = (textarea.value || '').slice(0, start);
    const lines = textBefore.split('\n');
    const line = lines.length;
    const col = lines[lines.length - 1].length + 1;
    posEl.textContent = `Ln ${line}, Col ${col}`;
  }

  updateGutter();
  updateCursorPos();

  textarea.addEventListener('scroll', () => {
    gutter.scrollTop = textarea.scrollTop;
  });

  textarea.addEventListener('input', () => {
    updateGutter();
    updateCursorPos();
  });

  ['click', 'keyup', 'select'].forEach((evt) => {
    textarea.addEventListener(evt, updateCursorPos);
  });

  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Tab') {
      e.preventDefault();
      const TAB_SPACES = '    ';
      const start = textarea.selectionStart || 0;
      const end = textarea.selectionEnd || 0;
      const val = textarea.value || '';

      if (e.shiftKey) {
        const lastNewline = val.lastIndexOf('\n', start - 1);
        const lineStart = lastNewline === -1 ? 0 : lastNewline + 1;
        if (val.startsWith(TAB_SPACES, lineStart)) {
          textarea.value = val.slice(0, lineStart) + val.slice(lineStart + 4);
          textarea.selectionStart = textarea.selectionEnd = Math.max(lineStart, start - 4);
        } else if (val.startsWith('  ', lineStart)) {
          textarea.value = val.slice(0, lineStart) + val.slice(lineStart + 2);
          textarea.selectionStart = textarea.selectionEnd = Math.max(lineStart, start - 2);
        }
      } else {
        textarea.value = val.substring(0, start) + TAB_SPACES + val.substring(end);
        textarea.selectionStart = textarea.selectionEnd = start + TAB_SPACES.length;
      }

      updateGutter();
      updateCursorPos();
    }
  });

  outputCloseBtn.addEventListener('click', () => {
    outputDrawer.classList.remove('is-visible');
  });

  function handleSave() {
    const content = textarea.value || '';
    if (typeof Blob !== 'undefined' && typeof URL !== 'undefined' && typeof URL.createObjectURL === 'function') {
      const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      setTimeout(() => {
        try { URL.revokeObjectURL(url); } catch (_e) {}
      }, 1000);
    }

    statusMsg.textContent = `Saved ${filename} (${content.length} B)`;
    emit({
      type: 'save',
      id: spec.__id,
      kind: 'editor',
      filename,
      bytes: content.length,
    });
  }

  saveBtn.addEventListener('click', handleSave);

  async function handleRun() {
    runBtn.disabled = true;
    runBtn.textContent = '⏳ Running...';
    statusMsg.textContent = 'Executing...';

    outputDrawer.classList.add('is-visible');
    outputContent.textContent = 'Executing code against backend...\n';

    const code = textarea.value || '';
    // The backend sandbox route (POST /api/sandbox/run) is the real execution
    // path for a user-opened editor: it runs the code in an isolated process and
    // returns stdout/stderr/exit_code. A caller may still override with
    // `runEndpoint` (e.g. an agent that wants the code submitted as a task).
    const endpoint = spec.runEndpoint || '/api/sandbox/run';
    const language = spec.language || 'python';
    const payload = spec.runEndpoint ? { code } : { language, code };

    const startTime = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    try {
      if (typeof fetch === 'function') {
        const response = await fetch(endpoint, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
          },
          body: JSON.stringify(payload),
        });

        const elapsed = Math.round(((typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now()) - startTime);
        let data;
        const contentType = response.headers ? (response.headers.get('content-type') || '') : '';
        if (contentType.includes('application/json')) {
          data = await response.json();
        } else {
          data = await response.text();
        }

        let formatted = `[Execution target: ${endpoint}]\n`;
        formatted += `HTTP ${response.status} ${response.statusText} (${elapsed}ms)\n\n`;
        if (typeof data === 'object') {
          formatted += JSON.stringify(data, null, 2);
        } else {
          formatted += data;
        }

        outputContent.textContent = formatted;
        statusMsg.textContent = response.ok ? `Done (${response.status})` : `Failed (${response.status})`;

        emit({
          type: 'run',
          id: spec.__id,
          kind: 'editor',
          endpoint,
          status: response.status,
          elapsed,
          response: data,
        });
      } else {
        outputContent.textContent = '[Execution mock]: fetch is not available in this environment';
      }
    } catch (err) {
      outputContent.textContent = `[Network / Execution Error]\n${err.message}`;
      statusMsg.textContent = 'Execution error';
      emit({
        type: 'error',
        id: spec.__id,
        kind: 'editor',
        message: err.message,
      });
    } finally {
      runBtn.disabled = false;
      runBtn.textContent = '▶ Run';
    }
  }

  runBtn.addEventListener('click', handleRun);

  body.__editor = {
    getValue: () => textarea.value,
    setValue: (val) => {
      textarea.value = val;
      updateGutter();
      updateCursorPos();
    },
    save: handleSave,
    run: handleRun,
    getLineCount: () => (textarea.value || '').split('\n').length,
    getTextarea: () => textarea,
  };

  return body;
}

// Public entry point for the editor view. Delegates to the injected builder
// (the real Monaco editor) and degrades to the textarea fallback if that
// builder is absent or throws, so the view is never blank.
export function buildEditorBody(body, spec = {}, emit = () => {}) {
  if (_editorBodyBuilder) {
    try {
      return _editorBodyBuilder(body, spec, emit);
    } catch (err) {
      if (typeof console !== 'undefined' && console.warn) {
        console.warn('editor builder failed; using textarea fallback', err);
      }
    }
  }
  return _buildFallbackEditorBody(body, spec, emit);
}

export function createStage(containerEl, options = {}) {
  if (!containerEl) throw new Error('createStage: a container element is required');

  containerEl.classList.add('stage-root', 'stage-canvas-host');
  const grid = document.createElement('div');
  grid.className = 'stage-grid';
  containerEl.appendChild(grid);

  const views = new Map(); // id -> { spec, cardEl }
  const listeners = new Set();
  let seq = 0;
  // [ExcalidrawCanvas] fill mode state & helpers
  let filledViewId = null;
  // `editor` and `terminal` are the two views that need the most room, so they
  // are fillable too (owner bug: they had no way to expand).
  const FILLABLE_KINDS = new Set(['whiteboard', 'iframe', 'video', 'image', 'editor', 'terminal']);

  // [Resize] Minimum card size in CSS px. A card can never be dragged smaller
  // than this, so it cannot collapse to nothing: 280 wide x 200 tall.
  const MIN_CARD_W = 280;
  const MIN_CARD_H = 200;
  const sizes = new Map(); // id -> { w, h }

  // [SplitPane] Region layout state. Free canvas (mode 0) is the default and
  // keeps every existing behaviour untouched. Mode 2 / 4 lays the stage out as
  // a CSS grid of regions: cards are re-parented into region elements (they
  // stay ordinary absolutely-positioned cards in region-local coordinates) and
  // can be dragged across regions; dividers drag the region ratios. Mode,
  // ratios and per-view region assignments are persisted so a reload restores
  // the layout.
  const SPLIT_KEY = 'uap.stage.split';
  const SPLIT_MIN_FRACTION = 0.15;

  const defaultMode = (options && typeof options.defaultSplitMode === 'number') ? options.defaultSplitMode : 0;
  function loadSplitMode() {
    const raw = getStorage(`${SPLIT_KEY}.mode`);
    if (raw === null) return defaultMode;
    const mode = Number(raw);
    return mode === 2 || mode === 4 ? mode : (mode === 0 ? 0 : defaultMode);
  }

  function loadSplitRatios() {
    const clamp = (v) => (typeof v === 'number' && isFinite(v) ? Math.min(Math.max(v, SPLIT_MIN_FRACTION), 1 - SPLIT_MIN_FRACTION) : 0.5);
    const raw = getStorage(`${SPLIT_KEY}.ratios`);
    if (raw) {
      try {
        const p = JSON.parse(raw);
        return { x: clamp(p?.x), y: clamp(p?.y) };
      } catch (_e) {}
    }
    return { x: 0.5, y: 0.5 };
  }

  let splitMode = loadSplitMode();
  let splitRatios = loadSplitRatios();
  let focusedViewId = null;
  const splitRegions = new Map(); // id -> region index
  const splitPositions = new Map(); // id -> { x, y } region-local
  let regionEls = [];
  let splitDividers = [];
  let resizingSplit = null; // { axis: 'x' | 'y', rect }
  let splitDropRegion = -1; // region under the pointer while a card is dragged

  // [InfiniteCanvas] Storage helpers (safe for LinkeDOM / SSR / private mode)
  function getStorage(key) {
    try {
      if (typeof localStorage !== 'undefined' && localStorage && typeof localStorage.getItem === 'function') {
        return localStorage.getItem(key);
      }
    } catch (_e) {}
    return null;
  }

  function setStorage(key, val) {
    try {
      if (typeof localStorage !== 'undefined' && localStorage && typeof localStorage.setItem === 'function') {
        localStorage.setItem(key, val);
      }
    } catch (_e) {}
  }

  function removeStorage(key) {
    try {
      if (typeof localStorage !== 'undefined' && localStorage && typeof localStorage.removeItem === 'function') {
        localStorage.removeItem(key);
      }
    } catch (_e) {}
  }

  // [InfiniteCanvas] Pan/zoom limits & state
  const MIN_ZOOM = 0.25;
  const MAX_ZOOM = 2.0;

  function loadInitialViewport() {
    const raw = getStorage('uap.stage.viewport');
    if (raw) {
      try {
        const p = JSON.parse(raw);
        if (typeof p?.x === 'number' && typeof p?.y === 'number' && typeof p?.zoom === 'number') {
          return {
            x: Math.round(p.x),
            y: Math.round(p.y),
            zoom: Math.min(Math.max(p.zoom, MIN_ZOOM), MAX_ZOOM),
          };
        }
      } catch (_e) {}
    }
    return { x: 40, y: 40, zoom: 1 };
  }

  let viewport = loadInitialViewport();
  const positions = new Map(); // id -> { x, y }
  let topZ = 10;
  let isPanning = false;
  let panStart = { x: 0, y: 0 };
  let isSpaceDown = false;
  let draggingCard = null;
  let hasCardMoved = false;
  let resizingCard = null;
  let hasCardResized = false;

  function saveViewport() {
    setStorage('uap.stage.viewport', JSON.stringify(viewport));
    emit({ type: 'viewport', viewport: { ...viewport } });
  }

  function applyViewport() {
    if (filledViewId) return; // Fill mode takes over whole container
    if (splitMode) {
      // [SplitPane] Regions are sized by the CSS grid and the dividers; the
      // pan/zoom transform of the free canvas must not apply.
      grid.style.transform = 'none';
      return;
    }
    grid.style.transform = `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.zoom})`;
    grid.style.transformOrigin = '0 0';
    const bgSize = Math.max(12, Math.round(24 * viewport.zoom));
    containerEl.style.backgroundPosition = `${viewport.x}px ${viewport.y}px`;
    containerEl.style.backgroundSize = `${bgSize}px ${bgSize}px`;
  }

  // Per-kind default size, kept in step with the CSS in app.css. Two families:
  // media surfaces are larger (580x480); everything else is the standard
  // 540x440 card, so a row of surfaces lines up.
  function getCardWidth(kind) {
    if (kind === 'whiteboard' || kind === 'iframe' || kind === 'video' || kind === 'image') return 580;
    return 540;
  }

  function getCardHeight(kind) {
    if (kind === 'whiteboard' || kind === 'iframe' || kind === 'video' || kind === 'image') return 480;
    return 440;
  }

  // [Resize] Per-view size, persisted like position. Returns null when the view
  // has never been resized so the CSS per-kind default stays in charge.
  function loadCardSize(id) {
    const raw = getStorage(`uap.stage.size.${id}`);
    if (!raw) return null;
    try {
      const s = JSON.parse(raw);
      if (typeof s?.w === 'number' && typeof s?.h === 'number') {
        return {
          w: Math.max(MIN_CARD_W, Math.round(s.w)),
          h: Math.max(MIN_CARD_H, Math.round(s.h)),
        };
      }
    } catch (_e) {}
    return null;
  }

  function applyCardSize(cardEl, id) {
    const size = sizes.get(id);
    if (!cardEl || !size) return;
    cardEl.style.width = `${size.w}px`;
    cardEl.style.height = `${size.h}px`;
  }

  function setCardSize(id, w, h, { persist = true } = {}) {
    const targetId = String(id);
    const size = {
      w: Math.max(MIN_CARD_W, Math.round(w)),
      h: Math.max(MIN_CARD_H, Math.round(h)),
    };
    sizes.set(targetId, size);
    const entry = views.get(targetId);
    if (entry) {
      applyCardSize(entry.cardEl, targetId);
      relayoutView(targetId);
    }
    if (persist) setStorage(`uap.stage.size.${targetId}`, JSON.stringify(size));
    return size;
  }

  function getCardSize(id) {
    const s = sizes.get(String(id));
    return s ? { ...s } : null;
  }

  // [Resize] Tell size-aware inner views that their box changed. Monaco in
  // particular caches its layout and MUST be given an explicit layout() call,
  // otherwise it keeps rendering at the old width. Other embeds (iframes,
  // images, videos, the terminal buffer) are laid out by CSS and follow
  // automatically, but we still dispatch a window resize so they can react.
  function relayoutView(id) {
    const entry = views.get(String(id));
    if (!entry) return;
    const body = entry.cardEl.querySelector('.stage-card-body');
    if (!body) return;
    const editor = body.__editor;
    if (editor && typeof editor.getMonaco === 'function') {
      const monacoEditor = editor.getMonaco();
      if (monacoEditor && typeof monacoEditor.layout === 'function') {
        monacoEditor.layout();
        // The filled class is applied by CSS; re-run once the browser has
        // recomputed the box so Monaco measures the real (new) size.
        if (typeof setTimeout === 'function') {
          setTimeout(() => {
            try { monacoEditor.layout(); } catch (_e) {}
          }, 0);
        }
      }
    }
    if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function' && typeof window.Event === 'function') {
      window.dispatchEvent(new window.Event('resize'));
    }
  }

  // [InfiniteCanvas] Deterministic placement rule.
  //
  // The old rule hard-coded a 3-column 500x440 grid and assumed every existing
  // card was 440x380, so a 540px-wide editor overlapped its neighbour and a
  // third column ran off the canvas (measured 2026-10-03: overlaps of 16px and a
  // card right edge 341px past the canvas). This version uses each card's REAL
  // width/height, wraps to the next row when a column would exceed the visible
  // canvas, and clamps every position so a card can never be placed off-screen.
  function containerSize() {
    let rect = null;
    if (typeof containerEl.getBoundingClientRect === 'function') {
      rect = containerEl.getBoundingClientRect();
    }
    const width = (rect && rect.width) || containerEl.clientWidth || 1227;
    const height = (rect && rect.height) || containerEl.clientHeight || 788;
    return { width, height };
  }

  // World-space rectangle currently visible inside the container (the grid is
  // translated by viewport.x/y and scaled by viewport.zoom).
  function worldBounds() {
    const { width, height } = containerSize();
    const zoom = viewport.zoom || 1;
    return {
      left: Math.round(-viewport.x / zoom),
      top: Math.round(-viewport.y / zoom),
      right: Math.round((width - viewport.x) / zoom),
      bottom: Math.round((height - viewport.y) / zoom),
    };
  }

  // A card's real box: an explicit resize wins, then the element's measured
  // size, then the per-kind default. Used for both the new card and every
  // existing card so collision tests compare like with like.
  function cardBox(kind, id) {
    const entry = views.get(String(id));
    const sized = sizes.get(String(id)) || loadCardSize(String(id));
    const measuredW = entry && entry.cardEl && entry.cardEl.offsetWidth;
    const measuredH = entry && entry.cardEl && entry.cardEl.offsetHeight;
    return {
      w: (sized && sized.w) || measuredW || getCardWidth(kind),
      h: (sized && sized.h) || measuredH || getCardHeight(kind),
    };
  }

  function getNextCardPosition(kind, id) {
    const rawSaved = getStorage(`uap.stage.pos.${id}`);
    if (rawSaved) {
      try {
        const p = JSON.parse(rawSaved);
        if (typeof p?.x === 'number' && typeof p?.y === 'number') {
          return { x: p.x, y: p.y };
        }
      } catch (_e) {}
    }

    const { w: cardW, h: cardH } = cardBox(kind, id);
    const bounds = worldBounds();
    const gap = 24;
    const originX = 40;
    const originY = 40;
    const startX = Math.max(originX, bounds.left);
    const startY = Math.max(originY, bounds.top);
    // The largest world coordinate at which the card is still fully visible.
    const maxX = Math.max(startX, bounds.right - cardW);
    const maxY = Math.max(startY, bounds.bottom - cardH);

    // Existing cards as real rectangles (skip self).
    const others = [];
    for (const [existingId, pos] of positions.entries()) {
      if (existingId === id) continue;
      const entry = views.get(existingId);
      const box = cardBox(entry?.spec?.kind, existingId);
      others.push({ x: pos.x, y: pos.y, w: box.w, h: box.h });
    }

    // Column/row pitch follows the widest/tallest card on the canvas, so slots
    // are guaranteed not to overlap whatever the mix of card sizes is.
    const colStep = Math.max(cardW, ...others.map((o) => o.w)) + gap;
    const rowStep = Math.max(cardH, ...others.map((o) => o.h)) + gap;

    const collidesAt = (x, y) =>
      others.some((o) =>
        x < o.x + o.w && x + cardW > o.x && y < o.y + o.h && y + cardH > o.y
      );

    // Pass 1: the first slot whose card is FULLY inside the visible canvas.
    // Pass 2: if the canvas is genuinely full, wrap into the next row (an
    // infinite-canvas position the user reaches with Fit/pan) -- never overlap
    // and never let the card's RIGHT edge pass the canvas.
    for (const pass of [1, 2]) {
      for (let row = 0; row < 500; row++) {
        const candY = startY + row * rowStep;
        if (pass === 1 && candY > maxY) break;
        for (let col = 0; col < 500; col++) {
          const candX = startX + col * colStep;
          if (candX > maxX) break;
          if (!collidesAt(candX, candY)) {
            return { x: Math.round(candX), y: Math.round(candY) };
          }
        }
      }
    }
    // Fallback (only if 500 rows are all occupied): a fresh column, right of
    // everything, still on the first row. Non-overlapping by construction.
    const farRight = others.reduce((m, o) => Math.max(m, o.x + o.w + gap), startX);
    return { x: Math.round(farRight), y: Math.round(startY) };
  }

  // [Arrange] Re-flow every free-canvas card into a clean, non-overlapping,
  // in-bounds grid. The manual escape hatch for a messy canvas.
  function arrangeCards() {
    if (splitMode) return 0; // regions own their layout in split mode
    const ids = Array.from(views.keys());
    if (ids.length === 0) return 0;
    for (const id of ids) {
      positions.delete(id);
      removeStorage(`uap.stage.pos.${id}`);
    }
    for (const id of ids) {
      const entry = views.get(id);
      if (!entry) continue;
      const pos = getNextCardPosition(entry.spec?.kind, id);
      positions.set(id, pos);
      setStorage(`uap.stage.pos.${id}`, JSON.stringify(pos));
      entry.cardEl.style.left = `${pos.x}px`;
      entry.cardEl.style.top = `${pos.y}px`;
    }
    emit({ type: 'arrange', ids });
    return ids.length;
  }

  // [SplitPane] -----------------------------------------------------------------
  function rectOf(el) {
    if (el && typeof el.getBoundingClientRect === 'function') {
      const r = el.getBoundingClientRect();
      if (r) return { left: r.left || 0, top: r.top || 0, width: r.width || 0, height: r.height || 0 };
    }
    return { left: 0, top: 0, width: 0, height: 0 };
  }

  function fr(v) {
    return `${(Math.round(v * 1000) / 1000)}fr`;
  }

  function persistSplit() {
    setStorage(`${SPLIT_KEY}.mode`, String(splitMode));
    setStorage(`${SPLIT_KEY}.ratios`, JSON.stringify(splitRatios));
  }

  function regionOf(id) {
    const r = splitRegions.get(String(id));
    if (typeof r === 'number' && r >= 0 && r < splitMode) return r;
    return 0;
  }

  function savedSplitRegion(id) {
    const raw = getStorage(`${SPLIT_KEY}.region.${id}`);
    if (raw !== null) {
      const r = Number(raw);
      if (Number.isFinite(r) && r >= 0 && r < splitMode) return r;
    }
    return null;
  }

  // A card with no remembered region goes to the least-loaded one (ties: lowest
  // index), so new cards spread across the regions instead of piling up in one.
  function loadSplitRegion(id) {
    const saved = savedSplitRegion(id);
    return saved === null ? leastLoadedRegion(String(id)) : saved;
  }

  function loadSplitPosition(id) {
    const raw = getStorage(`${SPLIT_KEY}.pos.${id}`);
    if (!raw) return null;
    try {
      const p = JSON.parse(raw);
      if (typeof p?.x === 'number' && typeof p?.y === 'number') return { x: p.x, y: p.y };
    } catch (_e) {}
    return null;
  }

  function countCardsInRegion(region, exceptId) {
    let n = 0;
    for (const id of views.keys()) {
      if (id !== exceptId && splitRegions.get(id) === region) n += 1;
    }
    return n;
  }

  function leastLoadedRegion(exceptId) {
    let best = 0;
    for (let r = 1; r < splitMode; r += 1) {
      if (countCardsInRegion(r, exceptId) < countCardsInRegion(best, exceptId)) best = r;
    }
    return best;
  }

  // A card entering a region without a remembered position cascades so every
  // card in the region stays visible (no two land on the same pixel).
  // Only cards created earlier count, so the first card sits at the origin.
  // ponytail: drop cards exactly at the pointer if a UX pass calls for it.
  function splitCascadePos(region, id) {
    const step = 36; // reveals the 24px close button of the card behind
    let n = 0;
    for (const other of views.keys()) {
      if (other === id) break;
      if (splitRegions.get(other) === region) n += 1;
    }
    n %= 4;
    return { x: 16 + n * step, y: 16 + n * step };
  }

  // [SplitPane] Inner padding so a card never sits flush against the region
  // edge or the node strip (measured: the editor card landed at x=348, exactly
  // the canvas left edge, touching the strip). Padding keeps a visible gutter.
  const REGION_PAD = 16;

  function clampPosToRegion(cardEl, region, pos) {
    const rect = rectOf(regionEls[region]);
    const w = (cardEl && cardEl.offsetWidth) || 0;
    const h = (cardEl && cardEl.offsetHeight) || 0;
    const innerW = Math.max(0, rect.width - REGION_PAD * 2);
    const innerH = Math.max(0, rect.height - REGION_PAD * 2);
    return {
      x: Math.round(Math.min(Math.max(pos.x - REGION_PAD, 0), Math.max(0, innerW - w)) + REGION_PAD),
      y: Math.round(Math.min(Math.max(pos.y - REGION_PAD, 0), Math.max(0, innerH - h)) + REGION_PAD),
    };
  }

  // [StageGuide] The guide is the ONE empty-canvas call to action. A region
  // hint ("Empty — add a surface, or drop a card here.") is the fallback used
  // only while the guide is hidden. Showing both at once put the hints UNDER
  // the centred guide (measured: hint boxes 348,212 602x788 and 974,212
  // 602x788 vs guide 682,371 560x470), so their centred text bled out on both
  // sides of the guide card. `guideElVisible()` lets the emptiness logic know
  // the guide owns the canvas right now.
  function guideElVisible() {
    return Boolean(guideEl && guideEl.style.display !== 'none' && views.size === 0 && !guideDismissed() && !guideSuppressed);
  }

  // Hide the "Empty — …" hint for a region once it holds at least one card, and
  // while the empty-canvas guide is on screen (the guide is the single empty
  // state; a hint must never render underneath it).
  function refreshRegionEmptiness() {
    const guideOwns = guideElVisible();
    for (let r = 0; r < regionEls.length; r += 1) {
      const hint = regionEls[r].querySelector('.stage-region-empty');
      if (hint) hint.style.display = (!guideOwns && countCardsInRegion(r) === 0) ? 'flex' : 'none';
    }
  }

  function removeSplitChrome() {
    for (const el of regionEls) el.remove();
    for (const el of splitDividers) el.remove();
    regionEls = [];
    splitDividers = [];
    grid.classList.remove('stage-grid-split', 'stage-grid-split-2', 'stage-grid-split-4');
    containerEl.classList.remove('stage-split-active');
  }

  function buildSplitChrome() {
    removeSplitChrome();
    if (!splitMode) return;
    const place = (el, col, row) => {
      el.style.gridColumn = String(col);
      el.style.gridRow = String(row);
    };
    for (let r = 0; r < splitMode; r += 1) {
      const el = document.createElement('div');
      el.className = 'stage-region';
      el.dataset.region = String(r);
      // A quiet, meaningful empty hint (not developer "REGION 1" jargon): it
      // tells the user what to do here and disappears the moment a card lands.
      const empty = document.createElement('div');
      empty.className = 'stage-region-empty';
      empty.textContent = 'Empty — add a surface, or drop a card here.';
      el.appendChild(empty);
      grid.appendChild(el);
      regionEls.push(el);
    }
    // Grid tracks: [region] [divider] [region] (x 2 rows in quad mode), so the
    // dividers are real grid items and resize natively with the regions.
    place(regionEls[0], 1, 1);
    place(regionEls[1], 3, 1);
    if (splitMode === 4) {
      place(regionEls[2], 1, 3);
      place(regionEls[3], 3, 3);
    }
    const vDivider = document.createElement('div');
    vDivider.className = 'stage-split-divider';
    vDivider.dataset.axis = 'x';
    vDivider.setAttribute('role', 'separator');
    vDivider.setAttribute('aria-orientation', 'vertical');
    vDivider.setAttribute('aria-label', 'Resize regions horizontally');
    place(vDivider, 2, '1 / -1');
    grid.appendChild(vDivider);
    splitDividers.push(vDivider);
    if (splitMode === 4) {
      const hDivider = document.createElement('div');
      hDivider.className = 'stage-split-divider';
      hDivider.dataset.axis = 'y';
      hDivider.setAttribute('role', 'separator');
      hDivider.setAttribute('aria-orientation', 'horizontal');
      hDivider.setAttribute('aria-label', 'Resize regions vertically');
      place(hDivider, '1 / -1', 2);
      grid.appendChild(hDivider);
      splitDividers.push(hDivider);
    }
    grid.classList.add('stage-grid-split', splitMode === 4 ? 'stage-grid-split-4' : 'stage-grid-split-2');
    containerEl.classList.add('stage-split-active');
    applySplitRatios();
    refreshRegionEmptiness();
  }

  function applySplitRatios() {
    if (!splitMode) return;
    grid.style.setProperty('--split-x', fr(splitRatios.x));
    grid.style.setProperty('--split-x2', fr(1 - splitRatios.x));
    grid.style.setProperty('--split-y', fr(splitRatios.y));
    grid.style.setProperty('--split-y2', fr(1 - splitRatios.y));
  }

  function defaultSplitPos(id, region) {
    return splitCascadePos(region, String(id));
  }

  // Place one card inside its region. `pos` wins for drops; otherwise the
  // remembered region-local position or the cascade default is used.
  function placeCardInRegion(id, region, pos) {
    const targetId = String(id);
    const entry = views.get(targetId);
    if (!entry) return;
    const regionEl = regionEls[region] || regionEls[0];
    if (regionEl && entry.cardEl.parentElement !== regionEl) {
      regionEl.appendChild(entry.cardEl);
    }
    const next = pos
      ? clampPosToRegion(entry.cardEl, region, pos)
      : (loadSplitPosition(targetId) || defaultSplitPos(targetId, region));
    splitPositions.set(targetId, next);
    setStorage(`${SPLIT_KEY}.region.${targetId}`, String(region));
    setStorage(`${SPLIT_KEY}.pos.${targetId}`, JSON.stringify(next));
    entry.cardEl.style.left = `${next.x}px`;
    entry.cardEl.style.top = `${next.y}px`;
    // app.css sizes a split card to the room left after this offset.
    entry.cardEl.style.setProperty('--card-x', `${next.x}px`);
    entry.cardEl.style.setProperty('--card-y', `${next.y}px`);
    refreshRegionEmptiness();
  }

  function layoutSplitCards() {
    // Remembered regions first, so the rest balance around them.
    splitRegions.clear();
    const ids = Array.from(views.keys());
    for (const id of ids) {
      const saved = savedSplitRegion(id);
      if (saved !== null) splitRegions.set(id, saved);
    }
    for (const id of ids) {
      if (!splitRegions.has(id)) {
        splitRegions.set(id, leastLoadedRegion(id));
        // A position remembered for another region would stack on that region's cards.
        removeStorage(`${SPLIT_KEY}.pos.${id}`);
      }
      placeCardInRegion(id, splitRegions.get(id));
    }
  }

  // Route a (re)created card into its region when in split mode. In free mode
  // this is a no-op: the card stays exactly where showView put it.
  function splitRouteView(id, spec) {
    if (!splitMode) return;
    const region = loadSplitRegion(id);
    splitRegions.set(id, region);
    const pos = (spec && spec.position && typeof spec.position.x === 'number' && typeof spec.position.y === 'number')
      ? { x: Math.round(spec.position.x), y: Math.round(spec.position.y) }
      : null;
    placeCardInRegion(id, region, pos);
  }

  function setSplitDropTarget(region) {
    if (splitDropRegion === region) return;
    if (splitDropRegion >= 0 && regionEls[splitDropRegion]) {
      regionEls[splitDropRegion].classList.remove('is-drop-target');
    }
    splitDropRegion = region;
    if (splitDropRegion >= 0 && regionEls[splitDropRegion]) {
      regionEls[splitDropRegion].classList.add('is-drop-target');
    }
  }

  function regionAtPoint(clientX, clientY) {
    for (let r = 0; r < regionEls.length; r += 1) {
      const rect = rectOf(regionEls[r]);
      if (clientX >= rect.left && clientX <= rect.left + rect.width
        && clientY >= rect.top && clientY <= rect.top + rect.height) {
        return r;
      }
    }
    return -1;
  }

  function setSplitRatio(axis, fraction) {
    if (!splitMode) return;
    const alpha = Math.min(Math.max(fraction, SPLIT_MIN_FRACTION), 1 - SPLIT_MIN_FRACTION);
    splitRatios = { ...splitRatios, [axis]: Math.round(alpha * 1000) / 1000 };
    applySplitRatios();
    persistSplit();
  }

  function setSplitMode(mode) {
    const next = (mode === 2 || mode === 4) ? mode : 0;
    const changed = next !== splitMode;
    splitMode = next;
    persistSplit();
    if (splitMode) {
      buildSplitChrome();
      layoutSplitCards();
    } else {
      removeSplitChrome();
      // Back to the infinite plane: every card returns to the grid at its
      // free-canvas position with its persisted size.
      for (const id of views.keys()) {
        const entry = views.get(id);
        if (!entry) continue;
        grid.appendChild(entry.cardEl);
        const pos = positions.get(id) || { x: 0, y: 0 };
        entry.cardEl.style.left = `${pos.x}px`;
        entry.cardEl.style.top = `${pos.y}px`;
        const size = sizes.get(id);
        if (size) applyCardSize(entry.cardEl, id);
      }
    }
    applyViewport();
    if (changed) emit({ type: 'split', mode: splitMode });
  }

  function getSplitMode() {
    return splitMode;
  }

  function setViewRegion(id, region) {
    if (!splitMode) return false;
    const targetId = String(id);
    if (!views.has(targetId)) return false;
    const next = Math.min(Math.max(Number(region) || 0, 0), splitMode - 1);
    splitRegions.set(targetId, next);
    placeCardInRegion(targetId, next);
    emit({ type: 'region', id: targetId, region: next });
    return true;
  }

  function getViewRegion(id) {
    if (!splitMode) return null;
    return regionOf(id);
  }

  // [InfiniteCanvas] Floating zoom controls UI
  const zoomControls = document.createElement('div');
  zoomControls.className = 'stage-zoom-controls';
  zoomControls.setAttribute('role', 'toolbar');
  zoomControls.setAttribute('aria-label', 'Canvas zoom controls');
  zoomControls.innerHTML = `
    <button type="button" class="stage-zoom-btn stage-zoom-arrange" data-action="arrange" title="Tidy cards into a clean grid" aria-label="Arrange cards">Arrange</button>
    <button type="button" class="stage-zoom-btn stage-zoom-out" data-action="zoom-out" title="Zoom Out" aria-label="Zoom Out">−</button>
    <button type="button" class="stage-zoom-level" data-action="zoom-reset" title="Reset zoom to 100%" aria-label="Reset zoom">${Math.round(viewport.zoom * 100)}%</button>
    <button type="button" class="stage-zoom-btn stage-zoom-in" data-action="zoom-in" title="Zoom In" aria-label="Zoom In">+</button>
    <button type="button" class="stage-zoom-btn stage-zoom-fit" data-action="zoom-fit" title="Fit all cards in view" aria-label="Fit to view">Fit</button>
  `;
  containerEl.appendChild(zoomControls);

  // [StageGuide] Empty-canvas usage guide. The canvas is the whole product, so
  // a first-time user needs to be told the real flow -- which controls exist and
  // what they do -- instead of a grid of tool tiles. Every label below is the
  // ACTUAL control name (task box placeholder, "Run", "+ Add surface", "Free" /
  // "Split 2" / "Split 4", the 📂 Open Folder button); nothing is invented. It
  // shows only while the canvas is empty and can be dismissed for good.
  const GUIDE_DISMISS_KEY = 'uap.stage.guide.dismissed';
  // Session-only suppression: once a task is run the guide steps aside for the
  // rest of the visit, but is not permanently dismissed (that is the explicit
  // "Don't show again" button).
  let guideSuppressed = false;
  const guideEl = document.createElement('div');
  guideEl.className = 'stage-guide';
  guideEl.setAttribute('role', 'region');
  guideEl.setAttribute('aria-label', 'How to use the canvas');
  guideEl.innerHTML = `
    <div class="stage-guide-head">
      <h2 class="stage-guide-title">How to use the canvas</h2>
      <button type="button" class="stage-guide-close" aria-label="Hide this guide" title="Hide this guide">✕</button>
    </div>
    <p class="stage-guide-lede">This canvas is your workspace. Two ways to start:</p>
    <ol class="stage-guide-steps">
      <li><strong>Ask for work.</strong> Type in the task box above (<em>“What do you want to research?”</em>) and press <kbd>Run</kbd>. Today only <code>research …</code> and <code>bug bounty on …</code> run; the report arrives as a surface here.</li>
      <li><strong>Add a surface yourself.</strong> Click <strong>+ Add surface</strong> in the toolbar above to open a tool on the canvas.</li>
    </ol>
    <div class="stage-guide-section">
      <span class="stage-guide-label">Surfaces</span>
      <ul class="stage-guide-list">
        <li><strong>Code Editor</strong> — edit real files; open your own project with the 📂 <strong>Open Folder</strong> button.</li>
        <li><strong>Terminal</strong> — a shell in your workspace.</li>
        <li><strong>Web Page</strong> — embed a site by URL. Sites that forbid embedding (Google, GitHub…) offer to open in a new tab instead.</li>
        <li><strong>Whiteboard</strong> — draw on an Excalidraw canvas.</li>
        <li><strong>Note</strong> — a Markdown scratchpad.</li>
      </ul>
    </div>
    <div class="stage-guide-section">
      <span class="stage-guide-label">Layout</span>
      <p class="stage-guide-text"><strong>Free</strong> lets you move and zoom cards; <strong>Split 2</strong> and <strong>Split 4</strong> arrange them in regions. In Free mode, the <strong>Arrange</strong> button tidies a messy canvas.</p>
    </div>
    <p class="stage-guide-note">When an agent opens a file, it appears here as an editor tab — agents drive this canvas.</p>
    <div class="stage-guide-actions">
      <button type="button" class="stage-guide-dismiss">Don’t show again</button>
    </div>
  `;
  containerEl.appendChild(guideEl);

  function guideDismissed() {
    return getStorage(GUIDE_DISMISS_KEY) === '1';
  }

  function updateEmptyState() {
    if (guideEl) {
      guideEl.style.display = views.size === 0 && !guideDismissed() && !guideSuppressed ? 'flex' : 'none';
    }
    // Refresh AFTER the guide's display is set: the region hints hide while the
    // guide owns the canvas (see refreshRegionEmptiness / guideElVisible).
    refreshRegionEmptiness();
  }

  function suppressGuide() {
    guideSuppressed = true;
    updateEmptyState();
  }

  guideEl.querySelector('.stage-guide-close')?.addEventListener('click', (e) => {
    e.stopPropagation();
    guideEl.style.display = 'none';
    // The region hints are the empty state while the guide is hidden.
    refreshRegionEmptiness();
  });
  guideEl.querySelector('.stage-guide-dismiss')?.addEventListener('click', (e) => {
    e.stopPropagation();
    setStorage(GUIDE_DISMISS_KEY, '1');
    guideEl.style.display = 'none';
    refreshRegionEmptiness();
  });


  function updateZoomDisplay() {
    const pct = `${Math.round(viewport.zoom * 100)}%`;
    const levelEl = zoomControls.querySelector('.stage-zoom-level');
    if (levelEl) levelEl.textContent = pct;
    if (typeof document !== 'undefined') {
      const toolbarResetBtn = document.getElementById('btn-zoom-reset');
      if (toolbarResetBtn) toolbarResetBtn.textContent = pct;
    }
  }

  function zoomAt(factor, cx, cy) {
    if (splitMode) return; // [SplitPane] the region grid has no transform to zoom
    const newZoom = Math.min(Math.max(viewport.zoom * factor, MIN_ZOOM), MAX_ZOOM);
    if (Math.abs(newZoom - viewport.zoom) < 0.001) return;
    const k = newZoom / viewport.zoom;
    viewport.zoom = Math.round(newZoom * 1000) / 1000;
    viewport.x = Math.round(cx - (cx - viewport.x) * k);
    viewport.y = Math.round(cy - (cy - viewport.y) * k);
    applyViewport();
    saveViewport();
    updateZoomDisplay();
  }

  function viewportCenter() {
    if (typeof containerEl.getBoundingClientRect === 'function') {
      const rect = containerEl.getBoundingClientRect();
      if (rect && rect.width > 0 && rect.height > 0) {
        return { cx: rect.width / 2, cy: rect.height / 2, rect };
      }
    }
    return { cx: 400, cy: 300, rect: { width: 800, height: 600 } };
  }

  function zoomIn() {
    const { cx, cy } = viewportCenter();
    zoomAt(1.2, cx, cy);
  }

  function zoomOut() {
    const { cx, cy } = viewportCenter();
    zoomAt(1 / 1.2, cx, cy);
  }

  function resetView() {
    viewport.zoom = 1;
    viewport.x = 40;
    viewport.y = 40;
    applyViewport();
    saveViewport();
    updateZoomDisplay();
  }

  function fitToView() {
    if (splitMode) return; // [SplitPane] nothing to fit: the grid owns the layout
    if (positions.size === 0) {
      resetView();
      return;
    }
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;

    for (const [id, pos] of positions.entries()) {
      const entry = views.get(id);
      const w = entry?.cardEl?.offsetWidth || getCardWidth(entry?.spec?.kind);
      const h = entry?.cardEl?.offsetHeight || getCardHeight(entry?.spec?.kind);
      minX = Math.min(minX, pos.x);
      minY = Math.min(minY, pos.y);
      maxX = Math.max(maxX, pos.x + w);
      maxY = Math.max(maxY, pos.y + h);
    }

    if (!isFinite(minX) || !isFinite(minY)) {
      resetView();
      return;
    }

    const { cx, cy, rect } = viewportCenter();
    const pad = 40;
    const widthFit = (rect.width - pad * 2) / Math.max(maxX - minX, 1);
    const heightFit = (rect.height - pad * 2) / Math.max(maxY - minY, 1);
    const fitZoom = Math.min(widthFit, heightFit);
    const zoom = Math.min(Math.max(fitZoom, MIN_ZOOM), 1.0);

    const midX = (minX + maxX) / 2;
    const midY = (minY + maxY) / 2;
    viewport = {
      zoom: Math.round(zoom * 1000) / 1000,
      x: Math.round(cx - midX * zoom),
      y: Math.round(cy - midY * zoom),
    };
    applyViewport();
    saveViewport();
    updateZoomDisplay();
  }

  zoomControls.addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    e.stopPropagation();
    const action = btn.dataset.action;
    if (action === 'zoom-in') zoomIn();
    else if (action === 'zoom-out') zoomOut();
    else if (action === 'zoom-fit') fitToView();
    else if (action === 'zoom-reset') resetView();
    else if (action === 'arrange') arrangeCards();
  });

  if (typeof document !== 'undefined') {
    document.getElementById('btn-zoom-in')?.addEventListener('click', () => zoomIn());
    document.getElementById('btn-zoom-out')?.addEventListener('click', () => zoomOut());
    document.getElementById('btn-zoom-fit')?.addEventListener('click', () => fitToView());
    document.getElementById('btn-zoom-reset')?.addEventListener('click', () => resetView());
  }

  applyViewport();
  updateZoomDisplay();

  function fillView(id) {
    const targetId = String(id);
    const entry = views.get(targetId);
    if (!entry) return false;
    if (filledViewId && filledViewId !== targetId) {
      const prev = views.get(filledViewId);
      if (prev) {
        prev.cardEl.classList.remove('stage-card-filled');
        if (prev.cardEl.__origParent) {
          prev.cardEl.__origParent.appendChild(prev.cardEl);
          delete prev.cardEl.__origParent;
        }
      }
    }
    filledViewId = targetId;
    containerEl.classList.add('stage-fill-active');
    if (splitMode) {
      entry.cardEl.__origParent = entry.cardEl.parentElement;
      containerEl.appendChild(entry.cardEl);
    }
    entry.cardEl.classList.add('stage-card-filled');
    relayoutView(targetId);
    emit({ type: 'fill', id: targetId, kind: entry.spec.kind, filled: true });
    return true;
  }

  function exitFill() {
    if (!filledViewId) return false;
    const currentId = filledViewId;
    const entry = views.get(currentId);
    if (entry) {
      entry.cardEl.classList.remove('stage-card-filled');
      if (entry.cardEl.__origParent) {
        entry.cardEl.__origParent.appendChild(entry.cardEl);
        delete entry.cardEl.__origParent;
      }
    }
    filledViewId = null;
    containerEl.classList.remove('stage-fill-active');
    applyViewport(); // restore infinite plane transform
    relayoutView(currentId); // back to the tiled box: re-measure again
    emit({ type: 'fill', id: currentId, kind: entry ? entry.spec.kind : null, filled: false });
    return true;
  }


  function getFilledViewId() {
    return filledViewId;
  }

  // [InfiniteCanvas] Wheel zoom handler
  containerEl.addEventListener('wheel', (e) => {
    if (filledViewId) return;
    if (splitMode) return; // [SplitPane] regions are sized by the dividers
    if (resizingCard) return; // a resize must not zoom the canvas
    const onCardBody = e.target.closest('.stage-card-body');
    if (onCardBody && !e.ctrlKey && !e.metaKey) {
      if (onCardBody.scrollHeight > onCardBody.clientHeight) {
        return; // allow vertical scroll inside scrollable content
      }
    }
    e.preventDefault();
    const rect = typeof containerEl.getBoundingClientRect === 'function'
      ? containerEl.getBoundingClientRect()
      : { left: 0, top: 0 };
    const factor = e.deltaY < 0 ? 1.1 : (1 / 1.1);
    zoomAt(factor, e.clientX - rect.left, e.clientY - rect.top);
  }, { passive: false });

  // [InfiniteCanvas] Mouse drag / pan interaction
  containerEl.addEventListener('mousedown', (e) => {
    if (filledViewId) return;

    // [SplitPane] 0. Dragging a region divider. Runs before everything else so
    // a divider grab never drags a card or pans a region.
    const divider = e.target.closest('.stage-split-divider');
    if (divider) {
      resizingSplit = { axis: divider.dataset.axis === 'y' ? 'y' : 'x', rect: rectOf(grid) };
      containerEl.classList.add('stage-split-resizing');
      e.preventDefault();
      e.stopPropagation();
      return;
    }

    // 1. Resizing a card by its corner handle. This runs before both the
    //    titlebar drag and the canvas pan so a resize never does either.
    const resizeHandle = e.target.closest('.stage-card-resize');
    if (resizeHandle) {
      const card = resizeHandle.closest('.stage-card');
      if (!card) return;
      const viewId = card.dataset.viewId;
      if (!viewId) return;
      // Use unscaled layout metrics (offsetWidth/Height), not the zoomed
      // getBoundingClientRect, so the resize baseline is correct at any zoom.
      const size = sizes.get(viewId) || {
        w: card.offsetWidth || getCardWidth(card.dataset.kind),
        h: card.offsetHeight || getCardHeight(card.dataset.kind),
      };
      topZ += 1;
      card.style.zIndex = String(topZ);
      resizingCard = {
        id: viewId,
        cardEl: card,
        startX: e.clientX,
        startY: e.clientY,
        startW: size.w,
        startH: size.h,
      };
      hasCardResized = false;
      containerEl.classList.add('stage-resizing');
      e.preventDefault();
      e.stopPropagation();
      return;
    }

    // 1. Dragging card by titlebar
    const titlebar = e.target.closest('.stage-card-titlebar');
    if (titlebar) {
      if (e.target.closest('button, a, input, select, textarea')) {
        return; // clicking a control in titlebar shouldn't drag
      }
      const card = titlebar.closest('.stage-card');
      if (!card) return;
      const viewId = card.dataset.viewId;
      if (!viewId) return;

      topZ += 1;
      card.style.zIndex = String(topZ);
      focusedViewId = viewId; // [CanvasWS] click = focus: resync the hello snapshot
      scheduleHello();

      // [SplitPane] In split mode the base position is region-local and the
      // grab offset is remembered so a cross-region drop lands under the cursor.
      let pos;
      if (splitMode) {
        pos = splitPositions.get(viewId) || { x: card.offsetLeft || 0, y: card.offsetTop || 0 };
      } else {
        pos = positions.get(viewId) || { x: card.offsetLeft || 0, y: card.offsetTop || 0 };
      }
      const cardRect = rectOf(card);
      draggingCard = {
        id: viewId,
        cardEl: card,
        startX: e.clientX,
        startY: e.clientY,
        cardX: pos.x,
        cardY: pos.y,
        grabOffsetX: e.clientX - cardRect.left,
        grabOffsetY: e.clientY - cardRect.top,
      };
      hasCardMoved = false;
      containerEl.classList.add('stage-dragging');
      if (splitMode) containerEl.classList.add('stage-card-split-dragging');
      e.preventDefault();
      return;
    }

    // 2. Pan canvas (free canvas only; regions are laid out by the grid)
    const isInsideCard = Boolean(e.target.closest('.stage-card'));
    const isInsideControls = Boolean(e.target.closest('.stage-zoom-controls'));
    if (isInsideControls) return;

    if (!splitMode && (!isInsideCard || e.button === 1 || isSpaceDown)) {
      if (e.button === 0 || e.button === 1) {
        isPanning = true;
        panStart = { x: e.clientX, y: e.clientY };
        containerEl.classList.add('stage-panning');
        e.preventDefault();
      }
    }
  });

  const onWindowMouseMove = (e) => {
    if (resizingSplit) {
      // [SplitPane] A divider drag only moves the region ratio.
      const { axis, rect } = resizingSplit;
      const span = axis === 'x' ? rect.width : rect.height;
      if (span > 0) {
        const frac = (axis === 'x' ? e.clientX - rect.left : e.clientY - rect.top) / span;
        setSplitRatio(axis, frac);
      }
    } else if (resizingCard) {
      hasCardResized = true;
      const dx = (e.clientX - resizingCard.startX) / viewport.zoom;
      const dy = (e.clientY - resizingCard.startY) / viewport.zoom;
      setCardSize(resizingCard.id, resizingCard.startW + dx, resizingCard.startH + dy, { persist: false });
    } else if (draggingCard) {
      hasCardMoved = true;
      // [SplitPane] Region-local coordinates are unscaled: the grid has no
      // transform, so a 1:1 pixel mapping is the correct one.
      const zoom = splitMode ? 1 : viewport.zoom;
      const dx = (e.clientX - draggingCard.startX) / zoom;
      const dy = (e.clientY - draggingCard.startY) / zoom;
      const newX = Math.round(draggingCard.cardX + dx);
      const newY = Math.round(draggingCard.cardY + dy);
      if (splitMode) {
        splitPositions.set(draggingCard.id, { x: newX, y: newY });
        setSplitDropTarget(regionAtPoint(e.clientX, e.clientY));
      } else {
        positions.set(draggingCard.id, { x: newX, y: newY });
      }
      draggingCard.cardEl.style.left = `${newX}px`;
      draggingCard.cardEl.style.top = `${newY}px`;
    } else if (isPanning) {
      const dx = e.clientX - panStart.x;
      const dy = e.clientY - panStart.y;
      panStart = { x: e.clientX, y: e.clientY };
      viewport.x += dx;
      viewport.y += dy;
      applyViewport();
    }
  };

  const onWindowMouseUp = (e) => {
    if (resizingSplit) {
      resizingSplit = null;
      containerEl.classList.remove('stage-split-resizing');
      setStorage(`${SPLIT_KEY}.ratios`, JSON.stringify(splitRatios));
      emit({ type: 'ratio', ratios: { ...splitRatios } });
    }
    if (resizingCard) {
      containerEl.classList.remove('stage-resizing');
      const size = sizes.get(resizingCard.id);
      if (size) {
        setStorage(`uap.stage.size.${resizingCard.id}`, JSON.stringify(size));
        if (hasCardResized) {
          emit({ type: 'resize', id: resizingCard.id, size: { ...size } });
        }
      }
      resizingCard = null;
      hasCardResized = false;
    }
    if (draggingCard) {
      containerEl.classList.remove('stage-dragging');
      const id = draggingCard.id;
      if (splitMode) {
        containerEl.classList.remove('stage-card-split-dragging');
        // A dragged card lands in whichever region the pointer is over; the
        // drop point is remembered so a reload puts it back exactly there.
        const dropRegion = splitDropRegion >= 0 ? splitDropRegion : regionOf(id);
        setSplitDropTarget(-1);
        if (dropRegion !== regionOf(id) && views.has(id)) {
          splitRegions.set(id, dropRegion);
          const rect = rectOf(regionEls[dropRegion]);
          const clientX = e && typeof e.clientX === 'number' ? e.clientX : rect.left + rect.width / 2;
          const clientY = e && typeof e.clientY === 'number' ? e.clientY : rect.top + rect.height / 2;
          placeCardInRegion(id, dropRegion, {
            x: clientX - rect.left - (draggingCard.grabOffsetX || 0),
            y: clientY - rect.top - (draggingCard.grabOffsetY || 0),
          });
          emit({ type: 'region', id, region: dropRegion });
        } else {
          const pos = splitPositions.get(id);
          if (pos) setStorage(`${SPLIT_KEY}.pos.${id}`, JSON.stringify(pos));
        }
        const finalPos = splitPositions.get(id) || null;
        if (hasCardMoved && finalPos) {
          emit({ type: 'move', id, position: finalPos, region: regionOf(id) });
        }
      } else {
        const pos = positions.get(id);
        if (pos) {
          setStorage(`uap.stage.pos.${id}`, JSON.stringify(pos));
          if (hasCardMoved) {
            emit({ type: 'move', id, position: pos });
          }
        }
      }
      draggingCard = null;
      hasCardMoved = false;
    }
    if (isPanning) {
      isPanning = false;
      containerEl.classList.remove('stage-panning');
      saveViewport();
    }
  };

  const onKeyDown = (e) => {
    if (e.key === 'Escape' && filledViewId) {
      exitFill();
      return;
    }
    if (e.code === 'Space' && !e.repeat) {
      const active = typeof document !== 'undefined' ? document.activeElement : null;
      const isEditing = active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable);
      if (!isEditing && !filledViewId) {
        isSpaceDown = true;
        containerEl.classList.add('stage-space-ready');
      }
    }
  };

  const onKeyUp = (e) => {
    if (e.code === 'Space') {
      isSpaceDown = false;
      containerEl.classList.remove('stage-space-ready');
    }
  };

  if (typeof window !== 'undefined') {
    window.addEventListener('mousemove', onWindowMouseMove);
    window.addEventListener('mouseup', onWindowMouseUp);
  }
  if (typeof document !== 'undefined') {
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('keyup', onKeyUp);
  }

  function emit(event) {
    for (const cb of listeners) {
      try { cb(event); } catch (err) { console.error('stage listener error:', err); }
    }
    // [CanvasWS] every structural/focus change re-syncs the canvas snapshot
    // (debounced; no-op when no socket is wired).
    scheduleHello();
  }

  // [CanvasWS] -----------------------------------------------------------------
  // One WebSocket per tab to /ws/canvas. The server pushes canvas commands
  // (open_file / add_view / focus_view) and this stage reports what is on
  // screen with a `hello` snapshot: on connect, on every structural change
  // (emit), on focus changes and on editor file/root changes (an editor
  // re-open is a `showView` with the same id, which emits `update`).
  //
  // Auth is opt-in (src/uap/server/auth.py): the bearer token the REST UI keeps
  // in localStorage under 'uap_api_token' is reused. A browser WebSocket cannot
  // set headers, so it rides the `?token=` query parameter the server checks
  // (same convention as the terminal socket in ui/js/terminal.js).
  //
  // A reloaded/redeployed server must not leave the tab dead: the socket
  // reconnects with exponential backoff until the page is unloaded.
  const CANVAS_WS_PATH = '/ws/canvas';
  const CANVAS_RECONNECT_BASE_MS = 500;
  const CANVAS_RECONNECT_MAX_MS = 5000;
  const CANVAS_HELLO_DEBOUNCE_MS = 150;

  let canvasSocket = null;
  let canvasReconnectTimer = null;
  let canvasReconnectDelay = CANVAS_RECONNECT_BASE_MS;
  let canvasClosedIntentionally = false;
  let helloTimer = null;

  function canvasToken() {
    return getStorage('uap_api_token') || '';
  }

  function canvasSocketUrl() {
    const loc = (typeof window !== 'undefined') ? window.location : null;
    const secure = Boolean(loc) && loc.protocol === 'https:';
    const proto = secure ? 'wss:' : 'ws:';
    const host = (loc && loc.host) || '127.0.0.1:8090';
    const token = canvasToken();
    return `${proto}//${host}${CANVAS_WS_PATH}${token ? `?token=${encodeURIComponent(token)}` : ''}`;
  }

  // Ring buffer of the last frames, in both directions: the dev/con panels
  // (window.__canvasMessages / window.__lastCanvasMessage) can then show what
  // the canvas channel actually exchanged without a proxy.
  function captureCanvasMessage(direction, payload) {
    if (typeof window === 'undefined') return;
    try {
      window.__lastCanvasMessage = { direction, payload };
      const ring = Array.isArray(window.__canvasMessages) ? window.__canvasMessages : [];
      ring.push({ direction, payload });
      window.__canvasMessages = ring.slice(-20);
    } catch (_e) {}
  }

  function wsOpenState() {
    return typeof WebSocket !== 'undefined' ? WebSocket.OPEN : 1;
  }

  function canvasOpen() {
    return Boolean(canvasSocket) && canvasSocket.readyState === wsOpenState();
  }

  function editorInstanceOf(entry) {
    if (!entry || !entry.cardEl || typeof entry.cardEl.querySelector !== 'function') return null;
    const body = entry.cardEl.querySelector('.stage-card-body');
    return (body && body.__editor) || null;
  }

  // Open path/root per card: from the injected editor when it exposes one,
  // otherwise from the view spec it was created/updated with (buildEditorBody
  // loads spec.path, and an editor re-open replaces the card).
  function viewOpenPath(entry) {
    const editor = editorInstanceOf(entry);
    if (editor && typeof editor.getPath === 'function') {
      const p = editor.getPath();
      if (p) return p;
    }
    return (entry.spec && entry.spec.path) || null;
  }

  function viewOpenRoot(entry) {
    const editor = editorInstanceOf(entry);
    if (editor && typeof editor.getRoot === 'function') return editor.getRoot();
    return (entry.spec && entry.spec.root) || null;
  }

  function helloPayload() {
    const focused = focusedViewId && views.has(focusedViewId) ? focusedViewId : null;
    return {
      type: 'hello',
      views: Array.from(views.values()).map((entry) => {
        const view = { id: entry.spec.__id, type: entry.spec.kind, title: titleFor(entry.spec) };
        const path = viewOpenPath(entry);
        const root = viewOpenRoot(entry);
        if (path) view.path = path;
        if (root) view.root = root;
        return view;
      }),
      focused,
    };
  }

  function sendHello() {
    if (!canvasOpen()) return;
    const hello = helloPayload();
    try {
      canvasSocket.send(JSON.stringify(hello));
      captureCanvasMessage('out', hello);
    } catch (_e) {}
  }

  // Debounced: a burst of structural events (open + split switch + region move)
  // must not spam one snapshot per card.
  function scheduleHello() {
    if (helloTimer || typeof setTimeout !== 'function') return;
    helloTimer = setTimeout(() => {
      helloTimer = null;
      sendHello();
    }, CANVAS_HELLO_DEBOUNCE_MS);
  }

  function onCanvasMessage(event) {
    const data = event && event.data;
    if (typeof data !== 'string') return;
    let msg;
    try {
      msg = JSON.parse(data);
    } catch (_e) {
      return; // not JSON: ignore, the channel stays usable
    }
    if (!msg || typeof msg !== 'object') return;
    captureCanvasMessage('in', msg);
    if (msg.type === 'open_file') {
      handleCanvasOpenFile(msg);
    } else if (msg.type === 'add_view') {
      const spec = (msg.spec && typeof msg.spec === 'object')
        ? msg.spec
        : (msg.view && typeof msg.view === 'object' ? msg.view : null);
      if (spec && spec.kind) showView(spec);
    } else if (msg.type === 'focus_view') {
      const id = msg.view_id || msg.id;
      if (id) focusView(id);
    }
    // `hello` and unknown types are ignored: this socket is a command channel,
    // not a second source of truth for the canvas state.
  }

  // open_file: prefer the injected editor (window.uapOpenWorkspaceFile, wired by
  // editor.js) so the file opens in the real Monaco-backed view; `false` means
  // its owning body is hidden, so stage.js opens its own editor view on the
  // same path instead of dropping the command.
  function canvasFileOpener() {
    if (_canvasFileOpener) return (path, root) => _canvasFileOpener(path, root);
    if (typeof window !== 'undefined' && typeof window.uapOpenWorkspaceFile === 'function') {
      return (path, root) => window.uapOpenWorkspaceFile(path, root);
    }
    return null;
  }

  function handleCanvasOpenFile(msg) {
    const path = typeof msg.path === 'string' ? msg.path : (typeof msg.file === 'string' ? msg.file : null);
    if (!path) return;
    const root = typeof msg.root === 'string' ? msg.root : undefined;
    const opener = canvasFileOpener();
    if (opener) {
      let result;
      try {
        result = opener(path, root);
      } catch (err) {
        result = false;
        if (typeof console !== 'undefined' && console.warn) {
          console.warn('canvas open_file handler failed; opening an editor view', err);
        }
      }
      if (result !== false && result !== null && result !== undefined) return; // handled (a promise counts)
    }
    // Fallback: reuse the path-keyed editor view, or the one editor view that
    // has no file yet, so repeated open_file commands land in the same card.
    const name = path.split('/').pop() || path;
    let id = `file:${path}`;
    if (!views.has(id)) {
      const editorView = Array.from(views.values())
        .find((entry) => entry.spec && entry.spec.kind === 'editor' && !entry.spec.path);
      if (editorView) id = editorView.spec.__id;
    }
    showView({
      id,
      kind: 'editor',
      path,
      root,
      filename: name,
      title: name,
    });
  }

  function focusView(id) {
    const targetId = String(id);
    if (!views.has(targetId)) return false;
    focusedViewId = targetId;
    const entry = views.get(targetId);
    topZ += 1;
    entry.cardEl.style.zIndex = String(topZ);
    emit({ type: 'focus', id: targetId });
    scheduleHello();
    return true;
  }

  function getFocusedViewId() {
    return focusedViewId && views.has(focusedViewId) ? focusedViewId : null;
  }

  function connectCanvas() {
    if (canvasSocket) return canvasSocket;
    if (typeof WebSocket === 'undefined') return null;
    if (typeof setTimeout !== 'function' || typeof clearTimeout !== 'function') return null;
    canvasClosedIntentionally = false;
    let ws;
    try {
      ws = new WebSocket(canvasSocketUrl());
    } catch (_e) {
      return null; // bad URL / disabled transport: stay on the free canvas
    }
    canvasSocket = ws;
    ws.onopen = () => {
      canvasReconnectDelay = CANVAS_RECONNECT_BASE_MS;
      sendHello();
    };
    ws.onmessage = onCanvasMessage;
    ws.onerror = () => {}; // close always follows; reconnect is handled there
    ws.onclose = () => {
      if (canvasSocket === ws) canvasSocket = null;
      if (canvasClosedIntentionally) return;
      // Server reload/redeploy: retry at the current delay (500ms, then
      // doubling up to 5s) instead of leaving the tab without a canvas.
      canvasReconnectTimer = setTimeout(() => {
        canvasReconnectTimer = null;
        if (!canvasClosedIntentionally) connectCanvas();
      }, canvasReconnectDelay);
      canvasReconnectDelay = Math.min(canvasReconnectDelay * 2, CANVAS_RECONNECT_MAX_MS);
    };
    captureCanvasMessage('connect', { url: canvasSocketUrl() });
    return ws;
  }

  function disconnectCanvas() {
    canvasClosedIntentionally = true;
    if (helloTimer && typeof clearTimeout === 'function') {
      clearTimeout(helloTimer);
      helloTimer = null;
    }
    if (canvasReconnectTimer && typeof clearTimeout === 'function') {
      clearTimeout(canvasReconnectTimer);
      canvasReconnectTimer = null;
    }
    if (canvasSocket) {
      const ws = canvasSocket;
      canvasSocket = null;
      ws.onclose = null; // no reconnect from an intentional close
      try {
        ws.close();
      } catch (_e) {}
    }
  }

  function setCanvasFileOpener(fn) {
    _canvasFileOpener = typeof fn === 'function' ? fn : null;
  }

  // Page unload ends the tab for good: stop the reconnect loop too.
  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('pagehide', disconnectCanvas);
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
        return buildMarkdownBody(body, spec, emit);
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
          fig.innerHTML = `<div class="stage-media-error" role="alert">Image failed to load: ${escapeHtml(spec.url)}<br>Check that the address is reachable and links straight to an image.</div>`;
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
          fig.innerHTML = `<div class="stage-media-error" role="alert">Video failed to load: ${escapeHtml(spec.url)}<br>Check that the address is reachable and links straight to a video file.</div>`;
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
        const url = (spec.url || '').trim();
        const controls = document.createElement('div');
        controls.className = 'stage-browser-controls';

        const btnReload = document.createElement('button');
        btnReload.type = 'button';
        btnReload.className = 'stage-browser-btn stage-browser-reload';
        btnReload.title = 'Reload';
        btnReload.setAttribute('aria-label', 'Reload');
        btnReload.textContent = '↻';

        const addressBar = document.createElement('div');
        addressBar.className = 'stage-browser-address-bar';

        const urlText = document.createElement('span');
        urlText.className = 'stage-browser-url-text';
        urlText.textContent = url || 'about:blank';

        // The padlock used to show for every URL, including plain http.
        const scheme = /^(https?):/i.test(url) ? url.slice(0, url.indexOf(':')).toLowerCase() : '';
        if (scheme) {
          const lockIcon = document.createElement('span');
          lockIcon.className = 'stage-browser-lock';
          lockIcon.textContent = scheme === 'https' ? '🔒' : '⚠';
          lockIcon.title = scheme === 'https' ? 'Secure connection (https)' : 'Not secure (http)';
          addressBar.appendChild(lockIcon);
        }
        addressBar.appendChild(urlText);

        const btnCopy = document.createElement('button');
        btnCopy.type = 'button';
        btnCopy.className = 'stage-browser-btn stage-browser-copy';
        btnCopy.title = 'Copy URL';
        btnCopy.setAttribute('aria-label', 'Copy URL');
        btnCopy.textContent = 'Copy';
        btnCopy.addEventListener('click', async () => {
          if (url) {
            await copyText(url);
            btnCopy.textContent = 'Copied';
            setTimeout(() => { btnCopy.textContent = 'Copy'; }, 1500);
          }
        });

        controls.appendChild(btnReload);
        controls.appendChild(addressBar);
        controls.appendChild(btnCopy);

        // The card header already has "Open ↗". A page that refuses embedding only
        // shows the browser's "refused to connect" page, which cannot be detected
        // here, so known blockers get an explicit notice below instead.
        const openHref = spec.srcdoc ? '' : openableUrl(url);

        if (!url && !spec.srcdoc) {
          const emptyEl = document.createElement('div');
          emptyEl.className = 'stage-browser-empty-state';
          emptyEl.style.cssText = 'display:flex;flex-direction:column;align-items:center;justify-content:center;height:calc(100% - 38px);color:var(--fg-muted,#9AA1B4);font-size:12px;gap:8px;padding:24px;text-align:center;';
          emptyEl.innerHTML = '<span style="font-size:24px;">🌐</span><span>Nothing loaded</span><span style="font-size:11px;color:var(--sds-color-text-subtle,#7E869E);">Enter a URL or launch a preview from a workflow run.</span>';
          body.appendChild(controls);
          body.appendChild(emptyEl);
          return body;
        }

        let frame = null;
        const mountFrame = () => {
          frame = document.createElement('iframe');
          frame.className = 'stage-iframe';
          if (spec.srcdoc) {
            frame.srcdoc = spec.srcdoc;
          } else {
            frame.src = url;
          }
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
          btnReload.disabled = false;
        };

        btnReload.addEventListener('click', () => {
          if (url && frame) { try { frame.src = url; } catch (_e) {} }
        });

        body.appendChild(controls);

        const blockedHost = spec.srcdoc ? null : framingBlockedHost(url);
        if (blockedHost) {
          btnReload.disabled = true;
          const note = document.createElement('div');
          note.className = 'stage-embed-blocked';
          note.setAttribute('role', 'status');

          const title = document.createElement('div');
          title.className = 'stage-embed-blocked-title';
          title.textContent = `${blockedHost} can’t be shown inside this page`;

          const text = document.createElement('div');
          text.className = 'stage-embed-blocked-text';
          text.textContent = 'The site tells browsers not to embed it (an X-Frame-Options or frame-ancestors header), '
            + 'so an embedded copy would only say “refused to connect”.';

          const actions = document.createElement('div');
          actions.className = 'stage-embed-blocked-actions';
          if (openHref) {
            const open = document.createElement('a');
            open.className = 'btn btn-accent btn-sm';
            open.href = openHref;
            open.target = '_blank';
            open.rel = 'noopener noreferrer';
            open.textContent = 'Open in new tab ↗';
            actions.appendChild(open);
          }
          const anyway = document.createElement('button');
          anyway.type = 'button';
          anyway.className = 'btn btn-sm';
          anyway.textContent = 'Try embedding anyway';
          anyway.addEventListener('click', () => {
            note.remove();
            mountFrame();
          });
          actions.appendChild(anyway);

          note.append(title, text, actions);
          body.appendChild(note);
          return body;
        }

        mountFrame();
        return body;
      }

      case 'handoff': {
        const wrap = document.createElement('div');
        wrap.className = 'stage-handoff-body';

        const heading = document.createElement('div');
        heading.className = 'stage-handoff-heading';

        const identity = document.createElement('div');
        identity.className = 'stage-handoff-identity';
        identity.innerHTML = `
          <div class="stage-handoff-agent-icon">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
              <rect x="2" y="2" width="12" height="12" rx="3" stroke="#C8CDDB" stroke-width="1.4"/>
              <circle cx="6" cy="7" r="1" fill="#C8CDDB"/>
              <circle cx="10" cy="7" r="1" fill="#C8CDDB"/>
              <path d="M5 11C5.5 11.5 6.5 12 8 12C9.5 12 10.5 11.5 11 11" stroke="#C8CDDB" stroke-width="1.2" stroke-linecap="round"/>
            </svg>
          </div>
          <span style="font-weight: 600; color: #E2E6F0; font-size: 13px;">${escapeHtml(spec.agent || 'Agent')}</span>
          <span style="color: var(--fg-muted); font-size: 11px;">just now</span>
        `;

        const status = document.createElement('div');
        status.className = 'stage-handoff-status';
        const statusText = spec.statusText || (spec.status ? String(spec.status) : 'Active');
        status.innerHTML = `
          <span class="status-dot"></span>
          <span style="font-size: 12px; color: var(--ok); font-weight: 500;">${escapeHtml(statusText)}</span>
        `;

        heading.appendChild(identity);
        heading.appendChild(status);

        const summary = document.createElement('div');
        summary.className = 'stage-handoff-summary';
        summary.textContent = spec.summary || 'No summary provided.';
        const changeLabel = document.createElement('div');
        changeLabel.className = 'stage-handoff-change-label';
        changeLabel.textContent = 'Request a change';

        const promptRow = document.createElement('form');
        promptRow.className = 'stage-handoff-prompt';
        promptRow.innerHTML = `
          <button type="button" class="stage-handoff-add-btn" title="Add context or file" aria-label="Add context">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path d="M7 2V12M2 7H12" stroke="#9AA1B4" stroke-width="1.6" stroke-linecap="round"/>
            </svg>
          </button>
          <input type="text" class="stage-handoff-input" placeholder="Ask agent to make a change..." aria-label="Ask agent to make a change" />
          <span class="stage-handoff-kbd font-mono">⌘ ↵</span>
          <button type="submit" class="stage-handoff-send" title="Send request" aria-label="Send request">
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
              <path d="M6 10V2M6 2L2 6M6 2L10 6" stroke="#FFFFFF" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </button>
        `;
        promptRow.addEventListener('submit', (e) => {
          e.preventDefault();
          const input = promptRow.querySelector('.stage-handoff-input');
          const val = input?.value?.trim();
          if (val) {
            emit({ type: 'handoff_prompt', text: val });
            if (input) input.value = '';
          }
        });

        wrap.appendChild(heading);
        wrap.appendChild(summary);
        wrap.appendChild(changeLabel);
        wrap.appendChild(promptRow);
        body.appendChild(wrap);
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

      // [StageTools] terminal + editor kinds
      case 'terminal': {
        return buildTerminalBody(body, spec, emit);
      }

      case 'editor': {
        return buildEditorBody(body, spec, emit);
      }

      case 'placeholder':
      default: {
        const custom = _viewBuilders.get(spec.kind);
        if (custom) {
          try {
            return custom(body, spec, emit);
          } catch (err) {
            body.textContent = '';
            return missing(body, spec.kind, `a working builder (${(err && err.message) || err})`);
          }
        }
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

  // [SaveExport] per-view actions (Download / Open / Copy)
  const LANG_EXTENSIONS = {
    python: '.py',
    py: '.py',
    javascript: '.js',
    js: '.js',
    typescript: '.ts',
    ts: '.ts',
    bash: '.sh',
    sh: '.sh',
    shell: '.sh',
    zsh: '.sh',
    html: '.html',
    htm: '.html',
    css: '.css',
    json: '.json',
    yaml: '.yaml',
    yml: '.yaml',
    sql: '.sql',
    rust: '.rs',
    rs: '.rs',
    go: '.go',
    c: '.c',
    cpp: '.cpp',
    'c++': '.cpp',
    ruby: '.rb',
    rb: '.rb',
    php: '.php',
    markdown: '.md',
    md: '.md',
    text: '.txt',
    txt: '.txt',
  };

  function downloadBlob(filename, text, mimeType = 'text/plain;charset=utf-8') {
    if (typeof Blob === 'undefined' || typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') {
      return;
    }
    const blob = new Blob([text], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => {
      try { URL.revokeObjectURL(url); } catch (_e) {}
    }, 1000);
  }

  function getDownloadFilename(spec) {
    if (spec.filename && typeof spec.filename === 'string' && spec.filename.trim()) {
      return spec.filename.trim();
    }
    const rawTitle = (spec.title || spec.kind || 'file')
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9._-]+/gi, '_')
      .replace(/^_+|_+$/g, '') || 'file';

    let ext = '.txt';
    if (spec.kind === 'markdown') {
      ext = '.md';
    } else if (spec.kind === 'html') {
      ext = '.html';
    } else if (spec.kind === 'code' || spec.kind === 'editor') {
      const lang = String(spec.language || '').toLowerCase().trim();
      ext = LANG_EXTENSIONS[lang] || '.txt';
    } else if (spec.kind === 'image') {
      ext = '.png';
      if (spec.url) {
        try {
          const base = (typeof window !== 'undefined' && window.location && window.location.href) || 'http://localhost/';
          const p = new URL(spec.url, base).pathname;
          const m = p.match(/\.(png|jpe?g|gif|webp|svg|bmp|ico)$/i);
          if (m) ext = m[0].toLowerCase();
        } catch (_e) {}
      }
    } else if (spec.kind === 'video') {
      ext = '.mp4';
      if (spec.url) {
        try {
          const base = (typeof window !== 'undefined' && window.location && window.location.href) || 'http://localhost/';
          const p = new URL(spec.url, base).pathname;
          const m = p.match(/\.(mp4|webm|ogg|mov|mkv)$/i);
          if (m) ext = m[0].toLowerCase();
        } catch (_e) {}
      }
    }

    if (rawTitle.toLowerCase().endsWith(ext)) {
      return rawTitle;
    }
    return `${rawTitle}${ext}`;
  }

  function getViewTextContent(spec) {
    if (spec.kind === 'markdown') {
      return spec.markdown !== undefined ? spec.markdown : spec.text;
    }
    if (spec.kind === 'html') {
      return typeof spec.html === 'string' ? spec.html : spec.text;
    }
    if (spec.kind === 'code') {
      return spec.text !== undefined ? spec.text : spec.code;
    }
    if (spec.kind === 'editor') {
      return spec.text !== undefined ? spec.text : (spec.code !== undefined ? spec.code : '');
    }
    if (typeof spec.text === 'string') {
      return spec.text;
    }
    return null;
  }

  function getViewUrl(spec) {
    if (spec.url && typeof spec.url === 'string') return spec.url;
    if (spec.kind === 'whiteboard') return spec.url || EXCALIDRAW_URL;
    return null;
  }

  function buildCardActions(spec) {
    const actions = document.createElement('div');
    actions.className = 'stage-card-actions';

    const textContent = getViewTextContent(spec);
    const viewUrl = getViewUrl(spec);

    // 1. Copy button (for text content or URL)
    // An editable note starts empty, so `textContent` is '' at build time; the
    // button must still appear and copy whatever the user has typed since.
    const copyTarget = typeof textContent === 'string' ? textContent : viewUrl;
    if (typeof copyTarget === 'string' && (copyTarget.length > 0 || spec.editable)) {
      const copyBtn = document.createElement('button');
      copyBtn.type = 'button';
      copyBtn.className = 'stage-card-action-btn stage-card-btn-copy';
      copyBtn.title = 'Copy to clipboard';
      copyBtn.setAttribute('aria-label', 'Copy to clipboard');
      copyBtn.textContent = 'Copy';
      copyBtn.addEventListener('click', async () => {
        const live = getViewTextContent(spec);
        const target = typeof live === 'string' ? live : (viewUrl || copyTarget);
        const ok = await copyText(target);
        copyBtn.textContent = ok ? 'Copied' : 'Failed';
        emit({ type: ok ? 'copy' : 'error', id: spec.__id, kind: spec.kind, message: ok ? 'copied' : 'copy failed' });
        setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500);
      });
      actions.appendChild(copyBtn);
    }

    // 2. Open in new tab (for anything with a URL)
    if (viewUrl) {
      const openBtn = document.createElement('button');
      openBtn.type = 'button';
      openBtn.className = 'stage-card-action-btn stage-card-btn-open';
      openBtn.title = 'Open in new tab';
      openBtn.setAttribute('aria-label', 'Open in new tab');
      openBtn.textContent = 'Open \u2197';
      openBtn.addEventListener('click', () => {
        try {
          if (typeof window !== 'undefined' && typeof window.open === 'function') {
            window.open(viewUrl, '_blank', 'noopener,noreferrer');
          }
          emit({ type: 'open_tab', id: spec.__id, kind: spec.kind, url: viewUrl });
        } catch (err) {
          emit({ type: 'error', id: spec.__id, kind: spec.kind, message: String(err) });
        }
      });
      actions.appendChild(openBtn);
    }

    // 3. Download button
    // "iframe / whiteboard -> not downloadable; hide the button rather than shipping a broken one"
    const isExcludedFromDownload = spec.kind === 'iframe' || spec.kind === 'whiteboard' || spec.kind === 'placeholder';
    if (!isExcludedFromDownload) {
      if (typeof textContent === 'string') {
        const dlBtn = document.createElement('button');
        dlBtn.type = 'button';
        dlBtn.className = 'stage-card-action-btn stage-card-btn-download';
        dlBtn.title = 'Download as file';
        dlBtn.setAttribute('aria-label', 'Download as file');
        dlBtn.textContent = 'Download';
        dlBtn.addEventListener('click', () => {
          const filename = getDownloadFilename(spec);
          const mime = spec.kind === 'html' ? 'text/html;charset=utf-8'
            : spec.kind === 'markdown' ? 'text/markdown;charset=utf-8'
            : 'text/plain;charset=utf-8';
          // Read the CURRENT content, not the snapshot from build time: an
          // editable note mutates spec.markdown/spec.text as the user types.
          const live = getViewTextContent(spec);
          const body = typeof live === 'string' ? live : textContent;
          downloadBlob(filename, body, mime);
          emit({ type: 'download', id: spec.__id, kind: spec.kind, filename });
        });
        actions.appendChild(dlBtn);
      } else if ((spec.kind === 'image' || spec.kind === 'video') && spec.url) {
        // "image / video -> offer the URL instead of a fake download"
        const dlBtn = document.createElement('button');
        dlBtn.type = 'button';
        dlBtn.className = 'stage-card-action-btn stage-card-btn-download';
        dlBtn.title = 'Download media';
        dlBtn.setAttribute('aria-label', 'Download media');
        dlBtn.textContent = 'Download';
        dlBtn.addEventListener('click', () => {
          const filename = getDownloadFilename(spec);
          const a = document.createElement('a');
          a.href = spec.url;
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          a.download = filename;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          emit({ type: 'download', id: spec.__id, kind: spec.kind, url: spec.url, filename });
        });
        actions.appendChild(dlBtn);
      }
    }

    return actions.children.length > 0 ? actions : null;
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

    // [ExcalidrawCanvas] fill mode: fillable kinds get a "Fill canvas" control
    let fillBtn = null;
    if (FILLABLE_KINDS.has(spec.kind)) {
      fillBtn = document.createElement('button');
      fillBtn.type = 'button';
      fillBtn.className = 'stage-card-fill' + (spec.kind === 'whiteboard' ? ' stage-card-fill-prominent' : '');
      fillBtn.title = spec.kind === 'whiteboard' ? 'Fill canvas (replaces stage with Excalidraw)' : 'Fill canvas';
      fillBtn.setAttribute('aria-label', 'Fill canvas');
      fillBtn.textContent = spec.kind === 'whiteboard' ? '\u26F6 Fill canvas' : '\u26F6 Fill';
      fillBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        fillView(spec.__id);
      });
    }

    const title = document.createElement('span');
    title.className = 'stage-card-title';
    title.textContent = titleFor(spec);

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'stage-card-close';
    close.setAttribute('aria-label', 'Close view');
    close.title = 'Close view';
    close.textContent = '\u00D7';
    close.addEventListener('click', () => {
      // A note's text lives only in its card: hand the host a copy it can offer back as Undo.
      const text = String(spec.markdown ?? spec.text ?? '');
      const undoable = spec.kind === 'markdown' && spec.editable && text.trim() !== '' ? { ...spec } : null;
      if (closeView(spec.__id) && undoable) emit({ type: 'close_undoable', id: spec.__id, spec: undoable });
    });

    titlebar.appendChild(kindBadge);
    if (fillBtn) titlebar.appendChild(fillBtn); // [ExcalidrawCanvas] fill mode
    titlebar.appendChild(title);
    const actions = buildCardActions(spec);
    if (actions) titlebar.appendChild(actions);
    titlebar.appendChild(close);

    // [ExcalidrawCanvas] fill mode: visible exit control in full-bleed mode
    const exitBtn = document.createElement('button');
    exitBtn.type = 'button';
    exitBtn.className = 'stage-card-exit-fill';
    exitBtn.setAttribute('aria-label', 'Exit fill mode');
    exitBtn.title = 'Exit fill canvas (Esc)';
    exitBtn.textContent = '\u2913 Exit fill';
    exitBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      exitFill();
    });
    card.appendChild(exitBtn);

    card.appendChild(titlebar);
    card.appendChild(buildBody(spec));

    // [Resize] Bottom-right corner handle. Dragging it resizes the card; the
    // titlebar keeps moving it and the canvas keeps panning/zooming.
    const resizeHandle = document.createElement('div');
    resizeHandle.className = 'stage-card-resize';
    resizeHandle.setAttribute('role', 'separator');
    resizeHandle.setAttribute('aria-label', 'Resize view');
    resizeHandle.title = 'Drag to resize';
    resizeHandle.dataset.viewId = spec.__id;
    card.appendChild(resizeHandle);

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
      const pos = (s.position && typeof s.position.x === 'number')
        ? { x: s.position.x, y: s.position.y }
        : (positions.get(s.__id) || getNextCardPosition(s.kind, s.__id));
      positions.set(s.__id, pos);
      replacement.style.left = `${pos.x}px`;
      replacement.style.top = `${pos.y}px`;
      setStorage(`uap.stage.pos.${s.__id}`, JSON.stringify(pos));
      const savedSize = sizes.get(s.__id) || loadCardSize(s.__id);
      if (savedSize) {
        sizes.set(s.__id, savedSize);
        applyCardSize(replacement, s.__id);
      }
      if (filledViewId === s.__id) replacement.classList.add('stage-card-filled');
      views.set(s.__id, { spec: s, cardEl: replacement });
      relayoutView(s.__id);
      // [SplitPane] A replacement card must re-enter its region: showView
      // re-parented it back onto the free canvas.
      splitRouteView(s.__id, s);
      emit({ type: 'update', id: s.__id, kind: s.kind, spec: s });
      return s.__id;
    }
    const card = buildCard(s);
    const pos = (s.position && typeof s.position.x === 'number')
      ? { x: s.position.x, y: s.position.y }
      : getNextCardPosition(s.kind, s.__id);
    positions.set(s.__id, pos);
    card.style.left = `${pos.x}px`;
    card.style.top = `${pos.y}px`;
    setStorage(`uap.stage.pos.${s.__id}`, JSON.stringify(pos));

    // [Resize] Restore a persisted size, if this view was resized before.
    const savedSize = (s.size && typeof s.size.w === 'number' && typeof s.size.h === 'number')
      ? { w: Math.max(MIN_CARD_W, Math.round(s.size.w)), h: Math.max(MIN_CARD_H, Math.round(s.size.h)) }
      : loadCardSize(s.__id);
    if (savedSize) {
      sizes.set(s.__id, savedSize);
      applyCardSize(card, s.__id);
      setStorage(`uap.stage.size.${s.__id}`, JSON.stringify(savedSize));
    }

    grid.appendChild(card);
    views.set(s.__id, { spec: s, cardEl: card });
    splitRouteView(s.__id, s);
    emit({ type: 'open', id: s.__id, kind: s.kind, spec: s });
    // [ExcalidrawCanvas] fill mode: enter fill mode if requested by spec
    if (s.fill === true) {
      fillView(s.__id);
    }
    updateEmptyState();
    return s.__id;
  }

  function closeView(id) {
    const targetId = String(id);
    const entry = views.get(targetId);
    if (!entry) return false;
    // [ExcalidrawCanvas] fill mode: if closing the filled view, reset fill state
    if (filledViewId === targetId) {
      exitFill();
    }
    entry.cardEl.remove();
    views.delete(targetId);
    positions.delete(targetId);
    sizes.delete(targetId);
    removeStorage(`uap.stage.pos.${targetId}`);
    removeStorage(`uap.stage.size.${targetId}`);
    splitRegions.delete(targetId);
    splitPositions.delete(targetId);
    removeStorage(`${SPLIT_KEY}.region.${targetId}`);
    removeStorage(`${SPLIT_KEY}.pos.${targetId}`);
    if (focusedViewId === targetId) focusedViewId = null;
    emit({ type: 'close', id: targetId });
    updateEmptyState();
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
    // [ExcalidrawCanvas] fill mode: reset fill state
    if (filledViewId) {
      exitFill();
    }
    for (const id of ids) {
      const entry = views.get(id);
      if (entry) entry.cardEl.remove();
      positions.delete(id);
      sizes.delete(id);
      removeStorage(`uap.stage.pos.${id}`);
      removeStorage(`uap.stage.size.${id}`);
      splitRegions.delete(id);
      splitPositions.delete(id);
      removeStorage(`${SPLIT_KEY}.region.${id}`);
      removeStorage(`${SPLIT_KEY}.pos.${id}`);
    }
    views.clear();
    focusedViewId = null;
    emit({ type: 'clear', ids });
    updateEmptyState();
    return ids.length;
  }

  function onEvent(cb) {
    if (typeof cb !== 'function') return () => {};
    listeners.add(cb);
    return () => listeners.delete(cb);
  }

  // [InfiniteCanvas] Viewport and positioning API
  function getViewport() {
    return { ...viewport };
  }

  function setViewport(next) {
    if (!next) return;
    if (typeof next.zoom === 'number') {
      viewport.zoom = Math.min(Math.max(next.zoom, MIN_ZOOM), MAX_ZOOM);
    }
    if (typeof next.x === 'number') viewport.x = Math.round(next.x);
    if (typeof next.y === 'number') viewport.y = Math.round(next.y);
    applyViewport();
    saveViewport();
    updateZoomDisplay();
  }

  function getCardPosition(id) {
    const p = positions.get(String(id));
    return p ? { ...p } : null;
  }

  function setCardPosition(id, x, y) {
    const targetId = String(id);
    const pos = { x: Math.round(x), y: Math.round(y) };
    positions.set(targetId, pos);
    setStorage(`uap.stage.pos.${targetId}`, JSON.stringify(pos));
    const entry = views.get(targetId);
    if (entry) {
      entry.cardEl.style.left = `${pos.x}px`;
      entry.cardEl.style.top = `${pos.y}px`;
    }
  }

  function destroy() {
    disconnectCanvas();
    if (typeof window !== 'undefined') {
      window.removeEventListener('mousemove', onWindowMouseMove);
      window.removeEventListener('mouseup', onWindowMouseUp);
      window.removeEventListener('pagehide', disconnectCanvas);
    }
    if (typeof document !== 'undefined') {
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('keyup', onKeyUp);
    }
  }

  const api = {
    showView,
    closeView,
    listViews,
    clear,
    onEvent,
    fillView,
    exitFill,
    getFilledViewId,
    // [InfiniteCanvas]
    getViewport,
    setViewport,
    zoomIn,
    zoomOut,
    fitToView,
    resetView,
    getCardPosition,
    setCardPosition,
    // [Resize]
    getCardSize,
    setCardSize,
    // [SplitPane]
    setSplitMode,
    getSplitMode,
    setSplitRatio,
    setViewRegion,
    getViewRegion,
    // [CanvasWS]
    connectCanvas,
    disconnectCanvas,
    sendHello,
    focusView,
    getFocusedViewId,
    setCanvasFileOpener,
    destroy,
    updateEmptyState,
    getEmptyStateElement: () => guideEl,
    // [StageGuide] persistence helpers so a host can offer "show guide again".
    isGuideDismissed: guideDismissed,
    suppressGuide,
    resetGuide: () => {
      removeStorage(GUIDE_DISMISS_KEY);
      updateEmptyState();
    },
    // Show the guide on demand, over open cards and after "Don't show again".
    showGuide: () => {
      removeStorage(GUIDE_DISMISS_KEY);
      guideSuppressed = false;
      if (guideEl) guideEl.style.display = 'flex';
      refreshRegionEmptiness();
    },
    arrangeCards,
  };
  containerEl.__stage = api;
  if (typeof window !== 'undefined') {
    window.stage = api;
    window.stageApi = api;
  }
  // [SplitPane] restore a persisted layout around the views index.html opens
  updateEmptyState();
  if (splitMode) {
    buildSplitChrome();
    layoutSplitCards();
  }
  // [CanvasWS] the owning page (index.html) wires the one socket per tab, not
  // the constructor: no socket is opened under SSR / a test harness that
  // defines WebSocket but has no server.
  return api;
}

export default createStage;
