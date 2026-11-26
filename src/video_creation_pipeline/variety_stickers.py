"""Deterministic transparent sticker frames from an attributed text-motion recipe."""
from functools import lru_cache
import math
from pathlib import Path

from .contract_io import artifact_reference, atomic_write_json, file_sha256, read_json_object
from .errors import ContractError
from .execution_receipts import execute_ffmpeg_with_receipt
from .review_resource_guard import ReviewResourceGuard, ReviewResourcePolicy

RECIPE = Path(__file__).with_name('policies')/'variety-laughter-recipe.v1.json'
MODES = {'variety', 'variety_reduced'}


def source_refs():
    return {'recipe':artifact_reference(RECIPE,'variety recipe'),
            'renderer':artifact_reference(__file__,'sticker frame renderer'),
            'license':artifact_reference(Path(__file__).with_name('scene_components')/'SAKURA_TEXT_LICENSE.txt','source license')}


def geometry(event, effect, size, width, height):
    onsets = event.get('syllable_frames')
    if len(event['text']) != 3 or any(c not in '哈嘿呵嘻' for c in event['text']):
        raise ContractError('variety v1 requires three explicit laughter characters')
    if (not isinstance(onsets,list) or len(onsets) != 3 or any(type(n) is not int for n in onsets)
            or onsets[0] != event['start_frame'] or not onsets[0] < onsets[1] < onsets[2] < event['end_frame']):
        raise ContractError('variety needs three verified syllable_frames inside the sound event')
    if effect['end_scale'] != 1:
        raise ContractError('variety size hierarchy comes from its recipe; end_scale must be 1')
    strengths = event.get('syllable_strengths', [1., 1., 1.])
    if (not isinstance(strengths, list) or len(strengths) != 3
            or any(isinstance(n, bool) or not isinstance(n, (int, float))
                   or not math.isfinite(n) or not .7 <= n <= 1 for n in strengths)):
        raise ContractError('syllable_strengths need three finite values in 0.7..1')
    r=read_json_object(RECIPE,'variety recipe')['motion_extent_em']
    x,y=effect['x_px'],effect['y_px']
    return {'x':(x-r['left']*size)/width,'y':(y-r['up']*size-effect['rise_px'])/height,
            'width':(r['left']+r['right'])*size/width,
            'height':((r['up']+r['down'])*size+effect['rise_px'])/height}


def _bezier(t, curve):
    if t <= 0: return 0.
    if t >= 1: return 1.
    x1,y1,x2,y2=curve
    def at(u,a,b): return 3*(1-u)**2*u*a+3*(1-u)*u*u*b+u**3
    lo,hi=0.,1.
    for _ in range(24):
        u=(lo+hi)/2
        if at(u,x1,x2)<t: lo=u
        else: hi=u
    return at((lo+hi)/2,y1,y2)


def letter_state(cue, index, frame, fps, recipe):
    onset=cue['syllable_frames'][index]
    if frame < onset or frame >= cue['visual_end_frame']: return None
    reduced=cue['mode']=='variety_reduced'
    age=(frame-onset)/fps
    progress=_bezier(min(1,age/recipe['entry_seconds']),recipe['curve'])
    values=recipe['keyframes']
    left,right=values[-2:]
    for a,b in zip(values,values[1:]):
        if a['t'] <= progress <= b['t']: left,right=a,b; break
    q=(progress-left['t'])/(right['t']-left['t'])
    motion={k:left[k]+(right[k]-left[k])*q for k in ('scale','opacity','y')}
    strength=cue.get('syllable_strengths',[1.,1.,1.])[index]
    # A later voiced pulse briefly moves already visible letters as one cluster.
    response=0.
    if not reduced:
        for later in cue['syllable_frames'][index+1:]:
            p=(frame-later)/(fps*recipe['entry_seconds'])
            if 0 <= p <= 1:
                response=max(response,math.sin(math.pi*p)*(1-p))
    delay=0 if reduced else round(index*recipe['exit_stagger_fraction']*cue['tail_frames'])
    tail=max(0,(frame-cue['end_frame']-delay)/max(1,cue['tail_frames']-delay))
    pose=recipe['letter_poses'][index]
    # Explicit reveal timing; motion-free variant keeps the same readable shape.
    return {'x_em':pose['x_em']+(0 if reduced else (index-1)*.18*tail),
            'y_em':pose['y_em']-(0 if reduced else recipe['response_rise_em']*response),
            'y_px':0 if reduced else motion['y']*strength-cue['rise_px']*tail,
            'scale':pose['size']*strength*(1 if reduced else motion['scale']*(1+recipe['response_scale']*response)),
            'squash':0 if reduced else math.sin(min(1,age/recipe['entry_seconds'])*math.pi)*.12*strength,
            'angle':pose['angle']+(0 if reduced else (index-1)*6*(1-progress)),
            'opacity':(1 if reduced else motion['opacity'])*(1-tail), 'age':age}


@lru_cache(maxsize=128)
def _letter_tile(font_path,size,char,color,outer,inner,outline,cream,shadow):
    from PIL import Image, ImageDraw, ImageFont
    font=ImageFont.truetype(font_path,size)
    box=font.getbbox(char,stroke_width=outer)
    pad=outer+shadow+8
    image=Image.new('RGBA',(box[2]-box[0]+2*pad,box[3]-box[1]+2*pad),(0,0,0,0))
    d=ImageDraw.Draw(image)
    x,y=pad-box[0],pad-box[1]
    d.text((x+shadow,y+shadow),char,font=font,fill=outline,stroke_width=outer,stroke_fill=outline)
    d.text((x,y),char,font=font,fill=cream,stroke_width=outer,stroke_fill=outline)
    d.text((x,y),char,font=font,fill=color,stroke_width=inner,stroke_fill=cream)
    return image


def draw_frame(plan, frame):
    from PIL import Image, ImageDraw
    o=plan['output']; size=plan['font_size_px']
    canvas=Image.new('RGBA',(o['width'],o['height']),(0,0,0,0))
    recipe=read_json_object(RECIPE,'variety recipe')
    for cue in plan['captions']:
        layer=Image.new('RGBA',canvas.size,(0,0,0,0)); d=ImageDraw.Draw(layer)
        if cue['mode']=='variety' and cue['start_frame'] <= frame < cue['end_frame']:
            # Three hand-selected rays at either side of the laugh cluster.
            for index,onset in enumerate(cue['syllable_frames']):
                p=(frame-onset)/max(1,round(.30*o['fps']))
                if not 0 <= p < 1: continue
                radius=size*(1.75+.35*p)
                for degrees in (155,185,210) if index<2 else (-35,0,25):
                    a=math.radians(degrees)
                    cx,cy=cue['x_px'],cue['y_px']-size*.12
                    start=(cx+radius*math.cos(a),cy+radius*.60*math.sin(a))
                    end=(cx+(radius+size*.22)*math.cos(a),cy+(radius+size*.22)*.60*math.sin(a))
                    alpha=round(255*(1-p))
                    d.line([start,end],fill=(23,44,57,alpha),width=max(4,round(size*.055)))
                    d.line([start,end],fill=tuple(bytes.fromhex(cue['color']))+(alpha,),width=max(2,round(size*.025)))
        for i,char in enumerate(cue['text']):
            state=letter_state(cue,i,frame,o['fps'],recipe)
            if not state or state['opacity'] <= 0: continue
            tile=_letter_tile(plan['font']['path'],size,char,'#'+cue['color'],round(size*recipe['stroke_outer_em']),
                round(size*recipe['stroke_inner_em']),'#'+recipe['outline_color'],'#'+recipe['inner_color'],round(size*recipe['shadow_em']))
            sx=state['scale']*(1+state['squash']); sy=state['scale']*(1-state['squash'])
            tile=tile.resize((max(1,round(tile.width*sx)),max(1,round(tile.height*sy))),Image.Resampling.LANCZOS)
            tile=tile.rotate(state['angle'],resample=Image.Resampling.BICUBIC,expand=True)
            if state['opacity']<1:
                tile.putalpha(tile.getchannel('A').point(lambda a: round(a*state['opacity'])))
            x=round(cue['x_px']+size*state['x_em']-tile.width/2)
            y=round(cue['y_px']+size*state['y_em']+state['y_px']-tile.height/2)
            layer.alpha_composite(tile,(x,y))
        box=layer.getbbox()
        if box:
            rect=cue['rect']; allowed=(rect['x']*o['width'],rect['y']*o['height'],
                (rect['x']+rect['width'])*o['width'],(rect['y']+rect['height'])*o['height'])
            if any(v < b-1 for v,b in zip(box[:2],allowed[:2])) or any(v > b+1 for v,b in zip(box[2:],allowed[2:])):
                raise ContractError('rendered sticker exceeds its checked motion envelope')
        canvas.alpha_composite(layer)
    return canvas


def materialize(plan, plan_path, folder):
    frames=folder/'sticker-frames'; frames.mkdir()
    rows=[]
    with ReviewResourceGuard(policy=ReviewResourcePolicy.from_environment()) as guard:
        for frame in range(plan['output']['frames']):
            path=frames/f'{frame:05d}.png'; image=draw_frame(plan,frame); image.save(path)
            rows.append({'frame':frame,'alpha_bbox':image.getbbox(),'sha256':file_sha256(path)})
    guard_receipt=guard.receipt_metadata()
    manifest=atomic_write_json({'frames':rows,'recipe':source_refs(),'resource_guard':guard_receipt,
        'format':'RGBA PNG','timing':'locked program frame index'},folder/'sticker-manifest.json')
    video=folder/'sticker.webm'
    execute_ffmpeg_with_receipt('render',['ffmpeg','-nostdin','-v','error','-n','-framerate',str(plan['output']['fps']),
        '-i',str(frames/'%05d.png'),'-an','-c:v','libvpx-vp9','-lossless','1','-pix_fmt','yuva420p','-auto-alt-ref','0',str(video)],
        [{'role':'plan','path':plan_path},{'role':'frame_manifest','path':manifest}],
        [{'role':'transparent_sticker','path':video}],folder/'sticker-receipt.json',guard_resource=True,timeout_s=600)
    return frames,manifest,video
