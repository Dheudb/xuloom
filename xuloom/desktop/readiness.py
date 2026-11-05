"""Local capability preflight. No downloads, inference, credential output or mutations."""
from importlib.util import find_spec
import shutil
import sys


FORMATS = (
    ('pdf','论文 / PDF','pymupdf','PyMuPDF','文字与页图读取；扫描页仍需视觉模型或独立 OCR'),
    ('docx','Word 文档','docx','python-docx','结构化编辑；版式需另行渲染核对'),
    ('xlsx','Excel 表格','openpyxl','openpyxl','单元格与公式保存；不等于 Excel 公式计算引擎'),
    ('pptx','PowerPoint 演示','pptx','python-pptx','结构化编辑；字体与版式需预览'),
    ('csv','CSV / JSON','csv',None,'标准库可读；大表可选 pandas、图表可选 matplotlib'),
)


def inspect(config, execution_mode='host'):
    formats=[]
    for key,label,module,package,note in FORMATS:
        ready=find_spec(module) is not None
        formats.append(dict(id=key,label=label,available=ready,note=note,
            install_argv=[sys.executable,'-m','pip','install',package] if package and not ready else []))
    renderer=shutil.which('soffice') or shutil.which('libreoffice')
    external=[dict(id=name,found=bool(shutil.which(name)),note='发现命令不等于版本、登录或供应商验收通过')
              for name in ('codex','claude','pi','dsh')]
    return dict(formats=formats,office_preview=bool(renderer),external_agents=external,
        model=dict(configured=bool(config.get('hasKey') and config.get('model')),provider=config.get('provider'),
            model=config.get('model'),verified_connection=False),
        isolation=execution_mode,network_tools_available=execution_mode!='workspace',
        checks=[{'name':'连接与模型','ready':bool(config.get('hasKey') and config.get('model'))},
                {'name':'办公格式预览工具','ready':bool(renderer)}],
        note='仅本地预检，未调用模型、联网、安装依赖或修改权限。已保存配置不等于真实连接验证。')
