"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.
"""
from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import quote

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if isinstance(value, str) and value.casefold() == "owner/repo":
        raise ValueError(
            "completion_contract literal placeholder OWNER/REPO is invalid; "
            "use a real repository name such as <owner>/<repository>"
        )
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _PR.fullmatch(value)):
        raise ValueError(
            "completion_contract must be local-only, a real repository name such as "
            "<owner>/<repository>, or an exact GitHub PR URL; the literal placeholder "
            "OWNER/REPO is invalid"
        )
    return value


def normalize_published_pr(value):
    """Return the PR URL from metadata.published_pr, or the value unchanged.

    Workers routinely publish the PR as an object ({"url": ..., "number": ...});
    only its URL is evidence. Any other non-string shape is returned as-is so
    collect_acceptance can reject it as caller input, not as a GitHub failure.
    """
    if isinstance(value, dict) and isinstance(value.get("url"), str):
        return value["url"]
    return value


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=30, check=True)
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


def _superseding_action_run(repo: str, pr_sha: str, check: dict) -> dict | None:
    """Return later green Actions evidence for a failed head-SHA job, if any.

    This fallback is intentionally narrow: it never alters required-check
    evidence, and a malformed or unavailable lookup remains a failed check.
    """
    if (check.get("app") or {}).get("slug") != "github-actions":
        return None
    try:
        job = _api(f"repos/{repo}/actions/jobs/{check['id']}")
        failed_run = _api(f"repos/{repo}/actions/runs/{job['run_id']}")
        workflow_id = failed_run["workflow_id"]
        created_at = failed_run["created_at"]
        head_branch = job["head_branch"]
        pages = _api(
            f"repos/{repo}/actions/workflows/{workflow_id}/runs?branch="
            f"{quote(head_branch, safe='')}&status=completed&per_page=100",
            paginate=True,
        )
        candidates = [run for page in pages for run in page["workflow_runs"]]
        for run in candidates:
            if (run.get("workflow_id") != workflow_id or run.get("conclusion") != "success"
                    or run.get("head_branch") != head_branch or run.get("created_at") <= created_at):
                continue
            job_pages = _api(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100", paginate=True)
            jobs = [candidate for page in job_pages for candidate in page["jobs"]]
            match = next((candidate for candidate in jobs
                          if candidate.get("name") == check.get("name")
                          and candidate.get("conclusion") == "success"), None)
            if not match:
                continue
            comparison = _api(f"repos/{repo}/compare/{pr_sha}...{run['head_sha']}")
            if comparison.get("status") in {"ahead", "identical"}:
                return {"run_id": run["id"], "job_id": match["id"],
                        "head_sha": run["head_sha"], "url": run.get("html_url")}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        return None
    return None


def collect_acceptance(contract: str, published_pr: object) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "pr_url": published_pr, "checks": [],
               "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    published_pr = normalize_published_pr(published_pr)
    receipt["pr_url"] = published_pr
    if published_pr is not None and not isinstance(published_pr, str):
        # Caller input, not GitHub: falling into the infra handler below sent
        # workers to debug gh auth for weeks while the fix was a string.
        receipt["pr_url"] = None
        receipt["detail"] = ("metadata.published_pr must be the PR URL string "
                             "https://github.com/<owner>/<repository>/pull/<number>.")
        return receipt
    try:
        declared = _PR.fullmatch(contract)
        url = contract if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        owner, name = repo.split("/")
        query = '''{repository(owner:%s,name:%s){pullRequest(number:%d){headRefOid baseRefName state
            baseRef{branchProtectionRule{requiredStatusChecks{context app{databaseId}}}}}}}''' % (
                json.dumps(owner), json.dumps(name), number)
        pr = _api("graphql", query=query)["data"]["repository"]["pullRequest"]
        sha, branch = pr["headRefOid"], pr["baseRefName"]
        receipt["head_sha"] = sha
        if pr["state"] not in {"OPEN", "MERGED"}:
            # t_e5a5924c: a pull request closed WITHOUT being merged is the one
            # acceptance outcome no retry, wait or re-run can ever change. It
            # used to fall through to the generic handler below and be reported
            # as "infra ... check gh authentication/API access and retry",
            # which sent an operator hunting a credential fault that did not
            # exist while the card sat permanently unfinishable. Name the real
            # state, and say plainly that it is terminal.
            receipt.update(
                classification="closed",
                detail=(f"pull request {url} is {str(pr['state']).lower()} and was not merged, so "
                        "its acceptance can never succeed; no retry changes this."),
                recovery=("Open a new card bound to the replacement pull request and archive this "
                          "one, or use kanban_block for an operator decision. A pull-request "
                          "completion contract binds once and cannot be rebound."))
            return receipt
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("current head is unavailable")
        protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
        required = {(r["context"], (r.get("app") or {}).get("databaseId")) for r in protection.get("requiredStatusChecks", [])}
        # A private repo on GitHub Free has no rulesets API: it answers 403
        # "Upgrade to GitHub Pro or make this repository public". That is a plan
        # limit, not an access failure, and letting it fall through to the generic
        # handler reports a correctly-authenticated gh as broken. Treated as "no
        # rulesets", which leaves the verdict to the no-required-checks branch below.
        try:
            rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100", paginate=True)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
            receipt["rulesets_unavailable"] = True
            rules = []
        for page in rules:
            for rule in page:
                if rule["type"] == "required_status_checks":
                    required.update((r["context"], r.get("integration_id"))
                                    for r in rule["parameters"]["required_status_checks"])
        receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
        if not required and receipt.get("rulesets_unavailable"):
            # No repository-enforced checks exist and none CAN on this plan. Verify
            # the head SHA's own runs instead of declaring the card unfinishable.
            pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest", paginate=True)
            runs = [r for page in pages for r in page["check_runs"]]
            graded = [r for r in runs if r.get("conclusion") not in ("skipped", "neutral", None)]
            receipt["acceptance_basis"] = "head-sha-check-runs"
            receipt["checks"] = []
            for run in graded:
                classification = _classify(run, sha, run.get("conclusion"), True)
                check = {"name": run["name"], "id": run["id"], "url": run.get("html_url"),
                         "head_sha": run.get("head_sha"), "classification": classification,
                         "conclusion": run.get("conclusion")}
                if classification == "failure":
                    superseded_by = _superseding_action_run(repo, sha, run)
                    if superseded_by:
                        check.update(classification="superseded", superseded_by=superseded_by)
                receipt["checks"].append(check)
            if not graded:
                receipt["detail"] = (
                    "No check runs on the head commit. This repo cannot enforce required "
                    "checks (private repo on GitHub Free), so at least one real check must "
                    "run before a PR contract can be accepted.")
                return receipt
            outcomes = ["success" if c["classification"] == "superseded" else c["classification"]
                        for c in receipt["checks"]]
            receipt["classification"] = next((x for x in outcomes if x != "success"), "success")
            receipt["ok"] = receipt["classification"] == "success"
            receipt["detail"] = (
                "Accepted on head-SHA check runs. This repo exposes no branch protection "
                "or rulesets (private repo on GitHub Free), so repository-enforced required "
                "checks are unavailable; a deleted workflow would remove its check rather "
                "than fail it.")
            return receipt
        if not required:
            receipt["detail"] = (
                "No repository-required checks are configured; explicitly use a "
                "local-only contract for non-CI tasks."
                + (" This repo exposes no branch protection or rulesets API, which "
                   "is the case for a private repository on GitHub Free — the "
                   "absence is a plan limit, not a misconfiguration, and no gh "
                   "token scope changes it."
                   if receipt.get("rulesets_unavailable") else "")
            )
            return receipt
        pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest", paginate=True)
        runs = [run for page in pages for run in page["check_runs"]]
        if len({r["id"] for r in runs}) != pages[0]["total_count"]:
            raise ValueError("Incomplete check-run pagination")
        statuses = [{**s, "sha": sha} for page in _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100", paginate=True) for s in page]
        outcomes = []
        for context, app_id in sorted(required, key=str):
            matching = [r for r in runs if r["name"] == context and
                        (app_id in (None, -1) or r["app"]["id"] == app_id)]
            # A legacy status can satisfy an unpinned context, but never a check pinned to an app.
            legacy = [s for s in statuses if s["context"] == context] if app_id in (None, -1) else []
            selected = matching + ([max(legacy, key=lambda s: s["id"])] if legacy else [])
            if not selected:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
            for check in selected:
                is_run = "conclusion" in check
                outcome = check.get("conclusion") if is_run else check["state"]
                classification = _classify(check, sha, outcome, is_run)
                outcomes.append(classification)
                receipt["checks"].append({"name": context, "id": check["id"],
                    "url": check.get("html_url") or check.get("target_url"),
                    "head_sha": check.get("head_sha", check.get("sha")),
                    "classification": classification, "conclusion": outcome})
        # Re-read after all pages: old-head successes are never transferable.
        current = _api(f"repos/{repo}/pulls/{number}")
        if current["head"]["sha"] != sha or current["base"]["ref"] != branch or (current["state"] == "closed" and not current.get("merged")):
            receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
            return receipt
        receipt["classification"] = next((x for x in outcomes if x != "success"), "missing" if not outcomes else "success")
        receipt["ok"] = receipt["classification"] == "success"
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        # Never persist gh stderr (credentials/host details); the failed phase is actionable.
        receipt.update(classification="infra", detail="GitHub acceptance evidence unavailable or incomplete; check gh authentication/API access and retry.")
        return receipt


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")
