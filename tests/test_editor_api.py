"""Tests for the editor backend: file browser, save, and "Open in Zed".

The feature: the canvas code editor stops being a textarea toy. It browses the
real workspace, opens a real file, saves it back to disk, and can hand the file
to the native Zed editor running on the server's machine.

Security is the point of most of these tests. ``POST /api/editor/open`` spawns a
process and ``POST /api/workspace/file`` writes to disk, so both must be:

* **contained** -- a ``..`` traversal, an absolute path outside the root, and a
  symlink pointing out are all refused (same discipline as the artifact store);
* **auth-gated** -- viewers are refused;
* **argv-safe** -- the path is one argv element, never a shell string;
* **non-blocking** -- launching the editor returns immediately, detached.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from uap.server import create_app
from uap.server import app as app_mod

TOKEN = "editor-test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "README.md").write_text("# Project\n", encoding="utf-8")
    sub = root / "pkg"
    sub.mkdir()
    (sub / "mod.py").write_text("x = 1\n", encoding="utf-8")
    return root


@pytest.fixture()
def app(workspace: Path, tmp_path: Path) -> FastAPI:
    return create_app(
        runs_dir=tmp_path / "runs",
        run_inline=True,
        heartbeat_interval=0.05,
        workspace_dir=workspace,
    )


@pytest.fixture()
def client(app: FastAPI):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def with_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UAP_API_TOKEN", TOKEN)


@pytest.fixture()
def no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UAP_API_TOKEN", raising=False)


# A tiny executable used in place of zed so tests never open a real GUI.
def _fake_editor(tmp_path: Path, name: str = "fake-editor") -> Path:
    script = tmp_path / name
    script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


# --------------------------------------------------------------------------- #
# 1. File browser listing
# --------------------------------------------------------------------------- #

def test_list_workspace_root_marks_directories(client: TestClient) -> None:
    res = client.get("/api/workspace/files", params={"path": ""})
    assert res.status_code == 200, res.text
    data = res.json()
    names = {e["name"]: e for e in data["entries"]}
    assert set(names) == {"hello.py", "README.md", "pkg"}
    assert names["pkg"]["is_dir"] is True
    assert names["hello.py"]["is_dir"] is False
    # directories sort first
    assert data["entries"][0]["name"] == "pkg"
    assert data["path"] == ""


def test_list_subdirectory(client: TestClient) -> None:
    res = client.get("/api/workspace/files", params={"path": "pkg"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert [e["name"] for e in data["entries"]] == ["mod.py"]
    assert data["path"] == "pkg"
    assert data["entries"][0]["path"] == "pkg/mod.py"


def test_list_a_file_is_400_not_a_directory(client: TestClient) -> None:
    res = client.get("/api/workspace/files", params={"path": "hello.py"})
    assert res.status_code == 400
    assert "not a directory" in res.json()["detail"]


# --------------------------------------------------------------------------- #
# 2. Containment refusals -- the file browser must not escape
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "escape",
    [
        "..",
        "../",
        "../../etc",
        "../outside.txt",
        "pkg/../../etc/passwd",
        "pkg/../../outside.txt",
    ],
)
def test_list_refuses_parent_traversal(client: TestClient, workspace: Path, escape: str) -> None:
    (workspace.parent / "outside.txt").write_text("SECRET", encoding="utf-8")
    res = client.get("/api/workspace/files", params={"path": escape})
    assert res.status_code == 400, res.text
    assert "escapes the workspace root" in res.json()["detail"]


def test_list_refuses_absolute_path_outside_root(client: TestClient, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    res = client.get("/api/workspace/files", params={"path": str(outside)})
    assert res.status_code == 400
    assert "escapes the workspace root" in res.json()["detail"]


def test_list_refuses_symlink_pointing_out(client: TestClient, workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "leak.txt").write_text("TOP SECRET", encoding="utf-8")
    os.symlink(outside, workspace / "escape-link")

    # Navigating *through* the symlink is refused outright...
    res = client.get("/api/workspace/files", params={"path": "escape-link"})
    assert res.status_code == 400, res.text
    assert "escapes the workspace root" in res.json()["detail"]

    # ...and a root listing marks the link as not contained rather than letting
    # the browser walk into it.
    root_res = client.get("/api/workspace/files", params={"path": ""})
    link = next(e for e in root_res.json()["entries"] if e["name"] == "escape-link")
    assert link["contained"] is False


def test_list_refuses_nul_byte(client: TestClient) -> None:
    res = client.get("/api/workspace/files", params={"path": "hello\x00.py"})
    assert res.status_code == 400
    assert "NUL" in res.json()["detail"]


def test_list_missing_path_is_404(client: TestClient) -> None:
    res = client.get("/api/workspace/files", params={"path": "nope"})
    assert res.status_code == 404


# --------------------------------------------------------------------------- #
# 3. Read + write round-trip (the "edit the project" path)
# --------------------------------------------------------------------------- #

def test_read_file_returns_content(client: TestClient) -> None:
    res = client.get("/api/workspace/file", params={"path": "hello.py"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["content"] == "print('hi')\n"
    assert data["path"] == "hello.py"


def test_write_file_persists_to_disk(client: TestClient, workspace: Path) -> None:
    res = client.post(
        "/api/workspace/file",
        json={"path": "hello.py", "content": "print('edited')\n"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["bytes"] == len("print('edited')\n")
    # The real file on disk changed -- not a download.
    assert (workspace / "hello.py").read_text(encoding="utf-8") == "print('edited')\n"


def test_write_creates_new_file_and_parent_dirs(client: TestClient, workspace: Path) -> None:
    res = client.post(
        "/api/workspace/file",
        json={"path": "newdir/new.py", "content": "y = 2\n"},
    )
    assert res.status_code == 200, res.text
    assert (workspace / "newdir" / "new.py").read_text(encoding="utf-8") == "y = 2\n"


def test_read_refuses_escape(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "outside.txt").write_text("SECRET", encoding="utf-8")
    res = client.get("/api/workspace/file", params={"path": "../outside.txt"})
    assert res.status_code == 400
    assert "escapes the workspace root" in res.json()["detail"]


def test_write_refuses_escape_and_does_not_touch_outside(client: TestClient, workspace: Path) -> None:
    outside = workspace.parent / "outside.txt"
    outside.write_text("UNCHANGED", encoding="utf-8")
    res = client.post(
        "/api/workspace/file",
        json={"path": "../outside.txt", "content": "PWNED"},
    )
    assert res.status_code == 400, res.text
    assert outside.read_text(encoding="utf-8") == "UNCHANGED"


def test_write_refuses_symlink_out(client: TestClient, workspace: Path, tmp_path: Path) -> None:
    outside_dir = tmp_path / "outdir"
    outside_dir.mkdir()
    target = outside_dir / "victim.txt"
    target.write_text("ORIGINAL", encoding="utf-8")
    os.symlink(target, workspace / "link.txt")

    res = client.post(
        "/api/workspace/file",
        json={"path": "link.txt", "content": "PWNED"},
    )
    assert res.status_code == 400, res.text
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


def test_read_rejects_binary(client: TestClient, workspace: Path) -> None:
    (workspace / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    res = client.get("/api/workspace/file", params={"path": "blob.bin"})
    assert res.status_code == 415


# --------------------------------------------------------------------------- #
# 4. Editor status
# --------------------------------------------------------------------------- #

def test_editor_status_reports_available_binary(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = _fake_editor(tmp_path)
    monkeypatch.setenv("UAP_EDITOR_BIN", str(binary))
    res = client.get("/api/editor/status")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["available"] is True
    assert data["binary"] == str(binary)
    assert data["reason"]


def test_editor_status_reports_missing_binary(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UAP_EDITOR_BIN", "definitely-not-installed-xyz")
    res = client.get("/api/editor/status")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["available"] is False
    assert data["binary"] == "definitely-not-installed-xyz"
    assert "not found" in data["reason"]


def test_editor_status_default_binary_is_zed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UAP_EDITOR_BIN", raising=False)
    assert app_mod.editor_status()["binary"] == "zed"


# --------------------------------------------------------------------------- #
# 5. Open in Zed -- launch, argv safety, containment, detachment
# --------------------------------------------------------------------------- #

def test_open_launches_editor_with_path_as_argv(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exact request/response, and proof the path is one argv element."""
    # A fake editor that records its argv to a file, then sleeps so we can
    # observe that the request returned before it exited (detachment).
    record = tmp_path / "argv.txt"
    script = tmp_path / "recorder"
    script.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\nsleep 30\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("UAP_EDITOR_BIN", str(script))

    res = client.post("/api/editor/open", json={"path": "hello.py"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["launched"] is True
    assert data["binary"] == str(script)
    assert isinstance(data["pid"], int) and data["pid"] > 0
    assert data["path"] == "hello.py"

    # The path reached the child as exactly one argv element.
    import time
    for _ in range(50):
        if record.exists():
            break
        time.sleep(0.05)
    lines = record.read_text(encoding="utf-8").splitlines()
    assert lines == [str(workspace / "hello.py")]


def test_open_path_with_shell_metacharacters_is_inert(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A filename containing ``;``/``$(...)`` must not run a command."""
    evil = "evil; touch pwned"
    (workspace / evil).write_text("x", encoding="utf-8")

    record = tmp_path / "argv.txt"
    script = tmp_path / "recorder2"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("UAP_EDITOR_BIN", str(script))

    res = client.post("/api/editor/open", json={"path": evil})
    assert res.status_code == 200, res.text
    assert not (workspace / "pwned").exists()
    assert not Path("pwned").exists()

    import time
    for _ in range(50):
        if record.exists():
            break
        time.sleep(0.05)
    lines = record.read_text(encoding="utf-8").splitlines()
    assert lines == [str(workspace / evil)]


def test_open_returns_immediately_detached(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The route must not block on the editor's lifetime."""
    import time

    script = tmp_path / "slow-editor"
    script.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("UAP_EDITOR_BIN", str(script))

    start = time.monotonic()
    res = client.post("/api/editor/open", json={"path": "hello.py"})
    elapsed = time.monotonic() - start
    assert res.status_code == 200, res.text
    # A 60s editor must not make the request take 60s.
    assert elapsed < 5.0, f"open blocked for {elapsed:.1f}s"


def test_open_refuses_traversal(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UAP_EDITOR_BIN", str(_fake_editor(tmp_path)))
    (tmp_path / "outside.txt").write_text("SECRET", encoding="utf-8")
    res = client.post("/api/editor/open", json={"path": "../outside.txt"})
    assert res.status_code == 400, res.text
    assert "escapes the workspace root" in res.json()["detail"]


def test_open_refuses_absolute_path_outside_root(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UAP_EDITOR_BIN", str(_fake_editor(tmp_path)))
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET", encoding="utf-8")
    res = client.post("/api/editor/open", json={"path": str(outside)})
    assert res.status_code == 400, res.text
    assert "escapes the workspace root" in res.json()["detail"]


def test_open_refuses_symlink_pointing_out(
    client: TestClient, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UAP_EDITOR_BIN", str(_fake_editor(tmp_path)))
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET", encoding="utf-8")
    os.symlink(outside, workspace / "link.txt")

    res = client.post("/api/editor/open", json={"path": "link.txt"})
    assert res.status_code == 400, res.text
    assert "escapes the workspace root" in res.json()["detail"]


def test_open_reaps_the_child_without_blocking(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A launched editor that exits must be reaped, not left as a zombie.

    ``start_new_session`` detaches the child; without a waiter the kernel keeps
    a zombie in the server's process table. The route spawns a tiny daemon
    thread to ``wait()`` on it. This test launches a short-lived editor and
    confirms the pid is gone (and not a zombie) shortly after it exits.
    """
    import os
    import time

    script = tmp_path / "quick-editor"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("UAP_EDITOR_BIN", str(script))

    res = client.post("/api/editor/open", json={"path": "hello.py"})
    assert res.status_code == 200, res.text
    pid = res.json()["pid"]

    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return  # fully reaped
        # If it still exists, it must not be a zombie we own.
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                state = fh.read().split(") ", 1)[1].split(" ", 1)[0]
        except FileNotFoundError:
            return
        if state == "Z":
            # give the reaper thread a moment
            time.sleep(0.05)
            continue
        time.sleep(0.05)
    # If we get here the process is gone or was reaped.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
        state = fh.read().split(") ", 1)[1].split(" ", 1)[0]
    assert state != "Z", f"launched editor left a zombie (pid {pid})"


def test_open_missing_file_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("UAP_EDITOR_BIN", str(_fake_editor(tmp_path)))
    res = client.post("/api/editor/open", json={"path": "does-not-exist.py"})
    assert res.status_code == 404


def test_open_when_editor_missing_is_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UAP_EDITOR_BIN", "definitely-not-installed-xyz")
    res = client.post("/api/editor/open", json={"path": "hello.py"})
    assert res.status_code == 409
    assert "not found" in res.json()["detail"]


# --------------------------------------------------------------------------- #
# 6. Auth gate -- viewers are refused
# --------------------------------------------------------------------------- #

def test_open_requires_auth_when_enabled(
    app: FastAPI, with_auth: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("UAP_EDITOR_BIN", str(_fake_editor(tmp_path)))
    with TestClient(app) as client:
        assert client.post("/api/editor/open", json={"path": "hello.py"}).status_code == 401
        assert client.get("/api/workspace/files").status_code == 401


def test_viewer_cannot_open_in_editor(
    app: FastAPI, no_auth: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A viewer role is refused (403) even with a valid credential."""
    from uap.server.auth import Identity

    monkeypatch.setenv("UAP_EDITOR_BIN", str(_fake_editor(tmp_path)))
    viewer = Identity(user_id="v", name="Viewer", role="viewer", is_bootstrap=False)
    monkeypatch.setattr(app_mod, "get_current_identity", lambda _conn: viewer)

    with TestClient(app) as client:
        res = client.post("/api/editor/open", json={"path": "hello.py"})
        assert res.status_code == 403, res.text
        assert "viewers" in res.json()["detail"]

        # The viewer may still READ the browser (read-only role).
        assert client.get("/api/workspace/files").status_code == 200

        # ...but not write.
        write = client.post(
            "/api/workspace/file",
            json={"path": "hello.py", "content": "nope"},
        )
        assert write.status_code == 403, write.text


# --------------------------------------------------------------------------- #
# 7. Container parameter plumbing
# --------------------------------------------------------------------------- #

def test_workspace_dir_param_is_resolved(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    app = create_app(runs_dir=tmp_path / "runs", run_inline=True, workspace_dir=root)
    assert app.state.workspace_dir == root.resolve()


def test_workspace_dir_falls_back_to_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "envws"
    root.mkdir()
    monkeypatch.setenv("UAP_WORKSPACE_DIR", str(root))
    app = create_app(runs_dir=tmp_path / "runs", run_inline=True)
    assert app.state.workspace_dir == root.resolve()


# --------------------------------------------------------------------------- #
# 8. Pure-function containment unit tests
# --------------------------------------------------------------------------- #

def test_confine_allows_inside_and_refuses_outside(tmp_path: Path) -> None:
    root = (tmp_path / "root").resolve()
    root.mkdir()
    (root / "a").mkdir()

    assert app_mod.confine_workspace_path("a", root) == root / "a"
    assert app_mod.confine_workspace_path("", root) == root
    assert app_mod.confine_workspace_path(".", root) == root

    for bad in ("..", "../x", "/etc", str(tmp_path)):
        with pytest.raises(PermissionError):
            app_mod.confine_workspace_path(bad, root)


# --------------------------------------------------------------------------- #
# 9. Multi-root workspace selection
# --------------------------------------------------------------------------- #

def test_default_roots_and_home_browsing(client: TestClient, app: FastAPI, workspace: Path) -> None:
    assert app.state.workspace_roots == {
        "workspace": workspace.resolve(), "home": Path.home().resolve()
    }
    roots = client.get("/api/workspace/roots")
    assert roots.status_code == 200
    assert roots.json() == {"roots": [
        {"id": "workspace", "label": "workspace", "path": str(workspace.resolve())},
        {"id": "home", "label": "home", "path": str(Path.home().resolve())},
    ]}
    home_listing = client.get("/api/workspace/files", params={"root": "home"})
    assert home_listing.status_code == 200
    assert home_listing.json()["root"] == str(Path.home().resolve())


def test_custom_roots_replace_defaults_and_preserve_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workspace: Path
) -> None:
    other = tmp_path / "other-project"
    other.mkdir()
    (other / "main.py").write_text("print('other')\n", encoding="utf-8")
    monkeypatch.setenv("UAP_WORKSPACE_ROOTS", f"workspace:{workspace},other:{other}")
    app = create_app(runs_dir=tmp_path / "runs", run_inline=True, workspace_dir=workspace)
    assert list(app.state.workspace_roots) == ["workspace", "other"]
    with TestClient(app) as client:
        roots = client.get("/api/workspace/roots").json()["roots"]
        assert [root["id"] for root in roots] == ["workspace", "other"]
        listing = client.get("/api/workspace/files", params={"root": "other"})
        assert [entry["name"] for entry in listing.json()["entries"]] == ["main.py"]
        content = client.get("/api/workspace/file", params={"root": "other", "path": "main.py"})
        assert content.json()["content"] == "print('other')\n"
        saved = client.post("/api/workspace/file", json={
            "root": "other", "path": "main.py", "content": "print('saved')\n"
        })
        assert saved.status_code == 200
        assert (other / "main.py").read_text() == "print('saved')\n"
        assert (workspace / "hello.py").read_text() == "print('hi')\n"


@pytest.mark.parametrize("entry", ["other:/missing", "other:", "not-an-entry"])
def test_invalid_configured_root_fails_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workspace: Path, entry: str
) -> None:
    monkeypatch.setenv("UAP_WORKSPACE_ROOTS", entry)
    with pytest.raises(ValueError, match="workspace root|UAP_WORKSPACE_ROOTS"):
        create_app(runs_dir=tmp_path / "runs", run_inline=True, workspace_dir=workspace)


def test_unknown_root_is_400_for_all_file_routes(client: TestClient) -> None:
    requests = [
        client.get("/api/workspace/files", params={"root": "missing"}),
        client.get("/api/workspace/file", params={"root": "missing", "path": "hello.py"}),
        client.post("/api/workspace/file", json={"root": "missing", "path": "hello.py", "content": "x"}),
        client.post("/api/editor/open", json={"root": "missing", "path": "hello.py"}),
    ]
    for response in requests:
        assert response.status_code == 400, response.text
        assert "valid ids: workspace, home" in response.json()["detail"]


def test_selected_root_refuses_cross_root_traversal_and_symlinks(
    client: TestClient, app: FastAPI, workspace: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    secret = other / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    app.state.workspace_roots["other"] = other.resolve()
    os.symlink(secret, workspace / "cross-root-link")
    os.symlink("/etc/hostname", other / "etc-link")
    cases = [("workspace", "../other/secret.txt"), ("workspace", "cross-root-link"),
             ("other", "../workspace/hello.py"), ("other", "etc-link")]
    for root, path in cases:
        responses = [
            client.get("/api/workspace/file", params={"root": root, "path": path}),
            client.post("/api/workspace/file", json={"root": root, "path": path, "content": "PWNED"}),
            client.post("/api/editor/open", json={"root": root, "path": path}),
        ]
        for response in responses:
            assert response.status_code == 400, (root, path, response.text)
            assert "escapes the workspace root" in response.json()["detail"]
    assert secret.read_text() == "SECRET"
    listing = client.get("/api/workspace/files", params={"root": "other"}).json()
    assert next(item for item in listing["entries"] if item["name"] == "etc-link")["contained"] is False


def test_viewer_can_read_other_root_but_cannot_write(
    app: FastAPI, no_auth: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from uap.server.auth import Identity

    other = tmp_path / "other"
    other.mkdir()
    (other / "example.txt").write_text("original")
    app.state.workspace_roots["other"] = other.resolve()
    monkeypatch.setattr(app_mod, "get_current_identity", lambda _conn: Identity(
        user_id="v", name="Viewer", role="viewer", is_bootstrap=False
    ))
    with TestClient(app) as client:
        assert client.get("/api/workspace/files", params={"root": "other"}).status_code == 200
        assert client.get("/api/workspace/file", params={"root": "other", "path": "example.txt"}).status_code == 200
        result = client.post("/api/workspace/file", json={
            "root": "other", "path": "example.txt", "content": "changed"
        })
        assert result.status_code == 403
    assert (other / "example.txt").read_text() == "original"


def test_editor_uses_selected_roots_absolute_path(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "file.py").write_text("x = 42\n")
    app.state.workspace_roots["other"] = other.resolve()
    launched = []

    class FakeProcess:
        pid = 123

        def wait(self):
            return 0

    def fake_popen(argv, **kwargs):
        launched.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setenv("UAP_EDITOR_BIN", sys.executable)
    monkeypatch.setattr(app_mod.subprocess, "Popen", fake_popen)
    result = client.post("/api/editor/open", json={"root": "other", "path": "file.py"})
    assert result.status_code == 200, result.text
    assert launched[0][0] == [sys.executable, str(other / "file.py")]
    assert launched[0][1]["cwd"] == str(other)


# --------------------------------------------------------------------------- #
# 10. Dynamic root registration, deletion, and persistence
# --------------------------------------------------------------------------- #

def test_add_workspace_root_success_and_delete(
    client: TestClient, tmp_path: Path
) -> None:
    new_dir = tmp_path / "my_project"
    new_dir.mkdir()
    (new_dir / "index.ts").write_text("console.log('hi');\n", encoding="utf-8")

    res = client.post(
        "/api/workspace/roots",
        json={"path": str(new_dir), "label": "My Project"},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["label"] == "My Project"
    assert data["path"] == str(new_dir.resolve())
    root_id = data["id"]
    assert root_id == "my_project"

    roots = client.get("/api/workspace/roots").json()["roots"]
    assert any(r["id"] == root_id and r["label"] == "My Project" for r in roots)

    # Can list and read in the newly registered root
    listing = client.get("/api/workspace/files", params={"root": root_id})
    assert listing.status_code == 200
    assert [e["name"] for e in listing.json()["entries"]] == ["index.ts"]

    content = client.get(
        "/api/workspace/file", params={"root": root_id, "path": "index.ts"}
    )
    assert content.status_code == 200
    assert content.json()["content"] == "console.log('hi');\n"

    # DELETE removes the added root
    delete_res = client.delete(f"/api/workspace/roots/{root_id}")
    assert delete_res.status_code == 200
    assert delete_res.json() == {"deleted": True, "id": root_id}

    roots_after = client.get("/api/workspace/roots").json()["roots"]
    assert not any(r["id"] == root_id for r in roots_after)


def test_add_workspace_root_dedupes_by_resolved_path(
    client: TestClient, tmp_path: Path
) -> None:
    p = tmp_path / "proj"
    p.mkdir()
    res1 = client.post("/api/workspace/roots", json={"path": str(p), "label": "P1"})
    assert res1.status_code == 200
    id1 = res1.json()["id"]

    # Re-adding the same directory returns the existing registration
    res2 = client.post("/api/workspace/roots", json={"path": str(p), "label": "P2"})
    assert res2.status_code == 200
    assert res2.json()["id"] == id1
    assert res2.json()["label"] == "P1"


def test_add_workspace_root_rejections(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from uap.server.auth import Identity

    # 1. Relative path rejected with 400
    rel = client.post("/api/workspace/roots", json={"path": "some/relative/path"})
    assert rel.status_code == 400
    assert "path must be absolute" in rel.json()["detail"]

    # 2. File path rejected with 400
    f = tmp_path / "not_a_dir.txt"
    f.write_text("hello")
    file_res = client.post("/api/workspace/roots", json={"path": str(f)})
    assert file_res.status_code == 400
    assert "not a directory" in file_res.json()["detail"]

    # 3. Missing path rejected with 400
    missing_res = client.post(
        "/api/workspace/roots", json={"path": str(tmp_path / "does_not_exist")}
    )
    assert missing_res.status_code == 400
    assert "does not exist" in missing_res.json()["detail"]

    # 4. Viewer role rejected with 403
    valid_dir = tmp_path / "valid"
    valid_dir.mkdir()
    monkeypatch.setattr(
        app_mod,
        "get_current_identity",
        lambda _conn: Identity(
            user_id="v", name="Viewer", role="viewer", is_bootstrap=False
        ),
    )
    viewer_res = client.post(
        "/api/workspace/roots", json={"path": str(valid_dir)}
    )
    assert viewer_res.status_code == 403
    assert "viewers cannot modify workspace roots" in viewer_res.json()["detail"]


def test_delete_workspace_root_rejections(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uap.server.auth import Identity

    # Cannot delete built-ins
    for builtin in ("workspace", "home"):
        res = client.delete(f"/api/workspace/roots/{builtin}")
        assert res.status_code == 400
        assert f"cannot delete built-in workspace root '{builtin}'" in res.json()["detail"]

    # Cannot delete unknown root
    res_unknown = client.delete("/api/workspace/roots/nonexistent_root_id")
    assert res_unknown.status_code == 404
    assert "unknown workspace root" in res_unknown.json()["detail"]

    # Viewer role rejected with 403
    monkeypatch.setattr(
        app_mod,
        "get_current_identity",
        lambda _conn: Identity(
            user_id="v", name="Viewer", role="viewer", is_bootstrap=False
        ),
    )
    viewer_del = client.delete("/api/workspace/roots/some_id")
    assert viewer_del.status_code == 403


def test_added_root_persistence_across_app_restart(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    custom = tmp_path / "external_project"
    custom.mkdir()
    (custom / "app.py").write_text("print('external')\n")

    # App 1: add the root
    app1 = create_app(runs_dir=runs, run_inline=True, workspace_dir=ws)
    with TestClient(app1) as c1:
        res = c1.post(
            "/api/workspace/roots", json={"path": str(custom), "label": "External"}
        )
        assert res.status_code == 200
        root_id = res.json()["id"]

    # App 2: simulate server restart with same runs_dir
    app2 = create_app(runs_dir=runs, run_inline=True, workspace_dir=ws)
    with TestClient(app2) as c2:
        roots = c2.get("/api/workspace/roots").json()["roots"]
        matching = [r for r in roots if r["id"] == root_id]
        assert len(matching) == 1
        assert matching[0]["label"] == "External"
        assert matching[0]["path"] == str(custom.resolve())

        # Files are readable in app2
        read_res = c2.get(
            "/api/workspace/file", params={"root": root_id, "path": "app.py"}
        )
        assert read_res.status_code == 200
        assert read_res.json()["content"] == "print('external')\n"


def test_added_root_confinement_and_symlink_escape(
    client: TestClient, tmp_path: Path
) -> None:
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    secret_file = outside / "secret.env"
    secret_file.write_text("SUPER_SECRET=123\n")

    added = tmp_path / "added_root"
    added.mkdir()
    safe_file = added / "safe.txt"
    safe_file.write_text("safe content\n")

    # Symlink pointing outside the root
    symlink_target = added / "leak_link"
    os.symlink(secret_file, symlink_target)

    res = client.post("/api/workspace/roots", json={"path": str(added), "label": "Added"})
    assert res.status_code == 200
    rid = res.json()["id"]

    # Listing marks the symlink as not contained
    listing = client.get("/api/workspace/files", params={"root": rid}).json()
    link_entry = next(e for e in listing["entries"] if e["name"] == "leak_link")
    assert link_entry["contained"] is False

    # Safe file is readable
    safe_res = client.get(
        "/api/workspace/file", params={"root": rid, "path": "safe.txt"}
    )
    assert safe_res.status_code == 200
    assert safe_res.json()["content"] == "safe content\n"

    # Symlink escape is refused with 400
    escaped = client.get(
        "/api/workspace/file", params={"root": rid, "path": "leak_link"}
    )
    assert escaped.status_code == 400
    assert "escapes the workspace root" in escaped.json()["detail"]

    # Path traversal outside root is refused with 400
    traversal = client.get(
        "/api/workspace/file", params={"root": rid, "path": "../outside_dir/secret.env"}
    )
    assert traversal.status_code == 400
    assert "escapes the workspace root" in traversal.json()["detail"]

    # Writing to symlink or traversal is refused
    write_esc = client.post(
        "/api/workspace/file",
        json={"root": rid, "path": "leak_link", "content": "overwrite"},
    )
    assert write_esc.status_code == 400
    assert "escapes the workspace root" in write_esc.json()["detail"]
    assert secret_file.read_text() == "SUPER_SECRET=123\n"
