import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from video_creation_pipeline.case_cover_visual_plan import validate_case_cover_visual_plan
from video_creation_pipeline.errors import ContractError


class CoverAssetBinding(unittest.TestCase):
    def test_same_id_cannot_replace_hash_bound_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def store(name,value):
                path=root/name;path.write_text(json.dumps(value),encoding='utf-8')
                return dict(path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            title=store('title.json',{'items':[{'id':'test','selected':{'publication_title':'Test title','cover_overlay':['Focus']}}]})
            asset=store('synthetic.bin',{'synthetic':True})
            b=dict(x=.1,y=.1,width=.8,height=.8)
            plan=dict(schema='lucas.case_cover_visual_plan.v1',candidate_only=True,manual_publish_only=True,
                title_cover_plan_ref=title,candidate=dict(candidate_id='test',title='Test title',overlay_text='Focus'),
                visual_route=dict(composition_strategy='person_led',hero_element_count=1),
                assets=[dict(asset_id='asset',availability='available',source_kind='video_frame',external_upload_allowed=False,artifact=asset)],
                profiles=[dict(profile_id='vertical_profile_3_4',width=1080,height=1440)],
                render_spec=dict(kind='person',graphic_kind='none',profile_layouts={'vertical_profile_3_4':dict(text_box=b,text_align='left',media=[dict(asset_id='asset',fit_mode='contain',crop_allowed=False,box=b)])}),
                render_handoff=dict(renderer='existing_remotion_still',new_renderer_created=False))
            good=store('good.json',{'placements':[dict(asset_id='asset',source_asset=asset)]})
            bad=store('bad.json',{'placements':[dict(asset_id='asset',source_asset={**asset,'sha256':'0'*64})]})
            # Isolate this new cross-contract equality gate; full safe-layout contracts
            # have separate tests and the current-project real render validation.
            with patch('video_creation_pipeline.case_cover_visual_plan.validate_safe_media_layout_plan'):
                validate_case_cover_visual_plan(plan,safe_media_layout_path=good['path'])
                with self.assertRaisesRegex(ContractError,'hash drifted'):
                    validate_case_cover_visual_plan(plan,safe_media_layout_path=bad['path'])


if __name__=='__main__':unittest.main()
