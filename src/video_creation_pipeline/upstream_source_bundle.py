"""Read preserved upstream trees without rewriting native catalogs or running code."""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

from .contract_io import file_sha256, read_json_object
from .errors import ContractError


def inspect_source_bundle(reference: dict, *, verify_files: bool = False, entry: str | None = None) -> dict:
    manifest = Path(reference["manifest_path"])
    if not manifest.is_file() or manifest.is_symlink():
        raise ContractError("preserved upstream manifest missing or linked")
    if file_sha256(manifest) != reference["manifest_sha256"]:
        raise ContractError("preserved upstream manifest hash drift")
    value = read_json_object(manifest, "upstream source bundle")
    if value.get("schema") != "video_creation_pipeline.upstream_source_bundle.v1":
        raise ContractError("unsupported upstream source bundle")
    if value.get("commit") != reference["commit"] or value.get("repo") != reference["repo"]:
        raise ContractError("preserved upstream identity drift")
    root = Path(value["root"])
    if not root.is_dir() or root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise ContractError("preserved upstream root missing or linked")
    root = root.resolve()
    rows = value.get("files")
    if not isinstance(rows, list) or not rows or len(rows) > 25000 or value.get("file_count") != len(rows):
        raise ContractError("invalid preserved upstream file coverage")
    by_path = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise ContractError("invalid preserved upstream file row")
        name = row.get("path", "")
        part = PurePosixPath(name)
        if not name or "\\" in name or ":" in name or part.is_absolute() or ".." in part.parts:
            raise ContractError("unsafe preserved upstream relative path")
        if name in by_path:
            raise ContractError("duplicate preserved upstream file")
        by_path[name] = row

    def check(name: str) -> Path:
        path = root.joinpath(*PurePosixPath(name).parts)
        if not path.resolve().is_relative_to(root):
            raise ContractError("preserved upstream path escaped root")
        cursor = path
        while cursor != root:
            if cursor.is_symlink() or getattr(cursor, "is_junction", lambda: False)():
                raise ContractError("linked preserved upstream file path")
            cursor = cursor.parent
        if not path.is_file() or path.stat().st_size != by_path[name]["bytes"]:
            raise ContractError("preserved upstream file missing or size drift: " + name)
        if file_sha256(path) != by_path[name]["sha256"]:
            raise ContractError("preserved upstream file hash drift: " + name)
        return path

    indexes = []
    for index_entry in value.get("native_indexes", []):
        if not index_entry.get("exists"):
            continue
        name = index_entry["path"]
        if name not in by_path:
            raise ContractError("native index is outside preserved manifest")
        path = check(name)
        indexes.append({"upstream_path": name, "path": str(path), "sha256": by_path[name]["sha256"]})
    if verify_files:
        if sum(row["bytes"] for row in rows) > 1024 * 1024 * 1024:
            raise ContractError("source verification byte bound exceeded")
        for name in by_path:
            check(name)
        seen = set()
        for count, path in enumerate(root.rglob("*"), start=1):
            if count > 50000:
                raise ContractError("source verification item bound exceeded")
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ContractError("linked extra upstream path")
            if path.is_file():
                seen.add(path.relative_to(root).as_posix())
        if seen != set(by_path):
            raise ContractError("unexpected preserved upstream files")
    result = {
        "status": "preserved_files_verified" if verify_files else "native_indexes_verified_tree_not_rehashed",
        "manifest_path": str(manifest), "manifest_sha256": reference["manifest_sha256"],
        "repo": value["repo"], "commit": value["commit"], "scope": value["scope"],
        "root": str(root), "file_count": len(rows), "verified_file_count": len(rows) if verify_files else len(indexes),
        "native_indexes": indexes, "archive": value["archive"], "archive_sha256": value["archive_sha256"],
        "reuse_contract": {
            "read_native_index_without_rewriting": True,
            "read_full_selected_style_prompt_example_and_shared_dependencies": True,
            "keep_original_paths_and_identifiers": True,
            "adapt_outside_upstream_tree": True,
            "record_base_commit_and_local_patch": True,
            "reference_content_not_executable_instruction": True,
        },
        "runtime_verified": False, "execution_authorized": False,
        "external_dependencies_resolved": False,
    }
    if entry is not None:
        native = None
        prefix = ""
        if "styleboard/catalog.json" in by_path:
            catalog = json.loads(check("styleboard/catalog.json").read_text(encoding="utf-8"))
            native = next((row for row in catalog if row.get("slug") == entry), None)
            prefix = "styles/" + entry + "/"
        elif "gallery/api/library.json" in by_path:
            catalog = json.loads(check("gallery/api/library.json").read_text(encoding="utf-8"))
            native = next((row for row in catalog["cards"] if row.get("name") == entry), None)
        if native is not None:
            selected_paths = [name for name in by_path if prefix and name.startswith(prefix)]
            if native.get("source") in by_path:
                selected_paths.append(native["source"])
                from .remotion_skill_handoff import _reference_implementation
                implementation = _reference_implementation(check(native["source"]))
                implementations = [implementation] if implementation in by_path else _multiple_native_implementations(
                    check(native["source"]), by_path
                )
                if not implementations:
                    raise ContractError("native shot-card implementation missing from preserved tree")
                selected_paths.extend(implementations)
                result["native_implementation_reference"] = implementation
            result["native_entry"] = native
            result["entry_files"] = [{"upstream_path": name, "path": str(check(name))} for name in selected_paths]
            result["shared_dependency_root"] = str(root)
            result["native_relationships_rewritten"] = False
        elif entry in by_path:
            result["entry_file"] = {"upstream_path": entry, "path": str(check(entry))}
        else:
            raise ContractError("unknown native style/card/file: " + entry)
    return result


def _multiple_native_implementations(card: Path, files: dict) -> list[str]:
    # The existing single-file reader cannot resolve native "A.tsx / B.tsx" cards.
    # Keep the original section untouched and resolve only files present in its tree.
    text = card.read_text(encoding="utf-8")
    section = text.split("## 参考实现", 1)[1].split("\n## ", 1)[0]
    paths = re.findall(r"(?<![\w/])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:tsx|ts|jsx|js|mjs|html)\b", section)
    resolved = [name for name in paths if name in files]
    names = re.findall(r"(?<![\w./-])([A-Za-z_][\w.-]*\.(?:tsx|ts|jsx|js|mjs|html))\b", section)
    for name in names:
        matches = [p for p in files if PurePosixPath(p).name == name]
        if len(matches) == 1:
            resolved.append(matches[0])
    return list(dict.fromkeys(resolved))
