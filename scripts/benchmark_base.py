"""Local baseline runner; fresh processes, isolated config, loopback model fixture only."""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time


def child(soak):
    import asyncio
    import gc
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    connect = socket.socket.connect
    def local_connect(sock, address):
        if address[0] not in ('127.0.0.1','::1','localhost'): raise OSError('Benchmark blocks external connections')
        return connect(sock, address)
    socket.socket.connect = local_connect
    start=time.perf_counter()
    from xuloom.core.engine import QueryEngine
    imported=time.perf_counter()
    def memory():
        if sys.platform=='win32':
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_=[('cb',wintypes.DWORD),('faults',wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in ('peak','working','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile')]
            kernel=ctypes.WinDLL('kernel32');kernel.GetCurrentProcess.restype=wintypes.HANDLE
            api=ctypes.WinDLL('psapi');api.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
            counters=Counters();counters.cb=ctypes.sizeof(counters)
            if not api.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):raise ctypes.WinError()
            return {'working_set_bytes':counters.working,'peak_working_set_bytes':counters.peak}
        import resource
        return {'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)}
    report={'engine_import_seconds':imported-start,'memory_after_import':memory()}
    async def workload():
        begin=time.perf_counter();engines=[QueryEngine() for _ in range(5)]
        report['five_empty_engines_seconds']=time.perf_counter()-begin
        report['memory_five_idle_engines']=memory()
        engine=engines[0]
        from xuloom.core.messages import create_user_message,create_assistant_message
        engine.messages=[m for _ in range(500) for m in (create_user_message('Source evidence '*300),create_assistant_message('Confirmed result '*200))]
        assert engine.persist_session_state()
        report['history_messages']=len(engine.messages)
        report['history_json_bytes']=len(json.dumps(engine.messages).encode())
        identity=engine.session_id;await engine.aclose()
        begin=time.perf_counter();restored=QueryEngine();assert restored.load_session(identity)
        report['history_load_seconds']=time.perf_counter()-begin
        begin=time.perf_counter()
        for _ in range(100):restored._get_anthropic_tools_schema()
        report['hundred_tool_schema_reads_seconds']=time.perf_counter()-begin
        begin=time.perf_counter()
        for _ in range(100):restored.session_manager.list_session_ids()
        report['hundred_session_lists_seconds']=time.perf_counter()-begin
        from xuloom.tools.base import BaseAgenticTool
        from pydantic import BaseModel
        class EmptyInput(BaseModel):pass
        async def echo(self,**kwargs):return 'virtual benchmark tool'
        virtual={f'fixture_tool_{i}':type('FixtureTool'+str(i),(BaseAgenticTool,),{'name':f'fixture_tool_{i}',
            'description':'Virtual benchmark evidence search','input_schema':EmptyInput,'execute':echo})() for i in range(1000)}
        restored.register_mcp_tools(virtual)
        search=restored.available_tools['tool_search_tool']
        begin=time.perf_counter()
        for _ in range(20):await search.execute(query='benchmark evidence')
        report['twenty_discoveries_1000_virtual_tools_seconds']=time.perf_counter()-begin
        restored.subagent_registry=[{'agent_id':f'fixture_{i}','status':'completed','task':'Virtual benchmark record'} for i in range(1000)]
        restored._sync_message_context()
        begin=time.perf_counter()
        for _ in range(100):restored.available_tools['agent_tool']._inventory()
        report['hundred_inventory_views_1000_virtual_workers_seconds']=time.perf_counter()-begin
        for other in engines[1:]:await other.aclose()
        await restored.aclose();gc.collect()
        report['memory_after_close']=memory()
        samples=[];deadline=time.monotonic()+soak
        while time.monotonic()<deadline:
            item=QueryEngine();await item.aclose();gc.collect();samples.append(memory());await asyncio.sleep(.25)
        report['short_soak_seconds']=soak;report['short_soak_memory']=samples
        report['imported_optional_sdks_before_model']={name:name in sys.modules for name in ('anthropic','openai','mcp','pymupdf')}
        bodies=[];arrivals=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                arrivals.append(time.perf_counter());bodies.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                for delta,finish in [({'content':'LOCAL_RESULT'},None),({},'stop')]:
                    self.wfile.write(('data: '+json.dumps({'choices':[{'index':0,'delta':delta,'finish_reason':finish}]})+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            begin=time.perf_counter()
            async with QueryEngine(model='fixture',model_provider='openai',api_key='fixture',api_base_url=f'http://127.0.0.1:{server.server_port}/v1') as main:
                result=await main.run('Hello',sys_print_callback=lambda _:None);assert result.ok,result
            report['configured_engine_to_first_request_seconds']=arrivals[0]-begin
            report['loopback_model_calls']=len(bodies)
            def normalize(value):
                if isinstance(value,str):return value.replace(str(Path.cwd()),'<workspace>').replace(Path.cwd().as_posix(),'<workspace>')
                if isinstance(value,list):return [normalize(x) for x in value]
                if isinstance(value,dict):return {k:normalize(v) for k,v in value.items()}
                return value
            report['normalized_model_input_characters']=len(json.dumps(normalize({'messages':bodies[0]['messages'],'tools':bodies[0].get('tools',[])}),ensure_ascii=False,sort_keys=True))
        finally:server.shutdown();server.server_close();thread.join(3)
    asyncio.run(workload());print(json.dumps(report,ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref',help='Compare an existing local git commit; no network fetch')
    parser.add_argument('--samples',type=int,default=3)
    parser.add_argument('--soak-seconds',type=float,default=0)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--child',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--child-ready',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.samples<1 or args.soak_seconds<0:parser.error('Invalid measurement range')
    if args.child:return child(args.soak_seconds)
    if args.child_ready:
        start=time.perf_counter();ready=[]
        from rich.console import Console
        def input_ready(self,*args,**kwargs):
            ready.append(time.perf_counter()-start)
            return '/quit'
        Console.input=input_ready
        from xuloom.main import main as cli
        sys.argv=['xuloom'];cli()
        assert len(ready)==1
        print(json.dumps({'ready_seconds':ready[0],'mode':'production non-TTY initialization; input intercepted, not a screen-paint measurement'}))
        return
    repo=Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='xuloom-baseline-') as folder:
        root=Path(folder);package=repo
        if args.ref:
            archive=subprocess.run(['git','archive',args.ref,'xuloom'],cwd=repo,capture_output=True,check=True)
            import io
            package=root/'source';package.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:tar.extractall(package,filter='data')
        result={'reference':args.ref or 'working-tree','python':sys.version,'platform':sys.platform,'samples':args.samples,'loopback_only':True}
        for kind,extra in [('help',['-m','xuloom','--help']),('exec_help',['-m','xuloom','exec','--help']),('interactive_ready',[str(Path(__file__).resolve()),'--child-ready']),('runtime',[str(Path(__file__).resolve()),'--child','--soak-seconds',str(args.soak_seconds)])]:
            measurements=[]
            for index in range(args.samples):
                work=root/(kind+str(index));work.mkdir()
                env={k:v for k,v in os.environ.items() if not k.startswith(('XULOOM_','OPENAI_','ANTHROPIC_'))}
                env.update(XULOOM_CONFIG_DIR=str(work/'config'),XULOOM_DATA_DIR=str(work/'data'),HOME=str(work/'home'),USERPROFILE=str(work/'home'),PYTHONPATH=str(package),PYTHONIOENCODING='utf-8')
                begin=time.perf_counter();process=subprocess.run([sys.executable,'-X','utf8',*extra],cwd=work,env=env,capture_output=True,text=True,encoding='utf-8',timeout=120)
                if process.returncode:raise RuntimeError(process.stderr[-3000:]+process.stdout[-1000:])
                measurements.append({'process_seconds':time.perf_counter()-begin,**(json.loads(process.stdout.splitlines()[-1]) if kind in ('runtime','interactive_ready') else {})})
            result[kind]={'median_process_seconds':statistics.median(x['process_seconds'] for x in measurements),'measurements':measurements}
        if args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
