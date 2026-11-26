import {useMemo} from 'react';
import type {FC} from 'react';
import {ThreeCanvas} from '@remotion/three';
import {useVideoConfig} from 'remotion';
import {DoubleSide,Euler,PerspectiveCamera,Plane,Vector3} from 'three';
import type {Palette} from './object-motion';
import {eventProgress,sliderCrankState} from './process-motion';
import type {ProcessScene} from './process-motion';

export const SliderCrank3D:FC<{scene:ProcessScene;frame:number;palette:Palette}>=({scene,frame,palette})=>{
 const {width,height}=useVideoConfig();
 const w=Math.round(width*scene.region.width),h=Math.round(height*scene.region.height);
 const p=scene.process.parameters;
 const colors=scene.process.colors;
 const radius=Number(p.crank_radius),length=Number(p.rod_length),housing=Number(p.housing_radius);
 const angle=Number(p.initial_angle)+frame*2*Math.PI/Number(p.cycle_frames);
 const mechanism=sliderCrankState(radius,length,angle);
 const cut=eventProgress(scene,frame,String(p.cutaway_event_id));
 const orient=eventProgress(scene,frame,String(p.orient_event_id));
 const turn=-0.35+0.22*orient;
 const planes=useMemo(()=>[new Plane(new Vector3(0,0,-1).applyEuler(new Euler(0.06,turn,0)),1.65*(1-cut)-0.08)], [cut,turn]);
 const label=(id:string)=>scene.objects.find(n=>n.id===id)!.label;
 const focus=scene.events.filter(e=>e.action==='focus'&&e.startFrame<=frame).sort((a,b)=>b.startFrame-a.startFrame)[0];
 const focussed=focus?.targetId;
 const project=(point:[number,number,number])=>{
  const camera=new PerspectiveCamera(43,w/h,0.1,100);
  camera.position.set(0,0,10.5);
  camera.updateMatrixWorld();
  const v=new Vector3(...point).applyEuler(new Euler(0.06,turn,0)).add(new Vector3(0,-1.65,0)).project(camera);
  return [(v.x+1)*w/2,(1-v.y)*h/2];
 };
 const callouts=[
  {id:String(p.piston_object_id),color:colors.piston_annotation,point:[0,mechanism.slider[1],0.22] as [number,number,number],x:w*0.76,y:h*0.22},
  {id:String(p.rod_object_id),color:colors.rod_annotation,point:[mechanism.rodMidpoint[0],mechanism.rodMidpoint[1],0.22] as [number,number,number],x:w*0.02,y:h*0.52},
  {id:String(p.crank_object_id),color:colors.crank_annotation,point:[0,0,0.24] as [number,number,number],x:w*0.75,y:h*0.82},
 ];
 return <div style={{position:'absolute',inset:0}}>
  <ThreeCanvas width={w} height={h} camera={{position:[0,0,10.5],fov:43}} gl={{alpha:true,antialias:true,localClippingEnabled:true}}>
   <ambientLight intensity={1.45}/><directionalLight position={[4,6,8]} intensity={3.2}/><directionalLight position={[-5,1,3]} intensity={1.3}/>
   <group position={[0,-1.65,0]} rotation={[0.06,turn,0]}>
    <mesh position={[0,-1.48,0]}><boxGeometry args={[3.6,0.2,1.7]}/><meshStandardMaterial color={colors.base} roughness={0.85}/></mesh>
    <mesh position={[0,2.65,0]}>
     <cylinderGeometry args={[housing,housing,2.45,64,1,true]}/>
     <meshStandardMaterial color={colors.housing} metalness={0.45} roughness={0.36} side={DoubleSide} clippingPlanes={planes}/>
    </mesh>
    <mesh position={[0,2.65,0]}>
     <cylinderGeometry args={[housing-0.05,housing-0.05,2.45,64,1,true]}/>
     <meshStandardMaterial color={colors.housing_inner} metalness={0.3} roughness={0.58} side={DoubleSide} clippingPlanes={planes}/>
    </mesh>
    {[1.425,3.875].map(y=><mesh key={y} position={[0,y,0]} rotation={[-Math.PI/2,0,0]}>
     <ringGeometry args={[housing-0.05,housing,64]}/><meshStandardMaterial color={colors.cut_edge} side={DoubleSide} clippingPlanes={planes}/>
    </mesh>)}
    <mesh position={[0,0,0]} rotation={[Math.PI/2,0,0]}>
     <cylinderGeometry args={[1.4,1.4,1.3,64,1,true]}/><meshStandardMaterial color={colors.housing} metalness={0.4} roughness={0.45} side={DoubleSide} clippingPlanes={planes}/>
    </mesh>
    <mesh position={[0,0,0.67]} rotation={[Math.PI/2,0,0]}>
     <cylinderGeometry args={[1.4,1.4,0.08,64]}/><meshStandardMaterial color={colors.housing} metalness={0.4} roughness={0.4} clippingPlanes={planes}/>
    </mesh>
    <mesh position={[0,0,-0.67]} rotation={[Math.PI/2,0,0]}>
     <cylinderGeometry args={[1.4,1.4,0.08,64]}/><meshStandardMaterial color={colors.housing} metalness={0.4} roughness={0.4}/>
    </mesh>
    <mesh position={[0,mechanism.slider[1],0]}>
     <cylinderGeometry args={[Number(p.piston_radius),Number(p.piston_radius),0.52,48]}/><meshStandardMaterial color={colors.piston} metalness={0.65} roughness={0.26}/>
    </mesh>
    {[-0.14,0.14].map(delta=><mesh key={delta} position={[0,mechanism.slider[1]+delta,0]}>
     <cylinderGeometry args={[Number(p.piston_radius)+0.015,Number(p.piston_radius)+0.015,0.055,48]}/><meshStandardMaterial color={colors.piston_rings} metalness={0.45} roughness={0.3}/>
    </mesh>)}
    <mesh position={[mechanism.rodMidpoint[0],mechanism.rodMidpoint[1],0.22]} rotation={[0,0,mechanism.rodAngle]}>
     <boxGeometry args={[0.2,length,0.16]}/><meshStandardMaterial color={colors.rod} metalness={0.45} roughness={0.34}/>
    </mesh>
    {[mechanism.pin,mechanism.slider].map((point,i)=><mesh key={i} position={[point[0],point[1],0.22]} rotation={[Math.PI/2,0,0]}>
     <cylinderGeometry args={[0.2,0.2,0.27,24]}/><meshStandardMaterial color={colors.pins} metalness={0.55} roughness={0.25}/>
    </mesh>)}
    <group rotation={[0,0,-angle]}>
     <mesh rotation={[Math.PI/2,0,0]}><cylinderGeometry args={[1.15,1.15,0.14,64]}/><meshStandardMaterial color={colors.crank} metalness={0.5} roughness={0.4}/></mesh>
     {[0,1,2,3].map(i=><group key={i} rotation={[0,0,i*Math.PI/2]}><mesh position={[0,0.62,0.09]}><boxGeometry args={[0.12,0.95,0.08]}/><meshStandardMaterial color={colors.spokes} metalness={0.3} roughness={0.4}/></mesh></group>)}
     <mesh position={[0,radius,0.13]} rotation={[Math.PI/2,0,0]}><cylinderGeometry args={[0.14,0.14,0.28,24]}/><meshStandardMaterial color={colors.pins} metalness={0.5} roughness={0.28}/></mesh>
    </group>
    <mesh position={[0,0,0.7]} rotation={[Math.PI/2,0,0]}><cylinderGeometry args={[0.2,0.2,1.1,32]}/><meshStandardMaterial color={colors.shaft} metalness={0.75} roughness={0.24}/></mesh>
    <group position={[0,0,1.27]} rotation={[0,0,-angle]}>
     <mesh><ringGeometry args={[0.62,0.76,48]}/><meshStandardMaterial color={colors.flywheel} metalness={0.5} roughness={0.32} side={DoubleSide}/></mesh>
     {[0,1,2,3].map(i=><group key={i} rotation={[0,0,i*Math.PI/2]}><mesh position={[0,0.37,0]}><boxGeometry args={[0.095,0.65,0.12]}/><meshStandardMaterial color={colors.flywheel} metalness={0.5} roughness={0.32}/></mesh></group>)}
    </group>
   </group>
  </ThreeCanvas>
  <svg width="100%" height="100%" viewBox={`0 0 ${w} ${h}`} style={{position:'absolute',inset:0,pointerEvents:'none'}}>
   {cut>0.95?callouts.map(c=>{
    const [px,py]=project(c.point);
    const xEnd=c.x<w/2?c.x+88:c.x;
    return <g key={c.id} fontFamily={palette.bodyFont}>
     <path d={`M${px} ${py}L${xEnd} ${c.y+8}`} fill="none" stroke={c.color} strokeWidth={focussed===c.id?3:2}/>
     <circle cx={px} cy={py} r="5" fill={c.color}/>
     <text x={c.x} y={c.y} fontSize="36" fontWeight="650" fill={focussed===c.id?c.color:palette.foreground}>{label(c.id)}</text>
    </g>;
   }):null}
   {cut>0.95?<g fill={palette.muted} stroke={colors.piston_annotation}>
    <path d={`M${w*0.72} ${h*0.12}V${h*0.42}M${w*0.7} ${h*0.14}L${w*0.72} ${h*0.12}L${w*0.74} ${h*0.14}M${w*0.7} ${h*0.40}L${w*0.72} ${h*0.42}L${w*0.74} ${h*0.40}`} fill="none" strokeWidth="2"/>
   </g>:null}
  </svg>
 </div>;
};
