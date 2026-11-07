"""Selection contracts only: no remote providers, media or installed-runtime claims."""
import json
from copy import deepcopy

import pytest

from video_creation_pipeline import object_scene_routes as routes
from video_creation_pipeline.contract_io import file_sha256
from video_creation_pipeline.errors import ContractError
from video_creation_pipeline.semantic_editorial_cli import main


def selected(result):
    return [row for rows in result['module_selection']['source_layers'].values() for row in rows]


def test_multi_layer_song_pv_and_original_performance():
    result = routes.recommend_scene_route('authored_performance', techniques=['song_pv','character_performance','editing'], narration_timing='word')
    assert result['authored_media_handoff_required'] and result['requires_scene_design']
    assert not result['source_media_required']
    selection = result['module_selection']
    assert {'workflow','shot','foundation','editor'} <= selection['source_layers'].keys()
    assert not selection['time_binding']['evidence_verified_by_this_selection']
    assert all(not row['execution_authorized'] for row in selected(result))
    assert all('preserve_source_pixels_use_annotation_layer_only' not in row['required_resolutions'] for row in selected(result))


def test_explicit_source_is_a_limit_and_mismatch_not_silently_accepted():
    result = routes.recommend_scene_route('flow', techniques=['diagram'], source_projects=['pathcraft'])
    rows = selected(result)
    assert [row['id'] for row in rows] == ['pathcraft']
    assert 'explicit_source_technique_fit_not_reviewed' in rows[0]['required_resolutions']


def test_interactive_does_not_invent_video_or_audio_requirements():
    result = routes.recommend_scene_route('flow', techniques=['interactive_web'], delivery='interactive')
    assert result['visual_route'] is None and result['component_id'] is None
    assert result['delivery_route'] == 'native_web_interaction'
    selection = result['module_selection']
    assert not selection['existing_scene_components'] and not selection['existing_consumers']
    assert selection['time_binding']['next_action'] == 'use_native_events_or_static_export_no_narration_required'


def test_video_flags_interactive_clock_and_source_only_status():
    result = routes.recommend_scene_route('flow', techniques=['interactive_web'])
    row = next(row for row in selected(result) if row['id']=='motion-web')
    assert 'deterministic_time_adapter_required' in row['required_resolutions']
    assert not row['evidence_check']['installed_or_callable_claimed']


def test_source_pixels_and_unknown_word_alignment_are_preserved():
    result = routes.recommend_scene_route('evidence', techniques=['collage'])
    assert result['visual_route'] == 'hyperframes_evidence_focus'
    row = next(row for row in selected(result) if row['layer']=='shot')
    assert 'preserve_source_pixels_use_annotation_layer_only' in row['required_resolutions']
    binding = result['module_selection']['time_binding']
    assert not binding['estimate_from_character_count']
    assert binding['next_action'].startswith('obtain_measured_audio_anchors')


def test_changed_missing_source_and_unresolved_identity(tmp_path, monkeypatch):
    catalog = routes.animation_source_catalog()
    project = next(p for p in catalog['projects'] if p['id']=='anatomy')
    source = tmp_path / 'source.js'
    source.write_text('reviewed')
    project['local_evidence'] = [{'path':str(source),'sha256':file_sha256(source)}]
    monkeypatch.setattr(routes, 'animation_source_catalog', lambda: deepcopy(catalog))
    assert selected(routes.recommend_scene_route('mechanism', source_projects=['anatomy']))[0]['evidence_check']['status']=='snapshot_verified'
    source.write_text('drifted')
    assert 'source_changed' in selected(routes.recommend_scene_route('mechanism', source_projects=['anatomy']))[0]['required_resolutions']
    source.unlink()
    assert 'source_missing' in selected(routes.recommend_scene_route('mechanism', source_projects=['anatomy']))[0]['required_resolutions']
    unknown = selected(routes.recommend_scene_route('flow',source_projects=['badcat-animate']))[0]
    assert unknown['selection_status']=='reference_only'
    assert 'project_identity_unresolved' in unknown['required_resolutions']


@pytest.mark.parametrize('kwargs', [
    {'techniques':['unknown']}, {'source_projects':['unknown']}, {'delivery':'audio'},
    {'narration_timing':'guessed'}, {'techniques':'diagram'},
])
def test_unknown_inputs_fail_instead_of_substituting(kwargs):
    with pytest.raises(ContractError):
        routes.recommend_scene_route('flow', **kwargs)


@pytest.mark.parametrize('kwargs', [
    {'techniques': {}}, {'techniques': ''}, {'techniques': ()},
    {'source_projects': {}}, {'source_projects': ''}, {'source_projects': ()},
    {'delivery': ''}, {'narration_timing': ''},
])
def test_invalid_empty_values_are_not_silently_replaced_by_defaults(kwargs):
    with pytest.raises(ContractError):
        routes.recommend_scene_route('flow', **kwargs)


def test_explicit_empty_lists_keep_selection_empty():
    result = routes.recommend_scene_route('flow', techniques=[], source_projects=[])
    assert result['module_selection']['status'] == 'needs_technique_or_source'
    assert result['module_selection']['source_layers'] == {}
    assert result['module_selection']['delivery'] == 'video'
    assert result['module_selection']['time_binding']['declared_evidence'] == 'unverified'


def test_no_local_evidence_is_visible_and_does_not_graduate_runtime():
    result = routes.recommend_scene_route('flow', source_projects=['roughjs'])
    row = selected(result)[0]
    assert 'local_source_or_native_api_evidence_required' in row['required_resolutions']
    assert row['selection_status']=='needs_resolution'
    assert not row['evidence_check']['installed_or_callable_claimed']


def test_existing_component_is_owned_by_original_catalog():
    module = pytest.importorskip('video_creation_pipeline.object_scenes', reason='Optional local object-authoring integration')
    object_scene_component_catalog = module.object_scene_component_catalog
    expected = next(row for row in object_scene_component_catalog()['components'] if row['id']=='queue_flow_2d')
    result = routes.recommend_scene_route('queue',techniques=['diagram'])
    assert result['module_selection']['existing_scene_components']==[expected]
    assert result['component_id'] is None
    assert result['base_component_candidate_id']=='queue_flow_2d'
    assert result['module_selection']['existing_component_fit'].startswith('unverified')


def test_source_routing_remains_available_without_optional_object_integration(monkeypatch):
    def missing(*args):
        raise ModuleNotFoundError('optional object_scenes module')
    monkeypatch.setattr(routes, 'import_module', missing)
    result = routes.recommend_scene_route('flow', techniques=['diagram'])
    assert result['module_selection']['source_layers']
    assert result['module_selection']['existing_scene_components'] == []
    assert result['module_selection']['existing_component_catalog_status']=='existing_source_inventory_unavailable'


def test_optional_inventory_is_reused_without_copying_components(monkeypatch):
    from types import SimpleNamespace
    entry={'id':'queue_flow_2d','sources':{'owned':'hash-bound fixture'}}
    monkeypatch.setattr(routes,'import_module',lambda *args:SimpleNamespace(object_scene_component_catalog=lambda:{'components':[entry]}))
    result=routes.recommend_scene_route('queue',techniques=['diagram'])
    assert result['module_selection']['existing_scene_components']==[entry]


def test_mechanism_label_does_not_match_every_device_to_slider_crank():
    result = routes.recommend_scene_route('mechanism',techniques=['anatomy_mechanism'])
    assert result['component_id'] is None and result['requires_scene_design']
    assert result['base_component_candidate_id']=='slider_crank_3d'


def test_cli_query_and_artifact_round_trip(tmp_path,capsys):
    artifact = tmp_path/'selection.json'
    assert main(['object-scene-route','--visual-job','flow','--technique','diagram','--technique','continuous_transform','--output',str(artifact)])==0
    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads(artifact.read_text(encoding='utf-8'))
    assert len(printed['module_selection']['techniques'])==2
    assert main(['animation-catalog'])==0
    assert len(json.loads(capsys.readouterr().out)['techniques'])>=24
    assert main(['object-scene-route','--visual-job','flow','--technique','not-real'])==2
    assert json.loads(capsys.readouterr().out)['status']=='blocked'
