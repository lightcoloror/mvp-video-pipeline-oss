// Executes the real inline script with a tiny DOM/storage fixture.
// This is NOT browser, layout, real keyboard, or screen-reader acceptance.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const path=require('node:path');
const source=process.argv[2]||path.join(__dirname,'../src/video_creation_pipeline/author_editor.html');
const html=fs.readFileSync(source,'utf8');
const code=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const defaults={kind:'at_for',boundary:'start',offset:'0',offsetUnit:'seconds',duration:'5',unit:'seconds',endBoundary:'end'};
const selectValues={kind:['at_for','until_for','span'],boundary:['start','end'],offsetUnit:['seconds','frames'],unit:['seconds','frames'],endBoundary:['end','start']};
function fixture(data={},storage=new Map(),denyStorage=false,failDownload=false){
  const doc={activeElement:null},els={};let downloads=0;
  class Element{
    constructor(id,tag='DIV'){this.id=id;this.tagName=tag;this._value=defaults[id]||'';this.textContent='';this.style={};this.attrs={};this.options=[];this.disabled=false;this.hidden=false;this.handlers={};}
    get value(){return this._value} set value(v){this._value=String(v)}
    set selectedIndex(i){this._value=this.options[i]?.value||''}
    append(el){this.options.push(el);if(this.options.length===1)this._value=el.value}
    addEventListener(name,fn){this.handlers[name]=fn}
    setAttribute(k,v){this.attrs[k]=v}removeAttribute(k){delete this.attrs[k]}
    focus(){doc.activeElement=this}
    click(){if(this.tagName==='A'){if(failDownload)throw Error('denied');downloads++}else if(!this.disabled)this.onclick?.()}
  }
  for(const match of html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"[^>]*>/g))els[match[2]]=new Element(match[2],match[1].toUpperCase());
  for(const [id,values]of Object.entries(selectValues))if(els[id])for(const v of values){const e=new Element('', 'OPTION');e.value=v;els[id].append(e)}
  els.download.disabled=true;
  const long='这是一段需要完整保留且不改变证据含义的长中文词锚点'.repeat(12);
  const content={source_spec:{sha256:'a'.repeat(64)},duration:20,fps:25,origin:0,
    cues:[{cue_id:'synthetic-cue',timing:{full_frame_s:.2,enter_s:.3,exit_s:.5,hold_s:4,timeline_start_s:1}}],
    words:[{word_id:'w1',text:long,start_ms:1000,end_ms:1500},{word_id:'w2',text:'终点',start_ms:7000,end_ms:7500}],...data};
  els.data.textContent=JSON.stringify(content);
  doc.getElementById=id=>els[id];doc.createElement=tag=>new Element('',tag.toUpperCase());
  doc.querySelectorAll=()=>Object.values(els).filter(e=>['INPUT','SELECT'].includes(e.tagName));
  const sessionStorage={getItem(k){if(denyStorage)throw Error('denied');return storage.get(k)||null},setItem(k,v){if(denyStorage)throw Error('denied');storage.set(k,v)},removeItem(k){if(denyStorage)throw Error('denied');storage.delete(k)}};
  let error=null;
  try{vm.runInNewContext(code,{document:doc,sessionStorage,URL:{createObjectURL:()=> 'blob:synthetic',revokeObjectURL(){}},Blob:class{},setTimeout:fn=>fn()},{timeout:1000})}catch(e){error=e}
  return {els,doc,storage,error,long,get downloads(){return downloads},input(id,value){els[id].value=value;els[id].handlers.input?.()},preview(){els.preview.click()}};
}
const checks=[];
function check(name,fn){try{fn();checks.push({name,status:'passed'})}catch(e){checks.push({name,status:'failed',error:e.message})}}
check('empty cues gives next action and disables editing',()=>{const f=fixture({cues:[]});assert.equal(f.error,null);assert.equal(f.els.controls.disabled,true);assert.match(f.els.state.textContent,/没有可编辑动效/)});
check('empty words gives alignment recovery',()=>{const f=fixture({words:[]});assert.equal(f.error,null);assert.equal(f.els.controls.disabled,true);assert.match(f.els.state.textContent,/词锚点/)});
check('empty offset rejected rather than coerced to zero',()=>{const f=fixture();f.input('offset','');f.preview();assert.equal(f.els.download.disabled,true);assert.equal(f.doc.activeElement.id,'offset');assert.match(f.els.error.textContent,/填写偏移/)});
check('empty duration keeps inputs and focuses invalid field',()=>{const f=fixture();f.input('offset','0.4');f.input('duration','');f.preview();assert.equal(f.els.offset.value,'0.4');assert.equal(f.doc.activeElement.id,'duration');assert.equal(f.els.download.disabled,true)});
check('range error preserves input then valid retry succeeds',()=>{const f=fixture();f.input('duration','999');f.preview();assert.match(f.els.error.textContent,/本片段/);assert.equal(f.els.duration.value,'999');f.input('duration','4');f.preview();assert.equal(f.els.download.disabled,false);assert.equal(f.els.error.textContent,'')});
check('changed values clear stale preview and export readiness',()=>{const f=fixture();f.preview();assert.equal(f.els.download.disabled,false);f.input('offset','1');assert.equal(f.els.download.disabled,true);assert.equal(f.els.after.style.width,'0%');assert.equal(f.els.before.style.width,'0%')});
check('same-source reload restores raw inputs but requires preview',()=>{const f=fixture();f.input('offset','0.6');const next=fixture({},f.storage);assert.equal(next.els.offset.value,'0.6');assert.equal(next.els.download.disabled,true);assert.match(next.els.state.textContent,/恢复/)});
check('different source never restores old draft',()=>{const f=fixture();f.input('offset','0.6');const next=fixture({source_spec:{sha256:'b'.repeat(64)}},f.storage);assert.equal(next.els.offset.value,'0')});
check('storage unavailable does not lose active inputs',()=>{const f=fixture({},new Map(),true);f.input('offset','0.2');f.preview();assert.equal(f.els.offset.value,'0.2');assert.equal(f.els.download.disabled,false);assert.match(f.els.state.textContent,/未允许暂存/)});
check('corrupt storage is contained and recoverable',()=>{const store=new Map([['author-edit:'+'a'.repeat(64)+':0:20','{']]);const f=fixture({},store);assert.equal(f.error,null);assert.match(f.els.state.textContent,/损坏/);f.preview();assert.equal(f.els.download.disabled,false)});
check('duplicate export is suppressed until re-preview',()=>{const f=fixture();f.preview();f.els.download.click();f.els.download.click();assert.equal(f.downloads,1);assert.equal(f.els.download.disabled,true)});
check('failed download preserves edits and permits explicit retry',()=>{const f=fixture({},new Map(),false,true);f.input('offset','0.1');f.preview();f.els.download.click();assert.match(f.els.error.textContent,/下载/);assert.equal(f.els.offset.value,'0.1');f.preview();assert.equal(f.els.download.disabled,false)});
check('long Chinese text retained separately from native select',()=>{const f=fixture();assert.equal(f.els.wordText.textContent,f.long);assert.equal(f.els.word.options[0].textContent,`${f.long} (w1)`)});
check('span short-transition error never focuses hidden duration',()=>{const f=fixture();f.input('kind','span');f.input('endWord','w1');f.preview();assert.equal(f.doc.activeElement.id,'error');assert.equal(f.els.download.disabled,true)});
check('invalid source disables editing with plain recovery copy',()=>{const f=fixture({fps:0});assert.equal(f.error,null);assert.equal(f.els.controls.disabled,true);assert.match(f.els.state.textContent,/重新导出/)});
console.log(JSON.stringify({source,evidence_level:'isolated_javascript_with_simulated_DOM_and_storage',
  not_verified:['real_keyboard','browser_reload','layout_320px','text_zoom_200_percent','screen_reader','contrast_rendering'],checks},null,2));
process.exitCode=checks.every(x=>x.status==='passed')?0:1;
