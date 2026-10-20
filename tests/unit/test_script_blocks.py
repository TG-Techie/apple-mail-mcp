"""Unit tests for the AppleScript fragments the connector's scripts share."""


import pytest

from apple_mail_mcp.script_blocks import wrap_as_json_script


class TestWrapAsJsonScript:
    def test_wrapper_contains_framework_directive(self) -> None:
        script = wrap_as_json_script(
            'tell application "Mail"\n    set resultData to {}\nend tell',
            timeout=60,
        )
        assert 'use framework "Foundation"' in script
        assert "use scripting additions" in script

    def test_wrapper_appends_json_serialization(self) -> None:
        script = wrap_as_json_script(
            'tell application "Mail"\n    set resultData to {}\nend tell',
            timeout=60,
        )
        assert "NSJSONSerialization" in script
        assert "dataWithJSONObject:resultData" in script

    def test_wrapper_preserves_body(self) -> None:
        body = 'tell application "Mail"\n    set resultData to {name:"INBOX"}\nend tell'
        script = wrap_as_json_script(body, timeout=60)
        assert body in script

    def test_wrapper_orders_framework_before_body_before_epilogue(self) -> None:
        body = 'tell application "Mail"\n    set resultData to {name:"INBOX"}\nend tell'
        script = wrap_as_json_script(body, timeout=60)
        framework_idx = script.index('use framework "Foundation"')
        body_idx = script.index(body)
        epilogue_idx = script.index("NSJSONSerialization")
        assert framework_idx < body_idx < epilogue_idx

    # Regression coverage for issue #227 — Mail's default AppleEvent timeout
    # is 60 s, so without an explicit `with timeout` clause an Exchange/EWS
    # iteration that takes 70 s raises `AppleEvent timed out (-1712)` no
    # matter what subprocess timeout the connector was constructed with.

    def test_wrapper_includes_with_timeout_clause(self) -> None:
        body = 'tell application "Mail"\n    set resultData to {}\nend tell'
        script = wrap_as_json_script(body, timeout=180)
        assert "with timeout of 180 seconds" in script
        assert "end timeout" in script

    def test_wrapper_timeout_brackets_the_tell_body(self) -> None:
        """The tell block must be inside the with-timeout block — putting it
        outside leaves the AppleEvent default of 60 s in force."""
        body = 'tell application "Mail"\n    set resultData to {}\nend tell'
        script = wrap_as_json_script(body, timeout=180)
        with_idx = script.index("with timeout of 180 seconds")
        body_idx = script.index(body)
        end_timeout_idx = script.index("end timeout")
        assert with_idx < body_idx < end_timeout_idx

    @pytest.mark.parametrize("timeout", [30, 60, 120, 300, 600])
    def test_wrapper_emits_caller_supplied_timeout(self, timeout: int) -> None:
        body = 'tell application "Mail"\n    set resultData to {}\nend tell'
        script = wrap_as_json_script(body, timeout=timeout)
        assert f"with timeout of {timeout} seconds" in script
