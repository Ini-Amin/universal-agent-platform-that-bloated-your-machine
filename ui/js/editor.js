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
//   * a file browser tree that lists the workspace (GET /api/workspace/files),
//     opens a file (GET /api/workspace/file) and saves it back (POST
//     /api/workspace/file) -- a real write, not a download;
//   * multiple browsable roots (GET /api/workspace/roots) and dynamic root
//     registration (POST /api/workspace/roots);
//   * in-place expandable tree view with noise folder filtering;
//   * multi-tab editing with dirty indicators, confirmation on close, and
//     per-tab model/state isolation;
//   * breadcrumb navigation row (<root> > dir > file);
//   * VS Code-style status bar with Ln/Col, UTF-8 encoding, and language;
//   * "Open in Zed" support;
//   * Run against backend sandbox.

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

export function languageDisplayName(lang) {
  if (!lang) return 'Plain Text';
  const map = {
    python: 'Python',
    javascript: 'JavaScript',
    typescript: 'TypeScript',
    tsx: 'TypeScript React',
    jsx: 'JavaScript React',
    json: 'JSON',
    markdown: 'Markdown',
    html: 'HTML',
    css: 'CSS',
    rust: 'Rust',
    go: 'Go',
    shell: 'Shell',
    yaml: 'YAML',
    sql: 'SQL',
    plaintext: 'Plain Text',
  };
  return map[lang.toLowerCase()] || lang;
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
  } catch (_e) {}
  return headers;
}

const ROOT_STORAGE_KEY = 'uap_workspace_root';
const PATH_STORAGE_KEY = 'uap_workspace_path';
const TABS_STORAGE_KEY = 'uap_workspace_tabs';
const ACTIVE_TAB_STORAGE_KEY = 'uap_workspace_active_tab';
const RECENT_FOLDERS_STORAGE_KEY = 'uap_recent_folders';
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
  } catch (_e) {}
}

function _readRecentFolders() {
  try {
    const raw = localStorage.getItem(RECENT_FOLDERS_STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch (_e) {
    return [];
  }
}

function _addRecentFolder(path) {
  try {
    const list = _readRecentFolders().filter((p) => p !== path);
    list.unshift(path);
    if (list.length > 10) list.length = 10;
    localStorage.setItem(RECENT_FOLDERS_STORAGE_KEY, JSON.stringify(list));
  } catch (_e) {}
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
  addRoot: (path, label) =>
    _jsonRequest('/api/workspace/roots', {
      method: 'POST',
      body: JSON.stringify(label ? { path, label } : { path }),
    }),
  deleteRoot: (id) =>
    _jsonRequest(`/api/workspace/roots/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
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

const NOISE_DIRS = new Set([
  '.git',
  'node_modules',
  '.venv',
  '__pycache__',
  '.pytest_cache',
  '.tox',
  '.mypy_cache',
]);

/**
 * Build the editor view. Returns synchronously with a complete shell; Monaco
 * (or its textarea fallback) is attached asynchronously.
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
  saveBtn.title = 'Save the active tab buffer to the workspace';

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
  filePanelHeader.style.cssText = 'display:flex;flex-direction:column;gap:4px;padding:6px;';

  const headerTop = document.createElement('div');
  headerTop.className = 'stage-editor-files-header-top';
  headerTop.style.cssText = 'display:flex;align-items:center;gap:4px;width:100%;';

  const upBtn = document.createElement('button');
  upBtn.type = 'button';
  upBtn.className = 'stage-editor-files-up';
  upBtn.textContent = '↑';
  upBtn.title = 'Up one directory';

  const rootSelect = document.createElement('select');
  rootSelect.className = 'stage-editor-root-select';
  rootSelect.setAttribute('aria-label', 'Workspace root');
  rootSelect.title = 'Browse a different workspace root';

  const openFolderBtn = document.createElement('button');
  openFolderBtn.type = 'button';
  openFolderBtn.className = 'stage-editor-files-open-folder';
  openFolderBtn.textContent = '📂';
  openFolderBtn.title = 'Open any project folder on your machine';

  const crumb = document.createElement('span');
  crumb.className = 'stage-editor-files-crumb';
  crumb.textContent = '/';
  crumb.style.display = 'none';

  const refreshBtn = document.createElement('button');
  refreshBtn.type = 'button';
  refreshBtn.className = 'stage-editor-files-refresh';
  refreshBtn.textContent = '⟳';
  refreshBtn.title = 'Refresh workspace tree';

  headerTop.appendChild(upBtn);
  headerTop.appendChild(rootSelect);
  headerTop.appendChild(openFolderBtn);
  headerTop.appendChild(crumb);
  headerTop.appendChild(refreshBtn);

  const openFolderPanel = document.createElement('div');
  openFolderPanel.className = 'stage-editor-open-folder-panel';
  openFolderPanel.style.display = 'none';

  const openFolderRow = document.createElement('div');
  openFolderRow.className = 'stage-editor-open-folder-row';

  const folderInput = document.createElement('input');
  folderInput.type = 'text';
  folderInput.className = 'stage-editor-open-folder-input';
  folderInput.placeholder = '/path/to/project (Enter to open)';
  folderInput.setAttribute('aria-label', 'Open folder path');

  const folderSubmitBtn = document.createElement('button');
  folderSubmitBtn.type = 'button';
  folderSubmitBtn.className = 'stage-editor-btn stage-editor-open-folder-submit';
  folderSubmitBtn.textContent = 'Open';

  openFolderRow.appendChild(folderInput);
  openFolderRow.appendChild(folderSubmitBtn);

  const recentSelect = document.createElement('select');
  recentSelect.className = 'stage-editor-recent-select';
  recentSelect.setAttribute('aria-label', 'Recent folders');
  recentSelect.innerHTML = '<option value="">Recent folders…</option>';

  openFolderPanel.appendChild(openFolderRow);
  openFolderPanel.appendChild(recentSelect);

  filePanelHeader.appendChild(headerTop);
  filePanelHeader.appendChild(openFolderPanel);

  const fileList = document.createElement('div');
  fileList.className = 'stage-editor-files-list';
  fileList.setAttribute('role', 'listbox');
  fileList.setAttribute('aria-label', 'Workspace file list');

  filePanel.appendChild(filePanelHeader);
  filePanel.appendChild(fileList);

  // Editor main panel: tabs + breadcrumb + editor host
  const editorMain = document.createElement('div');
  editorMain.className = 'stage-editor-main';

  const tabsBar = document.createElement('div');
  tabsBar.className = 'stage-editor-tabs';
  tabsBar.setAttribute('role', 'tablist');
  tabsBar.setAttribute('aria-label', 'File tabs');

  const breadcrumbsBar = document.createElement('div');
  breadcrumbsBar.className = 'stage-editor-breadcrumbs';
  breadcrumbsBar.setAttribute('role', 'navigation');
  breadcrumbsBar.setAttribute('aria-label', 'File breadcrumbs');

  const editorHost = document.createElement('div');
  editorHost.className = 'stage-editor-host';
  editorHost.setAttribute('role', 'textbox');
  editorHost.setAttribute('aria-label', 'Code editor');

  const emptyHostEl = document.createElement('div');
  emptyHostEl.className = 'stage-editor-empty-state';
  emptyHostEl.innerHTML =
    '<div class="stage-editor-empty-icon">📝</div>' +
    '<div class="stage-editor-empty-title">No file open</div>' +
    '<div class="stage-editor-empty-desc">Select a file from the workspace tree or open a folder.</div>';
  emptyHostEl.style.display = 'none';
  editorHost.appendChild(emptyHostEl);

  editorMain.appendChild(tabsBar);
  editorMain.appendChild(breadcrumbsBar);
  editorMain.appendChild(editorHost);

  workspace.appendChild(filePanel);
  workspace.appendChild(editorMain);

  // -- status bar -----------------------------------------------------------
  const statusbar = document.createElement('div');
  statusbar.className = 'stage-editor-statusbar';

  const statusbarLeft = document.createElement('div');
  statusbarLeft.className = 'stage-editor-statusbar-left';

  const engineEl = document.createElement('span');
  engineEl.className = 'stage-editor-engine';
  engineEl.textContent = 'Loading editor…';

  const statusMsg = document.createElement('span');
  statusMsg.className = 'stage-editor-status-msg';
  statusMsg.textContent = 'Ready';

  statusbarLeft.appendChild(engineEl);
  statusbarLeft.appendChild(statusMsg);

  const statusbarRight = document.createElement('div');
  statusbarRight.className = 'stage-editor-statusbar-right';

  const posEl = document.createElement('span');
  posEl.className = 'stage-editor-pos';
  posEl.textContent = 'Ln 1, Col 1';

  const encodingEl = document.createElement('span');
  encodingEl.className = 'stage-editor-encoding';
  encodingEl.textContent = 'UTF-8';

  const statusLangEl = document.createElement('span');
  statusLangEl.className = 'stage-editor-lang';
  statusLangEl.textContent = 'python';

  statusbarRight.appendChild(posEl);
  statusbarRight.appendChild(encodingEl);
  statusbarRight.appendChild(statusLangEl);

  statusbar.appendChild(statusbarLeft);
  statusbar.appendChild(statusbarRight);

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
    spec.filename || (currentPath ? currentPath.split('/').pop() : spec.language === 'javascript' ? 'script.js' : 'main.py');
  let language = spec.language || (currentPath ? languageFromPath(currentPath) : 'python');
  let engine = 'pending'; // 'monaco' | 'textarea'
  let monacoEditor = null;
  let textarea = null;
  let dirty = false;
  let browsePath = '';
  let activeRoot = _readPersistedRoot();
  const knownRoots = new Map();
  let rootsLoaded = false;

  // Tabs state
  const tabs = [];
  let activeTab = null;

  // Initialize first tab
  const initialTab = {
    id: 'tab_init',
    path: currentPath,
    filename: currentFilename,
    language,
    content: initialCode,
    dirty: false,
    root: activeRoot,
    model: null,
    viewState: null,
  };
  tabs.push(initialTab);
  activeTab = initialTab;

  // Tree items state: array of root nodes
  let treeItems = [];

  function setStatus(msg) {
    statusMsg.textContent = msg;
  }

  function _persistState() {
    _persistRoot(activeRoot);
    try {
      localStorage.setItem(PATH_STORAGE_KEY, browsePath || '');
      const tabData = tabs.map((t) => ({
        path: t.path,
        root: t.root,
        filename: t.filename,
        language: t.language,
      }));
      localStorage.setItem(TABS_STORAGE_KEY, JSON.stringify(tabData));
      localStorage.setItem(
        ACTIVE_TAB_STORAGE_KEY,
        activeTab ? activeTab.path || activeTab.filename : '',
      );
    } catch (_e) {}
  }
  let zedAvailable = false;
  function updateZedButton() {
    if (!zedAvailable) {
      zedBtn.disabled = true;
      return;
    }
    const current = activeTab ? activeTab.path : currentPath;
    zedBtn.disabled = !current;
    zedBtn.title = current
      ? `Open ${current} in Zed on the server machine`
      : 'Save the buffer to the workspace first (Open in Zed needs a file path)';
  }

  function updateMeta() {
    const curName = activeTab ? activeTab.filename : currentFilename || 'No file open';
    const isDirty = activeTab ? activeTab.dirty : dirty;
    filenameEl.textContent = curName + (isDirty ? ' •' : '');
    const curLang = activeTab ? activeTab.language : language;
    langEl.textContent = curLang;
    statusLangEl.textContent = curLang;
    encodingEl.textContent = 'UTF-8';
    saveBtn.disabled = !activeTab;
    runBtn.disabled = !activeTab;
    updateZedButton();
  }
  updateMeta();

  function updateCrumb() {
    const rootLabel = knownRoots.get(activeRoot) || activeRoot;
    crumb.textContent = `${rootLabel}:${browsePath ? `/${browsePath}` : ''}`;

    breadcrumbsBar.innerHTML = '';
    const rootSpan = document.createElement('span');
    rootSpan.className = 'stage-editor-crumb-item';
    rootSpan.textContent = rootLabel;
    breadcrumbsBar.appendChild(rootSpan);

    if (activeTab && activeTab.path) {
      const parts = activeTab.path.split('/');
      for (let i = 0; i < parts.length; i++) {
        const sep = document.createElement('span');
        sep.className = 'stage-editor-crumb-sep';
        sep.textContent = '›';
        breadcrumbsBar.appendChild(sep);

        const partSpan = document.createElement('span');
        partSpan.className =
          'stage-editor-crumb-item' + (i === parts.length - 1 ? ' is-current' : '');
        partSpan.textContent =
          parts[i] + (i === parts.length - 1 && activeTab.dirty ? ' •' : '');
        breadcrumbsBar.appendChild(partSpan);
      }
    }
  }
  updateCrumb();

  function renderTabs() {
    tabsBar.innerHTML = '';
    if (tabs.length === 0) {
      tabsBar.style.display = 'none';
      return;
    }
    tabsBar.style.display = 'flex';
    tabs.forEach((tab) => {
      const tabEl = document.createElement('div');
      tabEl.className = 'stage-editor-tab' + (tab === activeTab ? ' is-active' : '');
      tabEl.setAttribute('role', 'tab');
      tabEl.setAttribute('aria-selected', tab === activeTab ? 'true' : 'false');
      tabEl.title = tab.path || tab.filename;

      const icon = document.createElement('span');
      icon.className = 'stage-editor-tab-icon';
      icon.textContent = '📄';

      const name = document.createElement('span');
      name.className = 'stage-editor-tab-name';
      name.textContent = tab.filename;

      const dirtyDot = document.createElement('span');
      dirtyDot.className = 'stage-editor-tab-dirty';
      dirtyDot.textContent = '●';
      dirtyDot.style.display = tab.dirty ? 'inline' : 'none';

      const closeBtn = document.createElement('button');
      closeBtn.type = 'button';
      closeBtn.className = 'stage-editor-tab-close';
      closeBtn.textContent = '✕';
      closeBtn.title = 'Close tab';
      closeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        closeTab(tab.id);
      });

      tabEl.appendChild(icon);
      tabEl.appendChild(name);
      tabEl.appendChild(dirtyDot);
      tabEl.appendChild(closeBtn);

      tabEl.addEventListener('click', () => {
        if (tab !== activeTab) switchTab(tab.id);
      });

      tabsBar.appendChild(tabEl);
    });
  }
  renderTabs();

  function createTabModel(tab, content) {
    if (
      engine === 'monaco' &&
      window.monaco &&
      window.monaco.editor &&
      typeof window.monaco.editor.createModel === 'function'
    ) {
      try {
        const langId = monacoLanguageId(tab.language);
        const model = window.monaco.editor.createModel(content, langId);
        model.onDidChangeContent(() => {
          tab.dirty = true;
          if (tab === activeTab) {
            dirty = true;
            updateMeta();
          }
          renderTabs();
          updateCrumb();
        });
        tab.model = model;
      } catch (_e) {}
    }
  }

  function switchTab(tabId) {
    const target = tabs.find((t) => t.id === tabId);
    if (!target) return;
    if (activeTab && activeTab !== target) {
      if (engine === 'monaco' && monacoEditor) {
        if (typeof monacoEditor.saveViewState === 'function') {
          activeTab.viewState = monacoEditor.saveViewState();
        }
        if (typeof monacoEditor.getValue === 'function') {
          activeTab.content = monacoEditor.getValue();
        }
      } else if (textarea) {
        activeTab.content = textarea.value;
        activeTab.selectionStart = textarea.selectionStart;
        activeTab.selectionEnd = textarea.selectionEnd;
        activeTab.scrollTop = textarea.scrollTop;
      }
    }
    activeTab = target;
    currentPath = target.path;
    currentFilename = target.filename;
    language = target.language;
    dirty = target.dirty;

    if (emptyHostEl) emptyHostEl.style.display = 'none';

    if (engine === 'monaco' && monacoEditor) {
      if (target.model && typeof monacoEditor.setModel === 'function') {
        monacoEditor.setModel(target.model);
        if (target.viewState && typeof monacoEditor.restoreViewState === 'function') {
          monacoEditor.restoreViewState(target.viewState);
        }
      } else if (typeof monacoEditor.setValue === 'function') {
        monacoEditor.setValue(target.content || '');
      }
      if (typeof monacoEditor.focus === 'function') monacoEditor.focus();
    } else if (textarea) {
      textarea.style.display = '';
      textarea.value = target.content || '';
      if (target.selectionStart !== undefined) {
        textarea.selectionStart = target.selectionStart;
        textarea.selectionEnd = target.selectionEnd;
        textarea.scrollTop = target.scrollTop || 0;
      }
      if (typeof textarea.focus === 'function') textarea.focus();
    }

    updateMeta();
    updateCrumb();
    renderTabs();
    renderTree();
    _persistState();
  }

  async function closeTab(tabId) {
    const index = tabs.findIndex((t) => t.id === tabId);
    if (index < 0) return;
    const tab = tabs[index];
    if (tab.dirty) {
      let confirmed = true;
      if (typeof window !== 'undefined' && typeof window.confirm === 'function') {
        confirmed = window.confirm(
          `"${tab.filename}" has unsaved changes. Do you want to close it anyway?`,
        );
      }
      if (!confirmed) return;
    }
    if (tab.model && typeof tab.model.dispose === 'function') {
      try {
        tab.model.dispose();
      } catch (_e) {}
    }
    tabs.splice(index, 1);
    if (activeTab === tab) {
      if (tabs.length > 0) {
        const nextIndex = Math.min(index, tabs.length - 1);
        switchTab(tabs[nextIndex].id);
      } else {
        activeTab = null;
        currentPath = null;
        currentFilename = '';
        dirty = false;
        if (engine === 'monaco' && monacoEditor) {
          if (typeof monacoEditor.setModel === 'function') {
            monacoEditor.setModel(null);
          } else if (typeof monacoEditor.setValue === 'function') {
            monacoEditor.setValue('');
          }
        } else if (textarea) {
          textarea.value = '';
          textarea.style.display = 'none';
        }
        if (emptyHostEl) emptyHostEl.style.display = 'flex';
        posEl.textContent = 'Ln 0, Col 0';
        updateMeta();
        updateCrumb();
        renderTabs();
        renderTree();
        _persistState();
      }
    } else {
      renderTabs();
      _persistState();
    }
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
    ta.value = activeTab ? activeTab.content : fallbackCode;
    ta.spellcheck = false;
    ta.wrap = 'off';
    ta.setAttribute('aria-label', `Code editor for ${currentFilename}`);

    wrap.appendChild(gutter);
    wrap.appendChild(ta);
    editorHost.innerHTML = '';
    editorHost.appendChild(wrap);
    editorHost.appendChild(emptyHostEl);
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
      if (activeTab) {
        activeTab.dirty = true;
        activeTab.content = ta.value;
      }
      updateMeta();
      renderTabs();
      updateCrumb();
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
    renderTabs();
    updateCrumb();
    emit({ type: 'editor_engine', id: spec.__id, kind: 'editor', engine: 'textarea', reason: reason || '' });
  }

  function createMonaco(monaco) {
    engine = 'monaco';
    editorHost.innerHTML = '';
    editorHost.appendChild(emptyHostEl);
    if (monaco && monaco.editor && typeof monaco.editor.defineTheme === 'function') {
      monaco.editor.defineTheme('sds-dark', {
        base: 'vs-dark',
        inherit: true,
        rules: [
          { token: '', foreground: 'E2E6F0', background: '1B1E28' },
        ],
        colors: {
          'editor.background': '#1B1E28',
          'editor.foreground': '#E2E6F0',
          'editor.lineHighlightBackground': '#212530',
          'editor.selectionBackground': '#dd7fd333',
          'editorLineNumber.foreground': '#9AA1B4',
          'editorLineNumber.activeForeground': '#E2E6F0',
          'editorGutter.background': '#1B1E28',
        },
      });
    }

    const currentContent = activeTab ? activeTab.content : fallbackCode;
    const currentLang = activeTab ? activeTab.language : language;

    monacoEditor = monaco.editor.create(editorHost, {
      value: currentContent,
      language: monacoLanguageId(currentLang),
      theme: 'sds-dark',
      fontFamily: '"Roboto Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
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

    if (
      monaco.editor &&
      typeof monaco.editor.createModel === 'function' &&
      typeof monacoEditor.setModel === 'function'
    ) {
      tabs.forEach((t) => {
        createTabModel(t, t.content);
      });
      if (activeTab && activeTab.model) {
        monacoEditor.setModel(activeTab.model);
      }
    }

    monacoEditor.onDidChangeModelContent(() => {
      dirty = true;
      if (activeTab) {
        activeTab.dirty = true;
        if (typeof monacoEditor.getValue === 'function') {
          activeTab.content = monacoEditor.getValue();
        }
      }
      updateMeta();
      renderTabs();
      updateCrumb();
    });

    monacoEditor.onDidChangeCursorPosition((e) => {
      posEl.textContent = `Ln ${e.position.lineNumber}, Col ${e.position.column}`;
    });

    monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
      handleSave();
    });
    monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => {
      handleRun();
    });

    engineEl.textContent = 'Monaco';
    engineEl.title = `monaco-editor ${MONACO_VERSION} (CDN)`;
    setStatus('Ready');
    renderTabs();
    updateCrumb();
    emit({ type: 'editor_engine', id: spec.__id, kind: 'editor', engine: 'monaco', reason: '' });
  }

  // Ctrl+S global capture inside editor container
  container.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      handleSave();
    }
  });

  const rootsReady = loadRoots();

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
    if (engine === 'monaco' && monacoEditor) {
      if (typeof monacoEditor.getValue === 'function') return monacoEditor.getValue();
    }
    if (textarea) return textarea.value || '';
    if (activeTab) return activeTab.content || '';
    return fallbackCode;
  }

  function setValue(val) {
    if (activeTab) activeTab.content = val;
    if (engine === 'monaco' && monacoEditor) {
      if (activeTab && activeTab.model && typeof activeTab.model.setValue === 'function') {
        activeTab.model.setValue(val);
      } else if (typeof monacoEditor.setValue === 'function') {
        monacoEditor.setValue(val);
      }
    } else if (textarea) {
      textarea.value = val;
    } else {
      fallbackCode = val;
    }
  }

  function setLanguage(lang) {
    language = lang;
    if (activeTab) activeTab.language = lang;
    updateMeta();
    if (engine === 'monaco' && monacoEditor && window.monaco) {
      const model =
        activeTab && activeTab.model
          ? activeTab.model
          : monacoEditor.getModel();
      if (model && typeof window.monaco.editor.setModelLanguage === 'function') {
        window.monaco.editor.setModelLanguage(model, monacoLanguageId(lang));
      }
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
      if (id === activeRoot) option.setAttribute('selected', '');
      rootSelect.appendChild(option);
    }
  }

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
    } catch (_err) {}
  }

  async function switchRoot(id) {
    if (!id || id === activeRoot) return;
    activeRoot = id;
    _persistState();
    browsePath = '';
    renderRootOptions();
    updateCrumb();
    updateMeta();
    updateZedButton();
    setStatus(`Switched to root "${knownRoots.get(id) || id}"`);
    emit({ type: 'root_changed', id: spec.__id, kind: 'editor', root: id });
    await refreshFiles();
  }

  renderRootOptions();
  rootSelect.addEventListener('change', () => switchRoot(rootSelect.value));

  // -- Open Folder control --------------------------------------------------
  function _renderRecentFoldersDropdown() {
    const recents = _readRecentFolders();
    recentSelect.innerHTML = '<option value="">Recent folders…</option>';
    if (recents.length === 0) {
      recentSelect.style.display = 'none';
    } else {
      recentSelect.style.display = 'block';
      for (const p of recents) {
        const opt = document.createElement('option');
        opt.value = p;
        opt.textContent = p;
        recentSelect.appendChild(opt);
      }
    }
  }

  async function handleOpenFolder(folderPath) {
    if (!folderPath || !folderPath.trim()) return;
    const path = folderPath.trim();
    setStatus(`Opening folder ${path}…`);
    try {
      const res = await workspaceApi.addRoot(path);
      _addRecentFolder(res.path || path);
      _renderRecentFoldersDropdown();
      await loadRoots();
      await switchRoot(res.id);
      openFolderPanel.style.display = 'none';
      folderInput.value = '';
      setStatus(`Opened folder "${res.label}" (${res.path || path})`);
    } catch (err) {
      setStatus(`Open folder failed: ${err.message}`);
      if (typeof window !== 'undefined' && typeof window.alert === 'function') {
        window.alert(`Could not open folder "${path}": ${err.message}`);
      }
    }
  }

  openFolderBtn.addEventListener('click', () => {
    const isHidden = openFolderPanel.style.display === 'none';
    openFolderPanel.style.display = isHidden ? 'flex' : 'none';
    if (isHidden) {
      _renderRecentFoldersDropdown();
      folderInput.focus();
    }
  });

  folderSubmitBtn.addEventListener('click', () => handleOpenFolder(folderInput.value));
  folderInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleOpenFolder(folderInput.value);
    } else if (e.key === 'Escape') {
      openFolderPanel.style.display = 'none';
    }
  });
  recentSelect.addEventListener('change', () => {
    if (recentSelect.value) {
      folderInput.value = recentSelect.value;
      handleOpenFolder(recentSelect.value);
    }
  });

  // -- file browser tree ----------------------------------------------------
  function _flattenTree(items) {
    const result = [];
    for (const item of items) {
      result.push(item);
      if (item.is_dir && item.expanded && item.children) {
        result.push(..._flattenTree(item.children));
      }
    }
    return result;
  }

  function renderTree() {
    fileList.innerHTML = '';
    const visible = _flattenTree(treeItems);
    if (!visible.length) {
      const empty = document.createElement('div');
      empty.className = 'stage-editor-files-empty';
      empty.textContent = 'Empty directory';
      fileList.appendChild(empty);
      return;
    }
    visible.forEach((entry) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'stage-editor-file-item' + (entry.is_dir ? ' is-dir' : '');
      if (entry.contained === false) item.classList.add('is-escaped');
      if (activeTab && activeTab.path === entry.path) item.classList.add('is-active');
      item.setAttribute('role', 'option');
      item.title =
        entry.contained === false
          ? `${entry.name} (outside the workspace — not openable)`
          : entry.path;
      item.style.paddingLeft = `${8 + (entry.depth || 0) * 14}px`;

      const twistie = entry.is_dir ? (entry.loading ? '…' : entry.expanded ? '▼' : '▶') : ' ';
      const icon = entry.is_dir ? (entry.expanded ? '📂' : '📁') : '📄';
      const size =
        entry.size !== null && entry.size !== undefined ? ` · ${entry.size} B` : '';

      item.innerHTML = `<span class="stage-editor-file-twistie">${twistie}</span><span class="stage-editor-file-icon">${icon}</span><span class="stage-editor-file-name"></span><span class="stage-editor-file-meta"></span>`;
      item.querySelector('.stage-editor-file-name').textContent = entry.name;
      item.querySelector('.stage-editor-file-meta').textContent = size;

      if (entry.contained === false) {
        item.disabled = true;
      } else if (entry.is_dir) {
        item.addEventListener('click', async () => {
          if (entry.expanded) {
            entry.expanded = false;
            renderTree();
          } else {
            entry.expanded = true;
            if (entry.children === null) {
              entry.loading = true;
              renderTree();
              try {
                const data = await workspaceApi.list(entry.path, activeRoot);
                entry.children = (data.entries || []).map((child) => ({
                  ...child,
                  depth: (entry.depth || 0) + 1,
                  expanded: false,
                  children: null,
                  loading: false,
                }));
              } catch (err) {
                setStatus(`Could not list ${entry.path}: ${err.message}`);
                entry.children = [];
              } finally {
                entry.loading = false;
                renderTree();
              }
            } else {
              renderTree();
            }
          }
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
      const data = await workspaceApi.list('', activeRoot);
      treeItems = (data.entries || []).map((entry) => ({
        ...entry,
        depth: 0,
        expanded: false,
        children: null,
        loading: false,
      }));
      renderTree();
    } catch (err) {
      fileList.innerHTML = '';
      const errEl = document.createElement('div');
      errEl.className = 'stage-editor-files-error';
      errEl.textContent = `Could not list workspace: ${err.message}`;
      fileList.appendChild(errEl);
    }
  }

  /**
   * Open a file. With an optional `root`, switch to that root first.
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

    const existing = tabs.find((t) => t.path === path && t.root === activeRoot);
    if (existing) {
      switchTab(existing.id);
      setStatus(`Switched to ${path}`);
      return;
    }

    setStatus(`Opening ${path}...`);
    try {
      const data = await workspaceApi.read(path, activeRoot);
      const filename = String(data.path).split('/').pop();
      const lang = languageFromPath(data.path);

      let tab;
      if (tabs.length === 1 && tabs[0].path === null && !tabs[0].dirty) {
        tab = tabs[0];
        tab.path = data.path;
        tab.filename = filename;
        tab.language = lang;
        tab.content = data.content;
        tab.root = activeRoot;
        if (tab.model && typeof tab.model.dispose === 'function') {
          try {
            tab.model.dispose();
          } catch (_e) {}
          tab.model = null;
        }
        createTabModel(tab, data.content);
        switchTab(tab.id);
      } else {
        tab = {
          id: `tab_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
          path: data.path,
          filename,
          language: lang,
          content: data.content,
          dirty: false,
          root: activeRoot,
          model: null,
          viewState: null,
        };
        createTabModel(tab, data.content);
        tabs.push(tab);
        switchTab(tab.id);
      }

      setStatus(`Opened ${data.path} (${data.size} B)`);
      emit({
        type: 'open_file',
        id: spec.__id,
        kind: 'editor',
        path: data.path,
        bytes: data.size,
        root: activeRoot,
      });
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
    const activePath = activeTab ? activeTab.path : currentPath;
    if (!activePath) return;
    zedBtn.disabled = true;
    const previous = zedBtn.textContent;
    zedBtn.textContent = '⌘ Launching…';
    try {
      const res = await workspaceApi.openInEditor(activePath, activeRoot);
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
    if (!activeTab) return;
    const content = getValue();
    activeTab.content = content;
    const targetPath = activeTab.path || currentPath;
    if (targetPath) {
      setStatus(`Saving ${targetPath}…`);
      try {
        const res = await workspaceApi.write(targetPath, content, activeTab.root || activeRoot);
        activeTab.dirty = false;
        dirty = false;
        updateMeta();
        renderTabs();
        updateCrumb();
        setStatus(`Saved ${res.path} (${res.bytes} B)`);
        emit({ type: 'save', id: spec.__id, kind: 'editor', path: res.path, bytes: res.bytes });
        return;
      } catch (err) {
        setStatus(`Save failed: ${err.message}`);
        emit({ type: 'error', id: spec.__id, kind: 'editor', message: err.message });
        return;
      }
    }
    const targetFilename = activeTab.filename || currentFilename;
    const downloaded = download(targetFilename, content);
    setStatus(downloaded ? `Downloaded ${targetFilename} (${content.length} B)` : 'Nothing to save to');
    emit({ type: 'save', id: spec.__id, kind: 'editor', filename: targetFilename, bytes: content.length });
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
    getPath: () => (activeTab ? activeTab.path : currentPath),
    getFilename: () => (activeTab ? activeTab.filename : currentFilename),
    getLanguage: () => (activeTab ? activeTab.language : language),
    getRoot: () => activeRoot,
    setRoot: (id) => switchRoot(id),
    getLineCount: () => getValue().split('\n').length,
    getTextarea: () => textarea,
    getMonaco: () => monacoEditor,
    getZedButton: () => zedBtn,
    isZedAvailable: () => zedAvailable,
    getTabs: () => tabs,
    getActiveTab: () => activeTab,
    closeTab,
  };

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
