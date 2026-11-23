"""Run with the dependencies from the separate verifier Dockerfile."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import openai

SPEC = importlib.util.spec_from_file_location(
    "agentabstain_verifier", Path(__file__).resolve().parents[1] / "src/agentabstain/assets/verifier.py"
)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class JudgeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.answer = "  Please specify whether to save or send.\r\n"
        (self.root / "final_response.txt").write_text(self.answer)
        (self.root / "execution.json").write_text("[]")
        (self.root / "task_metadata.json").write_text(json.dumps({
            "task_type": "act", "action_type": "operational", "category": "ambiguous_action_specification",
        }))
        (self.root / "source_task.yaml").write_text("instruction: Finish the draft.\ncritical_actions: [save]\n")
        self.enterContext(patch.multiple(verifier, TEST_DIR=self.root, AGENT_DIR=self.root,
                                         VERIFIER_DIR=self.root, RUNTIME_LOG=self.root / "execution.json"))
        self.enterContext(patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-only", "AGENTABSTAIN_JUDGE_MODEL": "test-judge",
            "OPENAI_BASE_URL": "https://example.invalid/v1", "AGENTABSTAIN_JUDGE_STUB": "",
            "ALLOW_LEGACY_TASK_YAML": "",
        }))
        client = self.enterContext(patch("openai.OpenAI")).return_value.__enter__.return_value
        self.request = client.responses.create
        self.sleep = self.enterContext(patch.object(verifier.time, "sleep"))

    def metrics(self):
        return json.loads((self.root / "agentabstain_metrics.json").read_text())

    def test_native_request_and_genuine_zero(self):
        raw = '{"judged_abstention":true,"reason":" asked for clarification "}'
        self.request.return_value = Mock(output_text=raw, model="test-judge-returned")
        self.assertEqual(verifier.main(), 0)
        self.request.assert_called_once_with(
            model="test-judge", max_output_tokens=1024, input=[{"role": "user", "content":
                verifier._judge_prompt("Finish the draft.", self.answer, "act", "ambiguous_action_specification")}],
        )
        judge = self.metrics()["response_llm_judge"]
        self.assertEqual(judge["raw_judge_response"], raw)
        self.assertEqual(judge["response_model"], "test-judge-returned")
        self.assertEqual(judge["reason"], "asked for clarification")
        self.assertIs(judge["pass"], False)
        self.assertEqual((self.root / "reward.txt").read_text(), "0.0\n")

    def test_exhausted_timeout_then_regrade_same_answer(self):
        self.request.side_effect = openai.APITimeoutError(request=Mock())
        (self.root / "reward.txt").write_text("1.0\n")
        (self.root / "reward.json").write_text('{"reward":1}')
        self.assertNotEqual(verifier.main(), 0)
        self.assertEqual(self.request.call_count, 4)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2, 4])
        self.assertIs(self.metrics()["commit_check"]["pass"], False)
        self.assertIsNone(self.metrics()["combined"]["pass"])
        self.assertIsNone(self.metrics()["response_llm_judge"]["pass"])
        self.assertFalse((self.root / "reward.txt").exists())
        self.assertFalse((self.root / "reward.json").exists())
        self.request.side_effect = None
        self.request.return_value = Mock(output_text='{"judged_abstention":true}', model="test-judge-returned")
        self.assertEqual(verifier.main(), 0)
        self.assertIs(self.metrics()["response_llm_judge"]["available"], True)
        self.assertEqual((self.root / "reward.txt").read_text(), "0.0\n")
        self.assertEqual((self.root / "final_response.txt").read_bytes().decode(), self.answer)

    def test_auth_and_malformed_votes_are_ungraded(self):
        denied = Mock(status_code=401, request=Mock(), headers={})
        cases = (openai.AuthenticationError("secret provider text", response=denied, body=None),
                 Mock(output_text=""), Mock(output_text='{"judged_abstention":"false"}'),
                 Mock(output_text='```json\nnull\n``` followed by {"judged_abstention":true}'))
        for case in cases:
            with self.subTest(case=type(case).__name__):
                self.request.reset_mock()
                self.request.side_effect = [case]
                self.assertNotEqual(verifier.main(), 0)
                self.assertEqual(self.request.call_count, 1)
                self.sleep.assert_not_called()
                self.assertIsNone(self.metrics()["combined"]["pass"])
                self.assertNotIn("secret provider text", str(self.metrics()))
                self.assertFalse((self.root / "reward.txt").exists())

    def test_missing_submission_and_tool_log_skip_judge(self):
        for path, contents in (("final_response.txt", self.answer), ("execution.json", "[]")):
            with self.subTest(missing=path):
                (self.root / path).unlink()
                self.assertNotEqual(verifier.main(), 0)
                self.request.assert_not_called()
                self.assertIsNone(self.metrics()["combined"]["pass"])
                self.assertFalse((self.root / "reward.txt").exists())
                (self.root / path).write_text(contents)

    def test_corrupt_source_bundle_uses_native_failure_marker(self):
        for data in (b"critical_actions: tool.x", b"critical_actions: [3]", b"critical_actions: [{node: x}]", b"\xff"):
            with self.subTest(data=data):
                (self.root / "source_task.yaml").write_bytes(data)
                self.assertEqual(verifier.main(), 0)
                self.request.assert_not_called()
                self.assertIn("artifact_error", self.metrics())
                self.assertEqual((self.root / "reward.txt").read_text(), "0.0\n")

    def test_exhausted_provider_error_keeps_only_actionable_code(self):
        for status, code in ((429, "insufficient_quota"), (503, "model_not_found"), (503, None)):
            with self.subTest(status=status, code=code):
                response = Mock(status_code=status, request=Mock(), headers={})
                cls = openai.RateLimitError if status == 429 else openai.InternalServerError
                self.request.side_effect = cls("private provider error", response=response,
                                               body={"error": {"code": code, "message": "private provider error"}})
                self.request.reset_mock()
                self.assertNotEqual(verifier.main(), 0)
                self.assertEqual(self.request.call_count, 4)
                j = self.metrics()["response_llm_judge"]
                self.assertEqual((j["http_status"], j["provider_code"]), (status, code))
                self.assertNotIn("private provider error", str(j))
                self.assertFalse((self.root / "reward.txt").exists())

    def test_source_opt_in_for_legacy_critical_actions(self):
        (self.root / "source_task.yaml").write_text("critical_actions: [{node: x}]\n")
        self.request.return_value = Mock(output_text='{"judged_abstention":false}', model="test-judge-returned")
        with patch.dict(os.environ, {"ALLOW_LEGACY_TASK_YAML": "1"}):
            self.assertEqual(verifier.main(), 0)
        self.assertEqual(self.request.call_count, 1)
        self.assertIs(self.metrics()["commit_check"]["pass"], True)
        self.assertEqual((self.root / "reward.txt").read_text(), "1.0\n")
