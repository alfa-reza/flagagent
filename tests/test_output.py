from flagagent.tools import (
    LOGGED_TOOL_OUTPUT_BYTES,
    MODEL_TOOL_OUTPUT_BYTES,
    ShellResult,
    normalize_shell_result,
    truncate_utf8,
)


def test_truncate_utf8_preserves_head_tail_and_byte_budget():
    value, truncated = truncate_utf8("abcdefghijklmnopqrstuvwxyz", 14)

    assert truncated is True
    assert value.startswith("a")
    assert "truncated" in value
    assert value.endswith("z")
    assert len(value.encode()) <= 14


def test_truncate_utf8_handles_multibyte_and_tiny_budgets():
    value, truncated = truncate_utf8("αβγδε", 8)
    tiny, tiny_truncated = truncate_utf8("abcdef", 3)

    value.encode("utf-8")
    assert truncated is True
    assert len(value.encode()) <= 8
    assert tiny_truncated is True
    assert len(tiny.encode()) <= 3


def test_normalization_bounds_streams_independently_from_original():
    original = ShellResult("A" * 40, "B" * 40, 0, False, False)

    model, logged = normalize_shell_result(original, model_limit=16, logged_limit=32)

    assert len(model.stdout.encode()) <= 16
    assert len(model.stderr.encode()) <= 16
    assert len(logged.stdout.encode()) <= 32
    assert len(logged.stderr.encode()) <= 32
    assert logged.stdout.endswith("A")
    assert logged.stderr.endswith("B")
    assert len(logged.stdout) > len(model.stdout)
    assert model.truncated is True
    assert logged.truncated is True


def test_normalization_preserves_upstream_truncation_for_short_output():
    original = ShellResult("short", "", 0, False, True)

    model, logged = normalize_shell_result(original, model_limit=16, logged_limit=32)

    assert model.stdout == "short"
    assert logged.stdout == "short"
    assert model.truncated is True
    assert logged.truncated is True


def test_canonical_byte_boundaries_are_exact():
    at_limit = truncate_utf8("a" * MODEL_TOOL_OUTPUT_BYTES, MODEL_TOOL_OUTPUT_BYTES)
    assert at_limit == ("a" * MODEL_TOOL_OUTPUT_BYTES, False)

    over_limit = truncate_utf8(
        "a" * (MODEL_TOOL_OUTPUT_BYTES + 1), MODEL_TOOL_OUTPUT_BYTES
    )
    assert over_limit[1] is True
    assert len(over_limit[0].encode()) <= MODEL_TOOL_OUTPUT_BYTES
    assert "truncated" in over_limit[0]
    assert over_limit[0].startswith("a")
    assert over_limit[0].endswith("a")

    logged_over = truncate_utf8(
        "b" * (LOGGED_TOOL_OUTPUT_BYTES + 1), LOGGED_TOOL_OUTPUT_BYTES
    )
    assert logged_over[1] is True
    assert len(logged_over[0].encode()) <= LOGGED_TOOL_OUTPUT_BYTES
    assert "truncated" in logged_over[0]
    assert logged_over[0].startswith("b")
    assert logged_over[0].endswith("b")

    multibyte_exact = "α" * (MODEL_TOOL_OUTPUT_BYTES // 2)
    assert truncate_utf8(multibyte_exact, MODEL_TOOL_OUTPUT_BYTES) == (
        multibyte_exact,
        False,
    )
    multibyte_over, truncated = truncate_utf8(
        multibyte_exact + "α", MODEL_TOOL_OUTPUT_BYTES
    )
    assert truncated is True
    assert len(multibyte_over.encode()) <= MODEL_TOOL_OUTPUT_BYTES
    assert "truncated" in multibyte_over


def test_default_limits_bound_multibyte_streams_independently():
    original = ShellResult(
        "α" * (MODEL_TOOL_OUTPUT_BYTES + 1),
        "β" * (LOGGED_TOOL_OUTPUT_BYTES + 1),
        0,
        False,
    )

    model, logged = normalize_shell_result(original)

    assert len(model.stdout.encode()) <= MODEL_TOOL_OUTPUT_BYTES
    assert len(model.stderr.encode()) <= MODEL_TOOL_OUTPUT_BYTES
    assert len(logged.stdout.encode()) <= LOGGED_TOOL_OUTPUT_BYTES
    assert len(logged.stderr.encode()) <= LOGGED_TOOL_OUTPUT_BYTES
    assert "truncated" in model.stdout
    assert "truncated" in model.stderr
    assert "truncated" not in logged.stdout
    assert "truncated" in logged.stderr
    assert model.truncated is True
    assert logged.truncated is True
    assert model.stdout.startswith("α")
    assert model.stdout.endswith("α")
    assert logged.stdout == original.stdout
    assert len(logged.stdout) > len(model.stdout)
    model.stdout.encode("utf-8")
    logged.stderr.encode("utf-8")


def test_exact_boundary_is_not_truncated():
    value, truncated = truncate_utf8("12345678", 8)

    assert value == "12345678"
    assert truncated is False
