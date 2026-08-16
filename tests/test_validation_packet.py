import json

from validation_packet import build_failed_validation_packet, extract_first_failure


def test_packet_selects_first_python_failure_and_confines_paths(tmp_path):
    source = tmp_path / "src" / "target.py"
    test = tmp_path / "tests" / "test_target.py"
    source.parent.mkdir()
    test.parent.mkdir()
    source.write_text("def calculate():\n    return 1\n")
    test.write_text("def test_calculate(): pass\n")
    output = (
        f'File "{source}", line 1, in calculate\n'
        "    return 1\n"
        "AssertionError: expected 2, got 1\n"
        f'File "{test}", line 1, in test_calculate\n'
        "AssertionError: later cascade\n"
    )

    packet = build_failed_validation_packet(
        "run_tests", {"command": "pytest -q", "cwd": str(tmp_path)}, output,
        tmp_path, changed_paths=("src/target.py",),
    )

    assert packet.validation_status == "behavior_failure"
    assert packet.detected_source_path == "src/target.py"
    assert packet.primary_failure.error_type == "AssertionError"
    assert packet.primary_failure.line == 1
    assert packet.transaction_overlaps == ("src/target.py",)
    assert str(tmp_path) not in packet.to_json()


def test_packet_marks_no_tests_as_setup_without_inventing_product_path(tmp_path):
    packet = build_failed_validation_packet(
        "run_tests", {"command": "pytest -q"},
        "Exit code: 5\nno tests ran in 0.01s",
        tmp_path,
    )
    assert packet.validation_status == "setup_failure"
    assert packet.detected_source_path == ""
    assert packet.primary_failure.kind == "setup"


def test_packet_is_bounded_and_canonical_json(tmp_path):
    packet = build_failed_validation_packet(
        "run_tests", {"command": "pytest -q"},
        "TypeError: " + ("x" * 5000), tmp_path,
    )
    rendered = packet.to_json()
    assert len(rendered) < 3000
    assert json.loads(rendered)["schema_version"] == "1"


def test_parser_returns_none_for_non_diagnostic_output(tmp_path):
    assert extract_first_failure("still working\ninspected files\n", tmp_path) is None
