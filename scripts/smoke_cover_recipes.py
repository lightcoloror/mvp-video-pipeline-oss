"""Local synthetic browser smoke for CoverStill, not a production render receipt.

Reuse an installed Remotion project; never mutate its source or install packages.
The new output directory must be a direct child so Node resolves that runtime.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from test_cover_recipes import fixture
from video_creation_pipeline.cover_recipes import RECIPES
from video_creation_pipeline.case_cover_visual_plan import case_cover_variant_projection
from video_creation_pipeline.remotion_project import _COVER_TSX


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--runtime", type=Path, required=True)
    p.add_argument("--browser", type=Path, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--node", default="node")
    args = p.parse_args()
    runtime = args.runtime.resolve()
    out = (runtime / args.name).resolve()
    if out.parent != runtime or out.exists():
        raise ValueError("smoke output must be a new direct child of the runtime")
    cli = runtime / "node_modules/@remotion/cli/remotion-cli.js"
    if not cli.is_file() or not args.browser.is_file():
        raise ValueError("local runtime or browser is absent; no download will be attempted")
    out.mkdir()
    public = out / "public"
    public.mkdir()
    for i, color in enumerate(("#1a7772", "#dc8242")):
        (public / f"synthetic-{i}.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="700" height="600"><rect width="700" height="600" rx="40" fill="{color}"/><circle cx="350" cy="210" r="100" fill="#f9eee0"/><path d="M130 520 Q350 190 570 520" fill="#f9eee0"/><text x="350" y="580" text-anchor="middle" font-size="42" fill="white">SYNTHETIC {i+1}</text></svg>', encoding="utf-8")
    variants = []
    for recipe in RECIPES:
        plan = fixture(recipe)
        safe = {"placements": [{"assetId": a["asset_id"], "publicPath": f'{a["asset_id"]}.svg'} for a in plan["assets"]]}
        variants.extend(case_cover_variant_projection(plan, safe_layout=safe))
    (out / "Cover.tsx").write_text(_COVER_TSX, encoding="utf-8")
    (out / "variants.json").write_text(json.dumps(variants, ensure_ascii=False), encoding="utf-8")
    (out / "index.tsx").write_text('''import React from "react";
import {registerRoot, Still} from "remotion";
import {CoverStill} from "./Cover";
import variants from "./variants.json";
const style = {background:"#f6f1e7", foreground:"#203238", primary:"#226963", accent:"#dd793f", muted:"#576668", panel:"#e4e7de", displayFont:"Microsoft YaHei", bodyFont:"Microsoft YaHei"};
registerRoot(() => <>{variants.map(v => <Still key={v.compositionId} id={v.compositionId} component={CoverStill as any} width={v.width} height={v.height} defaultProps={{variant:v,visualStyle:style}} />)}</>);
''', encoding="utf-8")
    results = []
    for v in variants:
        target = out / (v["candidateId"] + ".png")
        command = [args.node, str(cli), "still", str(out / "index.tsx"), v["compositionId"], str(target), "--browser-executable", str(args.browser.resolve()), "--public-dir", str(public), "--concurrency", "1"]
        run = subprocess.run(command, cwd=runtime, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        (out / (v["candidateId"] + ".log")).write_text(run.stdout + run.stderr, encoding="utf-8")
        results.append({"recipe": v["candidateId"], "returncode": run.returncode, "png_exists": target.is_file(), "path": str(target)})
        print(json.dumps(results[-1]), flush=True)
        (out / "smoke-results.json").write_text(json.dumps({"synthetic_only": True, "production_receipt": False, "results": results}, indent=2), encoding="utf-8")
        if run.returncode or not target.is_file(): return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
