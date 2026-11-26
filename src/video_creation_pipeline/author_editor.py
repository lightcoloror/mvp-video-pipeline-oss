"""Revision-bound local timing edits; a confirmed edit is still a draft."""
from copy import deepcopy
from pathlib import Path
from .contract_io import read_json_object, validate_artifact_reference, artifact_reference, atomic_write_json
from .author_bindings import validate_author_binding_projection
from .temporal_expressions import resolve_window
from .errors import ContractError
import json


def export_author_editor(projection_path, output_path):
    projection = read_json_object(projection_path, "author projection")
    validate_author_binding_projection(projection)
    spec = read_json_object(validate_artifact_reference(projection["source_spec"], "spec"), "spec")
    alignment = read_json_object(validate_artifact_reference(spec["source_word_alignment"], "alignment"), "alignment")
    data = {"source_spec": projection["source_spec"], "cues": projection["cues"],
            "words": [w for w in alignment["words"] if
                      w["start_ms"] >= spec["recording_start_s"] * 1000 and
                      w["end_ms"] <= spec["recording_end_s"] * 1000],
            "fps": spec["output"]["fps"], "origin": spec["recording_start_s"],
            "duration": projection["output"]["duration_s"]}
    payload = json.dumps(data, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = Path(__file__).with_suffix(".html").read_text(encoding="utf-8")
    target = Path(output_path)
    if target.exists():
        raise ContractError("editor output must be a new file")
    target.write_text(template.replace("__EDITOR_DATA__", payload), encoding="utf-8")
    return {"status": "local_editor_exported", "editor": artifact_reference(target, "local editor"),
            "network_required": False, "source_modified": False}


def preview_author_edit(projection_path, proposal):
    projection = read_json_object(projection_path, "author projection")
    validate_author_binding_projection(projection)
    if proposal.get("schema") != "video_creation_pipeline.author_edit_proposal.v1" or proposal.get("source_spec") != projection["source_spec"]:
        raise ContractError("edit proposal must bind the current exact source spec")
    spec = read_json_object(validate_artifact_reference(projection["source_spec"], "spec"), "spec")
    alignment = read_json_object(validate_artifact_reference(spec["source_word_alignment"], "alignment"), "alignment")
    words = {word["word_id"]: word for word in alignment["words"]}
    edits = proposal.get("edits")
    if not isinstance(edits, list) or not edits:
        raise ContractError("edit proposal requires edits")
    draft = deepcopy(spec)
    bindings = {b["cue"]["cue_id"]: b for b in draft["cue_bindings"]}
    old = {p["cue_id"]: p for p in projection["positions"]}
    changes, seen = [], set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"cue_id", "temporal_expression"}:
            raise ContractError("only explicit cue timing edits are supported")
        cue_id = edit["cue_id"]
        if cue_id not in bindings or cue_id in seen:
            raise ContractError("edit cue identity is unknown or duplicated")
        seen.add(cue_id)
        expression = edit["temporal_expression"]
        window = resolve_window(expression, words, fps=spec["output"]["fps"],
            recording_start_s=spec["recording_start_s"], recording_end_s=spec["recording_end_s"])
        binding = bindings[cue_id]
        cue = binding["cue"]
        transitions = sum(cue["timing"][name] for name in ("full_frame_s", "enter_s", "exit_s"))
        if window["end_s"] - window["start_s"] < transitions:
            raise ContractError("edited duration is shorter than motion transitions")
        cue["timing"].pop("hold_s", None)
        binding.clear()
        binding.update(cue=cue, temporal_expression=deepcopy(expression))
        changes.append({"cue_id": cue_id, "before_local_s": old[cue_id]["segment_local_time_s"],
                        "after_window": window})
    return {"source_projection": artifact_reference(projection_path, "author projection"),
            "changes": changes, "draft_spec": draft, "storyboard_reapproval_required": True,
            "render_performed": False}


def apply_author_edit(projection_path, proposal_path, output_path, *, confirm=False):
    if not confirm:
        raise ContractError("confirm the timing preview before writing the draft")
    preview = preview_author_edit(projection_path, read_json_object(proposal_path, "edit proposal"))
    target = Path(output_path)
    if target.exists():
        raise ContractError("draft output must be a new file; source files are never overwritten")
    atomic_write_json(preview["draft_spec"], target)
    return {"status": "draft_written", "draft_spec": artifact_reference(target, "edited author spec"),
            "changes": preview["changes"], "storyboard_reapproval_required": True}
