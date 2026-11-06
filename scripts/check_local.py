"""Run isolated local Python regressions and bind results to one source snapshot."""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import platform
import runpy
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]


def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT,text=True).strip()
def hashes():return runpy.run_path(str(ROOT/'desktop/scripts/stage-update.py'))['source_hashes']()


def worker(spec):
    sys.path.insert(0,str(ROOT/'tests'))
    suite=unittest.defaultTestLoader.loadTestsFromNames(spec['modules'])
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    report={'run':result.testsRun,'skipped':[{'test':t.id(),'reason':why} for t,why in result.skipped],
        'failures':[{'test':t.id(),'traceback':text} for t,text in result.failures],
        'errors':[{'test':t.id(),'traceback':text} for t,text in result.errors],
        'expectedFailures':[t.id() for t,_ in result.expectedFailures],
        'unexpectedSuccesses':[t.id() for t in result.unexpectedSuccesses], 'passed':result.wasSuccessful()}
    Path(spec['report']).write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    return 0 if result.wasSuccessful() else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs',type=int,default=3)
    parser.add_argument('--output',type=Path,default=ROOT/'desktop/build/local-validation.json')
    parser.add_argument('--allow-dirty',action='store_true',help='Development diagnosis only; cannot certify a clean release')
    parser.add_argument('--worker',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.worker:return worker(json.loads(args.worker.read_text()))
    if not 1<=args.jobs<=4:parser.error('Use 1 to 4 local test processes')
    revision=git('rev-parse','HEAD');clean=not git('status','--porcelain','--untracked-files=normal')
    if not clean and not args.allow_dirty:parser.error('Commit the candidate first, or use --allow-dirty for diagnosis')
    before=hashes();files=sorted((ROOT/'tests').glob('test_*.py'))
    groups=[[] for _ in range(args.jobs)];weights=[0]*args.jobs
    def weight(file):
        count=sum(isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith('test_') for node in ast.walk(ast.parse(file.read_text(encoding='utf-8-sig'))))
        return count*(8 if any(key in file.stem for key in ('scaling_ssh','scaling_hierarchy','noninteractive_cli','native_ssh','external_ssh')) else 1)
    for file in sorted(files,key=weight,reverse=True):
        index=min(range(args.jobs),key=weights.__getitem__);groups[index].append(file.stem);weights[index]+=weight(file)
    output=args.output.resolve();output.parent.mkdir(parents=True,exist_ok=True)
    start=time.monotonic();reports=[]
    with tempfile.TemporaryDirectory(prefix='xuloom-local-checks-') as folder:
        temporary=Path(folder)
        def run(index,modules):
            directory=temporary/str(index);directory.mkdir()
            report=output.with_name(output.stem+f'-part{index}.json');log=output.with_name(output.stem+f'-part{index}.log')
            spec=directory/'spec.json';spec.write_text(json.dumps({'modules':sorted(modules),'report':str(report)}))
            env={k:v for k,v in os.environ.items() if not k.startswith(('XULOOM_','OPENAI_','ANTHROPIC_'))}
            env.update(XULOOM_CONFIG_DIR=str(directory/'config'),XULOOM_DATA_DIR=str(directory/'data'),HOME=str(directory/'home'),USERPROFILE=str(directory/'home'),
                PYTHONPATH=str(ROOT),PYTHONIOENCODING='utf-8',XULOOM_NETWORK_MODE='direct')
            with log.open('w',encoding='utf-8') as stream:
                process=subprocess.run([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--worker',str(spec)],cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=2400)
            value=json.loads(report.read_text(encoding='utf-8')) if report.exists() else {'run':0,'skipped':[],'failures':[],'errors':[{'test':'worker','traceback':'Worker did not write a report'}],'passed':False}
            value.update(exitCode=process.returncode,log=log.name,modules=sorted(modules))
            return value
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures={pool.submit(run,i,g):i for i,g in enumerate(groups)}
            for future in as_completed(futures):
                value=future.result();reports.append(value)
                print(json.dumps({'part':futures[future],'run':value['run'],'passed':value['passed'],'seconds':round(time.monotonic()-start,2)}),flush=True)
    unchanged=before==hashes() and revision==git('rev-parse','HEAD')
    clean_final=not git('status','--porcelain','--untracked-files=normal')
    report={'passed':all(r['passed'] and r['exitCode']==0 for r in reports) and unchanged and (clean_final or args.allow_dirty),
        'sourceCommit':revision,'cleanSource':clean and clean_final,'sources':before,'scope':'Local Python protocol, lifecycle, cancellation, recovery and extension checks; deterministic and loopback fixtures',
        'realProvidersTested':False,'platform':platform.platform(),'pythonVersion':platform.python_version(),'elapsedSeconds':round(time.monotonic()-start,2),
        'summary':{'pythonTests':sum(r['run'] for r in reports),'skipped':sum(len(r['skipped']) for r in reports),
            'failures':sum(len(r['failures'])+len(r['errors']) for r in reports)},'parts':reports}
    output.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('sources','parts')}),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
