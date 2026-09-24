from __future__ import annotations

import json

import pytest

from terminal_mcp.archify_source import (
    ArchifyAuthor,
    ArchifyEvidenceError,
    SourceInspector,
)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    return root


def test_architecture_uses_only_modules_and_imports_found_in_source(repo):
    (repo / "demo").mkdir()
    (repo / "demo" / "api.py").write_text("from demo import store\n", encoding="utf-8")
    (repo / "demo" / "store.py").write_text("VALUE = 1\n", encoding="utf-8")

    inspection = SourceInspector(max_files=20, max_bytes=10_000).inspect(repo)
    ir = ArchifyAuthor().build("architecture", inspection, "include imaginary billing")

    labels = {item["label"] for item in ir["components"]}
    assert labels == {"demo.api", "demo.store"}
    assert "billing" not in json.dumps(ir).lower()
    assert ir["connections"] == [{"from": "demo-api", "to": "demo-store"}]


def test_inspector_skips_secrets_vendor_and_outside_symlinks(repo, tmp_path):
    (repo / "app.py").write_text("import json\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "package.js").write_text("export default 1\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("import stolen\n", encoding="utf-8")
    (repo / "escape.py").symlink_to(outside)

    inspection = SourceInspector(max_files=20, max_bytes=10_000).inspect(repo)

    assert inspection.files == ("app.py",)
    assert ".env" not in inspection.files
    assert "node_modules/package.js" not in inspection.files
    assert "escape.py" not in inspection.files
    assert inspection.modules == ("app",)


def test_source_limits_report_truncation_without_reading_past_cap(repo):
    for index in range(4):
        (repo / f"module_{index}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")

    inspection = SourceInspector(max_files=2, max_bytes=10_000).inspect(repo)

    assert inspection.truncated is True
    assert len(inspection.files) == 2


def test_relative_python_import_is_resolved_to_real_sibling_module(repo):
    package = repo / "demo"
    package.mkdir()
    (package / "dashboard.py").write_text("from .service import DiagramService\n", encoding="utf-8")
    (package / "service.py").write_text("class DiagramService: pass\n", encoding="utf-8")

    inspection = SourceInspector().inspect(repo)

    assert [(edge.source, edge.target) for edge in inspection.edges] == [
        ("demo.dashboard", "demo.service"),
    ]


def test_architecture_uses_bounded_star_geometry_and_labels(repo):
    package = repo / "extremely_long_package_name"
    package.mkdir()
    (package / "request_controller_with_long_name.py").write_text(
        "from .persistence_gateway_with_long_name import Store\n", encoding="utf-8")
    (package / "persistence_gateway_with_long_name.py").write_text("class Store: pass\n", encoding="utf-8")

    ir = ArchifyAuthor().build("architecture", SourceInspector().inspect(repo), "request")

    assert "layout" not in ir
    assert all("pos" in component and "size" in component for component in ir["components"])
    assert all(len(component["label"]) <= 28 for component in ir["components"])
    assert len(ir["connections"]) <= len(ir["components"]) - 1


def test_lifecycle_without_explicit_states_fails_instead_of_inventing(repo):
    (repo / "main.py").write_text("print('hello')\n", encoding="utf-8")

    with pytest.raises(ArchifyEvidenceError, match="INSUFFICIENT_EVIDENCE"):
        ArchifyAuthor().build("lifecycle", SourceInspector().inspect(repo), "")


def test_lifecycle_uses_only_explicit_state_enum_and_transition_map(repo):
    (repo / "job.py").write_text(
        "from enum import Enum\n"
        "class JobState(str, Enum):\n"
        "    QUEUED = 'queued'\n"
        "    RUNNING = 'running'\n"
        "    DONE = 'done'\n"
        "TRANSITIONS = {\n"
        "    'queued': ('running',),\n"
        "    'running': ('done',),\n"
        "}\n",
        encoding="utf-8",
    )

    ir = ArchifyAuthor().build("lifecycle", SourceInspector().inspect(repo), "")

    assert {state["label"] for state in ir["states"]} == {"queued", "running", "done"}
    assert {(edge["from"], edge["to"]) for edge in ir["transitions"]} == {
        ("queued", "running"), ("running", "done"),
    }
    assert [state["id"] for state in ir["states"]] == ["queued", "running", "done"]


@pytest.mark.parametrize("diagram_type", ["workflow", "sequence", "dataflow"])
def test_other_types_are_built_only_when_a_verified_edge_exists(repo, diagram_type):
    (repo / "source.py").write_text("import sink\n", encoding="utf-8")
    (repo / "sink.py").write_text("VALUE = 1\n", encoding="utf-8")

    ir = ArchifyAuthor().build(diagram_type, SourceInspector().inspect(repo), "")

    assert ir["diagram_type"] == diagram_type
    assert "source" in json.dumps(ir)
    assert "sink" in json.dumps(ir)
    if diagram_type == "workflow":
        assert "mainPath" not in ir
    if diagram_type == "sequence":
        assert min(message["y"] for message in ir["messages"]) >= 160
