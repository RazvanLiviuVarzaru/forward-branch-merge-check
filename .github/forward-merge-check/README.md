# Forward Branch Merge Check

This repository is intended to be the central home for forward-merge checking.

Target repositories should keep only small workflow stubs. The scripts,
configuration, tests, and reusable workflows live here, so behavior changes are
controlled by pushes to this repository.

## Repository Model

There are two repositories involved:

- Tool repository: this repository. It owns the reusable workflows, scripts,
  tests, and branch-chain configuration.
- Target repository: for example `MariaDB/server`. It owns the branches and PRs
  being checked.

The target repository calls reusable workflows from the tool repository:

```yaml
jobs:
  pr-forward-mergeability:
    uses: YOUR_ORG/forward-branch-merge-check/.github/workflows/pr-forward-mergeability.yml@main
```

The reusable workflow checks out:

1. the target repository into `target/`
2. this tool repository into `tool/`

Then it runs scripts from `tool/` against Git refs in `target/`.

## Target Repository Installation

Copy only these tiny workflow stubs into the target repository:

```text
.github/workflows/pr-forward-mergeability.yml
.github/workflows/forward-merge-chain-health.yml
```

Examples are provided in:

```text
examples/target-workflows/
```

Replace `YOUR_ORG/forward-branch-merge-check` with the real tool repository.

For PR checks, the tiny PR workflow stub should live on the target repository's
default branch. It can run for pull requests targeting release branches because
`pull_request` workflows can be filtered by the pull request base branch.

For the scheduled chain-health check, the tiny chain-health workflow only needs
to exist on the target repository's default branch.

If the tool repository is private, create a target-repository secret with a token
that can read the tool repository:

```text
FORWARD_MERGE_CHECK_TOKEN
```

If the tool repository is public, you can remove the `tool_repository_token`
secret mapping from the target stubs.

## Configuration

The default branch chain is configured in the tool repository at:

```text
.github/forward-merge-check/forward-merge-chain.yml
```

```yaml
base_branch: "10.6"

branches:
  - "10.6"
  - "10.11"
  - "11.4"
  - "11.8"
  - "12.3"
  - "main"

notifications:
  slack:
    enabled: true
    secret: SLACK_WEBHOOK_URL
  zulip:
    enabled: true
    secret: ZULIP_WEBHOOK_URL
```

`branches` must be ordered from oldest/stablest to newest.

`base_branch` is where the full chain-health check starts. Usually this is the
first branch in `branches`.

For multiple target repositories, keep multiple config files in this repository
and point each target stub at the right one:

```yaml
with:
  config_path: .github/forward-merge-check/config/mariadb-server.yml
```

## Reusable PR Workflow

Reusable workflow:

```text
.github/workflows/pr-forward-mergeability.yml
```

Target stub example:

```yaml
name: PR forward mergeability

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read

jobs:
  pr-forward-mergeability:
    uses: YOUR_ORG/forward-branch-merge-check/.github/workflows/pr-forward-mergeability.yml@main
    with:
      tool_repository: YOUR_ORG/forward-branch-merge-check
      tool_ref: main
      config_path: .github/forward-merge-check/forward-merge-chain.yml
    secrets:
      tool_repository_token: ${{ secrets.FORWARD_MERGE_CHECK_TOKEN }}
```

Expected behavior:

- If the PR target branch is not in the configured chain, the workflow exits
  successfully and skips the check.
- The baseline sanity check starts at the PR target branch, not at the global
  configured `base_branch`.
- If the baseline chain is healthy downstream from the PR target, the PR is
  tested through every later branch.
- If the baseline chain is already broken downstream from the PR target, the PR
  is tested only until that known-broken edge.
- If the PR causes a new conflict before the known-broken edge, the workflow
  fails.
- If the only conflict is already present in the baseline chain, the PR workflow
  exits successfully.

Example:

```text
baseline:
10.6 -> 10.11: ok
10.11 -> 11.4: ok
11.4 -> 11.8: already broken

PR target:
10.11
```

The PR workflow tests:

```text
PR-applied 10.11 -> 11.4
```

Then it stops before `11.4 -> 11.8`, because that edge is already broken
without the PR.

## Reusable Chain-Health Workflow

Reusable workflow:

```text
.github/workflows/forward-merge-chain-health.yml
```

Target stub example:

```yaml
name: Forward merge chain health

on:
  schedule:
    - cron: "*/30 * * * *"

  workflow_dispatch:

permissions:
  contents: read

jobs:
  chain-health:
    uses: YOUR_ORG/forward-branch-merge-check/.github/workflows/forward-merge-chain-health.yml@main
    with:
      tool_repository: YOUR_ORG/forward-branch-merge-check
      tool_ref: main
      config_path: .github/forward-merge-check/forward-merge-chain.yml
      state_cache_key: forward-merge-chain-state
    secrets:
      tool_repository_token: ${{ secrets.FORWARD_MERGE_CHECK_TOKEN }}
      slack_webhook_url: ${{ secrets.SLACK_WEBHOOK_URL }}
      zulip_webhook_url: ${{ secrets.ZULIP_WEBHOOK_URL }}
```

This workflow does not run on every push, so developer branches do not trigger
it.

The workflow checks the whole configured chain and records every adjacent edge
it can.

Important behavior:

- If an edge merges cleanly, the next edge uses the synthetic merge result.
- If an edge is already broken, the next edge resets to the real target branch.
- The script does not build synthetic merge state across a failed edge.

Example:

```text
10.6 -> 10.11: conflict
```

The next check is:

```text
real 10.11 -> 11.4
```

not:

```text
failed synthetic 10.11 -> 11.4
```

The workflow fails if any checked edge has a conflict. It still writes state and
can send notifications before failing.

## Conflict Reports

When a merge fails, the script prints:

- the blocked edge, for example `10.11 -> 11.4`
- conflicted files
- the first likely source-side commit that introduced the conflict
- candidate source-side commits that touched the conflicted files

The "first likely" commit is an approximation. The script tries source-side
non-merge commits independently against the target branch. A Git conflict is
caused by the interaction between both branches, so this is a debugging hint,
not a proof.

## Notifications

Notifications are only sent by the chain-health workflow. PR workflows do not
send Slack or Zulip messages.

The chain-health workflow stores previous health state in the target
repository's GitHub Actions cache.

```text
restore prefix: forward-merge-chain-state-
saved key:      forward-merge-chain-state-<run_id>-<run_attempt>
path: .forward-merge-check-state/state.json
```

The workflow restores the newest cache entry with the configured prefix before
the check, writes the new state after the check, and saves it under a new
run-specific cache key. This avoids personal access tokens or GitHub Apps.

The chain-health job uses a concurrency group so two scheduled/manual runs do
not compute and save state at the same time.

GitHub caches are immutable and can be evicted. Keeping one tiny state file per
run is intentional; GitHub will age out old entries. If the state cache
disappears, the next run acts like a first run and creates a new cache entry.

The state contains:

- `status`: `healthy` or `broken`
- `config_fingerprint`: fingerprint of `base_branch` and `branches`
- `chain_fingerprint`: fingerprint of branch heads in the configured chain
- `health_fingerprint`: fingerprint of the merge-health result
- compact per-edge results

This state prevents duplicate notifications. A chain that stays broken in the
same way will keep failing the workflow, but it will not send the same alert
every 30 minutes.

Notifications are sent when:

- the first observed state is already broken
- the chain changes from healthy to broken
- the chain changes from broken to healthy
- the broken result changes, for example a different blocked edge, conflicted
  files, or likely conflict commit
- the configured chain changes while the chain is broken

Notifications are suppressed when:

- the chain stays healthy
- the chain stays broken in the same way

To enable external notifications, add either or both target-repository secrets:

```text
SLACK_WEBHOOK_URL
ZULIP_WEBHOOK_URL
```

If a secret is missing, that destination is skipped. If both secrets are
missing, the workflow still computes and stores state but sends no external
notification.

## Expected Exit Codes

`check_forward_mergeability.py` exits with:

- `0` when the checked PR/chain is clean or skipped
- `1` when the checked PR/chain has a conflict
- `2` when there is a script/config/runtime error

For PR mode, baseline-chain conflicts do not directly cause exit code `1`.
Only PR-forward conflicts in the tested range fail the PR.

## Local Use

For local manual testing, keep the scripts in this tool repository and point
them at any target repository you already have checked out locally.

Example layout:

```text
~/src/forward-branch-merge-check   # this tool repository
~/src/server                       # target repository to check
```

From the tool repository:

```bash
cd ~/src/forward-branch-merge-check
```

Make sure the target repository has the branch-chain refs available:

```bash
git -C ~/src/server fetch origin \
  +refs/heads/10.6:refs/remotes/origin/10.6 \
  +refs/heads/10.11:refs/remotes/origin/10.11 \
  +refs/heads/11.4:refs/remotes/origin/11.4 \
  +refs/heads/11.8:refs/remotes/origin/11.8 \
  +refs/heads/12.3:refs/remotes/origin/12.3 \
  +refs/heads/main:refs/remotes/origin/main
```

If a configured branch does not exist in the target repository, update the
config file instead of fetching it. For example, if the config lists `12.3` but
the target repo only has `origin/12.0` and `origin/12.1`, change the chain to
the real branch names before running the check.

Print configured branches:

```bash
python3 .github/forward-merge-check/scripts/check_forward_mergeability.py \
  --config-file .github/forward-merge-check/forward-merge-chain.yml \
  --print-branches
```

Run the full chain-health check from a clone that has the branch-chain refs
available:

```bash
python3 .github/forward-merge-check/scripts/check_forward_mergeability.py \
  --repo ~/src/server \
  --mode chain-health \
  --config-file .github/forward-merge-check/forward-merge-chain.yml
```

Run a PR-like check against any local ref:

```bash
python3 .github/forward-merge-check/scripts/check_forward_mergeability.py \
  --repo ~/src/server \
  --mode pr \
  --config-file .github/forward-merge-check/forward-merge-chain.yml \
  --base-branch 10.6 \
  --pr-ref my-local-pr-branch
```

The target repository does not need a copy of the scripts for these local
checks. The `--repo` option points the tool at the target repository.

Override the branch chain without editing the config file:

```bash
python3 .github/forward-merge-check/scripts/check_forward_mergeability.py \
  --repo ~/src/server \
  --mode chain-health \
  --base-branch release/1.0 \
  --branches release/1.0 release/1.1 main
```

## Tests

The test suite uses only the Python standard library and local synthetic Git
repositories:

```bash
python3 -m unittest discover \
  -s .github/forward-merge-check/tests
```
