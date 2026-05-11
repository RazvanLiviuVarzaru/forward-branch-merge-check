import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_script(name: str):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


check = load_script("check_forward_mergeability")
send = load_script("send_chain_notification")
update_var = load_script("update_github_variable")


def quietly(func, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


def run(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.strip()


def git(repo: Path, *args: str) -> str:
    return run(["git", *args], repo)


class GitRepo:
    def __init__(self, path: Path):
        self.path = path
        run(["git", "init", "--initial-branch=master", str(path)], Path.cwd())
        git(path, "config", "user.name", "Tester")
        git(path, "config", "user.email", "tester@example.invalid")

    def write(self, relative: str, content: str) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        git(self.path, "add", ".")
        git(self.path, "commit", "-m", message)
        return git(self.path, "rev-parse", "HEAD")

    def checkout(self, branch: str, start: str | None = None) -> None:
        args = ["checkout", "-B", branch]
        if start:
            args.append(start)
        git(self.path, *args)

    def expose_origin_ref(self, branch: str) -> str:
        git(self.path, "update-ref", f"refs/remotes/origin/{branch}", branch)
        return git(self.path, "rev-parse", branch)


def make_clean_chain(root: Path) -> GitRepo:
    repo = GitRepo(root / "repo")
    repo.write("file1.txt", "base\n")
    repo.write("file2.txt", "base\n")
    repo.write("file3.txt", "base\n")
    repo.commit("base")

    repo.checkout("old", "master")
    repo.write("file1.txt", "old\n")
    repo.commit("old file1")

    repo.checkout("next", "master")
    repo.write("file2.txt", "next\n")
    repo.commit("next file2")

    repo.checkout("main", "master")
    repo.write("file3.txt", "main\n")
    repo.commit("main file3")

    for branch in ["old", "next", "main"]:
        repo.expose_origin_ref(branch)

    return repo


def make_conflicting_chain(root: Path) -> tuple[GitRepo, str]:
    repo = GitRepo(root / "repo")
    repo.write("file.txt", "base\n")
    repo.commit("base")

    repo.checkout("old", "master")
    repo.write("file.txt", "old\n")
    old_sha = repo.commit("old changes line")

    repo.checkout("next", "master")
    repo.write("file.txt", "next\n")
    repo.commit("next changes line")

    repo.checkout("main", "next")

    for branch in ["old", "next", "main"]:
        repo.expose_origin_ref(branch)

    return repo, old_sha


def make_downstream_broken_chain(root: Path) -> GitRepo:
    repo = GitRepo(root / "repo")
    repo.write("old-only.txt", "base\n")
    repo.write("mid-only.txt", "base\n")
    repo.write("shared-with-later.txt", "base\n")
    repo.write("pr-only.txt", "base\n")
    repo.commit("base")

    repo.checkout("old", "master")
    repo.write("old-only.txt", "old\n")
    repo.commit("old changes")

    repo.checkout("mid", "master")
    repo.write("mid-only.txt", "mid\n")
    repo.commit("mid changes")

    repo.checkout("next", "master")
    repo.write("shared-with-later.txt", "next\n")
    repo.commit("next changes")

    repo.checkout("later", "master")
    repo.write("shared-with-later.txt", "later\n")
    repo.commit("later changes")

    repo.checkout("pr", "mid")
    repo.write("pr-only.txt", "pr\n")
    repo.commit("pr changes")

    for branch in ["old", "mid", "next", "later"]:
        repo.expose_origin_ref(branch)

    return repo


class ConfigTests(unittest.TestCase):
    def test_loads_chain_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "chain.yml"
            config.write_text(
                """
base_branch: "old"
branches:
  - "old"
  - "next"
  - "main"
notifications:
  slack:
    enabled: false
  zulip:
    enabled: true
""".lstrip(),
                encoding="utf-8",
            )

            loaded = check.load_config(config)

        self.assertEqual(loaded["base_branch"], "old")
        self.assertEqual(loaded["branches"], ["old", "next", "main"])
        self.assertEqual(loaded["notifications"]["slack"]["enabled"], False)
        self.assertEqual(loaded["notifications"]["zulip"]["enabled"], True)

    def test_rejects_duplicate_branch_list(self):
        args = argparse.Namespace(
            branches=["old", "old"],
            branch_file=None,
            config_file=ROOT / "forward-merge-chain.yml",
        )

        with self.assertRaisesRegex(ValueError, "duplicates"):
            check.load_branches(args)


class MergeCheckTests(unittest.TestCase):
    def test_missing_branch_refs_reports_fetch_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_clean_chain(Path(tmp))

            with self.assertRaisesRegex(ValueError, "refs/remotes/origin/missing"):
                check.ensure_branch_refs(repo.path, ["old", "missing"])

    def test_chain_health_clean_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_clean_chain(Path(tmp))
            scratch = Path(tmp) / "scratch"
            scratch.mkdir()

            results = quietly(
                check.run_chain_health_mode,
                repo.path,
                ["old", "next", "main"],
                "old",
                scratch,
            )

        self.assertEqual([result.status for result in results], ["merge_ok", "merge_ok"])

    def test_chain_health_conflict_reports_likely_commit_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, old_sha = make_conflicting_chain(Path(tmp))
            scratch = Path(tmp) / "scratch"
            scratch.mkdir()

            results = quietly(
                check.run_chain_health_mode,
                repo.path,
                ["old", "next", "main"],
                "old",
                scratch,
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].status, "conflict")
        self.assertEqual(results[0].conflicted_files, ["file.txt"])
        self.assertIsNotNone(results[0].first_conflicting_commit)
        self.assertEqual(results[0].first_conflicting_commit.sha, old_sha)
        self.assertEqual(results[1].source_label, "next")
        self.assertEqual(results[1].target, "main")
        self.assertEqual(results[1].status, "nothing_to_merge")

    def test_pr_mode_stops_before_preexisting_downstream_break(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_downstream_broken_chain(Path(tmp))
            scratch = Path(tmp) / "scratch"
            scratch.mkdir()

            results = quietly(
                check.run_pr_mode,
                repo.path,
                ["old", "mid", "next", "later"],
                "mid",
                "pr",
                scratch,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].target, "next")
        self.assertEqual(results[0].status, "merge_ok")

    def test_pr_mode_skips_unconfigured_base_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_clean_chain(Path(tmp))
            scratch = Path(tmp) / "scratch"
            scratch.mkdir()

            results = quietly(check.run_pr_mode, repo.path, ["old", "next"], "other", "HEAD", scratch)

        self.assertEqual(results, [])


class StateTests(unittest.TestCase):
    def test_first_broken_run_notifies_and_duplicate_is_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _old_sha = make_conflicting_chain(Path(tmp))
            scratch = Path(tmp) / "scratch"
            scratch.mkdir()
            results = quietly(
                check.run_chain_health_mode,
                repo.path,
                ["old", "next", "main"],
                "old",
                scratch,
            )
            state = check.current_state(repo.path, ["old", "next", "main"], "old", results)

        self.assertEqual(
            check.notification_reasons(None, state),
            [check.NotificationReason.FIRST_RUN],
        )
        self.assertEqual(check.notification_reasons(check.compact_state(state), state), [])

    def test_resolution_and_health_change_reasons(self):
        previous_broken = {
            "status": "broken",
            "config_fingerprint": "cfg",
            "chain_fingerprint": "chain-a",
            "health_fingerprint": "health-a",
        }
        resolved = {
            "status": "healthy",
            "config_fingerprint": "cfg",
            "chain_fingerprint": "chain-b",
            "health_fingerprint": "health-b",
        }
        changed_broken = {
            "status": "broken",
            "config_fingerprint": "cfg",
            "chain_fingerprint": "chain-b",
            "health_fingerprint": "health-b",
        }

        self.assertIn(
            check.NotificationReason.RESOLVED,
            check.notification_reasons(previous_broken, resolved),
        )
        self.assertIn(
            check.NotificationReason.HEALTH_CHANGED,
            check.notification_reasons(previous_broken, changed_broken),
        )
        self.assertIn(
            check.NotificationReason.CHAIN_CHANGED,
            check.notification_reasons(previous_broken, changed_broken),
        )

    def test_write_outputs_writes_compact_state_notification_and_github_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_output = Path(tmp) / "state.json"
            notification_output = Path(tmp) / "notification.json"
            github_output = Path(tmp) / "github-output.txt"
            args = argparse.Namespace(
                state_output=state_output,
                notification_output=notification_output,
                github_output=github_output,
            )
            state = {
                "version": 1,
                "checked_at": "2026-05-11T00:00:00+00:00",
                "status": "broken",
                "base_branch": "old",
                "branches": ["old", "next"],
                "branch_heads": {"old": "a", "next": "b"},
                "config_fingerprint": "cfg",
                "chain_fingerprint": "chain",
                "health_fingerprint": "health",
                "results": [
                    {
                        "source_label": "old",
                        "source_ref": "refs/remotes/origin/old",
                        "target": "next",
                        "status": "conflict",
                        "message": "blocked",
                        "conflicted_files": ["file.txt"],
                        "first_conflicting_commit": {
                            "sha": "abc",
                            "author": "Tester <tester@example.invalid>",
                            "subject": "break it",
                        },
                        "candidate_commits": [],
                    }
                ],
            }

            check.write_outputs(args, state, [check.NotificationReason.BROKEN])
            compact = json.loads(state_output.read_text(encoding="utf-8"))
            notification = json.loads(notification_output.read_text(encoding="utf-8"))
            github_lines = github_output.read_text(encoding="utf-8").splitlines()

        self.assertNotIn("candidate_commits", compact["results"][0])
        self.assertTrue(notification["notify"])
        self.assertIn("Blocked edge: old -> next", notification["text"])
        self.assertIn("should_notify=true", github_lines)
        self.assertIn("status=broken", github_lines)


class NotificationScriptTests(unittest.TestCase):
    def test_suppressed_notification_does_not_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            notification = Path(tmp) / "notification.json"
            notification.write_text('{"notify": false}', encoding="utf-8")
            config = Path(tmp) / "config.yml"
            config.write_text("branches:\n  - old\n  - next\n", encoding="utf-8")

            with mock.patch.object(send, "post_slack_webhook") as slack_post:
                with mock.patch.object(sys, "argv", ["send", "--notification", str(notification), "--config-file", str(config)]):
                    code = quietly(send.main)

        self.assertEqual(code, 0)
        slack_post.assert_not_called()

    def test_posts_to_enabled_webhooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            notification = Path(tmp) / "notification.json"
            notification.write_text('{"notify": true, "text": "hello"}', encoding="utf-8")
            config = Path(tmp) / "config.yml"
            config.write_text(
                """
branches:
  - old
  - next
notifications:
  slack:
    enabled: true
  zulip:
    enabled: false
""".lstrip(),
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": "https://slack.example"}, clear=False):
                with mock.patch.object(send, "post_slack_webhook") as slack_post:
                    with mock.patch.object(send, "post_zulip_webhook") as zulip_post:
                        with mock.patch.object(sys, "argv", ["send", "--notification", str(notification), "--config-file", str(config)]):
                            code = quietly(send.main)

        self.assertEqual(code, 0)
        slack_post.assert_called_once_with("https://slack.example", "hello")
        zulip_post.assert_not_called()


class UpdateGithubVariableTests(unittest.TestCase):
    def test_updates_existing_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            value_file = Path(tmp) / "state.json"
            value_file.write_text('{"status":"healthy"}', encoding="utf-8")

            with mock.patch.dict(
                os.environ,
                {"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "owner/repo"},
                clear=False,
            ):
                with mock.patch.object(update_var, "api_request", return_value=204) as api_request:
                    with mock.patch.object(sys, "argv", ["update", "--name", "STATE", "--value-file", str(value_file)]):
                        code = quietly(update_var.main)

        self.assertEqual(code, 0)
        api_request.assert_called_once()
        self.assertEqual(api_request.call_args.args[1], "PATCH")

    def test_creates_missing_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            value_file = Path(tmp) / "state.json"
            value_file.write_text('{"status":"healthy"}', encoding="utf-8")

            with mock.patch.dict(
                os.environ,
                {"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "owner/repo"},
                clear=False,
            ):
                with mock.patch.object(update_var, "api_request", side_effect=[404, 201]) as api_request:
                    with mock.patch.object(sys, "argv", ["update", "--name", "STATE", "--value-file", str(value_file)]):
                        code = quietly(update_var.main)

        self.assertEqual(code, 0)
        self.assertEqual(api_request.call_count, 2)
        self.assertEqual(api_request.call_args_list[0].args[1], "PATCH")
        self.assertEqual(api_request.call_args_list[1].args[1], "POST")


if __name__ == "__main__":
    unittest.main()
