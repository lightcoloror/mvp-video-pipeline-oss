"""Hash-bound adapters for external Remotion components and ClipKit SFX.

The module deliberately keeps upstream code outside this repository.  It reads
structured registries, emits selection contracts, and executes ClipKit's own
DSP source through Node's type-stripping runtime in a temporary staging area.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    file_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .semantic_motion import validate_semantic_motion_plan
from .stock_media import (
    build_licensed_media_asset,
    build_sound_effect_profile,
    validate_sound_effect_profile,
    write_stock_media_contract,
)

EXTERNAL_MOTION_CATALOG_SCHEMA = "video_creation_pipeline.external_motion_catalog.v1"
EXTERNAL_MOTION_SELECTION_PLAN_SCHEMA = (
    "video_creation_pipeline.external_motion_selection_plan.v1"
)
CLIPKIT_SFX_PACK_RECEIPT_SCHEMA = "video_creation_pipeline.clipkit_sfx_pack_receipt.v1"

ONDA_REPOSITORY = "https://github.com/remotion-dev/onda"
ONDA_COMMIT = "3c814051269597b31fee1603bef7ccfb93d091b6"
REMOTION_BITS_REPOSITORY = "https://github.com/remotion-dev/remotion-bits"
REMOTION_BITS_COMMIT = "6c71169aa061f15313fadbdc6e29a3a3a87f2c03"
CLIPKIT_REPOSITORY = "https://github.com/clipkit-video/clipkit"
CLIPKIT_COMMIT = "dcc26e5bfb170beee8681ecafdeb786da455606e"
CLIPKIT_PACKAGE_VERSION = "1.0.0"
CLIPKIT_PACK_ID = "clipkit-restrained-v1"

_ONDA_ROLE_DEFAULTS = {
    "structure": "chapter-card",
    "relationship": "node-graph",
    "sequence": "timeline",
    "comparison": "split-screen",
    "number": "count-up",
    "evidence": "highlight",
}
_REMOTION_BITS_ROLE_DEFAULTS = {
    "structure": "bit-staggered-fade-in",
    "relationship": "bit-card-stack",
    "sequence": "bit-list-reveal",
    "comparison": "bit-mosaic-reframe",
    "number": "bit-basic-counter",
    "evidence": "bit-ken-burns",
}
_ROLE_SFX_DEFAULTS = {
    "structure": "clipkit-soft-whoosh",
    "relationship": "clipkit-soft-sweep",
    "sequence": "clipkit-soft-tick",
    "comparison": "clipkit-soft-whoosh",
    "number": "clipkit-soft-tick",
    "evidence": "clipkit-soft-pop",
}
_CLIPKIT_ITEMS = (
    {
        "asset_id": "clipkit-soft-whoosh",
        "source_name": "whoosh",
        "seed": 1701,
        "tune": 180,
        "category": "restrained_transition",
        "tags": ["orientation", "soft", "transition", "whoosh"],
    },
    {
        "asset_id": "clipkit-soft-sweep",
        "source_name": "sweep",
        "seed": 1702,
        "tune": 180,
        "category": "restrained_relationship",
        "tags": ["causality", "relationship", "soft", "transition"],
    },
    {
        "asset_id": "clipkit-soft-impact",
        "source_name": "impact",
        "seed": 1703,
        "tune": 95,
        "category": "restrained_caution",
        "tags": ["caution", "emphasis", "low", "soft"],
    },
    {
        "asset_id": "clipkit-soft-pop",
        "source_name": "pop",
        "seed": 1704,
        "tune": 180,
        "category": "restrained_emphasis",
        "tags": ["commitment", "emphasis", "resolution", "soft"],
    },
    {
        "asset_id": "clipkit-soft-tick",
        "source_name": "tick",
        "seed": 1705,
        "tune": 160,
        "category": "restrained_marker",
        "tags": ["causality", "commitment", "emphasis", "soft", "tick"],
    },
    {
        "asset_id": "clipkit-soft-downlifter",
        "source_name": "downlifter",
        "seed": 1706,
        "tune": 160,
        "category": "restrained_resolution",
        "tags": ["resolution", "soft", "transition"],
    },
)
_CLIPKIT_ALLOWED_NAMES = {str(row["source_name"]) for row in _CLIPKIT_ITEMS}
_CLIPKIT_SOURCE_FILES = (
    "catalog.ts",
    "encode-wav.ts",
    "finish.ts",
    "index.ts",
    "sfx.ts",
)

_NODE_RUNNER = r"""
import {writeFileSync} from 'node:fs';
import {listSfx, renderSfx} from './catalog.ts';
import {encodeWav} from './encode-wav.ts';

const [action, name, seedText, tuneText, output] = process.argv.slice(2);
if (action === 'list') {
  console.log(JSON.stringify(listSfx().map(({name, label, category, tags}) => ({
    name, label, category, tags,
  }))));
  process.exit(0);
}
if (action !== 'generate' || !name || !output) {
  throw new Error('usage: list | generate <name> <seed> <tune> <output>');
}
const audio = renderSfx(name, {
  seed: Number(seedText),
  tune: Number(tuneText),
  finish: {transient: 0.10, air: 0.08, drive: 0.08, parallel: 0.05, width: 0.08},
});
if (!audio) throw new Error(`unknown ClipKit SFX: ${name}`);
writeFileSync(output, encodeWav(audio));
console.log(JSON.stringify({
  name,
  sampleRate: audio.sampleRate,
  frames: Math.min(audio.left.length, audio.right.length),
  output,
}));
""".strip()


def external_motion_assets_doctor(
    *,
    onda_root: str | Path | None = None,
    remotion_bits_root: str | Path | None = None,
    clipkit_root: str | Path | None = None,
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for name, root, inspector in (
        ("onda", onda_root, _inspect_onda),
        ("remotion_bits", remotion_bits_root, _inspect_remotion_bits),
        ("clipkit_sfx", clipkit_root, _inspect_clipkit),
    ):
        if root is None:
            checks[name] = {"status": "not_supplied"}
            continue
        try:
            snapshot = inspector(Path(root))
            checks[name] = {
                "status": "ready_metadata_only",
                "source_commit": snapshot["source_commit"],
                "item_count": snapshot["item_count"],
            }
        except ContractError as exc:
            checks[name] = {"status": "blocked", "reason": str(exc)}
    supplied = [row for row in checks.values() if row["status"] != "not_supplied"]
    status = (
        "blocked"
        if any(row["status"] == "blocked" for row in supplied)
        else "ready_metadata_only"
        if supplied
        else "not_supplied"
    )
    return {
        "schema": "video_creation_pipeline.external_motion_assets_doctor.v1",
        "status": status,
        "checks": checks,
        "selection_policy": [
            "explicit_override",
            "semantic_role",
            "template_default",
            "no_sfx",
        ],
        "arbitrary_fallback": False,
        "network_execution_performed": False,
        "source_code_vendored": False,
        "candidate_only": True,
    }


def build_external_motion_catalog(
    onda_root: str | Path,
    *,
    remotion_bits_root: str | Path | None = None,
    clipkit_root: str | Path | None = None,
) -> dict[str, Any]:
    payload = _external_motion_catalog_snapshot(
        Path(onda_root),
        Path(remotion_bits_root) if remotion_bits_root is not None else None,
        Path(clipkit_root) if clipkit_root is not None else None,
    )
    payload["catalog_sha256"] = payload_sha256(payload, "catalog_sha256")
    validate_external_motion_catalog(payload)
    return payload


def validate_external_motion_catalog(payload: dict[str, Any]) -> None:
    if payload.get("schema") != EXTERNAL_MOTION_CATALOG_SCHEMA:
        raise ContractError("unsupported external motion catalog schema")
    if payload.get("catalog_sha256") != payload_sha256(payload, "catalog_sha256"):
        raise ContractError("external motion catalog integrity check failed")
    sources = _object(payload.get("sources"))
    onda_root = Path(str(_object(sources.get("onda")).get("source_root") or ""))
    bits = sources.get("remotion_bits")
    clipkit = sources.get("clipkit_sfx")
    expected = _external_motion_catalog_snapshot(
        onda_root,
        (
            Path(str(_object(bits).get("source_root") or ""))
            if isinstance(bits, dict)
            else None
        ),
        (
            Path(str(_object(clipkit).get("source_root") or ""))
            if isinstance(clipkit, dict)
            else None
        ),
    )
    actual = {key: value for key, value in payload.items() if key != "catalog_sha256"}
    if actual != expected:
        raise ContractError("external motion catalog drifted from reviewed sources")


def build_external_motion_selection_plan(
    catalog_path: str | Path,
    semantic_motion_plan_path: str | Path,
    *,
    overrides_path: str | Path | None = None,
) -> dict[str, Any]:
    catalog_file = Path(catalog_path).expanduser().resolve()
    catalog = read_json_object(catalog_file, "external motion catalog")
    validate_external_motion_catalog(catalog)
    semantic_file = Path(semantic_motion_plan_path).expanduser().resolve()
    semantic = read_json_object(semantic_file, "semantic motion plan")
    validate_semantic_motion_plan(semantic)
    overrides, override_reference = _load_overrides(overrides_path)
    available = {
        (str(row["source_id"]), str(row["item_id"])): row
        for row in catalog["motion_items"]
    }
    clipkit_available = "clipkit_sfx" in _object(catalog.get("sources"))
    selections: list[dict[str, Any]] = []
    for raw in semantic.get("cues") or []:
        cue = _object(raw)
        cue_id = _required_text(cue.get("cue_id"), "semantic motion cue id")
        role = str(cue.get("information_role") or "evidence")
        override = overrides.get(cue_id)
        if override is not None:
            source_id = _required_text(override.get("source_id"), "override source")
            item_id = _required_text(override.get("item_id"), "override item")
            basis = "explicit_override"
        else:
            source_id = "onda"
            item_id = _ONDA_ROLE_DEFAULTS.get(role, "highlight")
            basis = "semantic_role"
            if (source_id, item_id) not in available:
                source_id = "remotion_bits"
                item_id = _REMOTION_BITS_ROLE_DEFAULTS.get(role, "bit-fade-in")
                basis = "template_default"
        item = available.get((source_id, item_id))
        if item is None:
            raise ContractError(
                f"no reviewed external motion item for {cue_id}: {source_id}/{item_id}"
            )
        sfx_asset_id = None
        sfx_basis = "no_sfx"
        if override is not None and override.get("sfx_asset_id") is not None:
            sfx_asset_id = _required_text(
                override.get("sfx_asset_id"), "override SFX asset"
            )
            sfx_basis = "explicit_override"
        elif clipkit_available:
            sfx_asset_id = _ROLE_SFX_DEFAULTS.get(role)
            sfx_basis = "template_default" if sfx_asset_id else "no_sfx"
        if sfx_asset_id is not None and sfx_asset_id not in {
            str(row["asset_id"]) for row in _CLIPKIT_ITEMS
        }:
            raise ContractError("external motion SFX override is not approved")
        selections.append(
            {
                "cue_id": cue_id,
                "story_segment_ref": str(cue.get("story_segment_ref") or ""),
                "information_role": role,
                "motion_component": {
                    "source_id": source_id,
                    "item_id": item_id,
                    "title": item["title"],
                    "selection_basis": basis,
                    "source_item": item["source_item"],
                },
                "sound_effect": {
                    "asset_id": sfx_asset_id,
                    "selection_basis": sfx_basis,
                    "central_timeline_cue_required": sfx_asset_id is not None,
                    "component_embedded_audio_forbidden": True,
                },
            }
        )
    payload: dict[str, Any] = {
        "schema": EXTERNAL_MOTION_SELECTION_PLAN_SCHEMA,
        "source_catalog": artifact_reference(catalog_file, "external motion catalog"),
        "source_semantic_motion_plan": artifact_reference(
            semantic_file, "semantic motion plan"
        ),
        "overrides": override_reference,
        "selections": selections,
        "selection_policy": {
            "order": [
                "explicit_override",
                "semantic_role",
                "template_default",
                "no_sfx",
            ],
            "arbitrary_fallback": False,
            "component_embedded_audio": False,
            "central_timeline_audio": True,
        },
        "authority_boundary": {
            "candidate_only": True,
            "source_timeline_modified": False,
            "renderer_replaced": False,
            "second_ffmpeg_outlet_created": False,
            "automatic_download": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    payload["plan_sha256"] = payload_sha256(payload, "plan_sha256")
    validate_external_motion_selection_plan(payload)
    return payload


def validate_external_motion_selection_plan(payload: dict[str, Any]) -> None:
    if payload.get("schema") != EXTERNAL_MOTION_SELECTION_PLAN_SCHEMA:
        raise ContractError("unsupported external motion selection plan schema")
    if payload.get("plan_sha256") != payload_sha256(payload, "plan_sha256"):
        raise ContractError("external motion selection plan integrity check failed")
    catalog_path = validate_artifact_reference(
        _object(payload.get("source_catalog")), "external motion catalog"
    )
    catalog = read_json_object(catalog_path, "external motion catalog")
    validate_external_motion_catalog(catalog)
    semantic_path = validate_artifact_reference(
        _object(payload.get("source_semantic_motion_plan")), "semantic motion plan"
    )
    semantic = read_json_object(semantic_path, "semantic motion plan")
    validate_semantic_motion_plan(semantic)
    if payload.get("overrides") is not None:
        validate_artifact_reference(_object(payload["overrides"]), "motion overrides")
    available = {
        (str(row["source_id"]), str(row["item_id"])): row
        for row in catalog["motion_items"]
    }
    expected_cues = {
        _required_text(_object(row).get("cue_id"), "semantic motion cue id"): _object(
            row
        )
        for row in semantic.get("cues") or []
    }
    rows = payload.get("selections")
    if not isinstance(rows, list) or len(rows) != len(expected_cues):
        raise ContractError("external motion selection set is incomplete")
    seen: set[str] = set()
    approved_sfx = {str(row["asset_id"]) for row in _CLIPKIT_ITEMS}
    for raw in rows:
        row = _object(raw)
        cue_id = _required_text(row.get("cue_id"), "external motion cue id")
        cue = expected_cues.get(cue_id)
        if cue is None or cue_id in seen:
            raise ContractError("external motion selection cue set drifted")
        seen.add(cue_id)
        if row.get("story_segment_ref") != cue.get("story_segment_ref") or row.get(
            "information_role"
        ) != cue.get("information_role"):
            raise ContractError("external motion semantic cue binding drifted")
        component = _object(row.get("motion_component"))
        key = (
            str(component.get("source_id") or ""),
            str(component.get("item_id") or ""),
        )
        item = available.get(key)
        if item is None or component.get("source_item") != item.get("source_item"):
            raise ContractError("external motion component source drifted")
        if component.get("selection_basis") not in {
            "explicit_override",
            "semantic_role",
            "template_default",
        }:
            raise ContractError("external motion selection basis is invalid")
        sound = _object(row.get("sound_effect"))
        asset_id = sound.get("asset_id")
        if asset_id is not None and asset_id not in approved_sfx:
            raise ContractError("external motion sound choice is not approved")
        if sound != {
            "asset_id": asset_id,
            "selection_basis": sound.get("selection_basis"),
            "central_timeline_cue_required": asset_id is not None,
            "component_embedded_audio_forbidden": True,
        } or sound.get("selection_basis") not in {
            "explicit_override",
            "template_default",
            "no_sfx",
        }:
            raise ContractError("external motion sound policy is invalid")
    if seen != set(expected_cues):
        raise ContractError("external motion selection cues are incomplete")
    if payload.get("selection_policy") != {
        "order": [
            "explicit_override",
            "semantic_role",
            "template_default",
            "no_sfx",
        ],
        "arbitrary_fallback": False,
        "component_embedded_audio": False,
        "central_timeline_audio": True,
    }:
        raise ContractError("external motion selection policy is invalid")
    if payload.get("authority_boundary") != {
        "candidate_only": True,
        "source_timeline_modified": False,
        "renderer_replaced": False,
        "second_ffmpeg_outlet_created": False,
        "automatic_download": False,
        "external_provider_called": False,
        "upload_performed": False,
        "publish_performed": False,
    }:
        raise ContractError("external motion selection boundary is invalid")


def bootstrap_clipkit_sfx_pack(
    clipkit_root: str | Path,
    output_root: str | Path,
    *,
    purpose: str = "commercial",
    platform: str = "social_media",
) -> dict[str, Any]:
    source = _inspect_clipkit(Path(clipkit_root))
    root = Path(output_root).expanduser().resolve()
    owner = {
        "schema": "video_creation_pipeline.clipkit_sfx_pack_owner.v1",
        "pack_id": CLIPKIT_PACK_ID,
        "source_commit": CLIPKIT_COMMIT,
        "managed_outputs_only": True,
        "user_media_overwrite_allowed": False,
    }
    _prepare_managed_root(root, owner, ".clipkit-sfx-pack-owner.json")
    license_source = Path(source["license_file"]["path"])
    license_snapshot = root / "license" / "CLIPKIT-APACHE-2.0.txt"
    _copy_exact(license_source, license_snapshot, "ClipKit license")
    profiles: list[Path] = []
    assets: list[dict[str, Any]] = []
    for item in _CLIPKIT_ITEMS:
        media = root / "assets" / f"{item['asset_id']}.wav"
        generation = _run_clipkit(
            Path(clipkit_root),
            action="generate",
            name=str(item["source_name"]),
            seed=int(item["seed"]),
            tune=int(item["tune"]),
            output=media,
        )
        asset = build_licensed_media_asset(
            media,
            license_snapshot,
            {
                "asset_id": item["asset_id"],
                "media_type": "sound_effect",
                "source_id": "clipkit_sfx",
                "item_url": (
                    f"{CLIPKIT_REPOSITORY}/tree/{CLIPKIT_COMMIT}/packages/sfx"
                ),
                "license_id": "apache-2.0",
                "title": f"ClipKit {item['source_name']}",
                "author": "ClipKit contributors",
                "description": (
                    "Deterministic restrained local synthesis from the reviewed "
                    "ClipKit SFX engine."
                ),
                "tags": item["tags"],
                "download_method": "local_generation",
                "downloaded_at": "2026-09-01T00:00:00+08:00",
                "attribution_text": (
                    "Generated with @clipkit/sfx 1.0.0; Apache-2.0 source notice retained."
                ),
            },
            purpose=purpose,
            platform=platform,
        )
        manifest_path = root / "manifests" / f"{item['asset_id']}.asset.json"
        write_stock_media_contract(asset, manifest_path)
        duration_s = _wav_duration(media)
        profile = build_sound_effect_profile(
            manifest_path,
            duration_s=duration_s,
            category=str(item["category"]),
            tags=item["tags"],
            description=(
                "Restrained deterministic cue; audition before release and keep on "
                "the central motion-SFX timeline."
            ),
        )
        profile_path = root / "profiles" / f"{item['asset_id']}.profile.json"
        write_stock_media_contract(profile, profile_path)
        profiles.append(profile_path)
        assets.append(
            {
                "asset_id": item["asset_id"],
                "source_name": item["source_name"],
                "seed": item["seed"],
                "tune": item["tune"],
                "generation": generation,
                "source_sha256": file_sha256(media),
                "profile": artifact_reference(profile_path, "sound effect profile"),
                "profile_sha256": profile["profile_sha256"],
            }
        )
    payload: dict[str, Any] = {
        "schema": CLIPKIT_SFX_PACK_RECEIPT_SCHEMA,
        "pack_id": CLIPKIT_PACK_ID,
        "source": source,
        "license_snapshot": artifact_reference(
            license_snapshot, "ClipKit license snapshot"
        ),
        "purpose": purpose,
        "platform": platform,
        "assets": sorted(assets, key=lambda row: str(row["asset_id"])),
        "selection_policy": {
            "approved_names": sorted(_CLIPKIT_ALLOWED_NAMES),
            "game_feedback_defaults_forbidden": True,
            "arbitrary_fallback": False,
            "automatic_download": False,
            "audition_required": True,
        },
        "authority_boundary": {
            "candidate_assets_only": True,
            "source_repository_modified": False,
            "source_code_vendored": False,
            "second_audio_timeline_created": False,
            "automatic_download": False,
            "external_provider_called": False,
            "upload_performed": False,
            "publish_performed": False,
        },
    }
    payload["pack_sha256"] = payload_sha256(payload, "pack_sha256")
    validate_clipkit_sfx_pack_receipt(payload)
    receipt = atomic_write_json(payload, root / "clipkit-sfx-pack-receipt.json")
    return {"receipt": receipt, "profiles": profiles, "payload": payload}


def validate_clipkit_sfx_pack_receipt(payload: dict[str, Any]) -> None:
    if payload.get("schema") != CLIPKIT_SFX_PACK_RECEIPT_SCHEMA:
        raise ContractError("unsupported ClipKit SFX pack receipt schema")
    if payload.get("pack_sha256") != payload_sha256(payload, "pack_sha256"):
        raise ContractError("ClipKit SFX pack integrity check failed")
    if payload.get("pack_id") != CLIPKIT_PACK_ID:
        raise ContractError("ClipKit SFX pack id drifted")
    source = _object(payload.get("source"))
    current = _inspect_clipkit(Path(str(source.get("source_root") or "")))
    if source != current:
        raise ContractError("ClipKit SFX source drifted")
    validate_artifact_reference(
        _object(payload.get("license_snapshot")), "ClipKit license snapshot"
    )
    rows = payload.get("assets")
    if not isinstance(rows, list) or len(rows) != len(_CLIPKIT_ITEMS):
        raise ContractError("ClipKit SFX pack asset set is incomplete")
    expected_ids = {str(row["asset_id"]) for row in _CLIPKIT_ITEMS}
    if {str(_object(row).get("asset_id")) for row in rows} != expected_ids:
        raise ContractError("ClipKit SFX pack asset ids drifted")
    for raw in rows:
        row = _object(raw)
        profile_path = validate_artifact_reference(
            _object(row.get("profile")), "sound effect profile"
        )
        profile = read_json_object(profile_path, "sound effect profile")
        validate_sound_effect_profile(profile)
        if (
            profile.get("asset_id") != row.get("asset_id")
            or profile.get("profile_sha256") != row.get("profile_sha256")
            or _object(profile.get("source_audio")).get("sha256")
            != row.get("source_sha256")
        ):
            raise ContractError("ClipKit SFX profile binding drifted")
    if payload.get("selection_policy") != {
        "approved_names": sorted(_CLIPKIT_ALLOWED_NAMES),
        "game_feedback_defaults_forbidden": True,
        "arbitrary_fallback": False,
        "automatic_download": False,
        "audition_required": True,
    }:
        raise ContractError("ClipKit SFX selection policy drifted")
    if payload.get("authority_boundary") != {
        "candidate_assets_only": True,
        "source_repository_modified": False,
        "source_code_vendored": False,
        "second_audio_timeline_created": False,
        "automatic_download": False,
        "external_provider_called": False,
        "upload_performed": False,
        "publish_performed": False,
    }:
        raise ContractError("ClipKit SFX pack boundary is invalid")


def profile_paths_from_clipkit_pack_receipt(
    receipt_path: str | Path,
) -> list[Path]:
    payload = read_json_object(receipt_path, "ClipKit SFX pack receipt")
    validate_clipkit_sfx_pack_receipt(payload)
    return [
        Path(str(_object(row["profile"])["path"])).resolve()
        for row in payload["assets"]
    ]


def write_external_motion_contract(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    validate_external_motion_contract(payload)
    return atomic_write_json(payload, output_path)


def validate_external_motion_contract(payload: dict[str, Any]) -> None:
    validators = {
        EXTERNAL_MOTION_CATALOG_SCHEMA: validate_external_motion_catalog,
        EXTERNAL_MOTION_SELECTION_PLAN_SCHEMA: validate_external_motion_selection_plan,
        CLIPKIT_SFX_PACK_RECEIPT_SCHEMA: validate_clipkit_sfx_pack_receipt,
    }
    validator = validators.get(str(payload.get("schema") or ""))
    if validator is None:
        raise ContractError("unsupported external motion asset contract")
    validator(payload)


def _external_motion_catalog_snapshot(
    onda_root: Path,
    remotion_bits_root: Path | None,
    clipkit_root: Path | None,
) -> dict[str, Any]:
    onda = _inspect_onda(onda_root)
    sources: dict[str, Any] = {"onda": onda}
    motion_items = list(onda["items"])
    if remotion_bits_root is not None:
        bits = _inspect_remotion_bits(remotion_bits_root)
        sources["remotion_bits"] = bits
        motion_items.extend(bits["items"])
    sfx_items: list[dict[str, Any]] = []
    if clipkit_root is not None:
        clipkit = _inspect_clipkit(clipkit_root)
        sources["clipkit_sfx"] = clipkit
        sfx_items = [
            {
                "asset_id": row["asset_id"],
                "source_name": row["source_name"],
                "category": row["category"],
                "tags": list(row["tags"]),
                "default_seed": row["seed"],
                "default_tune": row["tune"],
                "execution_mode": "fixed_source_local_dsp",
            }
            for row in _CLIPKIT_ITEMS
        ]
    return {
        "schema": EXTERNAL_MOTION_CATALOG_SCHEMA,
        "sources": sources,
        "motion_items": sorted(
            motion_items, key=lambda row: (str(row["source_id"]), str(row["item_id"]))
        ),
        "sound_items": sorted(sfx_items, key=lambda row: str(row["asset_id"])),
        "counts": {
            "motion_items": len(motion_items),
            "sound_items": len(sfx_items),
        },
        "authority_boundary": {
            "metadata_only": True,
            "source_code_copied": False,
            "automatic_install": False,
            "automatic_download": False,
            "render_execution_performed": False,
        },
    }


def _inspect_onda(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if _git_head(root) != ONDA_COMMIT:
        raise ContractError("Onda source commit drifted")
    registry_path = _safe_source_file(root, "registry/registry.json", "Onda registry")
    license_path = _safe_source_file(root, "LICENSE", "Onda license")
    registry = read_json_object(registry_path, "Onda registry")
    rows = registry.get("items")
    if not isinstance(rows, list) or not rows:
        raise ContractError("Onda registry has no items")
    items: list[dict[str, Any]] = []
    for raw in rows:
        summary = _object(raw)
        name = _required_text(summary.get("name"), "Onda item name")
        item_path = _safe_source_file(
            root, f"registry/r/{name}.json", f"Onda registry item {name}"
        )
        item = read_json_object(item_path, f"Onda item {name}")
        if item.get("name") != name:
            raise ContractError("Onda registry item identity drifted")
        categories = summary.get("categories")
        category = (
            str(categories[0])
            if isinstance(categories, list) and categories
            else "uncategorized"
        )
        items.append(
            {
                "source_id": "onda",
                "item_id": name,
                "title": str(summary.get("title") or name),
                "description": str(summary.get("description") or ""),
                "category": category,
                "dependencies": sorted(
                    str(value) for value in summary.get("dependencies") or []
                ),
                "source_item": artifact_reference(item_path, f"Onda item {name}"),
            }
        )
    return {
        "source_id": "onda",
        "repository": ONDA_REPOSITORY,
        "source_root": str(root),
        "source_commit": ONDA_COMMIT,
        "license": "MIT",
        "license_file": artifact_reference(license_path, "Onda license"),
        "registry": artifact_reference(registry_path, "Onda registry"),
        "item_count": len(items),
        "items": sorted(items, key=lambda row: str(row["item_id"])),
    }


def _inspect_remotion_bits(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if _git_head(root) != REMOTION_BITS_COMMIT:
        raise ContractError("Remotion Bits source commit drifted")
    registry_path = _safe_source_file(root, "registry.json", "Remotion Bits registry")
    package_path = _safe_source_file(
        root, "package.json", "Remotion Bits package metadata"
    )
    package = read_json_object(package_path, "Remotion Bits package metadata")
    if package.get("name") != "remotion-bits" or package.get("license") != "MIT":
        raise ContractError("Remotion Bits package license metadata drifted")
    registry = read_json_object(registry_path, "Remotion Bits registry")
    rows = registry.get("items")
    if not isinstance(rows, list) or not rows:
        raise ContractError("Remotion Bits registry has no items")
    items: list[dict[str, Any]] = []
    for raw in rows:
        row = _object(raw)
        name = _required_text(row.get("name"), "Remotion Bits item name")
        files = []
        for raw_file in row.get("files") or []:
            file_row = _object(raw_file)
            path = _safe_source_file(
                root,
                file_row.get("relativePath"),
                f"Remotion Bits item file {name}",
            )
            files.append(artifact_reference(path, f"Remotion Bits {name} file"))
        if not files:
            raise ContractError(f"Remotion Bits item has no source file: {name}")
        items.append(
            {
                "source_id": "remotion_bits",
                "item_id": name,
                "title": str(row.get("title") or name),
                "description": str(row.get("description") or ""),
                "category": str(row.get("type") or "uncategorized"),
                "dependencies": sorted(
                    str(_object(value).get("name") or value)
                    for value in row.get("dependencies") or []
                ),
                "source_item": artifact_reference(
                    registry_path, f"Remotion Bits registry item {name}"
                ),
                "source_files": files,
            }
        )
    return {
        "source_id": "remotion_bits",
        "repository": REMOTION_BITS_REPOSITORY,
        "source_root": str(root),
        "source_commit": REMOTION_BITS_COMMIT,
        "license": "package_metadata_mit_no_root_license_observed",
        "package_metadata": artifact_reference(
            package_path, "Remotion Bits package metadata"
        ),
        "registry": artifact_reference(registry_path, "Remotion Bits registry"),
        "item_count": len(items),
        "items": sorted(items, key=lambda row: str(row["item_id"])),
    }


def _inspect_clipkit(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if _git_head(root) != CLIPKIT_COMMIT:
        raise ContractError("ClipKit source commit drifted")
    package_root = root / "packages" / "sfx"
    package_path = _safe_source_file(
        root, "packages/sfx/package.json", "ClipKit package"
    )
    license_path = _safe_source_file(root, "packages/sfx/LICENSE", "ClipKit license")
    package = read_json_object(package_path, "ClipKit SFX package")
    if (
        package.get("name") != "@clipkit/sfx"
        or package.get("version") != CLIPKIT_PACKAGE_VERSION
        or package.get("license") != "Apache-2.0"
        or package.get("dependencies") != {}
    ):
        raise ContractError("ClipKit SFX package metadata drifted")
    source_files = {
        name: artifact_reference(
            _safe_source_file(package_root, f"src/{name}", f"ClipKit {name}"),
            f"ClipKit {name}",
        )
        for name in _CLIPKIT_SOURCE_FILES
    }
    items = _run_clipkit(root, action="list")
    if not isinstance(items, list) or len(items) != 16:
        raise ContractError("ClipKit SFX catalog is incomplete")
    names = {str(_object(row).get("name") or "") for row in items}
    if not _CLIPKIT_ALLOWED_NAMES.issubset(names):
        raise ContractError("ClipKit reviewed SFX subset is missing")
    return {
        "source_id": "clipkit_sfx",
        "repository": CLIPKIT_REPOSITORY,
        "source_root": str(root),
        "source_commit": CLIPKIT_COMMIT,
        "package_name": "@clipkit/sfx",
        "package_version": CLIPKIT_PACKAGE_VERSION,
        "license": "Apache-2.0",
        "license_file": artifact_reference(license_path, "ClipKit license"),
        "package_metadata": artifact_reference(package_path, "ClipKit package"),
        "source_files": source_files,
        "item_count": len(items),
        "approved_item_count": len(_CLIPKIT_ALLOWED_NAMES),
        "node_runtime": "node>=22 experimental-strip-types",
    }


def _run_clipkit(
    root: Path,
    *,
    action: str,
    name: str | None = None,
    seed: int | None = None,
    tune: int | None = None,
    output: Path | None = None,
) -> Any:
    node = shutil.which("node")
    if node is None:
        raise ContractError("ClipKit SFX requires Node 22 or newer")
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, check=False, timeout=10
    )
    try:
        major = int(version.stdout.strip().lstrip("v").split(".", 1)[0])
    except (ValueError, IndexError) as exc:
        raise ContractError("unable to determine Node version for ClipKit") from exc
    if version.returncode != 0 or major < 22:
        raise ContractError("ClipKit fixed-source adapter requires Node 22 or newer")
    source_dir = root.expanduser().resolve() / "packages" / "sfx" / "src"
    runtime_root = (
        Path(
            os.environ.get(
                "VIDEO_PIPELINE_TEST_RUNTIME",
                str(Path.cwd() / ".external-motion-runtime"),
            )
        )
        .expanduser()
        .resolve()
    )
    runtime_root.mkdir(parents=True, exist_ok=True)
    stage = runtime_root / f"clipkit-sfx-{uuid4().hex}"
    stage.mkdir()
    try:
        for filename in _CLIPKIT_SOURCE_FILES:
            source = _safe_source_file(source_dir, filename, f"ClipKit {filename}")
            text = source.read_text(encoding="utf-8")
            for module in ("sfx", "finish", "encode-wav", "catalog"):
                text = text.replace(f"'./{module}.js'", f"'./{module}.ts'")
            (stage / filename).write_text(text, encoding="utf-8")
        runner = stage / "runner.mjs"
        runner.write_text(_NODE_RUNNER + "\n", encoding="utf-8")
        command = [node, "--experimental-strip-types", str(runner), action]
        if action == "generate":
            if name not in _CLIPKIT_ALLOWED_NAMES or output is None:
                raise ContractError("ClipKit generation request is not approved")
            output = output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            command.extend([name, str(seed), str(tune), str(output)])
        env = dict(os.environ)
        env["NODE_NO_WARNINGS"] = "1"
        result = subprocess.run(
            command,
            cwd=stage,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            raise ContractError(
                f"ClipKit source execution failed: {(result.stderr or result.stdout).strip()}"
            )
        try:
            payload = json.loads(result.stdout.strip())
        except json.JSONDecodeError as exc:
            raise ContractError("ClipKit source returned invalid JSON") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if action == "generate":
        if output is None or not output.is_file() or output.stat().st_size <= 44:
            raise ContractError("ClipKit did not produce a non-empty WAV")
        if output.read_bytes()[:4] != b"RIFF":
            raise ContractError("ClipKit output is not a RIFF WAV")
        return {
            "engine": "@clipkit/sfx",
            "package_version": CLIPKIT_PACKAGE_VERSION,
            "source_commit": CLIPKIT_COMMIT,
            "source_name": name,
            "seed": seed,
            "tune": tune,
            "finish_profile": "restrained-v1",
            "sample_rate": int(_object(payload).get("sampleRate") or 0),
            "frames": int(_object(payload).get("frames") or 0),
            "node_major": major,
        }
    return payload


def _load_overrides(
    path: str | Path | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    if path is None:
        return {}, None
    file = Path(path).expanduser().resolve()
    payload = read_json_object(file, "external motion overrides")
    if payload.get("schema") != "video_creation_pipeline.external_motion_overrides.v1":
        raise ContractError("unsupported external motion overrides schema")
    rows = payload.get("selections")
    if not isinstance(rows, list):
        raise ContractError("external motion overrides require selections")
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = _object(raw)
        cue_id = _required_text(row.get("cue_id"), "override cue id")
        if cue_id in result:
            raise ContractError("external motion overrides contain duplicate cues")
        if set(row) - {"cue_id", "source_id", "item_id", "sfx_asset_id"}:
            raise ContractError("external motion override contains unknown fields")
        result[cue_id] = row
    return result, artifact_reference(file, "external motion overrides")


def _prepare_managed_root(root: Path, owner: dict[str, Any], marker: str) -> None:
    marker_path = root / marker
    if root.exists():
        if not root.is_dir() or not marker_path.is_file():
            raise ContractError("ClipKit output root is not managed by this adapter")
        current = read_json_object(marker_path, "ClipKit output owner")
        if current != owner:
            raise ContractError("ClipKit output root owner drifted")
        return
    root.mkdir(parents=True)
    atomic_write_json(owner, marker_path)


def _copy_exact(source: Path, target: Path, label: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if file_sha256(source) != file_sha256(target):
            raise ContractError(f"{label} output conflicts with existing file")
        return
    shutil.copyfile(source, target)


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as audio:
            rate = audio.getframerate()
            frames = audio.getnframes()
    except (wave.Error, OSError) as exc:
        raise ContractError("ClipKit output WAV could not be decoded") from exc
    if rate <= 0 or frames <= 0:
        raise ContractError("ClipKit output WAV has invalid timing")
    return frames / rate


def _git_head(root: Path) -> str:
    if not root.is_dir():
        raise ContractError(f"external source root not found: {root}")
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        raise ContractError("external source root is not a readable Git checkout")
    return result.stdout.strip()


def _safe_source_file(root: Path, relative: Any, label: str) -> Path:
    root = root.expanduser().resolve()
    value = str(relative or "").strip()
    if not value:
        raise ContractError(f"{label} path is missing")
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContractError(f"{label} escapes the reviewed source root") from exc
    if not candidate.is_file():
        raise ContractError(f"{label} was not found")
    return candidate


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"{label} is required")
    return text


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("expected an object")
    return value
