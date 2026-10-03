// Real code editor for the canvas stage (§Editor).
//
// The stage's editor view used to be a <textarea> with a hand-drawn gutter.
// This module replaces it with a Monaco editor loaded from the CDN via the
// standard AMD loader (deliberately NOT bundled: the platform adds no build
// step and no new npm dependency).
//
// Two things are non-negotiable:
//
//   1. Honest degradation. If the CDN is unreachable (offline, blocked, slow)
//      the view falls back to the previous textarea editor AND says so in the
//      status bar. It is never a blank panel.
//   2. No silent feature claims. The status bar reports which engine is
//      actually live ("Monaco" vs "Plain textarea"), so a reviewer can tell
//      what they are looking at without opening devtools.
//
// It also makes the editor useful for a real project:
//
//   * a file browser that lists the workspace (GET /api/workspace/files),
//     opens a file (GET /api/workspace/file) and saves it back (POST
//     /api/workspace/file) -- a real write, not a download;
//   * multiple browsable roots (GET /api/workspace/roots): the browser header
//     holds a root picker next to the breadcrumb, every workspace/editor
//     request carries the active root, and the choice persists in
//     localStorage; switching roots resets navigation and editor context;
//   * "Open in Zed": Zed is a native GUI app and cannot be embedded in a
//     browser, so the server launches it on its own machine (POST
//     /api/editor/open); availability comes from GET /api/editor/status;
//   * Run, which executes the buffer through POST /api/sandbox/run.

export const MONACO_VERSION = '0.52.0';
export const MONACO_LOADER_URL =
  `https://cdn.jsdelivr.net/npm/monaco-editor@${MONACO_VERSION}/min/vs/loader.js`;
export const MONACO_VS_URL =
  `https://cdn.jsdelivr.net/npm/monaco-editor@${MONACO_VERSION}/min/vs`;

// spec.language -> Monaco language id. Unknown languages become 'plaintext'
// (Monaco's id for no highlighting) rather than guessing.
export const LANGUAGE_MAP = {
  python: 'python',
  py: 'python',
  javascript: 'javascript',
  js: 'javascript',
  jsx: 'javascript',
  typescript: 'typescript',
  ts: 'typescript',
  tsx: 'typescript',
  json: 'json',
  markdown: 'markdown',
  md: 'markdown',
  html: 'html',
  xml: 'xml',
  css: 'css',
  scss: 'scss',
  less: 'less',
  shell: 'shell',
  sh: 'shell',
  bash: 'shell',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'ini',
  ini: 'ini',
  sql: 'sql',
  rust: 'rust',
  go: 'go',
  java: 'java',
  c: 'c',
  h: 'c',
  cpp: 'cpp',
  'c++': 'cpp',
  cs: 'csharp',
  ruby: 'ruby',
  rb: 'ruby',
  php: 'php',
  lua: 'lua',
  dockerfile: 'dockerfile',
  text: 'plaintext',
  plaintext: 'plaintext',
};

/** Map a `spec.language` value to a Monaco language id; unknown -> plaintext. */
export function monacoLanguageId(language) {
  if (language === null || language === undefined) return 'plaintext';
  const key = String(language).trim().toLowerCase();
  return LANGUAGE_MAP[key] || 'plaintext';
}

const EXTENSION_LANGUAGE = {
  py: 'python',
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  jsx: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  json: 'json',
  md: 'markdown',
  markdown: 'markdown',
  html: 'html',
  htm: 'html',
  xml: 'xml',
  css: 'css',
  scss: 'scss',
  sh: 'shell',
  bash: 'shell',
  zsh: 'shell',
  yml: 'yaml',
  yaml: 'yaml',
  toml: 'ini',
  ini: 'ini',
  cfg: 'ini',
  sql: 'sql',
  rs: 'rust',
  go: 'go',
  java: 'java',
  c: 'c',
  h: 'c',
  cpp: 'cpp',
  cc: 'cpp',
  hpp: 'cpp',
  cs: 'csharp',
  rb: 'ruby',
  php: 'php',
  lua: 'lua',
  txt: 'plaintext',
};

/** Best-effort language for a file path, for syntax highlighting. */
export function languageFromPath(path) {
  if (!path) return 'plaintext';
  const name = String(path).split('/').pop() || '';
  if (name.toLowerCase() === 'dockerfile') return 'dockerfile';
  const dot = name.lastIndexOf('.');
  if (dot < 0) return 'plaintext';
  const ext = name.slice(dot + 1).toLowerCase();
  return EXTENSION_LANGUAGE[ext] || 'plaintext';
}

// ---------------------------------------------------------------------------
// Monaco AMD loader (no bundling)
// ---------------------------------------------------------------------------

let _monacoPromise = null;

function _injectScript(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement('script');
    el.src = src;
    el.async = true;
    el.onload = () => resolve();
    el.onerror = () => reject(new Error(`could not load script: ${src}`));
    document.head.appendChild(el);
  });
}

/**
 * Load Monaco through the AMD loader. Resolves with the `monaco` namespace, or
 * rejects with a human-readable reason (offline / blocked / timeout). The
 * failure is cached so a broken CDN does not retry on every keystroke.
 */
export async function loadMonaco({
  loaderUrl = MONACO_LOADER_URL,
  vsUrl = MONACO_VS_URL,
  timeoutMs = 12000,
} = {}) {
  if (typeof window !== 'undefined' && window.monaco && window.monaco.editor) {
    return window.monaco;
  }
  if (_monacoPromise) return _monacoPromise;

  _monacoPromise = new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error('timed out loading Monaco from the CDN'));
      }
    }, timeoutMs);
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn(value);
    };

    const requireEditor = () => {
      try {
        window.require.config({ paths: { vs: vsUrl } });
        window.require(
          ['vs/editor/editor.main'],
          () => {
            if (window.monaco && window.monaco.editor) finish(resolve, window.monaco);
            else finish(reject, new Error('Monaco loaded but window.monaco is missing'));
          },
          (err) => finish(reject, new Error(`Monaco editor.main failed to load: ${(err && err.message) || err}`)),
        );
      } catch (err) {
        finish(reject, err instanceof Error ? err : new Error(String(err)));
      }
    };

    if (typeof window !== 'undefined' && typeof window.require === 'function') {
      requireEditor();
      return;
    }
    _injectScript(loaderUrl).then(requireEditor).catch((err) => finish(reject, err));
  });

  // A rejected promise must not poison a later retry (e.g. network restored).
  _monacoPromise.catch(() => {
    _monacoPromise = null;
  });
  return _monacoPromise;
}

// ---------------------------------------------------------------------------
// Small HTTP helpers (token-aware, same key as js/api.js)
// ---------------------------------------------------------------------------

function _authHeaders() {
  const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
  try {
    const token = localStorage.getItem('uap_api_token');
    if (token) headers.Authorization = `Bearer ${token}`;
  } catch (_e) {
    /* storage disabled: unauthenticated request is still correct when auth is off */
  }
  return headers;
}

// The active workspace root lives in localStorage so the browser reopens on the
// root the user left it on. The id (never the absolute path) is stored, so a
// stale selection is simply discarded when the server no longer offers it.
const ROOT_STORAGE_KEY = 'uap_workspace_root';
const DEFAULT_ROOT_ID = 'workspace';

function _readPersistedRoot() {
  try {
    return localStorage.getItem(ROOT_STORAGE_KEY) || DEFAULT_ROOT_ID;
  } catch (_e) {
    return DEFAULT_ROOT_ID;
  }
}

function _persistRoot(root) {
  try {
    localStorage.setItem(ROOT_STORAGE_KEY, root);
  } catch (_e) {
    /* storage disabled: the selection stays session-only */
  }
}

async function _jsonRequest(path, options = {}) {
  const res = await fetch(path, { headers: _authHeaders(), ...options });
  let body = null;
  const contentType = res.headers ? res.headers.get('content-type') || '' : '';
  if (contentType.includes('application/json')) {
    body = await res.json().catch(() => null);
  } else {
    body = await res.text().catch(() => '');
  }
  if (!res.ok) {
    const detail = body && typeof body === 'object' ? body.detail : body;
    const err = new Error(`HTTP ${res.status}: ${detail || res.statusText}`);
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  return body;
}

/** Thin wrappers so callers (and tests) see the exact route shape. */
export const workspaceApi = {
  roots: () => _jsonRequest('/api/workspace/roots'),
  list: (path = '', root) =>
    _jsonRequest(
      `/api/workspace/files?path=${encodeURIComponent(path)}${
        root ? `&root=${encodeURIComponent(root)}` : ''
      }`,
    ),
  read: (path, root) =>
    _jsonRequest(
      `/api/workspace/file?path=${encodeURIComponent(path)}${
        root ? `&root=${encodeURIComponent(root)}` : ''
      }`,
    ),
  write: (path, content, root) =>
    _jsonRequest('/api/workspace/file', {
      method: 'POST',
      body: JSON.stringify(root ? { path, content, root } : { path, content }),
    }),
  editorStatus: () => _jsonRequest('/api/editor/status'),
  openInEditor: (path, root) =>
    _jsonRequest('/api/editor/open', {
      method: 'POST',
      body: JSON.stringify(root ? { path, root } : { path }),
    }),
};

// ---------------------------------------------------------------------------
// The view
// ---------------------------------------------------------------------------

const DEFAULT_PYTHON = '# Python Sandbox\nprint("Hello from UAP!")\n';

/**
 * Build the editor view. Returns synchronously with a complete shell; Monaco
 * (or its textarea fallback) is attached asynchronously. The returned body
 * exposes `body.__editor` for programmatic access and tests.
 *
 * Card API (what other modules should use):
 *   const body = buildEditorBody(hostEl, spec, emit);
 *   body.__editor.openFile(path, root?)  // open a file, optionally switching
 *                                        // to `root` first (see the roots list)
 *   body.__editor.getRoot() / setRoot(id)
 * Window hooks with the same call shape: `window.UAP.openFile(path, root?)`
 * and the canvas-bridge seam `window.uapOpenWorkspaceFile(path, root?)`.
 */
export function buildEditorBody(body, spec = {}, emit = () => {}) {
  body.classList.add('stage-editor-body');

  const container = document.createElement('div');
  container.className = 'stage-editor-container';

  // -- toolbar --------------------------------------------------------------
  const toolbar = document.createElement('div');
  toolbar.className = 'stage-editor-toolbar';

  const metaGroup = document.createElement('div');
  metaGroup.className = 'stage-editor-meta';

  const filenameEl = document.createElement('span');
  filenameEl.className = 'stage-editor-filename';

  const langEl = document.createElement('span');
  langEl.className = 'stage-editor-lang';

  metaGroup.appendChild(filenameEl);
  metaGroup.appendChild(langEl);

  const actionsGroup = document.createElement('div');
  actionsGroup.className = 'stage-editor-actions';

  const filesBtn = document.createElement('button');
  filesBtn.type = 'button';
  filesBtn.className = 'stage-editor-btn stage-editor-files-btn';
  filesBtn.textContent = '📁 Files';
  filesBtn.title = 'Show or hide the workspace file browser';

  const zedBtn = document.createElement('button');
  zedBtn.type = 'button';
  zedBtn.className = 'stage-editor-btn stage-editor-zed-btn';
  zedBtn.textContent = '⌘ Open in Zed';
  zedBtn.title = 'Open this file in the Zed editor on the server machine';
  zedBtn.disabled = true;

  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'stage-editor-btn stage-editor-save-btn';
  saveBtn.textContent = '💾 Save';
  saveBtn.title = 'Save the buffer to the workspace (or download it if unsaved)';

  const runBtn = document.createElement('button');
  runBtn.type = 'button';
  runBtn.className = 'stage-editor-btn stage-editor-run-btn';
  runBtn.textContent = '▶ Run';
  runBtn.title = 'Execute the code against the backend sandbox';

  actionsGroup.appendChild(filesBtn);
  actionsGroup.appendChild(zedBtn);
  actionsGroup.appendChild(saveBtn);
  actionsGroup.appendChild(runBtn);

  toolbar.appendChild(metaGroup);
  toolbar.appendChild(actionsGroup);

  // -- workspace: file browser + editor host --------------------------------
  const workspace = document.createElement('div');
  workspace.className = 'stage-editor-workspace';

  const filePanel = document.createElement('aside');
  filePanel.className = 'stage-editor-files';
  filePanel.setAttribute('aria-label', 'Workspace files');

  const filePanelHeader = document.createElement('div');
  filePanelHeader.className = 'stage-editor-files-header';
  const upBtn = document.createElement('button');
  upBtn.type = 'button';
  upBtn.className = 'stage-editor-files-up';
  upBtn.textContent = '↑';
  upBtn.title = 'Up one directory';
  const rootSelect = document.createElement('select');
  rootSelect.className = 'stage-editor-root-select';
  rootSelect.setAttribute('aria-label', 'Workspace root');
  rootSelect.title = 'Browse a different workspace root';
  // Styled inline against the app's design tokens (ui/css/app.css is not this
  // module's to edit); it sits in the 210px browser header next to the crumb.
  rootSelect.style.cssText =
    'background: var(--panel-3); color: var(--fg); border: 1px solid var(--border);' +
    'border-radius: 4px; font-family: var(--font-mono); font-size: 11px;' +
    'max-width: 82px; min-width: 0; padding: 1px 2px;';
  const crumb = document.createElement('span');
  crumb.className = 'stage-editor-files-crumb';
  crumb.textContent = '/';
  const refreshBtn = document.createElement('button');
  refreshBtn.type = 'button';
  refreshBtn.className = 'stage-editor-files-refresh';
  refreshBtn.textContent = '⟳';
  refreshBtn.title = 'Refresh listing';
  filePanelHeader.appendChild(upBtn);
  filePanelHeader.appendChild(rootSelect);
  filePanelHeader.appendChild(crumb);
  filePanelHeader.appendChild(refreshBtn);

  const fileList = document.createElement('div');
  fileList.className = 'stage-editor-files-list';
  fileList.setAttribute('role', 'listbox');
  fileList.setAttribute('aria-label', 'Workspace file list');

  filePanel.appendChild(filePanelHeader);
  filePanel.appendChild(fileList);

  const editorHost = document.createElement('div');
  editorHost.className = 'stage-editor-host';
  editorHost.setAttribute('role', 'textbox');
  editorHost.setAttribute('aria-label', 'Code editor');

  workspace.appendChild(filePanel);
  workspace.appendChild(editorHost);

  // -- status bar -----------------------------------------------------------
  const statusbar = document.createElement('div');
  statusbar.className = 'stage-editor-statusbar';

  const posEl = document.createElement('span');
  posEl.className = 'stage-editor-pos';
  posEl.textContent = 'Ln 1, Col 1';

  const engineEl = document.createElement('span');
  engineEl.className = 'stage-editor-engine';
  engineEl.textContent = 'Loading editor…';

  const statusMsg = document.createElement('span');
  statusMsg.className = 'stage-editor-status-msg';
  statusMsg.textContent = 'Ready';

  statusbar.appendChild(posEl);
  statusbar.appendChild(engineEl);
  statusbar.appendChild(statusMsg);

  // -- output drawer --------------------------------------------------------
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

  // -- state ----------------------------------------------------------------
  const initialCode =
    spec.code !== undefined ? spec.code : spec.text !== undefined ? spec.text : '';
  let fallbackCode = initialCode;
  let currentPath = typeof spec.path === 'string' && spec.path ? spec.path : null;
  let currentFilename =
    spec.filename || (currentPath ? currentPath.split('/').pop() : spec.language === 'javascript' ? 'script.js' : 'script.py');
  let language = spec.language || (currentPath ? languageFromPath(currentPath) : 'python');
  let engine = 'pending'; // 'monaco' | 'textarea'
  let monacoEditor = null;
  let textarea = null;
  let dirty = false;
  let browsePath = '';
  // Active workspace root: restored from localStorage, validated against the
  // server's root list. `knownRoots` is empty while loading (and stays empty
  // if the server cannot list roots), in which case the persisted id is shown
  // as the only option and requests still carry it.
  let activeRoot = _readPersistedRoot();
  const knownRoots = new Map(); // id -> label
  let rootsLoaded = false;

  function updateMeta() {
    filenameEl.textContent = currentFilename + (dirty ? ' •' : '');
    langEl.textContent = language;
  }
  updateMeta();

  function setStatus(msg) {
    statusMsg.textContent = msg;
  }

  // -- editor engine: Monaco, else an honest textarea fallback ---------------
  function createTextareaFallback(reason) {
    engine = 'textarea';
    const wrap = document.createElement('div');
    wrap.className = 'stage-editor-fallback';

    const gutter = document.createElement('div');
    gutter.className = 'stage-editor-gutter';
    gutter.setAttribute('aria-hidden', 'true');
    const gutterInner = document.createElement('div');
    gutterInner.className = 'stage-editor-gutter-inner';
    gutter.appendChild(gutterInner);

    const ta = document.createElement('textarea');
    ta.className = 'stage-editor-textarea';
    ta.value = fallbackCode;
    ta.spellcheck = false;
    ta.wrap = 'off';
    ta.setAttribute('aria-label', `Code editor for ${currentFilename}`);

    wrap.appendChild(gutter);
    wrap.appendChild(ta);
    editorHost.innerHTML = '';
    editorHost.appendChild(wrap);
    textarea = ta;

    function updateGutter() {
      const lines = (ta.value || '').split('\n');
      let text = '';
      for (let i = 1; i <= Math.max(lines.length, 1); i++) text += i + '\n';
      gutterInner.textContent = text;
    }
    function updateCursorPos() {
      const start = ta.selectionStart || 0;
      const before = (ta.value || '').slice(0, start).split('\n');
      posEl.textContent = `Ln ${before.length}, Col ${before[before.length - 1].length + 1}`;
    }
    ta.addEventListener('scroll', () => {
      gutter.scrollTop = ta.scrollTop;
    });
    ta.addEventListener('input', () => {
      dirty = true;
      updateMeta();
      updateGutter();
      updateCursorPos();
    });
    ['click', 'keyup', 'select'].forEach((evt) => ta.addEventListener(evt, updateCursorPos));
    ta.addEventListener('keydown', (e) => {
      if (e.key !== 'Tab') return;
      e.preventDefault();
      const start = ta.selectionStart || 0;
      const end = ta.selectionEnd || 0;
      const spaces = '    ';
      ta.value = ta.value.substring(0, start) + spaces + ta.value.substring(end);
      ta.selectionStart = ta.selectionEnd = start + spaces.length;
      updateGutter();
      updateCursorPos();
    });
    updateGutter();
    updateCursorPos();

    engineEl.textContent = 'Plain textarea';
    engineEl.title = reason || 'Monaco unavailable';
    setStatus(reason ? `Monaco unavailable — ${reason}` : 'Monaco unavailable');
    emit({ type: 'editor_engine', id: spec.__id, kind: 'editor', engine: 'textarea', reason: reason || '' });
  }

  function createMonaco(monaco) {
    engine = 'monaco';
    editorHost.innerHTML = '';
    monacoEditor = monaco.editor.create(editorHost, {
      value: fallbackCode,
      language: monacoLanguageId(language),
      theme: 'vs-dark',
      automaticLayout: true,
      minimap: { enabled: false },
      fontSize: 12,
      tabSize: 4,
      scrollBeyondLastLine: false,
      bracketPairColorization: { enabled: true },
      matchBrackets: 'always',
      autoClosingBrackets: 'always',
      suggestOnTriggerCharacters: true,
      quickSuggestions: true,
      wordBasedSuggestions: true,
      multiCursorModifier: 'alt',
      renderWhitespace: 'selection',
    });

    monacoEditor.onDidChangeModelContent(() => {
      dirty = true;
      updateMeta();
    });
    monacoEditor.onDidChangeCursorPosition((e) => {
      posEl.textContent = `Ln ${e.position.lineNumber}, Col ${e.position.column}`;
    });

    // Ctrl/Cmd+S saves instead of the browser's "save page".
    monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
      handleSave();
    });
    // Ctrl/Cmd+Enter runs.
    monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => {
      handleRun();
    });

    engineEl.textContent = 'Monaco';
    engineEl.title = `monaco-editor ${MONACO_VERSION} (CDN)`;
    setStatus('Ready');
    emit({ type: 'editor_engine', id: spec.__id, kind: 'editor', engine: 'monaco', reason: '' });
  }

  // The first listing must use the *validated* active root: a persisted id the
  // server no longer offers is corrected before any file request goes out.
  const rootsReady = loadRoots();

  // Attach the editor engine. Monaco is loaded from the CDN; on any failure we
  // fall back to the textarea and say why.
  const engineReady = loadMonaco()
    .then((monaco) => createMonaco(monaco))
    .catch((err) =>
      createTextareaFallback(err && err.message ? err.message : 'CDN unreachable'),
    );
  Promise.all([engineReady, rootsReady]).then(() => {
    refreshFiles();
    refreshEditorStatus();
  });

  // -- value access ---------------------------------------------------------
  function getValue() {
    if (engine === 'monaco' && monacoEditor) return monacoEditor.getValue();
    if (textarea) return textarea.value || '';
    return fallbackCode;
  }
  function setValue(val) {
    if (engine === 'monaco' && monacoEditor) {
      monacoEditor.setValue(val);
    } else if (textarea) {
      textarea.value = val;
    } else {
      fallbackCode = val;
    }
  }
  function setLanguage(lang) {
    language = lang;
    updateMeta();
    if (engine === 'monaco' && monacoEditor && window.monaco) {
      const model = monacoEditor.getModel();
      if (model) window.monaco.editor.setModelLanguage(model, monacoLanguageId(lang));
    }
  }

  // -- root picker ----------------------------------------------------------
  function renderRootOptions() {
    const ids = knownRoots.size ? [...knownRoots.keys()] : [activeRoot || DEFAULT_ROOT_ID];
    rootSelect.innerHTML = '';
    for (const id of ids) {
      const option = document.createElement('option');
      option.value = id;
      option.textContent = knownRoots.get(id) || id;
      // Attribute, not `select.value = ...`: identical effect in a browser,
      // and linkedom (the DOM used by the UI tests) has a read-only value.
      if (id === activeRoot) option.setAttribute('selected', '');
      rootSelect.appendChild(option);
    }
  }

  function updateCrumb() {
    // The breadcrumb names the active root: "home:" or "home:/pkg/mod".
    crumb.textContent = `${knownRoots.get(activeRoot) || activeRoot}:${
      browsePath ? `/${browsePath}` : ''
    }`;
  }

  /**
   * Fetch the server's roots, validate the persisted selection, and build the
   * picker. Never rejects: an unavailable roots list keeps the persisted root
   * as the only option so the browser still works.
   */
  async function loadRoots() {
    try {
      const data = await workspaceApi.roots();
      const fetched = (data && data.roots) || [];
      if (!fetched.length) return;
      rootsLoaded = true;
      knownRoots.clear();
      for (const root of fetched) knownRoots.set(root.id, root.label || root.id);
      if (!knownRoots.has(activeRoot)) activeRoot = knownRoots.keys().next().value;
      renderRootOptions();
      updateCrumb();
    } catch (_err) {
      /* roots unavailable (e.g. older server): keep the persisted option */
    }
  }

  /**
   * Switch the active root: persist it, reset navigation and the editor's file
   * context (an open path belongs to the *old* root and must not be saved into
   * the new one), then relist. The buffer itself is left untouched.
   */
  async function switchRoot(id) {
    if (!id || id === activeRoot) return;
    activeRoot = id;
    _persistRoot(id);
    browsePath = '';
    currentPath = null;
    currentFilename =
      spec.filename || (spec.language === 'javascript' ? 'script.js' : 'script.py');
    dirty = false;
    renderRootOptions();
    updateCrumb();
    updateMeta();
    updateZedButton();
    setStatus(`Switched to root "${knownRoots.get(id) || id}"`);
    emit({ type: 'root_changed', id: spec.__id, kind: 'editor', root: id });
    refreshFiles();
  }

  renderRootOptions();

  rootSelect.addEventListener('change', () => switchRoot(rootSelect.value));

  // -- file browser ---------------------------------------------------------
  function renderEntries(data) {
    fileList.innerHTML = '';
    const entries = (data && data.entries) || [];
    if (!entries.length) {
      const empty = document.createElement('div');
      empty.className = 'stage-editor-files-empty';
      empty.textContent = 'Empty directory';
      fileList.appendChild(empty);
    }
    entries.forEach((entry) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'stage-editor-file-item' + (entry.is_dir ? ' is-dir' : '');
      if (entry.contained === false) item.classList.add('is-escaped');
      item.setAttribute('role', 'option');
      item.title = entry.contained === false ? `${entry.name} (outside the workspace — not openable)` : entry.path;
      const icon = entry.is_dir ? '📁' : '📄';
      const size = entry.size !== null && entry.size !== undefined ? ` · ${entry.size} B` : '';
      item.innerHTML = `<span class="stage-editor-file-icon">${icon}</span><span class="stage-editor-file-name"></span><span class="stage-editor-file-meta"></span>`;
      item.querySelector('.stage-editor-file-name').textContent = entry.name;
      item.querySelector('.stage-editor-file-meta').textContent = size;
      if (entry.contained === false) {
        item.disabled = true;
      } else if (entry.is_dir) {
        item.addEventListener('click', () => {
          browsePath = entry.path;
          refreshFiles();
        });
      } else {
        item.addEventListener('click', () => openFile(entry.path));
      }
      fileList.appendChild(item);
    });
  }

  async function refreshFiles() {
    updateCrumb();
    upBtn.disabled = !browsePath;
    try {
      const data = await workspaceApi.list(browsePath, activeRoot);
      renderEntries(data);
    } catch (err) {
      fileList.innerHTML = '';
      const errEl = document.createElement('div');
      errEl.className = 'stage-editor-files-error';
      errEl.textContent = `Could not list workspace: ${err.message}`;
      fileList.appendChild(errEl);
    }
  }

  /**
   * Open a file. With an optional `root`, switch to that root first (when it is
   * one the server offers); an unknown id is refused here rather than sent to
   * the server as a 400. Without `root` the active root is used.
   */
  async function openFile(path, root) {
    if (root && root !== activeRoot) {
      if (knownRoots.has(root) || !rootsLoaded) {
        await switchRoot(root);
      } else {
        setStatus(`Unknown workspace root "${root}"`);
        emit({
          type: 'error',
          id: spec.__id,
          kind: 'editor',
          message: `unknown workspace root: ${root}`,
        });
        return;
      }
    }
    setStatus(`Opening ${path}...`);
    try {
      const data = await workspaceApi.read(path, activeRoot);
      currentPath = data.path;
      currentFilename = String(data.path).split('/').pop();
      setValue(data.content);
      setLanguage(languageFromPath(data.path));
      dirty = false;
      updateMeta();
      setStatus(`Opened ${data.path} (${data.size} B)`);
      updateZedButton();
      emit({ type: 'open_file', id: spec.__id, kind: 'editor', path: data.path, bytes: data.size, root: activeRoot });
    } catch (err) {
      setStatus(`Open failed: ${err.message}`);
      emit({ type: 'error', id: spec.__id, kind: 'editor', message: err.message });
    }
  }

  upBtn.addEventListener('click', () => {
    if (!browsePath) return;
    const parts = browsePath.split('/');
    parts.pop();
    browsePath = parts.join('/');
    refreshFiles();
  });
  refreshBtn.addEventListener('click', () => refreshFiles());
  filesBtn.addEventListener('click', () => {
    const hidden = filePanel.classList.toggle('is-hidden');
    filesBtn.classList.toggle('is-active', !hidden);
  });

  // -- Open in Zed ----------------------------------------------------------
  let zedAvailable = false;
  function updateZedButton() {
    if (!zedAvailable) {
      zedBtn.disabled = true;
      return;
    }
    zedBtn.disabled = !currentPath;
    zedBtn.title = currentPath
      ? `Open ${currentPath} in Zed on the server machine`
      : 'Save the buffer to the workspace first (Open in Zed needs a file path)';
  }

  async function refreshEditorStatus() {
    try {
      const status = await workspaceApi.editorStatus();
      zedAvailable = !!status.available;
      zedBtn.textContent = zedAvailable ? '⌘ Open in Zed' : '⌘ Zed not installed';
      if (!zedAvailable) {
        zedBtn.title = status.reason || 'editor not installed';
        emit({ type: 'editor_status', id: spec.__id, kind: 'editor', available: false, reason: status.reason });
      }
      updateZedButton();
    } catch (err) {
      zedAvailable = false;
      zedBtn.disabled = true;
      zedBtn.textContent = '⌘ Zed unavailable';
      zedBtn.title = err.message;
    }
  }

  zedBtn.addEventListener('click', async () => {
    if (!currentPath) return;
    zedBtn.disabled = true;
    const previous = zedBtn.textContent;
    zedBtn.textContent = '⌘ Launching…';
    try {
      const res = await workspaceApi.openInEditor(currentPath, activeRoot);
      setStatus(`Launched ${res.binary} on ${res.path} (pid ${res.pid})`);
      emit({ type: 'open_in_zed', id: spec.__id, kind: 'editor', path: res.path, pid: res.pid });
    } catch (err) {
      setStatus(`Zed launch failed: ${err.message}`);
      emit({ type: 'error', id: spec.__id, kind: 'editor', message: err.message });
    } finally {
      zedBtn.textContent = previous;
      updateZedButton();
    }
  });

  // -- Save -----------------------------------------------------------------
  function download(filename, content) {
    if (
      typeof Blob === 'undefined' ||
      typeof URL === 'undefined' ||
      typeof URL.createObjectURL !== 'function'
    ) {
      return false;
    }
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(() => {
      try {
        URL.revokeObjectURL(url);
      } catch (_e) {}
    }, 1000);
    return true;
  }

  async function handleSave() {
    const content = getValue();
    if (currentPath) {
      setStatus(`Saving ${currentPath}…`);
      try {
        const res = await workspaceApi.write(currentPath, content, activeRoot);
        dirty = false;
        updateMeta();
        setStatus(`Saved ${res.path} (${res.bytes} B)`);
        emit({ type: 'save', id: spec.__id, kind: 'editor', path: res.path, bytes: res.bytes });
        return;
      } catch (err) {
        setStatus(`Save failed: ${err.message}`);
        emit({ type: 'error', id: spec.__id, kind: 'editor', message: err.message });
        return;
      }
    }
    // Untitled buffer: keep the old download behaviour, honestly labelled.
    const downloaded = download(currentFilename, content);
    setStatus(downloaded ? `Downloaded ${currentFilename} (${content.length} B)` : 'Nothing to save to');
    emit({ type: 'save', id: spec.__id, kind: 'editor', filename: currentFilename, bytes: content.length });
  }
  saveBtn.addEventListener('click', handleSave);

  // -- Run ------------------------------------------------------------------
  async function handleRun() {
    runBtn.disabled = true;
    runBtn.textContent = '⏳ Running...';
    setStatus('Executing...');
    outputDrawer.classList.add('is-visible');
    outputContent.textContent = 'Executing code against backend...\n';

    const code = getValue();
    const endpoint = spec.runEndpoint || '/api/sandbox/run';
    const payload = spec.runEndpoint ? { code } : { language, code };
    const startTime =
      typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now();
    try {
      if (typeof fetch === 'function') {
        const response = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify(payload),
        });
        const elapsed = Math.round(
          (typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now()) -
            startTime,
        );
        let data;
        const contentType = response.headers ? response.headers.get('content-type') || '' : '';
        data = contentType.includes('application/json') ? await response.json() : await response.text();

        let formatted = `[Execution target: ${endpoint}]\n`;
        formatted += `HTTP ${response.status} ${response.statusText} (${elapsed}ms)\n\n`;
        formatted += typeof data === 'object' ? JSON.stringify(data, null, 2) : data;
        outputContent.textContent = formatted;
        setStatus(response.ok ? `Done (${response.status})` : `Failed (${response.status})`);
        emit({ type: 'run', id: spec.__id, kind: 'editor', endpoint, status: response.status, elapsed, response: data });
      } else {
        outputContent.textContent = '[Execution mock]: fetch is not available in this environment';
      }
    } catch (err) {
      outputContent.textContent = `[Network / Execution Error]\n${err.message}`;
      setStatus('Execution error');
      emit({ type: 'error', id: spec.__id, kind: 'editor', message: err.message });
    } finally {
      runBtn.disabled = false;
      runBtn.textContent = '▶ Run';
    }
  }
  runBtn.addEventListener('click', handleRun);

  outputCloseBtn.addEventListener('click', () => outputDrawer.classList.remove('is-visible'));

  body.__editor = {
    getValue,
    setValue,
    save: handleSave,
    run: handleRun,
    openFile,
    refreshFiles,
    openInZed: () => zedBtn.click(),
    getEngine: () => engine,
    getPath: () => currentPath,
    getFilename: () => currentFilename,
    getLanguage: () => language,
    getRoot: () => activeRoot,
    setRoot: (id) => switchRoot(id),
    getLineCount: () => getValue().split('\n').length,
    getTextarea: () => textarea,
    getMonaco: () => monacoEditor,
    getZedButton: () => zedBtn,
    isZedAvailable: () => zedAvailable,
  };

  // Console/other-module hooks, same call shape as body.__editor.openFile:
  //   openFile(path, root?)  -- root is optional; omit to use the active one.
  // The flat `uapOpenWorkspaceFile` name is the canvas-bridge seam
  // (stage.setCanvasFileOpener); it must answer truthy only when THIS editor
  // is on screen, so a hidden editor lets stage.js open its own editor view.
  // With several editor views alive the last-built one owns the globals.
  if (typeof window !== 'undefined') {
    const uap = (window.UAP = window.UAP || {});
    uap.openFile = (path, root) => body.__editor.openFile(path, root);
    const visible =
      typeof body.offsetParent === 'undefined' || body.offsetParent !== null;
    window.uapOpenWorkspaceFile = (path, root) =>
      visible ? body.__editor.openFile(path, root) : false;
  }

  return body;
}

export default buildEditorBody;
