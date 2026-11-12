"""The shared search helper turns user terms into SQL LIKE patterns."""

from core.search import like_pattern


def test_plain_terms_keep_the_contains_behaviour():
    assert like_pattern("Sheikh") == "%Sheikh%"
    assert like_pattern("  Anmba ") == "%Anmba%"
    assert like_pattern("") is None
    assert like_pattern(None) is None
    assert like_pattern("   ") is None


def test_star_and_question_mark_become_sql_wildcards():
    assert like_pattern("Sh*ura") == "Sh%ura"
    assert like_pattern("Anmb?") == "Anmb_"
    assert like_pattern("*") == "%"
    assert like_pattern("?mba") == "_mba"


def test_literal_percent_and_underscore_are_escaped():
    # A typed % or _ must match itself, not act as a wildcard.
    assert like_pattern("50%") == "%50\\%%"
    assert like_pattern("a_b") == "%a\\_b%"
    assert like_pattern("a\\b") == "%a\\\\b%"


def test_wildcard_terms_escape_existing_sql_wildcards():
    assert like_pattern("a%*") == "a\\%%"
    assert like_pattern("50%?") == "50\\%_"
