"""Account-neutral editable layers for the existing Remotion Still outlet.

No URLs, scripts, model execution, arbitrary CSS, cropping or new renderer.
Coordinates describe the final canvas; protected image regions are source-relative.
"""
import math
import re

from .errors import ContractError
from .photo_editorial_cover import rect, overlaps
from .typography_gates import build_typography_gate


def number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ContractError("cover layer number out of range")
    return value


def color(value):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ContractError("cover layer requires six-digit hex color")


def validate_cover_layers(plan):
    layouts = plan['render_spec']['profile_layouts']
    if not any(layout.get('cover_layers') is not None for layout in layouts.values()):
        return
    assets = {a['asset_id']: a for a in plan['assets']}
    for profile in plan['profiles']:
        layout = layouts[profile['profile_id']]
        scene = layout.get('cover_layers')
        if scene is None:
            continue
        if layout.get('photo_editorial') is not None or plan['visual_route'].get('composition_recipe_id') is not None:
            raise ContractError('cover layers cannot override another treatment')
        if not isinstance(scene, dict) or scene.get('schema') != 'local.cover_layers.v1' or type(scene.get('generative_model_used')) is not bool:
            raise ContractError('unsupported cover layers or missing generation declaration')
        generated_used = False
        color(scene.get('background'))
        layers = scene.get('layers')
        if not isinstance(layers, list) or not 1 <= len(layers) <= 32:
            raise ContractError('cover needs one to 32 layers')
        media = {m['asset_id']: m for m in layout['media']}
        ids, used, protected, text_boxes = set(), set(), [], []
        gate = build_typography_gate(profile['profile_id'], width=profile['width'], height=profile['height'])
        for layer in layers:
            lid = layer.get('id')
            if not isinstance(lid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', lid) or lid in ids:
                raise ContractError('invalid or duplicate cover layer id')
            ids.add(lid)
            kind = layer.get('type')
            box = rect(layer.get('box'))
            if kind == 'image':
                aid = layer.get('asset_id')
                if aid not in media or aid in used or media[aid]['box'] != box:
                    raise ContractError('image layer must match unique bound media placement')
                used.add(aid)
                asset = assets[aid]
                if type(asset.get('generated')) is not bool or asset.get('use_approved') is not True or asset.get('privacy_reviewed') is not True:
                    raise ContractError('cover layers require explicitly approved media')
                if asset['generated']:
                    auth = plan.get('image_generation', {})
                    request = auth.get('request_text', '').replace(' ', '')
                    if scene['generative_model_used'] is not True or asset.get('role') != 'decoration' or asset.get('source_kind') != 'generated_editorial_visual':
                        raise ContractError('generated layer must be declared concept decoration, never real subject')
                    if auth.get('authorization_status') != 'explicit-user-authorized' or not any(t in request for t in ('AI生图', '模型生成图片')):
                        raise ContractError('generated decoration requires explicit user authorization')
                    generated_used = True
                dims = asset.get('dimensions')
                if not isinstance(dims, list) or len(dims) != 2:
                    raise ContractError('cover image dimensions required')
                iw, ih = [number(x, 1, 100000) for x in dims]
                bw, bh = box['width'] * profile['width'], box['height'] * profile['height']
                scale = min(bw / iw, bh / ih)
                dx = box['x'] + (bw - iw * scale) / 2 / profile['width']
                dy = box['y'] + (bh - ih * scale) / 2 / profile['height']
                if layer.get('subject_reviewed') is not True:
                    raise ContractError('image subject regions must be explicitly reviewed')
                df = layer.get('depth_fusion')
                if df is not None:
                    if not isinstance(df, dict) or df.get('mode') != 'text_behind_subject':
                        raise ContractError('invalid depth_fusion mode')
                    if df.get('alpha_matte_verified') is not True:
                        raise ContractError('depth_fusion requires verified transparent alpha matte')
                    backend = df.get('matting_backend')
                    if not isinstance(backend, str) or not backend.strip():
                        raise ContractError('depth_fusion requires matting_backend declaration')
                    max_occ = number(df.get('max_glyph_occlusion_ratio', 0.22), 0.01, 0.25)
                    meas_char = number(df.get('measured_max_char_occlusion'), 0.0, 1.0)
                    meas_title = number(df.get('measured_title_occlusion'), 0.0, 1.0)
                    if meas_char > max_occ + 1e-6:
                        raise ContractError(f'depth_fusion glyph occlusion {meas_char:.3f} exceeds limit {max_occ:.3f}')
                    if meas_title > 0.18 + 1e-6:
                        raise ContractError(f'depth_fusion total title occlusion {meas_title:.3f} exceeds limit 0.180')
                for region in layer.get('protected_regions', []):
                    r = rect(region)
                    protected.append((lid, dict(x=dx+r['x']*iw*scale/profile['width'], y=dy+r['y']*ih*scale/profile['height'], width=r['width']*iw*scale/profile['width'], height=r['height']*ih*scale/profile['height'])))
            elif kind == 'text':
                text = layer.get('text')
                if not isinstance(text, str) or not text.strip() or len(text) > 500:
                    raise ContractError('cover layer text required or too long')
                font = number(layer.get('font_px'), 1, 1000)
                if layer.get('role') not in gate['roles'] or font < gate['roles'][layer['role']]:
                    raise ContractError('cover layer font below canonical minimum')
                if layer.get('align', 'left') not in ('left', 'center', 'right'):
                    raise ContractError('invalid text alignment')
                color(layer.get('color'))
                safe = rect(profile.get('safe_zone', dict(x=0,y=0,width=1,height=1)))
                if box['x'] < safe['x'] or box['y'] < safe['y'] or box['x']+box['width'] > safe['x']+safe['width']+.00001 or box['y']+box['height'] > safe['y']+safe['height']+.00001:
                    raise ContractError('cover text escapes safe zone')
                lines = text.split('\n')
                if len(lines)*font*1.12 > box['height']*profile['height'] or any(sum(.65 if ord(c)<128 else 1.05 for c in line)*font > box['width']*profile['width']+.01 for line in lines):
                    raise ContractError('cover text does not fit; shorten or reflow, never shrink')
                if any(overlaps(box, b) for b in text_boxes):
                    raise ContractError('cover text layers overlap')
                text_boxes.append(box)
            elif kind in ('rect', 'ellipse', 'line'):
                color(layer.get('color'))
                number(layer.get('stroke_px', 4), 1, 40)
            else:
                raise ContractError('unsupported cover layer type')
        if used != set(media):
            raise ContractError('bound media missing from cover layers')
        if generated_used != scene['generative_model_used']:
            raise ContractError('cover generation declaration does not match bound layers')
        if generated_used and not any(x['type']=='text' and x.get('role')=='ai_disclosure' and 'AI' in x.get('text','') for x in layers):
            raise ContractError('generated decoration needs visible disclosure')
        if not any(x['type'] == 'text' and x['role'] == 'overlay' for x in layers):
            raise ContractError('cover layers need a headline')
        # Protect only against subsequently painted layers. Background underlays
        # are permitted; a later image must not cover an earlier protected face.
        # Explicit opt-in depth_fusion foreground cutouts may cross behind-subject
        # overlay masthead text when measured glyph occlusion passed the soft budget.
        order = {x['id']: i for i, x in enumerate(layers)}
        for text_layer in (x for x in layers if x['type']=='text'):
            for later in layers[order[text_layer['id']]+1:]:
                if not overlaps(text_layer['box'], later['box']):
                    continue
                is_allowed_depth_cutout = (
                    text_layer.get('role') == 'overlay'
                    and later.get('type') == 'image'
                    and isinstance(later.get('depth_fusion'), dict)
                    and later['depth_fusion'].get('mode') == 'text_behind_subject'
                    and later['depth_fusion'].get('alpha_matte_verified') is True
                )
                if not is_allowed_depth_cutout:
                    raise ContractError('later cover layer hides text')
        for owner, region in protected:
            for layer in layers[order[owner] + 1:]:
                if overlaps(region, layer['box']):
                    raise ContractError('cover layer covers protected subject')
