"""`ta doctor` — check that the desk is wired correctly before a run.

The failure modes this catches are all silent ones: a subagent file whose
frontmatter does not parse (Claude Code skips it, and `/analyze` then invokes an
agent that does not exist), a command referencing a renamed agent, or an agent
documenting a `bin/ta` subcommand that no longer exists. None of these surface as
an error — they surface as a run that quietly does less than it should.
"""

from __future__ import annotations

import re
from pathlib import Path

from .common import ROOT, env

AGENTS_DIR = ROOT / ".claude" / "agents"
COMMANDS_DIR = ROOT / ".claude" / "commands"

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# `bin/ta <subcommand>` as written in the docs, including the `memory log` form.
_TA_CALL_RE = re.compile(r"bin/ta\s+([a-z][a-z-]*)(?:\s+([a-z][a-z-]*))?")

EXPECTED_AGENTS = {
    "market-analyst", "sentiment-analyst", "news-analyst", "fundamentals-analyst",
    "bull-researcher", "bear-researcher", "research-manager", "trader",
    "aggressive-risk-analyst", "conservative-risk-analyst", "neutral-risk-analyst",
    "portfolio-manager", "reflection-analyst",
}


def _frontmatter(path: Path) -> dict[str, str] | None:
    """Parse the leading YAML block. Only flat `key: value` pairs are used here."""
    match = _FRONTMATTER_RE.match(path.read_text())
    if not match:
        return None
    fields = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def _choices(parser) -> dict:
    """The subcommand map of an argparse parser, or {} if it has no subparsers."""
    for action in parser._actions:
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            return action.choices
    return {}


def _known_commands() -> tuple[set[str], set[str]]:
    """Subcommands the CLI actually exposes, read from the live parser.

    Reading the parser rather than a hardcoded list is the point: this check only
    has value if it cannot drift from the real CLI.
    """
    from ta import build_parser  # imported lazily: ta.py imports this module

    top = _choices(build_parser())
    nested: set[str] = set()
    for parser in top.values():
        nested.update(_choices(parser).keys())
    return set(top), nested


def run() -> str:
    problems: list[str] = []
    notes: list[str] = []
    lines: list[str] = ["# Doctor", ""]

    # --- dependencies ------------------------------------------------------
    lines.append("## Dependencies")
    for module in ("yfinance", "stockstats", "pandas", "requests"):
        try:
            imported = __import__(module)
            version = getattr(imported, "__version__", None)
            if version is None:  # stockstats and others expose no __version__
                from importlib.metadata import PackageNotFoundError, version as pkg_version

                try:
                    version = pkg_version(module)
                except PackageNotFoundError:
                    version = "installed"
            lines.append(f"- ok    {module} {version}")
        except ImportError:
            lines.append(f"- FAIL  {module} not installed")
            problems.append(f"{module} is missing — run ./setup.sh")

    # --- agents ------------------------------------------------------------
    lines.append("\n## Subagents")
    found: set[str] = set()
    if not AGENTS_DIR.exists():
        problems.append(f"no agents directory at {AGENTS_DIR}")
    else:
        for path in sorted(AGENTS_DIR.glob("*.md")):
            fields = _frontmatter(path)
            if fields is None:
                lines.append(f"- FAIL  {path.name}: no YAML frontmatter — Claude Code will ignore this file")
                problems.append(f"{path.name} has no frontmatter")
                continue
            missing = [k for k in ("name", "description") if k not in fields]
            if missing:
                lines.append(f"- FAIL  {path.name}: missing {', '.join(missing)}")
                problems.append(f"{path.name} missing {', '.join(missing)}")
                continue
            name = fields["name"]
            found.add(name)
            if name != path.stem:
                lines.append(f"- WARN  {path.name}: frontmatter name is '{name}'")
                notes.append(f"{path.name}: filename and agent name differ (harmless, but confusing)")
            else:
                lines.append(f"- ok    {name} ({fields.get('model', 'inherit')}, tools: {fields.get('tools', 'all')})")

    for expected in sorted(EXPECTED_AGENTS - found):
        lines.append(f"- FAIL  {expected} not defined")
        problems.append(f"subagent '{expected}' is missing — /analyze will fail at its stage")

    # --- commands ----------------------------------------------------------
    lines.append("\n## Commands")
    if not COMMANDS_DIR.exists():
        problems.append(f"no commands directory at {COMMANDS_DIR}")
    else:
        for path in sorted(COMMANDS_DIR.glob("*.md")):
            fields = _frontmatter(path)
            if fields is None or "description" not in fields:
                lines.append(f"- FAIL  {path.name}: missing frontmatter or description")
                problems.append(f"{path.name} frontmatter is incomplete")
            else:
                lines.append(f"- ok    /{path.stem}")

            # Agent names a command dispatches to must exist.
            body = path.read_text()
            referenced_names = set(re.findall(r"`([a-z][a-z-]*)`", body)) & (
                EXPECTED_AGENTS | {r for r in re.findall(r"`([a-z][a-z-]+-(?:analyst|researcher|manager))`", body)}
            )
            for referenced in referenced_names:
                if referenced not in found:
                    lines.append(f"- FAIL  /{path.stem} references unknown agent '{referenced}'")
                    problems.append(f"/{path.stem} references agent '{referenced}' which does not exist")

    # --- documented CLI calls actually exist -------------------------------
    lines.append("\n## Documented `bin/ta` calls")
    top, nested = _known_commands()
    bad: set[str] = set()
    for path in list(AGENTS_DIR.glob("*.md")) + list(COMMANDS_DIR.glob("*.md")) + [ROOT / "CLAUDE.md"]:
        if not path.exists():
            continue
        for first, second in _TA_CALL_RE.findall(path.read_text()):
            if first not in top:
                bad.add(f"{path.name}: `bin/ta {first}`")
            elif first in ("memory", "run") and second and second not in nested:
                bad.add(f"{path.name}: `bin/ta {first} {second}`")
    if bad:
        for item in sorted(bad):
            lines.append(f"- FAIL  {item} is not a real subcommand")
            problems.append(item)
    else:
        lines.append("- ok    every documented subcommand exists")

    # --- optional config ---------------------------------------------------
    lines.append("\n## Optional configuration")
    if env("FRED_API_KEY"):
        lines.append("- ok    FRED_API_KEY set — macro series available")
    else:
        lines.append("- note  FRED_API_KEY unset — `bin/ta macro` returns a labeled placeholder")
        notes.append("FRED macro series are unavailable without a free key in .env")

    # --- verdict -----------------------------------------------------------
    lines.append("\n## Verdict\n")
    if problems:
        lines.append(f"**{len(problems)} problem(s) found:**\n")
        lines.extend(f"- {p}" for p in problems)
    else:
        lines.append("**Healthy.** The desk is wired correctly — run `/analyze TICKER`.")
    if notes:
        lines.append("\nNotes:")
        lines.extend(f"- {n}" for n in notes)

    return "\n".join(lines)
