"""Build, validate or render optional video progress packaging via the existing outlet."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from video_creation_pipeline.contract_io import atomic_write_json, artifact_reference
from video_creation_pipeline.progress_overlay import build, render, validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('build')
    for flag in ('program', 'packaging', 'layout', 'spec', 'workspace-root', 'output'):
        create.add_argument('--' + flag, required=True)
    create.add_argument('--script', help='Video script; Markdown H2 headings are the default chapter titles')
    check = sub.add_parser('validate')
    check.add_argument('--plan', required=True)
    run = sub.add_parser('render')
    run.add_argument('--plan', required=True)
    run.add_argument('--output-root', required=True)
    args = parser.parse_args()
    if args.action == 'build':
        plan = build(args.program, args.packaging, args.layout, args.spec, args.workspace_root,
                     script_ref=artifact_reference(args.script,'video script') if args.script else None)
        print(atomic_write_json(plan, args.output))
    elif args.action == 'validate':
        plan = validate(args.plan)
        print(json.dumps({'status': 'valid', 'chapters': len(plan['chapters'])}))
    else:
        print(json.dumps(render(args.plan, args.output_root), ensure_ascii=False))


if __name__ == '__main__':
    main()
