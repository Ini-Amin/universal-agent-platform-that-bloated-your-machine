// §44/§45 WebSocket client + event log renderer
// Reconnect-safe with exponential backoff and seq resync

import { authHeaders, getApiToken } from './api.js';
import { eventStore, executionStore } from './state.js';

export function createEventStream() {
  let ws = null;
  let currentExecutionId = null;
  let lastSeenSeq = 0;
  let reconnectTimer = null;
  let reconnectDelay = 500;
  const MAX_RECONNECT_DELAY = 5000;
  let manuallyClosed = false;
  // Connection state surfaced to the header badge + runtime controls:
  // idle | connecting | open | reconnecting | closed
  let connectionStatus = 'idle';
  const statusListeners = new Set();
  // Approval events and pause states change GET /tasks/{id}'s pending_approvals.
  // Refetch it at most once per window (a reconnect replays every event) and
  // never with two requests in flight.
  const TASK_SYNC_DELAY_MS = 250;
  let taskSyncTimer = null;
  let taskSyncRunning = false;
  let taskSyncQueued = false;

  function setStatus(next) {
    if (next === connectionStatus) return;
    connectionStatus = next;
    for (const fn of statusListeners) {
      try {
        fn(connectionStatus);
      } catch (err) {
        console.error('EventStream status listener error:', err);
      }
    }
  }

  function getWsUrl(executionId) {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Browsers cannot set headers on a WS handshake, so the token rides as a
    // query param (the one channel the server accepts; see server/auth.py).
    const token = getApiToken();
    const query = token ? `?token=${encodeURIComponent(token)}` : '';
    return `${proto}//${window.location.host}/ws/executions/${encodeURIComponent(executionId)}${query}`;
  }

  function connect(executionId) {
    if (ws) {
      disconnect();
    }
    // A new execution means a new log: without this, events from the previous
    // run stayed in the ring buffer and the panel showed two runs interleaved
    // (and, with the old append-only code, the same run three times over).
    if (currentExecutionId !== executionId) {
      lastSeenSeq = 0;
      eventStore.setState({ events: [] });
      _cancelTaskSync();
    }
    currentExecutionId = executionId;
    manuallyClosed = false;
    setStatus('connecting');
    _openSocket();
  }

  function _openSocket() {
    if (!currentExecutionId || manuallyClosed) return;

    try {
      ws = new WebSocket(getWsUrl(currentExecutionId));
    } catch (err) {
      console.error('WebSocket creation error:', err);
      setStatus('reconnecting');
      _scheduleReconnect();
      return;
    }

    ws.onopen = () => {
      setStatus('open');
      reconnectDelay = 500; // Reset backoff
      if (lastSeenSeq > 0) {
        // Reconnected: request resync from last seen sequence
        send({ type: 'resync', after_seq: lastSeenSeq });
      }
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        _handleMessage(msg);
      } catch (err) {
        console.error('Failed to parse WebSocket message:', err, event.data);
      }
    };

    ws.onerror = (err) => {
      console.warn('WebSocket error:', err);
    };

    ws.onclose = () => {
      ws = null;
      if (!manuallyClosed) {
        setStatus('reconnecting');
        _scheduleReconnect();
      }
    };
  }

  function _scheduleReconnect() {
    if (reconnectTimer || manuallyClosed) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      reconnectDelay = Math.min(reconnectDelay * 1.5, MAX_RECONNECT_DELAY);
      _openSocket();
    }, reconnectDelay);
  }

  async function _syncTaskDetails(executionId) {
    if (!executionId) return;
    try {
      const res = await fetch(`/tasks/${encodeURIComponent(executionId)}`, { headers: authHeaders() });
      if (res.ok) {
        const task = await res.json();
        if (executionStore.getState().executionId === executionId) {
          executionStore.setState({
            status: task.status || executionStore.getState().status,
            output: task.output !== undefined && task.output !== null ? task.output : executionStore.getState().output,
            error: task.error !== undefined && task.error !== null ? task.error : executionStore.getState().error,
            artifacts: Array.isArray(task.artifacts) ? task.artifacts : (executionStore.getState().artifacts || []),
            task,
          });
        }
      }
    } catch {
      // ignore network errors during poll
    }
  }

  function _scheduleTaskSync() {
    if (taskSyncTimer) return;
    taskSyncTimer = setTimeout(async () => {
      taskSyncTimer = null;
      if (taskSyncRunning) {
        taskSyncQueued = true;
        return;
      }
      taskSyncRunning = true;
      await _syncTaskDetails(currentExecutionId);
      taskSyncRunning = false;
      if (taskSyncQueued) {
        taskSyncQueued = false;
        _scheduleTaskSync();
      }
    }, TASK_SYNC_DELAY_MS);
  }

  function _cancelTaskSync() {
    if (taskSyncTimer) clearTimeout(taskSyncTimer);
    taskSyncTimer = null;
    taskSyncQueued = false;
  }

  function _handleMessage(msg) {
    if (!msg || typeof msg !== 'object') return;

    if (msg.type === 'status') {
      const statusData = msg.status || {};
      const newStatus = statusData.status || 'unknown';
      executionStore.setState({
        status: newStatus,
        resume_count: statusData.resume_count ?? 0,
        error: statusData.error || null,
        output: statusData.output || null,
        artifacts: Array.isArray(statusData.artifacts) ? statusData.artifacts : [],
      });
      if (currentExecutionId && (newStatus === 'completed' || newStatus === 'failed')) {
        _syncTaskDetails(currentExecutionId);
      } else if (newStatus === 'paused' || newStatus === 'awaiting_approval') {
        _scheduleTaskSync();
      }
    } else if (msg.type === 'event') {
      const ev = msg.event || {};
      const seq = msg.seq || ev.seq || lastSeenSeq + 1;
      lastSeenSeq = Math.max(lastSeenSeq, seq);

      eventStore.setState((s) => {
        // Deduplicate by seq. A reconnect issues `resync {after_seq}` and the
        // server may also replay from an earlier point, so the same event can
        // arrive more than once. Without this, every reconnect multiplied the
        // visible log (observed live: each event rendered 3x).
        const existing = new Set(s.events.map((e) => e.seq));
        if (existing.has(seq)) return {};
        const nextEvents = [...s.events, { ...ev, seq }];
        if (nextEvents.length > s.max) {
          nextEvents.splice(0, nextEvents.length - s.max);
        }
        return { events: nextEvents };
      });

      // Update node status overlay from lifecycle events
      const kind = ev.kind || '';
      // `approval` (event bus) and `approval_requested` / `approval_decided` (durable events).
      if (kind.includes('approval')) _scheduleTaskSync();
      const nodeId = ev.node || (ev.payload && (ev.payload.node_id || ev.payload.node));
      if (nodeId) {
        let nodeStatus = null;
        if (kind.includes('started')) nodeStatus = 'running';
        else if (kind.includes('finished') || kind.includes('completed')) nodeStatus = 'completed';
        else if (kind.includes('failed') || kind.includes('error')) nodeStatus = 'failed';
        else if (kind.includes('paused')) nodeStatus = 'paused';
        else if (kind.includes('skipped')) nodeStatus = 'skipped';

        if (nodeStatus) {
          executionStore.setState((s) => ({
            activeNodes: { ...s.activeNodes, [nodeId]: nodeStatus },
          }));
        }
      }

      if (kind.includes('task_finished') || kind.includes('finished') || kind.includes('completed') || kind.includes('failed')) {
        if (currentExecutionId) {
          _syncTaskDetails(currentExecutionId);
        }
      }
    } else if (msg.type === 'pong') {
      // Heartbeat ok
    } else if (msg.type === 'forked') {
      console.info('Execution forked:', msg.execution_id);
    } else if (msg.type === 'error') {
      console.warn('Server error via WebSocket:', msg.message);
    }
  }

  function send(data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(data));
      return true;
    }
    return false;
  }

  function disconnect() {
    manuallyClosed = true;
    _cancelTaskSync();
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (ws) {
      ws.close();
      ws = null;
    }
    currentExecutionId = null;
    setStatus('closed');
  }

  function pause() {
    return send({ type: 'pause' });
  }

  function resume() {
    return send({ type: 'resume' });
  }

  function fork(label = '') {
    return send({ type: 'fork', label });
  }

  function resync(afterSeq = 0) {
    send({ type: 'resync', after_seq: afterSeq });
  }

  function ping() {
    send({ type: 'ping' });
  }

  return {
    connect,
    disconnect,
    send,
    pause,
    resume,
    fork,
    resync,
    ping,
    getStatus: () => connectionStatus,
    // True when the socket was closed on purpose (switching execution, page
    // teardown) rather than dropped. Callers use it to avoid warning the user
    // about a disconnect they asked for.
    isDeliberatelyClosed: () => manuallyClosed,
    onStatusChange: (fn) => {
      statusListeners.add(fn);
      return () => statusListeners.delete(fn);
    },
  };
}

// Renders the virtualized event log into the console container
export function initEventConsole(containerEl) {
  if (!containerEl) return;

  function render(events) {
    const slice = events.slice(-200);

    if (slice.length === 0) {
      // Honest empty state: a blank console is indistinguishable from broken.
      containerEl.innerHTML = '<div class="console-empty">No events yet — run a task or select an execution to stream lifecycle events here.</div>';
      return;
    }

    const fragment = document.createDocumentFragment();

    for (const ev of slice) {
      const row = document.createElement('div');
      row.className = 'event-row';

      const seqSpan = document.createElement('span');
      seqSpan.className = 'event-seq';
      seqSpan.textContent = `#${ev.seq ?? ''}`;

      const kindSpan = document.createElement('span');
      kindSpan.className = `event-kind kind-${(ev.kind || 'generic').replace(/_/g, '-')}`;
      kindSpan.textContent = ev.kind || 'unknown';

      const nodeSpan = document.createElement('span');
      nodeSpan.className = 'event-node';
      nodeSpan.textContent = ev.node || (ev.payload && ev.payload.node) || '';

      const detailSpan = document.createElement('span');
      detailSpan.className = 'event-detail';
      const payloadStr = ev.payload ? JSON.stringify(ev.payload) : '';
      detailSpan.textContent = payloadStr;

      row.appendChild(seqSpan);
      row.appendChild(kindSpan);
      if (nodeSpan.textContent) row.appendChild(nodeSpan);
      row.appendChild(detailSpan);
      fragment.appendChild(row);
    }

    containerEl.innerHTML = '';
    containerEl.appendChild(fragment);
    containerEl.scrollTop = containerEl.scrollHeight;
  }

  return eventStore.subscribe((state) => {
    render(state.events);
  });
}
