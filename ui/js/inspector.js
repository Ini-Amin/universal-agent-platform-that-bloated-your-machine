// §53 Execution UI & Inspector Pane
// Shows selected node details + referenced resources, execution status/metrics,
// decision traces, library/system resources, artifact content viewer,
// and runtime controls (Pause/Resume/Fork).

import {
  getExecutionTraces,
  pauseExecution,
  resumeExecution,
  getKnowledgeProvenance,
  getArtifactContent,
  getAgents,
  getTools,
  getSkills,
} from './api.js';
import { canvasStore, executionStore, uiStore } from './state.js';
import { showToast, showPromptToast, dismissToast } from './toast.js';

function escapeHtml(str) {
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

export function initInspector(containerEl, { eventStream = null } = {}) {
  if (!containerEl) return null;

  let currentTraces = [];
  let lastFetchedExecutionId = null;

  async function fetchTraces(executionId) {
    if (!executionId || executionId === lastFetchedExecutionId) return;
    try {
      lastFetchedExecutionId = executionId;
      currentTraces = await getExecutionTraces(executionId);
    } catch {
      currentTraces = [];
    }
    render();
  }

  function render() {
    const canvasState = canvasStore.getState();
    const execState = executionStore.getState();
    const uiState = uiStore.getState();

    const graph = canvasState.graph;
    const selectedNodeId = (canvasState.selectedNodeIds || [])[0];
    const selectedNode = graph && Array.isArray(graph.nodes) ? graph.nodes.find((n) => n.id === selectedNodeId) : null;

    if (execState.executionId && execState.executionId !== lastFetchedExecutionId) {
      fetchTraces(execState.executionId);
    }

    // 1. Contextual view: Selected Library / System Resource
    if (uiState.selectedResource) {
      renderResourceInspector(uiState.selectedResource);
      return;
    }

    // 2. Contextual view: Selected Graph Node
    if (selectedNode) {
      renderNodeInspector(selectedNode);
      return;
    }

    // 3. Default view: Execution Overview + Output & Artifacts + Controls + Traces
    renderExecutionOverview(execState, graph);
  }

  // --- 1. Resource Inspector (Agents, Tools, Skills, Knowledge, Models, MCP, Policies, Artifacts) ---
  function renderResourceInspector(resource) {
    const { type, data } = resource;
    let bodyHtml = '';

    const backBtn = `
      <div class="inspector-nav-back">
        <button id="btn-back-to-run" class="btn btn-sm">← Back to Overview</button>
      </div>
    `;

    if (type === 'agent') {
      const caps = Array.isArray(data?.capabilities) ? data.capabilities : [];
      bodyHtml = `
        <div class="inspector-section">
          <div class="section-title">Agent Resource</div>
          <h2 class="resource-title">${escapeHtml(data?.name || 'Unknown Agent')}</h2>
          <div class="meta-grid" style="margin-top: 10px;">
            <span class="meta-label">Type:</span>
            <span class="meta-value"><span class="badge badge-kind">Agent</span></span>
            <span class="meta-label">Capabilities:</span>
            <span class="meta-value">${caps.length} defined</span>
          </div>
          <div style="margin-top: 12px;">
            <h4 style="font-size: 11px; color: var(--fg-muted); margin-bottom: 6px;">Capabilities</h4>
            <div class="ref-badges">
              ${caps.map((c) => `<span class="badge badge-accent">${escapeHtml(c)}</span>`).join('') || '<span class="text-muted">None specified</span>'}
            </div>
          </div>
        </div>
      `;
    } else if (type === 'tool') {
      const riskTier = data?.risk_tier !== undefined ? data.risk_tier : '?';
      const riskLabel = data?.risk_label || 'Unrated';
      const schemaStr = data?.input_schema ? JSON.stringify(data.input_schema, null, 2) : '{}';

      bodyHtml = `
        <div class="inspector-section">
          <div class="section-title">Tool Resource</div>
          <h2 class="resource-title">${escapeHtml(data?.name || 'Unknown Tool')}</h2>
          <div class="meta-grid" style="margin-top: 10px;">
            <span class="meta-label">Risk Tier:</span>
            <span class="meta-value"><span class="badge badge-warn">Tier ${escapeHtml(riskTier)}: ${escapeHtml(riskLabel)}</span></span>
            <span class="meta-label">Source:</span>
            <span class="meta-value"><span class="badge badge-kind">${escapeHtml(data?.source || 'local')}</span></span>
          </div>
          <div style="margin-top: 12px;">
            <h4 style="font-size: 11px; color: var(--fg-muted); margin-bottom: 4px;">Description</h4>
            <p class="text-muted" style="line-height: 1.4;">${escapeHtml(data?.description || 'No description provided.')}</p>
          </div>
          <div style="margin-top: 12px;">
            <h4 style="font-size: 11px; color: var(--fg-muted); margin-bottom: 4px;">Input Schema</h4>
            <pre class="code-block output-code-block">${escapeHtml(schemaStr)}</pre>
          </div>
        </div>
      `;
    } else if (type === 'skill') {
      bodyHtml = `
        <div class="inspector-section">
          <div class="section-title">Skill Resource</div>
          <h2 class="resource-title">${escapeHtml(data?.name || 'Unknown Skill')}</h2>
          <div class="meta-grid" style="margin-top: 10px;">
            <span class="meta-label">Domain:</span>
            <span class="meta-value"><span class="badge badge-kind">${escapeHtml(data?.domain || 'general')}</span></span>
          </div>
          <div style="margin-top: 12px;">
            <h4 style="font-size: 11px; color: var(--fg-muted); margin-bottom: 4px;">Description</h4>
            <p class="text-muted">${escapeHtml(data?.description || 'No description.')}</p>
          </div>
        </div>
      `;
    } else if (type === 'knowledge') {
      const confPct = Math.round((data?.confidence || 0) * 100);
      bodyHtml = `
        <div class="inspector-section">
          <div class="section-title">Knowledge Statement</div>
          <p class="resource-statement" style="font-size: 13px; font-weight: 500; margin: 8px 0;">${escapeHtml(data?.statement || '')}</p>
          <div class="meta-grid" style="margin-top: 10px;">
            <span class="meta-label">ID:</span>
            <span class="meta-value font-mono">${escapeHtml(data?.knowledge_id || 'unknown')}</span>
            <span class="meta-label">Domain:</span>
            <span class="meta-value">${escapeHtml(data?.domain || 'general')}</span>
            <span class="meta-label">Status:</span>
            <span class="meta-value"><span class="badge badge-${data?.status || 'kind'}">${escapeHtml(data?.status || 'unverified')}</span></span>
            <span class="meta-label">Confidence:</span>
            <span class="meta-value font-mono">${confPct}%</span>
            <span class="meta-label">Provenance:</span>
            <span class="meta-value font-mono">${data?.provenance_count ?? 0} references</span>
          </div>
          <div style="margin-top: 14px;">
            <h4 style="font-size: 11px; color: var(--fg-muted); margin-bottom: 6px;">Provenance Records</h4>
            <div id="knowledge-provenance-list" class="text-muted font-mono" style="font-size: 11px;">Loading provenance…</div>
          </div>
        </div>
      `;
    } else if (type === 'model') {
      const caps = Array.isArray(data?.capabilities) ? data.capabilities : [];
      bodyHtml = `
        <div class="inspector-section">
          <div class="section-title">System Model</div>
          <h2 class="resource-title font-mono">${escapeHtml(data?.id || 'Unknown')}</h2>
          <div class="meta-grid" style="margin-top: 10px;">
            <span class="meta-label">Provider:</span>
            <span class="meta-value">${escapeHtml(data?.provider || 'unknown')}</span>
          </div>
          <div style="margin-top: 12px;">
            <h4 style="font-size: 11px; color: var(--fg-muted); margin-bottom: 6px;">Capabilities</h4>
            <div class="ref-badges">
              ${caps.map((c) => `<span class="badge badge-accent">${escapeHtml(c)}</span>`).join('') || '<span class="text-muted">None</span>'}
            </div>
          </div>
        </div>
      `;
    } else if (type === 'mcp') {
      const tools = Array.isArray(data?.tools) ? data.tools : [];
      const isRunning = Boolean(data?.running);
      bodyHtml = `
        <div class="inspector-section">
          <div class="section-title">MCP Server</div>
          <h2 class="resource-title">${escapeHtml(data?.name || 'MCP Server')}</h2>
          <div class="meta-grid" style="margin-top: 10px;">
            <span class="meta-label">Status:</span>
            <span class="meta-value"><span class="badge ${isRunning ? 'badge-ok' : 'badge-kind'}">${isRunning ? 'running' : 'stopped'}</span></span>
            <span class="meta-label">Command:</span>
            <span class="meta-value font-mono" style="font-size: 11px;">${escapeHtml(data?.command || 'none')}</span>
          </div>
          <div style="margin-top: 12px;">
            <h4 style="font-size: 11px; color: var(--fg-muted); margin-bottom: 6px;">Exposed Tools (${tools.length})</h4>
            <div class="ref-badges">
              ${tools.map((t) => `<span class="badge badge-kind">${escapeHtml(t)}</span>`).join('') || '<span class="text-muted">No tools exposed</span>'}
            </div>
          </div>
        </div>
      `;
    } else if (type === 'policy') {
      const isAllow = data?.effect === 'allow';
      bodyHtml = `
        <div class="inspector-section">
          <div class="section-title">Security Policy</div>
          <h2 class="resource-title font-mono">${escapeHtml(data?.id || 'Policy')}</h2>
          <div class="meta-grid" style="margin-top: 10px;">
            <span class="meta-label">Effect:</span>
            <span class="meta-value"><span class="badge ${isAllow ? 'badge-ok' : 'badge-err'}">${escapeHtml(data?.effect || 'deny')}</span></span>
            <span class="meta-label">Priority:</span>
            <span class="meta-value font-mono">${data?.priority ?? 0}</span>
            <span class="meta-label">Subject:</span>
            <span class="meta-value font-mono">${escapeHtml(data?.subject || '*')}</span>
            <span class="meta-label">Action:</span>
            <span class="meta-value font-mono">${escapeHtml(data?.action || '*')}</span>
            <span class="meta-label">Resource:</span>
            <span class="meta-value font-mono">${escapeHtml(data?.resource_pattern || '*')}</span>
          </div>
          <div style="margin-top: 12px;">
            <h4 style="font-size: 11px; color: var(--fg-muted); margin-bottom: 4px;">Reason / Condition</h4>
            <p class="text-muted">${escapeHtml(data?.reason || 'No description.')}</p>
          </div>
        </div>
      `;
    } else if (type === 'integrations') {
      bodyHtml = `
        <div class="inspector-section">
          <div class="section-title">Integrations</div>
          <h2 class="resource-title">External Integrations</h2>
          <p class="text-muted" style="margin-top: 12px;">No external integrations configured in this environment.</p>
        </div>
      `;
    } else if (type === 'artifact') {
      const art = data || {};
      const execState = executionStore.getState();
      const isStub = (execState.evidence_source === 'deterministic-stubs') || (execState.task?.evidence_source === 'deterministic-stubs');

      const stubNotice = isStub ? `
        <div class="evidence-stub-badge" style="margin: 10px 0;">
          <span class="badge badge-warn">deterministic-stub evidence</span>
          <div style="margin-top: 4px;">This artifact was generated by a deterministic stub collector. Never treat stub data as confirmed live vulnerability evidence.</div>
        </div>
      ` : '';

      bodyHtml = `
        <div class="inspector-section">
          <div class="section-title">Artifact Viewer</div>
          <h2 class="resource-title font-mono" style="font-size: 13px;">${escapeHtml(art.uri || art.type || 'Artifact')}</h2>
          ${stubNotice}
          <div class="meta-grid" style="margin-top: 10px;">
            <span class="meta-label">Index:</span>
            <span class="meta-value font-mono">${art.index ?? 0}</span>
            <span class="meta-label">Type:</span>
            <span class="meta-value"><span class="badge badge-kind">${escapeHtml(art.type || 'artifact')}</span></span>
            <span class="meta-label">Source:</span>
            <span class="meta-value font-mono">${escapeHtml(art.source || 'unknown')}</span>
            <span class="meta-label">URI:</span>
            <span class="meta-value font-mono">${escapeHtml(art.uri || 'none')}</span>
          </div>
          <div style="margin-top: 14px;">
            <div class="output-header">
              <span class="meta-label">Content:</span>
              <button id="btn-copy-artifact-content" class="btn btn-sm" type="button" disabled>Copy</button>
            </div>
            <div id="artifact-content-viewer" class="text-muted font-mono" style="font-size: 11px;">Loading artifact content…</div>
          </div>
        </div>
      `;
    }

    containerEl.innerHTML = backBtn + bodyHtml;

    // Back button listener
    const btnBack = containerEl.querySelector('#btn-back-to-run');
    if (btnBack) {
      btnBack.onclick = () => {
        uiStore.setState({ selectedResource: null });
      };
    }

    // Async data loading for knowledge provenance
    if (type === 'knowledge' && data?.knowledge_id) {
      const provEl = containerEl.querySelector('#knowledge-provenance-list');
      getKnowledgeProvenance(data.knowledge_id)
        .then((res) => {
          if (!provEl) return;
          const list = res?.provenance || [];
          if (!list || list.length === 0) {
            provEl.innerHTML = '<div class="text-muted font-sans">No provenance entries available.</div>';
            return;
          }
          provEl.innerHTML = list
            .map((p) => `
              <div class="provenance-card" style="background: var(--panel-2); border: 1px solid var(--border); padding: 6px 8px; border-radius: 4px; margin-bottom: 6px;">
                <div style="color: var(--accent);">${escapeHtml(p.source || 'source')}</div>
                <div class="text-muted" style="font-size: 10px;">${escapeHtml(p.ts || p.timestamp || '')}</div>
                ${p.detail ? `<div style="color: var(--fg); margin-top: 2px;">${escapeHtml(p.detail)}</div>` : ''}
              </div>
            `)
            .join('');
        })
        .catch(() => {
          if (provEl) provEl.innerHTML = '<div class="text-muted font-sans">Provenance endpoint offline or returned 404.</div>';
        });
    }

    // Async data loading for artifact content
    if (type === 'artifact' && data?.taskId && data.index !== undefined) {
      const viewerEl = containerEl.querySelector('#artifact-content-viewer');
      const copyBtn = containerEl.querySelector('#btn-copy-artifact-content');
      getArtifactContent(data.taskId, data.index)
        .then((res) => {
          if (!viewerEl) return;
          const text = res?.content !== undefined ? res.content : '';
          viewerEl.outerHTML = `<pre id="artifact-content-viewer" class="code-block output-code-block">${escapeHtml(text)}</pre>`;
          if (copyBtn) {
            copyBtn.disabled = false;
            copyBtn.onclick = async () => {
              try {
                if (navigator.clipboard?.writeText) {
                  await navigator.clipboard.writeText(text);
                }
                copyBtn.textContent = 'Copied';
                setTimeout(() => { if (copyBtn) copyBtn.textContent = 'Copy'; }, 1500);
              } catch (e) {
                console.error(e);
              }
            };
          }
        })
        .catch((err) => {
          if (viewerEl) viewerEl.innerHTML = `<div class="text-muted font-sans">${escapeHtml(err.message || 'Content unavailable or binary format.')}</div>`;
        });
    }
  }

  // --- 2. Node Inspector (Title, Ports, Config, Referenced Resources) ---
  function renderNodeInspector(selectedNode) {
    const execState = executionStore.getState();
    const nodeStatus = execState.activeNodes[selectedNode.id] || selectedNode.status || (execState.executionId && execState.status === 'running' ? 'pending' : null);

    const inputsHtml = (selectedNode.inputs || [])
      .map((p) => `<div class="port-item"><span class="port-type">${escapeHtml(p.type)}</span> <strong>${escapeHtml(p.name)}</strong></div>`)
      .join('') || '<div class="text-muted">None</div>';

    const outputsHtml = (selectedNode.outputs || [])
      .map((p) => `<div class="port-item"><span class="port-type">${escapeHtml(p.type)}</span> <strong>${escapeHtml(p.name)}</strong></div>`)
      .join('') || '<div class="text-muted">None</div>';

    const configJson = selectedNode.config ? JSON.stringify(selectedNode.config, null, 2) : '{}';

    // Parse REAL referenced resources from node config (§53 / product directive)
    const config = selectedNode.config || {};
    const refItems = [];

    // Check agent references
    const agentRef = config.agent || config.agent_name;
    if (agentRef && typeof agentRef === 'string') {
      refItems.push({ type: 'agent', name: agentRef, label: `Agent: ${agentRef}` });
    }

    // Check tool references
    const toolRef = config.tool || config.tool_name || config.collector;
    if (toolRef && typeof toolRef === 'string') {
      refItems.push({ type: 'tool', name: toolRef, label: `Tool: ${toolRef}` });
    }
    if (Array.isArray(config.tools)) {
      for (const t of config.tools) {
        if (typeof t === 'string') refItems.push({ type: 'tool', name: t, label: `Tool: ${t}` });
      }
    }

    // Check skill references
    const skillRef = config.skill || config.skill_name;
    if (skillRef && typeof skillRef === 'string') {
      refItems.push({ type: 'skill', name: skillRef, label: `Skill: ${skillRef}` });
    }
    if (Array.isArray(config.skills)) {
      for (const s of config.skills) {
        if (typeof s === 'string') refItems.push({ type: 'skill', name: s, label: `Skill: ${s}` });
      }
    }

    // Check synthesizer references
    if (config.synthesizer && typeof config.synthesizer === 'string') {
      refItems.push({ type: 'synthesizer', name: config.synthesizer, label: `Synthesizer: ${config.synthesizer}` });
    }

    let refSectionHtml = '';
    if (refItems.length > 0) {
      const buttonsHtml = refItems
        .map((r) => {
          if (r.type === 'synthesizer') {
            return `<span class="badge badge-kind">${escapeHtml(r.label)}</span>`;
          }
          return `<button type="button" class="btn btn-sm btn-inspect-ref" data-ref-type="${r.type}" data-ref-name="${escapeHtml(r.name)}">${escapeHtml(r.label)}</button>`;
        })
        .join(' ');

      refSectionHtml = `
        <div class="inspector-section">
          <h4 style="font-size: 11px; color: var(--fg-muted); margin-bottom: 6px;">Referenced Resources</h4>
          <div class="ref-badges" style="display: flex; flex-wrap: wrap; gap: 6px;">
            ${buttonsHtml}
          </div>
        </div>
      `;
    }

    const html = `
      <div class="inspector-nav-back">
        <button id="btn-deselect-node" class="btn btn-sm">← Back to Overview</button>
      </div>
      <div class="inspector-section">
        <h3 class="section-title">Selected Node: ${escapeHtml(selectedNode.title || selectedNode.id)}</h3>
        <div class="meta-grid">
          <span class="meta-label">ID:</span>
          <span class="meta-value font-mono">${escapeHtml(selectedNode.id)}</span>
          <span class="meta-label">Kind:</span>
          <span class="meta-value"><span class="badge badge-kind">${escapeHtml(selectedNode.kind)}</span></span>
          <span class="meta-label">Status:</span>
          <span class="meta-value"><span class="badge badge-${nodeStatus || 'kind'}">${nodeStatus || 'pending'}</span></span>
          <span class="meta-label">Version Ref:</span>
          <span class="meta-value font-mono">${escapeHtml(selectedNode.version_ref || 'unpinned')}</span>
        </div>
        <div class="ports-block">
          <h4>Inputs</h4>
          ${inputsHtml}
          <h4>Outputs</h4>
          ${outputsHtml}
        </div>
        <div class="config-block">
          <h4>Config</h4>
          <pre class="code-block">${escapeHtml(configJson)}</pre>
        </div>
      </div>
      ${refSectionHtml}
    `;

    containerEl.innerHTML = html;

    const btnDeselect = containerEl.querySelector('#btn-deselect-node');
    if (btnDeselect) {
      btnDeselect.onclick = () => {
        canvasStore.setState({ selectedNodeIds: [] });
      };
    }

    containerEl.querySelectorAll('.btn-inspect-ref').forEach((btn) => {
      btn.onclick = async () => {
        const refType = btn.getAttribute('data-ref-type');
        const refName = btn.getAttribute('data-ref-name');
        if (!refType || !refName) return;

        if (refType === 'agent') {
          try {
            const list = await getAgents();
            const found = (list || []).find((a) => a.name === refName);
            uiStore.setState({ selectedResource: { type: 'agent', data: found || { name: refName, capabilities: [] } } });
          } catch {
            uiStore.setState({ selectedResource: { type: 'agent', data: { name: refName, capabilities: [] } } });
          }
        } else if (refType === 'tool') {
          try {
            const list = await getTools();
            const found = (list || []).find((t) => t.name === refName);
            uiStore.setState({ selectedResource: { type: 'tool', data: found || { name: refName, description: '', risk_tier: 1 } } });
          } catch {
            uiStore.setState({ selectedResource: { type: 'tool', data: { name: refName, description: '', risk_tier: 1 } } });
          }
        } else if (refType === 'skill') {
          try {
            const list = await getSkills();
            const found = (list || []).find((s) => s.name === refName);
            uiStore.setState({ selectedResource: { type: 'skill', data: found || { name: refName, domain: 'general', description: '' } } });
          } catch {
            uiStore.setState({ selectedResource: { type: 'skill', data: { name: refName, domain: 'general', description: '' } } });
          }
        }
      };
    });
  }

  // --- 3. Execution Overview (Default view) ---
  function renderExecutionOverview(execState, graph) {
    const isRunning = execState.status === 'running';
    const isPaused = execState.status === 'paused';
    const canPause = isRunning;
    const canResume = isPaused;
    const canFork = Boolean(execState.executionId);

    // Evidence Source Warning Banner (Product Directive Requirement)
    const isStub = (execState.evidence_source === 'deterministic-stubs') || (execState.task?.evidence_source === 'deterministic-stubs');
    const evidenceBannerHtml = isStub ? `
      <div class="evidence-stub-badge">
        <span class="badge badge-warn">deterministic-stub evidence</span>
        <div style="margin-top: 4px; font-size: 11px; line-height: 1.4;">
          Research and BBP evidence comes from deterministic stub collectors. Never present stub output as real findings.
        </div>
      </div>
    ` : '';

    // Output & Artifacts Section (§53 + regression tests)
    let outputBodyHtml = '';
    if (execState.output) {
      outputBodyHtml = `
        <div class="output-header">
          <span class="meta-label">Output:</span>
          <button id="btn-copy-output" class="btn btn-sm" type="button">Copy</button>
        </div>
        <pre class="code-block output-code-block">${escapeHtml(execState.output)}</pre>
      `;
    } else if (execState.status === 'running' || execState.status === 'connecting') {
      outputBodyHtml = `<div class="text-muted font-mono" style="padding: 6px 0;">Running…</div>`;
    } else if (execState.status === 'idle' || execState.status === 'none' || !execState.executionId) {
      outputBodyHtml = `<div class="text-muted" style="padding: 6px 0;">Run a task or generate a proposal to see results here.</div>`;
    } else {
      outputBodyHtml = `<div class="text-muted" style="padding: 6px 0;">(No output produced)</div>`;
    }

    let artifactsHtml = '';
    const artifacts = execState.artifacts || [];
    if (Array.isArray(artifacts) && artifacts.length > 0) {
      const itemsHtml = artifacts.map((art, idx) => {
        const typeStr = escapeHtml(art.type || 'artifact');
        const uriStr = art.uri || '';
        // Show the FILE NAME, not the full path. The absolute path
        // (`data/runs/artifacts/<uuid>/...`) is 80+ characters of noise that
        // wrapped to 50px per row and buried the answer; the basename plus a
        // hover title carries the same information for a human.
        const baseName = uriStr ? uriStr.split('/').pop() : '';
        let uriRender = '';
        if (uriStr.startsWith('http://') || uriStr.startsWith('https://')) {
          uriRender = `<a href="${escapeHtml(uriStr)}" target="_blank" rel="noopener" class="artifact-link">${escapeHtml(uriStr)}</a>`;
        } else if (uriStr) {
          uriRender = `<span class="artifact-name" title="${escapeHtml(uriStr)}">${escapeHtml(baseName)}</span>
            <button type="button" class="btn btn-sm btn-view-artifact" data-artifact-index="${idx}">View</button>
            <button type="button" class="btn btn-sm btn-copy-artifact" data-artifact-index="${idx}">Copy</button>`;
        } else {
          uriRender = `<span class="text-muted">(no uri)</span> <button type="button" class="btn btn-sm btn-view-artifact" data-artifact-index="${idx}">View</button>`;
        }
        return `
          <li class="artifact-item">
            <span class="badge badge-kind">${typeStr}</span>
            ${uriRender}
          </li>
        `;
      }).join('');

      artifactsHtml = `
        <div class="artifacts-wrapper" style="margin-top: 10px;">
          <div class="meta-label" style="margin-bottom: 4px;">Files (${artifacts.length}):</div>
          <ul class="artifacts-list">${itemsHtml}</ul>
        </div>
      `;
    }

    const outputArtifactsHtml = `
      <div class="inspector-section output-section">
        <h3 class="section-title">Output &amp; Artifacts</h3>
        ${execState.error ? `<div class="error-box">${escapeHtml(execState.error)}</div>` : ''}
        ${outputBodyHtml}
        ${artifactsHtml}
      </div>
    `;

    // Runtime Controls Section (§53)
    const streamStatus = eventStream?.getStatus?.() || 'idle';
    const streamUp = streamStatus === 'open';
    const controlsHtml = `
      <div class="inspector-section controls-section">
        <h3 class="section-title">Runtime Controls</h3>
        <div class="btn-group">
          <button id="btn-pause" class="btn btn-warn" ${canPause ? '' : 'disabled'}>Pause</button>
          <button id="btn-resume" class="btn btn-ok" ${canResume ? '' : 'disabled'}>Resume</button>
          <button id="btn-fork" class="btn btn-accent" ${canFork && streamUp ? '' : 'disabled'}>Fork</button>
        </div>
        ${streamUp ? '' : `<div class="text-muted" style="font-size: 11px; margin-top: 8px;">Execution stream ${escapeHtml(streamStatus)} — control messages cannot be delivered until it reconnects.</div>`}
      </div>
    `;

    // Execution Overview Section
    const execHtml = `
      <div class="inspector-section">
        <h3 class="section-title">Execution Overview</h3>
        ${evidenceBannerHtml}
        <div class="meta-grid">
          <span class="meta-label">ID:</span>
          <span class="meta-value font-mono">${execState.executionId ? execState.executionId.slice(0, 8) + '...' : 'none'}</span>
          <span class="meta-label">Status:</span>
          <span class="meta-value"><span class="badge badge-${execState.status}">${execState.status}</span></span>
          <span class="meta-label">Resumes:</span>
          <span class="meta-value font-mono">${execState.resume_count ?? 0}</span>
          <span class="meta-label">Checkpoints:</span>
          <span class="meta-value font-mono">${(execState.checkpoints || []).length > 0 ? execState.checkpoints.length : 'not reported'}</span>
          <span class="meta-label">Tokens:</span>
          <span class="meta-value font-mono">${execState.tokenUsage ? execState.tokenUsage : 'not reported'}</span>
        </div>
        ${execState.error ? `<div class="error-box" style="margin-top: 8px;">${escapeHtml(execState.error)}</div>` : ''}
      </div>
    `;

    // Workflow definition metadata if loaded
    let workflowDefHtml = '';
    if (graph) {
      workflowDefHtml = `
        <div class="inspector-section">
          <h3 class="section-title">Workflow Definition</h3>
          <div class="meta-grid">
            <span class="meta-label">Name:</span>
            <span class="meta-value font-bold">${escapeHtml(graph.name || 'Untitled')}</span>
            <span class="meta-label">ID:</span>
            <span class="meta-value font-mono">${escapeHtml(graph.id || 'none')}</span>
            <span class="meta-label">Nodes:</span>
            <span class="meta-value font-mono">${(graph.nodes || []).length}</span>
            <span class="meta-label">Edges:</span>
            <span class="meta-value font-mono">${(graph.edges || []).length}</span>
          </div>
          ${graph.description ? `<p class="text-muted" style="margin-top: 6px;">${escapeHtml(graph.description)}</p>` : ''}
        </div>
      `;
    }

    // Decision Traces Section
    let tracesHtml = '';
    if (currentTraces && currentTraces.length > 0) {
      const traceCards = currentTraces
        .map(
          (t) => `
        <div class="trace-card">
          <div class="trace-header">
            <span class="trace-type">${escapeHtml(t.decision_type || 'decision')}</span>
            ${t.confidence != null ? `<span class="trace-conf">${Math.round(t.confidence * 100)}%</span>` : ''}
          </div>
          <div class="trace-chosen"><strong>Chosen:</strong> ${escapeHtml(t.chosen || '')}</div>
          <div class="trace-rationale">${escapeHtml(t.rationale || '')}</div>
        </div>
      `
        )
        .join('');

      tracesHtml = `
        <div class="inspector-section">
          <h3 class="section-title">Decision Traces (${currentTraces.length})</h3>
          <div class="traces-list">
            ${traceCards}
          </div>
        </div>
      `;
    } else {
      tracesHtml = `
        <div class="inspector-section">
          <h3 class="section-title">Decision Traces</h3>
          <div class="text-muted">No operational decisions recorded.</div>
        </div>
      `;
    }

    containerEl.innerHTML = outputArtifactsHtml + controlsHtml + execHtml + workflowDefHtml + tracesHtml;

    // Attach listeners
    // 1. Copy Output
    const btnCopyOutput = containerEl.querySelector('#btn-copy-output');
    if (btnCopyOutput) {
      btnCopyOutput.onclick = async () => {
        try {
          if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(execState.output || '');
          }
          btnCopyOutput.textContent = 'Copied';
          setTimeout(() => { if (btnCopyOutput) btnCopyOutput.textContent = 'Copy'; }, 1500);
        } catch (err) {
          console.error('Failed to copy output:', err);
        }
      };
    }

    // 2. Copy Artifact URI
    containerEl.querySelectorAll('.btn-copy-artifact').forEach((btn) => {
      btn.onclick = async () => {
        const idx = parseInt(btn.getAttribute('data-artifact-index'), 10);
        const art = artifacts[idx];
        if (!art || !art.uri) return;
        try {
          if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(art.uri);
          }
          btn.textContent = 'Copied';
          setTimeout(() => { if (btn) btn.textContent = 'Copy'; }, 1500);
        } catch (err) {
          console.error('Failed to copy artifact URI:', err);
        }
      };
    });

    // 3. View Artifact
    containerEl.querySelectorAll('.btn-view-artifact').forEach((btn) => {
      btn.onclick = () => {
        const idx = parseInt(btn.getAttribute('data-artifact-index'), 10);
        const art = artifacts[idx];
        if (!art) return;
        uiStore.setState({
          selectedResource: {
            type: 'artifact',
            data: {
              ...art,
              taskId: execState.executionId,
              index: idx,
            },
          },
        });
      };
    });

    // 4. Runtime Controls (Pause / Resume / Fork)
    const btnPause = containerEl.querySelector('#btn-pause');
    const btnResume = containerEl.querySelector('#btn-resume');
    const btnFork = containerEl.querySelector('#btn-fork');

    if (btnPause) {
      btnPause.onclick = async () => {
        btnPause.disabled = true;
        const eid = execState.executionId;
        if (!eid) return;
        try {
          await pauseExecution(eid);
        } catch (err) {
          showToast({ kind: 'error', title: 'Pause Failed', message: err.message || String(err) });
          return;
        }
        executionStore.setState({ status: 'paused' });
        const delivered = eventStream ? eventStream.pause() : false;
        if (delivered) {
          showToast({ kind: 'info', title: 'Execution Paused', message: `Execution ${eid.slice(0, 8)} paused.` });
        } else {
          showToast({
            kind: 'warn',
            title: 'Paused, stream offline',
            message: `Execution ${eid.slice(0, 8)} paused, but the live stream is ${eventStream?.getStatus?.() || 'idle'} — live events resume after reconnect.`,
            timeoutMs: 8000,
          });
        }
      };
    }

    if (btnResume) {
      btnResume.onclick = async () => {
        btnResume.disabled = true;
        const eid = execState.executionId;
        if (!eid) return;
        try {
          await resumeExecution(eid);
        } catch (err) {
          showToast({ kind: 'error', title: 'Resume Failed', message: err.message || String(err) });
          return;
        }
        executionStore.setState({ status: 'running' });
        const delivered = eventStream ? eventStream.resume() : false;
        if (delivered) {
          showToast({ kind: 'info', title: 'Execution Resumed', message: `Execution ${eid.slice(0, 8)} resumed.` });
        } else {
          showToast({
            kind: 'warn',
            title: 'Resumed, stream offline',
            message: `Execution ${eid.slice(0, 8)} resumed, but the live stream is ${eventStream?.getStatus?.() || 'idle'} — live events resume after reconnect.`,
            timeoutMs: 8000,
          });
        }
      };
    }

    if (btnFork && eventStream) {
      btnFork.onclick = () => {
        // Non-blocking label prompt (replaces window.prompt)
        showPromptToast({
          title: 'Fork execution',
          message: 'Enter a label for the forked execution:',
          placeholder: 'e.g. retry-with-better-queries',
          submitLabel: 'Fork',
          onSubmit: async (label, toastId) => {
            dismissToast(toastId);
            const delivered = eventStream.fork(label || '');
            if (delivered) {
              showToast({ kind: 'info', title: 'Fork Requested', message: `Fork "${label || 'untitled'}" requested.` });
            } else {
              showToast({
                kind: 'error',
                title: 'Fork Not Sent',
                message: `The live execution stream is ${eventStream.getStatus?.() || 'idle'} — the fork request was not delivered.`,
              });
            }
          },
        });
      };
    }
  }

  const unsubCanvas = canvasStore.subscribe(render);
  const unsubExec = executionStore.subscribe(render);
  const unsubUi = uiStore.subscribe(render);

  render();

  return {
    destroy: () => {
      unsubCanvas();
      unsubExec();
      unsubUi();
    },
    refreshTraces: (id) => fetchTraces(id),
  };
}
