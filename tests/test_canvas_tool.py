"""The agent registry exposes the canvas API as callable tools."""

from pathlib import Path

import pytest

from uap.contracts import ApprovalRequest, ApprovalState
from uap.tools import ToolRegistry, register_local_tools


async def test_canvas_registry_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from uap.tools import canvas as canvas_module

    calls = []

    def fake_request(method, endpoint, payload=None):
        calls.append((method, endpoint, payload))
        return {"delivered": 2} if method == "POST" else {"clients": [{"views": []}]}

    monkeypatch.setattr(canvas_module, "_request", fake_request)
    registry = ToolRegistry()
    register_local_tools(registry, tmp_path)
    assert registry.get("canvas_commands")[0].risk_tier == 3
    assert registry.get("canvas_state")[0].risk_tier == 0
    state = await registry.call("canvas_state", {})
    assert state.ok and state.result == {"clients": [{"views": []}]}
    commands = [{"type": "open_file", "path": "README.md"}]
    approval = ApprovalRequest(
        task_id="agent", action="control canvas", state=ApprovalState.APPROVED,
        decided_by="operator",
    )
    sent = await registry.call("canvas_commands", {"commands": commands}, approval=approval)
    assert sent.ok and sent.result == {"delivered": 2}
    assert calls == [
        ("GET", "/api/canvas/state", None),
        ("POST", "/api/canvas/commands", {"commands": commands}),
    ]
