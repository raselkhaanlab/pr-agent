"""Push the PR review result to an external webhook (KPIHub).

push_review_to_webhook(git_provider, review_data) sends the review to PR_AGENT_WEBHOOK__URL,
signed with HMAC-SHA256 if PR_AGENT_WEBHOOK__SECRET is set. Retries on network errors/timeouts/5xx
(not on 4xx), up to PR_AGENT_WEBHOOK__MAX_RETRIES times. Each push carries a stable
Idempotency-Key header so retries can be deduped on the receiving end.

Config is env-var only, not a CLI arg - PR-Agent's --key=value override can be triggered from
a PR comment, and its blocklist doesn't catch "secret".

Never raises - failures are logged and swallowed so a webhook outage can't break a review.
"""

import asyncio
import datetime
import hashlib
import hmac
import json
import threading
import uuid
from typing import Any, Optional

import aiohttp

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.log import get_logger

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_RETRIES = 1
DEFAULT_BACKOFF_SECONDS = 1


def push_review_to_webhook(git_provider: GitProvider, review_data: dict) -> bool:
    """Build the payload and deliver it, blocking until done (bounded - see module docstring). Returns True on success.

    Never raises - swallows and logs any unexpected error so a failure here
    can't crash the caller's own flow. Delivery runs on a thread that this
    joins, so the wait is real but capped by the configured timeout/retry
    settings - never unbounded, never silently skipped.
    """
    try:
        payload = build_review_webhook_payload(git_provider, review_data)

        result = {}

        def _run():
            result["ok"] = asyncio.run(send_review_webhook(payload))

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join()
        return result.get("ok", False)
    except Exception as e:
        get_logger().warning(f"Unexpected error while pushing review data to webhook: {e}")
        return False


def build_review_webhook_payload(git_provider: GitProvider, review_data: dict) -> dict[str, Any]:
    """Assemble the JSON payload: PR context + the parsed review result.

    review_data comes straight from pr_reviewer.py's parsed LLM output, which is itself
    wrapped in a top-level "review" key (from the YAML prompt template) - unwrapped here
    so agent_review holds the actual fields (score, key_issues_to_review, ...) directly
    instead of nesting them one level deeper as agent_review.review.*.
    """
    payload = {
        "event_time": datetime.datetime.now(datetime.UTC).isoformat(),
        "event_type": "review",
        "sender": "ait-pr-agent",
        "pull_request": _extract_pr_context(git_provider),
        "agent_review": _normalize_review_fields(review_data.get("review", review_data)),
    }
    get_logger().debug(f"Built review webhook payload: {payload}")
    return payload


_NEGATIVE_ANSWERS = ("no", "none", "n/a", "")


def _to_int(value):
    """Best-effort int coercion for numeric fields the LLM sometimes returns as a string
    (e.g. "4\n" from a YAML block scalar). Left as-is if it's not a clean number."""
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value
    return value


def _to_bool(value):
    """Best-effort bool coercion for yes/no fields. Matched against the negative case
    rather than requiring an exact "yes", since the answer isn't always a bare word
    (e.g. "No\n", "no", or a full sentence)."""
    if isinstance(value, str):
        return value.strip().lower() not in _NEGATIVE_ANSWERS
    return bool(value)


def _clean_str(value):
    return value.strip() if isinstance(value, str) else value


def _normalize_review_fields(review: dict) -> dict:
    """Coerce known type-inconsistent LLM output fields to a stable shape for KPIHub.

    The parsed YAML review can carry the same field as different types across runs -
    numbers/booleans coming back as strings (often with a trailing newline from a YAML
    block scalar), or a bare "No"/"Yes" implicitly typed as a bool by YAML itself.
    Normalized here so the JSON payload has a stable schema regardless of what shape
    this particular run produced.
    """
    normalized = dict(review)

    for key in ("estimated_effort_to_review_[1-5]", "score"):
        if key in normalized:
            normalized[key] = _to_int(normalized[key])

    if "relevant_tests" in normalized:
        normalized["relevant_tests"] = _to_bool(normalized["relevant_tests"])

    # security_concerns isn't a plain yes/no - the prompt asks for the literal string "No"
    # when clean, or a full description otherwise. Split into a boolean flag (consistent
    # type, easy to filter/alert on) plus the explanation in its own field, rather than
    # collapsing to bool and silently discarding the description.
    if "security_concerns" in normalized:
        raw = normalized.pop("security_concerns")
        has_concern = _to_bool(raw)
        normalized["security_concerns"] = has_concern
        normalized["security_concerns_details"] = raw.strip() if has_concern and isinstance(raw, str) else None

    # Each issue's string fields (relevant_file, issue_header, issue_content) come from the
    # same "|" block-scalar YAML style, so they carry the same trailing-newline noise.
    issues = normalized.get("key_issues_to_review")
    if isinstance(issues, list):
        normalized["key_issues_to_review"] = [
            {k: _clean_str(v) for k, v in issue.items()} if isinstance(issue, dict) else issue
            for issue in issues
        ]

    return normalized


async def send_review_webhook(payload: dict[str, Any]) -> bool:
    """Sign and POST the payload to the configured webhook URL, retrying on failure.

    Never raises - logs and returns False on any failure or missing config.
    """
    url = get_settings().get("PR_AGENT_WEBHOOK.URL", None)
    secret = get_settings().get("PR_AGENT_WEBHOOK.SECRET", None)
    if not url:
        get_logger().warning("PR_AGENT_WEBHOOK__URL is not configured; skipping webhook push")
        return False

    max_retries = get_settings().get("PR_AGENT_WEBHOOK.MAX_RETRIES", DEFAULT_MAX_RETRIES)
    total_attempts = max_retries + 1  # 1 initial call + up to max_retries retries
    timeout_seconds = get_settings().get("PR_AGENT_WEBHOOK.TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    backoff_seconds = get_settings().get("PR_AGENT_WEBHOOK.BACKOFF_SECONDS", DEFAULT_BACKOFF_SECONDS)

    body = json.dumps(payload, default=str).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": str(uuid.uuid4()),  # generated once, reused across every retry attempt below
    }
    if secret:
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-PR-Agent-Signature-256"] = f"sha256={signature}"
    else:
        get_logger().warning("PR_AGENT_WEBHOOK__SECRET is not configured; sending webhook unsigned")

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(1, total_attempts + 1):
            get_logger().info(f"Attempt {attempt}/{total_attempts} to push review data to webhook")
            outcome = await _post_once(session, url, body, headers, attempt, total_attempts)
            if outcome is not None:
                return outcome
            if attempt < total_attempts:
                await asyncio.sleep(backoff_seconds * (2 ** (attempt - 1)))

    get_logger().warning(f"Failed to push review data to webhook after {total_attempts} attempts")
    return False


async def _post_once(session: aiohttp.ClientSession, url: str, body: bytes, headers: dict,
                      attempt: int, total_attempts: int) -> Optional[bool]:
    """Single POST attempt. Returns True/False to stop retrying, None to retry the next attempt."""
    try:
        async with session.post(url, data=body, headers=headers) as response:
            if 400 <= response.status < 500:
                text = await response.text()
                get_logger().warning(f"Webhook rejected payload (status={response.status}); not retrying: {text}")
                return False
            response.raise_for_status()
            get_logger().info(f"Successfully pushed review data to webhook (status={response.status}, attempt={attempt})")
            return True
    except aiohttp.ClientError as e:
        get_logger().warning(f"Webhook push attempt {attempt}/{total_attempts} failed: {e}")
        return None
    except asyncio.TimeoutError as e:
        get_logger().warning(f"Webhook push attempt {attempt}/{total_attempts} timed out: {e}")
        return None
    except Exception as e:
        get_logger().warning(f"Webhook push attempt {attempt}/{total_attempts} failed: {e}")
        return None


def _extract_pr_context(git_provider: GitProvider) -> dict[str, Any]:
    """Extract PR context fields.

    Uses provider-generic methods where available. Reviewers, participants,
    state, author, and timestamps aren't exposed by the base GitProvider
    interface, so those fall back to Bitbucket's raw PR payload
    (`git_provider.pr.data`) when present.
    """
    get_title = getattr(git_provider, "get_title", None)
    context: dict[str, Any] = {
        "url": _safe_call(git_provider.get_pr_url),
        "repository": _safe_call(lambda: f"{git_provider.workspace_slug}/{git_provider.repo_slug}"),
        "title": _safe_call(get_title) if get_title else None,
        "pr_id": getattr(git_provider, "pr_num", None),
        "source_branch": _safe_call(git_provider.get_pr_branch),
        "destination_branch": None,
        "state": None,
        "author": None,
        "reviewers": [],
        "participants": [],
        "created_at": None,
        "updated_at": None,
    }

    raw = getattr(getattr(git_provider, "pr", None), "data", None)
    if isinstance(raw, dict):
        context["destination_branch"] = ((raw.get("destination") or {}).get("branch") or {}).get("name")
        context["state"] = raw.get("state")
        context["author"] = (raw.get("author") or {}).get("display_name")
        context["created_at"] = raw.get("created_on")
        context["updated_at"] = raw.get("updated_on")
        context["reviewers"] = [
            {"display_name": r.get("display_name"), "uuid": r.get("uuid")}
            for r in (raw.get("reviewers") or [])
            if isinstance(r, dict)
        ]
        context["participants"] = [
            {
                "display_name": (p.get("user") or {}).get("display_name"),
                "role": p.get("role"),
                "approved": p.get("approved"),
            }
            for p in (raw.get("participants") or [])
            if isinstance(p, dict)
        ]

    return context


def _safe_call(fn) -> Optional[Any]:
    try:
        return fn()
    except Exception:
        return None
