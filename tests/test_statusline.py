"""Black-box tests for the installed provider's stdio contract."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


PROVIDER = Path(__file__).resolve().parents[1] / "statusline.py"
STATE_KEY = "com.openai/codex-statusline-state"


def request(method, identifier=None, **params):
    message = {"jsonrpc": "2.0", "method": method, "params": params}
    if identifier is not None:
        message["id"] = identifier
    return message


def exchange(messages, env=None):
    wire = "".join(json.dumps(message) + "\n" for message in messages)
    env = {**os.environ, "MOSS_COMPACT": "0", **(env or {})}
    server = subprocess.run(
        [sys.executable, "-u", str(PROVIDER)], input=wire.encode(),
        capture_output=True, check=True, timeout=5, env=env,
    )
    return [json.loads(line) for line in server.stdout.splitlines()]


def read_messages(uri, *snapshots):
    return [
        request("resources/read", n, uri=uri, _meta={STATE_KEY: state})
        for n, state in enumerate(snapshots, 1)
    ]


def walk_tree(node, depth=0):
    if node is None:
        return
    yield node, depth
    if isinstance(node, dict):
        for child in node["children"]:
            yield from walk_tree(child, depth + 1)


def extract_tree(reply, uri):
    resource, = reply["result"]["contents"]
    if resource["uri"] != uri or resource["mimeType"] != "application/json":
        raise AssertionError("The resource did not match the advertised JSON endpoint")
    return json.loads(resource["text"])


def display(node):
    if isinstance(node, str):
        return node
    gap = " " * node["props"]["gap"] if node["type"] == "row" else ""
    return gap.join(display(child) for child in node["children"] if child is not None)


def account(weekly_used, five_hour_used, reset=None):
    return {"rate_limits": [{
        "limit_id": "codex",
        "primary_limit": {"window_minutes": 300, "used_percent": five_hour_used, "resets_at": reset},
        "secondary_limit": {"window_minutes": 10080, "used_percent": weekly_used},
    }]}


class StatuslineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        replies = exchange([request("tools/list", 1)])
        tools = replies[0]["result"]["tools"]
        endpoints = {}
        for tool in tools:
            entry, = tool["_meta"]["openai/ui"]["entrypoints"]
            if entry["type"] != "statusline":
                raise AssertionError("Expected a statusline entrypoint")
            endpoints[tool["name"]] = entry["resourceUri"]
        if len(tools) != 2 or len(set(endpoints.values())) != 2:
            raise AssertionError("Expected two providers with distinct resource URIs")
        cls.uri = endpoints["statusline"]
        compact, = [tool for tool in tools if "compact" in tool["title"].lower()]
        cls.compact_uri = endpoints[compact["name"]]

    def render(self, *snapshots, env=None):
        replies = exchange(read_messages(self.uri, *snapshots), env)
        self.assertEqual(len(replies), len(snapshots))
        return [extract_tree(reply, self.uri) for reply in replies]

    def test_handshake_notifications_and_resource_errors(self):
        messages = [
            request("initialize", "init", protocolVersion="2025-06-18",
                    capabilities={}, clientInfo={"name": "moss-test", "version": "1"}),
            request("notifications/initialized"),
            request("resources/list", 0),
            request("resources/read", "bad", uri="ui://unknown/footer"),
            request("ping", "ok"),
        ]
        hello, listing, error, pong = exchange(messages)
        self.assertEqual([reply["id"] for reply in (hello, listing, error, pong)], ["init", 0, "bad", "ok"])
        self.assertEqual(hello["result"]["protocolVersion"], "2025-06-18")
        self.assertTrue({"resources", "tools"}.issubset(hello["result"]["capabilities"]))
        self.assertEqual({r["uri"] for r in listing["result"]["resources"]}, {self.uri, self.compact_uri})
        self.assertIsInstance(error["error"]["code"], int)
        self.assertEqual(pong["result"], {})

    def test_each_read_uses_the_displayed_thread(self):
        before, after = map(display, self.render(
            {"thread": {"model": {"display_name": "Model One"}, "usage": {"context": {"used_percentage": 72}}}},
            {"thread": {"model": {"display_name": "Model Two"}, "usage": {"context": {}}}},
        ))
        self.assertIn("Model One", before)
        self.assertIn("28% ctx left", before)
        self.assertIn("Model Two", after)
        self.assertNotIn("Model One", after)
        self.assertNotIn("ctx left", after)

    def test_picker_layout_is_selected_per_resource_read(self):
        state = {"thread": {"model": {"display_name": "Example Model"},
                            "usage": {"context": {"used_percentage": 30}}},
                 "account": account(32, 96)}
        uris = [self.uri, self.compact_uri, self.uri]
        reads = [request("resources/read", i, uri=uri, _meta={STATE_KEY: state})
                 for i, uri in enumerate(uris)]
        replies = exchange(reads)
        self.assertEqual([reply["id"] for reply in replies], [0, 1, 2])
        full, compact, full_again = [display(extract_tree(reply, uri)) for reply, uri in zip(replies, uris)]
        self.assertIn("68% week left", full)
        self.assertIn("Example Model", full)
        self.assertIn("wk 68% 5h 4% ctx 70%", compact)
        self.assertNotIn("Example Model", compact)
        self.assertEqual(full, full_again)

    def test_week_and_urgent_five_hour_limit_fit_a_narrow_front_section(self):
        now = time.time()
        state = {
            "account": account(32, 97, now + 47.5 * 60),
            "thread": {"agent": {"state": "working"}, "model": {"display_name": "Example Model"},
                       "usage": {"context": {"context_window_size": 1000, "used_tokens": 300}}},
        }
        summary, = map(display, self.render(state))
        self.assertIn("68% week left", summary[:60])
        self.assertIn("3% 5h left", summary[:60])
        self.assertIn("70% ctx left", summary)
        self.assertIn("5h in 48m", summary)
        self.assertLess(summary.index("3% 5h left"), summary.index("70% ctx left"))

    def test_account_windows_with_missing_fractional_and_overdrawn_balances(self):
        state = {"account": account(99.6, 130, time.time() - 60)}
        other = {"limit_id": "other", "primary_limit": {"window_minutes": 10080, "used_percent": 80}}
        state["account"]["rate_limits"].insert(0, other)
        available, absent = map(display, self.render(state, {}))
        self.assertIn("<1% week left", available)
        self.assertIn("0% 5h left", available)
        self.assertNotIn("5h in", available)
        self.assertIn("week —", absent)
        self.assertNotIn("%", absent)

    def test_compact_layout_fits_common_fields_in_a_48_column_split(self):
        state = {"thread": {"agent": {"state": "working"},
                            "model": {"display_name": "Example Model"},
                            "usage": {"context": {"used_percentage": 30}}},
                 "account": account(32, 96, time.time() + 3600)}
        with tempfile.TemporaryDirectory() as parent:
            workspace = Path(parent) / "garden"
            workspace.mkdir()
            state["thread"]["workspace"] = {"cwd": str(workspace)}
            sample, = map(display, self.render(state, env={"MOSS_COMPACT": "1", "PATH": ""}))
            self.assertIn("wk 68% 5h 4% ctx 70%", sample)
            self.assertIn("garden", sample)
            self.assertLessEqual(len(sample), 48)
            self.assertNotIn("Example Model", sample)
            self.assertNotIn("↺", sample)
            state["account"] = account(32, 10)
            healthy, = map(display, self.render(state, env={"MOSS_COMPACT": "1", "PATH": ""}))
            self.assertNotIn("5h", healthy)

    def test_ui_tree_limits_and_terminal_control_characters(self):
        noisy_name = "𐐀" * 20000 + "\x1b[31m\nmodel"
        state = {"thread": {"model": {"display_name": noisy_name, "reasoning_level": noisy_name,
                                      "service_tier_display_name": noisy_name}}}
        tree, = self.render(state)
        nodes = list(walk_tree(tree))
        leaves = [node for node, _ in nodes if isinstance(node, str)]
        self.assertLessEqual(len(json.dumps(tree, ensure_ascii=False).encode()), 64 * 1024)
        self.assertLessEqual(sum(len(leaf.encode()) for leaf in leaves), 16 * 1024)
        self.assertLessEqual(len(nodes), 256)
        self.assertLessEqual(max(depth for _, depth in nodes), 16)
        self.assertTrue(all(leaf.isprintable() for leaf in leaves))

    @unittest.skipUnless(shutil.which("git"), "Git integration requires Git")
    def test_real_worktree_and_optional_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "project"
            worktree.mkdir()
            env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "MOSS_COMPACT": "0"}

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=worktree, env=env, stderr=subprocess.STDOUT)

            git("init", "-q", "-b", "topic")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            (worktree / "notes").write_text("base\n")
            git("add", "notes")
            git("commit", "-qm", "base")
            git("branch", "base")
            git("branch", "--set-upstream-to=base", "topic")
            git("commit", "--allow-empty", "-qm", "advance topic")
            git("mv", "notes", "renamed")
            (worktree / "renamed").write_text("edited since rename\n")
            (worktree / "extra").write_text("new\n")
            state = {"thread": {"workspace": {"cwd": str(worktree)}}}
            summary, = map(display, self.render(state, env=env))
            for piece in ("topic", "↑1", "1 staged", "1 edited", "1 new"):
                self.assertIn(piece, summary)
            short, = map(display, self.render(state, env={**env, "MOSS_COMPACT": "1"}))
            self.assertIn("⎇ topic ↑1 +1 ~1 ?1", short)

            git("config", "filter.example.clean", "cat")
            filtered, = map(display, self.render(state, env=env))
            self.assertIn("topic", filtered)
            self.assertNotIn("1 staged", filtered)
            git("config", "--unset", "filter.example.clean")

            git("checkout", "-q", "--detach")
            detached, = map(display, self.render(state, env=env))
            self.assertIn(git("rev-parse", "HEAD").decode().strip()[:7], detached)

            no_git, = map(display, self.render(state, env={**env, "PATH": str(root / "empty")}))
            self.assertIn("project", no_git)
            self.assertNotIn("1 staged", no_git)
            self.assertNotIn("1 edited", no_git)


if __name__ == "__main__":
    unittest.main()
