from flagagent.tools import ShellResult, normalize_shell_result, truncate_utf8


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


def test_exact_boundary_is_not_truncated():
    value, truncated = truncate_utf8("12345678", 8)

    assert value == "12345678"
    assert truncated is False
