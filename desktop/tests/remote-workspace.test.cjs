const test = require('node:test'), assert = require('node:assert/strict');
const {remoteWorkspaces, keyOf} = require('../electron/remote-workspace.cjs');
function fixture() {
 let saved={}, active;
 const sessions=new Map(), calls=[], disposed=[];
 const profile={id:'lab',name:'Lab',host:'my-host',cwd:'/remote/project',python:'python3',platform:'posix',port:null,ssh_config:''};
 let failPermission=false, due=[], disposeWork=async()=>{};
 const context={settings:()=>saved,saveSettings:value=>{saved=value;},sessions,active:()=>active,activate:value=>{active=value;},saveWorkspace:()=>{},admit:async()=>{},send:()=>{},broadcast:()=>({}),running:source=>source.busy,
  methods:new Set(['prompt','prompt.resume','files.read','git.status','config.select']),
  config:()=>({request:async()=>({connections:[profile]})}),
  dispose:async id=>{disposed.push(id);await disposeWork(id);sessions.delete(id);},
  transport:(_remote,onEvent)=>({closed:false,close:async()=>{},request:async(method,params)=>{
   calls.push({method,params});
   if(method==='remote.connection')return {profile,machine:{hostname:'remote-host',platform:'linux',cwd:profile.cwd}};
   if(method==='sessions.create')return {sessionId:'saved-1'};
   if(method==='initialize')return {project:profile.cwd,history:[],config:{model:'remote-model'},capabilities:{skills:[],agents:[]}};
   if(method==='permissions.set'&&failPermission)throw new Error('lost reply');
   if(method==='schedule.due')return due;
   return {started:true};
  }})};
 const work=remoteWorkspaces(context);
 return {work,context,sessions,calls,disposed,profile,saved:()=>saved,setSaved:value=>{saved=value;},setDispose:work=>{disposeWork=work;},active:()=>active,setFailure:()=>{failPermission=true;},setDue:value=>{due=value;}};
}
test('SSH navigation stores only references and routes prompts through its transport',async()=>{
 const f=fixture();const {value}=await f.work.handle('remote.open',{id:'lab'});
 assert(value.id.startsWith('ssh:'));assert.equal(value.remote.host,'my-host');assert.equal(f.active(),value.id);
 assert.equal(Object.keys(f.saved().remoteChats).length,1);assert.equal(f.work.projects()[0].path,'ssh:'+keyOf(f.profile));
 f.work.update(value.id,{draft:'keep this',draftEditId:''});assert.equal(f.work.find(value.id).draft,'keep this');
 await f.work.handle('prompt',{session:value.id,text:'real task'});
 assert(f.calls.some(row=>row.method==='prompt'&&row.params.text==='real task'));
 assert.equal(f.work.find(value.id).draft,'');
 assert(!JSON.stringify(f.saved()).includes('api_key'));
});

test('broken references and display groups cannot poison the catalog or expose raw fields',async()=>{
 const f=fixture();const {value}=await f.work.handle('remote.open',{id:'lab'});
 const good=f.saved().remoteChats[value.id], path=f.work.projects()[0].path;
 assert.throws(()=>f.work.update(value.id,{updated:{bad:true}}),/格式无效/);assert.deepEqual(f.saved().remoteChats[value.id],good);
 f.setSaved({...f.saved(),remoteChats:{...f.saved().remoteChats,broken:{api_key:'SECRET',title:[]}}});
 assert.equal(f.work.issues(),1);assert.equal(f.work.projects()[0].sessions.length,1);
 assert(!JSON.stringify(f.work.projects()).includes('draft'));assert(!JSON.stringify(f.work.projects()).includes('SECRET'));
 assert.equal(f.work.find(value.id).id,value.id);
 for(const [field,bad] of [['title',[]],['updated',{}],['permissionMode',null],['draftAttachments',['local-file']]]){
  f.setSaved({remoteChats:{[value.id]:{...good,[field]:bad}}});assert.equal(f.work.issues(),1,field);assert.equal(f.work.projects().length,0,field);
 }
 for(const bad of [null,false,[],"broken"]){
  f.setSaved({remoteChats:bad});assert.equal(f.work.issues(),1);assert.deepEqual(f.work.projects(),[]);
  const count=f.calls.length;await assert.rejects(f.work.handle('remote.open',{id:'lab'}),/未尝试连接/);assert.equal(f.calls.length,count);
 }
 f.setSaved({remoteChats:{[value.id]:good},remoteProjects:{[path]:{archived:'bad',name:[]}}});
 assert.equal(f.work.issues(),1);assert.equal(f.work.projects()[0].open,false);assert.equal(f.work.projects()[0].archived,true);
 const count=f.calls.length;await assert.rejects(f.work.handle('session.select',{id:value.id}),/未尝试连接/);assert.equal(f.calls.length,count);
});

test('forget is local-only, refuses running ownership and leaves remote history untouched',async()=>{
 const f=fixture();const {value}=await f.work.handle('remote.open',{id:'lab'}),source=f.sessions.get(value.id);
 const ref={id:'a'.repeat(64),version:'b'.repeat(64),field:'remoteChats',kind:'entry',chatId:value.id};
 const local=[];f.context.config=()=>({request:async(method,params)=>{
  local.push(method);if(method==='remote.references')return{references:[ref]};
  assert.equal(method,'remote.forget');assert.equal(params.confirm,true);const settings=f.saved();delete settings.remoteChats[value.id];return{removed:true};
 }});
 const count=f.calls.length;source.busy=true;
 await assert.rejects(f.work.handle('remote.forget',{...ref,confirm:true}),/请先停止/);assert.equal(f.disposed.length,0);
 source.busy=false;
 await assert.rejects(f.work.handle('remote.forget',{...ref,version:'c'.repeat(64),confirm:true}),/已更改/);
 const result=await f.work.handle('remote.forget',{...ref,confirm:true});assert(result.value.removed);assert.equal(result.value.activeId,'');
 assert.equal(f.calls.length,count);assert.deepEqual(f.disposed,[value.id]);assert.equal(source.moving,true);
 assert(!f.calls.some(call=>call.method==='storage.mutate'));
});

test('pending forget gates prompts and scheduler, and a failed close preserves references',async()=>{
 const f=fixture();const {value}=await f.work.handle('remote.open',{id:'lab'}),source=f.sessions.get(value.id);
 const ref={id:'a'.repeat(64),version:'b'.repeat(64),field:'remoteChats',kind:'container'};
 const local=[];f.context.config=()=>({request:async method=>{local.push(method);return{references:[ref]};}});
 let entered,release;const closing=new Promise(resolve=>{entered=resolve;}),gate=new Promise(resolve=>{release=resolve;});
 f.setDispose(async()=>{entered();await gate;throw new Error('close failed');});
 const operation=f.work.handle('remote.forget',{...ref,confirm:true});await closing;
 assert.equal(source.moving,true);
 const count=f.calls.length;await assert.rejects(f.work.handle('prompt',{session:value.id,text:'must not run'}),/正在移除/);
 await f.work.tickSchedules();assert.equal(f.calls.length,count);release();
 await assert.rejects(operation,/close failed/);assert.equal(source.moving,false);
 assert(f.work.find(value.id));assert(!local.includes('remote.forget'));
});
test('permission acknowledgement loss closes ownership without saving full access',async()=>{
 const f=fixture();const {value}=await f.work.handle('remote.open',{id:'lab'});f.setFailure();
 await assert.rejects(f.work.handle('permissions.set',{session:value.id,mode:'full'}),/未确认/);
 assert.deepEqual(f.disposed,[value.id]);assert.equal(f.work.find(value.id).permissionMode,'ask');
});
test('SSH file actions never fall through to a local path or mixed delete',async()=>{
 const f=fixture();const {value}=await f.work.handle('remote.open',{id:'lab'});
 for(const method of ['attachments.import','project.reveal','capabilities.directory'])
  await assert.rejects(f.work.handle(method,{session:value.id,path:'/remote/project'}),/未尝试打开本机/);
 assert.deepEqual(await f.work.handle('preview.open',{session:value.id,path:'/remote/project/page.html'}),{handled:false});
 await assert.rejects(f.work.handle('session.delete',{ids:[value.id,'local-row'],confirm:true}),/分别管理/);
 await assert.rejects(f.work.handle('session.move',{id:value.id,path:'C:/local'}),/没有移动/);
 assert.equal(f.disposed.length,0);
});

test('archive clears selection, active work prevents workspace close, due messages stay bound to remote identity',async()=>{
 const f=fixture();const {value}=await f.work.handle('remote.open',{id:'lab'});
 const group=f.work.projects()[0].path, source=f.sessions.get(value.id);
 source.busy=true;
 await assert.rejects(f.work.handle('project.close',{path:group}),/先停止/);
 assert.equal(f.disposed.length,0);source.busy=false;
 f.setDue([{id:'ours',session_id:'saved-1',cwd:f.profile.cwd},{id:'different-session',session_id:'elsewhere',cwd:f.profile.cwd},{id:'different-workspace',session_id:'saved-1',cwd:'/other'}]);
 await f.work.tickSchedules();
 assert.deepEqual(f.calls.filter(row=>row.method==='schedule.deliver').map(row=>row.params.id),['ours']);
 const result=await f.work.handle('session.update',{id:value.id,patch:{archived:true}});
 assert.equal(result.value.activeId,'');assert.equal(f.active(),undefined);
 await f.work.tickSchedules();
 assert.equal(f.calls.filter(row=>row.method==='schedule.deliver').length,1);
});
