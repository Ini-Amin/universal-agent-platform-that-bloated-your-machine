// §54 Frontend State Architecture - separated independent stores
// No globals leaking between domains

export function createStore(initialState) {
  let state = Object.freeze({ ...initialState });
  const listeners = new Set();

  function getState() {
    return state;
  }

  function setState(updater) {
    // Functional updaters MERGE into the current state (React-style) — every
    // call site uses them to change one slice (e.g. `{activeNodes: {...}}`).
    // The old code replaced the whole state, so a lifecycle event wiped
    // executionStore.status and a canvas drag wiped canvasStore.graph
    // (pipeline vanished after any click — reproduced 2026-10-02).
    const patch = typeof updater === 'function' ? updater(state) : updater;
    const nextState = { ...state, ...patch };
    state = Object.freeze(nextState);
    for (const listener of listeners) {
      try {
        listener(state);
      } catch (err) {
        console.error('Store subscriber error:', err);
      }
    }
    return state;
  }

  function subscribe(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  }

  return { getState, setState, subscribe };
}

// Canvas state: canonical graph dict + selection + viewport transform (§54/§55)
export const canvasStore = createStore({
  graph: null,
  selectedNodeIds: [],
  selectedEdgeIds: [],
  viewport: { x: 0, y: 0, zoom: 1 },
  error: null,
});

// Execution state: durable status, active node status map, checkpoints, metrics (§53/§54)
export const executionStore = createStore({
  executionId: null,
  status: 'idle',
  resume_count: 0,
  checkpoints: [],
  activeNodes: {},
  tokenUsage: 0,
  cost: 0,
  error: null,
  output: null,
  artifacts: [],
  evidence_source: null,
  workspace_id: null,
  workflow: null,
  task: null,
});

// Event stream state: ring buffer of last 500 canonical events (§45/§54)
export const eventStore = createStore({
  events: [],
  max: 500,
});

// UI state: pane layout, tabs, console toggle, toasts, workspaces, inspector targets (§52/§54)
export const uiStore = createStore({
  activePane: 'canvas',
  sidebarTab: 'runs',
  sidebarNav: 'runs',
  consoleOpen: true,
  consoleTab: 'events',
  toasts: [],
  activeWorkspaceId: null,
  workspaces: [],
  selectedResource: null,
  activeProposal: null,
});
