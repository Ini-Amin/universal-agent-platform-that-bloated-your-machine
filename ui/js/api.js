// §44 REST client wrappers matching the frozen contract

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      ...options.headers,
    },
    ...options,
  });
  if (!res.ok) {
    const errorText = await res.text().catch(() => res.statusText);
    const err = new Error(`HTTP ${res.status}: ${errorText}`);
    err.status = res.status;
    err.body = errorText;
    throw err;
  }
  return res.json();
}

// Tasks / Runs
export async function listTasks() {
  return request('/tasks');
}

export async function getTask(taskId) {
  return request(`/tasks/${encodeURIComponent(taskId)}`);
}

export async function createTask(input, userId = null, workspaceId = null) {
  const body = { input };
  if (userId) body.user_id = userId;
  if (workspaceId) body.workspace_id = workspaceId;
  return request('/tasks', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// Workspaces
export async function listWorkspaces() {
  return request('/api/workspaces');
}

export async function createWorkspace(name, description = '') {
  return request('/api/workspaces', {
    method: 'POST',
    body: JSON.stringify({ name, description }),
  });
}

// Proposals
export async function generateProposal(input, workspaceId = null) {
  const body = { input };
  if (workspaceId) body.workspace_id = workspaceId;
  return request('/api/proposals', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// Workflows
export async function getWorkflows() {
  return request('/api/workflows');
}

// Executions (Graph, Traces, Pause, Resume)
export async function getExecutionGraph(executionId) {
  return request(`/api/executions/${encodeURIComponent(executionId)}/graph`);
}

export async function getExecutionTraces(executionId) {
  return request(`/api/executions/${encodeURIComponent(executionId)}/traces`);
}

export async function pauseExecution(executionId) {
  return request(`/api/executions/${encodeURIComponent(executionId)}/pause`, {
    method: 'POST',
  });
}

export async function resumeExecution(executionId) {
  return request(`/api/executions/${encodeURIComponent(executionId)}/resume`, {
    method: 'POST',
  });
}

// Resources (Agents, Tools, Skills, Models, Policies, MCP)
export async function getAgents() {
  return request('/api/resources/agents');
}

export async function getTools() {
  return request('/api/resources/tools');
}

export async function getSkills() {
  return request('/api/resources/skills');
}

export async function getModels() {
  return request('/api/resources/models');
}

export async function getPolicies() {
  return request('/api/resources/policies');
}

export async function getMcp() {
  return request('/api/resources/mcp');
}

// Knowledge & Provenance
export async function getKnowledge() {
  return request('/api/knowledge');
}

export async function getKnowledgeProvenance(knowledgeId) {
  return request(`/api/knowledge/${encodeURIComponent(knowledgeId)}/provenance`);
}

// Artifacts
export async function getTaskArtifacts(taskId) {
  return request(`/api/tasks/${encodeURIComponent(taskId)}/artifacts`);
}

export async function getArtifactContent(taskId, index) {
  return request(`/api/tasks/${encodeURIComponent(taskId)}/artifacts/${index}/content`);
}

// Legacy Library & Approvals
export async function listLibrary(kind = null, status = null) {
  const params = new URLSearchParams();
  if (kind) params.set('kind', kind);
  if (status) params.set('status', status);
  const q = params.toString() ? `?${params.toString()}` : '';
  return request(`/api/library${q}`);
}

export async function decideApproval(approvalId, approved, decidedBy = 'user') {
  return request(`/approvals/${encodeURIComponent(approvalId)}/decide`, {
    method: 'POST',
    body: JSON.stringify({ approved, decided_by: decidedBy }),
  });
}
