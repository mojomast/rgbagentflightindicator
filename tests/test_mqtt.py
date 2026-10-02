"""The MQTT publisher against a fake paho client - no broker, no network.

The fake records every publish, subscription, will and connect, so the tests can
assert the exact retained discovery contract Home Assistant consumes. The real
paho import is skipped whenever a factory is injected, which is also how the
suite proves the dependency stays optional: the missing-paho case patches the
module away and expects one clear RuntimeError.
"""

from __future__ import annotations

import json
import sys
import unittest
from unittest import mock

from rgi.events import EventHub, LaneEvent
from rgi.mqtt import MqttPublisher


class FakePublish:
    def __init__(self, rc: int = 0):
        self.rc = rc


class FakeMessage:
    def __init__(self, topic: str, payload):
        self.topic = topic
        self.payload = payload


class FakeClient:
    """The slice of ``paho.mqtt.client.Client`` this module touches."""

    def __init__(self):
        self.published: list[tuple] = []
        self.subscriptions: list[tuple] = []
        self.wills: list[tuple] = []
        self.connects: list[tuple] = []
        self.reconnect_delays: list[tuple] = []
        self.loop_started = 0
        self.loop_stopped = 0
        self.disconnected = 0
        self.auth = None
        self.tls = False
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.fail_publish = False
        self.fail_connect = False

    def username_pw_set(self, username, password=None):
        self.auth = (username, password)

    def tls_set(self, *args, **kwargs):
        self.tls = True

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.wills.append((topic, payload, qos, retain))

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        self.reconnect_delays.append((min_delay, max_delay))

    def connect(self, host, port, keepalive=60):
        self.connects.append((host, port, keepalive))
        if self.fail_connect:
            raise OSError("connection refused")

    def loop_start(self):
        self.loop_started += 1

    def loop_stop(self):
        self.loop_stopped += 1

    def disconnect(self):
        self.disconnected += 1

    def publish(self, topic, payload=None, qos=0, retain=False):
        if self.fail_publish:
            raise RuntimeError("no broker")
        self.published.append((topic, payload, qos, retain))
        return FakePublish()

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))

    def connect_now(self):
        """Simulate the broker accepting the connection (paho callback)."""
        assert self.on_connect is not None
        self.on_connect(self, None, {}, 0)


def snapshot_with(*lanes) -> dict:
    """``(session, slot, state, label)`` tuples -> a daemon status shape."""
    return {"sessions": {
        session: {"slot": slot, "state": state, "label": label,
                  "key": f"F{slot}", "agent": "opencode", "ident": "a",
                  "host": "h", "in_flight_s": 4.2, "info": {"prompt": "secret"}}
        for session, slot, state, label in lanes}}


class MqttTest(unittest.TestCase):
    def setUp(self):
        self.hub = EventHub()
        self.logs: list[tuple[str, str]] = []
        self.client = FakeClient()
        self.publisher: MqttPublisher | None = None

    def make(self, snapshot=None, **kwargs) -> MqttPublisher:
        if snapshot is None:
            snapshot = {"sessions": {}}
        if not callable(snapshot):
            fixed = snapshot
            snapshot = lambda: fixed
        options = {
            "host": "broker.test", "port": 1883,
            "client_factory": lambda: self.client,
            "log": lambda level, message: self.logs.append((level, message)),
        }
        options.update(kwargs)
        self.publisher = MqttPublisher(
            self.hub, snapshot, "test-machine", **options)
        self.addCleanup(self.publisher.stop)
        return self.publisher

    def by_topic(self) -> dict[str, tuple]:
        """The last publish per topic: (payload, qos, retain)."""
        out: dict[str, tuple] = {}
        for topic, payload, qos, retain in self.client.published:
            out[topic] = (payload, qos, retain)
        return out

    def aggregate(self) -> dict:
        assert self.publisher is not None
        return json.loads(self.by_topic()[self.publisher.state_topic][0])

    # -- discovery and availability ---------------------------------------
    def test_discovery_lwt_and_retained_availability(self):
        publisher = self.make(snapshot_with(("opencode:one", 0, "working", "Fix")))
        self.assertTrue(publisher.open())
        self.client.connect_now()
        published = self.by_topic()

        self.assertEqual(published[publisher.availability_topic],
                         ("online", 1, True))
        self.assertEqual(self.client.wills,
                         [(publisher.availability_topic, "offline", 1, True)])

        config = json.loads(
            published[publisher.discovery_aggregate][0])
        self.assertEqual(config["unique_id"], "rgi_test-machine_aggregate")
        self.assertEqual(config["state_topic"], "rgi/test-machine/state")
        self.assertEqual(config["json_attributes_topic"], "rgi/test-machine/state")
        self.assertEqual(config["availability_topic"],
                         "rgi/test-machine/availability")
        self.assertEqual(config["payload_available"], "online")
        self.assertEqual(config["payload_not_available"], "offline")
        self.assertEqual(published[publisher.discovery_aggregate][2], True)

        lane_topic = "homeassistant/sensor/test-machine/lane_0/config"
        lane_config = json.loads(published[lane_topic][0])
        self.assertEqual(lane_config["unique_id"], "rgi_test-machine_lane_0")
        self.assertEqual(lane_config["state_topic"], "rgi/test-machine/lane/0/state")
        self.assertEqual(lane_config["json_attributes_topic"],
                         "rgi/test-machine/lane/0/state")
        self.assertEqual(lane_config["availability_topic"],
                         "rgi/test-machine/availability")

        self.assertEqual(self.client.subscriptions, [
            ("rgi/test-machine/command/clear", 1),
            ("rgi/test-machine/command/ack", 1),
        ])

    def test_tls_and_auth_are_configured_before_connect(self):
        publisher = self.make(snapshot_with(), username="user", password="pass",
                              tls=True)
        self.assertTrue(publisher.open())
        self.assertTrue(self.client.tls)
        self.assertEqual(self.client.auth, ("user", "pass"))
        self.assertEqual(self.client.reconnect_delays, [(1, 60)])
        self.assertEqual(self.client.connects, [("broker.test", 1883, 60)])

    # -- state --------------------------------------------------------------
    def test_aggregate_takes_the_most_urgent_lane(self):
        publisher = self.make(snapshot_with(
            ("a", 0, "done", "d"),
            ("b", 1, "working", "w"),
            ("c", 2, "idle", "i")))
        publisher.open()
        self.client.connect_now()
        aggregate = self.aggregate()
        self.assertEqual(aggregate["state"], "working")
        self.assertEqual(aggregate["total"], 3)
        self.assertEqual(aggregate["counts"], {
            "blocked": 0, "error": 0, "working": 1,
            "done": 1, "idle": 1, "other": 0})

        self.hub.publish(LaneEvent("state", "c", 2, {
            "slot": 2, "state": "blocked", "label": "c", "host": "h"}))
        self.assertEqual(self.aggregate()["state"], "blocked")
        self.assertEqual(self.aggregate()["counts"]["blocked"], 1)

    def test_empty_panel_is_idle_but_a_lone_offline_lane_is_offline(self):
        publisher = self.make({"sessions": {}})
        publisher.open()
        self.client.connect_now()
        self.assertEqual(self.aggregate()["state"], "idle")
        self.hub.publish(LaneEvent("state", "ghost", 3, {
            "slot": 3, "state": "offline", "label": "ghost"}))
        self.assertEqual(self.aggregate()["state"], "offline")

    def test_lane_payload_is_bounded_and_retained(self):
        publisher = self.make(snapshot_with(("opencode:one", 0, "blocked", "Fix")))
        publisher.open()
        self.client.connect_now()
        published = self.by_topic()
        payload, qos, retain = published["rgi/test-machine/lane/0/state"]
        self.assertEqual(qos, 1)
        self.assertTrue(retain)
        body = json.loads(payload)
        self.assertEqual(body["state"], "blocked")
        self.assertEqual(body["slot"], 0)
        self.assertEqual(body["key"], "F0")
        self.assertEqual(body["label"], "Fix")
        self.assertEqual(body["host"], "h")
        self.assertEqual(body["ident"], "a")
        self.assertNotIn("info", body)
        self.assertNotIn("secret", payload)

    def test_end_publishes_a_retained_offline_lane(self):
        publisher = self.make(snapshot_with(("opencode:one", 0, "done", "Fix")))
        publisher.open()
        self.client.connect_now()
        self.hub.publish(LaneEvent("end", "opencode:one", 0, {
            "slot": 0, "state": "done", "label": "Fix"}))
        payload = json.loads(self.by_topic()["rgi/test-machine/lane/0/state"][0])
        self.assertEqual(payload["state"], "offline")
        self.assertEqual(self.aggregate()["state"], "idle")

    # -- snapshot and dedupe ----------------------------------------------
    def test_snapshot_republish_is_compared_before_publishing(self):
        publisher = self.make(snapshot_with(("opencode:one", 0, "working", "Fix")))
        publisher.open()
        self.client.connect_now()
        count = len(self.client.published)
        self.assertTrue(publisher.publish_snapshot())     # identical payloads
        self.assertEqual(len(self.client.published), count)

        self.client.published.clear()
        self.hub.publish(LaneEvent("state", "opencode:one", 0, {
            "slot": 0, "state": "blocked", "label": "Fix"}))
        self.assertEqual(len(self.client.published), 2)   # lane + aggregate only

    def test_reconnect_republishes_everything(self):
        publisher = self.make(snapshot_with(("opencode:one", 0, "working", "Fix")))
        publisher.open()
        self.client.connect_now()
        self.client.published.clear()
        self.client.connect_now()                          # a fresh connection
        topics = {topic for topic, _, _, _ in self.client.published}
        self.assertIn(publisher.availability_topic, topics)
        self.assertIn(publisher.discovery_aggregate, topics)
        self.assertIn("rgi/test-machine/lane/0/state", topics)

    def test_events_before_connect_are_picked_up_by_the_snapshot(self):
        snapshot = {"sessions": {}}
        publisher = self.make(snapshot)
        publisher.open()
        self.hub.publish(LaneEvent("state", "a", 0, {
            "slot": 0, "state": "blocked", "label": "late"}))
        self.assertEqual(self.client.published, [])        # not connected yet
        snapshot["sessions"]["a"] = {"slot": 0, "state": "blocked", "label": "late"}
        self.client.connect_now()
        self.assertEqual(self.aggregate()["state"], "blocked")

    # -- commands -----------------------------------------------------------
    def test_command_topics_dispatch_to_callbacks(self):
        cleared: list[str] = []
        acked: list[str] = []
        publisher = self.make(snapshot_with(), on_clear=cleared.append,
                              on_ack=acked.append)
        publisher.open()
        self.client.connect_now()
        self.client.on_message(self.client, None, FakeMessage(
            "rgi/test-machine/command/clear", b'{"session": "opencode:one"}'))
        self.client.on_message(self.client, None, FakeMessage(
            "rgi/test-machine/command/ack", b'{"session": "claude:two"}'))
        self.assertEqual(cleared, ["opencode:one"])
        self.assertEqual(acked, ["claude:two"])

    def test_bad_command_payloads_are_ignored(self):
        cleared: list[str] = []
        publisher = self.make(snapshot_with(), on_clear=cleared.append)
        publisher.open()
        self.client.connect_now()
        for payload in (b"{}", b'{"session": ""}', b"{not json"):
            self.client.on_message(self.client, None, FakeMessage(
                "rgi/test-machine/command/clear", payload))
        self.assertEqual(cleared, [])
        self.assertTrue(any("ignored" in message for _, message in self.logs))

    def test_missing_handler_is_logged_not_raised(self):
        publisher = self.make(snapshot_with())
        publisher.open()
        self.client.connect_now()
        self.client.on_message(self.client, None, FakeMessage(
            "rgi/test-machine/command/ack", b'{"session": "a"}'))
        self.assertTrue(any("no handler" in message for _, message in self.logs))

    # -- failure isolation --------------------------------------------------
    def test_missing_paho_raises_one_clear_runtime_error(self):
        publisher = MqttPublisher(self.hub, {"sessions": {}}, "test-machine",
                                  client_factory=None,
                                  log=lambda level, message: self.logs.append(
                                      (level, message)))
        self.addCleanup(publisher.stop)
        with mock.patch.dict(sys.modules, {
                "paho": None, "paho.mqtt": None, "paho.mqtt.client": None}):
            with self.assertRaises(RuntimeError) as raised:
                publisher.open()
        self.assertIn("paho-mqtt", str(raised.exception))
        # the hub subscription is harmless without a client
        self.hub.publish(LaneEvent("state", "a", 0, {"slot": 0, "state": "blocked"}))

    def test_refused_broker_and_broken_publish_are_swallowed(self):
        publisher = self.make(snapshot_with(("a", 0, "working", "w")))
        self.client.fail_connect = True
        self.assertFalse(publisher.open())                 # no exception
        self.assertTrue(any("did not answer" in message
                            for _, message in self.logs))
        self.client.fail_connect = False
        self.assertTrue(publisher.open())
        self.client.connect_now()

        self.client.fail_publish = True
        self.hub.publish(LaneEvent("state", "a", 0, {
            "slot": 0, "state": "blocked", "label": "w"}))
        self.assertTrue(any("failed" in message for _, message in self.logs))
        self.assertEqual(self.hub._subscribers, [publisher.handle])

    def test_stop_publishes_offline_and_disconnects(self):
        publisher = self.make(snapshot_with(("a", 0, "working", "w")))
        publisher.open()
        self.client.connect_now()
        publisher.stop()
        published = self.by_topic()
        self.assertEqual(published[publisher.availability_topic][0], "offline")
        self.assertEqual(self.client.disconnected, 1)
        self.assertEqual(self.client.loop_stopped, 1)


if __name__ == "__main__":
    unittest.main()
