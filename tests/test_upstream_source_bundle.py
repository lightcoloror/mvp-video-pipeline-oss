import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.object_scene_routes import recommend_scene_route, source_bundle_for_project
from video_creation_pipeline.upstream_source_bundle import inspect_source_bundle


def fixture(tmp_path):
    root = tmp_path / "upstream"
    root.mkdir()
    (root / "example.js").write_text("original", encoding="utf-8")
    value = {"schema": "video_creation_pipeline.upstream_source_bundle.v1", "id": "sample",
             "repo": "https://github.com/example/source", "commit": "a" * 40, "scope": "entire_tracked_archive",
             "root": str(root), "archive": "retained.tar", "archive_sha256": "b" * 64,
             "files": [{"path": "example.js", "bytes": 8, "sha256": hashlib.sha256(b"original").hexdigest()}],
             "file_count": 1, "native_indexes": [{"path": "example.js", "exists": True}]}
    manifest = tmp_path / "manifest.json"
    def save(data):
        manifest.write_text(json.dumps(data), encoding="utf-8")
        return {"manifest_path": str(manifest), "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "repo": value["repo"], "commit": value["commit"]}
    return root, value, save


def test_original_bytes_and_authority_are_distinct(tmp_path):
    root, value, save = fixture(tmp_path)
    result = inspect_source_bundle(save(value), verify_files=True, entry="example.js")
    assert result["status"] == "preserved_files_verified"
    assert not result["runtime_verified"] and not result["execution_authorized"]
    assert (root / "example.js").read_text() == "original"


@pytest.mark.parametrize("change", ["manifest", "file", "missing", "extra", "identity", "duplicate", "escape"])
def test_corruption_and_escape_are_rejected(tmp_path, change):
    root, value, save = fixture(tmp_path)
    reference = save(value)
    if change == "manifest":
        reference["manifest_sha256"] = "0" * 64
    elif change == "file":
        (root / "example.js").write_text("modified")
    elif change == "missing":
        (root / "example.js").unlink()
    elif change == "extra":
        (root / "extra.js").write_text("not tracked")
    elif change == "identity":
        reference["commit"] = "c" * 40
    elif change == "duplicate":
        value["files"].append(deepcopy(value["files"][0])); value["file_count"] = 2
        reference = save(value)
    else:
        value["files"][0]["path"] = "../outside.js"
        reference = save(value)
    with pytest.raises(ContractError):
        inspect_source_bundle(reference, verify_files=True)


def test_unknown_native_entry_does_not_guess(tmp_path):
    _, value, save = fixture(tmp_path)
    with pytest.raises(ContractError):
        inspect_source_bundle(save(value), entry="not-a-style")


@pytest.mark.parametrize("identity", ["lemo-opuscar", "video-shotcraft", "ai-design-skills", "pdoom-explainer",
                                    "openfilm-one-prompt", "ai-music-creator", "indieshade-video-director", "rea-video"])
def test_real_preserved_bundle_verification(identity):
    result = source_bundle_for_project(identity, verify_files=True)
    assert result["verified_file_count"] == result["file_count"]
    assert result["reuse_contract"]["read_native_index_without_rewriting"]


def test_native_style_and_prompt_code_links_are_retained():
    lemo = source_bundle_for_project("lemo-opuscar", entry="archive-lighttable")
    assert lemo["native_entry"]["slug"] == "archive-lighttable"
    assert any(row["upstream_path"].endswith("STYLE.md") for row in lemo["entry_files"])
    assert any(row["upstream_path"].endswith("timeline.js") for row in lemo["entry_files"])
    shot = source_bundle_for_project("video-shotcraft", entry="ai-stream-response")
    assert shot["native_entry"]["source"] == "references/shots/interaction/ai-stream-response.md"
    assert shot["native_entry"]["styles"] and not shot["native_relationships_rewritten"]
    assert any(row["upstream_path"].endswith(".tsx") for row in shot["entry_files"])


def test_named_sources_use_existing_route_and_never_execute():
    result = recommend_scene_route("flow", techniques=["continuous_transform"], source_projects=["pdoom-explainer", "openfilm-one-prompt"])
    rows = result["module_selection"]["source_layers"]["shot"]
    assert all(row["source_package"]["native_indexes"] for row in rows)
    assert all(not row["execution_authorized"] for row in rows)
    assert result["style_authority"].startswith("selected original style/example")
    assert all("explicit_source_technique_fit_not_reviewed" not in row["required_resolutions"] for row in rows)


def test_every_native_shot_card_keeps_its_multi_file_implementations():
    package = source_bundle_for_project("video-shotcraft")
    library = json.loads(Path(package["root"], "gallery/api/library.json").read_text(encoding="utf-8"))
    multiple = 0
    for card in library["cards"]:
        selected = source_bundle_for_project("video-shotcraft", entry=card["name"])
        assert selected["native_entry"] == card
        implementations = [row for row in selected["entry_files"] if row["upstream_path"].endswith(".tsx")]
        assert implementations, card["name"]
        multiple += len(implementations) > 1
    assert multiple >= 39
