import io
import json
import os
import unittest
from unittest.mock import Mock, patch

from ax_local.worker.delegation import make_tools
from popot_agents.worker.http_harness import run_http


class WorkerDelegationTests(unittest.TestCase):
    def test_tool_explains_each_allowed_specialist_and_handoff(self):
        tools = make_tools({"AX_DELEGATION_URL": "http://api:8005",
                            "AX_DELEGATION_TOKEN": "private-token",
                            "AX_DELEGATION_ROLES": '["code_reviewer"]',
                            "AX_DELEGATION_DESCRIPTIONS": json.dumps({
                                "code_reviewer": "Review code for bugs and regressions.",
                                "forbidden": "Hidden specialist."})})
        schema = tools["delegate_task"]["schema"]["function"]
        text = json.dumps(schema)
        self.assertIn("Review code for bugs and regressions", text)
        self.assertNotIn("Hidden specialist", text)
        self.assertNotIn("private-token", text)
        self.assertIn("diff", text)
        self.assertEqual(schema["parameters"]["properties"]["role"]["enum"], ["code_reviewer"])

    def test_failed_task_preserves_child_stage_and_safe_reason(self):
        tools = make_tools({"AX_DELEGATION_URL": "http://api:8005",
                            "AX_DELEGATION_TOKEN": "test-capability",
                            "AX_DELEGATION_ROLES": '["backend_engineer"]'})
        failure = {"stage": "worker_message", "code": "mcp_discovery_failed",
                   "error_type": "AgentRunError"}
        task = {"id": "0123456789abcdef", "status": {"state": "TASK_STATE_FAILED",
                "message": {"parts": [{"text": "MCP server fetch failed during discovery"}]}},
                "metadata": {"failure": failure}}
        response = io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": 1,
                                         "result": {"task": task}}).encode())
        with patch("ax_local.worker.delegation.request.urlopen", return_value=response):
            result = json.loads(tools["delegate_task"]["call"](
                {"role": "backend_engineer", "task": "delegate onward"},
                timeout_seconds=5, call_id="call-1"))
        self.assertEqual(result["sessionId"], "0123456789abcdef")
        self.assertEqual(result["role"], "backend_engineer")
        self.assertEqual(result["error"], "MCP server fetch failed during discovery")
        self.assertEqual(result["failure"], failure)

    def test_only_ax_workers_with_allowed_roles_get_delegation_tool(self):
        self.assertEqual(make_tools({}), {})
        tools = make_tools({"AX_DELEGATION_URL": "http://api:8005",
                            "AX_DELEGATION_TOKEN": "test-capability",
                            "AX_DELEGATION_ROLES": '["qa_engineer"]'})
        schema = tools["delegate_task"]["schema"]["function"]["parameters"]
        self.assertEqual(schema["properties"]["role"]["enum"], ["qa_engineer"])
        self.assertEqual(schema["required"], ["role", "task"])

    def test_harness_passes_child_answer_back_to_parent_model(self):
        schemas = {"delegate_task": {"schema": {"type": "function", "function": {
            "name": "delegate_task", "parameters": {"type": "object"}}},
            "call": Mock(return_value='{"answer":"tested"}')}}
        answers = [
            {"content": None, "tool_calls": [{"id": "call-1", "type": "function",
                "function": {"name": "delegate_task",
                             "arguments": '{"role":"qa_engineer","task":"test"}'}}]},
            {"content": "final answer"},
        ]
        requests = []
        def urlopen(call, **_kwargs):
            requests.append(json.loads(call.data))
            return io.BytesIO(json.dumps({"choices": [{"message": answers.pop(0)}]}).encode())
        with patch.dict(os.environ, {"HARNESS_BASE_URL": "http://model/v1", "HARNESS_MODEL": "test"}), \
             patch("popot_agents.worker.http_harness.request.urlopen", side_effect=urlopen):
            result = run_http("do work", {}, extra_tools=schemas)
        self.assertEqual(result, "final answer")
        self.assertEqual(requests[1]["messages"][-1]["content"], '{"answer":"tested"}')
        self.assertRegex(schemas["delegate_task"]["call"].call_args.kwargs["call_id"],
                         r"^[0-9a-f]{32}:call-1$")


if __name__ == "__main__":
    unittest.main()
