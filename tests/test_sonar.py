"""Behavior tests for scripts/sonar (stdlib unittest, no dependencies).

Each test builds its own repos, transcripts, and state dir under a tempdir, and
points CLAUDE_CONFIG_DIR and SONAR_STATE_DIR there, so nothing reads or writes
the developer's own ~/.claude.
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SONAR = REPO_ROOT / "scripts" / "sonar"


def load_sonar():
    loader = importlib.machinery.SourceFileLoader("sonar", str(SONAR))
    spec = importlib.util.spec_from_loader("sonar", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


sonar = load_sonar()

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, **GIT_ENV})


def make_repo(path: Path, files: dict[str, str], message: str = "init") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    for name, text in files.items():
        f = path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)
    return path


class SonarCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sonar-test-")).resolve()
        self.state = self.tmp / "state"
        self.config = self.tmp / "claude"
        self.env = mock.patch.dict(os.environ, {
            "SONAR_STATE_DIR": str(self.state),
            "CLAUDE_CONFIG_DIR": str(self.config),
            "CLAUDE_CODE_SESSION_ID": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])


class RedactTest(unittest.TestCase):
    def test_known_token_shapes_are_redacted(self):
        samples = [
            "key AKIAABCDEFGHIJKLMNOP here",
            "ghp_" + "a1B2" * 10,
            "glpat-" + "x" * 20,
            "xoxb-1234567890-abcdefghij",
            "sk-ant-api03-" + "Ab1" * 12,
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
            "Authorization: Bearer abcdefghijklmnop1234",
        ]
        for s in samples:
            with self.subTest(s=s):
                out = sonar.redact(s)
                self.assertIn(sonar.REDACTED, out)

    def test_key_value_keeps_the_key_and_drops_the_value(self):
        out = sonar.redact('db_password = "hunter2hunter2"')
        self.assertIn("db_password", out)
        self.assertNotIn("hunter2", out)

    def test_url_credentials_are_redacted(self):
        out = sonar.redact("clone https://bob:s3cretpw@example.com/repo.git")
        self.assertNotIn("s3cretpw", out)
        self.assertIn("example.com/repo.git", out)

    def test_private_key_withholds_the_whole_line(self):
        self.assertIn("withheld", sonar.redact("-----BEGIN RSA PRIVATE KEY-----"))

    def test_ordinary_text_shas_and_paths_survive(self):
        for s in ["commit 4f2a9c81d0e3b5a7f6c4d2e1b0a9f8e7d6c5b4a3 landed",
                  "see ~/.claude/projects/-Users-me-src-github-me-sonar/x.jsonl",
                  "the retry budget is three attempts"]:
            with self.subTest(s=s):
                self.assertEqual(sonar.redact(s), s)

    def test_query_strings_are_dropped_from_urls(self):
        self.assertEqual(sonar.strip_url("https://x.dev/a?token=abc#frag"), "https://x.dev/a")


class SnippetTest(unittest.TestCase):
    def test_snippet_is_capped_and_centered_on_the_term(self):
        line = "x " * 300 + "the retry budget" + " y" * 300
        out = sonar.snippet(line, ["retry"])
        self.assertLessEqual(len(out), sonar.SNIPPET_MAX + 2)
        self.assertIn("retry budget", out)
        self.assertTrue(out.startswith("…") and out.endswith("…"))

    def test_snippet_redacts_before_cutting(self):
        token = "ghp_" + "a1B2" * 10
        out = sonar.snippet("retry " + token + " z" * 200, ["retry"])
        self.assertNotIn("a1B2a1B2", out)


class TermsTest(unittest.TestCase):
    def test_short_terms_drop_when_longer_ones_exist(self):
        self.assertEqual(sonar.clean_terms(["a", "retry", "Retry", " budget  plan "]),
                         ["retry", "budget plan"])

    def test_a_lone_short_term_is_kept(self):
        self.assertEqual(sonar.clean_terms(["io"]), ["io"])


class ShallowTest(SonarCase):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp / "src" / "app", {
            "docs/guide.md": "intro\nthe retry budget is three attempts\n",
            "notes.txt": "a retry happened\n",
            "retry-budget.md": "nothing relevant inside\n",
            "other.txt": "unrelated\n",
        })

    def run_shallow(self, *terms, paths=()):
        return sonar.shallow(list(terms), self.repo, [Path(p) for p in paths])

    def test_ranks_lines_with_more_terms_and_names_with_terms_first(self):
        hits = self.run_shallow("retry", "budget")
        paths = [h["path"] for h in hits]
        self.assertEqual(paths[0], "retry-budget.md")
        self.assertEqual(paths[1], "docs/guide.md")
        self.assertIn("notes.txt", paths)
        self.assertNotIn("other.txt", paths)
        guide = hits[1]
        self.assertEqual(guide["line"], 2)
        self.assertIn("retry budget", guide["snippet"])

    def test_grep_fallback_finds_the_same_lines(self):
        sonar._RG_CACHE.clear()
        self.addCleanup(sonar._RG_CACHE.clear)
        with mock.patch.dict(os.environ, {"SONAR_NO_RG": "1"}):
            self.assertIsNone(sonar.ripgrep())
            hits = self.run_shallow("retry", "budget")
        self.assertEqual([h["path"] for h in hits][:2], ["retry-budget.md", "docs/guide.md"])

    def test_extra_paths_outside_the_repo_are_searched(self):
        outside = self.tmp / "elsewhere" / "plan.md"
        outside.parent.mkdir()
        outside.write_text("retry budget lives here\n")
        hits = self.run_shallow("retry", "budget", paths=[outside])
        self.assertTrue(any(h["abspath"] == str(outside) for h in hits))

    def test_secrets_in_matched_lines_are_redacted(self):
        (self.repo / "env.md").write_text("retry token=ghp_" + "a1B2" * 10 + "\n")
        hits = self.run_shallow("retry")
        env = next(h for h in hits if h["path"] == "env.md")
        self.assertNotIn("a1B2a1B2", env["snippet"])


class DeepSourcesTest(SonarCase):
    def setUp(self):
        super().setUp()
        parent = self.tmp / "src"
        self.outside = self.tmp / "notes" / "retry.md"
        self.outside.parent.mkdir(parents=True)
        self.outside.write_text("the retry budget, written down elsewhere\n")
        self.repo = make_repo(parent / "app", {
            "README.md": f"retry budget: see [notes]({os.path.relpath(self.outside, parent / 'app')})"
                         " and [gone](docs/missing.md) and "
                         "[spec](https://example.com/retry-budget?token=abc)\n",
        }, message="Add the retry budget")
        (self.repo / "README.md").write_text("cleared\n")
        git(self.repo, "commit", "-qam", "Clear the readme")
        (self.repo / "README.md").write_text(
            f"retry budget: see [notes]({os.path.relpath(self.outside, self.repo)})"
            " and [gone](docs/missing.md) and "
            "[spec](https://example.com/retry-budget?token=abc)\n")
        self.sibling = make_repo(parent / "lib", {"src/retry.py": "RETRY_BUDGET = 3  # retry budget\n",
                                                  "src/other.py": "retry once\n"})

    def test_dead_links_are_noticed(self):
        notices = sonar.dead_links([self.repo / "README.md"], self.repo)
        self.assertEqual(len(notices), 1)
        self.assertIn("docs/missing.md", notices[0])

    def test_one_hop_reaches_a_linked_file_outside_the_repo(self):
        hops = sonar.Hops(["retry", "budget"], [], self.repo, [self.repo / "README.md"],
                          set(), time.monotonic() + 10)
        hits = hops.hop1()
        self.assertEqual([h["abspath"] for h in hits], [str(self.outside)])
        self.assertEqual(hits[0]["detail"], "1 hop from README.md via 'retry', 'budget'")

    def test_links_are_listed_without_query_strings(self):
        hits = sonar.deep_links(["retry", "budget"], self.repo, [self.repo / "README.md"],
                                time.monotonic() + 10)
        self.assertEqual([h["snippet"] for h in hits], ["https://example.com/retry-budget"])

    def test_history_finds_messages_and_changes(self):
        hits = sonar.deep_history(["retry", "budget"], self.repo, time.monotonic() + 20)
        subjects = [h["snippet"] for h in hits]
        self.assertIn("Add the retry budget", subjects)
        self.assertIn("Clear the readme", subjects)  # removed the text: git log -S

    def test_siblings_need_two_terms(self):
        hits = sonar.deep_siblings(["retry", "budget"], self.repo, time.monotonic() + 20)
        self.assertEqual([h["path"] for h in hits], ["../lib/src/retry.py"])


class TranscriptTest(SonarCase):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp / "src" / "app", {"a.txt": "x\n"})
        d = self.config / "projects" / sonar.re.sub(r"[^A-Za-z0-9]", "-", str(self.repo))
        d.mkdir(parents=True)
        secret = "ghp_" + "a1B2" * 10
        past = [
            {"type": "user", "timestamp": "2026-10-01T10:00:00Z",
             "message": {"role": "user", "content": f"where is the retry budget doc? my token {secret}"}},
            {"type": "user", "timestamp": "2026-10-01T10:01:00Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "content": "retry budget in a tool result"}]}},
            {"type": "assistant", "timestamp": "2026-10-01T10:02:00Z",
             "message": {"role": "assistant", "content": [
                 {"type": "thinking", "thinking": "retry budget thoughts"},
                 {"type": "tool_use", "input": {"command": "rg retry budget"}}]}},
        ]
        (d / "past-session.jsonl").write_text("\n".join(json.dumps(r) for r in past) + "\n")
        now = {"type": "user", "timestamp": "2026-11-01T10:00:00Z",
               "message": {"role": "user", "content": "retry budget, this session"}}
        (d / "this-session.jsonl").write_text(json.dumps(now) + "\n")
        self.secret = secret

    def test_past_sessions_match_typed_text_only_and_skip_this_session(self):
        hits = sonar.deep_transcripts(["retry", "budget"], [self.repo], "this-session",
                                      time.monotonic() + 10)
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertTrue(h["path"].endswith("past-session.jsonl"))
        self.assertEqual(h["line"], 1)
        self.assertIn("claude --resume past-session", h["detail"])
        self.assertNotIn(self.secret, h["snippet"])
        self.assertIn(sonar.REDACTED, h["snippet"])

    def test_tool_output_and_thinking_are_not_searched(self):
        rec = {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "secret plan"},
            {"type": "tool_use", "input": {"x": "secret plan"}}]}}
        self.assertEqual(sonar.transcript_text(rec), "")


class LoadedFilesTest(SonarCase):
    SID = "loaded-session"

    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp / "src" / "app", {
            "a.md": "retry budget in the repo\n",
            "b.md": "retry budget in the repo too\n",
        })
        self.outside = self.tmp / "notes" / "plan.md"
        self.outside.parent.mkdir(parents=True)
        self.outside.write_text("retry budget in a file the session read\n")
        self.mentioned = self.repo / "mentioned.md"
        self.mentioned.write_text("nothing\n")
        self.not_loaded = self.tmp / "notes" / "secret-path.md"
        self.not_loaded.write_text("x\n")
        d = self.config / "projects" / "-anything"
        d.mkdir(parents=True)

        def tool(name, **inp):
            return {"type": "assistant", "cwd": str(self.repo), "message": {"content": [
                {"type": "tool_use", "name": name, "input": inp}]}}
        recs = [
            {"type": "user", "cwd": str(self.repo),
             "message": {"content": "look at @mentioned.md and mail me@example.com"}},
            tool("Read", file_path=str(self.outside)),
            tool("Edit", file_path=str(self.repo / "b.md"), old_string="x", new_string="y"),
            tool("Bash", command=f"cat {self.not_loaded}"),
            {"type": "user", "cwd": str(self.repo), "message": {"content": [
                {"type": "tool_result", "content": f"see {self.not_loaded}"}]}},
        ]
        (d / f"{self.SID}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")

    def test_collects_read_edit_and_mentions_newest_first_and_nothing_else(self):
        got = sonar.loaded_files(self.SID)
        self.assertEqual(got, [(self.repo / "b.md").resolve(), self.outside.resolve(),
                               self.mentioned.resolve()])

    def test_unknown_or_missing_session_loads_nothing(self):
        self.assertEqual(sonar.loaded_files("no-such-session"), [])
        self.assertEqual(sonar.loaded_files(None), [])

    def test_loaded_files_are_searched_and_win_ties(self):
        loaded = sonar.loaded_files(self.SID)
        hits = sonar.shallow(["retry", "budget"], self.repo, [], loaded=loaded)
        paths = [h["path"] for h in hits]
        self.assertLess(paths.index("b.md"), paths.index("a.md"))
        self.assertTrue(any(h["abspath"] == str(self.outside.resolve()) for h in hits))
        b = next(h for h in hits if h["path"] == "b.md")
        self.assertEqual(b["detail"], "loaded this session")

    def test_more_terms_elsewhere_still_beat_a_loaded_file(self):
        (self.repo / "c.md").write_text("retry budget backoff\n")
        loaded = [(self.repo / "b.md").resolve()]
        hits = sonar.shallow(["retry", "budget", "backoff"], self.repo, [], loaded=loaded)
        self.assertEqual(hits[0]["path"], "c.md")


class TopicTest(SonarCase):
    def setUp(self):
        super().setUp()
        files = {f"filler{i}.txt": "the cache config handles state and settings\n"
                 for i in range(25)}
        files.update({
            "docs/a.md": "The retry budget caps attempts.\nEach jitter window doubles.\n"
                         "cursor: pointer\n",
            "docs/b.md": "retry budget and jitter go together\njitter keeps clients apart\n",
            "docs/c.md": "token AKIAABCDEFGHIJKLMNOP retry budget\n",
        })
        self.repo = make_repo(self.tmp / "app", files)

    def test_expansion_picks_co_occurring_rare_words(self):
        files = [self.repo / "docs" / n for n in ("a.md", "b.md", "c.md")]
        words = sonar.expand_terms(["retry", "budget"], files, self.repo, time.monotonic() + 10)
        self.assertEqual(words[0], "jitter")
        for w in words:
            self.assertNotIn(w, ("retry", "budget", "the", "cache", "cursor"))
            self.assertNotIn("akia", w)

    def test_a_topic_word_never_counts_alone(self):
        lines = [(str(self.repo / "x.md"), 1, "jitter jitter"),
                 (str(self.repo / "y.md"), 1, "retry with jitter")]
        hits = sonar.rank_files(lines, ["retry", "budget"], self.repo, "linked",
                                min_terms=2, expanded=["jitter"])
        self.assertEqual([h["path"] for h in hits], ["y.md"])
        self.assertEqual(hits[0]["terms"], ["retry", "jitter"])


class ReferencesTest(SonarCase):
    def test_links_imports_mentions_and_python_modules(self):
        repo = make_repo(self.tmp / "app", {
            "docs/guide.md": "x\n", "src/util.ts": "x\n", "pkg/__init__.py": "",
            "pkg/helpers.py": "x\n", "lib/inc.h": "x\n", "notes.txt": "x\n",
        })
        src = repo / "src" / "main.ts"
        src.write_text('import { a } from "./util"\nsee [guide](../docs/guide.md)\n'
                       'and `notes.txt` and "lib/inc.h" and https://example.com/x.md\n')
        got = {p.relative_to(repo.resolve()).as_posix() for p, _, _ in sonar.references(src, repo)}
        self.assertEqual(got, {"src/util.ts", "docs/guide.md", "notes.txt", "lib/inc.h"})
        py = repo / "pkg" / "main.py"
        py.write_text("from .helpers import go\nimport os\n")
        got = [p.name for p, _, _ in sonar.references(py, repo)]
        self.assertEqual(got, ["helpers.py"])

    def test_a_path_in_another_repo_does_not_resolve_against_this_one(self):
        repo = make_repo(self.tmp / "app", {"plugin.yml": "x\n"})
        other = self.tmp / "other" / "notes.md"
        other.parent.mkdir()
        other.write_text("see `plugin.yml`\n")
        self.assertEqual(sonar.references(other, repo), [])


class HopsTest(SonarCase):
    def test_two_hops_are_labeled_with_how_they_were_reached(self):
        repo = make_repo(self.tmp / "app", {
            "README.md": "retry budget: see [a](docs/a.md)\n",
            "docs/a.md": "retry notes, see [b](b.md) and [c](c.md)\n",
            "docs/b.md": "the retry budget, in full\n",
            "docs/c.md": "jitter only\n",
        })
        hops = sonar.Hops(["retry", "budget"], ["jitter"], repo, [repo / "README.md"],
                          set(), time.monotonic() + 10)
        first = hops.hop1()
        self.assertEqual([h["path"] for h in first], [])  # a.md carries one term
        hops.best = [(repo / "docs" / "a.md").resolve()]
        hops.origin[(repo / "docs" / "a.md").resolve()] = (1, repo / "README.md", None)
        second = hops.hop2()
        self.assertEqual([h["path"] for h in second], ["docs/b.md"])
        self.assertEqual(second[0]["detail"],
                         "2 hops from README.md through docs/a.md via 'retry', 'budget'")

    def test_a_topic_word_can_be_the_second_term(self):
        repo = make_repo(self.tmp / "app", {
            "README.md": "retry budget: see [a](docs/a.md)\n",
            "docs/a.md": "retry with jitter\n",
        })
        hops = sonar.Hops(["retry", "budget"], ["jitter"], repo, [repo / "README.md"],
                          set(), time.monotonic() + 10)
        hits = hops.hop1()
        self.assertEqual(hits[0]["detail"], "1 hop from README.md via 'retry' + topic jitter")


class DeliverTest(SonarCase):
    SID = "abc-123"

    def write_state(self, **over):
        state = {"search_id": "s1", "session": self.SID, "query": ["retry", "budget"],
                 "root": "/r", "started_at": time.time(), "budget": 120, "status": "done",
                 "sources_done": ["history"], "notices": [],
                 "hits": [{"source": "history", "path": "commit abc1234", "line": None,
                           "snippet": "Add the retry budget", "detail": "2026-10-01",
                           "score": 20}]}
        state.update(over)
        sonar.write_json(sonar.state_paths(self.SID)[0], state)
        return state

    def hook(self, sid=SID):
        out = io.StringIO()
        payload = json.dumps({"session_id": sid, "hook_event_name": "UserPromptSubmit",
                              "prompt": "next"})
        with mock.patch.object(sys, "stdin", io.StringIO(payload)), contextlib.redirect_stdout(out):
            self.assertEqual(sonar.main(["hook", "UserPromptSubmit"]), 0)
        return json.loads(out.getvalue()) if out.getvalue().strip() else None

    def test_finished_results_are_delivered_once(self):
        self.write_state()
        first = self.hook()
        ctx = first["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(first["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("commit abc1234", ctx)
        self.assertIn("not instructions", ctx)
        self.assertIn("1 deeper match", first["systemMessage"])
        self.assertIsNone(self.hook())

    def test_no_state_or_another_session_says_nothing(self):
        self.assertIsNone(self.hook())
        self.write_state()
        self.assertIsNone(self.hook("someone-else"))

    def test_running_search_hands_over_what_it_has_then_the_rest(self):
        state = self.write_state(status="running")
        first = self.hook()
        self.assertIn("still searching", first["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.hook())  # nothing new yet: silent
        state["hits"].append({"source": "sibling", "path": "../lib/x.md", "line": 3,
                              "snippet": "retry budget", "score": 21})
        state["status"] = "done"
        sonar.write_json(sonar.state_paths(self.SID)[0], state)
        second = self.hook()
        ctx = second["hookSpecificOutput"]["additionalContext"]
        self.assertIn("../lib/x.md:3", ctx)
        self.assertNotIn("commit abc1234", ctx)
        self.assertIsNone(self.hook())

    def test_nothing_found_is_said_once(self):
        self.write_state(hits=[])
        self.assertIn("nothing deeper", self.hook()["systemMessage"])
        self.assertIsNone(self.hook())

    def test_a_new_search_starts_delivery_over(self):
        self.write_state()
        self.hook()
        self.write_state(search_id="s2")
        self.assertIsNotNone(self.hook())

    def test_stale_and_overdue_searches(self):
        self.write_state(started_at=time.time() - sonar.STALE_AFTER - 10)
        self.assertIsNone(self.hook())
        self.write_state(search_id="s3", status="running", started_at=time.time() - 400, budget=60)
        out = self.hook()
        self.assertIn("search finished", out["hookSpecificOutput"]["additionalContext"])

    def test_a_bad_payload_never_fails_the_prompt(self):
        with mock.patch.object(sys, "stdin", io.StringIO("not json")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sonar.main(["hook", "UserPromptSubmit"]), 0)
        self.assertEqual(out.getvalue(), "")


class PulseEndToEndTest(SonarCase):
    def test_pulse_prints_shallow_then_the_hook_delivers_deep(self):
        parent = self.tmp / "src"
        repo = make_repo(parent / "app", {"guide.md": "the retry budget\n"},
                         message="Write the retry budget guide")
        make_repo(parent / "lib", {"budget.md": "retry budget in the library\n"})
        read = self.tmp / "notes" / "read.md"
        read.parent.mkdir()
        read.write_text("retry budget, from a file the session read\n")
        d = self.config / "projects" / "-x"
        d.mkdir(parents=True)
        (d / "e2e-session.jsonl").write_text(json.dumps(
            {"type": "assistant", "cwd": str(repo), "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": str(read)}}]}}) + "\n")
        env = {**os.environ, "CLAUDE_CODE_SESSION_ID": "e2e-session"}
        out = subprocess.run([sys.executable, str(SONAR), "pulse", "--budget", "30", "--",
                              "retry", "budget"], cwd=repo, env=env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("guide.md:1", out.stdout)
        self.assertIn("the 1 file this session loaded", out.stdout)
        self.assertIn("read.md:1 [loaded this session]", out.stdout)
        self.assertIn("arrives with your next prompt", out.stdout)

        results = self.state / "e2e-session.json"
        deadline = time.time() + 30
        while time.time() < deadline:
            state = sonar.read_json(results) or {}
            if state.get("status") == "done":
                break
            time.sleep(0.2)
        self.assertEqual(state.get("status"), "done", state)

        hook = subprocess.run([sys.executable, str(SONAR), "hook", "UserPromptSubmit"],
                              input=json.dumps({"session_id": "e2e-session"}), env=env,
                              capture_output=True, text=True, timeout=10)
        ctx = json.loads(hook.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("../lib/budget.md:1", ctx)
        self.assertIn("Write the retry budget guide", ctx)


class CliTest(SonarCase):
    def test_version_reads_plugin_json(self):
        want = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
        out = subprocess.run([sys.executable, str(SONAR), "--version"],
                             capture_output=True, text=True)
        self.assertTrue(out.stdout.startswith(f"sonar {want}"), out.stdout)

    def test_no_args_prints_help_and_fails(self):
        out = subprocess.run([sys.executable, str(SONAR)], capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("pulse", out.stderr)

    def test_state_dir_follows_the_marketplace_install(self):
        cache = self.config / "plugins" / "cache" / "acme" / "sonar" / "0.1.0"
        cache.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"SONAR_STATE_DIR": ""}), \
                mock.patch.object(sonar, "PKG_ROOT", cache.resolve()):
            self.assertEqual(sonar.state_dir(),
                             self.config / "plugins" / "data" / "sonar-acme" / "pulse")
        with mock.patch.dict(os.environ, {"SONAR_STATE_DIR": ""}):
            self.assertEqual(sonar.state_dir().parent.name, "sonar-inline")

    def test_pulse_without_a_session_skips_the_deep_pass(self):
        repo = make_repo(self.tmp / "app", {"a.md": "retry budget\n"})
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        out = subprocess.run([sys.executable, str(SONAR), "pulse", "retry"], cwd=repo,
                             env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("deeper search not started", out.stdout)


if __name__ == "__main__":
    unittest.main()
