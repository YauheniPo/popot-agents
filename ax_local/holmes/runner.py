"""Executed only in the pinned Holmes image, with explicitly scoped tools."""

import json
import os
from pathlib import Path
import sys
import tempfile

from holmes.core.llm import DefaultLLM
from holmes.core.tool_calling_llm import ToolCallingLLM
from holmes.core.tools_utils.tool_executor import ToolExecutor
from holmes.plugins.toolsets import load_toolsets_from_config


class FinishingLLM(DefaultLLM):
    def __init__(self, *, final_report_prompt, **kwargs):
        super().__init__(**kwargs)
        self.final_report_prompt = final_report_prompt

    def completion(self, messages, *, tools=None, **kwargs):
        # DefaultLLM 0.43.0 omits tool_choice when tools=None. Some routed
        # models keep emitting calls from history, exhausting Holmes's budget.
        if not tools:
            self.args['tool_choice'] = 'none'
            messages = [*messages, {'role':'system','content':
                self.final_report_prompt}]
        try:
            return super().completion(messages, tools=tools, **kwargs)
        finally:
            self.args.pop('tool_choice', None)


def main():
    settings = json.loads(Path('/etc/popot-holmes/settings.json').read_text())
    toolsets = load_toolsets_from_config(json.loads(Path('/etc/popot-holmes/tools.json').read_text()))
    for toolset in toolsets:
        toolset.check_prerequisites()
    executor = ToolExecutor(toolsets)
    if not executor.tools_by_name:
        raise RuntimeError('AX diagnosis tools could not be initialized')
    model = 'openai/' + os.environ['HOLMES_MODEL']
    llm = FinishingLLM(model=model, api_key=os.environ['OPENAI_API_KEY'],
                    api_base=os.environ['OPENAI_API_BASE'],
                    args=settings['llm_args'], final_report_prompt=settings['prompts']['final_report'])
    prompt = settings['prompts']['diagnosis']
    print(json.dumps({'model': model, 'tools': sorted(executor.tools_by_name)}), flush=True)
    with tempfile.TemporaryDirectory(prefix='holmes-ax-') as directory:
        agent = ToolCallingLLM(executor, max_steps=settings['max_steps'], llm=llm, tool_results_dir=Path(directory))
        result = agent.call([{'role': 'system', 'content': prompt},
                             {'role': 'user', 'content': sys.argv[1]}])
    if not result.result:
        raise RuntimeError('Holmes returned no diagnosis')
    print(result.result, flush=True)
    print(json.dumps({'llm_calls': result.num_llm_calls,
                      'tool_calls': len(result.tool_calls or [])}), flush=True)


if __name__ == '__main__':
    main()
