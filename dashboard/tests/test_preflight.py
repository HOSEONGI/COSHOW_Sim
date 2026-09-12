"""Preflight telemetry must observe safety decisions without making new ones."""
import importlib.util
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
import rclpy
from geometry_msgs.msg import PoseStamped
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

from crazyflie_interfaces.msg import LogDataGeneric, Status


SPEC = importlib.util.spec_from_file_location(
    "dashboard_preflight", Path(__file__).resolve().parents[2] / "tools/preflight_node.py")
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Recorder:
    def __init__(self):
        self.messages = []

    def publish(self, message):
        self.messages.append(json.loads(message.data))


@pytest.fixture
def node(monkeypatch):
    started = not rclpy.ok()
    if started:
        rclpy.init(args=[])
    clock = Clock()
    monkeypatch.setattr(preflight, "time", clock)
    instance = preflight.Preflight()
    instance.test_clock = clock
    yield instance
    instance.destroy_node()
    if started:
        rclpy.try_shutdown()


def record(node):
    recorder = Recorder()
    node.pub_status = recorder
    node.test_clock.now += 1.01
    node._publish_status()
    return recorder


def seed_gate(node):
    for name in node.names:
        pose = PoseStamped()
        pose.pose.position.x, pose.pose.position.y = node.expected[name]
        node._on_pose(name, pose)
        status = Status()
        status.battery_voltage = 3.98
        status.supervisor_info = Status.SUPERVISOR_INFO_CAN_BE_ARMED
        node._on_status(name, status)
        for _ in range(node.hist_len):
            node._on_kalman(name, LogDataGeneric(values=[0.0004, 0.0003, 0.0002]))
    node.pose_stable = 0.0


def test_initial_status_schema_and_latched_qos(node):
    assert hasattr(node, "pub_status"), "missing /preflight/status publisher"
    qos = node.pub_status.qos_profile
    assert qos.depth == 1
    assert qos.reliability == ReliabilityPolicy.RELIABLE
    assert qos.durability == DurabilityPolicy.TRANSIENT_LOCAL
    payload = record(node).messages[-1]
    assert payload == {
        "stage": 0,
        "stages": [{"name": name, "result": "pending"} for name in (
            "server_ready", "reset_estimators", "gate", "arm")],
        "drones": {name: {
            "kal_ok": False, "kal_range": None, "pose_ok": False,
            "pose_err": None, "sup_ok": False, "sup_why": "no fresh /status",
            "battery_v": None, "armed": False, "can_fly": False,
        } for name in node.names},
        "ready": False, "abort_reason": None, "t": node.test_clock.now,
    }


def test_late_ros_subscriber_receives_initial_status(node):
    observer = Node("preflight_status_late_test")
    received = []
    observer.create_subscription(
        String, "/preflight/status", lambda message: received.append(json.loads(message.data)),
        QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                   durability=DurabilityPolicy.TRANSIENT_LOCAL))
    try:
        deadline = time.monotonic() + 3.0
        while not received and time.monotonic() < deadline:
            rclpy.spin_once(observer, timeout_sec=0.05)
        assert received, "a late subscriber did not receive retained status"
        assert received[0]["stage"] == 0
        assert received[0]["ready"] is False
    finally:
        observer.destroy_node()


def test_only_content_changes_or_one_second_heartbeat_publish(node):
    recorder = record(node)
    count = len(recorder.messages)
    node.test_clock.now += 0.2
    node._publish_status()
    assert len(recorder.messages) == count
    node.test_clock.now += 0.8
    node._publish_status()
    assert len(recorder.messages) == count + 1
    first, second = recorder.messages[-2:]
    assert first.pop("t") < second.pop("t")
    assert first == second
    node._set_ready(True, "test ready")
    assert recorder.messages[-1]["ready"] is True
    assert len(recorder.messages) == count + 2


def test_async_change_does_not_delay_next_heartbeat_for_two_seconds(node, monkeypatch):
    recorder = record(node)
    monkeypatch.setattr(preflight, "time", time)
    node._publish_status()
    # A receipt near the next periodic callback must not suppress the heartbeat
    # until the following whole-second callback (almost two seconds later).
    time.sleep(0.8)
    node._on_status(node.names[0], Status())
    count = len(recorder.messages)
    deadline = time.monotonic() + 1.15
    while len(recorder.messages) == count and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.02)
    assert len(recorder.messages) > count, "heartbeat gap exceeds one second plus timer tolerance"


@pytest.mark.parametrize("method,stage", [
    ("wait_server_ready", 1), ("reset_estimators", 2),
    ("wait_preflight", 3), ("arm_all", 4),
])
def test_failure_marks_original_stage_decision(node, method, stage):
    recorder = record(node)
    node.t_server = node.t_pre = 0.0
    node.cli_params = SimpleNamespace(wait_for_service=lambda **kwargs: False)
    node.cli_arm = {node.names[0]: SimpleNamespace(wait_for_service=lambda **kwargs: False)}
    assert getattr(node, method)() is False
    updates = [p for p in recorder.messages if p["stage"] == stage]
    assert updates[0]["stages"][stage - 1]["result"] == "running"
    assert updates[-1]["stages"][stage - 1]["result"] == "fail"
    assert updates[-1]["ready"] is False
    assert updates[-1]["abort_reason"] is None


@pytest.mark.parametrize("case", ["timeout", "empty", "rejected"])
def test_reset_response_failure_marks_stage_without_proceeding(node, case):
    recorder = record(node)
    result = None if case == "empty" else SimpleNamespace(
        results=[SimpleNamespace(successful=False, reason="reset rejected")])
    future = SimpleNamespace(done=lambda: case != "timeout", result=lambda: result)
    node.cli_params = SimpleNamespace(wait_for_service=lambda **kwargs: True,
                                     call_async=lambda request: future)
    assert node.reset_estimators() is False
    assert recorder.messages[-1]["stage"] == 2
    assert recorder.messages[-1]["stages"][1]["result"] == "fail"
    assert recorder.messages[-1]["stages"][2]["result"] == "pending"


def test_gate_observation_is_cached_and_flight_status_remains_live(node, monkeypatch):
    recorder = record(node)
    seed_gate(node)
    real_pose_ok = node._pose_ok
    evaluations = []

    def count_pose(name):
        evaluations.append(name)
        return real_pose_ok(name)

    monkeypatch.setattr(node, "_pose_ok", count_pose)
    assert node.wait_preflight() is True
    assert evaluations == node.names
    payload = recorder.messages[-1]
    assert payload["stage"] == 3
    assert payload["stages"][2]["result"] == "pass"
    for name in node.names:
        assert payload["drones"][name]["kal_ok"] is True
        assert payload["drones"][name]["kal_range"] == [0.0, 0.0, 0.0]
        assert payload["drones"][name]["pose_ok"] is True
        assert payload["drones"][name]["pose_err"] == [0.0, 0.0, 0.0]
        assert payload["drones"][name]["sup_ok"] is True
        assert payload["drones"][name]["sup_why"] == "manual-arm"

    name = node.names[0]
    airborne = PoseStamped()
    airborne.pose.position.z = 1.0
    node._on_pose(name, airborne)
    armed = Status()
    armed.battery_voltage = 3.7
    armed.supervisor_info = (Status.SUPERVISOR_INFO_IS_ARMED
                             | Status.SUPERVISOR_INFO_CAN_FLY)
    count = len(recorder.messages)
    node._on_status(name, armed)
    assert len(recorder.messages) == count + 1
    node.test_clock.now += 1.1
    node._publish_status()
    assert evaluations == node.names, "telemetry re-ran the stateful initial-position gate"
    drone = recorder.messages[-1]["drones"][name]
    assert drone["pose_ok"] is True
    assert drone["pose_err"] == [0.0, 0.0, 0.0]
    assert drone["armed"] is True
    assert drone["can_fly"] is True
    assert drone["battery_v"] == pytest.approx(3.7)


def test_stage_success_is_reported_at_each_existing_pass(node):
    recorder = record(node)
    node.cli_addlog = {n: SimpleNamespace(service_is_ready=lambda: True) for n in node.names}
    seed_gate(node)
    assert node.wait_server_ready() is True
    result = SimpleNamespace(results=[SimpleNamespace(successful=True)])
    future = SimpleNamespace(done=lambda: True, result=lambda: result)
    node.cli_params = SimpleNamespace(wait_for_service=lambda **kwargs: True,
                                     call_async=lambda request: future)
    assert node.reset_estimators() is True
    seed_gate(node)
    assert node.wait_preflight() is True
    arm_calls = []
    for name in node.names:
        status = Status()
        status.supervisor_info = (Status.SUPERVISOR_INFO_IS_ARMED
                                  | Status.SUPERVISOR_INFO_CAN_FLY)
        node._on_status(name, status)
    node.cli_arm = {n: SimpleNamespace(wait_for_service=lambda **kwargs: True,
                                      call_async=lambda request: arm_calls.append(request.arm))
                    for n in node.names}
    assert node.arm_all() is True
    assert arm_calls == [True] * len(node.names)
    assert recorder.messages[-1]["stage"] == 4
    assert [s["result"] for s in recorder.messages[-1]["stages"]] == ["pass"] * 4


def test_arm_timeout_still_disarms_before_reporting_failure(node):
    recorder = record(node)
    calls = []
    node.cli_arm = {n: SimpleNamespace(wait_for_service=lambda **kwargs: True,
                                      call_async=lambda request: calls.append(request.arm))
                    for n in node.names}
    node.t_arm = 0.0
    assert node.arm_all() is False
    assert calls == [True] * len(node.names) + [False] * len(node.names)
    assert recorder.messages[-1]["stages"][3]["result"] == "fail"


def test_inflight_abort_publishes_exact_reason_and_ready_false_before_landing(node, monkeypatch):
    recorder = record(node)
    for name in ("wait_server_ready", "reset_estimators", "wait_preflight", "arm_all"):
        monkeypatch.setattr(node, name, lambda: True)
    reason = "cf230: /status 끊김"
    monkeypatch.setattr(node, "check_inflight", lambda: reason)
    land = []
    monkeypatch.setattr(node, "land_all", lambda why: land.append((why, recorder.messages[-1])))
    node.run()
    assert land[0][0] == reason
    assert land[0][1]["ready"] is False
    assert land[0][1]["abort_reason"] == reason
