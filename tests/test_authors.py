"""Tests for author-name matching (services/authors.py).

The regression cases are drawn from a real photo-intake run whose covers
silently failed: the vision model read ASCII-ised, abbreviated names off
the spines and the old substring match rejected every one of them.
"""
import pytest

from app.services import authors


class TestNormalize:
    def test_strips_combining_accents(self):
        assert authors.normalize("García Márquez") == ["garcia", "marquez"]

    @pytest.mark.parametrize("name,expected", [
        ("Stanisław Lem", ["stanislaw", "lem"]),
        ("Jo Nesbø", ["jo", "nesbo"]),
        ("Đorđe Balašević", ["dorde", "balasevic"]),
        ("Halldór Laxness", ["halldor", "laxness"]),
    ])
    def test_folds_stroked_letters_nfkd_leaves_alone(self, name, expected):
        """NFKD decomposes é but not ł/ø/đ — those need the explicit table."""
        assert authors.normalize(name) == expected

    def test_splits_punctuation_rather_than_keeping_it(self):
        assert authors.normalize("R.P. Feynman!") == ["r", "p", "feynman"]

    def test_empty_input(self):
        assert authors.normalize("") == []
        assert authors.normalize("...") == []


class TestMatches:
    @pytest.mark.parametrize("wanted,found,why", [
        ("Stanislaw Lem", "Stanisław Lem", "ASCII-ised diacritic"),
        ("Richard P. Feynman", "Richard Phillips Feynman", "initial vs full middle name"),
        ("James Duane", "James J. Duane", "dropped middle initial"),
        ("Gabriel Garcia Marquez", "Gabriel García Márquez", "stripped accents"),
        ("R. P. Feynman", "Richard Phillips Feynman", "all-initial given names"),
        ("Wickman", "Gino Wickman", "bare surname on the wanted side"),
        ("Gino Wickman", "Wickman", "bare surname on the found side"),
        ("Matt Dinniman", "Matt Dinniman, Someone Else", "first of a joined list"),
        ("Ralph Leighton", "Richard Phillips Feynman, Ralph Leighton", "later in a joined list"),
        ("joseph heller", "JOSEPH HELLER", "case difference"),
    ])
    def test_accepts_the_same_person(self, wanted, found, why):
        assert authors.matches(wanted, found), why

    @pytest.mark.parametrize("wanted,found,why", [
        ("George Orwell", "Jane Smith", "unrelated author"),
        ("Frank Herbert", "Brian Herbert", "same surname, different person"),
        ("Richard Feynman", "Robert Feynman", "given names collide only on initial"),
        ("Herman Melville", "SparkNotes Editors", "study guide, the case the check exists for"),
        ("Andy Weir", None, "no author on the result"),
        ("Andy Weir", "", "empty author on the result"),
    ])
    def test_rejects_a_different_person(self, wanted, found, why):
        assert not authors.matches(wanted, found), why

    def test_no_wanted_author_accepts_anything(self):
        """Nothing to check against — the caller has no author to verify."""
        assert authors.matches(None, "Anyone At All")
        assert authors.matches("", "Anyone At All")

    def test_unparseable_wanted_author_is_not_a_free_pass(self):
        """Punctuation-only names normalize to nothing; that must not match."""
        assert not authors.matches("???", "Frank Herbert")

    def test_only_the_first_wanted_author_is_checked(self):
        """Matches the documented contract: wanted's first author decides it."""
        assert authors.matches("Frank Herbert, Kevin J. Anderson", "Frank Herbert")
        assert not authors.matches("Kevin J. Anderson, Frank Herbert", "Frank Herbert")


class TestJoinNames:
    def test_keeps_caller_order(self):
        assert authors.join_names(["Kevin J. Anderson", "Frank Herbert"]) == "Kevin J. Anderson, Frank Herbert"

    def test_drops_blanks_and_none(self):
        assert authors.join_names(["Frank Herbert", None, "  ", ""]) == "Frank Herbert"

    def test_drops_exact_repeat_first_occurrence_wins(self):
        assert authors.join_names(["Frank Herbert", "Frank Herbert"]) == "Frank Herbert"

    def test_case_different_names_are_kept_as_two(self):
        assert authors.join_names(["Lem", "lem"]) == "Lem, lem"

    def test_diacritic_variants_are_kept_as_two(self):
        """join_names does exact dedup only — it is not authors.matches()."""
        assert authors.join_names(["Stanisław Lem", "Stanislaw Lem"]) == "Stanisław Lem, Stanislaw Lem"

    def test_empty_input_returns_none(self):
        assert authors.join_names([]) is None

    def test_all_blank_input_returns_none(self):
        assert authors.join_names([None, "  ", ""]) is None

    def test_single_name_has_no_separator(self):
        assert authors.join_names(["Frank Herbert"]) == "Frank Herbert"

    def test_bare_string_argument_raises_type_error(self):
        with pytest.raises(TypeError):
            authors.join_names("Frank Herbert")

    def test_bare_mapping_argument_raises_type_error(self):
        """Iterating a mapping yields its keys, so this would store "name"."""
        with pytest.raises(TypeError):
            authors.join_names({"name": "Frank Herbert"})

    def test_non_str_elements_are_dropped(self):
        assert authors.join_names(["Frank Herbert", {"name": "Someone"}, 42]) == "Frank Herbert"

    def test_accepts_a_generator(self):
        assert authors.join_names(n for n in ["Frank Herbert", "Kevin J. Anderson"]) == "Frank Herbert, Kevin J. Anderson"

    def test_accepts_a_tuple(self):
        assert authors.join_names(("Frank Herbert", "Kevin J. Anderson")) == "Frank Herbert, Kevin J. Anderson"
