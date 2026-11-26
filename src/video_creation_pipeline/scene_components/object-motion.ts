import {interpolate} from 'remotion';
export type Action = {id: string; action: string; targetId: string; startFrame: number; endFrame: number; payoffFrame: number};
export type SceneObject = {id: string; label: string; amount_label?: string};
export type ObjectSceneData = {id: string; componentId: string; startFrame: number; durationInFrames: number; headline: string; originLabel?: string; initialSplit: number; initialActiveIds: string[]; objects: SceneObject[]; events: Action[]; syntheticFixtureOnly: boolean; palette: Palette; region: {x: number; y: number; width: number; height: number}};
export type Palette = {background: string; foreground: string; primary: string; accent: string; muted: string; bodyFont: string};
export const smooth = (t: number) => {const x = Math.max(0, Math.min(1, t)); return x*x*(3-2*x);};
export const progress = (frame: number, event: Action) => smooth(interpolate(frame, [event.startFrame, event.endFrame], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'}));
export const activity = (scene: ObjectSceneData, frame: number, objectId: string) => {
 const events = scene.events.filter(e => e.targetId === objectId && ['connect','reveal','advance'].includes(e.action));
 return Math.max(scene.initialActiveIds.includes(objectId) ? 1 : 0, ...events.map(e => progress(frame, e)));
};
export const splitProgress = (scene: ObjectSceneData, frame: number) => {
 const events = scene.events.filter(e => e.action === 'split');
 return Math.max(scene.initialSplit, ...events.map(e => progress(frame, e)));
};
export const cameraProgress = (scene: ObjectSceneData, frame: number) => {
 const events = scene.events.filter(e => e.action === 'advance');
 return Math.max(0, ...events.map(e => progress(frame, e)));
};
export const emphasis = (scene: ObjectSceneData, frame: number, objectId: string) => {
 const events = scene.events.filter(e => e.action === 'focus' && e.startFrame <= frame).sort((a,b) => b.startFrame-a.startFrame);
 if (!events.length) return 0;
 return events[0].targetId === objectId ? progress(frame, events[0]) : 0;
};
