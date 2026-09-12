"""notes config loading + the one cross-module invariant that cannot be
enforced by an import: config.py deliberately keeps its own literal copy of
the servable MIME list (to keep its import graph narrow), so this file is
what stops the two from drifting apart.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from terminal_mcp.config import NotesConfig, _load_notes_config, _NOTES_SERVABLE_MIME_TYPES, load_config
from terminal_mcp.notes_service import CANONICAL_EXTENSIONS, DEFAULT_ALLOWED_MIME_TYPES


def test_the_servable_mime_list_never_drifts_from_the_sniffer():
    """If notes_service learns a new format, config.py's allowlist gate must
    learn it in the same commit -- otherwise an operator config that names the
    new type is rejected at startup for no good reason."""
    assert set(_NOTES_SERVABLE_MIME_TYPES) == set(CANONICAL_EXTENSIONS)
    assert set(DEFAULT_ALLOWED_MIME_TYPES) <= set(_NOTES_SERVABLE_MIME_TYPES)


def test_defaults_are_on_with_no_config_section_at_all():
    config = _load_notes_config({})
    assert config.enabled is True
    assert config.attachments_dir == ""
    assert config.max_attachment_bytes == 10 * 1024 * 1024
    # The source_path transport is OFF until an operator names a root.
    assert config.attachment_source_roots == ()


def test_a_non_dict_section_degrades_to_defaults():
    assert _load_notes_config("nonsense") == NotesConfig()
    assert _load_notes_config(None) == NotesConfig()


def test_a_full_section_loads(tmp_path):
    config = _load_notes_config({
        "enabled": False,
        "attachments_dir": str(tmp_path / "att"),
        "max_attachment_bytes": 2048,
        "allowed_mime_types": ["image/png", "image/webp"],
        "attachment_source_roots": [str(tmp_path / "inbox"), "~/Pictures"],
    })
    assert config.enabled is False
    assert config.attachments_dir == str(tmp_path / "att")
    assert config.max_attachment_bytes == 2048
    assert config.allowed_mime_types == ("image/png", "image/webp")
    assert config.attachment_source_roots == (str(tmp_path / "inbox"), "~/Pictures")


def test_a_single_string_is_accepted_where_a_list_is_expected():
    config = _load_notes_config({"allowed_mime_types": "image/png",
                                 "attachment_source_roots": "/srv/shots"})
    assert config.allowed_mime_types == ("image/png",)
    assert config.attachment_source_roots == ("/srv/shots",)


def test_mime_types_are_case_normalised():
    assert _load_notes_config({"allowed_mime_types": ["IMAGE/PNG"]}).allowed_mime_types == ("image/png",)


@pytest.mark.parametrize("raw,fragment", [
    ({"max_attachment_bytes": 0}, "must be positive"),
    ({"max_attachment_bytes": -1}, "must be positive"),
    ({"allowed_mime_types": []}, "at least one type"),
    ({"allowed_mime_types": ["application/pdf"]}, "cannot verify or serve"),
    ({"allowed_mime_types": ["image/svg+xml"]}, "cannot verify or serve"),
    ({"attachment_source_roots": ["relative/path"]}, "absolute paths"),
])
def test_bad_values_fail_loudly_at_load_time(raw, fragment):
    with pytest.raises(ValueError) as excinfo:
        _load_notes_config(raw)
    assert fragment in str(excinfo.value)


def test_a_tilde_root_counts_as_absolute():
    # expanduser makes it absolute, which is what the server will actually
    # open -- rejecting it would be wrong.
    assert _load_notes_config({"attachment_source_roots": ["~/Pictures"]}).attachment_source_roots


def test_the_repo_config_yaml_still_loads_with_the_new_section(tmp_path):
    """The section is optional: the committed config.yaml/config.example.yaml
    must keep loading unchanged."""
    for name in ("config.yaml", "config.example.yaml"):
        path = Path(__file__).resolve().parents[1] / name
        if not path.exists():
            continue
        config = load_config(path)
        assert config.notes.enabled in (True, False)


def test_a_yaml_section_round_trips_through_load_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "permissions": {"allow_read": True},
        "allowed_session_patterns": ["test-*"],
        "notes": {"enabled": True, "max_attachment_bytes": 4096,
                  "attachment_source_roots": [str(tmp_path)]},
    }), encoding="utf-8")
    config = load_config(path)
    assert config.notes.max_attachment_bytes == 4096
    assert config.notes.attachment_source_roots == (str(tmp_path),)
