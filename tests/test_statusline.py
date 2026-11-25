"""Tests for the provider's stdio contract and adaptive layout."""

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
sys.path.insert(0, str(PROVIDER.parent))
from statusline import adaptive_render


STATE_KEY = "com.openai/codex-statusline-state"


def request(method, identifier=None, **params):
    message = {"jsonrpc": "2.0", "method": method, "params": params}
    if identifier is not None:
        message["id"] = identifier
    return message


def exchange(messages, env=None):
    wire = "".join(json.dumps(message) + "\n" for message in messages)
    env = {**os.environ, "MOSS_COMPACT": "0", **(env or {})}
    # Exercise the shared-server layout regardless of the test runner's console.
    detached = ({"creationflags": subprocess.DETACHED_PROCESS} if os.name == "nt"
                else {"start_new_session": True})
    server = subprocess.run(
        [sys.executable, "-u", str(PROVIDER)], input=wire.encode(),
        capture_output=True, check=True, timeout=5, env=env, **detached,
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


def account(weekly_used, five_hour_used, reset=None, weekly_reset=None):
    return {"rate_limits": [{
        "limit_id": "codex",
        "primary_limit": {"window_minutes": 300, "used_percent": five_hour_used, "resets_at": reset},
        "secondary_limit": {"window_minutes": 10080, "used_percent": weekly_used, "resets_at": weekly_reset},
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
        if len(tools) != 3 or len(set(endpoints.values())) != 3:
            raise AssertionError("Expected three providers with distinct resource URIs")
        cls.uri = endpoints["statusline"]
        compact, = [tool for tool in tools if "compact" in tool["title"].lower()]
        cls.compact_uri = endpoints[compact["name"]]
        adaptive, = [tool for tool in tools if tool["title"] == "Moss · adaptive"]
        cls.adaptive_uri = endpoints[adaptive["name"]]

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
        self.assertEqual({r["uri"] for r in listing["result"]["resources"]},
                         {self.uri, self.compact_uri, self.adaptive_uri})
        self.assertEqual(self.adaptive_uri, "ui://my-statusline/adaptive")
        self.assertIsInstance(error["error"]["code"], int)
        self.assertEqual(pong["result"], {})

    def test_each_read_uses_the_displayed_thread(self):
        before, after = map(display, self.render(
            {"thread": {"model": {"display_name": "Model One"}, "usage": {"context": {"used_percentage": 72}}}},
            {"thread": {"model": {"display_name": "Model Two"}, "usage": {"context": {}}}},
        ))
        self.assertIn("Model One", before)
        self.assertIn("Context 28% left", before)
        self.assertIn("Model Two", after)
        self.assertNotIn("Model One", after)
        self.assertNotIn("Context ", after)

    def test_estimated_usd_credits_and_unavailable_cost(self):
        cases = [
            ({"total_cost_usd": 2.75, "total_credits": 500}, "Est. cost $2.75", "~$2.75"),
            ({"total_cost_usd": 0}, "Est. cost $0.00", "~$0.00"),
            ({"total_cost_usd": 0.001}, "Est. cost <$0.01", "<$0.01"),
            ({"total_credits": 3.5}, "3.5 credits", "3.5cr"),
            ({"total_cost_usd": -1, "total_credits": 1}, "1 credit", "1cr"),
        ]
        for uri, expected_index in [(self.uri, 1), (self.compact_uri, 2)]:
            states = [{"thread": {"usage": {"cost": cost}}} for cost, _, _ in cases]
            replies = exchange(read_messages(uri, *states))
            for case, reply in zip(cases, replies):
                with self.subTest(uri=uri, cost=case[0]):
                    result = display(extract_tree(reply, uri))
                    self.assertIn(case[expected_index], result)
                    if "total_credits" in case[0] and case[0].get("total_cost_usd", -1) < 0:
                        self.assertNotIn("$", result)

    def test_thread_tokens_when_billing_is_unavailable(self):
        totals = {"total_input_tokens": 12000, "cache_read_input_tokens": 10000,
                  "other_input_tokens": 2000, "output_tokens": 345}
        cases = [
            (totals, "Thread tokens 12,345", "12.3k tok"),
            ({"total_input_tokens": 0, "output_tokens": 0}, "Thread tokens 0", "0 tok"),
            ({"total_input_tokens": 999950, "output_tokens": 49}, "Thread tokens 999,999", "999.9k tok"),
            ({"output_tokens": 10}, None, None),
            ({}, None, None),
        ]
        for uri, position in ((self.uri, 1), (self.compact_uri, 2), (self.adaptive_uri, 2)):
            states = [{"thread": {"usage": {"total": total, "context": {"used_tokens": 42}}}}
                      for total, _, _ in cases]
            for case, reply in zip(cases, exchange(read_messages(uri, *states))):
                with self.subTest(uri=uri, total=case[0]):
                    result = display(extract_tree(reply, uri))
                    self.assertNotIn("$", result)
                    self.assertNotIn("Cost", result)
                    if case[position] is None:
                        self.assertNotIn("tok", result.lower())
                    else:
                        self.assertIn(case[position], result)
        state = {"thread": {"usage": {"total": totals, "cost": {"total_cost_usd": 0}}}}
        full, = map(display, self.render(state))
        self.assertIn("Est. cost $0.00", full)
        self.assertNotIn("Thread tokens", full)

    def test_adaptive_budget_priority_and_reversible_resize(self):
        state = {
            "thread": {
                "agent": {"state": "working"},
                "model": {"display_name": "Garden Model", "reasoning_level": "high",
                          "service_tier_display_name": "Standard"},
                "usage": {"cost": {"total_cost_usd": 4.27}, "context": {"used_percentage": 42}},
            },
            "account": account(26, 96),
        }
        reply, = exchange(read_messages(self.adaptive_uri, state))
        self.assertIn("$4.27", display(extract_tree(reply, self.adaptive_uri)))

        unknown = display(adaptive_render(state, columns=None))
        prefix = ["$4.27", "wk 74%", "5h 4%", "ctx 58%", "Garden Model"]
        positions = [unknown.index(field) for field in prefix]
        self.assertEqual(positions, sorted(positions))

        counts = [42, 80, 160, 42]
        rendered = [display(adaptive_render(state, columns=columns)) for columns in counts]
        for columns, line in zip(counts, rendered):
            with self.subTest(columns=columns):
                self.assertLessEqual(len(line), columns - 4)
                for value in ("$4.27", "74%", "4%", "58%"):
                    self.assertIn(value, line)
        narrow, medium, wide, narrow_again = rendered
        self.assertNotIn("Garden Model", narrow)
        self.assertIn("Garden Model", medium)
        self.assertIn("Est. cost $4.27", wide)
        self.assertIn("effort high", wide)
        self.assertIn("tier standard", wide)
        self.assertEqual(narrow_again, narrow)

        state["thread"]["model"]["display_name"] = "竹" * 18
        wide_name = display(adaptive_render(state, columns=80))
        self.assertLessEqual(len(wide_name) + wide_name.count("竹"), 76)

    def test_full_footer_groups_reset_context_and_model_settings(self):
        state = {
            "account": account(35, 20, weekly_reset=time.time() + (3 * 24 + 9) * 3600),
            "thread": {
                "agent": {"state": "ready"},
                "usage": {"context": {"used_percentage": 38}},
                "model": {"display_name": "GPT-6.2-Astra", "reasoning_level": "ultra",
                          "service_tier_display_name": "Ultrafast"},
            },
        }
        summary, = map(display, self.render(state))
        self.assertRegex(summary, r"Week 65% left +↺ 3d 9h")
        self.assertRegex(summary, r"Context 62% left +━━━━┄┄")
        self.assertIn("effort ultra", summary)
        self.assertIn("tier ultrafast", summary)

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
        self.assertIn("Week 68% left", full)
        self.assertIn("Example Model", full)
        self.assertIn("wk 68% 5h 4% ctx 70%", compact)
        self.assertNotIn("Example Model", compact)
        self.assertEqual(full, full_again)

    def test_week_and_urgent_five_hour_limit_fit_a_narrow_front_section(self):
        now = time.time()
        state = {
            "account": account(32, 97, now + 47.5 * 60, now + 3 * 86400 + 9 * 3600),
            "thread": {"agent": {"state": "working"}, "model": {"display_name": "Example Model"},
                       "usage": {"context": {"context_window_size": 1000, "used_tokens": 300}}},
        }
        summary, = map(display, self.render(state))
        self.assertIn("Week 68% left", summary[:60])
        self.assertIn("5h 3% left", summary[:60])
        self.assertIn("Context 70% left", summary)
        self.assertRegex(summary, r"5h 3% left +↺ 48m")
        self.assertLess(summary.index("5h 3% left"), summary.index("Context 70% left"))

    def test_account_windows_with_missing_fractional_and_overdrawn_balances(self):
        state = {"account": account(99.6, 130, time.time() - 60)}
        other = {"limit_id": "other", "primary_limit": {"window_minutes": 10080, "used_percent": 80}}
        state["account"]["rate_limits"].insert(0, other)
        available, absent = map(display, self.render(state, {}))
        self.assertIn("Week <1% left", available)
        self.assertIn("5h 0% left", available)
        self.assertNotIn("↺", available)
        self.assertIn("Week —", absent)
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
