// Toast notification system (§52/§54)
// Renders uiStore.toasts into a fixed top-right stack with auto-dismiss and actions.

import { uiStore } from './state.js';

let keyListenerAttached = false;

export function dismissToast(id) {
  const current = uiStore.getState().toasts || [];
  const target = current.find((t) => t.id === id);
  if (target && target._timer) {
    clearTimeout(target._timer);
  }
  uiStore.setState((s) => ({
    toasts: (s.toasts || []).filter((t) => t.id !== id),
  }));
}
// Non-blocking prompt: returns toast id; the caller-supplied onSubmit(value, toastId)
// receives whatever text was typed.
export function showPromptToast({
  title,
  message = '',
  placeholder = '',
  submitLabel = 'Submit',
  onSubmit,
  dismissLabel = 'Cancel',
} = {}) {
  return showToast({
    kind: 'info',
    title,
    message,
    input: { placeholder, submitLabel, onSubmit },
    actions: [{ label: dismissLabel, onClick: (t) => dismissToast(t.id) }],
    timeoutMs: 0,
  });
}


export function showToast({
  kind = 'info',
  title = '',
  message = '',
  actions = [],
  timeoutMs = null,
  input = null,
} = {}) {
  const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;

  // Default timeout: errors and clarifications persist (timeoutMs = 0), others auto-dismiss after 8s
  let effectiveTimeout = timeoutMs;
  if (effectiveTimeout === null || effectiveTimeout === undefined) {
    effectiveTimeout = (kind === 'error' || kind === 'clarify') ? 0 : 8000;
  }

  let timer = null;
  if (effectiveTimeout > 0) {
    timer = setTimeout(() => {
      dismissToast(id);
    }, effectiveTimeout);
  }

  const defaultTitle = kind === 'error' ? 'Error' : kind === 'clarify' ? 'Clarification Needed' : 'Notice';

  const toast = {
    id,
    kind,
    title: title || defaultTitle,
    message: String(message || ''),
    actions: Array.isArray(actions) ? actions : [],
    input: input || null,
    timeoutMs: effectiveTimeout,
    _timer: timer,
    createdAt: Date.now(),
  };

  uiStore.setState((s) => ({
    toasts: [...(s.toasts || []), toast],
  }));

  return id;
}

export function initToasts(containerEl = null) {
  let container = containerEl;
  if (!container) {
    container = document.getElementById('toast-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'toast-container';
      container.className = 'toast-container';
      document.body.appendChild(container);
    }
  }

  // Keyboard accessibility: Escape dismisses newest toast
  if (!keyListenerAttached) {
    window.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        const toasts = uiStore.getState().toasts || [];
        if (toasts.length > 0) {
          dismissToast(toasts[toasts.length - 1].id);
        }
      }
    });
    keyListenerAttached = true;
  }

  function render(state) {
    const toasts = state.toasts || [];
    const activeIds = new Set(toasts.map((t) => t.id));

    // Remove obsolete toast nodes
    const existingNodes = Array.from(container.querySelectorAll('.toast'));
    for (const node of existingNodes) {
      const id = node.getAttribute('data-toast-id');
      if (!activeIds.has(id)) {
        node.remove();
      }
    }

    // Append new toast nodes (preserving existing inputs and focus)
    for (const toast of toasts) {
      if (!container.querySelector(`[data-toast-id="${toast.id}"]`)) {
        const toastEl = createToastElement(toast);
        container.appendChild(toastEl);
      }
    }
  }

  function createToastElement(toast) {
    const el = document.createElement('div');
    el.className = `toast toast-${toast.kind || 'info'}`;
    el.setAttribute('data-toast-id', toast.id);
    el.setAttribute('role', 'alert');

    // Header: Title + Close Button
    const header = document.createElement('div');
    header.className = 'toast-header';

    const titleEl = document.createElement('div');
    titleEl.className = 'toast-title';
    titleEl.textContent = toast.title;
    header.appendChild(titleEl);

    const closeBtn = document.createElement('button');
    closeBtn.className = 'toast-close-btn';
    closeBtn.innerHTML = '&times;';
    closeBtn.setAttribute('type', 'button');
    closeBtn.setAttribute('aria-label', 'Close');
    closeBtn.onclick = () => dismissToast(toast.id);
    header.appendChild(closeBtn);

    el.appendChild(header);

    // Message
    if (toast.message) {
      const msgEl = document.createElement('div');
      msgEl.className = 'toast-message';
      msgEl.textContent = toast.message;
      el.appendChild(msgEl);
    }

    // Clarification form / inline input
    if (toast.input) {
      const form = document.createElement('div');
      form.className = 'toast-form';

      const inputEl = document.createElement('input');
      inputEl.type = 'text';
      inputEl.className = 'toast-input';
      inputEl.placeholder = toast.input.placeholder || 'Type your answer...';
      form.appendChild(inputEl);

      const actionsBar = document.createElement('div');
      actionsBar.className = 'toast-actions';

      const submitBtn = document.createElement('button');
      submitBtn.type = 'button';
      submitBtn.className = 'btn btn-sm btn-accent';
      submitBtn.textContent = toast.input.submitLabel || 'Answer';

      const doSubmit = () => {
        const val = inputEl.value;
        if (typeof toast.input.onSubmit === 'function') {
          toast.input.onSubmit(val, toast.id);
        }
      };

      submitBtn.onclick = doSubmit;
      inputEl.onkeydown = (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          doSubmit();
        }
      };
      actionsBar.appendChild(submitBtn);

      // Additional action buttons (e.g. Dismiss)
      if (Array.isArray(toast.actions)) {
        for (const act of toast.actions) {
          const actBtn = document.createElement('button');
          actBtn.type = 'button';
          actBtn.className = `btn btn-sm ${act.className || ''}`.trim();
          actBtn.textContent = act.label;
          actBtn.onclick = () => {
            if (typeof act.onClick === 'function') {
              act.onClick(toast);
            } else {
              dismissToast(toast.id);
            }
          };
          actionsBar.appendChild(actBtn);
        }
      }

      form.appendChild(actionsBar);
      el.appendChild(form);

      // Focus input promptly
      setTimeout(() => inputEl.focus(), 30);
    } else if (Array.isArray(toast.actions) && toast.actions.length > 0) {
      const actionsBar = document.createElement('div');
      actionsBar.className = 'toast-actions';
      for (const act of toast.actions) {
        const actBtn = document.createElement('button');
        actBtn.type = 'button';
        actBtn.className = `btn btn-sm ${act.className || ''}`.trim();
        actBtn.textContent = act.label;
        actBtn.onclick = () => {
          if (typeof act.onClick === 'function') {
            act.onClick(toast);
          } else {
            dismissToast(toast.id);
          }
        };
        actionsBar.appendChild(actBtn);
      }
      el.appendChild(actionsBar);
    }

    return el;
  }

  const unsub = uiStore.subscribe(render);
  render(uiStore.getState());

  return {
    destroy: () => unsub(),
  };
}
