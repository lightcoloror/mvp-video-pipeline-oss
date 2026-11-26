import type {FC} from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';
import {ObjectScene} from './ObjectScene';
import type {ObjectSceneData,Palette} from './object-motion';
import type {ProcessScene} from './process-motion';
// QUEUEFLOW2D_IMPORT
// SLIDERCRANK3D_IMPORT

export const ProcessObjectScene:FC<{scene:ObjectSceneData;palette:Palette}>=({scene:raw,palette:inherited})=>{
 const frame=useCurrentFrame();
 if(!['queue_flow_2d','slider_crank_3d'].includes(raw.componentId)) return <ObjectScene scene={raw} palette={inherited}/>;
 const scene=raw as ProcessScene;
 const palette=scene.palette??inherited;
 const r=scene.region;
 return <AbsoluteFill style={{background:palette.background,color:palette.foreground,fontFamily:palette.bodyFont}}>
  <div style={{position:'absolute',left:'10%',top:'9.6%',width:'67%',fontSize:62,fontWeight:750,lineHeight:1.3}}>{scene.headline}</div>
  <div style={{position:'absolute',left:`${r.x*100}%`,top:`${r.y*100}%`,width:`${r.width*100}%`,height:`${r.height*100}%`}}>
   {scene.componentId==='queue_flow_2d'? /* QUEUEFLOW2D_RENDER */ null : /* SLIDERCRANK3D_RENDER */ null}
  </div>
  <div style={{position:'absolute',left:'10%',top:'74.5%',width:'67%',fontSize:30,lineHeight:1.5,color:palette.muted}}>{scene.syntheticFixtureOnly?'视觉节拍预览 · 待接实际口播':'机制示意 · 动作依照原速口播'}<br/>{scene.process.simulationKind==='synthetic_discrete_event_queue'?'虚构物流模型；包裹身份与数量保持':'简化传动机构；部件保持连接'}</div>
 </AbsoluteFill>;
};
