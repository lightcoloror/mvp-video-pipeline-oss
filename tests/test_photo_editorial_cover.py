import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from video_creation_pipeline.photo_editorial_cover import validate_photo_editorial
from video_creation_pipeline.photo_layout import build_photo_media_preflight
from video_creation_pipeline.errors import ContractError


def fixture():
    rect=dict(x=0,y=0,width=1,height=1)
    return dict(visual_route={}, assets=[dict(asset_id='real',generated=False,use_approved=True,privacy_reviewed=True,dimensions=[1080,1440])], profiles=[dict(profile_id='vertical_profile_3_4',width=1080,height=1440)],render_spec={'profile_layouts':{'vertical_profile_3_4':{'media':[dict(asset_id='real',box=rect)],'photo_editorial':dict(schema='local.photo_editorial_cover.v1',hero_asset_id='real',protected_regions=[dict(x=.2,y=.3,width=.6,height=.4)],text_nodes=[dict(text='测试',role='overlay',font_px=150,box=dict(x=.1,y=.8,width=.8,height=.18))])}}})


class PhotoEditorialTests(unittest.TestCase):
    def test_valid_photo_and_legacy(self):
        validate_photo_editorial(fixture())
        validate_photo_editorial({'render_spec':{'profile_layouts':{'old':{}}}})

    def test_low_font_overlap_generated_hero_and_crop_are_blocked(self):
        for case in ['font','overlap','generated','crop','protected','recipe']:
            p=fixture()
            e=p['render_spec']['profile_layouts']['vertical_profile_3_4']['photo_editorial']
            if case=='font': e['text_nodes'][0]['font_px']=30
            if case=='overlap': e['text_nodes'][0]['box']['y']=.4
            if case=='generated': p['assets'][0]['generated']=True
            if case=='crop': p['assets'][0]['dimensions']=[1080,1920]
            if case=='protected': e['protected_regions']=[]
            if case=='recipe': p['visual_route']['composition_recipe_id']='minimal-whitespace'
            with self.subTest(case=case), self.assertRaises(ContractError): validate_photo_editorial(p)

    def test_generated_decoration_requires_authorization_and_disclosure(self):
        p=fixture()
        p['assets'].append(dict(asset_id='ai',generated=True,role='decoration',use_approved=True))
        lay=p['render_spec']['profile_layouts']['vertical_profile_3_4']
        lay['media'].append(dict(asset_id='ai',box=dict(x=0,y=0,width=.1,height=.1)))
        with self.assertRaisesRegex(ContractError,'authorization'): validate_photo_editorial(p)
        p['image_generation']=dict(authorization_status='explicit-user-authorized',request_text='可以启用AI生图')
        with self.assertRaisesRegex(ContractError,'disclosure'): validate_photo_editorial(p)
        lay['photo_editorial']['text_nodes'].append(dict(text='AI 插画',role='ai_disclosure',font_px=24,box=dict(x=.2,y=.05,width=.5,height=.05)))
        validate_photo_editorial(p)

    def test_unicode_source_preflight(self):
        import cv2
        import numpy as np
        with tempfile.TemporaryDirectory(prefix='photo-unicode-') as root:
            folder=Path(root)/'中文目录'
            folder.mkdir()
            ok,data=cv2.imencode('.png',np.full((100,100,3),220,dtype=np.uint8))
            self.assertTrue(ok)
            (folder/'原图.png').write_bytes(data.tobytes())
            p=build_photo_media_preflight(folder)
            self.assertEqual(p['summary']['ready_count'],1)

if __name__=='__main__': unittest.main()
