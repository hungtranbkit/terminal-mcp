"""A knowledge map nobody writes to decays into one nobody trusts.

The failure this guards is subtler than "the map is stale". A stale map is
honestly old. A map that records modules as VERIFIED at a commit where they
were never touched is actively misleading -- and the easy way to build one is
to write back from the spec's intentions rather than from the real diff.

So these assert the honesty properties, not the happy path: only what actually
changed is refreshed, nothing is claimed for work that did not land, secrets
are refused rather than stripped, and shared documents are appended to rather
than rewritten.
"""
from __future__ import annotations

import subprocess

import pytest

from terminal_mcp import work_writeback as wb
from terminal_mcp import work_spec as ws
from terminal_mcp.project_knowledge import ProjectKnowledge, SecretInKnowledge


def _run(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A real git repo with a real knowledge map -- the write-back reads git,
    so faking git would prove nothing about the part that matters."""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "alpha.py").write_text("A = 1\n", encoding="utf-8")
    (root / "pkg" / "beta.py").write_text("B = 1\n", encoding="utf-8")
    _run(["git", "init", "-q"], root)
    _run(["git", "config", "user.email", "t@t"], root)
    _run(["git", "config", "user.name", "t"], root)
    _run(["git", "add", "-A"], root)
    _run(["git", "commit", "-qm", "base"], root)
    return root


@pytest.fixture
def knowledge(repo):
    know = ProjectKnowledge(str(repo))
    know.record_module("alpha", paths=["pkg/alpha.py"], summary="the alpha module")
    know.record_module("beta", paths=["pkg/beta.py"], summary="the beta module")
    return know


def _head(repo):
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _spec(repo, *, task_type=ws.FEATURE_NEW, **kw):
    spec = ws.WorkSpec(spec_id="spec_x", title="add a thing", task_type=task_type)
    spec.source_commit = _head(repo)
    spec.requirement = kw.pop("requirement", "the thing exists")
    for key, value in kw.items():
        setattr(spec, key, value)
    return spec


# -- only what actually changed --------------------------------------------------

def test_only_the_modules_the_diff_touched_are_refreshed(repo, knowledge):
    spec = _spec(repo, likely_files=("pkg/alpha.py", "pkg/beta.py"))
    (repo / "pkg" / "alpha.py").write_text("A = 2\n", encoding="utf-8")

    report = wb.write_back(spec, knowledge=knowledge, cwd=str(repo))

    assert report.modules_refreshed == ["alpha"]
    assert report.modules_skipped == ["beta"], \
        "a spec that PLANNED to touch beta but did not must not mark it verified"


def test_the_plan_is_not_used_as_evidence(repo, knowledge):
    """`likely_files` is an intention. Using it here is how a map comes to
    claim confidence it never earned."""
    spec = _spec(repo, likely_files=("pkg/alpha.py", "pkg/beta.py"))
    # Nothing edited at all.
    report = wb.write_back(spec, knowledge=knowledge, cwd=str(repo))

    assert report.written is False
    assert report.reason == wb.NO_CHANGES
    assert report.modules_refreshed == []


def test_an_uncommitted_edit_counts_because_it_is_what_will_run(repo, knowledge):
    spec = _spec(repo)
    (repo / "pkg" / "beta.py").write_text("B = 99\n", encoding="utf-8")

    report = wb.write_back(spec, knowledge=knowledge, cwd=str(repo))

    assert "beta" in report.modules_refreshed


def test_the_verified_commit_recorded_is_the_real_head(repo, knowledge):
    spec = _spec(repo)
    (repo / "pkg" / "alpha.py").write_text("A = 3\n", encoding="utf-8")

    report = wb.write_back(spec, knowledge=knowledge, cwd=str(repo))

    assert report.verified_commit == _head(repo)


# -- nothing is claimed for work that did not land ---------------------------------

def test_a_spec_that_never_reached_ready_records_nothing(repo, knowledge):
    spec = _spec(repo)  # nowhere near complete
    (repo / "pkg" / "alpha.py").write_text("A = 4\n", encoding="utf-8")

    report = wb.write_back_from_result(spec, knowledge=knowledge, cwd=str(repo))

    assert report.written is False
    assert report.reason == wb.NOT_READY


def test_no_knowledge_map_is_reported_not_raised(repo):
    report = wb.write_back(_spec(repo), knowledge=None, cwd=str(repo))
    assert report.written is False
    assert report.reason == wb.NO_KNOWLEDGE


def test_outside_a_git_repo_is_reported_not_raised(tmp_path, knowledge):
    spec = ws.WorkSpec(spec_id="s", title="t")
    report = wb.write_back(spec, knowledge=knowledge, cwd=str(tmp_path))
    assert report.reason in (wb.NOT_A_REPO, wb.NO_CHANGES)


# -- the documents ------------------------------------------------------------------

def test_a_bug_records_its_root_cause_in_known_issues(repo, knowledge):
    spec = _spec(repo, task_type=ws.BUG, title="workers report IDLE")
    (repo / "pkg" / "alpha.py").write_text("A = 5\n", encoding="utf-8")

    report = wb.write_back(spec, knowledge=knowledge, cwd=str(repo),
                           root_cause="occupancy read only from the queue")

    assert wb.KNOWN_ISSUES in report.documents_appended
    assert "occupancy read only from the queue" in knowledge.document(wb.KNOWN_ISSUES)


def test_a_feature_records_itself_in_the_feature_map(repo, knowledge):
    """Without this, the next feature's reuse search has only PLANS to go on;
    the feature map is what actually shipped."""
    spec = _spec(repo, requirement="operators can export CSV")
    (repo / "pkg" / "alpha.py").write_text("A = 6\n", encoding="utf-8")

    report = wb.write_back(spec, knowledge=knowledge, cwd=str(repo),
                           patterns=["routes register via register_dashboard()"])

    assert wb.FEATURE_MAP in report.documents_appended
    text = knowledge.document(wb.FEATURE_MAP)
    assert "operators can export CSV" in text
    assert "register_dashboard()" in text


def test_a_bug_does_not_get_written_into_the_feature_map(repo, knowledge):
    spec = _spec(repo, task_type=ws.BUG)
    (repo / "pkg" / "alpha.py").write_text("A = 7\n", encoding="utf-8")

    report = wb.write_back(spec, knowledge=knowledge, cwd=str(repo),
                           root_cause="x")

    assert wb.FEATURE_MAP not in report.documents_appended


def test_decisions_and_tests_are_recorded_when_supplied(repo, knowledge):
    spec = _spec(repo)
    (repo / "pkg" / "alpha.py").write_text("A = 8\n", encoding="utf-8")

    report = wb.write_back(spec, knowledge=knowledge, cwd=str(repo),
                           decisions=["chose to extend the existing serialiser"],
                           tests=["tests/test_export.py covers quoting"])

    assert wb.DECISIONS in report.documents_appended
    assert wb.TEST_MAP in report.documents_appended


def test_appending_twice_keeps_the_first_entry(repo, knowledge):
    """These documents are shared with every other lane and with humans.
    Rewriting one to add a line is how a paragraph disappears silently."""
    spec = _spec(repo)
    (repo / "pkg" / "alpha.py").write_text("A = 9\n", encoding="utf-8")
    wb.write_back(spec, knowledge=knowledge, cwd=str(repo),
                  decisions=["first decision"])
    wb.write_back(spec, knowledge=knowledge, cwd=str(repo),
                  decisions=["second decision"])

    text = knowledge.document(wb.DECISIONS)
    assert "first decision" in text and "second decision" in text


def test_a_pre_existing_document_from_another_lane_survives(repo, knowledge):
    knowledge.write_document(wb.DECISIONS, "# Decisions\n\n- a decision another lane made\n")
    spec = _spec(repo)
    (repo / "pkg" / "alpha.py").write_text("A = 10\n", encoding="utf-8")

    wb.write_back(spec, knowledge=knowledge, cwd=str(repo), decisions=["mine"])

    text = knowledge.document(wb.DECISIONS)
    assert "a decision another lane made" in text
    assert "mine" in text


# -- secrets ------------------------------------------------------------------------

def test_a_secret_is_refused_rather_than_stripped(repo, knowledge):
    """A knowledge base is long-lived and rarely audited -- the worst place
    for a quiet strip to fail open."""
    spec = _spec(repo)
    (repo / "pkg" / "alpha.py").write_text("A = 11\n", encoding="utf-8")

    with pytest.raises(SecretInKnowledge):
        wb.write_back(spec, knowledge=knowledge, cwd=str(repo),
                      decisions=["used api_key=sk-live-abcdef0123456789abcdef0123456789"])


# -- the porcelain column trap ---------------------------------------------------

def test_an_unstaged_modification_keeps_its_full_path(repo, knowledge):
    """Caught by this suite, not by review.

    `git status --porcelain` encodes status in the first two COLUMNS, so an
    unstaged modification's line begins with a space. The git helper used to
    `.strip()` the whole output, which ate that space on the FIRST line only --
    so exactly one path came back truncated by a character ("pkg/beta.py" ->
    "kg/beta.py"), matched no module, and the change was silently attributed
    to nothing.
    """
    spec = _spec(repo)
    (repo / "pkg" / "beta.py").write_text("B = 12\n", encoding="utf-8")

    paths = wb.actual_changed_paths(spec, cwd=str(repo))

    assert "pkg/beta.py" in paths
    assert not any(p.endswith("kg/beta.py") and p != "pkg/beta.py" for p in paths)


def test_writing_the_knowledge_map_is_not_itself_a_code_change(repo, knowledge):
    """The map lives in the repo it describes. Counting its own writes as
    changes would let any write-back justify itself -- recording would become
    the evidence that something happened."""
    spec = _spec(repo)
    # The knowledge fixture already wrote .projectflow/; nothing else touched.
    paths = wb.actual_changed_paths(spec, cwd=str(repo))

    assert not any(p.startswith(".projectflow") for p in paths)
    assert wb.write_back(spec, knowledge=knowledge, cwd=str(repo)).reason == wb.NO_CHANGES
