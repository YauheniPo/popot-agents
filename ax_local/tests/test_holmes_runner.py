"""The native Holmes final round must explicitly disable tool generation."""

from pathlib import Path
import runpy
from types import ModuleType
import unittest
from unittest.mock import patch


class HolmesRunnerTests(unittest.TestCase):
    def test_native_final_round_sends_none_and_preserves_ordinary_tool_calls(self):
        class NativeLLM:
            def __init__(self, **kwargs):
                self.args = kwargs.get('args', {})

            def completion(self, messages, **kwargs):
                return {'messages':messages,'args':dict(self.args),**kwargs}

        modules = {}
        for name, exports in {
            'holmes.core.llm':{'DefaultLLM':NativeLLM},
            'holmes.core.tool_calling_llm':{'ToolCallingLLM':object},
            'holmes.core.tools_utils.tool_executor':{'ToolExecutor':object},
            'holmes.plugins.toolsets':{'load_toolsets_from_config':None},
        }.items():
            module=ModuleType(name);module.__dict__.update(exports);modules[name]=module
        with patch.dict('sys.modules',modules):
            namespace=runpy.run_path(str(Path(__file__).parents[1]/'holmes/runner.py'))
        model=namespace['FinishingLLM'](args={'max_tokens':8192}, final_report_prompt='Configured final report')
        normal=model.completion([{'role':'user','content':'Inspect'}],tools=[{'function':{}}],tool_choice='auto')
        self.assertNotIn('tool_choice',normal['args'])
        final=model.completion([{'role':'user','content':'Inspect'}],tools=None,tool_choice=None)
        self.assertEqual(final['args']['tool_choice'],'none')
        self.assertEqual(final['messages'][-1]['content'], 'Configured final report')
        self.assertNotIn('tool_choice',model.args)
