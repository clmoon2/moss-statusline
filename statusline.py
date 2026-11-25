"""Moss — a local Codex footer. Run with Python 3.9+ over MCP stdio."""

import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


URI = "ui://my-statusline/footer"
COMPACT_URI = "ui://my-statusline/compact"
PROVIDERS = {
    URI: ("statusline", "Moss footer"),
    COMPACT_URI: ("statusline_compact", "Moss · compact"),
}


def advertised_tools():
    return [
        {"name": name, "title": title,
         "description": "Render the current Codex thread's status line.",
         "inputSchema": {"type": "object", "properties": {}},
         "_meta": {"openai/ui": {"entrypoints": [{"type": "statusline", "resourceUri": uri}]}}}
        for uri, (name, title) in PROVIDERS.items()
    ]


ACTIVITY = {
    "starting": ("◌", "cyan"),
    "ready": ("●", "green"),
    "working": ("▶", "green"),
    "thinking": ("✦", "cyan"),
    "waiting": ("◈", "yellow"),
}
branch_cache = {}


def text(value, color, bold=False):
    return {
        "type": "text",
        "props": {"color": color, "weight": "bold" if bold else "normal"},
        "children": [value],
    }


def row(children, gap=1, **props):
    return {"type": "row", "props": {"gap": gap, **props}, "children": children}


def compact(value, limit):
    value = "".join(char for char in value if char.isprintable())
    return value if len(value) <= limit else "…" + value[-(limit - 1):]


def git_status(cwd, short=False):
    now = time.monotonic()
    cache_key = (cwd, short)
    expires, cached = branch_cache.get(cache_key, (0, []))
    if now < expires:
        return cached

    parts = []
    git = shutil.which("git")
    if git and Path(cwd).is_absolute():
        parts = read_worktree(git, cwd, short)
    if len(branch_cache) >= 64:
        branch_cache.clear()
    branch_cache[cache_key] = (now + 5, parts)
    return parts


def read_worktree(git, cwd, short):
    # Resolve Git through our PATH, never by searching inside the displayed workspace.
    command = [
        git, "--no-pager", "--no-optional-locks",
        "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
        "-C", cwd,
    ]
    # A parent launched inside a different Git operation can have its own GIT_DIR,
    # worktree or index. The footer must follow the displayed thread's directory.
    config_sources = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"}
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("GIT_") or name in config_sources}
    env.update(GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="", GIT_TERMINAL_PROMPT="0")

    def run(args, timeout):
        return subprocess.run(command + args, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)

    try:
        # Status can invoke configured clean/process filters even when it only reads.
        # Keep such worktrees on branch-only display. Include conditional configs.
        filters = run(["config", "--includes", "--name-only", "--get-regexp",
                       r"^filter\..*\.(clean|process)$"], 0.10)
        if filters.returncode == 1:
            try:
                status = run(["status", "--porcelain=v2", "-z", "--branch",
                              "--ignore-submodules=all", "--untracked-files=normal"], 0.28)
                if status.returncode == 0 and len(status.stdout) <= 512 * 1024:
                    return git_summary(status.stdout, short)
            except subprocess.TimeoutExpired:
                pass
        head = run(["symbolic-ref", "--quiet", "--short", "HEAD"], 0.08)
        if head.returncode == 0:
            branch = compact(head.stdout.decode(errors="replace").rstrip(), 12 if short else 22)
        elif head.returncode == 1:
            head = run(["rev-parse", "--short", "--verify", "HEAD"], 0.08)
            if head.returncode:
                return []
            branch = "@" + compact(head.stdout.decode(errors="replace").rstrip(), 12)
        else:
            return []
        return [text("⎇ " + branch, "green")]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def git_summary(output, short=False):
    branch = oid = ""
    ahead = behind = staged = edited = new = conflicts = 0
    records = iter(output.split(b"\0"))
    for record in records:
        if record.startswith(b"# branch.head "):
            branch = record[14:].decode(errors="replace")
        elif record.startswith(b"# branch.oid "):
            oid = record[13:20].decode(errors="replace")
        elif record.startswith(b"# branch.ab "):
            ahead, behind = (abs(int(n)) for n in record.split()[2:])
        elif record.startswith((b"1 ", b"2 ")):
            staged += record[2:3] != b"."
            edited += record[3:4] != b"."
            if record.startswith(b"2 "):
                next(records, None)  # Original path of a rename or copy.
        elif record.startswith(b"? "):
            new += 1
        elif record.startswith(b"u "):
            conflicts += 1

    if not branch:
        return []
    branch_label = "@" + oid if branch == "(detached)" else compact(branch, 12 if short else 22)
    parts = [text("⎇ " + branch_label, "green")]
    sync = " ".join(label for count, label in [(ahead, f"↑{ahead}"), (behind, f"↓{behind}")] if count)
    if sync:
        parts.append(text(sync, "cyan"))
    for count, label, marker, color in [(conflicts, "conflict", "!", "red"),
                                        (staged, "staged", "+", "green"),
                                        (edited, "edited", "~", "yellow"),
                                        (new, "new", "?", "cyan")]:
        if count:
            value = f"{marker}{count}" if short else f"{count} {label}"
            parts.append(text(value, color, bold=label == "conflict"))
    return parts


def allowance_color(remaining):
    if remaining <= 10:
        return "red"
    if remaining <= 25:
        return "yellow"
    return "green"


def reset_countdown(timestamp):
    if timestamp is None:
        return ""
    minutes = math.ceil((timestamp - time.time()) / 60)
    if minutes <= 0:
        return ""
    days, hours = divmod(minutes // 60, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes % 60}m"
    return f"{minutes}m"


def allowances(account, short=False):
    limits = sorted(account.get("rate_limits") or [], key=lambda item: item.get("limit_id") != "codex")
    windows = {}
    for limit in limits:
        for key in ("primary_limit", "secondary_limit"):
            window = limit.get(key) or {}
            duration = window.get("window_minutes")
            if duration in (300, 10080) and window.get("used_percent") is not None:
                windows.setdefault(duration, (limit, window))

    priority = []
    detail = []
    for duration, label in [(10080, "week"), (300, "5h")]:
        if duration not in windows:
            if duration == 10080:
                priority.append(text("wk —" if short else "Week —", "status-metadata"))
            continue
        limit, window = windows[duration]
        # Keep small positive balances distinct from an exhausted limit.
        balance = max(0, min(100, 100 - window["used_percent"]))
        percentage = "<1%" if 0 < balance < 1 else f"{round(balance)}%"
        if duration == 10080:
            label = "wk" if short else "Week"
        if limit.get("limit_id") != "codex":
            name = compact(limit.get("limit_name") or limit.get("limit_id") or "", 15)
            label = f"{name} {label}".strip()
        if short:
            balance_field = text(f"{label} {percentage}", allowance_color(balance), bold=balance <= 10)
        else:
            fields = [text(label, "primary"), text(f"{percentage} left", allowance_color(balance),
                                                   bold=balance <= 10)]
            if duration == 10080 or balance <= 25:
                countdown = reset_countdown(window.get("resets_at"))
                if countdown:
                    fields.append(text(f"↺ {countdown}", "status-metadata"))
            balance_field = row(fields)
        if duration == 10080 or balance <= 25:
            priority.append(balance_field)
        else:
            detail.append(balance_field)
    return priority, detail


def render(state, short=None):
    """Return the UI tree for this resource read's thread."""
    thread = state.get("thread", {})
    model = thread.get("model", {})
    children = []
    if short is None:
        short = os.environ.get("MOSS_COMPACT") == "1"
    activity = thread.get("agent", {}).get("state")
    if activity in ACTIVITY:
        icon, color = ACTIVITY[activity]
        children.append(row([text(f" {icon} {activity.upper()} ", "black", True)],
                            gap=0, background=color))

    priority, limit_details = allowances(state.get("account") or {}, short)
    children.extend(priority)

    context = thread.get("usage", {}).get("context", {})
    used = context.get("used_percentage")
    if used is None and context.get("context_window_size", 0) > 0 and "used_tokens" in context:
        used = 100 * context["used_tokens"] / context["context_window_size"]
    if used is not None:
        remaining = max(0, min(100, round(100 - used)))
        color = allowance_color(remaining)
        if short:
            children.append(text(f"ctx {remaining}%", color))
        else:
            filled = round(remaining * 6 / 100)
            meter = row([text("━" * filled, color), text("┄" * (6 - filled), "status-metadata")], gap=0)
            children.append(row([text("Context", "primary"), text(f"{remaining}% left", color), meter]))

    if not short:
        children.extend(limit_details)
    if cwd := thread.get("workspace", {}).get("cwd"):
        path = Path(cwd)
        if path.is_relative_to(Path.home()):
            path = Path("~") / path.relative_to(Path.home())
        git = git_status(cwd, short)
        if not short or not git:
            where = (path.name or str(path)) if short else str(path)
            if short:
                children.append(text("⌂ " + compact(where, 12), "cyan"))
            else:
                children.append(row([text("│", "status-metadata"), text(compact(where, 30), "cyan")], gap=2))
        children.extend(git)
    if not short:
        label = compact(model.get("display_name") or model.get("id") or "Codex", 28)
        model_parts = [text(label, "primary", True)]
        if effort := model.get("reasoning_level"):
            model_parts.extend([text("· effort", "status-metadata"), text(compact(effort, 12).lower(), "cyan")])
        if tier := model.get("service_tier_display_name"):
            model_parts.extend([text("· tier", "status-metadata"), text(compact(tier, 16).lower(), "yellow")])
        children.append(row(model_parts))
    return row(children, gap=1 if short else 2, wrap="wrap")


STATE_KEY = "com.openai/codex-statusline-state"
MCP_VERSION = "2025-06-18"


def mcp_result(method, params):
    if method == "initialize":
        return {"serverInfo": {"name": "moss-statusline", "version": "1.0.0"},
                "capabilities": {"resources": {}, "tools": {}}, "protocolVersion": MCP_VERSION}
    if method == "resources/read" and params.get("uri") in PROVIDERS:
        uri = params["uri"]
        short = uri == COMPACT_URI or os.environ.get("MOSS_COMPACT") == "1"
        snapshot = params.get("_meta", {}).get(STATE_KEY, {})
        resource = {"uri": uri, "mimeType": "application/json",
                    "text": json.dumps(render(snapshot, short), ensure_ascii=False)}
        return {"contents": [resource]}
    if method == "tools/call":
        for uri, (name, _) in PROVIDERS.items():
            if params.get("name") == name:
                return {"content": [{"type": "text", "text": f"Moss renders the footer at {uri}."}]}
    if method in ("resources/read", "tools/call"):
        raise ValueError("Unknown resource or tool")
    discovery = {
        "tools/list": {"tools": advertised_tools()},
        "resources/list": {"resources": [
            {"uri": uri, "name": title, "mimeType": "application/json"}
            for uri, (_, title) in PROVIDERS.items()
        ]},
        "resources/templates/list": {"resourceTemplates": []},
        "ping": {},
    }
    if method not in discovery:
        raise LookupError("Unknown MCP method")
    return discovery[method]


def rpc(line):
    envelope = {"jsonrpc": "2.0", "id": None}
    try:
        request = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        envelope["error"] = {"code": -32700, "message": "Invalid JSON"}
        return envelope

    if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
            or not isinstance(request.get("method"), str)):
        envelope["error"] = {"code": -32600, "message": "Expected a JSON-RPC 2.0 request"}
        return envelope
    if "id" not in request:
        return None  # An MCP notification has no reply.
    envelope["id"] = request["id"]
    try:
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("Invalid parameters")
        envelope["result"] = mcp_result(request["method"], params)
    except LookupError:
        envelope["error"] = {"code": -32601, "message": "Method not found"}
    except (AttributeError, OverflowError, TypeError, ValueError):
        envelope["error"] = {"code": -32602, "message": "Invalid parameters"}
    return envelope


if __name__ == "__main__":
    for line in sys.stdin.buffer:
        reply = rpc(line)
        if reply is not None:
            # ASCII JSON on the wire works even on legacy terminal code pages.
            print(json.dumps(reply, separators=(",", ":")), flush=True)
