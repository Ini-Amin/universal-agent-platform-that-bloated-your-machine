"""A safe, dependency-free Git wrapper (Master sections 2.4 and 49).

Git is a first-class version control mechanism in UAP (section 49): workflow,
agent, tool and skill *definitions* must be Git-friendly, and AI-generated
changes must be visible in history. This module is the one place that talks to
the ``git`` executable.

Design rules (section 73 - explicit interfaces, clear error types):

* **No GitPython.** Every call is a plain ``subprocess.run`` with an argument
  list, ``shell=False`` implied (``shell`` is never passed as ``True``), a hard
  ``timeout``, and ``check=False`` so the wrapper - not the OS - decides what a
  non-zero exit means.
* **Typed errors.** A failed command raises :class:`GitError`, which carries the
  captured ``stderr`` and the offending ``command``.
* **Containment.** Any path handed to ``show`` / ``diff`` / ``file_history`` is
  resolved and rejected if it escapes the repository (no ``..`` escapes).
* **No global state.** One :class:`GitRepo` instance is a thin handle on a
  working tree; nothing is cached between calls.

``commit_all`` returns ``None`` (not a synthetic SHA) when there is nothing to
commit, so callers can distinguish "recorded a new revision" from "no-op".
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Sequence

__all__ = ["GitError", "GitRepo"]

# Field separator for machine-readable log output. ASCII unit separator cannot
# appear in a commit subject, so splitting on it is safe.
_FIELD = "\x1f"
_LOG_FORMAT = "%H%x1f%s%x1f%an%x1f%aI"
_TIMEOUT_S = 30
_LOCAL_EMAIL = "uap@local"
_LOCAL_NAME = "UAP"


class GitError(Exception):
    """A git command failed, or a path/argument violated a safety rule.

    Carries the captured ``stderr``, the process ``returncode`` (when there was
    one) and the ``command`` that was run, so callers can surface a precise
    diagnostic instead of swallowing the failure (section 73).
    """

    def __init__(
        self,
        message: str,
        *,
        stderr: str = "",
        returncode: int | None = None,
        command: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode
        self.command = tuple(command)


class GitRepo:
    """A minimal, safe handle on one git working tree."""

    def __init__(self, path: Path | str, *, git_bin: str = "git") -> None:
        self.path = Path(path)
        self.git_bin = git_bin

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    @classmethod
    def init(cls, path: Path | str, *, initial_branch: str = "main") -> "GitRepo":
        """Create (or re-initialise) a repository at ``path``.

        Ensures a *local* ``user.email`` / ``user.name`` so commits never depend
        on the machine's global git identity (deterministic, offline).
        """
        repo = cls(path)
        repo.path.mkdir(parents=True, exist_ok=True)
        repo._run("init", "-b", initial_branch)
        repo._run("config", "--local", "user.email", _LOCAL_EMAIL)
        repo._run("config", "--local", "user.name", _LOCAL_NAME)
        return repo

    # ------------------------------------------------------------------ #
    # Process plumbing - the single choke point for every git invocation
    # ------------------------------------------------------------------ #

    def _run(
        self, *args: str, ok_codes: tuple[int, ...] = (0,), check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        command = [self.git_bin, *args]
        try:
            proc = subprocess.run(
                command,
                cwd=self.path,
                capture_output=True,
                timeout=_TIMEOUT_S,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:  # cwd missing, or git not installed
            raise GitError(
                f"cannot run {self.git_bin!r} in {self.path}: {exc}", command=command
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(
                f"git timed out after {_TIMEOUT_S}s: {' '.join(command)}",
                command=command,
            ) from exc

        if check and proc.returncode not in ok_codes:
            raise GitError(
                f"git {' '.join(args)} failed with exit {proc.returncode}: "
                f"{proc.stderr.strip()}",
                stderr=proc.stderr,
                returncode=proc.returncode,
                command=command,
            )
        return proc

    def _run_text(self, *args: str, ok_codes: tuple[int, ...] = (0,)) -> str:
        return self._run(*args, ok_codes=ok_codes).stdout

    # ------------------------------------------------------------------ #
    # Path safety
    # ------------------------------------------------------------------ #

    def _resolve_in_repo(self, path: Path | str) -> Path:
        """Resolve ``path`` and reject anything outside the working tree."""
        if path is None or not str(path).strip():
            raise GitError("path must be a non-empty string")
        raw = Path(str(path))
        repo = self.path.resolve()
        candidate = raw.resolve() if raw.is_absolute() else (self.path / raw).resolve()
        if candidate != repo and repo not in candidate.parents:
            raise GitError(f"path {str(path)!r} escapes repository {repo}")
        return candidate

    def _rel_path(self, path: Path | str) -> str:
        """Return a repo-relative, POSIX-style path after containment checks."""
        candidate = self._resolve_in_repo(path)
        return candidate.relative_to(self.path.resolve()).as_posix()

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def is_repo(self) -> bool:
        """True when ``self.path`` is the root of a git working tree."""
        if not self.path.exists():
            return False
        proc = self._run("rev-parse", "--show-toplevel", check=False)
        if proc.returncode != 0:
            return False
        try:
            top = Path(proc.stdout.strip()).resolve()
        except OSError:  # pragma: no cover - defensive
            return False
        return top == self.path.resolve()

    def current_branch(self) -> str:
        """Name of the checked-out branch.

        Works on a freshly-initialised, commit-less repository (``main``). On a
        detached HEAD it returns the short commit id. Raises :class:`GitError`
        when ``self.path`` is not a repository.
        """
        name = self._run_text("branch", "--show-current").strip()
        if name:
            return name
        return self._run_text("rev-parse", "--short", "HEAD").strip()

    def status_porcelain(self) -> list[str]:
        """Machine-readable working-tree status, one entry per changed path."""
        return [
            line
            for line in self._run_text("status", "--porcelain").splitlines()
            if line.strip()
        ]

    def log(self, *, max_count: int = 20, path: str | None = None) -> list[dict]:
        """Recent commits as ``[{sha, message, author, ts}, ...]`` (newest first).

        On a repository with no commits yet this returns ``[]`` rather than
        raising - "nothing recorded" is a valid, non-exceptional answer.
        """
        args = ["log", f"--max-count={int(max_count)}", f"--format={_LOG_FORMAT}"]
        if path is not None:
            args += ["--", self._rel_path(path)]
        proc = self._run(*args, check=False)
        if proc.returncode != 0:
            if _is_unborn(proc.stderr):
                return []
            raise GitError(
                f"git log failed: {proc.stderr.strip()}",
                stderr=proc.stderr,
                returncode=proc.returncode,
                command=[self.git_bin, *args],
            )
        return self._parse_log(proc.stdout)

    def file_history(self, path: str, *, max_count: int = 20) -> list[dict]:
        """Commits that touched ``path``, same shape as :meth:`log`."""
        rel = self._rel_path(path)
        args = [
            "log",
            f"--max-count={int(max_count)}",
            f"--format={_LOG_FORMAT}",
            "--",
            rel,
        ]
        proc = self._run(*args, check=False)
        if proc.returncode != 0:
            if _is_unborn(proc.stderr):
                return []
            raise GitError(
                f"git log failed for {rel!r}: {proc.stderr.strip()}",
                stderr=proc.stderr,
                returncode=proc.returncode,
                command=[self.git_bin, *args],
            )
        return self._parse_log(proc.stdout)

    def show(self, sha: str, path: str) -> str:
        """Exact file content of ``path`` at revision ``sha``."""
        rel = self._rel_path(path)
        return self._run_text("show", f"{sha}:{rel}")

    def diff(self, sha_a: str, sha_b: str, *, path: str | None = None) -> str:
        """Unified diff between two revisions, optionally limited to ``path``.

        ``git diff`` exits ``1`` when differences exist; that is success here,
        not an error.
        """
        args = ["diff", "--no-color", sha_a, sha_b]
        if path is not None:
            args += ["--", self._rel_path(path)]
        proc = self._run(*args, check=False)
        if proc.returncode not in (0, 1):
            raise GitError(
                f"git diff failed: {proc.stderr.strip()}",
                stderr=proc.stderr,
                returncode=proc.returncode,
                command=[self.git_bin, *args],
            )
        return proc.stdout

    def diff_blobs(self, ref_a: str, ref_b: str) -> str:
        """Diff two arbitrary git object specs, e.g. ``"<sha>:<path>"``.

        Used by :mod:`uap.gitx.sync` to compare the committed content of two
        version files directly (rather than the trees that contain them).
        """
        args = ["diff", "--no-color", ref_a, ref_b]
        proc = self._run(*args, check=False)
        if proc.returncode not in (0, 1):
            raise GitError(
                f"git diff failed: {proc.stderr.strip()}",
                stderr=proc.stderr,
                returncode=proc.returncode,
                command=[self.git_bin, *args],
            )
        return proc.stdout

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #

    def commit_all(self, message: str, *, author: str | None = None) -> str | None:
        """Stage everything and commit.

        Returns the new commit SHA, or ``None`` when the working tree already
        matches HEAD (nothing to commit - a no-op is not an error).

        ``author`` is a git author identity. A bare name such as ``"uap"`` is
        expanded to ``uap <uap@local>`` so the actor is always visible in the
        log without callers having to build an email (section 49).
        """
        text = str(message).strip()
        if not text:
            raise GitError("commit message must be non-empty")

        self._run("add", "-A")
        if not self._run_text("status", "--porcelain").strip():
            return None

        args = ["commit", "-m", text]
        if author is not None:
            args.append(f"--author={self._format_author(author)}")
        self._run(*args)
        return self._run_text("rev-parse", "HEAD").strip()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_author(author: str) -> str:
        text = str(author).strip()
        if not text:
            raise GitError("author must be a non-empty string")
        if "<" in text and ">" in text:
            return text
        return f"{text} <{text}@local>"

    @staticmethod
    def _parse_log(text: str) -> list[dict]:
        entries: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = line.split(_FIELD)
            if len(parts) < 4:
                continue
            entries.append(
                {
                    "sha": parts[0],
                    "message": parts[1],
                    "author": parts[2],
                    "ts": parts[3],
                }
            )
        return entries


def _is_unborn(stderr: str) -> bool:
    """True when git complained that the current branch has no commits yet."""
    lowered = stderr.lower()
    return (
        "does not have any commits yet" in lowered
        or "unknown revision" in lowered
        or "bad revision" in lowered
    )
