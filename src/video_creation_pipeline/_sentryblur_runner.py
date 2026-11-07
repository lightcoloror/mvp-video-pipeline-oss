from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

RUNTIME_PROVIDER_MARKER = "VIDEO_CREATION_ORT_RUNTIME="
SESSION_PROVIDER_MARKER = "VIDEO_CREATION_ORT_SESSION="
CAPTURE_PATH_ENV = "VIDEO_CREATION_REDACTION_CAPTURE_PATH"
CAPTURE_TARGET_ENV = "VIDEO_CREATION_REDACTION_CAPTURE_TARGET"


def _write_marker(prefix: str, payload: dict[str, Any]) -> None:
    print(prefix + json.dumps(payload, sort_keys=True), flush=True)


def _configure_provider_audit(*, gpu_requested: bool) -> str:
    import onnxruntime as ort

    requested = "CUDAExecutionProvider" if gpu_requested else "CPUExecutionProvider"
    preloaded = False
    if gpu_requested:
        try:
            ort.preload_dlls(directory="")
            preloaded = True
        except Exception as exc:
            raise RuntimeError(f"failed to preload CUDA/cuDNN runtime: {exc}") from exc

    available = list(ort.get_available_providers())
    _write_marker(
        RUNTIME_PROVIDER_MARKER,
        {
            "runtime_version": str(ort.__version__),
            "requested_execution_provider": requested,
            "available_execution_providers": available,
            "cuda_libraries_preloaded": preloaded,
        },
    )
    if requested not in available:
        raise RuntimeError(f"requested execution provider is unavailable: {requested}")

    return requested


def _assert_session_provider(requested: str, actual: list[str]) -> None:
    _write_marker(
        SESSION_PROVIDER_MARKER,
        {
            "requested_execution_provider": requested,
            "actual_execution_providers": actual,
        },
    )
    if not actual or actual[0] != requested:
        raise RuntimeError(
            "ONNX session silently fell back from "
            f"{requested}: actual providers={actual}"
        )


def _install_detector_audit(requested: str) -> None:
    from insightface.app import FaceAnalysis
    from sentryblur.detectors import OpenImagePlateDetector

    original_prepare = FaceAnalysis.prepare

    def audited_prepare(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original_prepare(self, *args, **kwargs)
        providers: list[str] = []
        for model in self.models.values():
            if not hasattr(model, "session"):
                continue
            for provider in model.session.get_providers():
                value = str(provider)
                if value not in providers:
                    providers.append(value)
        _assert_session_provider(requested, providers)
        return result

    FaceAnalysis.prepare = audited_prepare

    original_plate_load = OpenImagePlateDetector._ensure_loaded

    def audited_plate_load(self: Any) -> None:
        original_plate_load(self)
        providers = _providers_from_object(self._detector)
        if not providers:
            raise RuntimeError("could not audit the plate detector ONNX session")
        _assert_session_provider(requested, providers)

    OpenImagePlateDetector._ensure_loaded = audited_plate_load


def _providers_from_object(value: Any, *, depth: int = 0) -> list[str]:
    if value is None or depth > 2:
        return []
    getter = getattr(value, "get_providers", None)
    if callable(getter):
        return [str(provider) for provider in getter()]
    for name in ("session", "_session", "ort_session", "model", "detector"):
        nested = getattr(value, name, None)
        providers = _providers_from_object(nested, depth=depth + 1)
        if providers:
            return providers
    return []


def _provider_doctor() -> None:
    requested = _configure_provider_audit(gpu_requested=True)
    _install_detector_audit(requested)
    from sentryblur.detectors import SCRFDFaceDetector

    SCRFDFaceDetector(use_gpu=True)


def _install_coordinate_capture(
    *, target: str, output_path: Path
) -> tuple[list[dict[str, Any]], Path]:
    if target not in {"face", "plate"}:
        raise RuntimeError("redaction coordinate capture target is invalid")
    if output_path.exists():
        raise RuntimeError("redaction coordinate capture output already exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    from sentryblur.detectors import OpenImagePlateDetector, SCRFDFaceDetector

    detector_type = SCRFDFaceDetector if target == "face" else OpenImagePlateDetector
    original_detect = detector_type.detect
    rows: list[dict[str, Any]] = []
    frame_index = 0

    def recording_detect(self: Any, frame_bgr: Any) -> Any:
        nonlocal frame_index
        detections = original_detect(self, frame_bgr)
        height, width = frame_bgr.shape[:2]
        for detection_index, value in enumerate(detections):
            values = [float(item) for item in value]
            rows.append(
                {
                    "observation_id": (
                        f"{target}-{frame_index:08d}-{detection_index:04d}"
                    ),
                    "frame_index": frame_index,
                    "frame_width": int(width),
                    "frame_height": int(height),
                    "xyxy": values[:4],
                    "confidence": values[4] if len(values) >= 5 else 1.0,
                    "target_type": target,
                }
            )
        frame_index += 1
        return detections

    detector_type.detect = recording_detect
    return rows, output_path


def _promote_capture(rows: list[dict[str, Any]], output_path: Path) -> None:
    temporary = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        temporary.replace(output_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> None:
    if sys.argv[1:] == ["--provider-doctor"]:
        _provider_doctor()
        return

    requested = _configure_provider_audit(gpu_requested="--gpu" in sys.argv[1:])
    _install_detector_audit(requested)
    capture_path = os.environ.get(CAPTURE_PATH_ENV)
    capture_target = os.environ.get(CAPTURE_TARGET_ENV)
    capture: tuple[list[dict[str, Any]], Path] | None = None
    if capture_path or capture_target:
        if not capture_path or not capture_target:
            raise RuntimeError("redaction coordinate capture environment is incomplete")
        capture = _install_coordinate_capture(
            target=capture_target,
            output_path=Path(capture_path).expanduser().resolve(),
        )
    import sentryblur.cli as sentryblur_cli

    sentryblur_cli._open_file = lambda _path: None
    if capture is None:
        sentryblur_cli.cli(prog_name="sentryblur")
        return
    sentryblur_cli.cli(prog_name="sentryblur", standalone_mode=False)
    _promote_capture(*capture)


if __name__ == "__main__":
    main()
