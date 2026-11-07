"""Compose explicit local audio/visual bindings; no automatic content generation."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from video_creation_pipeline.audio_timeline import compose, prepare

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['validate', 'render', 'performance-plan', 'performance-assets'])
    p.add_argument('--packaging', required=True)
    p.add_argument('--binding')
    p.add_argument('--spec', help='Explicit synthetic performance spec for performance-plan')
    p.add_argument('--plan', help='Hash-bound performance plan for performance-assets')
    p.add_argument('--cache')
    p.add_argument('--output')
    a = p.parse_args()
    if a.action == 'performance-plan':
        if not a.spec or not a.output:
            p.error('performance-plan requires --spec and --output')
        from video_creation_pipeline.explainer_recipes import build_performance_recipe
        from video_creation_pipeline.contract_io import atomic_write_json
        print(atomic_write_json(build_performance_recipe(a.packaging, a.spec), a.output))
    elif a.action == 'performance-assets':
        if not a.plan or not a.cache or not a.output:
            p.error('performance-assets requires --plan, --cache and --output')
        from video_creation_pipeline.performance_recipe import materialize
        from video_creation_pipeline.contract_io import read_json_object, artifact_reference
        plan = read_json_object(a.plan, 'performance plan')
        if plan['packaging'] != artifact_reference(a.packaging, 'packaging'):
            p.error('performance plan packaging mismatch')
        print(json.dumps(materialize(a.plan, a.cache, a.output)))
    elif not a.binding:
        p.error('validate/render requires --binding')
    elif a.action == 'validate':
        _, rows = prepare(a.packaging, a.binding)
        print(json.dumps({'valid': True, 'segments': len(rows)}))
    else:
        if not a.cache or not a.output:
            p.error('render requires --cache and --output')
        print(json.dumps(compose(a.packaging, a.binding, a.cache, a.output)))
