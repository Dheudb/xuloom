const test = require('node:test'), assert = require('node:assert/strict');
const {remoteWorkspaces, keyOf} = require('../electron/remote-workspace.cjs');
function fixture() {
 let saved={}, active;
 const sessions=new Map(), calls=[], disposed=[];
 const profile={id:'lab',name:'Lab',host:'my-host',cwd:'/remote/project',python:'python3',platform:'posix',port:null,ssh_config:''};
 let failPermission=false, due=[];
 const context={settings:()=>saved,saveSettings:value=>{saved=value;},sessions,active:()=>active,activate:value=>{active=value;},saveWorkspace:()=>{},admit:async()=>{},send:()=>{},broadcast:()=>({}),running:source=>source.busy,
  methods:new Set(['prompt','prompt.resume','files.read','git.status','config.select']),
  config:()=>({request:async()=>({connections:[profile]})}),
  dispose:async id=>{disposed.push(id);sessions.delete(id);},
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
 return {work,sessions,calls,disposed,profile,saved:()=>saved,active:()=>active,setFailure:()=>{failPermission=true;},setDue:value=>{due=value;}};
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
test('permission acknowledgement loss closes ownership without saving full access',async()=>{
 const f=fixture();const {value}=await f.work.handle('remote.open',{id:'lab'});f.setFailure();
 await assert.rejects(f.work.handle('permissions.set',{session:value.id,mode:'full'}),/未确认/);
 assert.deepEqual(f.disposed,[value.id]);assert.equal(f.work.find(value.id).permissionMode,'ask');
});
test('SSH file actions never fall through to a local path or mixed delete',async()=>{
 const f=fixture();const {value}=await f.work.handle('remote.open',{id:'lab'});
 for(const method of ['attachments.import','project.reveal','preview.open','capabilities.directory'])
  await assert.rejects(f.work.handle(method,{session:value.id,path:'/remote/project'}),/未尝试打开本机/);
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
