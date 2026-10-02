// §55 Canonical Graph Model renderer
// Renders the WorkflowGraph specification (nodes, edges, ports) independent of UI-only schemas.
// Execution overlay (§53): node status rings and execution badges.

import { canvasStore, executionStore, uiStore } from './state.js';

export const NODE_KINDS = {
  input: { label: 'Input', color: '#58a6ff' },
  output: { label: 'Output', color: '#3fb950' },
  agent: { label: 'Agent', color: '#bc8cff' },
  tool: { label: 'Tool', color: '#f0883e' },
  condition: { label: 'Condition', color: '#d29922' },
  parallel: { label: 'Parallel', color: '#39c5bb' },
  join: { label: 'Join', color: '#56d364' },
  subworkflow: { label: 'Subworkflow', color: '#a371f7' },
  synthesis: { label: 'Synthesis', color: '#f778ba' },
  approval: { label: 'Approval', color: '#e3b341' },
  knowledge: { label: 'Knowledge', color: '#79c0ff' },
  evaluation: { label: 'Evaluation', color: '#db61a2' },
};

export const PORT_COLORS = {
  any: '#8b949e',
  text: '#58a6ff',
  json: '#f0883e',
  evidence: '#39c5bb',
  artifact: '#3fb950',
  control: '#bc8cff',
};

const STATUS_COLORS = {
  running: '#58a6ff',
  completed: '#3fb950',
  failed: '#f85149',
  paused: '#d29922',
  pending: '#8b949e',
  skipped: '#6e7681',
};

const NODE_WIDTH = 190;
const HEADER_HEIGHT = 28;
const PORT_HEIGHT = 20;

function escapeHtml(str) {
  if (!str) return '';
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

export function initGraphCanvas(containerEl, { onNodeMoved = null } = {}) {
  if (!containerEl) return null;

  containerEl.innerHTML = `
    <div class="canvas-empty-state" style="display: none;"></div>
    <svg class="canvas-svg" width="100%" height="100%">
      <defs>
        <marker id="arrow-default" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 1 L 10 5 L 0 9 z" fill="#6e7681" />
        </marker>
        <marker id="arrow-selected" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 1 L 10 5 L 0 9 z" fill="#58a6ff" />
        </marker>
      </defs>
      <g class="canvas-bg">
        <rect width="100%" height="100%" fill="transparent" />
      </g>
      <g class="canvas-viewport">
        <g class="edges-layer"></g>
        <g class="nodes-layer"></g>
      </g>
    </svg>
  `;

  const svg = containerEl.querySelector('.canvas-svg');
  const bg = containerEl.querySelector('.canvas-bg');
  const viewport = containerEl.querySelector('.canvas-viewport');
  const edgesLayer = containerEl.querySelector('.edges-layer');
  const nodesLayer = containerEl.querySelector('.nodes-layer');
  const emptyEl = containerEl.querySelector('.canvas-empty-state');

  let isPanning = false;
  let panStart = { x: 0, y: 0 };
  let draggingNode = null;
  let dragStart = { x: 0, y: 0, nodeX: 0, nodeY: 0 };
  let hasMoved = false;

  function getNodePos(node, index) {
    if (node.position && Array.isArray(node.position) && node.position.length >= 2) {
      return { x: node.position[0], y: node.position[1] };
    }
    // Simple autolayout column stagger
    const col = index % 3;
    const row = Math.floor(index / 3);
    return { x: 60 + col * 260, y: 60 + row * 160 };
  }

  function getNodeHeight(node) {
    const inputs = (node.inputs || []).length;
    const outputs = (node.outputs || []).length;
    const maxPorts = Math.max(inputs, outputs, 1);
    return HEADER_HEIGHT + maxPorts * PORT_HEIGHT + 14;
  }

  function getPortCoords(node, portName, isOutput, index) {
    const pos = getNodePos(node, index);
    const ports = isOutput ? node.outputs || [] : node.inputs || [];
    const pIdx = ports.findIndex((p) => p.name === portName);
    const validIdx = pIdx >= 0 ? pIdx : 0;
    const y = pos.y + HEADER_HEIGHT + 14 + validIdx * PORT_HEIGHT;
    const x = isOutput ? pos.x + NODE_WIDTH : pos.x;
    return { x, y };
  }

  function render() {
    const canvasState = canvasStore.getState();
    const execState = executionStore.getState();
    const graph = canvasState.graph;
    const vp = canvasState.viewport;

    // Apply viewport transform
    viewport.setAttribute('transform', `translate(${vp.x}, ${vp.y}) scale(${vp.zoom})`);

    if (!graph || !Array.isArray(graph.nodes) || graph.nodes.length === 0) {
      nodesLayer.innerHTML = '';
      edgesLayer.innerHTML = '';
      if (emptyEl) {
        emptyEl.style.display = 'flex';
        const err = canvasState.error;
        if (err) {
          emptyEl.innerHTML = `
            <div class="empty-icon">⚠️</div>
            <div class="empty-title">Workflow Graph Unavailable</div>
            <div class="empty-desc">${escapeHtml(err)}</div>
          `;
        } else {
          emptyEl.innerHTML = `
            <div class="empty-icon">◈</div>
            <div class="empty-title">Workflow Canvas Ready</div>
            <div class="empty-desc">Enter a task description above and click <strong>Generate proposal</strong> to preview the workflow graph, or <strong>Run</strong> to execute.</div>
          `;
        }
      }
      return;
    }

    if (emptyEl) emptyEl.style.display = 'none';

    const nodeMap = new Map();
    graph.nodes.forEach((n, idx) => nodeMap.set(n.id, { node: n, index: idx }));

    // Render Edges
    const edges = graph.edges || [];
    let edgesSvg = '';
    for (const edge of edges) {
      const src = nodeMap.get(edge.source);
      const tgt = nodeMap.get(edge.target);
      if (!src || !tgt) continue;

      const p1 = getPortCoords(src.node, edge.source_port, true, src.index);
      const p2 = getPortCoords(tgt.node, edge.target_port, false, tgt.index);

      const dx = Math.abs(p2.x - p1.x) * 0.5 + 20;
      const d = `M ${p1.x} ${p1.y} C ${p1.x + dx} ${p1.y}, ${p2.x - dx} ${p2.y}, ${p2.x} ${p2.y}`;

      const isSelected = (canvasState.selectedEdgeIds || []).includes(edge.id);
      const strokeColor = isSelected ? '#58a6ff' : '#6e7681';
      const marker = isSelected ? 'url(#arrow-selected)' : 'url(#arrow-default)';

      edgesSvg += `
        <g class="graph-edge ${isSelected ? 'selected' : ''}" data-edge-id="${edge.id}">
          <path d="${d}" fill="none" stroke="${strokeColor}" stroke-width="${isSelected ? 3 : 2}" marker-end="${marker}" />
        </g>
      `;
    }
    edgesLayer.innerHTML = edgesSvg;

    // Render Nodes
    let nodesSvg = '';
    graph.nodes.forEach((node, idx) => {
      const pos = getNodePos(node, idx);
      const height = getNodeHeight(node);
      const kindInfo = NODE_KINDS[node.kind] || { label: node.kind, color: '#8b949e' };
      const isSelected = (canvasState.selectedNodeIds || []).includes(node.id);
      const nodeStatus = execState.activeNodes[node.id] || node.status || (execState.executionId && execState.status === 'running' ? 'pending' : null);

      // Status ring for execution overlay (§53)
      let statusRing = '';
      let statusBadgeSvg = '';
      if (nodeStatus && STATUS_COLORS[nodeStatus]) {
        const ringColor = STATUS_COLORS[nodeStatus];
        const isRunning = nodeStatus === 'running';
        statusRing = `
          <rect x="-4" y="-4" width="${NODE_WIDTH + 8}" height="${height + 8}" rx="10" ry="10"
                fill="none" stroke="${ringColor}" stroke-width="2.5" class="status-ring status-${nodeStatus} ${isRunning ? 'pulse' : ''}" />
        `;
        statusBadgeSvg = `
          <rect x="${NODE_WIDTH - 64}" y="6" width="56" height="16" rx="3" ry="3" fill="#0d1117" stroke="${ringColor}" stroke-width="1" />
          <text x="${NODE_WIDTH - 36}" y="17" fill="${ringColor}" font-size="9" font-family="monospace" font-weight="600" text-anchor="middle">${nodeStatus}</text>
        `;
      }

      // Input port dots
      const inputs = node.inputs || [];
      const inputDots = inputs
        .map((p, pIdx) => {
          const py = HEADER_HEIGHT + 14 + pIdx * PORT_HEIGHT;
          const pColor = PORT_COLORS[p.type] || PORT_COLORS.any;
          return `
          <g class="port port-input" data-port="${escapeHtml(p.name)}">
            <circle cx="0" cy="${py}" r="5" fill="${pColor}" stroke="#161b22" stroke-width="2" />
            <text x="10" y="${py + 4}" fill="#8b949e" font-size="10" font-family="monospace">${escapeHtml(p.name)}</text>
          </g>
        `;
        })
        .join('');

      // Output port dots
      const outputs = node.outputs || [];
      const outputDots = outputs
        .map((p, pIdx) => {
          const py = HEADER_HEIGHT + 14 + pIdx * PORT_HEIGHT;
          const pColor = PORT_COLORS[p.type] || PORT_COLORS.any;
          return `
          <g class="port port-output" data-port="${escapeHtml(p.name)}">
            <circle cx="${NODE_WIDTH}" cy="${py}" r="5" fill="${pColor}" stroke="#161b22" stroke-width="2" />
            <text x="${NODE_WIDTH - 10}" y="${py + 4}" fill="#8b949e" font-size="10" font-family="monospace" text-anchor="end">${escapeHtml(p.name)}</text>
          </g>
        `;
        })
        .join('');

      const titleText = escapeHtml(node.title || node.id);
      const displayTitle = titleText.length > 16 ? titleText.slice(0, 15) + '…' : titleText;

      nodesSvg += `
        <g class="graph-node ${isSelected ? 'selected' : ''}" data-node-id="${node.id}" transform="translate(${pos.x}, ${pos.y})">
          ${statusRing}
          <!-- Node background -->
          <rect width="${NODE_WIDTH}" height="${height}" rx="6" ry="6" fill="#161b22" stroke="${isSelected ? '#58a6ff' : '#30363d'}" stroke-width="${isSelected ? 2 : 1}" />
          <!-- Kind header banner -->
          <rect width="${NODE_WIDTH}" height="${HEADER_HEIGHT}" rx="6" ry="6" fill="#1c2330" />
          <rect y="${HEADER_HEIGHT - 3}" width="${NODE_WIDTH}" height="3" fill="${kindInfo.color}" />
          <!-- Header text -->
          <text x="10" y="18" fill="#e6edf3" font-size="12" font-weight="600">${displayTitle}</text>
          ${statusBadgeSvg ? statusBadgeSvg : `<text x="${NODE_WIDTH - 10}" y="18" fill="${kindInfo.color}" font-size="10" font-family="monospace" text-anchor="end">${kindInfo.label}</text>`}
          ${inputDots}
          ${outputDots}
        </g>
      `;
    });
    nodesLayer.innerHTML = nodesSvg;
  }

  // Pan interaction
  bg.addEventListener('mousedown', (e) => {
    isPanning = true;
    panStart = { x: e.clientX, y: e.clientY };
    canvasStore.setState({ selectedNodeIds: [], selectedEdgeIds: [] });
  });

  window.addEventListener('mousemove', (e) => {
    if (isPanning) {
      const dx = e.clientX - panStart.x;
      const dy = e.clientY - panStart.y;
      panStart = { x: e.clientX, y: e.clientY };
      canvasStore.setState((s) => ({
        viewport: { ...s.viewport, x: s.viewport.x + dx, y: s.viewport.y + dy },
      }));
    } else if (draggingNode) {
      hasMoved = true;
      const dx = (e.clientX - dragStart.x) / canvasStore.getState().viewport.zoom;
      const dy = (e.clientY - dragStart.y) / canvasStore.getState().viewport.zoom;
      const newX = Math.round(dragStart.nodeX + dx);
      const newY = Math.round(dragStart.nodeY + dy);

      canvasStore.setState((s) => {
        if (!s.graph || !Array.isArray(s.graph.nodes)) return {};
        const nextNodes = s.graph.nodes.map((n) => {
          if (n.id === draggingNode.id) {
            return { ...n, position: [newX, newY] };
          }
          return n;
        });
        return { graph: { ...s.graph, nodes: nextNodes } };
      });
    }
  });

  window.addEventListener('mouseup', () => {
    if (isPanning) {
      isPanning = false;
    }
    if (draggingNode) {
      if (hasMoved && onNodeMoved) {
        onNodeMoved(canvasStore.getState().graph);
      }
      draggingNode = null;
      hasMoved = false;
    }
  });

  // Node selection & drag
  nodesLayer.addEventListener('mousedown', (e) => {
    const nodeEl = e.target.closest('.graph-node');
    if (!nodeEl) return;
    e.stopPropagation();

    const nodeId = nodeEl.getAttribute('data-node-id');
    if (!nodeId) return;

    const canvasState = canvasStore.getState();
    const graph = canvasState.graph;
    const node = graph?.nodes?.find((n) => n.id === nodeId);
    if (!node) return;

    canvasStore.setState({ selectedNodeIds: [nodeId] });
    uiStore.setState({ selectedResource: null });

    const nodeIdx = graph.nodes.indexOf(node);
    const pos = getNodePos(node, nodeIdx);

    draggingNode = node;
    dragStart = {
      x: e.clientX,
      y: e.clientY,
      nodeX: pos.x,
      nodeY: pos.y,
    };
    hasMoved = false;
  });

  // Zoom interaction: wheel zooms around the pointer, buttons around the
  // viewport center, so pushing Zoom In never drifts the graph toward (0,0).
  function zoomAt(factor, cx, cy) {
    canvasStore.setState((s) => {
      const { zoom, x, y } = s.viewport;
      const newZoom = Math.min(Math.max(zoom * factor, 0.2), 3);
      const k = newZoom / zoom;
      return {
        viewport: {
          zoom: newZoom,
          x: cx - (cx - x) * k,
          y: cy - (cy - y) * k,
        },
      };
    });
  }

  function viewportCenter() {
    const rect = svg.getBoundingClientRect();
    return { cx: rect.width / 2, cy: rect.height / 2, rect };
  }

  // Center every node in the visible viewport (`Fit`).
  function fitToView() {
    const graph = canvasStore.getState().graph;
    if (!graph || !Array.isArray(graph.nodes) || graph.nodes.length === 0) return;

    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    graph.nodes.forEach((node, idx) => {
      const pos = getNodePos(node, idx);
      minX = Math.min(minX, pos.x);
      minY = Math.min(minY, pos.y);
      maxX = Math.max(maxX, pos.x + NODE_WIDTH);
      maxY = Math.max(maxY, pos.y + getNodeHeight(node));
    });

    const { cx, cy, rect } = viewportCenter();
    if (rect.width < 40 || rect.height < 40) return; // container not laid out yet

    const pad = 48;
    const fitZoom = Math.min(
      (rect.width - pad * 2) / Math.max(maxX - minX, 1),
      (rect.height - pad * 2) / Math.max(maxY - minY, 1),
      1.25
    );
    const zoom = Math.min(Math.max(fitZoom, 0.2), 3);

    canvasStore.setState({
      viewport: {
        zoom,
        x: cx - ((minX + maxX) / 2) * zoom,
        y: cy - ((minY + maxY) / 2) * zoom,
      },
    });
  }

  svg.addEventListener('wheel', (e) => {
    e.preventDefault();
    const rect = svg.getBoundingClientRect();
    const factor = e.deltaY < 0 ? 1.1 : 0.9;
    zoomAt(factor, e.clientX - rect.left, e.clientY - rect.top);
  });

  // Subscriptions to stores
  const unsubCanvas = canvasStore.subscribe(render);
  const unsubExec = executionStore.subscribe(render);

  render();

  return {
    destroy: () => {
      unsubCanvas();
      unsubExec();
    },
    zoomIn: () => {
      const { cx, cy } = viewportCenter();
      zoomAt(1.2, cx, cy);
    },
    zoomOut: () => {
      const { cx, cy } = viewportCenter();
      zoomAt(1 / 1.2, cx, cy);
    },
    resetView: () => {
      canvasStore.setState({ viewport: { x: 0, y: 0, zoom: 1 } });
    },
    fitToView,
  };
}
