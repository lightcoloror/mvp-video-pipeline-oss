"""Synthetic contract tests, never desktop import/export evidence."""
import copy
import json
import unittest
from pathlib import Path
import test_creative_tool_handoff as fixtures
from video_creation_pipeline.contract_io import artifact_reference, atomic_write_json, payload_sha256
from video_creation_pipeline.creative_tool_handoff import build_creative_tool_handoff, main
from video_creation_pipeline.creative_gates import build_approval_record
from video_creation_pipeline.generation_contracts import build_generation_task, CAPABILITY_PREFLIGHT_SCHEMA, GENERATION_RECEIPT_SCHEMA
from video_creation_pipeline.photocraft_revision import (
    BRIEF_SCHEMA, RETURN_SCHEMA, LOSS_AXES, TRIAL_STEPS,
    build_revision_plan, validate_revision_plan, validate_revision_return,
)
from video_creation_pipeline.errors import ContractError


def synthetic_brief(editable):
    return {"schema": BRIEF_SCHEMA, "source_kind": "synthetic", "editable_source": editable,
            "canvas": {"width": 1080, "height": 1920, "color_space": "sRGB"},
            "layers": [
                {"id": "source-swatch", "order": 0, "parent_id": None, "kind": "vector",
                 "visible": True, "opacity": 1, "blend_mode": "normal", "mask": "none", "effects": [],
                 "color_role": {"kind": "material", "reason": "Keep synthetic orange object's color unchanged."}},
                {"id": "annotation", "order": 1, "parent_id": None, "kind": "text", "visible": True,
                 "opacity": .75, "blend_mode": "normal", "mask": "synthetic rectangular mask", "effects": [],
                 "text": "用于人机修订合同验证的长中文注释，不能冒充真实客户证据。",
                 "font": {"family": "Noto Sans SC", "style": "Regular", "availability": "unverified", "substitution_allowed": False},
                 "color_role": {"kind": "brand_annotation", "reason": "Separate annotation layer; do not recolor the source swatch."}}],
            "crop": {"policy": "preserve_canvas", "rect_px": [0,0,1080,1920], "reason": "Preserve source context and alpha edges."},
            "export": {"preview_format": "png", "alpha": "preserve", "keep_editable_source": True},
            "requested_change": "Move only the annotation layer; report all font, mask and export losses."}


class PhotoCraftRevisionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.CreativeToolHandoffTests()
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.make_unit('image')
        self.root = self.f.root
        self.handoff = atomic_write_json(self.f.build('photocraft'), self.root/'handoff.json')
        source = self.root/'source.svg'; source.write_text('<svg><!-- synthetic layered-source placeholder; no editor trial --></svg>')
        self.brief = synthetic_brief(artifact_reference(source, 'editable'))
        self.brief_path = self.root/'brief.json'; self.save_brief()

    def save_brief(self):
        atomic_write_json(self.brief, self.brief_path)

    def plan(self):
        return build_revision_plan(self.handoff, self.brief_path)

    def returned(self):
        plan_path = atomic_write_json(self.plan(), self.root/'plan.json')
        edit = self.root/'edited.svg'; edit.write_text('<svg><!-- synthetic edited-source placeholder --></svg>')
        preview = self.root/'preview.png'; preview.write_bytes(b'synthetic-not-decoded-png-placeholder')
        data = {'schema': RETURN_SCHEMA, 'source_plan': artifact_reference(plan_path,'plan'),
                'scene_id':'storyboard-0001','status':'candidate_only','publish_approved':False,
                'editable_result':artifact_reference(edit,'edit'),'preview':artifact_reference(preview,'preview'),
                'change_notes':'Synthetic contract fixture; no software execution or real image verification.',
                'loss_report':{axis:{'status':'unverified','before':'Original requested property','after':'Not measured',
                                    'detail':'No installed editor was run.','evidence':[]} for axis in LOSS_AXES},
                'software_trial':{'status':'blocked','environment':'synthetic fixture only','limitations':'PhotoCraft unavailable'},
                'manual_media':None}
        return self.rehash(data)

    def rehash(self, data):
        data['return_sha256'] = payload_sha256(data,'return_sha256'); return data

    def test_plan_is_deterministic_and_binds_existing_handoff(self):
        plan = self.plan(); self.assertEqual(plan,self.plan()); validate_revision_plan(plan)
        self.assertFalse(plan['application_import_file']); self.assertFalse(plan['runtime_verified'])

    def test_non_photocraft_handoff_rejected(self):
        atomic_write_json(self.f.build('shaders'),self.handoff)
        with self.assertRaisesRegex(ContractError,'PhotoCraft'): self.plan()

    def test_layer_and_opacity_errors_rejected(self):
        for key,value in [('order',0),('id','source-swatch'),('opacity',float('nan')),('opacity',True),('parent_id','unknown')]:
            with self.subTest(key=key):
                old=copy.deepcopy(self.brief);self.brief['layers'][1][key]=value;self.save_brief()
                with self.assertRaises(ContractError): self.plan()
                self.brief=old

    def test_font_substitution_is_never_silent(self):
        self.brief['layers'][1]['font']['substitution_allowed']=True;self.save_brief()
        with self.assertRaisesRegex(ContractError,'substitution'):self.plan()

    def test_crop_canvas_and_export_loss_policy_required(self):
        for section,key,value in [('crop','rect_px',[0,0,500,500]),('canvas','width',1000),('export','keep_editable_source',False),('export','alpha','automatic')]:
            with self.subTest(key=key):
                old=copy.deepcopy(self.brief);self.brief[section][key]=value;self.save_brief()
                with self.assertRaises(ContractError):self.plan()
                self.brief=old

    def test_plan_rehash_does_not_grant_authority(self):
        plan=self.plan();plan['runtime_verified']=True;plan['plan_sha256']=payload_sha256(plan,'plan_sha256')
        with self.assertRaisesRegex(ContractError,'authority drifted'):validate_revision_plan(plan)

    def test_original_source_drift_rejected(self):
        plan=self.plan();Path(self.brief['editable_source']['path']).write_text('changed')
        with self.assertRaisesRegex(ContractError,'changed after'):validate_revision_plan(plan)

    def test_blocked_return_is_valid_metadata_but_not_ready_for_qa(self):
        result=validate_revision_return(self.returned())
        self.assertFalse(result['can_enter_existing_qa']);self.assertFalse(result['runtime_verified'])
        self.assertEqual(result['loss_review_required'],list(LOSS_AXES))

    def test_missing_loss_axis_and_false_preservation_rejected(self):
        value=self.returned();del value['loss_report']['fonts']
        with self.assertRaisesRegex(ContractError,'five loss axes'):validate_revision_return(self.rehash(value))
        value=self.returned();value['loss_report']['fonts']['status']='preserved'
        value['loss_report']['fonts']['evidence']=[value['preview']]
        with self.assertRaisesRegex(ContractError,'unrun software'):validate_revision_return(self.rehash(value))

    def test_trial_requires_all_five_actual_evidence_references(self):
        value=self.returned();value['software_trial']['status']='reported_executed';value['software_trial']['steps']={}
        with self.assertRaisesRegex(ContractError,'open/edit/save/reopen/export'):validate_revision_return(self.rehash(value))

    def test_original_cannot_be_returned_as_mutable_output(self):
        value=self.returned();value['editable_result']=self.brief['editable_source']
        with self.assertRaisesRegex(ContractError,'distinct'):validate_revision_return(self.rehash(value))

    def test_manual_media_validation_reused_and_stays_candidate(self):
        # Deliberately synthetic attestations test wiring only, not image decoding or human review.
        value=self.returned()
        ap=atomic_write_json(build_approval_record('storyboard',self.f.unit,approved_by='synthetic-test-fixture',approved_at='2026-10-11T00:00:00+08:00'),self.root/'approval.json')
        pf=atomic_write_json({'schema':CAPABILITY_PREFLIGHT_SCHEMA,'checked_at':'2026-10-11T00:00:00+08:00',
            'capabilities':[{'id':'manual_media_import','ready':True,'probe_method':'manual_operator_verification',
                             'installation_performed':False,'evidence':{'synthetic':True}}]},self.root/'preflight.json')
        task=build_generation_task(self.f.unit,ap,pf,output_dir=self.root)
        tp=atomic_write_json(task,self.root/'task.json')
        receipt={'schema':GENERATION_RECEIPT_SCHEMA,'task_id':task['task_id'],'task_sha256':task['task_sha256'],
            'generator':'manual','status':'completed','published':False,'fallback_used':False,'output':value['preview'],
            'technical_verification':{'width':1080,'height':1920,'decodable_image':True,'probe_method':'synthetic-test-only'},
            'visual_verification':{'inspected':True,'blank':False,'black':False,'wrong_composition':False,'representative_frames':[value['preview']]}}
        rp=atomic_write_json(receipt,self.root/'receipt.json')
        value['manual_media']={'task':artifact_reference(tp,'task'),'receipt':artifact_reference(rp,'receipt')}
        result=validate_revision_return(self.rehash(value));self.assertEqual(result['manual_media_validation']['acceptance_status'],'candidate_only')
        self.assertFalse(result['can_enter_existing_qa'])
        receipt['visual_verification']['blank']=True;atomic_write_json(receipt,rp)
        value['manual_media']['receipt']=artifact_reference(rp,'receipt')
        with self.assertRaisesRegex(ContractError,'visual inspection'):validate_revision_return(self.rehash(value))

    def test_cli_reuses_existing_entry_and_never_overwrites_plan(self):
        target=self.root/'cli-plan.json'
        args=['revision-plan','--handoff',str(self.handoff),'--brief',str(self.brief_path),'--output',str(target)]
        self.assertEqual(main(args),0);self.assertEqual(main(args),2)
        self.assertEqual(main(['revision-validate','--input',str(target)]),0)


if __name__=='__main__':unittest.main()
