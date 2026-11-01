"""Owned model wire steps for screenshot discovery and actual returned pixels."""
import base64
import io
import re

from PIL import Image
if __package__:
    from .external_discovery_api import CatalogModel
else:
    from external_discovery_api import CatalogModel


class CaptureModel:
    def __init__(self, url):
        self.url, self.requests, self.captures = url, [], []
        self.reply = CatalogModel('openai').reply

    def __call__(self, handler, body):
        if not body.get('stream'): return False
        users = [(index, row) for index,row in enumerate(body['messages']) if row['role']=='user' and 'REMOTE_CAPTURE_' in str(row.get('content',''))]
        if not users: return False
        index,user = users[-1]
        mode=re.search(r'REMOTE_CAPTURE_([A-Z]+)',str(user['content'])).group(1)
        after=body['messages'][index+1:]
        self.requests.append(body)
        names={tool['function']['name'] for tool in body.get('tools',[])}
        identity='capture-'+mode.lower()
        tools=[row for row in after if row['role']=='tool']
        if 'browser_tool' not in names:
            return self.reply(handler,body,name='tool_search_tool',values={'query':'browser render screenshot','activate':['browser_tool']},identity='discover-'+mode.lower())
        if not any(row.get('tool_call_id')==identity for row in tools):
            values={'action':'render','width':800,'height':600,'wait_ms':10000 if mode=='WAIT' else 0,'full_page':False}
            if mode in {'URL','WAIT'}:values['url']=self.url+'/page'
            else:values['file_path']='missing.html' if mode=='MISSING' else 'page.html'
            return self.reply(handler,body,name='browser_tool',values=values,identity=identity)
        images=[part for row in after if isinstance(row.get('content'),list) for part in row['content'] if isinstance(part,dict) and part.get('type')=='image_url']
        if mode in {'FILE','URL'}:
            assert images,'The remote screenshot did not reach the configured vision input'
            data=base64.b64decode(images[0]['image_url']['url'].split(',',1)[1])
            with Image.open(io.BytesIO(data)) as image:
                assert image.size==(800,600),image.size
                assert max(abs(a-b) for a,b in zip(image.convert('RGB').getpixel((790,590)),(170,187,204)))<=5
        else:
            assert not images,'A denied, missing or text-only image was sent to the model'
        self.captures.append({'mode':mode,'images':len(images),'tool':tools[-1]})
        return self.reply(handler,body,text='REMOTE_CAPTURE_'+mode+'_VERIFIED',identity='final-'+mode.lower())
