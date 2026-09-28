import json
import unittest
from unittest.mock import MagicMock, patch

from mercury.common import canonical, digest
from mercury.models import HTTPJSONAgent, OpenAIResponsesAgent, agent_from_config, strict_json


class ModelProtocolTests(unittest.TestCase):
    def setUp(self):
        self.observation={"observation_id":"a"*64,"as_of":"2026-09-24T02:00:00.000000+00:00"}
        self.decision={"research":{"version":"cn-research-v1","hypotheses":[]},
                       "strategy":{"version":"cn-strategy-v1","reason":"test draft"}}

    def invoke(self, payload, *, provider="openai"):
        raw=payload if isinstance(payload,bytes) else canonical(payload).encode()
        response=MagicMock();response.__enter__.return_value.read.return_value=raw
        opener=MagicMock();opener.open.return_value=response
        driver=OpenAIResponsesAgent(model="explicit-test-model",key_env="MERCURY_TEST_API_KEY") if provider=="openai" else HTTPJSONAgent(endpoint="https://example.test/model",model="explicit-test-model",key_env="MERCURY_TEST_API_KEY")
        with patch.dict("os.environ",{"MERCURY_TEST_API_KEY":"private-fixture-token"}), patch("mercury.models.urllib.request.build_opener",return_value=opener):
            result=driver.decide(self.observation,{"currency":"CNY"},{})
        return result,opener

    def wrapper(self, text=None, **changes):
        return {"status":"completed","error":None,"incomplete_details":None,"output":[{"type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":text or canonical(self.decision)}]}],**changes}

    def test_openai_protocol_no_tools_and_local_derived_ids(self):
        result,opener=self.invoke(self.wrapper())
        request=opener.open.call_args.args[0];body=json.loads(request.data)
        self.assertEqual(request.full_url,"https://api.openai.com/v1/responses")
        self.assertEqual(body["model"],"explicit-test-model")
        self.assertEqual(body["tools"],[]);self.assertEqual(body["tool_choice"],"none")
        self.assertFalse(body["store"]);self.assertEqual(body["text"]["format"]["type"],"json_object")
        self.assertNotIn("private-fixture-token",request.data.decode())
        self.assertEqual(result["research"]["observation_id"],self.observation["observation_id"])
        research=dict(result["research"]);identifier=research.pop("research_id")
        self.assertEqual(identifier,digest(research))
        self.assertEqual(result["strategy"]["research_id"],identifier)

    def test_incomplete_failed_refusal_and_tool_output_rejected(self):
        cases=[self.wrapper(status="incomplete"),self.wrapper(error={"message":"private-fixture-token"}),
               self.wrapper(output=[{"type":"function_call","name":"shell"}]),
               self.wrapper(output=[{"type":"message","role":"assistant","status":"completed","content":[{"type":"refusal","refusal":"no"}]}])]
        for value in cases:
            with self.subTest(value=value),self.assertRaises(ValueError) as error:self.invoke(value)
            self.assertNotIn("private-fixture-token",str(error.exception))

    def test_duplicate_nonfinite_and_prose_rejected(self):
        for value in ['{"x":1,"x":2}','{"x":NaN}','```json {} ```']:
            with self.assertRaises(ValueError):strict_json(value)
        with self.assertRaises(ValueError):self.invoke(self.wrapper('{"research":{},"research":{},"strategy":{}}'))

    def test_success_credential_echo_never_returned(self):
        for provider,payload in [("openai",self.wrapper(canonical({"research":{},"strategy":{"reason":"private-fixture-token"}}))),
                                 ("generic",{"research":{},"strategy":{"reason":"private-fixture-token"}})]:
            with self.assertRaisesRegex(ValueError,"credential") as error:self.invoke(payload,provider=provider)
            self.assertNotIn("private-fixture-token",str(error.exception))

    def test_other_observation_binding_is_rejected(self):
        value={"research":{"observation_id":"b"*64},"strategy":{}}
        with self.assertRaisesRegex(ValueError,"binding"):self.invoke(self.wrapper(canonical(value)))

    def test_configuration_model_required_and_no_url_credentials(self):
        with self.assertRaises((TypeError,ValueError)):agent_from_config({"provider":"openai-responses"})
        with self.assertRaises(ValueError):HTTPJSONAgent(endpoint="https://user:secret@example.test",model="m",key_env="KEY")
        with self.assertRaises(ValueError):OpenAIResponsesAgent(model="")
        self.assertNotEqual(OpenAIResponsesAgent(model='m',max_output_tokens=1024).identity,
                            OpenAIResponsesAgent(model='m',max_output_tokens=2048).identity)

    def test_http_failure_body_not_logged_and_no_retries(self):
        import urllib.error
        opener=MagicMock();opener.open.side_effect=urllib.error.URLError("private-fixture-token")
        with patch.dict("os.environ",{"MERCURY_TEST_API_KEY":"private-fixture-token"}),patch("mercury.models.urllib.request.build_opener",return_value=opener):
            with self.assertRaises(ValueError) as error:OpenAIResponsesAgent(model="m",key_env="MERCURY_TEST_API_KEY").decide(self.observation,{}, {})
        self.assertEqual(opener.open.call_count,1);self.assertNotIn("private-fixture-token",str(error.exception))
