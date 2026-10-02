// §56 Undo / Redo - distinguishes AI changes vs user changes
// Canonical graph dict serialized snapshots; Ctrl+Z / Ctrl+Shift+Z support

export function createUndoStack({ limit = 100 } = {}) {
  let stack = [];
  let pointer = -1;
  // State that existed before the FIRST pushed snapshot (the canvas content
  // the load/proposal replaced, or nothing). It is the undo target for entry
  // 0, so the first user change — and the auto-pushed graph load itself — is
  // always undoable instead of leaving Undo permanently disabled.
  let baseline = null;
  const listeners = new Set();

  function notify() {
    for (const fn of listeners) {
      try {
        fn();
      } catch (err) {
        console.error('UndoStack listener error:', err);
      }
    }
  }

  function push(snapshot, { actor = 'user', description = '', previous = null } = {}) {
    if (!snapshot) return;
    if (stack.length === 0) {
      // First snapshot of an era: remember what was on the canvas before it
      // so undo() can get back there (null = empty canvas).
      baseline = previous ?? null;
    }
    const serialized = JSON.parse(JSON.stringify(snapshot));
    const entry = {
      graph: serialized,
      actor,
      isAI: actor === 'ai',
      timestamp: Date.now(),
      description,
    };

    // Discard any forward redo history when a new action is performed
    stack = stack.slice(0, pointer + 1);
    stack.push(entry);

    if (stack.length > limit) {
      stack.shift();
    } else {
      pointer = stack.length - 1;
    }
    notify();
  }

  function undo() {
    if (!canUndo()) return null;
    const target = pointer === 0 ? baseline : stack[pointer - 1].graph;
    pointer--;
    notify();
    return target === null || target === undefined ? null : JSON.parse(JSON.stringify(target));
  }

  function redo() {
    if (!canRedo()) return null;
    pointer++;
    notify();
    return JSON.parse(JSON.stringify(stack[pointer].graph));
  }

  function canUndo() {
    // pointer is the index of the state the canvas currently shows; there is
    // always a state before it (the baseline for entry 0, the previous entry
    // otherwise), so any live snapshot is undoable.
    return pointer >= 0;
  }

  function canRedo() {
    return pointer < stack.length - 1;
  }

  function getCurrent() {
    return pointer >= 0 && pointer < stack.length ? stack[pointer] : null;
  }

  function getHistory() {
    return stack.map((entry, idx) => ({
      ...entry,
      isCurrent: idx === pointer,
      index: idx,
    }));
  }

  function clear() {
    stack = [];
    pointer = -1;
    baseline = null;
    notify();
  }

  function subscribe(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  }

  return {
    push,
    undo,
    redo,
    canUndo,
    canRedo,
    getCurrent,
    getHistory,
    clear,
    subscribe,
  };
}
