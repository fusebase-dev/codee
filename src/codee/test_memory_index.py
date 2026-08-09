"""Unit tests for the MEMORY.md line parser."""
from codee.admin_service import parse_index


def test_wellformed_line():
    [e] = parse_index("- [My Title](my_file.md) — short hook")
    assert e["matched"]
    assert e["title"] == "My Title"
    assert e["file"] == "my_file.md"
    assert e["hook"] == "short hook"
    assert e["lineno"] == 0


def test_line_without_hook():
    [e] = parse_index("- [No Hook](nohook.md)")
    assert e["matched"]
    assert e["file"] == "nohook.md"
    assert e["hook"] == ""


def test_non_matching_line_preserved():
    [e] = parse_index("## Some heading, not an entry")
    assert e["matched"] is False
    assert e["raw"] == "## Some heading, not an entry"


if __name__ == "__main__":
    test_wellformed_line()
    test_line_without_hook()
    test_non_matching_line_preserved()
    print("ok")
