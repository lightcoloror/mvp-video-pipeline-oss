import pytest
from author_binding_fixture import make_case, approval_args
from video_creation_pipeline.author_bindings import build_author_binding_projection
from video_creation_pipeline.author_editor import preview_author_edit, apply_author_edit, export_author_editor
from video_creation_pipeline.contract_io import atomic_write_json
from video_creation_pipeline.errors import ContractError


def test_preview_confirm_and_stale_edit(tmp_path):
    root = make_case(tmp_path / "case")
    projection = build_author_binding_projection(root / "binding-spec.json", **approval_args(root))
    path = atomic_write_json(projection, root / "projection.json")
    editor_path = root / "editor.html"
    export_author_editor(path, editor_path)
    editor = editor_path.read_text(encoding="utf-8")
    assert "__EDITOR_DATA__" not in editor
    assert "connect-src 'none'" in editor
    assert '确认并导出编辑提案' in editor
    proposal = {"schema": "video_creation_pipeline.author_edit_proposal.v1",
                "source_spec": projection["source_spec"], "edits": [{
                    "cue_id": projection["cues"][0]["cue_id"], "temporal_expression": {
                        "kind": "at_for", "anchor": {"word_id": "word-001", "boundary": "start"},
                        "duration": {"value": 5, "unit": "seconds"}}}]}
    proposal_path = atomic_write_json(proposal, root / "proposal.json")
    preview = preview_author_edit(path, proposal)
    assert preview["changes"][0]["after_window"]["start_s"] == 1
    assert preview["storyboard_reapproval_required"]
    output = root / "draft.json"
    with pytest.raises(ContractError, match="confirm"):
        apply_author_edit(path, proposal_path, output)
    assert not output.exists()
    assert apply_author_edit(path, proposal_path, output, confirm=True)["status"] == "draft_written"
    from author_binding_fixture import fixture_approval
    from video_creation_pipeline.author_bindings import build_author_bound_motion_bundle
    args = approval_args(root)
    with pytest.raises(ContractError, match='different artifact'):
        build_author_bound_motion_bundle(output, root / 'stale-approval', **args)
    args['storyboard_approval_path'] = fixture_approval('storyboard', output, root / 'draft-approval.json')
    assert build_author_bound_motion_bundle(output, root / 'regenerated', **args)['motion_plan'].exists()
    with pytest.raises(ContractError, match="new file"):
        apply_author_edit(path, proposal_path, output, confirm=True)
    proposal["source_spec"] = dict(proposal["source_spec"], sha256="0" * 64)
    with pytest.raises(ContractError, match="current exact"):
        preview_author_edit(path, proposal)
