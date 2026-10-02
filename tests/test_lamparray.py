"""HID LampArray reports and the thin backend - no hardware needed.

Every wire layout in this file is constructed byte by byte from the HUTRR84
reference descriptor order (little-endian), so the assertions pin the exact
bytes a device would see. The transport is faked: ``open()`` is exercised
against canned feature reports and a fake descriptor, which is as far as this
machine can go - there was no LampArray keyboard to plug in.
"""

from __future__ import annotations

import struct
import unittest
from unittest import mock

from rgi.backends.base import BackendUnavailable
from rgi.backends.lamparray import (
    ATTRIBUTES_REPORT_BYTES, DEFAULT_REPORT_IDS, LAMP_ATTRIBUTES_REPORT_BYTES,
    LamparrayBackend, LampArrayBackend,
    USAGE_ATTRIBUTES_REPORT, USAGE_ATTRIBUTES_REQUEST_REPORT,
    USAGE_ATTRIBUTES_RESPONSE_REPORT, USAGE_CONTROL_REPORT,
    USAGE_MULTI_UPDATE_REPORT, USAGE_RANGE_UPDATE_REPORT,
    build_attributes_request_report, build_control_report,
    build_multi_update_report, build_range_update_report,
    parse_attributes_report, parse_lamp_attributes_report, parse_report_ids,
)


def attributes_payload(count=3, kind=0x01, min_us=10000, width=100, height=200,
                       depth=300) -> bytes:
    return struct.pack("<HIIIII", count, width, height, depth, kind, min_us)


def lamp_payload(lamp_id=0, *, x=1000, y=2000, z=3000, latency_us=500,
                 purposes=0x02, levels=(255, 255, 255, 255),
                 programmable=1, binding=0) -> bytes:
    return (struct.pack("<HIIIII", lamp_id, x, y, z, latency_us, purposes)
            + bytes([*levels, programmable, binding]))


class FakeLampArray:
    """A feature-report channel with canned answers, shaped like HidTransport."""

    def __init__(self, bindings, *, count=None, kind=0x01, min_us=10000,
                 attributes=True, request_id=0x02, response_id=0x03,
                 attributes_id=0x01, descriptor=None):
        self.bindings = dict(bindings)
        self.count = count if count is not None else len(self.bindings)
        self.kind = kind
        self.min_us = min_us
        self.attributes = attributes
        self.request_id = request_id
        self.response_id = response_id
        self.attributes_id = attributes_id
        self.descriptor = descriptor
        self.sent: list[bytes] = []
        self.closed = 0
        self.requested_lamp = None
        self.fail_attributes = False
        self.short_lamp_report = False

    def get_feature(self, report_id, length=256):
        if self.fail_attributes:
            raise IOError("no device")
        if report_id == self.attributes_id:
            if not self.attributes:
                return b""
            return attributes_payload(self.count, self.kind, self.min_us)
        if report_id == self.response_id:
            if self.requested_lamp is None:
                return b""
            if self.short_lamp_report:
                return lamp_payload(self.requested_lamp)[:10]
            binding = self.bindings.get(self.requested_lamp, 0)
            return lamp_payload(self.requested_lamp, binding=binding)
        return b""

    def send_feature(self, report):
        report = bytes(report)
        self.sent.append(report)
        if report[0] == self.request_id and len(report) >= 3:
            self.requested_lamp = int.from_bytes(report[1:3], "little")

    def report_descriptor(self):
        return self.descriptor or b""

    def close(self):
        self.closed += 1


class FakeBackend(LamparrayBackend):
    """A backend whose device enumeration is a fixed fake path."""

    @classmethod
    def candidates(cls):
        return [{"path": b"fake-lamparray"}]


def make_backend(transport, **kwargs) -> FakeBackend:
    return FakeBackend(transport_factory=lambda path: transport, **kwargs)


def request_report(lamp_id, report_id=0x02) -> bytes:
    return bytes([report_id]) + struct.pack("<H", lamp_id)


def control_report(report_id=0x06, autonomous=0) -> bytes:
    return bytes([report_id, autonomous])


class TestParseAttributes(unittest.TestCase):
    def test_fields_are_little_endian(self):
        payload = struct.pack("<HIIIII", 0x0102, 0x03040506, 7, 8, 0x01, 10000)
        attrs = parse_attributes_report(payload)
        self.assertEqual(attrs["lamp_count"], 0x0102)
        self.assertEqual(attrs["bounding_box_width_um"], 0x03040506)
        self.assertEqual(attrs["bounding_box_height_um"], 7)
        self.assertEqual(attrs["bounding_box_depth_um"], 8)
        self.assertEqual(attrs["kind"], 0x01)
        self.assertEqual(attrs["kind_name"], "keyboard")
        self.assertEqual(attrs["min_update_interval_us"], 10000)
        self.assertEqual(attrs["min_update_interval_s"], 0.01)

    def test_unknown_kind_gets_a_readable_name(self):
        self.assertEqual(parse_attributes_report(attributes_payload(kind=0x42))["kind_name"],
                         "kind66")

    def test_zero_interval_is_zero_seconds(self):
        self.assertEqual(parse_attributes_report(attributes_payload(min_us=0))["min_update_interval_s"],
                         0.0)

    def test_wrong_length_is_refused(self):
        with self.assertRaises(ValueError):
            parse_attributes_report(attributes_payload()[:-1])
        with self.assertRaises(ValueError):
            parse_attributes_report(b"")
        self.assertEqual(len(attributes_payload()), ATTRIBUTES_REPORT_BYTES)


class TestParseLampAttributes(unittest.TestCase):
    def test_fields(self):
        attrs = parse_lamp_attributes_report(
            lamp_payload(5, x=1000, y=2000, z=3000, latency_us=500,
                         purposes=0x0A, levels=(3, 2, 1, 0), programmable=1,
                         binding=0x1E))
        self.assertEqual(attrs["lamp_id"], 5)
        self.assertEqual(attrs["position_um"], (1000, 2000, 3000))
        self.assertEqual(attrs["update_latency_us"], 500)
        self.assertEqual(attrs["purpose_names"], ("accent", "status"))
        self.assertEqual(attrs["level_counts"], (3, 2, 1, 0))
        self.assertTrue(attrs["is_programmable"])
        self.assertEqual(attrs["input_binding"], 0x1E)
        self.assertEqual(attrs["input_binding_usage_page"], 0x07)
        self.assertEqual(attrs["input_binding_usage_id"], 0x1E)

    def test_no_binding_has_no_usage(self):
        attrs = parse_lamp_attributes_report(lamp_payload(binding=0))
        self.assertIsNone(attrs["input_binding_usage_page"])
        self.assertIsNone(attrs["input_binding_usage_id"])

    def test_wrong_length_is_refused(self):
        with self.assertRaises(ValueError):
            parse_lamp_attributes_report(lamp_payload()[:-1])
        self.assertEqual(len(lamp_payload()), LAMP_ATTRIBUTES_REPORT_BYTES)


class TestBuildReports(unittest.TestCase):
    def test_attributes_request(self):
        self.assertEqual(build_attributes_request_report(0x1234, report_id=0x02),
                         bytes([0x02, 0x34, 0x12]))

    def test_multi_update_exact_bytes(self):
        entries = [(0, (255, 0, 0)), (2, (0, 255, 0))]
        report = build_multi_update_report(entries, report_id=0x04,
                                           update_complete=True)
        expected = (
            bytes([0x04, 2, 0x01])
            + struct.pack("<8H", 0, 2, 0, 0, 0, 0, 0, 0)
            + bytes([255, 0, 0, 255, 0, 255, 0, 255])
            + bytes(24)
        )
        self.assertEqual(report, expected)
        self.assertEqual(len(report), 51)

    def test_multi_update_without_complete_flag(self):
        report = build_multi_update_report([(7, (1, 2, 3))], report_id=0x04)
        self.assertEqual(report[2], 0)
        self.assertEqual(report[3:19], struct.pack("<8H", 7, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(report[19:23], bytes([1, 2, 3, 255]))

    def test_multi_update_limits(self):
        with self.assertRaises(ValueError):
            build_multi_update_report([], report_id=0x04)
        with self.assertRaises(ValueError):
            build_multi_update_report([(i, (0, 0, 0)) for i in range(9)],
                                      report_id=0x04)

    def test_channels_are_clamped(self):
        report = build_multi_update_report([(0, (-5, 300, None))], report_id=0x04)
        self.assertEqual(report[19:23], bytes([0, 255, 0, 255]))

    def test_range_update_exact_bytes(self):
        report = build_range_update_report(2, 5, (10, 20, 30), report_id=0x05,
                                           update_complete=True)
        self.assertEqual(report, bytes([0x05, 0x01]) + struct.pack("<HH", 2, 5)
                         + bytes([10, 20, 30, 255]))
        self.assertEqual(len(report), 10)

    def test_range_update_order_is_checked(self):
        with self.assertRaises(ValueError):
            build_range_update_report(5, 2, (0, 0, 0), report_id=0x05)
        with self.assertRaises(ValueError):
            build_range_update_report(-1, 2, (0, 0, 0), report_id=0x05)

    def test_control_report(self):
        self.assertEqual(build_control_report(0x06, False), bytes([0x06, 0x00]))
        self.assertEqual(build_control_report(0x06, True), bytes([0x06, 0x01]))


def descriptor_for(usage_ids: dict[int, int], other_page_usage=0x50) -> bytes:
    """A minimal report descriptor: each usage gets its own report id."""
    data = bytearray()
    data += bytes([0x05, 0x59, 0x09, 0x01, 0xA1, 0x01])   # LampArray app
    for usage, report_id in usage_ids.items():
        data += bytes([0x85, report_id])                  # Report ID
        data += bytes([0x09, usage, 0xA1, 0x02, 0xC0])    # Usage + Logical
    # a decoy on another page must never be mapped
    data += bytes([0x06, 0x00, 0xFF])                     # Usage Page 0xFF00
    data += bytes([0x85, 0x66, 0x09, other_page_usage, 0xA1, 0x02, 0xC0])
    data += bytes([0xC0])                                 # end application
    return bytes(data)


class TestParseReportIds(unittest.TestCase):
    def test_usages_map_to_report_ids(self):
        ids = parse_report_ids(descriptor_for({
            USAGE_ATTRIBUTES_REPORT: 0x11,
            USAGE_ATTRIBUTES_REQUEST_REPORT: 0x12,
            USAGE_ATTRIBUTES_RESPONSE_REPORT: 0x13,
            USAGE_MULTI_UPDATE_REPORT: 0x14,
            USAGE_RANGE_UPDATE_REPORT: 0x15,
            USAGE_CONTROL_REPORT: 0x16,
        }))
        self.assertEqual(ids, {
            USAGE_ATTRIBUTES_REPORT: 0x11,
            USAGE_ATTRIBUTES_REQUEST_REPORT: 0x12,
            USAGE_ATTRIBUTES_RESPONSE_REPORT: 0x13,
            USAGE_MULTI_UPDATE_REPORT: 0x14,
            USAGE_RANGE_UPDATE_REPORT: 0x15,
            USAGE_CONTROL_REPORT: 0x16,
        })

    def test_other_usage_pages_are_ignored(self):
        ids = parse_report_ids(descriptor_for({USAGE_ATTRIBUTES_REPORT: 0x11}))
        self.assertNotIn(0x50, ids)

    def test_extended_usage_item_sets_the_page(self):
        descriptor = bytes([
            0x05, 0x59, 0x09, 0x01, 0xA1, 0x01,
            0x85, 0x21,
            0x0B, 0x60, 0x00, 0x59, 0x00,             # Usage Page 0x59, Usage 0x60
            0xA1, 0x02, 0xC0, 0xC0,
        ])
        self.assertEqual(parse_report_ids(descriptor), {USAGE_RANGE_UPDATE_REPORT: 0x21})

    def test_garbage_stops_cleanly(self):
        self.assertEqual(parse_report_ids(b"\x05"), {})
        self.assertEqual(parse_report_ids(b""), {})


class TestOpen(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLampArray({
            0: 0x1E,          # "1" - number row
            1: 0x04,          # "a"
            2: 0x00,          # no binding
        })
        self.backend = make_backend(self.fake)
        self.addCleanup(self.backend.close)

    def test_open_probes_lamps_and_takes_control(self):
        self.backend.open()
        self.assertEqual(self.backend.count, 3)
        self.assertAlmostEqual(self.backend.min_interval, 0.01)
        labels = [lamp.label for lamp in self.backend.lamps()]
        groups = [lamp.group for lamp in self.backend.lamps()]
        self.assertEqual(labels, ["1", "a", "lamp2"])
        self.assertEqual(groups, ["number-row", "key", "key"])
        self.assertEqual(self.backend.lamps()[0].x, 1.0)     # 1000 um -> 1 mm
        self.assertEqual(self.backend.lamps()[0].y, 2.0)
        # requests for lamps 0..2, then the control report leaving autonomous
        self.assertEqual(self.fake.sent, [
            request_report(0), request_report(1), request_report(2),
            control_report(0x06, 0),
        ])

    def test_number_row_comes_from_input_binding(self):
        from rgi.backends.lamparray import NUMBER_ROW_USAGES
        for usage in NUMBER_ROW_USAGES:
            self.assertIn(usage, range(0x00, 0x100))
        self.backend.open()
        number_row = [lamp for lamp in self.backend.lamps()
                      if lamp.group == "number-row"]
        self.assertEqual([lamp.label for lamp in number_row], ["1"])

    def test_descriptor_report_ids_win(self):
        descriptor = descriptor_for({
            USAGE_ATTRIBUTES_REPORT: 0x11,
            USAGE_ATTRIBUTES_REQUEST_REPORT: 0x12,
            USAGE_ATTRIBUTES_RESPONSE_REPORT: 0x13,
            USAGE_MULTI_UPDATE_REPORT: 0x14,
            USAGE_RANGE_UPDATE_REPORT: 0x15,
            USAGE_CONTROL_REPORT: 0x16,
        })
        fake = FakeLampArray({0: 0x1E}, attributes_id=0x11, request_id=0x12,
                             response_id=0x13, descriptor=descriptor)
        backend = make_backend(fake)
        backend.open()
        self.assertEqual(backend.report_ids[USAGE_CONTROL_REPORT], 0x16)
        self.assertEqual(fake.sent, [request_report(0, 0x12), control_report(0x16, 0)])
        backend.close()
        self.assertIn(control_report(0x16, 1), fake.sent)

    def test_explicit_report_ids_override_everything(self):
        fake = FakeLampArray({0: 0x04}, attributes_id=0x41, request_id=0x42,
                             response_id=0x43)
        backend = FakeBackend(transport_factory=lambda path: fake,
                              report_ids={USAGE_ATTRIBUTES_REPORT: 0x41,
                                          USAGE_ATTRIBUTES_REQUEST_REPORT: 0x42,
                                          USAGE_ATTRIBUTES_RESPONSE_REPORT: 0x43,
                                          USAGE_MULTI_UPDATE_REPORT: 0x44,
                                          USAGE_RANGE_UPDATE_REPORT: 0x45,
                                          USAGE_CONTROL_REPORT: 0x46})
        backend.open()
        self.assertEqual(backend.report_ids[USAGE_ATTRIBUTES_REPORT], 0x41)
        self.assertEqual(fake.sent[0][0], 0x42)
        backend.close()

    def test_device_that_does_not_answer_is_unavailable(self):
        fake = FakeLampArray({0: 0x04}, attributes=False)
        backend = make_backend(fake)
        with self.assertRaises(BackendUnavailable) as caught:
            backend.open()
        self.assertIn("did not answer", str(caught.exception))

    def test_zero_lamps_is_unavailable(self):
        backend = make_backend(FakeLampArray({}, count=0))
        with self.assertRaises(BackendUnavailable) as caught:
            backend.open()
        self.assertIn("0 lamps", str(caught.exception))

    def test_transport_error_is_wrapped(self):
        fake = FakeLampArray({0: 0x04})
        fake.fail_attributes = True
        backend = make_backend(fake)
        with self.assertRaises(BackendUnavailable) as caught:
            backend.open()
        self.assertIn("feature report", str(caught.exception))

    def test_short_lamp_report_is_refused(self):
        fake = FakeLampArray({0: 0x04})
        fake.short_lamp_report = True
        backend = make_backend(fake)
        with self.assertRaises(BackendUnavailable) as caught:
            backend.open()
        self.assertIn("expected at least 28", str(caught.exception))

    def test_failed_open_closes_the_transport(self):
        fake = FakeLampArray({0: 0x04}, attributes=False)
        backend = make_backend(fake)
        with self.assertRaises(BackendUnavailable):
            backend.open()
        self.assertEqual(fake.closed, 1)
        self.assertIsNone(backend.transport)

    def test_forced_count_skips_extra_lamps(self):
        backend = make_backend(FakeLampArray({0: 0x04, 1: 0x05, 2: 0x06}), count=2)
        backend.open()
        self.assertEqual(len(backend.lamps()), 2)
        self.assertEqual([lamp.label for lamp in backend.lamps()], ["a", "b"])

    def test_default_report_ids_match_the_reference_descriptor(self):
        self.assertEqual(DEFAULT_REPORT_IDS, {
            USAGE_ATTRIBUTES_REPORT: 0x01,
            USAGE_ATTRIBUTES_REQUEST_REPORT: 0x02,
            USAGE_ATTRIBUTES_RESPONSE_REPORT: 0x03,
            USAGE_MULTI_UPDATE_REPORT: 0x04,
            USAGE_RANGE_UPDATE_REPORT: 0x05,
            USAGE_CONTROL_REPORT: 0x06,
        })


class TestWrite(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLampArray({0: 0x04, 1: 0x05, 2: 0x06})
        self.backend = make_backend(self.fake)
        self.backend.open()
        self.addCleanup(self.backend.close)
        self.fake.sent.clear()

    def test_a_coloured_frame_is_one_multi_update(self):
        self.backend.write([(255, 0, 0), (0, 255, 0), (0, 0, 255)])
        self.assertEqual(self.fake.sent, [build_multi_update_report(
            [(0, (255, 0, 0)), (1, (0, 255, 0)), (2, (0, 0, 255))],
            report_id=0x04, update_complete=True)])

    def test_more_than_eight_lamps_chunk_with_the_flag_on_the_last(self):
        fake = FakeLampArray({i: 0x04 + i for i in range(10)})
        backend = make_backend(fake)
        backend.open()
        self.addCleanup(backend.close)
        fake.sent.clear()
        colours = [(i, 0, 0) for i in range(10)]
        backend.write(colours)
        self.assertEqual(len(fake.sent), 2)
        self.assertEqual(fake.sent[0][:3], bytes([0x04, 8, 0x00]))
        self.assertEqual(fake.sent[1][:3], bytes([0x04, 2, 0x01]))

    def test_a_uniform_frame_uses_a_range_update(self):
        self.backend.write([(9, 8, 7)] * 3)
        self.assertEqual(self.fake.sent, [build_range_update_report(
            0, 2, (9, 8, 7), report_id=0x05, update_complete=True)])

    def test_a_uniform_frame_falls_back_when_the_descriptor_lacks_range(self):
        descriptor = descriptor_for({
            USAGE_ATTRIBUTES_REPORT: 0x01,
            USAGE_ATTRIBUTES_REQUEST_REPORT: 0x02,
            USAGE_ATTRIBUTES_RESPONSE_REPORT: 0x03,
            USAGE_MULTI_UPDATE_REPORT: 0x04,
            USAGE_CONTROL_REPORT: 0x06,
        })
        fake = FakeLampArray({0: 0x04, 1: 0x05}, descriptor=descriptor)
        backend = make_backend(fake)
        backend.open()
        self.addCleanup(backend.close)
        fake.sent.clear()
        backend.write([(1, 2, 3)] * 2)
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0][0], 0x04)

    def test_wrong_shape_is_dropped_not_painted(self):
        self.backend.write([(255, 0, 0)])
        self.assertEqual(self.fake.sent, [])

    def test_write_before_open_is_refused(self):
        backend = make_backend(FakeLampArray({0: 0x04}))
        with self.assertRaises(BackendUnavailable):
            backend.write([(1, 2, 3)])


class TestClose(unittest.TestCase):
    def test_close_hands_the_array_back_and_is_safe_twice(self):
        fake = FakeLampArray({0: 0x04})
        backend = make_backend(fake)
        backend.open()
        fake.sent.clear()
        backend.close()
        self.assertEqual(fake.sent, [control_report(0x06, 1)])
        self.assertEqual(fake.closed, 1)
        backend.close()
        self.assertEqual(fake.sent, [control_report(0x06, 1)])
        self.assertEqual(fake.closed, 1)
        self.assertIsNone(backend.transport)


class TestAvailability(unittest.TestCase):
    def test_true_when_a_lamp_array_is_visible(self):
        with mock.patch.object(LamparrayBackend, "candidates",
                               return_value=[{"path": b"x"}]):
            self.assertTrue(LamparrayBackend.available())

    def test_false_when_none_is_visible(self):
        with mock.patch.object(LamparrayBackend, "candidates", return_value=[]):
            self.assertFalse(LamparrayBackend.available())

    def test_false_when_hidapi_is_missing(self):
        with mock.patch.object(LamparrayBackend, "_hid",
                               side_effect=BackendUnavailable("no hidapi")):
            self.assertFalse(LamparrayBackend.available())

    def test_lowercase_a_alias_keeps_old_imports_working(self):
        self.assertIs(LampArrayBackend, LamparrayBackend)


if __name__ == "__main__":
    unittest.main()
