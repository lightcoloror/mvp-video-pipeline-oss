import type {ObjectSceneData} from './object-motion';
import {progress, smooth} from './object-motion';

export type Parcel = {id:string; entryFrame:number; readyFrame:number; releaseFrame:number; gateIndex:number; doneFrame:number};
export type ProcessScene = ObjectSceneData & {process:{parameters:Record<string,number|string>; colors:Record<string,string>; parcels?:Parcel[]; maximumWaiting?:number; simulationKind:string}};
export const eventProgress = (scene:ProcessScene, frame:number, id:string) => progress(frame, scene.events.find(e=>e.id===id)!);
export const queueCounts = (parcels:Parcel[], frame:number) => {
 const entered=parcels.filter(p=>p.entryFrame<=frame);
 const done=entered.filter(p=>p.doneFrame<=frame).length;
 return {entered:entered.length, done, waiting:entered.filter(p=>p.readyFrame<=frame && frame<p.releaseFrame).length, inSystem:entered.length-done};
};
export const parcelPosition = (parcels:Parcel[], parcel:Parcel, frame:number, approachFrames=70, headwayFrames=18) => {
 if(frame<parcel.releaseFrame) {
  const ahead=parcels.filter(p=>p.entryFrame<parcel.entryFrame && p.doneFrame>frame);
  const spacing=ahead.reduce((n,p)=>n+(1-smooth((frame-p.releaseFrame)/headwayFrames)),0)*74;
  const target=540-spacing;
  return {x:390,y:Math.min(target,80+(frame-parcel.entryFrame)*460/approachFrames),rotation:0};
 }
 const t=Math.max(0,Math.min(1,(frame-parcel.releaseFrame)/(parcel.doneFrame-parcel.releaseFrame)));
 const xEnd=parcel.gateIndex===0?215:565;
 // Queue -> branch -> gate -> exit, with the same parcel ID throughout.
 const branch=smooth(Math.min(1,t/0.5));
 const exit=smooth(Math.max(0,(t-0.5)/0.5));
 return {x:390+(xEnd-390)*branch,y:540+140*branch+320*exit,rotation:(xEnd<390?1:-1)*0.16*Math.sin(branch*Math.PI)};
};
export const sliderCrankState = (radius:number,length:number,angle:number) => {
 const x=radius*Math.sin(angle),y=radius*Math.cos(angle);
 const slider=y+Math.sqrt(length*length-x*x);
 return {pin:[x,y,0] as [number,number,number],slider:[0,slider,0] as [number,number,number],rodMidpoint:[x/2,(y+slider)/2,0] as [number,number,number],rodAngle:Math.atan2(x,slider-y)};
};
