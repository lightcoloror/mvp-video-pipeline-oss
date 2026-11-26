import type {FC} from 'react';
import type {ObjectSceneData, Palette} from './object-motion';
import {activity, splitProgress, emphasis} from './object-motion';
import {HOUSE_EASE} from './onda-easing';

export const ObjectFlow2D: FC<{scene: ObjectSceneData; frame: number; palette: Palette}> = ({scene, frame, palette}) => {
 const spread = splitProgress(scene, frame);
 const count = scene.objects.length;
 const origin = {x: 350, y: 95};
 const positions = scene.objects.map((_, i) => ({x: 350 + (i - (count-1)/2)*Math.min(280, 550/(count-1))*spread, y: 220 + i*(250/(count-1))*(1-spread) + 170*spread}));
 return <svg width="100%" height="100%" viewBox="0 0 700 650" role="img" aria-label={scene.headline}>
  <defs><pattern id={'grid-'+scene.id} width="35" height="35" patternUnits="userSpaceOnUse"><path d="M35 0H0V35" fill="none" stroke={palette.primary} strokeOpacity="0.07"/></pattern></defs>
  <rect width="700" height="650" fill={'url(#grid-'+scene.id+')'}/>
  <circle cx={origin.x} cy={origin.y} r="27" fill={palette.primary}/>
  {scene.originLabel ? <text x={origin.x} y={origin.y-48} textAnchor="middle" fill={palette.foreground} fontSize="38" fontFamily={palette.bodyFont}>{scene.originLabel}</text> : null}
  {positions.map((p,i) => {
   const node = scene.objects[i]; const t = activity(scene, frame, node.id);
   const focus = emphasis(scene, frame, node.id);
   const d = `M${origin.x} ${origin.y+30} C${origin.x} ${p.y-85} ${p.x} ${p.y-85} ${p.x} ${p.y-40}`;
   return <g key={node.id} data-object-id={node.id}>
    <path d={d} fill="none" stroke={palette.primary} strokeWidth="5" opacity="0.16"/>
    <path d={d} fill="none" stroke={palette.primary} strokeWidth="6" pathLength="1" strokeDasharray="1" strokeDashoffset={1-t}/>
    <circle cx={p.x} cy={p.y} r={28+10*t+6*focus} fill={t > 0.5 ? palette.accent : palette.background} stroke={palette.primary} strokeWidth={4+3*focus}/>
    <text x={p.x} y={p.y+80} textAnchor="middle" fill={palette.foreground} fontSize="38" fontWeight="650" fontFamily={palette.bodyFont}>{node.label}</text>
    {node.amount_label ? <text x={p.x} y={p.y+130} textAnchor="middle" fill={palette.primary} fontSize="38" opacity={HOUSE_EASE(t)} fontFamily={palette.bodyFont}>{node.amount_label}</text> : null}
   </g>;
  })}
 </svg>;
};
