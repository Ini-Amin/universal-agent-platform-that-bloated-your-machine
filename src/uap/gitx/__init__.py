"""First-class Git integration (Master sections 2.4, 49, 50, 51, 63).

Public API::

    from uap.gitx import GitRepo, DefinitionGitSync, CompatibilityChecker

* :class:`GitRepo` - a safe ``subprocess`` git wrapper (no GitPython).
* :class:`DefinitionGitSync` - mirror Library definitions to versioned JSON
  files inside a repository, with AI-visible commit messages (section 49).
* :class:`CompatibilityChecker` - schema/version-pin checks that produce a
  :class:`CompatibilityReport` and a non-executed migration proposal (section 51).
"""

from .compat import (
    CompatibilityChecker,
    CompatibilityIssue,
    CompatibilityReport,
)
from .repo import GitError, GitRepo
from .sync import DefinitionGitSync

__all__ = [
    "CompatibilityChecker",
    "CompatibilityIssue",
    "CompatibilityReport",
    "DefinitionGitSync",
    "GitError",
    "GitRepo",
]
