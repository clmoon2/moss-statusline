"""Preview Moss with fictional Codex snapshots. Requires only Python."""
import argparse
import os
import shutil
import sys
import time
from unittest.mock import patch

import statusline


SCENES = {
    "split": ("A 60-column split", "Thread cost, allowances and context stay visible together."),
    "ready": ("Ready to work", "Weekly allowance and context stay up front."),
    "limits": ("Running low", "At 25% or less, the five-hour balance moves up front."),
    "waiting": ("Waiting for input", "The branch shows local changes and commits ahead of upstream."),
}

ANSI = {
    "primary": 39, "status-metadata": 90, "black": 30,
    "green": 32, "yellow": 33, "red": 31, "cyan": 36,
}


def sample_tree(scene, columns=None):
    activity, short_usage, context_usage = {
        "ready": ("ready", 16, 24),
        "limits": ("working", 94, 52),
        "waiting": ("waiting", 18, 78),
        "split": ("thinking", 82, 46),
    }[scene]
    now = int(time.time())
    state = {
        "account": {"rate_limits": [{
            "limit_id": "codex",
            "primary_limit": {"window_minutes": 300, "used_percent": short_usage, "resets_at": now + 2520},
            "secondary_limit": {"window_minutes": 10080, "used_percent": 27, "resets_at": now + 223200},
        }]},
        "thread": {
            "agent": {"state": activity},
            "model": {"display_name": "Codex", "reasoning_level": "high"},
            "workspace": {"cwd": "/work/fern"},
            "usage": {"context": {"used_percentage": context_usage},
                      "cost": {"total_cost_usd": 1.84}},
        },
    }
    if scene == "ready":
        state["thread"]["usage"].pop("cost")
        state["thread"]["usage"]["total"] = {"total_input_tokens": 11200, "output_tokens": 1145}
    records = b"# branch.head main\0"
    if scene == "waiting":
        records = (b"# branch.head fix/cache\0# branch.ab +2 -0\0"
                   b"1 M. N... 100644 100644 100644 a b cache.py\0? cache_test.py\0")
    width = min(columns or 60, 60) if scene == "split" else columns or 180
    with patch.object(statusline, "git_status", side_effect=lambda cwd, short: statusline.git_summary(records, short)):
        return statusline.adaptive_render(state, width)


def spans(node, inherited=None):
    """Flatten one field of Moss's UI tree, retaining styles and row spacing."""
    style = dict(inherited or {})
    if isinstance(node, str):
        return [(node, style)]
    props = node.get("props", {})
    style.update({k: props[k] for k in ("color", "weight", "background") if k in props})
    result = []
    for i, child in enumerate(node["children"]):
        if i and node["type"] == "row":
            result.append((" " * props["gap"], style))
        result.extend(spans(child, style))
    return result


def lines(tree, columns):
    """Wrap between whole fields so every part of the preview can be read."""
    gap = " " * tree["props"]["gap"]
    line = []
    used = 0
    for node in tree["children"]:
        field = spans(node)
        size = sum(len(value) for value, _ in field)
        if line and used + len(gap) + size > columns:
            yield line
            line, used = [], 0
        if line:
            line.append((gap, {}))
            used += len(gap)
        line.extend(field)
        used += size
    if line:
        yield line


def colored(value, style):
    codes = [ANSI.get(style.get("color"), 39)]
    if style.get("weight") == "bold":
        codes.append(1)
    if style.get("background") in ANSI:
        codes.append(ANSI[style["background"]] + 10)
    return "\033[" + ";".join(map(str, codes)) + "m" + value + "\033[0m"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", choices=SCENES, help="Show just one sample.")
    args = parser.parse_args()
    width = shutil.get_terminal_size(fallback=(84, 24)).columns
    colors = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.getenv("TERM") != "dumb"
    for scene in ([args.scene] if args.scene else SCENES):
        title, _ = SCENES[scene]
        print(f"{title} (sample)")
        for line in lines(sample_tree(scene, width), width):
            print("".join(colored(value, style) if colors else value for value, style in line))
        print()


if __name__ == "__main__":
    main()
