import type {FC} from 'react';
import {ThreeCanvas} from '@remotion/three';
import {useVideoConfig} from 'remotion';
import type {ObjectSceneData, Palette} from './object-motion';
import {activity, splitProgress, cameraProgress, emphasis} from './object-motion';

export const StageTimeline3D: FC<{scene: ObjectSceneData; frame: number; palette: Palette}> = ({scene, frame, palette}) => {
 const {width, height} = useVideoConfig();
 const spread = splitProgress(scene, frame);
 const center = (scene.objects.length-1)/2;
 // A bounded camera move explains the stage arrangement; all state is a function of frame.
 const turn = cameraProgress(scene, frame);
 return <div style={{position: 'absolute', inset: 0}}>
  <ThreeCanvas width={Math.round(width*scene.region.width)} height={Math.round(height*scene.region.height)} camera={{position: [0, 6.5, 8.8], rotation: [-Math.atan2(7,8.8),0,0], fov: 42}} gl={{alpha: true, antialias: true}}>
   <ambientLight intensity={1.6}/><directionalLight position={[3,7,4]} intensity={2.5}/>
   <group rotation={[-0.05, -0.22 + 0.22*turn, 0]} position={[0,-0.7,0]}>
    <mesh rotation={[-Math.PI/2,0,0]} position={[0,-0.13,0]}><planeGeometry args={[6.5,3.5]}/><meshStandardMaterial color={palette.background} roughness={1}/></mesh>
    <mesh position={[0,0,0]}><boxGeometry args={[5.5,0.05,0.075]}/><meshStandardMaterial color={palette.primary} roughness={0.85}/></mesh>
    {scene.objects.map((node,i) => {
     const active = activity(scene,frame,node.id);
     const focus = emphasis(scene,frame,node.id);
     const x = (i-center)*(1.05+1.7*spread);
     return <group key={node.id} position={[x, 0, 0]}>
      <mesh position={[0,0.13,0]}><boxGeometry args={[1.8,0.24,1.35]}/><meshStandardMaterial color={palette.primary} roughness={0.9}/></mesh>
      <mesh position={[0,0.5+0.08*active+0.12*focus,0]} rotation={[0,0.12*(1-active),0]}><boxGeometry args={[1.25,0.22,0.95]}/><meshStandardMaterial color={palette.accent} roughness={0.8} transparent opacity={0.2+0.8*active}/></mesh>
      <mesh position={[0,0.27,0.84]}><sphereGeometry args={[0.085,16,12]}/><meshStandardMaterial color={palette.accent}/></mesh>
     </group>;
    })}
   </group>
  </ThreeCanvas>
  <div style={{position:'absolute',left:0,right:0,bottom:'9%',display:'flex',justifyContent:'space-around',gap:18}}>
   {scene.objects.map(node => <div key={node.id} data-object-id={node.id} style={{textAlign:'center',flex:1,color:palette.foreground,fontFamily:palette.bodyFont,fontSize:38,fontWeight:650}}>
    <div>{node.label}</div>{node.amount_label ? <div style={{color:palette.primary,fontSize:36,marginTop:16}}>{node.amount_label}</div> : null}
   </div>)}
  </div>
 </div>;
};
