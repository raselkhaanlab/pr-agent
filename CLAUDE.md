# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PR-Agent is an AI-powered code review agent. It automates PR reviews, suggestions, descriptions,
Q&A, and related tools across multiple git providers (GitHub, GitLab, Bitbucket, Azure DevOps,
Gitea, Gerrit, CodeCommit, local git) and multiple deployment surfaces (CLI, GitHub Action, Docker,
webhooks/apps for each provider).

## Commands

- Install: `pip install -r requirements.txt` (runtime) and `pip install -r requirements-dev.txt` (dev/test tooling).
- Run a single unit test: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_fix_json_escape_char.py -q`
- Run the full unit suite: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest -v`
- Run the CLI locally (needs an API key, e.g. `OPENAI_KEY`): `python -m pr_agent.cli --pr_url <PR URL> review`
- Build the CI test Docker target: `docker build -f docker/Dockerfile --target test .`
- Docs preview/publish: `mkdocs serve -f docs/mkdocs.yml` / `mkdocs gh-deploy -f docs/mkdocs.yml`
- Lint (Ruff, configured in `pyproject.toml`): line length 120, isort ordering, flake8-bugbear.
- `pre-commit run --all-files` runs the configured hooks (whitespace, TOML/YAML checks, isort) before submitting patches.

`PYTHONPATH=.` is required when invoking pytest from the repo root or imports break.

## Architecture

**Command dispatch**: `pr_agent/agent/pr_agent.py` defines `command2class`, mapping CLI/comment commands
(`review`, `describe`, `improve`, `ask`, `update_changelog`, `add_docs`, `generate_labels`, `help_docs`, etc.)
to tool classes. `PRAgent._handle_request` is the single entry point used by both the CLI and every
server/webhook: it applies repo-then-user settings, parses the action string, validates args, and
dispatches to the matching tool.

**Tools** (`pr_agent/tools/`): one class per capability (`PRReviewer`, `PRDescription`,
`PRCodeSuggestions`, `PRQuestions`, `PR_LineQuestions`, `PRUpdateChangelog`, `PRAddDocs`,
`PRGenerateLabels`, `PRSimilarIssue`, `PRHelpDocs`, `PRHelpMessage`, `PRConfig`, ...). Each tool owns
its own prompt construction, PR-content compression, and LLM call, then publishes results back via a
`GitProvider`.

**Git providers** (`pr_agent/git_providers/`): each provider subclasses `GitProvider`
(`git_provider.py`) and implements the platform-specific API for fetching diffs/files and publishing
comments, labels, and review output. `pr_agent/git_providers/__init__.py` holds the
`_GIT_PROVIDERS` registry keyed by `config.git_provider`, and `get_git_provider_with_context` caches
the resolved provider on the request context (used by servers) or resolves fresh (used by CLI).
When adding provider-specific fixes (e.g. branch name handling), check whether the same edge case
applies to `bitbucket_provider.py` vs `bitbucket_server_provider.py` — they are separate
implementations for cloud vs self-hosted Bitbucket.

**Algo layer** (`pr_agent/algo/`): provider-agnostic logic shared by tools — PR compression /
patch processing (`pr_processing.py`, `git_patch_processing.py`), token counting
(`token_handler.py`), language detection (`language_handler.py`), file filtering
(`file_filter.py`), and the AI handler abstraction (`ai_handlers/`: `base_ai_handler.py` plus
`litellm_ai_handler.py` / `openai_ai_handler.py` / `langchain_ai_handler.py` implementations,
LiteLLM being the default used by `PRAgent`).

**Configuration** (`pr_agent/config_loader.py` + `pr_agent/settings/`): built on Dynaconf with a
custom merge loader (`custom_merge_loader.py`) so multiple TOML files can layer without one file's
`[section]` clobbering another's. Load order: bundled defaults in `pr_agent/settings/*.toml`
(configuration, prompts per tool, ignore lists, label/doc templates) → secrets
(`settings/.secrets.toml`, `settings_prod/.secrets.toml`) → the target repo's own `pyproject.toml`
`[tool.pr-agent]` section (`_find_pyproject`, found by walking up to the nearest `.git`) →
repo-level `.pr_agent.toml` applied per-PR via `apply_repo_settings` → CLI/comment args. Prompts and
behavior should be changed by editing/overriding these TOML files, not hardcoded in Python. Secrets
can also come from AWS Secrets Manager (`apply_secrets_manager_config`), applied only where no
env/file value already exists.

**Servers** (`pr_agent/servers/`): one webhook/app entry point per platform (`github_app.py`,
`github_action_runner.py`, `github_polling.py`, `gitlab_webhook.py`, `bitbucket_app.py`,
`bitbucket_server_webhook.py`, `azuredevops_server_webhook.py`, `gerrit_server.py`,
`gitea_app.py`, plus Lambda variants). These are thin adapters that parse platform events into a
`pr_url` + command and call `PRAgent.handle_request`.

## Testing

- `tests/unittest/` — pytest unit tests, isolate helpers in `pr_agent/algo/`, `pr_agent/tools/`, or
  provider adapters; many existing files use parameterization.
- `tests/e2e_tests/` — integration tests against live providers; require provider tokens
  (`TOKEN_GITHUB`, `TOKEN_GITLAB`, `BITBUCKET_USERNAME`, `BITBUCKET_PASSWORD`) and can take minutes.
  Run only with credentials configured.
- `tests/health_test/main.py` — smoke test exercising `/describe`, `/review`, `/improve` end to end;
  update expected artifacts if prompt output changes meaningfully.

## Conventions

- Match the Ruff config: 120-char lines, isort import grouping, double quotes for strings.
- Configuration/prompt TOML files are a source of truth for behavior — preserve section order and
  comments when editing, and keep mirrored files (`.pr_agent.toml`, `pr_agent/settings/*.toml`)
  consistent when changing a setting that exists in both places.
- Don't hardcode values that belong in `pr_agent/settings/` or `.pr_agent.toml`.
- Conventional Commit-style messages (e.g. `fix: handle missing repo settings gracefully`); branch
  names follow `feature/<name>` or `fix/<issue>`.
- Ask before adding dependencies, renaming files, or changing workflow definitions — many consumers
  embed these paths and prompts.
