from transaction_buffer import TransactionBuffer, normalize_product_path


def test_normalize_product_path_excludes_non_product_paths(tmp_path):
    assert normalize_product_path(tmp_path, "src/core.py") == "src/core.py"
    assert normalize_product_path(tmp_path, "tests/test_core.py") is None
    assert normalize_product_path(tmp_path, ".agentic/probe.py") is None
    assert normalize_product_path(tmp_path, "package.json") is None
    assert normalize_product_path(tmp_path, "../outside.py") is None


def test_transaction_preserves_first_file_for_one_followup(tmp_path):
    buffer = TransactionBuffer(tmp_path, followup_turns=1)

    assert buffer.record_mutation("core_math.py", checkpoint_id=4) is True
    decision = buffer.note_validation_failed("pytest failed\nE AssertionError: missing property")

    assert decision.action == "preserve"
    assert buffer.active is True
    assert buffer.files == ("core_math.py",)
    assert buffer.metrics()["checkpoint_id"] == 4
    assert "core_math.py" in buffer.control_block()
    assert "Remaining transaction repair turns: 0" in buffer.control_block()

    assert buffer.record_mutation("matrix_solver.py", checkpoint_id=5) is True
    assert buffer.files == ("core_math.py", "matrix_solver.py")
    assert buffer.note_validation_passed() is True
    assert buffer.active is False
    assert buffer.files == ()
    assert buffer.control_block() == ""


def test_transaction_expiry_does_not_erase_workspace_state(tmp_path):
    buffer = TransactionBuffer(tmp_path, followup_turns=1)
    buffer.record_mutation("a.py", checkpoint_id=1)

    assert buffer.note_validation_failed("first failure").action == "preserve"
    decision = buffer.note_validation_failed("second failure")

    assert decision.action == "recover"
    assert buffer.active is False
    assert buffer.expired is True
    assert buffer.files == ("a.py",)
    assert "State: EXPIRED" in buffer.control_block()
