"""Regression coverage for superseded GitHub Actions head-SHA checks."""
from unittest import mock

from hermes_cli import kanban_pr_acceptance as acceptance


SHA = "a" * 40
LATER_SHA = "b" * 40
URL = "https://github.com/acme/repo/pull/7"


def _receipt(*, comparison="ahead", workflow_id=44, job_name="build", job_conclusion="success", lookup_error=False):
    failure = {"name": "build", "id": 12, "head_sha": SHA, "status": "completed",
               "conclusion": "failure", "app": {"slug": "github-actions"}}
    later = {"id": 40, "workflow_id": workflow_id, "head_branch": "dev",
             "created_at": "2026-11-10T00:01:00Z", "conclusion": "success",
             "head_sha": LATER_SHA, "html_url": "https://example.invalid/run/40"}

    def api(endpoint, *, query=None, paginate=False):
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": SHA, "baseRefName": "main", "state": "OPEN",
                "baseRef": {"branchProtectionRule": {"requiredStatusChecks": []}},
            }}}}
        if "/rules/branches/" in endpoint:
            raise ValueError("rulesets unavailable")
        if endpoint.endswith("/check-runs?per_page=100&filter=latest"):
            return [{"check_runs": [failure]}]
        if endpoint.endswith("/actions/jobs/12"):
            if lookup_error:
                raise ValueError("lookup failed")
            return {"run_id": 30, "head_branch": "dev"}
        if endpoint.endswith("/actions/runs/30"):
            return {"workflow_id": 44, "created_at": "2026-11-10T00:00:00Z"}
        if "/actions/workflows/44/runs?" in endpoint:
            return [{"workflow_runs": [later]}]
        if endpoint.endswith("/actions/runs/40/jobs?per_page=100"):
            return [{"jobs": [{"id": 50, "name": job_name, "conclusion": job_conclusion}]}]
        if "/compare/" in endpoint:
            return {"status": comparison}
        raise AssertionError(endpoint)

    with mock.patch.object(acceptance, "_api", side_effect=api):
        return acceptance.collect_acceptance("acme/repo", URL)


def test_later_green_descendant_actions_job_supersedes_failure():
    receipt = _receipt()
    assert receipt["ok"] is True
    check = receipt["checks"][0]
    assert check["classification"] == "superseded"
    assert check["superseded_by"] == {"run_id": 40, "job_id": 50,
                                      "head_sha": LATER_SHA, "url": "https://example.invalid/run/40"}


def test_non_descendant_later_run_stays_failed():
    assert _receipt(comparison="behind")["checks"][0]["classification"] == "failure"


def test_different_workflow_stays_failed():
    assert _receipt(workflow_id=45)["checks"][0]["classification"] == "failure"


def test_different_or_failed_job_stays_failed():
    assert _receipt(job_name="other")["checks"][0]["classification"] == "failure"
    assert _receipt(job_conclusion="failure")["checks"][0]["classification"] == "failure"


def test_lookup_api_error_fails_closed():
    assert _receipt(lookup_error=True)["checks"][0]["classification"] == "failure"


def test_required_checks_branch_does_not_lookup_superseding_actions_runs():
    calls = []

    def api(endpoint, *, query=None, paginate=False):
        calls.append(endpoint)
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": SHA, "baseRefName": "main", "state": "OPEN",
                "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                    {"context": "build", "app": {"databaseId": 1}}]}},
            }}}}
        if "/rules/branches/" in endpoint:
            return []
        if endpoint.endswith("/check-runs?per_page=100&filter=latest"):
            return [{"check_runs": [{"name": "build", "id": 12, "head_sha": SHA,
                                      "status": "completed", "conclusion": "failure",
                                      "app": {"id": 1}}], "total_count": 1}]
        if endpoint.endswith("/statuses?per_page=100"):
            return [{}]
        if endpoint.endswith("/pulls/7"):
            return {"head": {"sha": SHA}, "base": {"ref": "main"},
                    "state": "open", "merged": False}
        raise AssertionError(endpoint)

    with mock.patch.object(acceptance, "_api", side_effect=api):
        receipt = acceptance.collect_acceptance("acme/repo", URL)
    assert receipt["checks"][0]["classification"] == "failure"
    assert not any("/actions/jobs/" in endpoint for endpoint in calls)
