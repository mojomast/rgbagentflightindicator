"""The OTLP receiver: metadata in, lanes named, state left alone.

The fixture payloads under ``tests/fixtures/otlp`` are shaped like the
exporters this receiver claims to serve: Claude Code metrics and events,
Gemini CLI events, a Pydantic AI / OpenAI Agents span batch in camelCase, and
a snake_case GenAI metric batch. Every test is offline; no sockets are opened.
"""

from __future__ import annotations

import json
import os
import unittest

from rgi.otlp import InfoUpdate, OtlpReceiver, attr_value, attrs_to_dict

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "otlp")


def load(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def a(key: str, value) -> dict:
    """One OTLP attribute with the wrapper that fits the value."""
    if isinstance(value, bool):
        wrapped = {"boolValue": value}
    elif isinstance(value, int):
        wrapped = {"intValue": value}
    elif isinstance(value, float):
        wrapped = {"doubleValue": value}
    else:
        wrapped = {"stringValue": str(value)}
    return {"key": key, "value": wrapped}


def token_payload(resource_attrs, record_attrs=None, token_type="input",
                  **point) -> dict:
    """A minimal token-usage export, with overridable point fields."""
    payload = {"asInt": "7"}
    payload.update(point)
    attrs = []
    if token_type:
        attrs.append({"key": "gen_ai.token.type",
                      "value": {"stringValue": token_type}})
    attrs.extend(record_attrs or [])
    payload["attributes"] = attrs
    return {
        "resourceMetrics": [{
            "resource": {"attributes": resource_attrs},
            "scopeMetrics": [{"metrics": [{
                "name": "gen_ai.client.token.usage",
                "sum": {"dataPoints": [payload]},
            }]}],
        }]
    }


class ClaudeCodeMetricsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.receiver = OtlpReceiver(clock=lambda: 1_759_428_000.0)
        cls.updates = cls.receiver.handle(
            "/v1/metrics", load("claude_code_metrics.json"))
        cls.one = cls.updates[0]
        cls.fields = cls.one.fields

    def test_one_lane_per_session(self):
        self.assertEqual(len(self.updates), 1)
        self.assertIsInstance(self.one, InfoUpdate)
        self.assertEqual(self.one.session, "claude-code:ses_01J8CLAUDE")

    def test_input_and_output_token_types(self):
        self.assertEqual(self.fields["tokens"]["input"], 1200)
        self.assertEqual(self.fields["tokens"]["output"], 340)

    def test_cache_read_and_creation_are_kept(self):
        self.assertEqual(self.fields["tokens"]["cache_read"], 900)
        self.assertEqual(self.fields["tokens"]["cache_write"], 25)

    def test_cost_is_a_float(self):
        cost = self.fields["tokens"]["cost"]
        self.assertIsInstance(cost, float)
        self.assertAlmostEqual(cost, 0.0421, places=6)

    def test_model_is_reported(self):
        self.assertEqual(self.fields["model"], "claude-sonnet-4-5")

    def test_context_tokens_and_limit_become_a_percent(self):
        self.assertEqual(self.fields["context"],
                         {"used": 48000, "limit": 200000, "percent": 24.0})

    def test_operation_duration_is_ignored(self):
        self.assertNotIn("activity", self.fields)
        self.assertNotIn("duration", json.dumps(self.fields))

    def test_meta_names_the_signal_and_service(self):
        self.assertEqual(self.one.meta["signal"], "metrics")
        self.assertEqual(self.one.meta["service"], "claude-code")
        self.assertEqual(self.one.meta["records"], 6)
        self.assertEqual(self.one.meta["at"], 1759428000.0)


class ClaudeCodeLogsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.updates = OtlpReceiver().handle(
            "/v1/logs", load("claude_code_logs.json"))
        cls.one = cls.updates[0]
        cls.fields = cls.one.fields

    def test_one_lane_per_session(self):
        self.assertEqual([u.session for u in self.updates],
                         ["claude-code:ses_01J8CLAUDE"])

    def test_usage_attributes_on_logs(self):
        tokens = self.fields["tokens"]
        self.assertEqual(tokens["input"], 5000)
        self.assertEqual(tokens["output"], 120)
        self.assertEqual(tokens["cache_read"], 2000)
        self.assertEqual(tokens["cache_write"], 500)
        self.assertAlmostEqual(tokens["cost"], 0.031, places=6)

    def test_model_and_activity(self):
        self.assertEqual(self.fields["model"], "claude-sonnet-4-5")
        self.assertEqual(self.fields["activity"], "Bash")

    def test_prompts_and_tool_input_are_dropped(self):
        blob = json.dumps(self.fields)
        self.assertNotIn("deploy the thing", blob)
        self.assertNotIn("rm -rf", blob)
        self.assertNotIn("deleted the build", blob)
        self.assertNotIn("prompt", blob)
        self.assertNotIn("transcript", blob)

    def test_meta_event_is_the_last_vendor_event(self):
        self.assertEqual(self.one.meta["event"], "claude_code.tool_result")
        self.assertEqual(self.one.meta["signal"], "logs")

    def test_record_count_counts_folded_records(self):
        self.assertEqual(self.one.meta["records"], 3)


class GeminiCliLogsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.updates = OtlpReceiver().handle(
            "/v1/logs", load("gemini_cli_logs.json"))
        cls.one = cls.updates[0]
        cls.fields = cls.one.fields

    def test_resource_session_id_names_the_lane(self):
        self.assertEqual([u.session for u in self.updates],
                         ["gemini-cli:gemini-session-8f2"])

    def test_tokens_and_cost(self):
        tokens = self.fields["tokens"]
        self.assertEqual(tokens["input"], 800)
        self.assertEqual(tokens["output"], 50)
        self.assertAlmostEqual(tokens["cost"], 0.0125, places=6)

    def test_model(self):
        self.assertEqual(self.fields["model"], "gemini-2.5-pro")

    def test_context_percent_and_compactions(self):
        self.assertEqual(self.fields["context"],
                         {"percent": 73.5, "compactions": 2})

    def test_activity_is_the_latest_event(self):
        self.assertEqual(self.fields["activity"], "gemini_cli.agent.finish")

    def test_function_args_are_dropped_id_is_kept(self):
        blob = json.dumps(self.fields)
        self.assertNotIn("path", blob)
        self.assertNotIn("secret.txt", blob)
        # the id itself never reaches a field, but the key must survive the
        # privacy filter; check the helper directly.
        attrs = attrs_to_dict([
            a("function_args", "{}"), a("function_call_id", "call_7")])
        self.assertEqual(attrs, {"function_call_id": "call_7"})


class SpanBatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.updates = OtlpReceiver().handle(
            "/v1/traces", load("pydantic_ai_spans.json"))
        cls.one = cls.updates[0]
        cls.fields = cls.one.fields

    def test_service_instance_id_names_the_lane(self):
        self.assertEqual([u.session for u in self.updates],
                         ["pydantic-ai:run-7f3a91c2"])

    def test_span_usage_attributes_merge_last_value_wins(self):
        # token counters are cumulative, so the newest span's usage wins
        # rather than summing two calls into a number the session never had.
        tokens = self.fields["tokens"]
        self.assertEqual(tokens["input"], 150)
        self.assertEqual(tokens["output"], 30)
        self.assertEqual(tokens["cache_read"], 600)
        self.assertAlmostEqual(tokens["cost"], 0.0512, places=6)

    def test_model_from_request_and_response(self):
        self.assertEqual(self.fields["model"], "claude-sonnet-4-5")

    def test_activity_is_the_last_operation(self):
        self.assertEqual(self.fields["activity"], "chat")

    def test_span_content_and_tool_arguments_are_dropped(self):
        blob = json.dumps(self.fields)
        self.assertNotIn("haiku", blob)
        self.assertNotIn("secret input", blob)
        self.assertNotIn("lamps", blob)

    def test_meta_counts_the_folded_spans(self):
        self.assertEqual(self.one.meta["signal"], "traces")
        self.assertEqual(self.one.meta["records"], 3)


class SnakeCaseTest(unittest.TestCase):
    def test_snake_case_metrics_are_read(self):
        updates = OtlpReceiver().handle(
            "/v1/metrics", load("openai_agents_metrics_snake.json"))
        self.assertEqual([u.session for u in updates],
                         ["openai-agents:conv_9f2c"])
        tokens = updates[0].fields["tokens"]
        self.assertEqual(tokens["input"], 420)
        self.assertEqual(tokens["output"], 77)
        self.assertAlmostEqual(tokens["cost"], 0.0067, places=6)


class AttributionTest(unittest.TestCase):
    def receiver(self) -> OtlpReceiver:
        return OtlpReceiver(clock=lambda: 7200.0)

    def session_of(self, payload: dict) -> str:
        updates = self.receiver().handle("/v1/metrics", payload)
        self.assertEqual(len(updates), 1)
        return updates[0].session

    def test_resource_session_id_wins_over_record_session_id(self):
        payload = token_payload(
            [a("service.name", "acme"), a("session.id", "res-1")],
            [a("session.id", "rec-1")],
        )
        self.assertEqual(self.session_of(payload), "otlp:res-1")

    def test_key_precedence_beats_location(self):
        payload = token_payload(
            [a("service.name", "acme"), a("gen_ai.conversation.id", "conv-1")],
            [a("session.id", "rec-1")],
        )
        self.assertEqual(self.session_of(payload), "otlp:rec-1")

    def test_conversation_id_beats_rgi_session(self):
        payload = token_payload(
            [a("service.name", "acme"), a("rgi.session", "rgi-1"),
             a("gen_ai.conversation.id", "conv-2")],
        )
        self.assertEqual(self.session_of(payload), "otlp:conv-2")

    def test_rgi_session_beats_service_instance(self):
        payload = token_payload(
            [a("service.name", "acme"), a("service.instance.id", "inst-1"),
             a("rgi.session", "rgi-1")],
        )
        self.assertEqual(self.session_of(payload), "otlp:rgi-1")

    def test_service_instance_is_the_last_explicit_id(self):
        payload = token_payload(
            [a("service.name", "acme"), a("service.instance.id", "inst-1")],
        )
        self.assertEqual(self.session_of(payload), "otlp:inst-1")

    def test_a_known_harness_becomes_the_namespace(self):
        payload = token_payload(
            [a("service.name", "claude_code"), a("session.id", "s-1")],
        )
        self.assertEqual(self.session_of(payload), "claude-code:s-1")

    def test_unknown_service_keeps_the_otlp_namespace(self):
        payload = token_payload(
            [a("service.name", "my-custom-app"), a("session.id", "s-1")],
        )
        self.assertEqual(self.session_of(payload), "otlp:s-1")

    def test_numeric_session_ids_are_stringified(self):
        payload = token_payload([a("session.id", 4242)])
        self.assertEqual(self.session_of(payload), "otlp:4242")

    def test_session_ids_are_cleaned(self):
        payload = token_payload([a("session.id", "  ses one\n")])
        self.assertEqual(self.session_of(payload), "otlp:ses one")

    def test_trace_id_fallback(self):
        updates = self.receiver().handle(
            "/v1/logs", load("fallback_trace_logs.json"))
        self.assertEqual(updates[0].session,
                         "otlp:gemini-cli:4bf92f3577b3")
        self.assertEqual(updates[0].fields["tokens"]["input"], 10)

    def test_timestamp_fallback_uses_the_record(self):
        updates = OtlpReceiver(clock=lambda: 1.0).handle(
            "/v1/metrics", load("fallback_metrics.json"))
        self.assertEqual(updates[0].session,
                         "otlp:claude-code:488730")
        self.assertEqual(updates[0].fields["tokens"]["input"], 123)

    def test_clock_fallback_when_nothing_else_exists(self):
        payload = token_payload([a("service.name", "claude-code")])
        updates = OtlpReceiver(clock=lambda: 7200.0).handle(
            "/v1/metrics", payload)
        self.assertEqual(updates[0].session, "otlp:claude-code:2")

    def test_explicit_id_without_service_uses_otlp_namespace(self):
        payload = token_payload([a("session.id", "s-1")])
        self.assertEqual(self.session_of(payload), "otlp:s-1")


class PrivacyTest(unittest.TestCase):
    def test_content_keys_drop_unless_an_id_or_count(self):
        attrs = [
            a("prompt", "secret"),
            a("gen_ai.prompt.0.content", "secret"),
            a("tool_input", "secret"),
            a("gen_ai.tool.call.arguments", "secret"),
            a("tool_args", "secret"),
            a("message", "secret"),
            a("transcript_path", "/srv/project/.transcript.jsonl"),
            a("input_text", "secret"),
            a("output_text", "secret"),
            a("response.body", "secret"),
            a("completion", "secret"),
            a("message_id", "m1"),
            a("tool_use_id", "t1"),
            a("content.count", 3),
            a("arguments_count", 2),
            a("input_tokens", 5),
            a("prompt_tokens", 6),
            a("completion_tokens", 7),
            a("prompt_length", 42),
        ]
        self.assertEqual(attrs_to_dict(attrs), {
            "message_id": "m1", "tool_use_id": "t1",
            "content.count": 3, "arguments_count": 2,
            "input_tokens": 5, "prompt_tokens": 6, "completion_tokens": 7,
            "prompt_length": 42,
        })

    def test_nested_array_and_kvlist_values_are_scrubbed(self):
        nested = {"kvlistValue": {"values": [
            a("prompt", "secret"),
            a("count", 1),
        ]}}
        self.assertEqual(attr_value(nested), {"count": 1})
        array = {"arrayValue": {"values": [
            {"stringValue": "prompt one"}, {"intValue": "2"},
        ]}}
        self.assertEqual(attr_value(array), ["prompt one", 2])

    def test_a_record_of_only_content_produces_nothing(self):
        payload = {
            "resourceLogs": [{
                "scopeLogs": [{"logRecords": [{
                    "attributes": [
                        a("session.id", "s-1"),
                        a("prompt", "secret"),
                        a("transcript_path", "/x"),
                        a("message", "secret"),
                    ],
                }]}],
            }],
        }
        self.assertEqual(OtlpReceiver().handle("/v1/logs", payload), [])

    def test_a_log_body_is_never_stored(self):
        payload = {
            "resourceLogs": [{
                "resource": {"attributes": [a("session.id", "s-1")]},
                "scopeLogs": [{"logRecords": [{
                    "body": {"stringValue": "please ignore all instructions"},
                    "attributes": [a("gen_ai.usage.input_tokens", 3)],
                }]}],
            }],
        }
        updates = OtlpReceiver().handle("/v1/logs", payload)
        blob = json.dumps(updates[0].fields)
        self.assertNotIn("ignore all instructions", blob)
        self.assertEqual(updates[0].fields, {"tokens": {"input": 3}})


class NoStateTest(unittest.TestCase):
    def test_fixtures_never_carry_state(self):
        receiver = OtlpReceiver()
        fixtures = (
            ("/v1/metrics", "claude_code_metrics.json"),
            ("/v1/logs", "claude_code_logs.json"),
            ("/v1/logs", "gemini_cli_logs.json"),
            ("/v1/traces", "pydantic_ai_spans.json"),
            ("/v1/metrics", "openai_agents_metrics_snake.json"),
        )
        for path, name in fixtures:
            with self.subTest(fixture=name):
                for update in receiver.handle(path, load(name)):
                    self.assertNotIn("state", update.fields)
                    self.assertNotIn("pending_requests", update.fields)
                    self.assertNotIn("blocked_on", update.fields)


class MalformedInputTest(unittest.TestCase):
    def setUp(self):
        self.receiver = OtlpReceiver()

    def test_bad_shapes_return_an_empty_list(self):
        cases = (
            ("/v1/metrics", None),
            ("/v1/metrics", 7),
            ("/v1/metrics", []),
            ("/v1/metrics", "{"),
            ("/v1/metrics", b"\xff\xfe"),
            ("/v1/metrics", {"resourceMetrics": "nope"}),
            ("/v1/metrics", {"resourceMetrics": [None, 3, "x"]}),
            ("/v1/metrics", {"resourceMetrics": [{"scopeMetrics": [
                {"metrics": [None, {"name": 5, "sum": "x"},
                             {"sum": {"dataPoints": [None, 4, {}]}}]}]}]}),
            ("/v1/logs", {"resourceLogs": [{"resource": {"attributes": "no"},
                                            "scopeLogs": [{"logRecords": [
                                                {"attributes": "no", "body": 7},
                                                "x"]}]}]}),
            ("/v1/traces", {"resourceSpans": [{"scopeSpans": [{"spans": [
                {"traceId": 5, "attributes": [{"key": 5, "value": 3}]},
                "x"]}]}]}),
            ("/nope", {}),
            ("", {}),
            ("/v1/metrics?x=1", None),
            (None, {}),
        )
        for path, payload in cases:
            with self.subTest(path=path, payload=payload):
                try:
                    result = self.receiver.handle(path, payload)
                except Exception as exc:       # pragma: no cover - the failure
                    self.fail(f"handle raised {exc!r}")
                self.assertEqual(result, [])

    def test_a_json_string_is_accepted(self):
        text = json.dumps(load("gemini_cli_logs.json"))
        updates = self.receiver.handle("/v1/logs", text)
        self.assertEqual([u.session for u in updates],
                         ["gemini-cli:gemini-session-8f2"])

    def test_max_body_bounds_raw_text_only(self):
        small = OtlpReceiver(max_body=32)
        self.assertEqual(small.handle("/v1/logs", "x" * 64), [])
        # parsed dicts are bounded record-by-record, not by max_body
        updates = small.handle("/v1/logs", load("gemini_cli_logs.json"))
        self.assertEqual(len(updates), 1)

    def test_a_full_url_path_is_understood(self):
        updates = self.receiver.handle(
            "http://127.0.0.1:8730/v1/metrics?x=1",
            load("claude_code_metrics.json"))
        self.assertEqual(len(updates), 1)

    def test_attr_value_shapes(self):
        self.assertEqual(attr_value({"stringValue": "x"}), "x")
        self.assertEqual(attr_value({"string_value": "x"}), "x")
        self.assertEqual(attr_value({"intValue": "42"}), 42)
        self.assertEqual(attr_value({"int_value": 42}), 42)
        self.assertEqual(attr_value({"doubleValue": "1.5"}), 1.5)
        self.assertIs(attr_value({"boolValue": "true"}), True)
        self.assertIs(attr_value({"boolValue": False}), False)
        self.assertIsNone(attr_value({"boolValue": "maybe"}))
        self.assertIsNone(attr_value({"bytesValue": "aGk="}))
        self.assertIsNone(attr_value({"unknownValue": "x"}))
        self.assertIsNone(attr_value(None))
        self.assertEqual(attr_value("bare"), "bare")
        self.assertEqual(attr_value(7), 7)
        self.assertEqual(attr_value([1, 2]), [1, 2])

    def test_attrs_to_dict_tolerates_junk(self):
        self.assertEqual(attrs_to_dict(None), {})
        self.assertEqual(attrs_to_dict("x"), {})
        self.assertEqual(attrs_to_dict([None, 3, {"key": "", "value": {}}]), {})
        self.assertEqual(attrs_to_dict([{"Key": "k", "Value": {"intValue": "1"}}]),
                         {"k": 1})


class CapsTest(unittest.TestCase):
    def setUp(self):
        self.receiver = OtlpReceiver(clock=lambda: 7200.0)

    def test_attributes_are_capped_per_record(self):
        attrs = [a(f"k{i}", i) for i in range(300)]
        attrs[5] = a("gen_ai.usage.input_tokens", 5)
        attrs[250] = a("gen_ai.usage.output_tokens", 9)
        payload = token_payload([a("session.id", "s-1")], attrs,
                                token_type=None)
        updates = self.receiver.handle("/v1/metrics", payload)
        tokens = updates[0].fields["tokens"]
        self.assertEqual(tokens["input"], 5)
        self.assertNotIn("output", tokens)
        self.assertEqual(len(attrs_to_dict(attrs)), 200)

    def test_updates_are_capped(self):
        records = [
            {
                "body": {"stringValue": "gemini_cli.api_request"},
                "attributes": [a("session.id", f"s-{i}"),
                               a("gen_ai.request.model", f"m-{i}")],
            }
            for i in range(600)
        ]
        payload = {"resourceLogs": [{
            "resource": {"attributes": [a("service.name", "gemini-cli")]},
            "scopeLogs": [{"logRecords": records}],
        }]}
        updates = self.receiver.handle("/v1/logs", payload)
        self.assertEqual(len(updates), 500)
        self.assertEqual(updates[0].session, "gemini-cli:s-0")
        self.assertEqual(updates[-1].session, "gemini-cli:s-499")

    def test_records_are_capped(self):
        class Tiny(OtlpReceiver):
            MAX_RECORDS = 5

        records = [
            {
                "body": {"stringValue": "gemini_cli.api_request"},
                "attributes": [a("session.id", f"s-{i}")],
            }
            for i in range(20)
        ]
        payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": records}]}]}
        updates = Tiny(clock=lambda: 7200.0).handle("/v1/logs", payload)
        self.assertEqual(len(updates), 5)
        self.assertEqual([u.session for u in updates],
                         ["otlp:s-0", "otlp:s-1", "otlp:s-2",
                          "otlp:s-3", "otlp:s-4"])


if __name__ == "__main__":
    unittest.main()
