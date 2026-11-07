from __future__ import annotations

import math
import mimetypes
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .contract_io import (
    artifact_reference,
    atomic_write_json,
    canonical_json_sha256,
    payload_sha256,
    read_json_object,
    validate_artifact_reference,
)
from .errors import ContractError
from .execution_receipts import (
    FFMPEG_OUTLET_ID,
    execute_ffmpeg_with_receipt,
)
from .music_pipeline import build_music_asset_profile, validate_music_asset_profile

STOCK_SOURCE_REGISTRY_SCHEMA = "video_creation_pipeline.stock_source_registry.v1"
LICENSED_MEDIA_ASSET_SCHEMA = "video_creation_pipeline.licensed_media_asset.v1"
MEDIA_CATALOG_SCHEMA = "video_creation_pipeline.licensed_media_catalog.v1"
SOUND_EFFECT_PROFILE_SCHEMA = "video_creation_pipeline.sound_effect_profile.v1"
SOUND_EFFECT_CUE_SHEET_SCHEMA = "video_creation_pipeline.sound_effect_cue_sheet.v1"
SOUND_EFFECT_EDIT_PLAN_SCHEMA = "video_creation_pipeline.sound_effect_edit_plan.v1"

SFX_TIMING_AUTHORITIES = {"final_audio_or_srt", "scene_timeline", "script_anchors"}
SFX_SOUND_STYLES = {"general_knowledge", "developer_ui_feedback"}
SFX_FUNCTIONS = {
    "orientation",
    "causality",
    "commitment",
    "caution",
    "resolution",
    "emphasis",
    "transition",
}
SFX_PRIORITIES = {"primary", "supporting", "optional"}
SFX_VISUAL_TRIGGERS = {"onset", "impact", "settle"}
SFX_NARRATION_POSITIONS = {"before", "during", "after"}
SFX_DENSITY_TARGETS = {
    "sparse": (2.0, 3.5),
    "balanced": (3.5, 5.0),
    "dense": (4.5, 6.0),
}
SFX_FEEDBACK_FUNCTIONS = {
    "execution_start": {"orientation", "transition"},
    "result_ready": {"resolution", "commitment"},
    "rule_highlight": {"emphasis", "commitment"},
    "error_warning": {"caution"},
    "state_connect": {"causality"},
    "state_lock": {"commitment", "resolution"},
    "section_transition": {"transition"},
}

MEDIA_TYPES = {"image", "video", "music", "sound_effect", "template"}
PURPOSES = {"personal", "commercial"}
PLATFORMS = {
    "youtube",
    "social_media",
    "website",
    "online_ad",
    "podcast",
    "broadcast",
    "video_game",
    "physical_media",
    "internal",
}

_GENERAL_PLATFORMS = sorted(PLATFORMS)
_ONLINE_PLATFORMS = ["youtube", "social_media", "website", "online_ad", "podcast"]


def _policy(
    source_id: str,
    display_name: str,
    domain: str,
    homepage: str,
    license_url: str,
    media_types: list[str],
    licenses: dict[str, bool],
    *,
    platforms: list[str] | None = None,
    media_platforms: dict[str, list[str]] | None = None,
    modification_allowed: bool = True,
    commercial_use_allowed: bool = True,
    third_party_rights_review: bool = False,
    policy_verified_at: str = "2026-08-15",
    notes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "display_name": display_name,
        "domain": domain,
        "homepage": homepage,
        "license_url": license_url,
        "allowed_media_types": sorted(media_types),
        "accepted_licenses": [
            {
                "license_id": license_id,
                "attribution_required": attribution_required,
            }
            for license_id, attribution_required in sorted(licenses.items())
        ],
        "commercial_use_allowed": commercial_use_allowed,
        "modification_allowed": modification_allowed,
        "allowed_platforms": sorted(platforms or _GENERAL_PLATFORMS),
        "media_platforms": {
            key: sorted(value) for key, value in sorted((media_platforms or {}).items())
        },
        "third_party_rights_review": third_party_rights_review,
        "ingestion_methods": ["manual", "official_api"],
        "bulk_scraping_allowed": False,
        "policy_verified_at": policy_verified_at,
        "notes": sorted(notes or []),
    }


_SOURCE_POLICIES = {
    "freesound": _policy(
        "freesound",
        "Freesound",
        "freesound.org",
        "https://freesound.org/",
        "https://freesound.org/help/faq/",
        ["sound_effect"],
        {"cc0-1.0": False, "cc-by-3.0": True, "cc-by-4.0": True},
        notes=["CC BY requires attribution; CC BY-NC is intentionally rejected."],
    ),
    "mixkit": _policy(
        "mixkit",
        "Mixkit",
        "mixkit.co",
        "https://mixkit.co/",
        "https://mixkit.co/license/",
        ["music", "sound_effect", "template", "video"],
        {"mixkit-free-license": False},
        media_platforms={"music": _ONLINE_PLATFORMS},
        notes=[
            "Restricted License items are intentionally rejected.",
            "Mixkit music is not cleared here for TV/radio, games, or physical media.",
        ],
    ),
    "lobe_icons": _policy(
        "lobe_icons",
        "Lobe Icons",
        "icons.lobehub.com",
        "https://icons.lobehub.com/",
        "https://github.com/lobehub/lobe-icons/blob/master/LICENSE",
        ["image"],
        {"mit": True},
        third_party_rights_review=True,
        policy_verified_at="2026-08-20",
        notes=[
            "Use a fixed source commit or package version and retain the MIT notice.",
            "Brand and product marks require a separate trademark-use declaration.",
            "The pipeline does not imply affiliation or endorsement.",
        ],
    ),
    "pexels": _policy(
        "pexels",
        "Pexels",
        "pexels.com",
        "https://www.pexels.com/",
        "https://www.pexels.com/legal-pages/license/",
        ["image", "video"],
        {"pexels-license": False},
        third_party_rights_review=True,
        notes=["Unaltered resale and implied endorsement are not permitted."],
    ),
    "pixabay": _policy(
        "pixabay",
        "Pixabay",
        "pixabay.com",
        "https://pixabay.com/",
        "https://pixabay.com/service/license-summary/",
        ["image", "music", "sound_effect", "video"],
        {"pixabay-content-license": False},
        third_party_rights_review=True,
        notes=[
            "Standalone resale and trademark use are not permitted.",
            "Recognizable people, brands, and property may require separate clearance.",
        ],
    ),
    "unsplash": _policy(
        "unsplash",
        "Unsplash",
        "unsplash.com",
        "https://unsplash.com/",
        "https://unsplash.com/license",
        ["image"],
        {"unsplash-license": False},
        third_party_rights_review=True,
        notes=["Unmodified resale and competing image-library use are not permitted."],
    ),
    "thiings_free": _policy(
        "thiings_free",
        "Thiings Free",
        "thiings.co",
        "https://www.thiings.co/things",
        "https://www.thiings.co/terms",
        ["image"],
        {"thiings-free-license": True},
        commercial_use_allowed=False,
        third_party_rights_review=True,
        policy_verified_at="2026-08-20",
        notes=[
            "The free license is personal and non-commercial only.",
            "Attribution and an item-level license snapshot are required.",
            "Full-collection download and standalone redistribution are not allowed.",
        ],
    ),
    "thiings_paid": _policy(
        "thiings_paid",
        "Thiings Paid",
        "thiings.co",
        "https://www.thiings.co/things",
        "https://www.thiings.co/terms",
        ["image"],
        {"thiings-business-license": False, "thiings-indie-license": False},
        third_party_rights_review=True,
        policy_verified_at="2026-08-20",
        notes=[
            "Commercial use requires a matching paid-license receipt and revenue tier.",
            "Standalone resale and redistribution remain prohibited.",
        ],
    ),
    "youtube_audio_library": _policy(
        "youtube_audio_library",
        "YouTube Audio Library",
        "youtube.com",
        "https://www.youtube.com/audiolibrary",
        "https://support.google.com/youtube/answer/3376882",
        ["music", "sound_effect"],
        {
            "youtube-audio-library-standard": False,
            "youtube-audio-library-cc-by": True,
        },
        platforms=["youtube"],
        notes=[
            "This adapter only clears Audio Library assets for YouTube output.",
            "CC-licensed tracks require the supplied attribution text.",
        ],
    ),
}


def build_stock_source_registry() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": STOCK_SOURCE_REGISTRY_SCHEMA,
        "sources": _source_rows(),
        "selection_policy": {
            "commercial_default": True,
            "license_snapshot_required": True,
            "item_page_required": True,
            "manual_or_official_api_only": True,
            "unknown_license_rejected": True,
            "noncommercial_license_rejected": True,
            "no_derivatives_license_rejected": True,
        },
        "authority_boundary": {
            "network_execution_performed": False,
            "automatic_download": False,
            "bulk_scraping": False,
            "upload_performed": False,
            "publication_performed": False,
        },
    }
    payload["registry_sha256"] = payload_sha256(payload, "registry_sha256")
    validate_stock_source_registry(payload)
    return payload


def validate_stock_source_registry(payload: dict[str, Any]) -> None:
    if payload.get("schema") != STOCK_SOURCE_REGISTRY_SCHEMA:
        raise ContractError("unsupported stock source registry schema")
    if payload.get("registry_sha256") != payload_sha256(payload, "registry_sha256"):
        raise ContractError("stock source registry integrity check failed")
    if payload.get("sources") != _source_rows():
        raise ContractError("stock source registry differs from reviewed policies")
    expected_policy = {
        "commercial_default": True,
        "license_snapshot_required": True,
        "item_page_required": True,
        "manual_or_official_api_only": True,
        "unknown_license_rejected": True,
        "noncommercial_license_rejected": True,
        "no_derivatives_license_rejected": True,
    }
    if payload.get("selection_policy") != expected_policy:
        raise ContractError("stock source selection policy is invalid")
    boundary = _object(payload.get("authority_boundary"))
    if not all(
        boundary.get(field) is False
        for field in (
            "network_execution_performed",
            "automatic_download",
            "bulk_scraping",
            "upload_performed",
            "publication_performed",
        )
    ):
        raise ContractError("stock source registry authority boundary is invalid")


def build_licensed_media_asset(
    media_path: str | Path,
    license_snapshot_path: str | Path,
    metadata: dict[str, Any],
    *,
    purpose: str = "commercial",
    platform: str = "youtube",
) -> dict[str, Any]:
    source_id = _choice(metadata.get("source_id"), set(_SOURCE_POLICIES), "source")
    policy = deepcopy(_SOURCE_POLICIES[source_id])
    media_type = _choice(metadata.get("media_type"), MEDIA_TYPES, "media type")
    normalized_purpose = _choice(purpose, PURPOSES, "usage purpose")
    normalized_platform = _choice(platform, PLATFORMS, "distribution platform")
    license_id = _required_text(metadata.get("license_id"), "license id").casefold()
    attribution_required = _evaluate_source_license(
        policy,
        media_type=media_type,
        license_id=license_id,
        purpose=normalized_purpose,
        platform=normalized_platform,
    )
    item_url = _source_item_url(metadata.get("item_url"), policy)
    attribution_text = str(metadata.get("attribution_text") or "").strip()
    if attribution_required and not attribution_text:
        raise ContractError("this source license requires attribution text")
    method = _choice(
        metadata.get("download_method", "manual"),
        set(policy["ingestion_methods"]),
        "download method",
    )
    rights = _normalize_third_party_rights(
        metadata.get("third_party_rights"), media_type
    )
    snapshot = artifact_reference(license_snapshot_path, "license snapshot")
    if snapshot["bytes"] <= 0:
        raise ContractError("license snapshot cannot be empty")
    source_file = artifact_reference(media_path, "licensed media file")
    mime_type = _media_mime(
        source_file["path"], media_type, explicit=metadata.get("mime_type")
    )
    payload: dict[str, Any] = {
        "schema": LICENSED_MEDIA_ASSET_SCHEMA,
        "asset_id": _required_text(metadata.get("asset_id"), "asset id"),
        "media_type": media_type,
        "title": _required_text(metadata.get("title"), "asset title"),
        "author": _required_text(metadata.get("author"), "asset author"),
        "description": str(metadata.get("description") or "").strip(),
        "tags": _text_list(metadata.get("tags") or [], "asset tags"),
        "source_file": source_file,
        "mime_type": mime_type,
        "source": {
            "source_id": source_id,
            "item_url": item_url,
            "homepage": policy["homepage"],
            "download_method": method,
            "downloaded_at": _iso_datetime(
                metadata.get("downloaded_at"), "asset download timestamp"
            ),
            "network_execution_performed_by_pipeline": False,
        },
        "source_policy": policy,
        "source_policy_sha256": canonical_json_sha256(policy),
        "license": {
            "license_id": license_id,
            "license_uri": policy["license_url"],
            "license_snapshot": snapshot,
            "purpose": normalized_purpose,
            "platform": normalized_platform,
            "commercial_use_allowed": bool(policy["commercial_use_allowed"]),
            "modification_allowed": bool(policy["modification_allowed"]),
            "attribution_required": attribution_required,
            "attribution_text": attribution_text,
            "standalone_redistribution_allowed": False,
        },
        "third_party_rights": rights,
        "readiness": {
            "license_evidence_complete": True,
            "commercial_use_eligible": bool(policy["commercial_use_allowed"]),
            "local_catalog_eligible": True,
        },
        "authority_boundary": {
            "candidate_asset_only": True,
            "source_file_modified": False,
            "automatic_download": False,
            "bulk_scraping": False,
            "upload_performed": False,
            "publication_performed": False,
        },
    }
    payload["asset_manifest_sha256"] = payload_sha256(payload, "asset_manifest_sha256")
    validate_licensed_media_asset(payload)
    return payload


def validate_licensed_media_asset(payload: dict[str, Any]) -> None:
    if payload.get("schema") != LICENSED_MEDIA_ASSET_SCHEMA:
        raise ContractError("unsupported licensed media asset schema")
    if payload.get("asset_manifest_sha256") != payload_sha256(
        payload, "asset_manifest_sha256"
    ):
        raise ContractError("licensed media asset integrity check failed")
    _required_text(payload.get("asset_id"), "asset id")
    _required_text(payload.get("title"), "asset title")
    _required_text(payload.get("author"), "asset author")
    media_type = _choice(payload.get("media_type"), MEDIA_TYPES, "media type")
    source_file = validate_artifact_reference(
        _object(payload.get("source_file")), "licensed media file"
    )
    if payload.get("mime_type") != _media_mime(
        source_file, media_type, explicit=payload.get("mime_type")
    ):
        raise ContractError("licensed media MIME type is invalid")
    source = _object(payload.get("source"))
    source_id = _choice(source.get("source_id"), set(_SOURCE_POLICIES), "source")
    policy = deepcopy(_SOURCE_POLICIES[source_id])
    if payload.get("source_policy") != policy or payload.get(
        "source_policy_sha256"
    ) != canonical_json_sha256(policy):
        raise ContractError("licensed media asset source policy drifted")
    _source_item_url(source.get("item_url"), policy)
    _choice(
        source.get("download_method"),
        set(policy["ingestion_methods"]),
        "download method",
    )
    _iso_datetime(source.get("downloaded_at"), "asset download timestamp")
    if source.get("network_execution_performed_by_pipeline") is not False:
        raise ContractError(
            "stock media ingestion cannot claim pipeline network execution"
        )
    license_info = _object(payload.get("license"))
    purpose = _choice(license_info.get("purpose"), PURPOSES, "usage purpose")
    platform = _choice(license_info.get("platform"), PLATFORMS, "distribution platform")
    license_id = _required_text(license_info.get("license_id"), "license id").casefold()
    attribution_required = _evaluate_source_license(
        policy,
        media_type=media_type,
        license_id=license_id,
        purpose=purpose,
        platform=platform,
    )
    validate_artifact_reference(
        _object(license_info.get("license_snapshot")), "license snapshot"
    )
    if license_info.get("license_uri") != policy["license_url"]:
        raise ContractError("licensed media asset license URI drifted")
    expected_license = {
        "commercial_use_allowed": bool(policy["commercial_use_allowed"]),
        "modification_allowed": bool(policy["modification_allowed"]),
        "attribution_required": attribution_required,
        "standalone_redistribution_allowed": False,
    }
    if any(license_info.get(key) != value for key, value in expected_license.items()):
        raise ContractError("licensed media asset license decision is invalid")
    if (
        attribution_required
        and not str(license_info.get("attribution_text") or "").strip()
    ):
        raise ContractError("licensed media asset attribution is missing")
    if payload.get("third_party_rights") != _normalize_third_party_rights(
        payload.get("third_party_rights"), media_type
    ):
        raise ContractError("licensed media third-party rights are invalid")
    readiness = _object(payload.get("readiness"))
    if (
        readiness.get("license_evidence_complete") is not True
        or readiness.get("local_catalog_eligible") is not True
        or readiness.get("commercial_use_eligible")
        != bool(policy["commercial_use_allowed"])
    ):
        raise ContractError("licensed media readiness is invalid")
    boundary = _object(payload.get("authority_boundary"))
    if (
        boundary.get("candidate_asset_only") is not True
        or boundary.get("source_file_modified") is not False
        or any(
            boundary.get(field) is not False
            for field in (
                "automatic_download",
                "bulk_scraping",
                "upload_performed",
                "publication_performed",
            )
        )
    ):
        raise ContractError("licensed media authority boundary is invalid")


def build_licensed_media_catalog(
    manifest_paths: Iterable[str | Path],
    *,
    purpose: str = "commercial",
    platform: str = "youtube",
) -> dict[str, Any]:
    normalized_purpose = _choice(purpose, PURPOSES, "usage purpose")
    normalized_platform = _choice(platform, PLATFORMS, "distribution platform")
    assets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_path in manifest_paths:
        path = Path(raw_path).expanduser().resolve()
        asset = read_json_object(path, "licensed media asset")
        validate_licensed_media_asset(asset)
        if asset["asset_id"] in seen:
            raise ContractError("licensed media catalog asset ids must be unique")
        seen.add(asset["asset_id"])
        if (
            asset["license"]["purpose"] != normalized_purpose
            or asset["license"]["platform"] != normalized_platform
        ):
            raise ContractError(
                "licensed media asset targets another purpose or platform"
            )
        assets.append(
            {
                "asset_id": asset["asset_id"],
                "media_type": asset["media_type"],
                "manifest": artifact_reference(path, "licensed media asset"),
                "asset_manifest_sha256": asset["asset_manifest_sha256"],
                "source_id": asset["source"]["source_id"],
                "license_id": asset["license"]["license_id"],
                "attribution_required": asset["license"]["attribution_required"],
            }
        )
    if not assets:
        raise ContractError("licensed media catalog requires assets")
    assets.sort(key=lambda row: (row["media_type"], row["asset_id"]))
    payload: dict[str, Any] = {
        "schema": MEDIA_CATALOG_SCHEMA,
        "purpose": normalized_purpose,
        "platform": normalized_platform,
        "assets": assets,
        "counts": {
            media_type: sum(row["media_type"] == media_type for row in assets)
            for media_type in sorted(MEDIA_TYPES)
        },
        "authority_boundary": {
            "local_metadata_only": True,
            "network_execution_performed": False,
            "automatic_download": False,
            "publication_performed": False,
        },
    }
    payload["catalog_sha256"] = payload_sha256(payload, "catalog_sha256")
    validate_licensed_media_catalog(payload)
    return payload


def validate_licensed_media_catalog(payload: dict[str, Any]) -> None:
    if payload.get("schema") != MEDIA_CATALOG_SCHEMA:
        raise ContractError("unsupported licensed media catalog schema")
    if payload.get("catalog_sha256") != payload_sha256(payload, "catalog_sha256"):
        raise ContractError("licensed media catalog integrity check failed")
    purpose = _choice(payload.get("purpose"), PURPOSES, "usage purpose")
    platform = _choice(payload.get("platform"), PLATFORMS, "distribution platform")
    rows = payload.get("assets")
    if not isinstance(rows, list) or not rows:
        raise ContractError("licensed media catalog requires asset rows")
    expected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        path = validate_artifact_reference(
            _object(_object(row).get("manifest")), "licensed media asset"
        )
        asset = read_json_object(path, "licensed media asset")
        validate_licensed_media_asset(asset)
        if asset["asset_id"] in seen:
            raise ContractError("licensed media catalog contains duplicate assets")
        seen.add(asset["asset_id"])
        if (
            asset["license"]["purpose"] != purpose
            or asset["license"]["platform"] != platform
        ):
            raise ContractError("licensed media catalog purpose or platform drifted")
        expected.append(
            {
                "asset_id": asset["asset_id"],
                "media_type": asset["media_type"],
                "manifest": artifact_reference(path, "licensed media asset"),
                "asset_manifest_sha256": asset["asset_manifest_sha256"],
                "source_id": asset["source"]["source_id"],
                "license_id": asset["license"]["license_id"],
                "attribution_required": asset["license"]["attribution_required"],
            }
        )
    expected.sort(key=lambda row: (row["media_type"], row["asset_id"]))
    if rows != expected:
        raise ContractError("licensed media catalog rows are not normalized")
    counts = {
        media_type: sum(row["media_type"] == media_type for row in expected)
        for media_type in sorted(MEDIA_TYPES)
    }
    if payload.get("counts") != counts:
        raise ContractError("licensed media catalog counts are inaccurate")
    boundary = _object(payload.get("authority_boundary"))
    if boundary.get("local_metadata_only") is not True or any(
        boundary.get(field) is not False
        for field in (
            "network_execution_performed",
            "automatic_download",
            "publication_performed",
        )
    ):
        raise ContractError("licensed media catalog authority boundary is invalid")


def build_music_profile_from_licensed_asset(
    asset_manifest_path: str | Path,
    analysis_spec: dict[str, Any],
) -> dict[str, Any]:
    manifest_path = Path(asset_manifest_path).expanduser().resolve()
    asset = read_json_object(manifest_path, "licensed music asset")
    validate_licensed_media_asset(asset)
    if asset["media_type"] != "music":
        raise ContractError("music profiles require a licensed music asset")
    snapshot = asset["license"]["license_snapshot"]
    profile = build_music_asset_profile(
        asset["source_file"]["path"],
        asset_id=asset["asset_id"],
        duration_s=analysis_spec.get("duration_s"),
        windows=analysis_spec.get("windows") or [],
        license_info={
            "status": "allowed",
            "commercial_use_allowed": asset["license"]["commercial_use_allowed"],
            "license_uri": asset["license"]["license_uri"],
            "license_snapshot": (
                f"{asset['license']['license_id']}; "
                f"snapshot_sha256={snapshot['sha256']}; "
                f"asset_manifest_sha256={asset['asset_manifest_sha256']}"
            ),
            "attribution": asset["license"]["attribution_text"],
        },
        analyzer=_object(analysis_spec.get("analyzer")),
    )
    profile["licensed_asset_manifest"] = artifact_reference(
        manifest_path, "licensed music asset"
    )
    profile["licensed_asset_manifest_sha256"] = asset["asset_manifest_sha256"]
    profile["profile_sha256"] = payload_sha256(profile, "profile_sha256")
    validate_music_asset_profile(profile)
    validate_music_profile_license_binding(profile)
    return profile


def validate_music_profile_license_binding(profile: dict[str, Any]) -> None:
    reference = profile.get("licensed_asset_manifest")
    if reference is None:
        return
    manifest_path = validate_artifact_reference(
        _object(reference), "licensed music asset"
    )
    asset = read_json_object(manifest_path, "licensed music asset")
    validate_licensed_media_asset(asset)
    if asset["media_type"] != "music":
        raise ContractError("music profile license binding targets non-music media")
    if profile.get("licensed_asset_manifest_sha256") != asset.get(
        "asset_manifest_sha256"
    ):
        raise ContractError("music profile license binding is stale")
    if profile.get("asset_id") != asset.get("asset_id"):
        raise ContractError("music profile asset id differs from its license manifest")
    if _object(profile.get("source_audio")).get("sha256") != _object(
        asset.get("source_file")
    ).get("sha256"):
        raise ContractError("music profile audio differs from its license manifest")
    expected = (
        f"{asset['license']['license_id']}; "
        f"snapshot_sha256={asset['license']['license_snapshot']['sha256']}; "
        f"asset_manifest_sha256={asset['asset_manifest_sha256']}"
    )
    if (
        profile["license"]["license_uri"] != asset["license"]["license_uri"]
        or profile["license"]["license_snapshot"] != expected
        or profile["license"]["attribution"] != asset["license"]["attribution_text"]
    ):
        raise ContractError("music profile license differs from its asset manifest")


def build_sound_effect_profile(
    asset_manifest_path: str | Path,
    *,
    duration_s: float,
    category: str,
    tags: Iterable[str],
    description: str = "",
) -> dict[str, Any]:
    manifest_path = Path(asset_manifest_path).expanduser().resolve()
    asset = read_json_object(manifest_path, "licensed sound effect asset")
    validate_licensed_media_asset(asset)
    if asset["media_type"] != "sound_effect":
        raise ContractError("sound effect profiles require a sound effect asset")
    payload: dict[str, Any] = {
        "schema": SOUND_EFFECT_PROFILE_SCHEMA,
        "asset_id": asset["asset_id"],
        "source_audio": dict(asset["source_file"]),
        "licensed_asset_manifest": artifact_reference(
            manifest_path, "licensed sound effect asset"
        ),
        "licensed_asset_manifest_sha256": asset["asset_manifest_sha256"],
        "duration_s": _positive_number(duration_s, "sound effect duration"),
        "category": _required_text(category, "sound effect category").casefold(),
        "description": str(description or asset["description"]).strip(),
        "tags": _text_list([*asset["tags"], *list(tags)], "sound effect tags"),
        "license": {
            "license_id": asset["license"]["license_id"],
            "license_uri": asset["license"]["license_uri"],
            "purpose": asset["license"]["purpose"],
            "platform": asset["license"]["platform"],
            "commercial_use_allowed": asset["license"]["commercial_use_allowed"],
            "attribution_required": asset["license"]["attribution_required"],
            "attribution_text": asset["license"]["attribution_text"],
        },
        "authority_boundary": {
            "candidate_evidence_only": True,
            "source_audio_modified": False,
            "creates_second_audio_index": False,
        },
    }
    payload["profile_sha256"] = payload_sha256(payload, "profile_sha256")
    validate_sound_effect_profile(payload)
    return payload


def validate_sound_effect_profile(payload: dict[str, Any]) -> None:
    if payload.get("schema") != SOUND_EFFECT_PROFILE_SCHEMA:
        raise ContractError("unsupported sound effect profile schema")
    if payload.get("profile_sha256") != payload_sha256(payload, "profile_sha256"):
        raise ContractError("sound effect profile integrity check failed")
    _required_text(payload.get("asset_id"), "sound effect asset id")
    source_path = validate_artifact_reference(
        _object(payload.get("source_audio")), "sound effect audio"
    )
    manifest_path = validate_artifact_reference(
        _object(payload.get("licensed_asset_manifest")),
        "licensed sound effect asset",
    )
    asset = read_json_object(manifest_path, "licensed sound effect asset")
    validate_licensed_media_asset(asset)
    if (
        asset["media_type"] != "sound_effect"
        or payload.get("asset_id") != asset.get("asset_id")
        or payload.get("licensed_asset_manifest_sha256")
        != asset.get("asset_manifest_sha256")
        or _object(asset.get("source_file")).get("path") != str(source_path)
    ):
        raise ContractError("sound effect profile license binding is invalid")
    _positive_number(payload.get("duration_s"), "sound effect duration")
    _required_text(payload.get("category"), "sound effect category")
    if payload.get("tags") != _text_list(
        payload.get("tags") or [], "sound effect tags"
    ):
        raise ContractError("sound effect tags are not normalized")
    expected_license = {
        "license_id": asset["license"]["license_id"],
        "license_uri": asset["license"]["license_uri"],
        "purpose": asset["license"]["purpose"],
        "platform": asset["license"]["platform"],
        "commercial_use_allowed": asset["license"]["commercial_use_allowed"],
        "attribution_required": asset["license"]["attribution_required"],
        "attribution_text": asset["license"]["attribution_text"],
    }
    if payload.get("license") != expected_license:
        raise ContractError("sound effect profile license drifted")
    boundary = _object(payload.get("authority_boundary"))
    if (
        boundary.get("candidate_evidence_only") is not True
        or boundary.get("source_audio_modified") is not False
        or boundary.get("creates_second_audio_index") is not False
    ):
        raise ContractError("sound effect profile authority boundary is invalid")


def build_sound_effect_cue_sheet(
    source_path: str | Path,
    cues: Iterable[dict[str, Any]],
    *,
    timeline_duration_s: float,
    purpose: str = "commercial",
    platform: str = "youtube",
    semantic_spotting: dict[str, Any] | None = None,
) -> dict[str, Any]:
    duration = _positive_number(timeline_duration_s, "sound effect timeline duration")
    semantic_enabled = semantic_spotting is not None
    sound_style = "general_knowledge"
    if semantic_enabled:
        sound_style = _choice(
            _object(semantic_spotting).get("sound_style", "general_knowledge"),
            SFX_SOUND_STYLES,
            "sound effect style",
        )
    normalized_cues = _normalize_cues(
        cues,
        duration,
        semantic=semantic_enabled,
        sound_style=sound_style,
    )
    payload: dict[str, Any] = {
        "schema": SOUND_EFFECT_CUE_SHEET_SCHEMA,
        "source": artifact_reference(source_path, "sound effect cue source"),
        "purpose": _choice(purpose, PURPOSES, "usage purpose"),
        "platform": _choice(platform, PLATFORMS, "distribution platform"),
        "timeline_duration_s": duration,
        "cues": normalized_cues,
        "authority_boundary": {
            "candidate_cues_only": True,
            "source_timeline_modified": False,
            "automatic_provider_fallback": False,
            "automatic_publish": False,
        },
    }
    if semantic_enabled:
        payload["semantic_spotting"] = _normalize_semantic_spotting(
            _object(semantic_spotting), normalized_cues, duration
        )
    payload["cue_sheet_sha256"] = payload_sha256(payload, "cue_sheet_sha256")
    validate_sound_effect_cue_sheet(payload)
    return payload


def validate_sound_effect_cue_sheet(payload: dict[str, Any]) -> None:
    if payload.get("schema") != SOUND_EFFECT_CUE_SHEET_SCHEMA:
        raise ContractError("unsupported sound effect cue sheet schema")
    if payload.get("cue_sheet_sha256") != payload_sha256(payload, "cue_sheet_sha256"):
        raise ContractError("sound effect cue sheet integrity check failed")
    validate_artifact_reference(
        _object(payload.get("source")), "sound effect cue source"
    )
    _choice(payload.get("purpose"), PURPOSES, "usage purpose")
    _choice(payload.get("platform"), PLATFORMS, "distribution platform")
    duration = _positive_number(
        payload.get("timeline_duration_s"), "sound effect timeline duration"
    )
    semantic_raw = payload.get("semantic_spotting")
    semantic_enabled = semantic_raw is not None
    sound_style = "general_knowledge"
    if semantic_enabled:
        sound_style = _choice(
            _object(semantic_raw).get("sound_style", "general_knowledge"),
            SFX_SOUND_STYLES,
            "sound effect style",
        )
    expected_cues = _normalize_cues(
        payload.get("cues") or [],
        duration,
        semantic=semantic_enabled,
        sound_style=sound_style,
    )
    if payload.get("cues") != expected_cues:
        raise ContractError("sound effect cues are not normalized")
    if semantic_enabled and semantic_raw != _normalize_semantic_spotting(
        _object(semantic_raw), expected_cues, duration
    ):
        raise ContractError("sound effect semantic spotting is not normalized")
    boundary = _object(payload.get("authority_boundary"))
    if (
        boundary.get("candidate_cues_only") is not True
        or boundary.get("source_timeline_modified") is not False
        or boundary.get("automatic_provider_fallback") is not False
        or boundary.get("automatic_publish") is not False
    ):
        raise ContractError("sound effect cue authority boundary is invalid")


def build_sound_effect_edit_plan(
    cue_sheet_path: str | Path,
    profile_paths: Iterable[str | Path],
    *,
    min_score: float = 0.65,
    min_margin: float = 0.10,
) -> dict[str, Any]:
    cue_path = Path(cue_sheet_path).expanduser().resolve()
    cue_sheet = read_json_object(cue_path, "sound effect cue sheet")
    validate_sound_effect_cue_sheet(cue_sheet)
    profiles, references = _load_sound_effect_profiles(profile_paths)
    threshold = _ratio(min_score, "minimum sound effect score")
    margin = _ratio(min_margin, "minimum sound effect score margin")
    selections, rejected = _score_sound_effects(
        cue_sheet,
        profiles,
        min_score=threshold,
        min_margin=margin,
    )
    unresolved = sum(row["status"] == "missing_material" for row in selections)
    semantic_spotting = "semantic_spotting" in cue_sheet
    selection_policy: dict[str, Any] = {
        "mode": "deterministic_tag_match",
        "min_score": threshold,
        "min_margin": margin,
        "unresolved_behavior": "missing_material_fail_closed",
        "manual_review_required": False,
    }
    readiness: dict[str, Any] = {
        "render_eligible": unresolved == 0,
        "selected_count": len(selections) - unresolved,
        "missing_material_count": unresolved,
        "candidate_only": True,
    }
    authority_boundary: dict[str, Any] = {
        "source_audio_modified": False,
        "timeline_modified": False,
        "automatic_provider_fallback": False,
        "automatic_publish": False,
    }
    if semantic_spotting:
        selection_policy.update(
            {
                "audition_required_before_release": True,
                "candidate_mix_before_audition_allowed": True,
            }
        )
        audition_pending = sum(
            row.get("review_status") == "needs_audition" for row in selections
        )
        readiness.update(
            {
                "audition_pending_count": audition_pending,
                "release_eligible": unresolved == 0 and audition_pending == 0,
            }
        )
        authority_boundary.update(
            {
                "candidate_mix_without_audition_allowed": True,
                "release_without_audition_allowed": False,
            }
        )
    payload: dict[str, Any] = {
        "schema": SOUND_EFFECT_EDIT_PLAN_SCHEMA,
        "cue_sheet": artifact_reference(cue_path, "sound effect cue sheet"),
        "cue_sheet_sha256": cue_sheet["cue_sheet_sha256"],
        "profiles": references,
        "selection_policy": selection_policy,
        "selections": selections,
        "rejected": rejected,
        "readiness": readiness,
        "ffmpeg_handoff": {
            "outlet_id": FFMPEG_OUTLET_ID,
            "operation": "sound_effect_mix",
            "command_generated": False,
            "executed": False,
            "second_ffmpeg_orchestrator_created": False,
        },
        "authority_boundary": authority_boundary,
    }
    payload["sound_effect_edit_plan_sha256"] = payload_sha256(
        payload, "sound_effect_edit_plan_sha256"
    )
    validate_sound_effect_edit_plan(payload)
    return payload


def validate_sound_effect_edit_plan(payload: dict[str, Any]) -> None:
    if payload.get("schema") != SOUND_EFFECT_EDIT_PLAN_SCHEMA:
        raise ContractError("unsupported sound effect edit plan schema")
    if payload.get("sound_effect_edit_plan_sha256") != payload_sha256(
        payload, "sound_effect_edit_plan_sha256"
    ):
        raise ContractError("sound effect edit plan integrity check failed")
    cue_path = validate_artifact_reference(
        _object(payload.get("cue_sheet")), "sound effect cue sheet"
    )
    cue_sheet = read_json_object(cue_path, "sound effect cue sheet")
    validate_sound_effect_cue_sheet(cue_sheet)
    if payload.get("cue_sheet_sha256") != cue_sheet.get("cue_sheet_sha256"):
        raise ContractError("sound effect edit plan binds a stale cue sheet")
    profiles, expected_references = _load_sound_effect_profile_references(
        payload.get("profiles")
    )
    if payload.get("profiles") != expected_references:
        raise ContractError("sound effect profile references are not normalized")
    policy = _object(payload.get("selection_policy"))
    expected_policy: dict[str, Any] = {
        "mode": "deterministic_tag_match",
        "min_score": policy.get("min_score"),
        "min_margin": policy.get("min_margin"),
        "unresolved_behavior": "missing_material_fail_closed",
        "manual_review_required": False,
    }
    semantic_spotting = "semantic_spotting" in cue_sheet
    if semantic_spotting:
        expected_policy.update(
            {
                "audition_required_before_release": True,
                "candidate_mix_before_audition_allowed": True,
            }
        )
    if (
        policy.get("mode") != "deterministic_tag_match"
        or policy.get("unresolved_behavior") != "missing_material_fail_closed"
        or policy.get("manual_review_required") is not False
        or policy != expected_policy
    ):
        raise ContractError("sound effect selection policy is invalid")
    threshold = _ratio(policy.get("min_score"), "minimum sound effect score")
    margin = _ratio(policy.get("min_margin"), "minimum sound effect score margin")
    expected_selections, expected_rejected = _score_sound_effects(
        cue_sheet,
        profiles,
        min_score=threshold,
        min_margin=margin,
    )
    if (
        payload.get("selections") != expected_selections
        or payload.get("rejected") != expected_rejected
    ):
        raise ContractError("sound effect selections are inaccurate")
    unresolved = sum(row["status"] == "missing_material" for row in expected_selections)
    expected_readiness = {
        "render_eligible": unresolved == 0,
        "selected_count": len(expected_selections) - unresolved,
        "missing_material_count": unresolved,
        "candidate_only": True,
    }
    if semantic_spotting:
        audition_pending = sum(
            row.get("review_status") == "needs_audition" for row in expected_selections
        )
        expected_readiness.update(
            {
                "audition_pending_count": audition_pending,
                "release_eligible": unresolved == 0 and audition_pending == 0,
            }
        )
    if payload.get("readiness") != expected_readiness:
        raise ContractError("sound effect edit readiness is inaccurate")
    handoff = _object(payload.get("ffmpeg_handoff"))
    if handoff != {
        "outlet_id": FFMPEG_OUTLET_ID,
        "operation": "sound_effect_mix",
        "command_generated": False,
        "executed": False,
        "second_ffmpeg_orchestrator_created": False,
    }:
        raise ContractError("sound effect FFmpeg handoff is invalid")
    boundary = _object(payload.get("authority_boundary"))
    if (
        boundary.get("source_audio_modified") is not False
        or boundary.get("timeline_modified") is not False
        or boundary.get("automatic_provider_fallback") is not False
        or boundary.get("automatic_publish") is not False
    ):
        raise ContractError("sound effect edit authority boundary is invalid")
    if semantic_spotting and (
        boundary.get("candidate_mix_without_audition_allowed") is not True
        or boundary.get("release_without_audition_allowed") is not False
    ):
        raise ContractError("sound effect audition boundary is invalid")


def build_sound_effect_mix_command(
    program_video: str | Path,
    edit_plan: dict[str, Any],
    output_path: str | Path,
    *,
    deterministic: bool = False,
) -> list[str]:
    validate_sound_effect_edit_plan(edit_plan)
    if edit_plan["readiness"]["render_eligible"] is not True:
        raise ContractError("sound effect edit plan has missing material")
    program = Path(program_video).expanduser().resolve()
    if not program.is_file():
        raise ContractError("sound effect mix program video was not found")
    output = Path(output_path).expanduser().resolve()
    if output == program:
        raise ContractError("sound effect mix cannot overwrite the program video")
    output.parent.mkdir(parents=True, exist_ok=True)
    selections = [
        row for row in edit_plan["selections"] if row["status"] != "missing_material"
    ]
    if not selections:
        raise ContractError("sound effect mix requires selected effects")
    command = ["ffmpeg", "-y", "-i", str(program)]
    for row in selections:
        command.extend(["-i", row["source_audio"]["path"]])
    filters: list[str] = []
    labels = ["[0:a]"]
    for index, row in enumerate(selections, 1):
        duration = row["source_interval"]["end_s"] - row["source_interval"]["start_s"]
        fade_out_start = max(0.0, duration - row["fade_out_s"])
        delay_ms = int(round(row["timeline_start_s"] * 1000))
        filters.append(
            f"[{index}:a]atrim=start={row['source_interval']['start_s']:.6f}:"
            f"end={row['source_interval']['end_s']:.6f},"
            "asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={row['fade_in_s']:.3f},"
            f"afade=t=out:st={fade_out_start:.3f}:d={row['fade_out_s']:.3f},"
            f"volume={row['gain_db']:.3f}dB,adelay={delay_ms}:all=1[sfx{index}]"
        )
        labels.append(f"[sfx{index}]")
    filters.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0[aout]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-map_metadata",
            "-1",
        ]
    )
    if deterministic:
        command.extend(
            ["-fflags", "+bitexact", "-flags:a", "+bitexact", "-threads", "1"]
        )
    command.append(str(output))
    return command


def execute_sound_effect_mix(
    program_video: str | Path,
    edit_plan_path: str | Path,
    output_path: str | Path,
    receipt_path: str | Path,
    *,
    timeout_s: float = 120.0,
    deterministic: bool = False,
) -> dict[str, Any]:
    plan_path = Path(edit_plan_path).expanduser().resolve()
    plan = read_json_object(plan_path, "sound effect edit plan")
    validate_sound_effect_edit_plan(plan)
    command = build_sound_effect_mix_command(
        program_video, plan, output_path, deterministic=deterministic
    )
    inputs = [
        {"role": "program_video", "path": program_video},
        {"role": "sound_effect_edit_plan", "path": plan_path},
    ]
    for index, row in enumerate(plan["selections"], 1):
        if row["status"] != "missing_material":
            inputs.append(
                {
                    "role": f"sound_effect_{index:02}",
                    "path": row["source_audio"]["path"],
                }
            )
    return execute_ffmpeg_with_receipt(
        "sound_effect_mix",
        command,
        inputs,
        [{"role": "sound_effect_mixed_video", "path": output_path}],
        receipt_path,
        timeout_s=timeout_s,
    )


def write_stock_media_contract(
    payload: dict[str, Any], output_path: str | Path
) -> Path:
    validate_stock_media_contract(payload)
    return atomic_write_json(payload, output_path)


def validate_stock_media_contract(payload: dict[str, Any]) -> None:
    schema = payload.get("schema")
    validators = {
        STOCK_SOURCE_REGISTRY_SCHEMA: validate_stock_source_registry,
        LICENSED_MEDIA_ASSET_SCHEMA: validate_licensed_media_asset,
        MEDIA_CATALOG_SCHEMA: validate_licensed_media_catalog,
        SOUND_EFFECT_PROFILE_SCHEMA: validate_sound_effect_profile,
        SOUND_EFFECT_CUE_SHEET_SCHEMA: validate_sound_effect_cue_sheet,
        SOUND_EFFECT_EDIT_PLAN_SCHEMA: validate_sound_effect_edit_plan,
    }
    validator = validators.get(str(schema))
    if validator is None:
        if schema == "video_creation_pipeline.music_asset_profile.v1":
            validate_music_asset_profile(payload)
            validate_music_profile_license_binding(payload)
        else:
            raise ContractError("unsupported stock media contract")
    else:
        validator(payload)


def _source_rows() -> list[dict[str, Any]]:
    return [deepcopy(_SOURCE_POLICIES[key]) for key in sorted(_SOURCE_POLICIES)]


def _evaluate_source_license(
    policy: dict[str, Any],
    *,
    media_type: str,
    license_id: str,
    purpose: str,
    platform: str,
) -> bool:
    if media_type not in policy["allowed_media_types"]:
        raise ContractError(f"{policy['display_name']} does not provide {media_type}")
    licenses = {
        row["license_id"]: row["attribution_required"]
        for row in policy["accepted_licenses"]
    }
    if license_id not in licenses:
        raise ContractError("source license is not cleared for this pipeline")
    if purpose == "commercial" and policy["commercial_use_allowed"] is not True:
        raise ContractError("source license does not allow commercial use")
    allowed = policy["media_platforms"].get(media_type, policy["allowed_platforms"])
    if platform not in allowed:
        raise ContractError(
            "source license is not cleared for this distribution platform"
        )
    return bool(licenses[license_id])


def _source_item_url(value: Any, policy: dict[str, Any]) -> str:
    result = _required_text(value, "source item URL")
    parsed = urlparse(result)
    host = (parsed.hostname or "").casefold()
    domain = str(policy["domain"]).casefold()
    if parsed.scheme != "https" or not (host == domain or host.endswith(f".{domain}")):
        raise ContractError("source item URL does not belong to the selected source")
    return result


def _media_mime(
    media_path: str | Path, media_type: str, *, explicit: Any = None
) -> str:
    supplied = str(explicit or "").strip().casefold()
    detected = str(mimetypes.guess_type(str(media_path))[0] or "").casefold()
    result = supplied or detected
    if not result:
        raise ContractError("licensed media MIME type could not be determined")
    expected_prefix = {
        "image": "image/",
        "video": "video/",
        "music": "audio/",
        "sound_effect": "audio/",
    }.get(media_type)
    if expected_prefix and not result.startswith(expected_prefix):
        raise ContractError("licensed media MIME type conflicts with its media type")
    return result


def _normalize_third_party_rights(value: Any, media_type: str) -> dict[str, Any]:
    raw = _object(value)
    if media_type in {"image", "video"} and not raw:
        raise ContractError(
            "visual stock media requires a third-party rights declaration"
        )
    people = _boolean(raw.get("recognizable_people", False), "recognizable people")
    trademarks = _boolean(raw.get("trademarks", False), "trademarks")
    status = _choice(
        raw.get("status", "not_present"), {"not_present", "cleared"}, "rights status"
    )
    if (people or trademarks) and status != "cleared":
        raise ContractError(
            "recognizable people or trademarks require explicit clearance"
        )
    if status == "not_present" and (people or trademarks):
        raise ContractError("third-party rights declaration is contradictory")
    evidence = str(raw.get("evidence") or "").strip()
    if status == "cleared" and not evidence:
        raise ContractError("cleared third-party rights require evidence")
    return {
        "recognizable_people": people,
        "trademarks": trademarks,
        "status": status,
        "evidence": evidence,
    }


def _normalize_cues(
    cues: Iterable[dict[str, Any]],
    timeline_duration_s: float,
    *,
    semantic: bool = False,
    sound_style: str = "general_knowledge",
) -> list[dict[str, Any]]:
    rows = list(cues)
    if not rows:
        raise ContractError("sound effect cue sheet requires cues")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ContractError("sound effect cue must be an object")
        cue_id = _required_text(raw.get("cue_id"), "sound effect cue id")
        if cue_id in seen:
            raise ContractError("sound effect cue ids must be unique")
        seen.add(cue_id)
        duration = _positive_number(raw.get("duration_s"), "sound effect cue duration")
        fade_in = _nonnegative_number(
            raw.get("fade_in_s", 0.01), "sound effect fade in"
        )
        fade_out = _nonnegative_number(
            raw.get("fade_out_s", 0.03), "sound effect fade out"
        )
        if fade_in + fade_out > duration + 0.001:
            raise ContractError("sound effect fades exceed cue duration")
        gain = _number(raw.get("gain_db", -12.0), "sound effect gain")
        if gain < -60 or gain > 6:
            raise ContractError("sound effect gain must be between -60 and 6 dB")
        timeline_start = _nonnegative_number(
            raw.get("timeline_start_s"), "sound effect cue start"
        )
        if timeline_start + duration > timeline_duration_s + 0.001:
            raise ContractError("sound effect cue exceeds the timeline duration")
        cue: dict[str, Any] = {
            "cue_id": cue_id,
            "timeline_start_s": timeline_start,
            "duration_s": duration,
            "query": _required_text(raw.get("query"), "sound effect query"),
            "required_tags": _text_list(
                raw.get("required_tags") or [], "required sound effect tags"
            ),
            "optional_tags": _text_list(
                raw.get("optional_tags") or [], "optional sound effect tags"
            ),
            "prohibited_tags": _text_list(
                raw.get("prohibited_tags") or [], "prohibited sound effect tags"
            ),
            "preferred_asset_id": str(raw.get("preferred_asset_id") or "").strip(),
            "gain_db": gain,
            "fade_in_s": fade_in,
            "fade_out_s": fade_out,
            "evidence_ids": _text_list(
                raw.get("evidence_ids") or [], "sound effect evidence ids"
            ),
        }
        if semantic:
            cue.update(_normalize_semantic_cue(raw, sound_style=sound_style))
        normalized.append(cue)
    return sorted(normalized, key=lambda row: (row["timeline_start_s"], row["cue_id"]))


def _normalize_semantic_cue(raw: dict[str, Any], *, sound_style: str) -> dict[str, Any]:
    function = _choice(raw.get("function"), SFX_FUNCTIONS, "sound effect function")
    visual = _object(raw.get("visual_anchor"))
    narration = _object(raw.get("narration_anchor"))
    result: dict[str, Any] = {
        "event": _required_text(raw.get("event"), "sound effect semantic event"),
        "function": function,
        "priority": _choice(
            raw.get("priority"), SFX_PRIORITIES, "sound effect priority"
        ),
        "visual_anchor": {
            "action": _required_text(
                visual.get("action"), "sound effect visual anchor action"
            ),
            "trigger": _choice(
                visual.get("trigger"),
                SFX_VISUAL_TRIGGERS,
                "sound effect visual anchor trigger",
            ),
        },
        "narration_anchor": {
            "text": _required_text(
                narration.get("text"), "sound effect narration anchor text"
            ),
            "position": _choice(
                narration.get("position"),
                SFX_NARRATION_POSITIONS,
                "sound effect narration anchor position",
            ),
        },
        "spotting_status": "needs_audition",
        "rationale": _required_text(
            raw.get("rationale"), "sound effect spotting rationale"
        ),
    }
    if str(raw.get("spotting_status") or "needs_audition") != "needs_audition":
        raise ContractError(
            "active sound effect cues must remain needs_audition until a separate "
            "approval record exists"
        )
    if sound_style == "developer_ui_feedback":
        state_change = _object(raw.get("state_change"))
        before = _required_text(state_change.get("before"), "sound effect state before")
        after = _required_text(state_change.get("after"), "sound effect state after")
        if before == after:
            raise ContractError("sound effect state change must describe a real change")
        feedback_kind = _choice(
            state_change.get("feedback_kind"),
            set(SFX_FEEDBACK_FUNCTIONS),
            "sound effect feedback kind",
        )
        if function not in SFX_FEEDBACK_FUNCTIONS[feedback_kind]:
            raise ContractError(
                "sound effect feedback kind and function are inconsistent"
            )
        result["state_change"] = {
            "before": before,
            "after": after,
            "feedback_kind": feedback_kind,
        }
    return result


def _normalize_semantic_spotting(
    raw: dict[str, Any], cues: list[dict[str, Any]], timeline_duration_s: float
) -> dict[str, Any]:
    timing_authority = _choice(
        raw.get("timing_authority"),
        SFX_TIMING_AUTHORITIES,
        "sound effect timing authority",
    )
    sound_style = _choice(
        raw.get("sound_style", "general_knowledge"),
        SFX_SOUND_STYLES,
        "sound effect style",
    )
    density = _object(raw.get("density_plan"))
    profile = _choice(
        density.get("profile"), set(SFX_DENSITY_TARGETS), "sound effect density profile"
    )
    default_target = SFX_DENSITY_TARGETS[profile]
    target = _number_range(
        density.get("target_cues_per_min", default_target),
        "sound effect target cues per minute",
        minimum=0.1,
        maximum=8.0,
    )
    duration_minutes = timeline_duration_s / 60.0
    target_count = [
        math.ceil(duration_minutes * target[0]),
        math.floor(duration_minutes * target[1]),
    ]
    if target_count[1] < target_count[0]:
        target_count[1] = target_count[0]
    planned_count = len(cues)
    planned_rate = round(planned_count / duration_minutes, 3)
    max_gap_value = density.get("max_uncovered_semantic_gap_s")
    max_gap = (
        None
        if max_gap_value is None
        else _nonnegative_number(
            max_gap_value, "sound effect maximum uncovered semantic gap"
        )
    )
    omissions = _normalize_sfx_omissions(
        raw.get("omissions") or [], timeline_duration_s
    )
    return {
        "timing_authority": timing_authority,
        "sound_style": sound_style,
        "density_plan": {
            "profile": profile,
            "duration_s": timeline_duration_s,
            "target_cues_per_min": target,
            "target_count": target_count,
            "planned_cue_count": planned_count,
            "planned_cues_per_min": planned_rate,
            "density_within_target": target[0] <= planned_rate <= target[1],
            "max_uncovered_semantic_gap_s": max_gap,
        },
        "omissions": omissions,
        "review_rule": {
            "default_status": "needs_audition",
            "approval_requires_separate_record": True,
            "candidate_mix_before_audition_allowed": True,
            "release_before_audition_allowed": False,
        },
    }


def _normalize_sfx_omissions(
    value: Iterable[Any], timeline_duration_s: float
) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)):
        raise ContractError("sound effect omissions must be a list")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ContractError("sound effect omission must be an object")
        omission_id = _required_text(raw.get("omission_id"), "sound effect omission id")
        if omission_id in seen:
            raise ContractError("sound effect omission ids must be unique")
        seen.add(omission_id)
        start = _nonnegative_number(raw.get("start_s"), "sound effect omission start")
        end = _nonnegative_number(raw.get("end_s"), "sound effect omission end")
        if end <= start or end > timeline_duration_s + 0.001:
            raise ContractError("sound effect omission interval is invalid")
        rows.append(
            {
                "omission_id": omission_id,
                "start_s": start,
                "end_s": end,
                "reason": _required_text(
                    raw.get("reason"), "sound effect omission reason"
                ),
                "evidence_ids": _text_list(
                    raw.get("evidence_ids") or [], "sound effect omission evidence ids"
                ),
            }
        )
    return sorted(rows, key=lambda row: (row["start_s"], row["omission_id"]))


def _number_range(
    value: Any, label: str, *, minimum: float, maximum: float
) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ContractError(f"{label} must be a two-number range")
    low = _number(value[0], label)
    high = _number(value[1], label)
    if low < minimum or high > maximum or low > high:
        raise ContractError(f"{label} is outside the allowed range")
    return [low, high]


def _load_sound_effect_profiles(
    paths: Iterable[str | Path],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    profiles: dict[str, dict[str, Any]] = {}
    references: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        profile = read_json_object(path, "sound effect profile")
        validate_sound_effect_profile(profile)
        asset_id = profile["asset_id"]
        if asset_id in profiles:
            raise ContractError("sound effect profile ids must be unique")
        profiles[asset_id] = profile
        references.append(
            {
                "asset_id": asset_id,
                "artifact": artifact_reference(path, "sound effect profile"),
                "profile_sha256": profile["profile_sha256"],
            }
        )
    if not profiles:
        raise ContractError("sound effect planning requires profiles")
    references.sort(key=lambda row: row["asset_id"])
    return profiles, references


def _load_sound_effect_profile_references(
    value: Any,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    rows = value if isinstance(value, list) else []
    paths = []
    for row in rows:
        path = validate_artifact_reference(
            _object(_object(row).get("artifact")), "sound effect profile"
        )
        paths.append(path)
    return _load_sound_effect_profiles(paths)


def _score_sound_effects(
    cue_sheet: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    *,
    min_score: float,
    min_margin: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selections: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for cue in cue_sheet["cues"]:
        ranked: list[dict[str, Any]] = []
        for asset_id, profile in profiles.items():
            reasons: list[str] = []
            license_info = profile["license"]
            if license_info["purpose"] != cue_sheet["purpose"]:
                reasons.append("purpose_mismatch")
            if license_info["platform"] != cue_sheet["platform"]:
                reasons.append("platform_mismatch")
            if (
                cue_sheet["purpose"] == "commercial"
                and not license_info["commercial_use_allowed"]
            ):
                reasons.append("commercial_use_not_allowed")
            tags = set(profile["tags"])
            required = set(cue["required_tags"])
            prohibited = set(cue["prohibited_tags"])
            if not required.issubset(tags):
                reasons.append("required_tags_missing")
            if prohibited.intersection(tags):
                reasons.append("prohibited_tag_present")
            if reasons:
                rejected.append(
                    {"cue_id": cue["cue_id"], "asset_id": asset_id, "reasons": reasons}
                )
                continue
            query_terms = _search_terms(cue["query"])
            profile_terms = _search_terms(
                " ".join(
                    [profile["category"], profile["description"], *profile["tags"]]
                )
            )
            semantic = len(query_terms.intersection(profile_terms)) / max(
                1, len(query_terms)
            )
            optional = set(cue["optional_tags"])
            optional_score = len(optional.intersection(tags)) / max(1, len(optional))
            required_score = 1.0 if required else 0.0
            if required:
                score = 0.65 * required_score + 0.25 * semantic + 0.10 * optional_score
            else:
                score = 0.85 * semantic + 0.15 * optional_score
            ranked.append(
                {
                    "asset_id": asset_id,
                    "score": round(_clamp01(score), 6),
                    "score_components": {
                        "required_tags": round(required_score, 6),
                        "query_overlap": round(semantic, 6),
                        "optional_tags": round(optional_score, 6),
                    },
                    "source_audio": dict(profile["source_audio"]),
                    "source_duration_s": profile["duration_s"],
                    "license": dict(profile["license"]),
                }
            )
        ranked.sort(key=lambda row: (-row["score"], row["asset_id"]))
        preferred = next(
            (row for row in ranked if row["asset_id"] == cue["preferred_asset_id"]),
            None,
        )
        top = preferred or (ranked[0] if ranked else None)
        second_score = ranked[1]["score"] if len(ranked) > 1 else 0.0
        high_confidence = bool(
            top
            and top["score"] >= min_score
            and (preferred is not None or top["score"] - second_score >= min_margin)
        )
        if not high_confidence:
            missing: dict[str, Any] = {
                "cue_id": cue["cue_id"],
                "status": "missing_material",
                "reason": "no_high_confidence_licensed_match",
                "timeline_start_s": cue["timeline_start_s"],
                "duration_s": cue["duration_s"],
                "candidates": ranked[:5],
            }
            if "semantic_spotting" in cue_sheet:
                missing.update(
                    {
                        "review_status": "blocked",
                        "semantic_event": cue["event"],
                        "function": cue["function"],
                        "priority": cue["priority"],
                        "visual_anchor": cue["visual_anchor"],
                        "narration_anchor": cue["narration_anchor"],
                        "spotting_rationale": cue["rationale"],
                    }
                )
            selections.append(missing)
            continue
        source_duration = min(cue["duration_s"], float(top["source_duration_s"]))
        fade_in = min(cue["fade_in_s"], source_duration / 2)
        fade_out = min(cue["fade_out_s"], source_duration - fade_in)
        selected: dict[str, Any] = {
            "cue_id": cue["cue_id"],
            "status": "preferred_selected" if preferred else "auto_selected",
            "asset_id": top["asset_id"],
            "score": top["score"],
            "timeline_start_s": cue["timeline_start_s"],
            "duration_s": source_duration,
            "source_audio": top["source_audio"],
            "source_interval": {"start_s": 0.0, "end_s": source_duration},
            "gain_db": cue["gain_db"],
            "fade_in_s": fade_in,
            "fade_out_s": fade_out,
            "license": top["license"],
            "evidence_ids": cue["evidence_ids"],
            "candidates": ranked[:5],
        }
        if "semantic_spotting" in cue_sheet:
            selected.update(
                {
                    "review_status": "needs_audition",
                    "semantic_event": cue["event"],
                    "function": cue["function"],
                    "priority": cue["priority"],
                    "visual_anchor": cue["visual_anchor"],
                    "narration_anchor": cue["narration_anchor"],
                    "spotting_rationale": cue["rationale"],
                }
            )
            if "state_change" in cue:
                selected["state_change"] = cue["state_change"]
        selections.append(selected)
    rejected.sort(key=lambda row: (row["cue_id"], row["asset_id"]))
    return selections, rejected


def _search_terms(value: str) -> set[str]:
    lowered = str(value or "").casefold().strip()
    tokens = {token for token in re.split(r"[^\w\u4e00-\u9fff]+", lowered) if token}
    if lowered:
        tokens.add(lowered)
    return tokens


def _text_list(value: Iterable[Any], label: str) -> list[str]:
    if isinstance(value, (str, bytes)):
        raise ContractError(f"{label} must be a list")
    result = sorted({_required_text(item, label).casefold() for item in value})
    return result


def _iso_datetime(value: Any, label: str) -> str:
    text = _required_text(value, label)
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})", text
    ):
        raise ContractError(f"{label} must be an ISO-8601 timestamp with timezone")
    return text


def _required_text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{label} is required")
    return result


def _choice(value: Any, choices: set[str], label: str) -> str:
    result = _required_text(value, label).casefold()
    if result not in choices:
        raise ContractError(f"{label} is unsupported")
    return result


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{label} must be a boolean")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ContractError(f"{label} must be a finite number")
    return result


def _nonnegative_number(value: Any, label: str) -> float:
    result = _number(value, label)
    if result < 0:
        raise ContractError(f"{label} must be non-negative")
    return result


def _positive_number(value: Any, label: str) -> float:
    result = _number(value, label)
    if result <= 0:
        raise ContractError(f"{label} must be positive")
    return result


def _ratio(value: Any, label: str) -> float:
    result = _number(value, label)
    if result < 0 or result > 1:
        raise ContractError(f"{label} must be between 0 and 1")
    return result


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
