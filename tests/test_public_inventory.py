from __future__ import annotations

from terminal_mcp.public_inventory import collect_public_inventory, sanitize_public_inventory


def test_collect_public_inventory_emits_only_project_and_skill_identifiers(tmp_path, monkeypatch):
    skills_root = tmp_path / '.codex' / 'skills'
    (skills_root / 'review').mkdir(parents=True)
    (skills_root / 'not a skill').mkdir()
    (skills_root / 'linked').symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setenv('HOME', str(tmp_path))
    alpha = tmp_path / 'work' / 'alpha'
    alpha.mkdir(parents=True)
    beta = tmp_path / 'work' / 'beta project'
    beta.mkdir()
    private_file = tmp_path / 'private' / 'path' / 'with' / 'secrets.txt'
    private_file.parent.mkdir(parents=True)
    private_file.write_text('sensitive contents')
    rows = [
        {'pane_current_path': str(alpha)},
        {'pane_current_path': str(alpha)},
        {'pane_current_path': str(beta)},
        {'pane_current_path': str(private_file)},
    ]
    result = collect_public_inventory(rows)
    assert result == {'schema_version': 1, 'project_ids': ['alpha'], 'skill_names': ['review']}
    serialized = str(result)
    for forbidden in ('/work', '/private', 'secrets.txt', 'HOME', 'token', 'contents'):
        assert forbidden not in serialized


def test_inventory_sanitizer_drops_unknown_fields_paths_and_hostile_values():
    result = sanitize_public_inventory({
        'schema_version': 1,
        'project_ids': ['alpha', '/home/alice/private', 'alpha', 'x' * 100],
        'skill_names': ['review', '../secret', 'review'],
        'prompt': 'do not store', 'environment': {'TOKEN': 'secret'},
    })
    assert result == {'schema_version': 1, 'project_ids': ['alpha'], 'skill_names': ['review']}


def test_inventory_sanitizer_rejects_unknown_schema():
    assert sanitize_public_inventory({'schema_version': 2, 'project_ids': ['alpha']}) == {
        'schema_version': 1, 'project_ids': [], 'skill_names': []}
