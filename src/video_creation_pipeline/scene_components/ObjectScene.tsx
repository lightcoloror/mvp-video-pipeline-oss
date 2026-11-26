import type {FC} from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';
import type {ObjectSceneData, Palette} from './object-motion';
import {ObjectFlow2D} from './ObjectFlow2D';
// THREE_IMPORT
export const ObjectScene: FC<{scene: ObjectSceneData; palette: Palette}> = ({scene, palette: inheritedPalette}) => {
 const frame = useCurrentFrame();
 const palette = scene.palette ?? inheritedPalette;
 const r = scene.region;
 return <AbsoluteFill style={{backgroundColor:palette.background,color:palette.foreground,fontFamily:palette.bodyFont}}>
  <div style={{position:'absolute',left:'10%',top:'10%',width:'67%',fontSize:62,fontWeight:750,lineHeight:1.25}}>{scene.headline}</div>
  <div style={{position:'absolute',left:`${r.x*100}%`,top:`${r.y*100}%`,width:`${r.width*100}%`,height:`${r.height*100}%`}}>
   {scene.componentId === 'relation_flow_2d' ? <ObjectFlow2D scene={scene} frame={frame} palette={palette}/> : /* THREE_RENDER */ null}
  </div>
  <div style={{position:'absolute',left:'10%',top:'77%',width:'67%',fontSize:32,lineHeight:1.4,color:palette.muted}}>{scene.syntheticFixtureOnly ? '合成测试 · 无真实口播或客户素材' : '本案例流程示意；间距不代表实际时长'}</div>
 </AbsoluteFill>;
};
