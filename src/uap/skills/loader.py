"""Load :class:`Skill` definitions from Markdown files (Master section 11).

A skill file is Markdown with a YAML-*ish* frontmatter block delimited by ``---``
lines::

    ---
    name: api-recon
    description: Map an API's attack surface
    domain: bbp
    required_capabilities: http.request, dns.lookup
    version: 1
    ---

    # Methodology
    ...

The body after the closing delimiter becomes ``methodology``. The frontmatter is
parsed by a small hand-rolled ``key: value`` reader on purpose: skills must load
without pulling in a YAML dependency, and the accepted grammar stays tiny and
predictable.

``load_skills_from_dir`` fails loudly on a malformed file; ``load_skills_lenient``
skips malformed files and records a warning instead.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from uap.skills.skill import Skill

__all__ = [
    "FrontmatterError",
    "load_skill_file",
    "load_skills_from_dir",
    "load_skills_lenient",
    "parse_frontmatter",
]

_DELIMITER = "---"
# Keys the loader understands. Anything else is ignored, not an error.
_KNOWN_KEYS = {
    "name",
    "description",
    "domain",
    "methodology",
    "required_capabilities",
    "version",
}


class FrontmatterError(ValueError):
    """A skill file could not be parsed. Carries the offending path."""

    def __init__(self, path: Path | str, reason: str) -> None:
        self.path = Path(path)
        self.reason = reason
        super().__init__(f"{self.path}: {reason}")


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``text`` into ``(frontmatter, body)``.

    Raises :class:`ValueError` (without a path -- the caller adds it) when the
    file does not open with a ``---`` delimiter or never closes it.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _DELIMITER:
        raise ValueError("missing frontmatter: file must start with a '---' line")

    close = None
    for index in range(1, len(lines)):
        if lines[index].strip() == _DELIMITER:
            close = index
            break
    if close is None:
        raise ValueError("unterminated frontmatter: missing closing '---' line")

    frontmatter: dict[str, str] = {}
    for offset, raw in enumerate(lines[1:close], start=2):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"line {offset}: expected 'key: value', got {raw!r}")
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            raise ValueError(f"line {offset}: empty key")
        if key not in _KNOWN_KEYS:
            # Unknown keys are tolerated (forward-compatible frontmatter).
            continue
        frontmatter[key] = value.strip()

    body = "\n".join(lines[close + 1 :]).strip("\n")
    return frontmatter, body


def _skill_from_frontmatter(path: Path, frontmatter: dict[str, str], body: str) -> Skill:
    name = frontmatter.get("name", "").strip()
    if not name:
        raise FrontmatterError(path, "missing required frontmatter key 'name'")

    version = 1
    if "version" in frontmatter:
        try:
            version = int(frontmatter["version"])
        except ValueError as exc:
            raise FrontmatterError(
                path, f"invalid version {frontmatter['version']!r} (expected an integer)"
            ) from exc

    capabilities = [
        cap.strip()
        for cap in frontmatter.get("required_capabilities", "").split(",")
        if cap.strip()
    ]

    methodology = body.strip()
    if not methodology:
        methodology = frontmatter.get("methodology", "").strip()

    return Skill(
        name=name,
        description=frontmatter.get("description", "").strip(),
        domain=frontmatter.get("domain", "").strip(),
        methodology=methodology,
        required_capabilities=capabilities,
        version=version,
    )


def load_skill_file(path: Path | str) -> Skill:
    """Parse a single skill file. Raises :class:`FrontmatterError` on failure."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    try:
        frontmatter, body = parse_frontmatter(text)
    except ValueError as exc:
        raise FrontmatterError(path, str(exc)) from exc
    return _skill_from_frontmatter(path, frontmatter, body)


def load_skills_from_dir(path: Path | str) -> list[Skill]:
    """Load every ``*.md`` skill in ``path``, sorted by filename.

    Fails loudly: the first malformed file aborts the load with a
    :class:`ValueError` whose message contains that file's path.
    """
    directory = Path(path)
    if not directory.is_dir():
        raise ValueError(f"{directory}: not a directory")

    skills: list[Skill] = []
    for file in sorted(directory.glob("*.md")):
        try:
            skills.append(load_skill_file(file))
        except FrontmatterError as exc:
            raise ValueError(f"invalid skill file {exc.path}: {exc.reason}") from exc
    return skills


def load_skills_lenient(path: Path | str) -> list[Skill]:
    """Like :func:`load_skills_from_dir`, but skips malformed files.

    Each skipped file emits a :class:`UserWarning` naming the path, so a broken
    skill never disappears silently.
    """
    directory = Path(path)
    if not directory.is_dir():
        raise ValueError(f"{directory}: not a directory")

    skills: list[Skill] = []
    for file in sorted(directory.glob("*.md")):
        try:
            skills.append(load_skill_file(file))
        except FrontmatterError as exc:
            warnings.warn(f"skipping invalid skill file {exc.path}: {exc.reason}", stacklevel=2)
    return skills
