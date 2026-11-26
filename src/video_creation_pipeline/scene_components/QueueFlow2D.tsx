import type {FC} from 'react';
import type {Palette} from './object-motion';
import {eventProgress,parcelPosition,queueCounts} from './process-motion';
import type {ProcessScene} from './process-motion';

export const QueueFlow2D:FC<{scene:ProcessScene;frame:number;palette:Palette}>=({scene,frame,palette})=>{
 const p=scene.process.parameters;
 const colors=scene.process.colors;
 const parcels=scene.process.parcels!;
 const opening=eventProgress(scene,frame,String(p.open_event_id));
 const speed=eventProgress(scene,frame,String(p.speed_event_id));
 const counts=queueCounts(parcels,frame);
 const label=(id:string)=>scene.objects.find(n=>n.id===id)!.label;
 const visible=parcels.filter(box=>box.entryFrame<=frame && frame<box.doneFrame);
 const stripe=frame*2.8;
 return <svg width="100%" height="100%" viewBox="0 0 780 950" aria-label={scene.headline}>
  <defs>
   <pattern id={'paper-'+scene.id} width="26" height="26" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill={colors.belt_edge} opacity="0.2"/></pattern>
   <pattern id={'belt-'+scene.id} width="150" height="34" patternUnits="userSpaceOnUse" patternTransform={`translate(0 ${stripe})`}><path d="M0 0H150" stroke={colors.belt_slats} strokeOpacity="0.65" strokeWidth="3"/></pattern>
   <filter id={'shadow-'+scene.id} x="-50%" y="-50%" width="200%" height="200%"><feDropShadow dx="0" dy="5" stdDeviation="3" floodColor={palette.foreground} floodOpacity="0.12"/></filter>
  </defs>
  <rect width="780" height="950" fill={`url(#paper-${scene.id})`}/>
  <path d="M390 85V570Q390 628 215 680V1010M390 570Q390 628 565 680V1010" fill="none" stroke={colors.belt_edge} strokeWidth="154"/>
  <path d="M390 85V570Q390 628 215 680V1010M390 570Q390 628 565 680V1010" fill="none" stroke={colors.belt} strokeWidth="135"/>
  <rect x="323" y="86" width="134" height="472" fill={`url(#belt-${scene.id})`}/>
  <path d="M390 520V586M215 804V850M565 804V850" fill="none" stroke={colors.flow} strokeWidth="5" strokeDasharray="12 8"/>
  <path d="M376 572L390 589L404 572M201 837L215 854L229 837M551 837L565 854L579 837" fill="none" stroke={colors.flow} strokeWidth="5"/>
  <g fontFamily={palette.bodyFont} fill={palette.foreground}>
   <text x="40" y="64" fontSize="36" fontWeight="700">{label(String(p.parcel_object_id))}</text>
   <text x="40" y="115" fontSize="32" fill={palette.muted}>{speed>0.8?'到达间隔缩短':'上游持续送入'}</text>
   <path d="M215 114H292" stroke={colors.gate_frame} strokeWidth="3"/>
   <text x="530" y="236" fontSize="34">等待中</text>
   <text x="530" y="295" fontSize="52" fontWeight="750" fill={counts.waiting>0?colors.waiting:colors.clear}>{counts.waiting}<tspan fontSize="30" fill={palette.muted}> 件</tspan></text>
   <text x="530" y="353" fontSize="28" fill={palette.muted}>已经离开 {counts.done}</text>
  </g>
  {[215,565].map((x,i)=><g key={x}>
   <rect x={x-96} y="653" width="20" height="98" rx="6" fill={colors.gate_frame}/>
   <rect x={x+76} y="653" width="20" height="98" rx="6" fill={colors.gate_frame}/>
   <rect x={x-96} y={650-(i===0?1:opening)*66} width="192" height="28" rx="6" fill={i===0 || opening>=1?colors.open:colors.closed}/>
   <text x={i===0?99:666} y="705" textAnchor={i===0?'end':'start'} fill={palette.foreground} fontSize="30" fontWeight="650" fontFamily={palette.bodyFont}>{label(String(i===0?p.first_exit_object_id:p.second_exit_object_id)).slice(0,3)}{label(String(i===0?p.first_exit_object_id:p.second_exit_object_id)).length>3?<tspan x="666" dy="38">{label(String(p.second_exit_object_id)).slice(3)}</tspan>:null}</text>
   {i===1 && opening<1?<text x={x} y="819" textAnchor="middle" fontSize="30" fill={colors.closed} stroke={palette.background} strokeWidth="6" paintOrder="stroke" fontFamily={palette.bodyFont}>暂未开启</text>:null}
  </g>)}
  {visible.map(box=>{
   const s=parcelPosition(parcels,box,frame,Number(p.approach_frames),Number(p.fork_headway_frames));
   return <g key={box.id} data-parcel-id={box.id} transform={`translate(${s.x} ${s.y}) rotate(${s.rotation*180/Math.PI})`} filter={`url(#shadow-${scene.id})`}>
    <path d="M-35 -29H27L39 -17V32H-35Z" fill={colors.parcel} stroke={colors.parcel_edge} strokeWidth="2.5"/>
    <path d="M27 -29V-17H39M0 -29V32" fill="none" stroke={colors.tape} strokeWidth="5"/>
    <rect x="-27" y="-15" width="24" height="17" rx="2" fill={colors.label}/>
    <path d="M-24 -9H-6M-24 -4H-11" stroke={colors.parcel_edge} strokeWidth="2"/>
    <text x="15" y="24" textAnchor="middle" fill={colors.parcel_ink} fontSize="21" fontWeight="650" fontFamily={palette.bodyFont}>{Number(box.id.slice(-3))}</text>
   </g>;
  })}
 </svg>;
};
