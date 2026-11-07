from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--draft-content", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args(argv)

    source_root = Path(args.source_root).expanduser().resolve()
    draft_content = Path(args.draft_content).expanduser().resolve()
    output = Path(args.output_dir).expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"project output already exists: {output}")
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

    from draft_profiles import (  # type: ignore[import-not-found]
        get_draft_profile,
        write_profile_content,
    )

    profile = get_draft_profile(args.profile)
    template = source_root / profile.template_dir
    if not template.is_dir():
        raise FileNotFoundError(f"VectCut template not found: {template}")
    content = draft_content.read_text(encoding="utf-8-sig")
    shutil.copytree(template, output)
    write_profile_content(profile, output, content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
