#!/usr/bin/env python3
"""crew_dev.py: the dev-machine side of crew-hub. Standard library only, Python 3.9+. `crew_dev.py -h` lists commands.

Only events whose cwd is inside a registered crew worktree leave this machine, and only allow-listed
fields: never prompts, tool input, tool output or chat text.
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop", "PostToolUse", "TeammateIdle",
               "Stop", "StopFailure", "SessionEnd")
TOOL_MATCHER = "Agent|Task|SubagentHandback|SendMessage"
MARK = "crew_dev.py hook"
SEND_SECONDS = 2.0
MAX_BATCH = 200                 # hub accepts up to 500 per batch
MAX_BATCH_BYTES = 1_000_000     # hub accepts up to 2 MB per body
MAX_REPORT = 8000               # characters; the hub keeps the same amount
MAX_SPOOL_BYTES = 20_000_000    # past this, the oldest queued events are dropped
BACKOFF_DOWN = 60               # seconds without network calls after the hub could not be reached
BACKOFF_AUTH = 600              # ... after the hub refused the token
GIT_SECONDS = 120               # a git call that takes longer (stuck fetch) fails instead of hanging the MCP server
KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$")
# Only crew workers' reports leave the machine; other agents (Explore, general-purpose...) send status only.
CONTRACT = re.compile(r"^\s*crew-worker v\d+:\s*(succeeded|failed|blocked)\b", re.M)


# ---- files ---------------------------------------------------------------------------------------------
def home() -> str:
    return os.environ.get("CREW_HOME") or os.path.join(os.path.expanduser("~"), ".crew")


def _write_private(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def load_config() -> dict | None:
    try:
        with open(os.path.join(home(), "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_config(cfg: dict) -> None:
    _write_private(os.path.join(home(), "config.json"), json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


def update_config(change) -> dict:
    """Load, change and save config.json under a lock: every Claude Code session runs its own crew MCP server."""
    with _lock("config.lock"):
        cfg = load_config() or {}
        change(cfg)
        save_config(cfg)
        return cfg


def _plugin_marker() -> str:
    return os.path.join(home(), "plugin")


@contextmanager
def _lock(name: str, until: float | None = None):
    """Exclusive file lock. With `until` (a monotonic deadline) give up then and yield False."""
    os.makedirs(home(), mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(home(), name), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if until is not None else 0))
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    yield False
                    return
                time.sleep(0.05)
        yield True
    finally:
        os.close(fd)


def _spool() -> str:
    return os.path.join(home(), "spool.jsonl")


# ---- hub API -------------------------------------------------------------------------------------------
def api(cfg: dict, path: str, body: dict | None = None, timeout: float = 10.0, method: str = "POST") -> tuple[int, dict]:
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(cfg["hub"].rstrip("/") + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + cfg["token"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, {"detail": str(e)}


def _need_config() -> dict:
    cfg = load_config()
    if not cfg:
        sys.exit("Chưa cài: chạy crew_dev.py install --hub URL --machine TÊN-MÁY trước.")
    return cfg


def _key(value: str) -> str:
    if not KEY.match(value):
        sys.exit("Mã task không hợp lệ: " + value)
    return value


def hub_task(cfg: dict, key: str) -> dict | None:
    """The open task the hub lists under this key, whatever its case (t-3 is T-3); None if there is none."""
    status, body = api(cfg, "/api/tasks", method="GET")
    found = [t for t in body.get("tasks") or [] if isinstance(t.get("key"), str)] if status == 200 else []
    return next((t for t in found if t["key"] == key), None) or \
        next((t for t in found if t["key"].lower() == key.lower()), None)


def hub_key(cfg: dict, key: str) -> str:
    """Branch and worktree names follow the hub's spelling of a key."""
    task = hub_task(cfg, key)
    return task["key"] if task else key


# ---- hook: filter, queue, send -------------------------------------------------------------------------
def _real(path: str) -> str:
    return os.path.realpath(path) if path else ""


def in_worktree(cwd: str, cfg: dict) -> bool:
    cwd = _real(cwd)
    return any(cwd == wt or cwd.startswith(wt.rstrip("/") + "/") for wt in (cfg.get("worktrees") or {}).values())


def _head(text, lines: int = 20) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    return "\n".join(text.splitlines()[:lines])[:MAX_REPORT]


def _crew_report(text) -> str | None:
    """First lines of a report that carries the crew-worker contract line; anything else stays local."""
    head = _head(text)
    return head if head and CONTRACT.search(head) else None


def _first_text(*candidates) -> str | None:
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c
    return None


def filter_event(payload: dict, cfg: dict) -> dict | None:
    """Keep only what the canvas needs. Returns None when the event must not leave this machine."""
    if not isinstance(payload, dict):
        return None
    cwd = payload.get("cwd") or ""
    if not isinstance(cwd, str) or not in_worktree(cwd, cfg):
        return None
    hook = payload.get("hook_event_name")
    if hook not in HOOK_EVENTS:
        return None
    ev = {"event_id": uuid.uuid4().hex, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "hook": hook, "session_id": payload.get("session_id"), "cwd": _real(cwd)}
    for k in ("agent_id", "agent_type", "teammate_name", "source"):
        if isinstance(payload.get(k), str):
            ev[k] = payload[k]
    if hook == "SubagentStop":
        ev["report"] = _crew_report(payload.get("last_assistant_message"))
        tasks = payload.get("background_tasks") or []
        ev["background_other"] = sum(1 for t in tasks if isinstance(t, dict) and t.get("id") != payload.get("agent_id")
                                     and t.get("status", "running") == "running")
    elif hook == "PostToolUse":
        tool = payload.get("tool_name")
        tin = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        tout = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {}
        ev["tool_name"] = tool
        if tool in ("Agent", "Task"):
            ev["agent"] = {"id": _first_text(tout.get("agentId"), tout.get("agent_id")),
                           "description": _first_text(tout.get("description"), tin.get("description"), tin.get("name")),
                           "model": _first_text(tout.get("resolvedModel"))}
        elif tool == "SendMessage":
            ev["to"] = _first_text(tin.get("to"))
        elif tool == "SubagentHandback":
            ev["report"] = _crew_report(_first_text(tin.get("report"), tin.get("result"), tin.get("message"),
                                             tin.get("content"), tout.get("report"), tout.get("result")))
        else:
            return None
    return ev


def _read_spool() -> list:
    try:
        with open(_spool(), encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return []


def _keep_spool(lines: list) -> None:
    _write_private(_spool(), "".join(ln + "\n" for ln in lines))


def queue_event(ev: dict) -> None:
    with _lock("spool.lock"):
        with open(_spool(), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        os.chmod(_spool(), 0o600)
        if os.path.getsize(_spool()) > MAX_SPOOL_BYTES:
            lines = _read_spool()
            _keep_spool(lines[len(lines) // 2:])
            _log(f"spool over {MAX_SPOOL_BYTES} bytes: dropped the oldest {len(lines) // 2} events")


def _drop_first(n: int) -> None:
    with _lock("spool.lock"):
        _keep_spool(_read_spool()[n:])


def _batch(lines: list, cap: int) -> list:
    out, size = [], 0
    for ln in lines[:cap]:
        size += len(ln.encode("utf-8")) + 1
        if out and size > MAX_BATCH_BYTES:
            break
        out.append(ln)
    return out


def backoff() -> tuple:
    """(until, reason): no network calls from hooks before `until` (epoch seconds)."""
    try:
        with open(os.path.join(home(), "backoff.json"), encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("until", 0)), str(data.get("reason", ""))
    except (OSError, ValueError):
        return 0.0, ""


def _set_backoff(seconds: float, reason: str) -> None:
    _write_private(os.path.join(home(), "backoff.json"), json.dumps({"until": time.time() + seconds, "reason": reason}))
    _log(reason)


def flush(cfg: dict, seconds: float = SEND_SECONDS, respect_backoff: bool = True) -> int:
    """Send queued events in order; returns how many left the spool.

    One flusher at a time: a hook that finds another one busy waits for it (within its own deadline) and then
    sends what is left, so the last event of a session is not stranded.
    """
    deadline = time.monotonic() + seconds
    if respect_backoff and backoff()[0] > time.time():
        return 0
    done = 0
    cap = MAX_BATCH
    with _lock("flush.lock", until=deadline) as mine:
        if not mine:
            return 0
        while time.monotonic() < deadline:
            with _lock("spool.lock"):
                batch = _batch(_read_spool(), cap)
            if not batch:
                break
            events = []
            for ln in batch:
                try:
                    events.append(json.loads(ln))
                except ValueError:
                    pass  # a torn line is dropped with its batch
            status, body = api(cfg, "/api/events", {"events": events}, timeout=max(0.2, deadline - time.monotonic()))
            if status == 200:
                _drop_first(len(batch))
                done += len(batch)
                cap = MAX_BATCH
            elif status == 413 and len(batch) > 1:
                cap = max(1, len(batch) // 2)
            elif status in (400, 413, 422):
                _drop_first(len(batch))
                done += len(batch)
                _log(f"hub refused {len(batch)} event(s) ({status}): {body.get('detail', '')}; dropped them")
            elif status in (401, 403):
                _set_backoff(BACKOFF_AUTH, f"hub từ chối token ({status}); cấp token mới rồi chạy lại install")
                break
            else:
                _set_backoff(BACKOFF_DOWN, f"không gửi được tới hub ({status}): {body.get('detail', '')}")
                break
    return done


def _log(message: str) -> None:
    try:
        with open(os.path.join(home(), "hook.log"), "a", encoding="utf-8") as f:
            f.write(datetime.now().isoformat(timespec="seconds") + " " + message + "\n")
    except OSError:
        pass


def hook_main(stdin, plugin: bool = False) -> int:
    """Never raises and always exits 0: a hub problem must not stop Claude.

    Once the crew plugin's hooks have run on this machine, hooks left in settings.json by `install` stay quiet,
    so an event is never sent twice.
    """
    try:
        if plugin:
            if not os.path.exists(_plugin_marker()):
                _write_private(_plugin_marker(), "")
        elif os.path.exists(_plugin_marker()):
            return 0
        cfg = load_config()
        if not cfg:
            return 0
        payload = json.loads(stdin.read() or "{}")
        ev = filter_event(payload, cfg) if isinstance(payload, dict) else None
        if ev:
            queue_event(ev)
            flush(cfg)
    except Exception as e:  # noqa: BLE001 - see docstring
        _log(f"hook error: {e!r}")
    return 0


# ---- commands ------------------------------------------------------------------------------------------
def cmd_install(args) -> None:
    token = sys.stdin.read().strip() if not sys.stdin.isatty() else getpass.getpass("Token crew-hub: ").strip()
    if not token:
        sys.exit("Thiếu token.")
    new = {"hub": args.hub.rstrip("/"), "token": token, "machine": args.machine}
    status, body = api(new, "/api/events", {"events": []})
    if status != 200:
        sys.exit(f"Hub từ chối token hoặc không tới được ({status}): {body.get('detail', '')}")
    update_config(lambda cfg: cfg.update({**new, "worktrees": cfg.get("worktrees") or {}}))
    for stale in ("backoff.json", "plugin"):  # a working token ends any back-off; settings hooks speak again
        try:
            os.remove(os.path.join(home(), stale))
        except FileNotFoundError:
            pass
    from crew_settings import write_hooks
    path = write_hooks(install=True)
    print(f"Đã lưu cấu hình vào {os.path.join(home(), 'config.json')} (quyền 600).")
    print(f"Đã thêm hook vào {path}; bản cũ ở {path}.crew-bak. Mở lại phiên Claude Code để hook có hiệu lực.")


def cmd_uninstall(_args) -> None:
    from crew_settings import write_hooks
    path = write_hooks(install=False)
    print(f"Đã gỡ hook crew khỏi {path}. Cấu hình ở {home()} vẫn giữ.")


def cmd_claim(args) -> None:
    cfg = _need_config()
    status, body = api(cfg, f"/api/tasks/{_key(args.key)}/claim")
    if status != 200:
        sys.exit(f"Không claim được ({status}): {body.get('detail', '')}")
    key = body.get("key") or args.key
    print(f"Đã claim {key} về máy {body.get('machine')}. Tiếp theo: crew_dev.py start {key}")


def _git(args: list, cwd: str, check: bool = True):
    """Never waits on a prompt (credentials, host key) and never reads stdin: the MCP server's stdin is JSON-RPC."""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True, timeout=GIT_SECONDS,
                           stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        sys.exit(f"git {args[0]} quá {GIT_SECONDS} giây; kiểm tra mạng hoặc quyền truy cập remote.")
    return r.stdout.strip() if check else r


def cmd_start(args) -> None:
    task = hub_task(_need_config(), _key(args.key))
    if task is None:
        sys.exit(f"Hub không có task {args.key} đang mở, hoặc không tới được hub.")
    try:
        _start(task["key"], task.get("kind") or "feature", args.repo or os.getcwd(), args.base)
    except subprocess.CalledProcessError as e:
        sys.exit(f"git {e.cmd[1]} lỗi: {(e.stderr or '').strip() or e}")


def _start(key: str, kind: str, where: str, base: str | None) -> None:
    repo = _git(["rev-parse", "--show-toplevel"], where)
    if not base:
        try:
            base = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo).split("/", 1)[1]
        except (subprocess.CalledProcessError, IndexError):
            base = "main"
    path = os.path.join(repo, ".claude", "worktrees", key)
    if os.path.exists(path):
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], path)   # chosen by an earlier start
    else:
        branch = f"{kind}/{key}"   # feature/KEY, fix/KEY or hotfix/KEY, from the task's kind on the hub
        _git(["fetch", "origin", base], repo)
        has_branch = _git(["rev-parse", "--verify", "--quiet", "refs/heads/" + branch], repo, check=False).returncode == 0
        _git(["worktree", "add", path, branch] if has_branch else ["worktree", "add", "-b", branch, path, "origin/" + base], repo)
    exclude = os.path.join(_git(["rev-parse", "--git-common-dir"], repo), "info", "exclude")
    exclude = exclude if os.path.isabs(exclude) else os.path.join(repo, exclude)
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    current = open(exclude, encoding="utf-8").read() if os.path.exists(exclude) else ""
    if ".claude/worktrees/" not in current.split():
        with open(exclude, "a", encoding="utf-8") as f:
            f.write(("" if current.endswith("\n") or not current else "\n") + ".claude/worktrees/\n")
    real = os.path.realpath(path)
    status, body = api(_need_config(), f"/api/tasks/{key}/worktree", {"path": real, "branch": branch})
    if status != 200:
        sys.exit(f"Hub không nhận worktree ({status}): {body.get('detail', '')}")
    def remember(cfg):
        cfg.setdefault("worktrees", {})[key] = real
    update_config(remember)  # reloads first: another session may have registered its own worktree meanwhile
    print(f"Worktree: {real} (nhánh {branch}).")
    print(f"Làm việc trong đó để canvas thấy: cd {shlex.quote(real)} && claude")


def cmd_release(args) -> None:
    cfg = _need_config()
    status, body = api(cfg, f"/api/tasks/{_key(args.key)}/release")
    if status != 200:
        sys.exit(f"Không nhả được ({status}): {body.get('detail', '')}")
    key = body.get("key") or args.key
    update_config(lambda cfg: (cfg.get("worktrees") or {}).pop(key, None))
    print(f"Đã trả {key} về hàng chờ. Worktree trên máy vẫn giữ nguyên.")


def cmd_login(_args) -> None:
    cfg = _need_config()
    status, body = api(cfg, "/api/login-codes")
    if status != 200:
        sys.exit(f"Không lấy được mã ({status}): {body.get('detail', '')}")
    print(f"Mã đăng nhập: {body['code']} (dùng một lần, trong {body['expires_in'] // 60} phút)")
    print(f"Mở {cfg['hub']}/login rồi nhập mã.")


def cmd_flush(_args) -> None:
    """Send now, ignoring the hook back-off, and say what is still waiting and why."""
    print(f"Đã gửi {flush(_need_config(), seconds=30, respect_backoff=False)} sự kiện.")
    left = len(_read_spool())
    if left:
        print(f"Còn {left} sự kiện chờ gửi. Lý do gần nhất: {backoff()[1] or 'xem ' + os.path.join(home(), 'hook.log')}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="crew_dev.py", description="Phía máy dev của crew-hub")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", help="lưu hub + token (đọc từ stdin hoặc hỏi ẩn) và thêm hook vào settings cấp user")
    i.add_argument("--hub", required=True, help="vd http://<ip-hub>:8790 hoặc https://<máy-hub>.<tailnet>.ts.net:8443")
    i.add_argument("--machine", required=True, help="nhãn máy, trùng nhãn khi cấp token")
    sub.add_parser("uninstall", help="gỡ hook do script này thêm")
    sub.add_parser("claim", help="nhận task trong hàng chờ về máy này").add_argument("key")
    sub.add_parser("release", help="trả task về hàng chờ").add_argument("key")
    s = sub.add_parser("start", help="tạo worktree <repo>/.claude/worktrees/KEY trên nhánh <loại>/KEY "
                                     "(feature, fix hoặc hotfix theo task trên hub)")
    s.add_argument("key")
    s.add_argument("--repo")
    s.add_argument("--base")
    sub.add_parser("login", help="in mã đăng nhập canvas dùng một lần")
    sub.add_parser("hook", help="hook của Claude Code: xếp một sự kiện vào hàng đợi rồi gửi, không bao giờ chặn") \
        .add_argument("--plugin", action="store_true", help="gọi từ plugin crew (hook trong settings.json sẽ im)")
    sub.add_parser("flush", help="gửi ngay những gì còn trong hàng đợi")
    args = p.parse_args(argv)
    if args.cmd == "hook":
        return hook_main(sys.stdin, plugin=args.plugin)
    {"install": cmd_install, "uninstall": cmd_uninstall, "claim": cmd_claim, "start": cmd_start,
     "release": cmd_release, "login": cmd_login, "flush": cmd_flush}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
