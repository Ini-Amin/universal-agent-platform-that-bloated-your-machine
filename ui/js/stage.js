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
  terminal: 'Terminal',
  editor: 'Editor',
  placeholder: 'View',
};

// Verified by loading it in a real browser before wiring it here: the page
// returns two <canvas> elements (static + interactive) and sends no
// X-Frame-Options / CSP frame-ancestors, so it renders inside an <iframe>.
export const EXCALIDRAW_URL = 'https://excalidraw.com/';

export const SEARCH_STATUS_MARKDOWN = `# 🔍 Knowledge & Web Search

> **Search Provider Not Configured**
> No search API endpoint or MCP search server is currently available on the server.

### What Was Checked
- **Backend API Routes**: The server provides \`/tasks\`, \`/events\`, \`/api/executions\`, \`/api/sandbox/run\`, but no \`/api/search\` route exists in the API specification.
- **Registered Tools (\`GET /api/resources/tools\`)**: 14 tools registered (local file tools and 11 \`bugbounty-mcp\` security tools). No search provider (e.g., \`exa.search\`, \`brave.search\`, or \`tavily.search\`) is registered.
- **Research Workflow (\`src/uap/workflows/research.py\`)**: The research workflow attempts to resolve:
  1. An MCP tool matching \`*.search\` or containing \`search\`
  2. An HTTP search client (Brave/Exa API key)
  3. Falls back to deterministic simulation stubs (\`stub:web\`)

### How to Enable User-Driven Search
To allow users to search directly from the canvas, a backend route (e.g. \`POST /api/search\` or \`GET /api/search?q=...\`) or an MCP search server must be configured.`;

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

// ---------------------------------------------------------------------------
// [StageTools] terminal + editor helpers
// ---------------------------------------------------------------------------

export function ansiToHtml(str) {
  if (!str) return '';

  const fgColors = {
    30: '#484f58', 31: '#ff7b72', 32: '#3fb950', 33: '#d29922',
    34: '#58a6ff', 35: '#bc8cff', 36: '#39c5cf', 37: '#d1d5db',
    90: '#6e7681', 91: '#ffa198', 92: '#56d364', 93: '#e3b341',
    94: '#79c0ff', 95: '#d2a8ff', 96: '#56d4dd', 97: '#f0f6fc',
  };
  const bgColors = {
    40: '#161b22', 41: '#4c1517', 42: '#144620', 43: '#4d3800',
    44: '#0c2d6b', 45: '#3c1e6e', 46: '#154f57', 47: '#8b949e',
    100: '#21262d', 101: '#6e1d24', 102: '#1b632d', 103: '#6e5100',
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
  statusEl.textContent = 'Markdown Note';

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

  container.appendChild(toolbar);
  container.appendChild(buffer);
  container.appendChild(inputForm);
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

  if (spec.initialText) {
    appendOutput(spec.initialText.endsWith('\n') ? spec.initialText : spec.initialText + '\n');
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

export function createStage(containerEl) {
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
  const FILLABLE_KINDS = new Set(['whiteboard', 'iframe', 'video', 'image']);

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

  function saveViewport() {
    setStorage('uap.stage.viewport', JSON.stringify(viewport));
    emit({ type: 'viewport', viewport: { ...viewport } });
  }

  function applyViewport() {
    if (filledViewId) return; // Fill mode takes over whole container
    grid.style.transform = `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.zoom})`;
    grid.style.transformOrigin = '0 0';
    const bgSize = Math.max(12, Math.round(24 * viewport.zoom));
    containerEl.style.backgroundPosition = `${viewport.x}px ${viewport.y}px`;
    containerEl.style.backgroundSize = `${bgSize}px ${bgSize}px`;
  }

  function getCardWidth(kind) {
    if (kind === 'whiteboard' || kind === 'iframe') return 580;
    if (kind === 'terminal' || kind === 'editor') return 540;
    return 440;
  }

  function getCardHeight(kind) {
    if (kind === 'whiteboard' || kind === 'iframe') return 480;
    if (kind === 'terminal' || kind === 'editor') return 440;
    return 380;
  }

  // [InfiniteCanvas] Deterministic placement rule:
  // Loose grid (3 cols, cell 500x440, gap 24px) starting at (40, 40).
  // Finds the first slot where the card's bounding box does not overlap any existing card.
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

    const cardW = getCardWidth(kind);
    const cardH = getCardHeight(kind);
    const cellW = 500;
    const cellH = 440;
    const gap = 24;
    const cols = 3;
    const originX = 40;
    const originY = 40;

    for (let slot = 0; slot < 1000; slot++) {
      const col = slot % cols;
      const row = Math.floor(slot / cols);
      const candX = originX + col * (cellW + gap);
      const candY = originY + row * (cellH + gap);

      let collides = false;
      for (const [existingId, pos] of positions.entries()) {
        if (existingId === id) continue;
        const otherW = 440;
        const otherH = 380;
        const overlapX = candX < pos.x + otherW && candX + cardW > pos.x;
        const overlapY = candY < pos.y + otherH && candY + cardH > pos.y;
        if (overlapX && overlapY) {
          collides = true;
          break;
        }
      }

      if (!collides) {
        return { x: candX, y: candY };
      }
    }
    return { x: originX, y: originY };
  }

  // [InfiniteCanvas] Floating zoom controls UI
  const zoomControls = document.createElement('div');
  zoomControls.className = 'stage-zoom-controls';
  zoomControls.setAttribute('role', 'toolbar');
  zoomControls.setAttribute('aria-label', 'Canvas zoom controls');
  zoomControls.innerHTML = `
    <button type="button" class="stage-zoom-btn stage-zoom-out" data-action="zoom-out" title="Zoom Out" aria-label="Zoom Out">−</button>
    <button type="button" class="stage-zoom-level" data-action="zoom-reset" title="Reset zoom to 100%" aria-label="Reset zoom">${Math.round(viewport.zoom * 100)}%</button>
    <button type="button" class="stage-zoom-btn stage-zoom-in" data-action="zoom-in" title="Zoom In" aria-label="Zoom In">+</button>
    <button type="button" class="stage-zoom-btn stage-zoom-fit" data-action="zoom-fit" title="Fit all cards in view" aria-label="Fit to view">Fit</button>
  `;
  containerEl.appendChild(zoomControls);

  // [ToolWorkbench] Empty stage discoverability guide
  const emptyStateEl = document.createElement('div');
  emptyStateEl.className = 'stage-empty-state';
  emptyStateEl.setAttribute('role', 'region');
  emptyStateEl.setAttribute('aria-label', 'Workbench guide');
  emptyStateEl.innerHTML = `
    <div class="stage-empty-content">
      <div class="stage-empty-badge">✦ WORKBENCH CANVAS</div>
      <h2 class="stage-empty-title">Your interactive workspace is ready</h2>
      <p class="stage-empty-desc">
        Run a task above to have agents work here, or launch tools directly onto the canvas to draw, code, explore, and take notes.
      </p>
      <div class="stage-empty-actions">
        <button type="button" class="stage-empty-tool-btn" data-tool="whiteboard">
          <span class="stage-empty-icon">🎨</span>
          <span class="stage-empty-info">
            <span class="stage-empty-name">Whiteboard</span>
            <span class="stage-empty-sub">Excalidraw</span>
          </span>
        </button>
        <button type="button" class="stage-empty-tool-btn" data-tool="terminal">
          <span class="stage-empty-icon">💻</span>
          <span class="stage-empty-info">
            <span class="stage-empty-name">Terminal</span>
            <span class="stage-empty-sub">Shell session</span>
          </span>
        </button>
        <button type="button" class="stage-empty-tool-btn" data-tool="editor">
          <span class="stage-empty-icon">📝</span>
          <span class="stage-empty-info">
            <span class="stage-empty-name">Code Editor</span>
            <span class="stage-empty-sub">Python sandbox</span>
          </span>
        </button>
        <button type="button" class="stage-empty-tool-btn" data-tool="note">
          <span class="stage-empty-icon">📄</span>
          <span class="stage-empty-info">
            <span class="stage-empty-name">Note</span>
            <span class="stage-empty-sub">Markdown</span>
          </span>
        </button>
        <button type="button" class="stage-empty-tool-btn" data-tool="search">
          <span class="stage-empty-icon">🔍</span>
          <span class="stage-empty-info">
            <span class="stage-empty-name">Search</span>
            <span class="stage-empty-sub">Web & knowledge</span>
          </span>
        </button>
        <button type="button" class="stage-empty-tool-btn" data-tool="iframe">
          <span class="stage-empty-icon">🌐</span>
          <span class="stage-empty-info">
            <span class="stage-empty-name">Web Page</span>
            <span class="stage-empty-sub">Embed URL</span>
          </span>
        </button>
        <button type="button" class="stage-empty-tool-btn" data-tool="video">
          <span class="stage-empty-icon">🎬</span>
          <span class="stage-empty-info">
            <span class="stage-empty-name">Video</span>
            <span class="stage-empty-sub">Media player</span>
          </span>
        </button>
        <button type="button" class="stage-empty-tool-btn" data-tool="image">
          <span class="stage-empty-icon">🖼️</span>
          <span class="stage-empty-info">
            <span class="stage-empty-name">Image</span>
            <span class="stage-empty-sub">Image viewer</span>
          </span>
        </button>
      </div>
      <div class="stage-empty-footer">
        <span class="stage-empty-hint">Tip: Click <strong>+ Tools</strong> in the toolbar above anytime to add views.</span>
      </div>
    </div>
  `;
  containerEl.appendChild(emptyStateEl);

  function updateEmptyState() {
    if (emptyStateEl) {
      emptyStateEl.style.display = views.size === 0 ? 'flex' : 'none';
    }
  }

  emptyStateEl.querySelectorAll('.stage-empty-tool-btn').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const tool = btn.getAttribute('data-tool');
      emit({ type: 'empty_tool_click', tool });
      if (tool === 'whiteboard') {
        showView({ kind: 'whiteboard', title: 'Whiteboard' });
      } else if (tool === 'terminal') {
        showView({ kind: 'terminal', title: 'Terminal' });
      } else if (tool === 'editor') {
        showView({ kind: 'editor', title: 'Code Editor', language: 'python', filename: 'main.py', code: '# Python Sandbox\\nprint("Hello from UAP!")\\n' });
      } else if (tool === 'note') {
        showView({ kind: 'markdown', title: 'Note', markdown: '', editable: true });
      } else if (tool === 'search') {
        showView({ kind: 'markdown', id: 'tool-search', title: 'Search (Not Configured)', markdown: SEARCH_STATUS_MARKDOWN });
      } else {
        emit({ type: 'request_tool_input', tool });
      }
    });
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
      if (prev) prev.cardEl.classList.remove('stage-card-filled');
    }
    filledViewId = targetId;
    containerEl.classList.add('stage-fill-active');
    entry.cardEl.classList.add('stage-card-filled');
    emit({ type: 'fill', id: targetId, kind: entry.spec.kind, filled: true });
    return true;
  }

  function exitFill() {
    if (!filledViewId) return false;
    const currentId = filledViewId;
    const entry = views.get(currentId);
    if (entry) entry.cardEl.classList.remove('stage-card-filled');
    filledViewId = null;
    containerEl.classList.remove('stage-fill-active');
    applyViewport(); // restore infinite plane transform
    emit({ type: 'fill', id: currentId, kind: entry ? entry.spec.kind : null, filled: false });
    return true;
  }

  function getFilledViewId() {
    return filledViewId;
  }

  // [InfiniteCanvas] Wheel zoom handler
  containerEl.addEventListener('wheel', (e) => {
    if (filledViewId) return;
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

      const pos = positions.get(viewId) || { x: card.offsetLeft || 0, y: card.offsetTop || 0 };
      draggingCard = {
        id: viewId,
        cardEl: card,
        startX: e.clientX,
        startY: e.clientY,
        cardX: pos.x,
        cardY: pos.y,
      };
      hasCardMoved = false;
      containerEl.classList.add('stage-dragging');
      e.preventDefault();
      return;
    }

    // 2. Pan canvas
    const isInsideCard = Boolean(e.target.closest('.stage-card'));
    const isInsideControls = Boolean(e.target.closest('.stage-zoom-controls'));
    if (isInsideControls) return;

    if (!isInsideCard || e.button === 1 || isSpaceDown) {
      if (e.button === 0 || e.button === 1) {
        isPanning = true;
        panStart = { x: e.clientX, y: e.clientY };
        containerEl.classList.add('stage-panning');
        e.preventDefault();
      }
    }
  });

  const onWindowMouseMove = (e) => {
    if (draggingCard) {
      hasCardMoved = true;
      const dx = (e.clientX - draggingCard.startX) / viewport.zoom;
      const dy = (e.clientY - draggingCard.startY) / viewport.zoom;
      const newX = Math.round(draggingCard.cardX + dx);
      const newY = Math.round(draggingCard.cardY + dy);
      positions.set(draggingCard.id, { x: newX, y: newY });
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

  const onWindowMouseUp = () => {
    if (draggingCard) {
      containerEl.classList.remove('stage-dragging');
      const pos = positions.get(draggingCard.id);
      if (pos) {
        setStorage(`uap.stage.pos.${draggingCard.id}`, JSON.stringify(pos));
        if (hasCardMoved) {
          emit({ type: 'move', id: draggingCard.id, position: pos });
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

      // [StageTools] terminal + editor kinds
      case 'terminal': {
        return buildTerminalBody(body, spec, emit);
      }

      case 'editor': {
        return buildEditorBody(body, spec, emit);
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
    close.addEventListener('click', () => closeView(spec.__id));

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
      if (filledViewId === s.__id) replacement.classList.add('stage-card-filled');
      views.set(s.__id, { spec: s, cardEl: replacement });
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

    grid.appendChild(card);
    views.set(s.__id, { spec: s, cardEl: card });
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
    removeStorage(`uap.stage.pos.${targetId}`);
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
      removeStorage(`uap.stage.pos.${id}`);
    }
    views.clear();
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
    if (typeof window !== 'undefined') {
      window.removeEventListener('mousemove', onWindowMouseMove);
      window.removeEventListener('mouseup', onWindowMouseUp);
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
    destroy,
    updateEmptyState,
    getEmptyStateElement: () => emptyStateEl,
  };
  containerEl.__stage = api;
  if (typeof window !== 'undefined') {
    window.stage = api;
    window.stageApi = api;
  }
  updateEmptyState();
  return api;
}

export default createStage;
