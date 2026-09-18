from __future__ import annotations

import pytest

from terminal_mcp.config import load_config


def test_queue_project_feed_config_parses(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
queue:
  enabled: true
  drain_enabled: true
  drain_batch_size: 12
  project_feeds:
    - project_id: novaretail
      lane: linux-codex-work
      owner: linux-codex-work
      registry_path: /tmp/novaretail/TASKS.json
      preferred_task_ids: [NWR-UI-V3-POLISH-001]
""",
        encoding="utf-8",
    )
    loaded = load_config(cfg)
    assert loaded.queue.enabled is True
    assert loaded.queue.drain_enabled is True
    assert loaded.queue.drain_batch_size == 12
    assert len(loaded.queue.project_feeds) == 1
    feed = loaded.queue.project_feeds[0]
    assert feed.project_id == "novaretail"
    assert feed.lane == "linux-codex-work"
    assert feed.preferred_task_ids == ("NWR-UI-V3-POLISH-001",)


def test_queue_project_feed_rejects_non_work_lane(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
queue:
  project_feeds:
    - project_id: novaretail
      lane: linux-codex
      registry_path: /tmp/novaretail/TASKS.json
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="-work"):
        load_config(cfg)


def test_queue_project_feed_explicit_target_allows_non_work_name_and_affinity(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
queue:
  project_feeds:
    - project_id: novaretail
      lane: codex1
      target_session: codex1
      target_node_id: dell-5530
      allowed_agent_types: [codex]
      infer_task_size: true
      registry_path: /tmp/novaretail/TASKS.json
""", encoding="utf-8")
    loaded = load_config(cfg)
    feed = loaded.queue.project_feeds[0]
    assert feed.target_session == "codex1"
    assert feed.target_node_id == "dell-5530"
    assert feed.allowed_agent_types == ("codex",)
    assert feed.infer_task_size is True
