"""Offline verification contracts layered on creative_tool_handoff, never an executor."""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path
import re

from .contract_io import artifact_reference, payload_sha256, read_json_object, validate_artifact_reference
from .creative_tool_handoff import REVIEWED_TOOLS, validate_creative_tool_handoff
from .errors import ContractError
from .generation_contracts import validate_generation_receipt

PLAN_SCHEMA = "video_creation_pipeline.creative_tool_verification_plan.v1"
FILM_RETURN_SCHEMA = "video_creation_pipeline.filmcraft_trial_return.v1"
FORMATS = ("Edl", "Fcp7Xml", "Fcpxml", "Otio", "Aaf", "Omf")
LOSS_AXES = ("timing", "subtitles_fonts", "audio", "effects_transitions", "media_paths", "canvas_color")
TRIAL_STEPS = ("import", "chinese_subtitles", "audio_video_clock", "save", "reopen", "local_edit", "export")
SHADER_EXTRA_PINS = {
    "packages/core/src/telemetry/config.ts": "dc9c0ea949dc93409b580bc4475ddf81542dfc91676ac15cc8d16ab8ad7d0423",
    "packages/core/src/telemetry/environmentCollector.ts": "4a72c99b16ebc9631416fabcaad10d60575f2feeb086b91c86f11a26a4c0fc6c",
    "packages/core/src/gpu/index.ts": "6074bb9ee83c31d11f25e516db5f07e41e68cb7ff06449808f8df773dd32e873",
}


def _require(ok, message):
    if not ok:
        raise ContractError(message)


def _object(value, label):
    _require(isinstance(value, dict), f"{label} must be an object")
    return value


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _ref(value, label):
    return validate_artifact_reference(_object(value, label), label)


def audit_shader_snapshot(handoff: dict) -> dict:
    """Pinned text evidence and missing dependency inventory, not a whole-package network verdict."""
    validate_creative_tool_handoff(handoff)
    _require(handoff["tool"] == "shaders", "Shaders handoff required")
    root = Path(handoff["reviewed_source"]["review_root"])
    pin = REVIEWED_TOOLS["shaders"]
    rows = read_json_object(root / "source-manifest.json", "source manifest")["files"]
    modules, texts = {}, {}
    for name, expected in {**pin["files"], **SHADER_EXTRA_PINS}.items():
        matches = [r for r in rows if r.get("repo") == pin["repo"] and r.get("path") == name]
        _require(len(matches) == 1, "audit source missing or duplicated")
        row = matches[0]
        relative = f"upstream/{pin['repo']}/{name}"
        path = (root / relative).resolve()
        _require(path.is_relative_to(root.resolve()) and row.get("local_path") == relative, "audit path escaped")
        _require(path.is_file() and path.stat().st_size <= 1_000_000, "audit source missing or too large")
        ref = artifact_reference(path, "audit source")
        _require(row.get("commit") == pin["commit"] and row.get("sha256") == expected == ref["sha256"], "audit source pin drifted")
        _require(row.get("bytes") == ref["bytes"], "audit source size drifted")
        modules[name], texts[name] = ref, path.read_text(encoding="utf-8")
    # Inventory only: unresolved modules are not guessed or fetched. Type imports also remain visible.
    missing = []
    for name, content in texts.items():
        for spec in sorted(set(re.findall(r"\bfrom\s+['\"]([^'\"]+)['\"]", content))):
            if not spec.startswith('.'):
                missing.append({"importer": name, "specifier": spec, "kind": "external_dependency"})
                continue
            target = (Path(name).parent / spec).as_posix()
            # Normalize .. lexically without reading an arbitrary filesystem dependency.
            target = str((root / target).resolve().relative_to(root.resolve())).replace('\\', '/')
            if target + '.ts' not in texts and target + '/index.ts' not in texts:
                missing.append({"importer": name, "specifier": spec, "kind": "outside_audited_modules"})
    return {
        "status": "supported", "scope": "fixed_source_text_audit_only", "modules": modules,
        "telemetry": {
            "collector_path": "start -> warmup -> interval samples -> aggregateData -> collectEnvironment -> sendTelemetry -> fetch POST",
            "endpoint": "https://shaders.com/api/telemetry", "configured_sampling_rate": 0.05,
            "sampling_activation_verified": False,
            "collected_environment": ["domain", "browserFamily", "deviceType"],
            "also_collects": ["sessionId", "timestamp", "version", "performance", "renderer", "components"],
            "entrypoint_activation": "unverified: constructor/start caller absent from selected snapshot",
            "performance_tracking_false_is_network_opt_out": False,
        },
        "dependency_closure_complete": False, "missing_imports": missing,
        "network_boundary": "blocked_until_full_entrypoint_and_assets_audited_and_external_requests_denied",
        "host_requirements": ["fresh renderer per scene/replay", "explicit namespaced node IDs and local mask references",
                              "rational output clock; advance simulations once per frame", "pause reuses last frame without rendering",
                              "static local fallback for unavailable GPU/reduced motion", "dispose on exit"],
        "runtime_verified": False, "gpu_state_isolation": "unverified_without_missing_time/root/texture_modules",
    }


def build_verification_plan(handoff_path, fps_numerator: int, fps_denominator: int) -> dict:
    handoff = read_json_object(handoff_path, "creative handoff")
    validate_creative_tool_handoff(handoff)
    _require(handoff["tool"] in {"shaders", "filmcraft"}, "PhotoCraft must use its existing revision contract; Yovoice remains deferred")
    _require(type(fps_numerator) is int and type(fps_denominator) is int and 0 < fps_numerator <= 240000 and 0 < fps_denominator <= 10000,
             "bounded positive rational output frame rate required")
    fps = Fraction(fps_numerator, fps_denominator)
    _require(Fraction(1) <= fps <= 240, "output frame rate must be between 1 and 240")
    duration = Fraction(str(handoff["work_request"]["duration_s"]))
    _require(0 < duration <= 60, "verification plan is limited to a short scene of at most 60 seconds")
    plan = {
        "schema": PLAN_SCHEMA, "source_handoff": artifact_reference(handoff_path, "creative handoff"),
        "tool": handoff["tool"], "scene_id": handoff["work_request"]["scene_id"],
        "clock": {"fps_numerator": fps.numerator, "fps_denominator": fps.denominator,
                  "duration_s": handoff["work_request"]["duration_s"], "origin": "host_supplied_output_rate",
                  "time_formula": "local_frame * fps_denominator / fps_numerator",
                  "scene_duration_binding": "source_handoff.work_request.duration_s"},
        "status": "candidate_only", "runtime_verified": False, "application_import_file": False,
        "executionAuthority": "none_metadata_only", "publish_approved": False,
    }
    if handoff['tool'] == 'shaders':
        plan['source_audit'] = audit_shader_snapshot(handoff)
        plan['return_requirements'] = {'frame_sequence': '0 then one delta=1/fps per intervening frame',
            'backward_seek': 'dispose and replay from zero with a fresh renderer', 'pause': 'no render call',
            'static_fallback': 'local approved frame; no alternate GPU/backend startup',
            'runtime_evidence': 'separate from synthetic backend tests'}
    else:
        plan['source_audit'] = {'status': 'supported', 'scope': 'interchange_dispatch_declarations_only',
            'declared_import_export_formats': list(FORMATS), 'reports_required': ['import', 'export'],
            'reports_are_not_optional': 'unsupported features become warnings instead of exceptions',
            'details': ['AAF/OMF generic import reports embedded audio not extracted',
                        'EDL frame rate must be explicit; source code otherwise guesses',
                        'XML extension is ambiguous; inspect document type',
                        'exchange fragments are not native FilmCraft projects'],
            'windows_build_provenance': 'unverified: Cargo.toml, CLI main, release binaries/checksums absent',
            'runtime_verified': False}
        plan['return_requirements'] = {'schema': FILM_RETURN_SCHEMA, 'trial_steps': list(TRIAL_STEPS),
            'loss_axes': list(LOSS_AXES), 'windows_provenance': 'binary hash plus captured build/release origin evidence',
            'manual_media': 'existing manual video generation receipt, then existing QA and human review'}
    plan['plan_sha256'] = payload_sha256(plan, 'plan_sha256')
    return plan


def validate_verification_plan(plan: dict) -> None:
    _require(plan.get('schema') == PLAN_SCHEMA and plan.get('plan_sha256') == payload_sha256(plan, 'plan_sha256'), 'verification plan integrity failed')
    source = _ref(plan.get('source_handoff'), 'creative handoff')
    clock = _object(plan.get('clock'), 'clock')
    _require(plan == build_verification_plan(source, clock.get('fps_numerator'), clock.get('fps_denominator')), 'verification plan content drifted')


def validate_filmcraft_return(payload: dict) -> dict:
    """Validate reported artifacts; never upgrade them to independent native/runtime acceptance."""
    _require(payload.get('schema') == FILM_RETURN_SCHEMA and payload.get('return_sha256') == payload_sha256(payload, 'return_sha256'), 'FilmCraft return integrity failed')
    plan = read_json_object(_ref(payload.get('source_plan'), 'verification plan'), 'verification plan')
    validate_verification_plan(plan)
    _require(plan['tool'] == 'filmcraft' and payload.get('scene_id') == plan['scene_id'], 'FilmCraft scene/tool mismatch')
    _require(payload.get('status') == 'candidate_only' and payload.get('publish_approved') is False, 'FilmCraft return must remain an unpublished candidate')
    _require(payload.get('clock') == plan['clock'], 'output clock drifted')
    trial = _object(payload.get('software_trial'), 'software trial')
    _require(trial.get('status') in {'blocked', 'unverified', 'reported_executed'} and _text(trial.get('limitations')), 'software trial status/limitations required')
    losses = _object(payload.get('loss_report'), 'loss report')
    _require(set(losses) == set(LOSS_AXES), 'all FilmCraft loss axes required')
    pending = []
    for axis, row in losses.items():
        _object(row, axis)
        _require(row.get('status') in {'preserved', 'changed', 'unsupported', 'unverified'} and _text(row.get('detail')), 'loss status/detail required')
        refs = row.get('evidence')
        _require(isinstance(refs, list), 'loss evidence must be a list')
        for ref in refs: _ref(ref, f'{axis} evidence')
        if row['status'] != 'unverified': _require(bool(refs), 'inspected loss requires evidence')
        if row['status'] != 'preserved': pending.append(axis)
    reports = _object(payload.get('interchange_reports'), 'interchange reports')
    _require(set(reports) == {'import', 'export'}, 'both import and export reports required')
    entries = []
    for direction, row in reports.items():
        _object(row, direction)
        _require(row.get('status') in {'unverified', 'reported'}, 'explicit report status required')
        if row['status'] == 'reported':
            _require(row.get('format') in FORMATS, 'format not declared in pinned interchange source')
            report = read_json_object(_ref(row.get('artifact'), f'{direction} raw report'), 'raw report')
            _require(isinstance(report.get('entries'), list), 'raw report entries required')
            for entry in report['entries']:
                _object(entry, 'report entry')
                _require(entry.get('level') in {'Info', 'Warning'} and _text(entry.get('message')) and type(entry.get('count')) is int and entry['count'] > 0, 'invalid upstream report entry')
                entries.append({'direction': direction, **entry})
        else:
            _require(set(row) == {'status'}, 'unverified report cannot include fabricated results')
    handoff = read_json_object(plan['source_handoff']['path'], 'creative handoff')
    validation = None
    if trial['status'] == 'reported_executed':
        steps = _object(trial.get('steps'), 'software trial steps')
        _require(set(steps) == set(TRIAL_STEPS), 'all seven software trial steps required')
        for step, ref in steps.items(): _ref(ref, f'FilmCraft {step}')
        provenance = _object(trial.get('windows_provenance'), 'Windows provenance')
        _require(provenance.get('platform') == 'windows' and _text(provenance.get('version')), 'Windows platform/version required')
        _ref(provenance.get('binary'), 'installed Windows binary')
        _ref(provenance.get('origin_evidence'), 'Windows build/release provenance')
        native = _ref(payload.get('native_project'), 'reported native project')
        native_hash = artifact_reference(native, 'reported native project')['sha256']
        _require(native_hash not in {plan['source_handoff']['sha256'], handoff['source_shot_unit']['sha256'], payload['source_plan']['sha256']}, 'request packet is not a native project')
        _ref(payload.get('candidate_media'), 'candidate media')
    else:
        _require(all(row['status'] == 'unverified' for row in losses.values()), 'unrun software cannot claim inspected losses')
        _require(all(row['status'] == 'unverified' for row in reports.values()), 'unrun software cannot claim interchange results')
        _require(all(payload.get(k) is None for k in ('native_project', 'candidate_media', 'manual_media')), 'unrun software cannot return native/media execution results')
    binding = payload.get('manual_media')
    if binding is not None:
        _object(binding, 'manual media')
        task = read_json_object(_ref(binding.get('task'), 'generation task'), 'generation task')
        receipt = read_json_object(_ref(binding.get('receipt'), 'generation receipt'), 'generation receipt')
        _require(task.get('generator') == 'manual' and task.get('media_type') == 'video' and task.get('source_unit') == handoff['source_shot_unit'], 'existing manual video task for this shot required')
        _require(receipt.get('output') == payload['candidate_media'], 'manual receipt does not bind candidate media')
        validation = validate_generation_receipt(task, receipt)
        _require(validation['status'] == 'completed', 'manual generation receipt incomplete')
    ready = trial['status'] == 'reported_executed' and validation is not None and not pending and not entries and all(r['status'] == 'reported' for r in reports.values())
    return {'status': 'valid_filmcraft_contract_only', 'runtime_verified': False,
            'evidence_level': 'reported_artifacts_not_independent_software_acceptance',
            'software_trial_status': trial['status'], 'loss_review_required': pending,
            'interchange_review_required': entries, 'can_enter_existing_qa': ready,
            'native_project_verified': False, 'publish_approved': False}


def consume_photocraft_return(path, expected_handoff_path) -> dict:
    """Video consumer only; the design owner's contract remains authoritative."""
    from .photocraft_revision import validate_revision_return
    payload = read_json_object(path, 'PhotoCraft return')
    result = validate_revision_return(payload)
    expected = read_json_object(expected_handoff_path, 'expected PhotoCraft handoff')
    validate_creative_tool_handoff(expected)
    _require(expected['tool'] == 'photocraft', 'expected PhotoCraft handoff required')
    plan = read_json_object(payload['source_plan']['path'], 'PhotoCraft plan')
    _require(plan['source_handoff'] == artifact_reference(expected_handoff_path, 'expected PhotoCraft handoff'), 'PhotoCraft returned material belongs to a different handoff')
    return {'status': 'supported', 'scope': 'existing_material_contract_consumer',
            'source_return': artifact_reference(path, 'PhotoCraft return'), 'validation': result,
            'can_enter_existing_qa': result['can_enter_existing_qa'], 'runtime_verified': False,
            'editor_execution_performed': False, 'publish_approved': False}
