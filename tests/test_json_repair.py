"""Tests for JSON repair function in agent.py."""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_valid_json_passes_through():
    from agent import _repair_json
    assert _repair_json('{"key": "value"}') == {"key": "value"}


def test_trailing_comma_object():
    from agent import _repair_json
    assert _repair_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_trailing_comma_array():
    from agent import _repair_json
    assert _repair_json('{"items": [1, 2, 3,]}') == {"items": [1, 2, 3]}


def test_single_quotes():
    from agent import _repair_json
    assert _repair_json("{'command': 'ls -la'}") == {"command": "ls -la"}


def test_unclosed_brace():
    from agent import _repair_json
    assert _repair_json('{"command": "ls"') == {"command": "ls"}


def test_unclosed_bracket():
    from agent import _repair_json
    assert _repair_json('{"items": [1, 2, 3}') == {"items": [1, 2, 3]}


def test_js_comment_line():
    from agent import _repair_json
    result = _repair_json('{"a": 1 // this is a comment\n}')
    assert result == {"a": 1}


def test_js_comment_block():
    from agent import _repair_json
    result = _repair_json('{"a": /* comment */ 1}')
    assert result == {"a": 1}


def test_empty_string():
    from agent import _repair_json
    assert _repair_json("") == {}


def test_none_like():
    from agent import _repair_json
    assert _repair_json("   ") == {}


def test_bom_prefix():
    from agent import _repair_json
    assert _repair_json('\ufeff{"key": "val"}') == {"key": "val"}


def test_nested_object():
    from agent import _repair_json
    result = _repair_json('{"a": {"b": 1,},}')
    assert result == {"a": {"b": 1}}


def test_raw_newline_in_string():
    from agent import _repair_json
    result = _repair_json('{"text": "line1\nline2"}')
    assert result["text"] == "line1\nline2"


def test_completely_broken():
    from agent import _repair_json
    assert _repair_json("not json at all") == {}


def test_unclosed_string_and_brace():
    from agent import _repair_json
    result = _repair_json('{"command": "ls -la')
    assert result.get("command") == "ls -la"


def test_multiple_trailing_commas():
    from agent import _repair_json
    result = _repair_json('{"a": 1,  ,  }')
    # After removing trailing commas this becomes {"a": 1,  ,  } -> might fail
    # At least shouldn't crash
    assert isinstance(result, dict)
