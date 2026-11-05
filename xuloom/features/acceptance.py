"""Small declarative artifact checks; no executable expressions or scientific judgment."""
import csv
import hashlib
import json
import re

FIELDS={'file_exists':set(),'file_contains':{'text'},'file_sha256':{'sha256'},
        'json_fields':{'fields'},'csv_columns':{'columns','min_rows'}}


def validate(check):
    if not isinstance(check,dict) or check.get('kind') not in FIELDS or not isinstance(check.get('path'),str) or not check['path']:
        raise ValueError('Choose a declared file, SHA256, JSON fields or CSV columns check')
    kind=check['kind']
    if set(check)-{'kind','path'}-FIELDS[kind]: raise ValueError('Unknown acceptance check field')
    if kind=='file_contains' and (not isinstance(check.get('text'),str) or not check['text']): raise ValueError('file_contains requires nonempty text')
    if kind=='file_sha256' and not re.fullmatch('[a-fA-F0-9]{64}',str(check.get('sha256',''))): raise ValueError('Supply a full SHA256')
    if kind=='json_fields':
        if not isinstance(check.get('fields'),dict) or not check['fields'] or not all(isinstance(k,str) for k in check['fields']): raise ValueError('json_fields requires a nonempty exact field mapping')
        json.dumps(check['fields'],allow_nan=False)
    if kind=='csv_columns':
        cols=check.get('columns')
        if not isinstance(cols,list) or not cols or not all(isinstance(c,str) and c for c in cols) or len(set(cols))!=len(cols): raise ValueError('Supply unique nonempty CSV columns')
        if type(check.get('min_rows',0)) is not int or not 0<=check.get('min_rows',0)<=10000000: raise ValueError('Invalid CSV minimum row count')


def evaluate(path,check):
    validate(check)
    if not path.is_file(): return False
    kind=check['kind']
    if kind=='file_exists': return True
    if kind=='file_sha256':
        digest=hashlib.sha256()
        with path.open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
        return digest.hexdigest()==check['sha256'].lower()
    # Bounded parsers, not unrestricted model-supplied code.
    if path.stat().st_size>10*1024*1024: return False
    try:
        if kind=='file_contains':return check['text'] in path.read_text(encoding='utf-8')
        if kind=='json_fields':
            data=json.loads(path.read_text(encoding='utf-8'),parse_constant=lambda s:(_ for _ in ()).throw(ValueError(s)))
            # Canonical JSON distinguishes nested booleans from numbers as well.
            canonical=lambda value:json.dumps(value,sort_keys=True,allow_nan=False,separators=(',',':'))
            return isinstance(data,dict) and all(k in data and canonical(data[k])==canonical(v) for k,v in check['fields'].items())
        with path.open(encoding='utf-8-sig',newline='') as f:
            rows=csv.reader(f,strict=True);header=next(rows,[])
            if len(set(header))!=len(header) or not set(check['columns'])<=set(header):return False
            count=0
            for row in rows:
                if len(row)!=len(header): return False
                count+=1
            return count>=check.get('min_rows',0)
    except (ValueError,UnicodeError,csv.Error): return False
