"""Kanban decomposer — fan a triage task out into a graph of child tasks.

Invoked by ``hermes kanban decompose [task_id | --all]`` and the
auto-decompose path in the gateway dispatcher loop. Reads the user's
profile roster (with descriptions) and asks the auxiliary LLM to
return a task graph in JSON. Then atomically creates the children,
links them under the root, and flips the root ``triage -> todo``.

The root task stays alive and becomes the parent of every leaf child,
so when the whole graph completes the root wakes back up — its
assignee (the orchestrator profile) gets a chance to judge completion
and add more tasks if the work isn't done yet.

Design notes
------------

* Mirrors the shape of ``hermes_cli/kanban_specify.py``: lazy aux
  client import inside the function, lenient response parse, never
  raises on expected failure modes.

* The system prompt sees the *configured* profile roster — names plus
  descriptions plus the default fallback. Profiles without a
  description are still listed (with a note) so the decomposer can
  match on name as a fallback, but the user has an obvious incentive
  to describe them.

* ``fanout=false`` collapses to the same effect as ``kanban specify``:
  we tighten the body and flip ``triage -> todo`` as a single task,
  no children created. This makes ``decompose`` a strict superset of
  ``specify`` from the user's perspective.

* If the LLM picks an assignee that doesn't exist as a profile, we
  rewrite it to the configured ``default_assignee`` (or the default
  profile if unset). A child task NEVER ends up with ``assignee=None``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import profiles as profiles_mod

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Kanban decomposer for the Hermes Agent board.

A user dropped a rough idea into the Triage column. Your job is to break it
into a small graph of concrete child tasks and route each one to the best-
matching profile from the available roster.

You will be given:
  - The original task title and body
  - The list of available profiles (each with name + description)
  - The fallback "default_assignee" used when no profile fits

Output a single JSON object with this exact shape:

  {
    "fanout": true,
    "rationale": "<one sentence on why this decomposition>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for the worker on this child task>",
        "assignee": "<profile name from the roster, or null for default>",
        "parents": [<int>, ...]
      },
      ...
    ]
  }

Rules:
  - "parents" is a list of INDICES (0-based) into this same "tasks" list,
    expressing actual data dependencies. Tasks with no parents run in
    PARALLEL. Tasks with parents wait until every parent completes.
  - Prefer parallelism. If two tasks can be done independently, give
    them no parents so the dispatcher fans them out at once.
  - Use 2-6 tasks for normal work. Don't create 20 tiny tasks. Don't
    cram everything into 1 task.
  - Pick assignees from the roster by matching the task to the profile's
    DESCRIPTION (not just the name). When nothing matches well, use null
    and the system will route to the default_assignee.
  - Each child task body is what a fresh worker will read with no other
    context — be specific about goal, approach, and acceptance criteria.
  - CODING IMPLEMENTATION (writing/changing code, opening PRs, running
    migrations) MUST be assigned to an EXTERNAL WORKER LANE if one is
    listed in the roster (marked "[external worker lane]"). Never assign
    code implementation to a functional profile — those profiles produce
    specs, reviews and governance, not commits.
  - Do NOT create a task that both implements AND reviews the same work.
    Assign the implementation to an external worker lane; the system
    automatically creates the paired review task for the other lane.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "assignee": "<profile name from the roster, or null for default>"
  }

In that case the task stays as one work item, just with a tightened spec and
a concrete assignee. If no profile fits, use null and the system will route to
the default_assignee.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Available profiles (assignees you may pick from):
{roster}

Default assignee (used when no profile fits a task): {default_assignee}
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class DecomposeOutcome:
    """Result of decomposing a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    fanout: bool = False
    child_ids: list[str] | None = None
    new_title: Optional[str] = None
    #: How many auxiliary-LLM attempts were spent (1 when it worked first try).
    attempts: int = 1
    #: True when the failure looked retryable and every attempt was used up.
    transient: bool = False


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _extract_json_blob(raw: str) -> Optional[dict]:
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(val, dict):
        return None
    return val


def _profile_author() -> str:
    """Mirror of ``hermes_cli.kanban._profile_author``."""
    return (
        os.environ.get("HERMES_PROFILE")
        or os.environ.get("USER")
        or "decomposer"
    )


def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _resolve_orchestrator_profile(cfg: dict) -> str:
    """Resolve which profile owns the root/orchestration task after fan-out.

    Falls back to the active default profile when ``kanban.orchestrator_profile``
    is unset, so a task is never stranded for lack of an orchestrator.
    """
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("orchestrator_profile") or "").strip()
    if explicit:
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass
    # Fall back to the active default profile.
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _resolve_default_assignee(cfg: dict) -> str:
    """Resolve which profile catches child tasks the orchestrator can't route."""
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("default_assignee") or "").strip()
    if explicit:
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


_DEFAULT_WORKER_LANES_FILE = "~/clawd/hermesteam/config/worker-lanes.yaml"


def _resolve_worker_lanes_file(cfg: dict) -> str:
    """Path to the worker-lanes manifest (kanban.worker_lanes_file)."""
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get("worker_lanes_file") or "").strip()
    return os.path.expanduser(explicit or _DEFAULT_WORKER_LANES_FILE)


def _load_worker_lanes(cfg: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Return (external_lanes, review_pairs) from the worker-lanes manifest.

    ``external_lanes`` maps lane name -> human description used in the
    decomposer roster. ``review_pairs`` maps builder lane -> reviewer lane
    so implementation work can be split into a build + review pair.

    Missing / unreadable / malformed manifest degrades to ({}, {}) so the
    decomposer keeps working exactly as before external lanes existed.
    """
    path = _resolve_worker_lanes_file(cfg)
    try:
        import yaml  # type: ignore

        with open(path, encoding="utf-8", errors="replace") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        logger.debug("decompose: no worker-lanes manifest at %s", path)
        return {}, {}
    except Exception as exc:
        logger.warning("decompose: failed to read worker lanes %s: %s", path, exc)
        return {}, {}

    if not isinstance(data, dict):
        return {}, {}

    lanes: dict[str, str] = {}
    raw_lanes = data.get("external_lanes")
    if isinstance(raw_lanes, dict):
        for name, meta in raw_lanes.items():
            if not isinstance(name, str) or not name.strip():
                continue
            meta = meta if isinstance(meta, dict) else {}
            if meta.get("permanent_profile"):
                # Not an external lane in the sense we care about.
                continue
            kind = str(meta.get("kind") or "external").strip()
            lanes[name.strip()] = (
                f"[external worker lane, kind={kind}] coding implementation "
                f"worker. Assign code changes / PRs here, never to a "
                f"functional profile."
            )

    pairs: dict[str, str] = {}
    raw_pairs = data.get("review_pairs")
    if isinstance(raw_pairs, list):
        for entry in raw_pairs:
            if not isinstance(entry, dict):
                continue
            builder = entry.get("builder")
            reviewer = entry.get("reviewer")
            if (
                isinstance(builder, str)
                and isinstance(reviewer, str)
                and builder.strip()
                and reviewer.strip()
                and builder.strip() != reviewer.strip()
            ):
                pairs.setdefault(builder.strip(), reviewer.strip())

    # A lane with no configured reviewer cannot enforce builder != reviewer.
    lanes = {n: d for n, d in lanes.items() if n in pairs}
    pairs = {b: r for b, r in pairs.items() if b in lanes}
    return lanes, pairs


def _build_roster(
    external_lanes: Optional[dict[str, str]] = None,
) -> tuple[list[dict], set[str]]:
    """Return (roster_for_prompt, valid_assignee_names).

    Each roster entry is ``{name, description, has_description}``. The
    valid-set is used after the LLM responds to rewrite invalid
    assignees to the default fallback.

    ``external_lanes`` (name -> description) are appended to the roster so
    coding implementation can be routed to Codex/CC worker lanes instead of
    a functional Hermes profile. They are not on-disk profiles.
    """
    roster: list[dict] = []
    valid: set[str] = set()
    try:
        all_profiles = profiles_mod.list_profiles()
    except Exception as exc:
        logger.warning("decompose: failed to list profiles: %s", exc)
        return roster, valid
    for p in all_profiles:
        desc = (p.description or "").strip()
        roster.append({
            "name": p.name,
            "description": desc or f"(no description; profile named {p.name!r})",
            "has_description": bool(desc),
        })
        valid.add(p.name)
    for name, desc in (external_lanes or {}).items():
        if name in valid:
            continue
        roster.append({
            "name": name,
            "description": desc,
            "has_description": True,
        })
        valid.add(name)
    return roster, valid


def _format_roster(roster: list[dict]) -> str:
    if not roster:
        return "  (no profiles installed — decomposer cannot route work)"
    lines = []
    for entry in roster:
        tag = "" if entry["has_description"] else " ⚠ undescribed"
        lines.append(f"  - {entry['name']}{tag}: {entry['description']}")
    return "\n".join(lines)


def _normalize_assignee_choice(
    assignee: object,
    *,
    default_assignee: str,
    valid_names: set[str],
) -> str:
    """Return a valid assignee, falling back to ``default_assignee``.

    Fan-out children and the single-task fallback should share the same
    routing guarantee: promoted work must not be left unassigned.
    """
    if not isinstance(assignee, str) or not assignee.strip():
        return default_assignee
    chosen = assignee.strip()
    if chosen not in valid_names:
        return default_assignee
    return chosen


_DEFAULT_RETRY_ATTEMPTS = 3
_DEFAULT_RETRY_BASE_SECONDS = 2.0


def _resolve_retry_policy(cfg: dict) -> tuple[int, float]:
    """Return (max_attempts, base_delay_seconds) for the auxiliary call.

    The decomposer's auxiliary provider fails transiently often enough that a
    single attempt silently drops work during a ``--all`` sweep — the operator
    sees "0 decomposed" and no reason to look further. Bounded retry with
    exponential backoff turns that into either a success or a recorded,
    visible failure.
    """
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    try:
        attempts = int(kanban_cfg.get("decompose_retry_attempts", _DEFAULT_RETRY_ATTEMPTS))
    except (TypeError, ValueError):
        attempts = _DEFAULT_RETRY_ATTEMPTS
    try:
        base = float(kanban_cfg.get("decompose_retry_base_seconds", _DEFAULT_RETRY_BASE_SECONDS))
    except (TypeError, ValueError):
        base = _DEFAULT_RETRY_BASE_SECONDS
    # Never disable retry entirely and never let a typo wedge a sweep. The
    # base is capped low because ``call_llm`` already retries inside the
    # provider; stacking a long outer backoff on top of that can park a sweep
    # for an hour. Individual sleeps are capped again at call time.
    attempts = max(1, min(attempts, 5))
    base = max(0.0, min(base, 10.0))
    return attempts, base


#: Hard ceiling on any single backoff sleep, whatever the configured base.
_MAX_RETRY_SLEEP_SECONDS = 30.0

#: Exception-name fragments that mean "retrying will not help".
_PERMANENT_LLM_ERROR_HINTS = (
    "authentication", "authorization", "permission", "credential",
    "notfound", "invalidrequest", "badrequest", "unsupported",
)


def _is_permanent_llm_failure(reason: str) -> bool:
    """Whether an auxiliary failure is worth another attempt.

    ``call_llm`` raises provider-specific exception types; we only see the
    class name. Anything that reads as auth/config/shape is permanent — waiting
    two seconds and asking again produces the identical error and buys nothing
    but a slower failure.
    """
    lowered = reason.replace("_", "").replace(" ", "").lower()
    return any(hint in lowered for hint in _PERMANENT_LLM_ERROR_HINTS)


def _fail_after_parse(
    task_id: str, author: str, reason: str, attempts: int
) -> "DecomposeOutcome":
    """Fail a decomposition that got a usable response but could not land it.

    Schema violations, graph rejections and DB errors are not retried — the
    same prompt would produce the same shape — but they must still leave
    evidence on the task, otherwise a ``--all`` sweep drops the card as
    quietly as a provider outage used to.
    """
    _record_failure_evidence(task_id, author, reason, attempts)
    return DecomposeOutcome(
        task_id, False, reason, attempts=attempts, transient=False,
    )


def _record_failure_evidence(
    task_id: str, author: str, reason: str, attempts: int
) -> None:
    """Leave the failure on the task itself.

    A permanent decompose failure must not be a stderr line that scrolls away:
    the task stays in triage and carries a comment saying why, so the next
    reconciliation sees it.
    """
    try:
        with kb.connect_closing() as conn:
            kb.add_comment(
                conn,
                task_id,
                author or "decomposer",
                f"decompose failed after {attempts} attempt(s): {reason}. "
                f"Task remains in triage; rerun "
                f"`hermes kanban decompose {task_id}` once the cause is fixed.",
            )
    except Exception as exc:  # noqa: BLE001 - evidence must never mask the error
        logger.warning(
            "decompose: could not record failure evidence on %s: %s",
            task_id, exc,
        )


def _expand_external_lane_pairs(
    children: list[dict],
    review_pairs: dict[str, str],
) -> tuple[list[dict], int]:
    """Split every external-lane child into a build + review pair.

    A child assigned to an external worker lane (e.g. ``codex``) becomes:

      * the original child, retitled ``... [build]``
      * a NEW child ``... [review]`` assigned to the paired lane
        (``cc``), depending on the build child

    Anything that previously depended on the build child is re-pointed at
    the review child, so downstream work waits for review — not merely for
    the builder to stop typing. Builder never reviews its own output.

    Review children are appended at the end so existing indices stay valid.
    Returns ``(children, pairs_created)``.
    """
    if not review_pairs:
        return children, 0

    build_to_review: dict[int, int] = {}
    extra: list[dict] = []
    next_index = len(children)

    for idx, child in enumerate(children):
        reviewer = review_pairs.get(child.get("assignee") or "")
        if not reviewer:
            continue
        title = child.get("title") or ""
        if not title.endswith("[build]"):
            child["title"] = f"{title} [build]"[:200]
        extra.append({
            "title": f"{title} [review]"[:200],
            "body": (
                f"Review the implementation produced by the paired build task "
                f"(assignee: {child.get('assignee')}).\n\n"
                f"Build task goal:\n{child.get('body') or '(no body)'}\n\n"
                f"Reviewer duties: verify the acceptance criteria with evidence "
                f"(commit/PR link, command output). Request changes instead of "
                f"editing the builder's work. Only this review task may approve "
                f"the outcome."
            ),
            "assignee": reviewer,
            "parents": [idx],
        })
        build_to_review[idx] = next_index
        next_index += 1

    if not build_to_review:
        return children, 0

    for idx, child in enumerate(children):
        if idx in build_to_review:
            continue
        child["parents"] = [
            build_to_review.get(p, p) for p in child.get("parents") or []
        ]

    return children + extra, len(build_to_review)


def _resolve_root_assignee(
    children: list[dict],
    orchestrator: str,
    external_lanes: Optional[set[str]] = None,
    preferred_owner: Optional[str] = None,
) -> str:
    """Pick who wakes up on the root task once every child is done.

    Domain-dominated work keeps its summary inside that domain: if a
    strict majority of the domain children belong to one functional
    profile, that profile owns the summary. A security audit therefore
    summarises to the security profile — not to whichever gateway
    happened to run the decomposer, and never to an unrelated domain.

    Genuinely cross-domain work falls back to the orchestrator (大C), whose
    job is exactly cross-system reconciliation.

    External worker lanes are excluded from the vote: they build and
    review code, they do not own domains and never own a summary.
    """
    if preferred_owner:
        return preferred_owner

    lanes = external_lanes or set()
    owners = [
        (child.get("assignee") or "").strip()
        for child in children
        if (child.get("assignee") or "").strip()
    ]
    domain_owners = [o for o in owners if o not in lanes]
    if not domain_owners:
        return orchestrator
    counts: dict[str, int] = {}
    for owner in domain_owners:
        counts[owner] = counts.get(owner, 0) + 1
    top, top_n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    if top_n * 2 > len(domain_owners) and top != orchestrator:
        return top
    return orchestrator


_SECURITY_REQUEST_RE = re.compile(
    r"(?:\bsecurity\s+audit\b|\bthreat\s+model(?:ing)?\b|\bred[ -]?team\b|"
    r"\bpenetration\s+test(?:ing)?\b|\bvulnerabilit(?:y|ies)\b|"
    r"資安|安全(?:稽核|審計|測試)|威脅模型|漏洞|滲透測試|紅隊)",
    re.IGNORECASE,
)


def _is_security_request(title: str, body: str) -> bool:
    """Return whether the root must be owned by the security profile."""
    return bool(_SECURITY_REQUEST_RE.search(f"{title}\n{body}"))


def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks.

    Returns an outcome describing what happened. Never raises for
    expected failure modes (task not in triage, no aux client
    configured, API error, malformed response, decomposer returned
    fanout=true with empty task list) — those surface via ``ok=False``.
    """
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, "unknown task id")
    if task.status != "triage":
        return DecomposeOutcome(
            task_id, False, f"task is not in triage (status={task.status!r})"
        )

    cfg = _load_config()
    orchestrator = _resolve_orchestrator_profile(cfg)
    default_assignee = _resolve_default_assignee(cfg)
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    auto_promote = bool(kanban_cfg.get("auto_promote_children", True))
    external_lanes, review_pairs = _load_worker_lanes(cfg)
    roster, valid_names = _build_roster(external_lanes)
    external_lane_names = set(external_lanes) | set(review_pairs.values())
    security_owner = (
        "security"
        if "security" in valid_names
        and _is_security_request(task.title or "", task.body or "")
        else None
    )

    try:
        from agent.auxiliary_client import call_llm  # type: ignore
    except Exception as exc:
        logger.debug("decompose: auxiliary client import failed: %s", exc)
        return DecomposeOutcome(task_id, False, "auxiliary client unavailable")

    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=_truncate(task.title or "", 400),
        body=_truncate(task.body or "(no body)", 4000),
        roster=_format_roster(roster),
        default_assignee=default_assignee,
    )

    max_attempts, retry_base = _resolve_retry_policy(cfg)
    audit_author = author or _profile_author()
    parsed: Optional[dict] = None
    last_reason = "LLM error"
    attempt = 0

    # Bounded retry with exponential backoff. Both a provider error and a
    # malformed body are retryable: the same prompt usually succeeds on the
    # next attempt, and a sweep that gives up after one try drops the task
    # with nothing but a stderr line the operator never sees.
    for attempt in range(1, max_attempts + 1):
        try:
            # Route through call_llm so auxiliary.kanban_decomposer.* config
            # (provider/model/base_url, extra_body, reasoning_effort, retries)
            # all apply.
            resp = call_llm(
                task="kanban_decomposer",
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.3,
                max_tokens=4000,
                timeout=timeout or 180,
            )
        except Exception as exc:
            last_reason = f"LLM error: {type(exc).__name__}"
            logger.info(
                "decompose: attempt %d/%d failed for %s (%s)",
                attempt, max_attempts, task_id, exc,
            )
        else:
            try:
                raw = resp.choices[0].message.content or ""
            except Exception:
                raw = ""
            candidate = _extract_json_blob(raw)
            if candidate is not None:
                parsed = candidate
                break
            last_reason = "LLM returned malformed JSON"
            logger.info(
                "decompose: attempt %d/%d returned malformed JSON for %s",
                attempt, max_attempts, task_id,
            )

        if _is_permanent_llm_failure(last_reason):
            # Auth/config problems do not heal by waiting. Stop immediately so
            # the operator sees the real cause instead of a slow timeout.
            break
        if attempt < max_attempts and retry_base > 0:
            time.sleep(min(retry_base * (2 ** (attempt - 1)), _MAX_RETRY_SLEEP_SECONDS))

    if parsed is None:
        _record_failure_evidence(task_id, audit_author, last_reason, attempt)
        return DecomposeOutcome(
            task_id, False, f"{last_reason} (after {attempt} attempt(s))",
            attempts=attempt,
            transient=not _is_permanent_llm_failure(last_reason),
        )

    fanout = bool(parsed.get("fanout"))

    if not fanout:
        # Fall back to single-task spec promotion (same effect as specify).
        new_title = parsed.get("title")
        new_body = parsed.get("body")
        title_val = new_title.strip() if isinstance(new_title, str) and new_title.strip() else None
        body_val = new_body if isinstance(new_body, str) and new_body.strip() else None
        if title_val is None and body_val is None:
            return DecomposeOutcome(
                task_id, False, "decomposer returned fanout=false with no title/body",
            )
        requested_assignee = _normalize_assignee_choice(
            parsed.get("assignee"),
            default_assignee=default_assignee,
            valid_names=valid_names,
        )
        if requested_assignee in review_pairs:
            # A coding task is never allowed to collapse into one card. Turn
            # the LLM's single-task result into the same build/review graph as
            # fanout=true, then continue through the common validation path.
            parsed = {
                "tasks": [{
                    "title": title_val or task.title or "Coding implementation",
                    "body": body_val if body_val is not None else (task.body or ""),
                    "assignee": requested_assignee,
                    "parents": [],
                }]
            }
            fanout = True
        else:
            assignee_val = security_owner
            if assignee_val is None and not task.assignee:
                assignee_val = requested_assignee
            with kb.connect_closing() as conn:
                ok = kb.specify_triage_task(
                    conn,
                    task_id,
                    title=title_val,
                    body=body_val,
                    assignee=assignee_val,
                    author=audit_author,
                )
            if not ok:
                return DecomposeOutcome(
                    task_id, False, "task moved out of triage before promotion",
                    attempts=attempt,
                )
            return DecomposeOutcome(
                task_id, True, "single task (no fanout)",
                fanout=False, new_title=title_val, attempts=attempt,
            )

    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return _fail_after_parse(
            task_id, audit_author,
            "decomposer returned fanout=true with empty tasks list", attempt,
        )

    # Rewrite invalid assignees to the default fallback. Never leave a
    # task with assignee=None — the user explicitly does not want that.
    children: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return _fail_after_parse(
                task_id, audit_author, f"tasks[{idx}] is not an object", attempt,
            )
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return _fail_after_parse(
                task_id, audit_author,
                f"tasks[{idx}].title is missing or empty", attempt,
            )
        body = entry.get("body")
        if not isinstance(body, str):
            body = ""
        assignee = entry.get("assignee")
        chosen = _normalize_assignee_choice(
            assignee,
            default_assignee=default_assignee,
            valid_names=valid_names,
        )
        if (
            isinstance(assignee, str)
            and assignee.strip()
            and assignee.strip() not in valid_names
        ):
            logger.info(
                "decompose: task %s child %d picked unknown assignee %r — "
                "routing to default_assignee %r",
                task_id, idx, assignee, default_assignee,
            )
        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            parents = []
        # Clean parent indices: drop non-int and out-of-range.
        clean_parents = [p for p in parents if isinstance(p, int) and 0 <= p < len(raw_tasks) and p != idx]
        children.append({
            "title": title.strip()[:200],
            "body": body.strip(),
            "assignee": chosen,
            "parents": clean_parents,
        })

    children, pairs_created = _expand_external_lane_pairs(children, review_pairs)
    if pairs_created:
        logger.info(
            "decompose: task %s created %d external build/review pair(s)",
            task_id, pairs_created,
        )
    root_assignee = _resolve_root_assignee(
        children,
        orchestrator,
        external_lanes=external_lane_names,
        preferred_owner=security_owner,
    )

    try:
        with kb.connect_closing() as conn:
            child_ids = kb.decompose_triage_task(
                conn,
                task_id,
                root_assignee=root_assignee,
                children=children,
                author=audit_author,
                auto_promote=auto_promote,
            )
    except ValueError as exc:
        return _fail_after_parse(
            task_id, audit_author, f"DB rejected graph: {exc}", attempt,
        )
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return _fail_after_parse(
            task_id, audit_author, f"DB error: {type(exc).__name__}", attempt,
        )

    if child_ids is None:
        # Someone else moved the card; not our failure to comment on.
        return DecomposeOutcome(
            task_id, False, "task moved out of triage before decomposition",
            attempts=attempt,
        )

    return DecomposeOutcome(
        task_id, True, f"decomposed into {len(child_ids)} children",
        fanout=True, child_ids=child_ids, attempts=attempt,
    )


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Return task ids currently in the triage column."""
    with kb.connect_closing() as conn:
        rows = kb.list_tasks(
            conn,
            status="triage",
            tenant=tenant,
            limit=1000,
        )
    return [row.id for row in rows]
