"""Hooks in ~/.claude/settings.json for machines set up without the crew plugin (`crew_dev.py install`).

The plugin brings its own hooks; `crew_mcp.py` uses `has_ours`/`write_hooks` to drop these when a machine moves over.
"""
from __future__ import annotations

import json
import os
import shlex
import sys

from crew_dev import HOOK_EVENTS, MARK, TOOL_MATCHER, _write_private

CREW_DEV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crew_dev.py")


def claude_settings_path() -> str:
    return os.environ.get("CREW_CLAUDE_SETTINGS") or os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def _hook_command() -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(CREW_DEV)} hook"


def _is_ours(command) -> bool:
    """Our hook is `<python> <path>/crew_dev.py hook`, whatever quoting the path needed."""
    try:
        argv = shlex.split(str(command))
    except ValueError:
        return MARK in str(command)
    return len(argv) >= 2 and os.path.basename(argv[-2]) == "crew_dev.py" and argv[-1] == "hook"


def _strip_ours(settings: dict) -> dict:
    hooks = settings.get("hooks") or {}
    for name in list(hooks):
        kept = [entry for entry in hooks[name]
                if not any(_is_ours(h.get("command", "")) for h in (entry.get("hooks") or []))]
        if kept:
            hooks[name] = kept
        else:
            del hooks[name]
    if not hooks:
        settings.pop("hooks", None)
    return settings


def write_hooks(install: bool) -> str:
    """Add or remove our hooks. Follows a symlinked settings.json, keeps its permissions, and keeps the
    backup taken before the first change (later runs do not overwrite it)."""
    path = os.path.realpath(claude_settings_path())
    try:
        with open(path, encoding="utf-8") as f:
            original = f.read()
        settings = json.loads(original or "{}")
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        original, settings, mode = None, {}, 0o600
    if original is not None and not os.path.exists(path + ".crew-bak"):
        _write_private(path + ".crew-bak", original)
    settings = _strip_ours(settings)
    if install:
        hooks = settings.setdefault("hooks", {})
        for name in HOOK_EVENTS:
            entry = {"hooks": [{"type": "command", "command": _hook_command(), "timeout": 10}]}
            if name == "PostToolUse":
                entry = {"matcher": TOOL_MATCHER, **entry}
            hooks.setdefault(name, []).append(entry)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return path


def has_ours() -> bool:
    """True when settings.json still carries hooks from `crew_dev.py install`."""
    try:
        with open(os.path.realpath(claude_settings_path()), encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError):
        return False
    return any(_is_ours(h.get("command", "")) for entries in (settings.get("hooks") or {}).values()
               for e in entries for h in (e.get("hooks") or []))
