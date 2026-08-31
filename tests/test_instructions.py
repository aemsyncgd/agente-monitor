# tests/test_instructions.py
import os
import json
import tempfile
from src.ai.instructions import InstructionManager, InstructionType, DEFAULT_INSTRUCTIONS


def test_default_instructions():
    mgr = InstructionManager(os.path.join(tempfile.gettempdir(), "test_instructions.json"))
    insts = mgr.get_all()
    assert len(insts) == len(DEFAULT_INSTRUCTIONS)


def test_toggle_instruction():
    path = os.path.join(tempfile.gettempdir(), "test_toggle.json")
    if os.path.exists(path):
        os.remove(path)
    mgr = InstructionManager(path)

    # Disable an instruction
    success = mgr.set_enabled("detect_anomaly", False)
    assert success is True
    inst = mgr.get_by_id("detect_anomaly")
    assert inst.enabled is False

    # Re-enable
    success = mgr.set_enabled("detect_anomaly", True)
    assert success is True
    inst = mgr.get_by_id("detect_anomaly")
    assert inst.enabled is True

    os.remove(path)


def test_update_params():
    path = os.path.join(tempfile.gettempdir(), "test_params.json")
    if os.path.exists(path):
        os.remove(path)
    mgr = InstructionManager(path)

    success = mgr.update_params("detect_anomaly", {"check_interval_seconds": 120})
    assert success is True
    inst = mgr.get_by_id("detect_anomaly")
    assert inst.params["check_interval_seconds"] == 120

    os.remove(path)


def test_get_enabled_sorted_by_priority():
    path = os.path.join(tempfile.gettempdir(), "test_enabled.json")
    if os.path.exists(path):
        os.remove(path)
    mgr = InstructionManager(path)

    enabled = mgr.get_enabled()
    assert len(enabled) > 0
    # Should be sorted by priority (ascending)
    priorities = [i.priority for i in enabled]
    assert priorities == sorted(priorities)

    os.remove(path)


def test_add_custom_instruction():
    path = os.path.join(tempfile.gettempdir(), "test_custom.json")
    if os.path.exists(path):
        os.remove(path)
    mgr = InstructionManager(path)

    inst = mgr.add_custom(
        name="Custom Test",
        description="A test instruction",
        instruction_type=InstructionType.DETECT_ANOMALY,
        params={"test": True},
    )
    assert inst.id.startswith("custom_")
    assert inst.name == "Custom Test"
    assert mgr.get_by_id(inst.id) is not None

    # Can delete custom
    success = mgr.remove_custom(inst.id)
    assert success is True
    assert mgr.get_by_id(inst.id) is None

    os.remove(path)


def test_cannot_delete_built_in():
    path = os.path.join(tempfile.gettempdir(), "test_nodelete.json")
    if os.path.exists(path):
        os.remove(path)
    mgr = InstructionManager(path)

    success = mgr.remove_custom("detect_anomaly")
    assert success is False
    assert mgr.get_by_id("detect_anomaly") is not None

    os.remove(path)


def test_to_summary():
    path = os.path.join(tempfile.gettempdir(), "test_summary.json")
    if os.path.exists(path):
        os.remove(path)
    mgr = InstructionManager(path)

    summary = mgr.to_summary()
    assert len(summary) > 0
    assert "id" in summary[0]
    assert "name" in summary[0]
    assert "type" in summary[0]
    assert "enabled" in summary[0]

    os.remove(path)
