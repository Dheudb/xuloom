"""One native Ultra assessment; work uses existing tools, receipts and worker lifecycle."""
import hashlib
import json
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from xuloom.tools.base import BaseAgenticTool
from xuloom.agents.ultra_decision import Assignment
from xuloom.core.run_result import PersistenceError

NAMES = frozenset({'ultra_answer', 'ultra_prepare'})


def required(engine):
    return (getattr(engine,'collaboration_mode','auto') == 'ultra' and getattr(engine,'agent_depth',0) == 0
        and NAMES <= getattr(engine,'available_tools',{}).keys()
        and not engine.tool_context.get('external_backend') and not engine.tool_context.get('review_scope')
        and not engine.tool_context.get('collaboration_run', {}).get('decision')
        and not engine.tool_context.get('collaboration_run', {}).get('assessment'))


class AnswerInput(BaseModel):
    model_config = {'extra':'forbid'}
    answer: str = Field(min_length=1, description='Final answer for a genuinely simple, self-contained task only.')


class PrepareInput(BaseModel):
    model_config = {'extra':'forbid'}
    task_type: Literal['research', 'multi_path', 'sequential'] = Field(description='research: evidence-based investigation; multi_path: distinct approaches; sequential: dependent operations only.')
    main_task: str = Field(min_length=1, description='Work the main agent will perform while workers run.')
    evidence: Literal['search', 'provided', 'not_needed']
    search_queries: list[str] = Field(default_factory=list, description='External evidence queries; required when evidence=search.')
    workers: list[Assignment] = Field(default_factory=list, description='Distinct independent assignments; multi_path requires useful workers, never a quota.')
    rationale: str = Field(min_length=1, description="Brief user-facing explanation of the approach in the user's language; explain dependencies if sequential.")

    @model_validator(mode='after')
    def usable(self):
        if not self.main_task.strip() or not self.rationale.strip():
            raise ValueError('State meaningful main work and rationale')
        self.search_queries = list(dict.fromkeys(q.strip() for q in self.search_queries if q.strip()))
        if self.evidence == 'search' and not self.search_queries:
            raise ValueError('External research requires at least one concrete search query')
        if self.task_type in {'research','multi_path'} and self.evidence == 'not_needed':
            raise ValueError('Substantive investigation needs external search or provided evidence; familiarity alone is not evidence')
        if self.task_type == 'multi_path' and not self.workers:
            raise ValueError('Multi-path work requires independent worker assignments; use sequential only for dependent work')
        if any(not w.prompt.strip() or not w.description.strip() for w in self.workers):
            raise ValueError('Worker assignments must be meaningful')
        return self


class UltraAnswerTool(BaseAgenticTool):
    name = 'ultra_answer'
    description = 'Answer a simple greeting, bounded fact or small self-contained task. Research, comparisons and multiple substantive ideas require ultra_prepare.'
    input_schema = AnswerInput
    is_read_only = True
    async def execute(self, **kwargs):
        raise RuntimeError('Ultra assessment must use the owning engine')


class UltraPrepareTool(BaseAgenticTool):
    name = 'ultra_prepare'
    description = 'Prepare complex work: request needed external evidence and distinct parallel assignments. Use multi_path for multiple approaches; sequential only for dependencies.'
    input_schema = PrepareInput
    is_read_only = True
    def get_tool_definition(self):
        from xuloom.agents.agent_tool import AgentTool
        agent=AgentTool();agent.context=self.context
        definition=super().get_tool_definition()
        definition['input_schema']['$defs']['Assignment']['properties']['subagent_type']=agent.get_tool_definition()['input_schema']['properties']['subagent_type']
        return definition
    def validate_input(self, workers=None, **kwargs):
        from xuloom.agents.agent_tool import AgentTool
        agent=AgentTool();agent.context=self.context
        for worker in workers or []:
            error=agent.validate_input(subagent_type=worker.get('subagent_type'))
            if error:return error
    async def execute(self, **kwargs):
        raise RuntimeError('Ultra assessment must use the owning engine')


async def execute(engine, tool_name, values, *, turn, event_callback, sys_print_callback):
    run = engine.tool_context['collaboration_run']
    if run.get('assessment'):
        return 'Assessment already recorded; inspect existing tool/worker receipts. No operations were replayed.'
    if not required(engine):
        raise ValueError('Ultra assessment is not active for this task')
    if tool_name == 'ultra_answer':
        answer = AnswerInput.model_validate(values).answer.strip()
        if not answer or engine.tool_context['workflow_completion'].pending or engine.tool_context['subagent_tasks'].tasks:
            raise ValueError('Finish the active workflow/evidence collection instead of using a simple-task answer')
        run['assessment'] = {'kind':'direct', 'answer':answer}
        if not engine.persist_session_state():
            run.pop('assessment', None)
            raise PersistenceError('Ultra assessment could not be saved')
        return 'Simple-task answer recorded.'
    plan = PrepareInput.model_validate(values)
    from xuloom.core.tool_execution import tool_identity
    owner, digest = tool_identity(engine, tool_name, values)
    prefix = 'ultra-' + hashlib.sha256((owner+digest).encode()).hexdigest()[:20]
    searches = [{'type':'tool_use', 'id':f'{prefix}-search-{i}', 'name':'web_search_tool',
        'input':{'query':q,'max_results':8}} for i,q in enumerate(plan.search_queries)]
    calls = []
    seen = set()
    for worker in plan.workers:
        key = (worker.prompt.strip(),worker.description.strip(),worker.subagent_type or 'general-purpose')
        if key in seen: continue
        seen.add(key)
        identity = f'{prefix}-worker-{len(seen)}'
        calls.append({'type':'tool_use','id':identity,'name':'spawn_agent',
            'input':{**worker.model_dump(exclude_none=True),'request_id':identity}})
    calls.extend(searches)  # Start independent workers before the main evidence lookup.
    # Persist admission before actions. Repeated/uncertain assessment calls never
    # launch a second set; ordinary worker records remain the execution authority.
    run['assessment'] = {'kind':'work', **plan.model_dump(), 'calls':[c['id'] for c in calls]}
    if not engine.persist_session_state():
        run.pop('assessment',None)
        raise PersistenceError('Ultra work plan could not be saved; no work was started')
    # The model already supplied this public explanation. Reuse the ordinary
    # commentary channel, without another model request or a synthetic answer.
    await engine._emit_stream_event(event_callback, {'type':'text_delta', 'turn':turn,
        'channel':'commentary', 'source':'ultra_prepare', 'text':plan.rationale.strip() + '\n\n'})
    engine.activate_tools(['web_search_tool','spawn_agent'])
    results = await engine._execute_tool_batches(calls,sys_print_callback,turn=turn,event_callback=event_callback) if calls else []
    return json.dumps({'main_task':plan.main_task,'results':results,
        'next':'Continue your main task, inspect failures, and collect all worker results before final synthesis.'},ensure_ascii=False)
