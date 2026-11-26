"""Build, validate and render a cue-bound laughter overlay using the existing outlet."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from video_creation_pipeline.contract_io import atomic_write_json
from video_creation_pipeline.expressive_captions import build, render, validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('build')
    for name in ('program','packaging','layout','events','spec','workspace-root','output'):
        create.add_argument('--'+name, required=True)
    sub.add_parser('validate').add_argument('--plan', required=True)
    run = sub.add_parser('render')
    run.add_argument('--plan', required=True)
    run.add_argument('--output-root', required=True)
    args = parser.parse_args()
    if args.action == 'build':
        print(atomic_write_json(build(args.program,args.packaging,args.layout,args.events,args.spec,args.workspace_root), args.output))
    elif args.action == 'validate':
        plan = validate(args.plan)
        print(json.dumps({'status':'valid','events':len(plan['captions'])}))
    else:
        print(json.dumps(render(args.plan,args.output_root),ensure_ascii=False))


if __name__ == '__main__':
    main()
