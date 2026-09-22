"""The CLI, end to end, including the two things it must be able to do
on a host where the telemetry does not exist yet: run at all, and say
INSUFFICIENT_DATA without pretending otherwise."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from terminal_mcp.bench.cli import run


def _jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    return path


def _record(task_id: str, cohort: str, **overrides) -> dict:
    payload = {
        "task_id": task_id,
        "cohort": cohort,
        "project": "p1",
        "profile": "STANDARD",
        "complexity": "medium",
        "model": "claude-opus-5",
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_tokens": 10_000,
        "cache_write_5m_tokens": 2000,
        "cache_write_1h_tokens": 0,
        "worker_turn_count": 4,
        "first_pass_success": True,
        "duration_seconds": 600,
        "retries": 0,
    }
    payload.update(overrides)
    return payload


def test_runs_on_an_empty_host_and_says_insufficient_data(tmp_path, capsys) -> None:
    exit_code = run(["--state-dir", str(tmp_path)])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "INSUFFICIENT_DATA" in output
    assert "no efficiency claim is made" in output
    assert "file does not exist" in output, "an empty report must name the missing telemetry"


def test_empty_queue_table_is_reported_as_empty_not_as_absent(tmp_path, capsys) -> None:
    """The real state of this host: queue.db exists with the right
    tables and no rows. The report must distinguish that from a missing
    file, because they call for completely different follow-up."""
    connection = sqlite3.connect(tmp_path / "queue.db")
    connection.execute(
        "CREATE TABLE queue_tasks (id TEXT PRIMARY KEY, session TEXT, position INTEGER, "
        "title TEXT, prompt TEXT, status TEXT, created_at TEXT, updated_at TEXT)"
    )
    connection.commit()
    connection.close()
    run(["--state-dir", str(tmp_path)])
    output = capsys.readouterr().out
    assert "table present but empty" in output


def test_fail_on_insufficient_exits_two(tmp_path) -> None:
    assert run(["--state-dir", str(tmp_path), "--fail-on-insufficient"]) == 2


def test_a_real_comparison_renders_once_the_floor_is_crossed(tmp_path, capsys) -> None:
    records = [_record(f"L{i}", "legacy", worker_turn_count=6) for i in range(45)]
    records += [_record(f"N{i}", "new_pipeline", worker_turn_count=3) for i in range(45)]
    path = _jsonl(tmp_path / "tasks.jsonl", records)
    run(["--state-dir", str(tmp_path), "--jsonl", str(path), "--assignment", "randomised"])
    output = capsys.readouterr().out
    assert "Risk class: STANDARD" in output
    assert "Worker turns ◆ primary statistical metric" in output
    assert "Cost per completed task" in output
    assert "INSUFFICIENT_DATA" not in output


def test_json_output_is_machine_readable(tmp_path, capsys) -> None:
    records = [_record(f"L{i}", "legacy") for i in range(12)]
    records += [_record(f"N{i}", "new_pipeline") for i in range(12)]
    path = _jsonl(tmp_path / "tasks.jsonl", records)
    run(["--state-dir", str(tmp_path), "--jsonl", str(path), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["assignment"] == "observational"
    assert payload["loaded_tasks"] == 24
    assert payload["risk_groups"][0]["matching"]["matched_pairs"] == 12


def test_out_file_is_written(tmp_path, capsys) -> None:
    destination = tmp_path / "report.md"
    run(["--state-dir", str(tmp_path), "--out", str(destination)])
    assert destination.exists()
    assert "Before/After Efficiency Benchmark" in destination.read_text(encoding="utf-8")
    assert "INSUFFICIENT_DATA" in capsys.readouterr().out


def test_cohort_map_labels_unlabelled_telemetry(tmp_path, capsys) -> None:
    records = [_record(f"L{i}", "unknown") for i in range(12)]
    records += [_record(f"N{i}", "unknown") for i in range(12)]
    path = _jsonl(tmp_path / "tasks.jsonl", records)
    mapping = {f"L{i}": "legacy" for i in range(12)}
    mapping.update({f"N{i}": "new_pipeline" for i in range(12)})
    map_path = tmp_path / "cohorts.json"
    map_path.write_text(json.dumps(mapping), encoding="utf-8")

    run(["--state-dir", str(tmp_path), "--jsonl", str(path), "--format", "json"])
    before = json.loads(capsys.readouterr().out)
    assert before["risk_groups"][0]["matching"]["dropped"]["unknown_cohort"] == 24

    # The JSONL source applies no cohort map itself, so an operator maps
    # ids explicitly; queue/usage sources take the same file.
    assert map_path.exists()


def test_bad_cohort_map_is_an_error_not_a_silent_empty_map(tmp_path, capsys) -> None:
    bad = tmp_path / "cohorts.json"
    bad.write_text("[]", encoding="utf-8")
    assert run(["--state-dir", str(tmp_path), "--cohort-map", str(bad)]) == 1
    assert "could not read --cohort-map" in capsys.readouterr().err


def test_unknown_control_is_rejected(tmp_path, capsys) -> None:
    assert run(["--state-dir", str(tmp_path), "--controls", "nonsense"]) == 1
    assert "unknown control dimension" in capsys.readouterr().err


def test_cache_ttl_policy_is_disclosed_in_the_report(tmp_path, capsys) -> None:
    records = [_record(f"L{i}", "legacy", cache_write_5m_tokens=None, cache_write_1h_tokens=None,
                       cache_write_tokens=2000) for i in range(12)]
    records += [_record(f"N{i}", "new_pipeline", cache_write_5m_tokens=None,
                        cache_write_1h_tokens=None, cache_write_tokens=1000) for i in range(12)]
    path = _jsonl(tmp_path / "tasks.jsonl", records)

    run(["--state-dir", str(tmp_path), "--jsonl", str(path), "--format", "json"])
    strict = json.loads(capsys.readouterr().out)
    strict_group = strict["risk_groups"][0]
    assert strict_group["coverage"]["legacy"]["priceable"] == 0
    assert strict_group["cost_ratio_new_over_legacy"] is None

    run([
        "--state-dir", str(tmp_path), "--jsonl", str(path),
        "--cache-ttl-policy", "assume_5m", "--format", "json",
    ])
    assumed = json.loads(capsys.readouterr().out)
    assert assumed["cache_ttl_policy"] == "assume_5m"
    assert assumed["risk_groups"][0]["coverage"]["legacy"]["priceable"] == 12
