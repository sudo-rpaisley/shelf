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

    @pytest.mark.parametrize("name,expected", [
        ("O'Sullivan", ["o", "sullivan"]),
        ("Jean-Paul Sartre", ["jean", "paul", "sartre"]),
        ("Agent 007", ["agent", "007"]),
        ("Straße", ["strasse"]),
    ])
    def test_latin_pins_apostrophe_hyphen_digit_eszett(self, name, expected):
        """Regression pins: widening the split regex must not move these."""
        assert authors.normalize(name) == expected

    @pytest.mark.parametrize("name", [
        "刘慈欣",
        "Лев Толстой",
        "نجيب محفوظ",
    ])
    def test_non_latin_names_no_longer_normalize_to_nothing(self, name):
        """Before this change, the [a-z0-9] allowlist deleted every non-Latin
        letter, so these all normalized to []."""
        assert authors.normalize(name) != []


class TestNameKey:
    def test_initial_spacing_variants_are_one_identity(self):
        assert authors.name_key("J. R. R. Tolkien") == authors.name_key("J.R.R. Tolkien")

    def test_diacritic_variants_are_one_identity(self):
        assert authors.name_key("Stanisław Lem") == authors.name_key("Stanislaw Lem")

    def test_initial_vs_full_given_name_are_different_identities(self):
        """name_key is identity, not authors.matches() — G22."""
        assert authors.name_key("J. Smith") != authors.name_key("John Smith")

    def test_cyrillic_round_trips_through_matches(self):
        assert authors.matches("Лев Толстой", "Лев Толстой")


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


class TestParse:
    """One case per row of the measured table in issue #117's design.

    `parse` is the inverse of `join_names`: the stored string stays
    canonical, this only recovers the ordered (name, role) entries an
    indexer would derive from it.
    """

    def test_none_and_blank(self):
        assert authors.parse(None) == []
        assert authors.parse("") == []
        assert authors.parse("   ") == []

    def test_role_suffix_after_translator(self):
        result = authors.parse("Cixin Liu, Ken Liu - translator")
        assert [p.name for p in result] == ["Cixin Liu", "Ken Liu"]
        assert [p.role for p in result] == [None, "translator"]
        assert [p.position for p in result] == [0, 1]

    def test_generational_suffix_rejoins_the_name(self):
        result = authors.parse("William E. Shotts, Jr.")
        assert len(result) == 1
        assert result[0].name == "William E. Shotts, Jr."
        assert result[0].role is None

    def test_oxford_and_curly_apostrophe(self):
        result = authors.parse("Bryan O’Sullivan, John Goerzen, and Donald Bruce Stewart")
        assert [p.name for p in result] == [
            "Bryan O’Sullivan",
            "John Goerzen",
            "Donald Bruce Stewart",
        ]

    def test_ampersand_with_no_comma_does_not_split(self):
        result = authors.parse("Alberto Artasanchez & Prateek Joshi")
        assert len(result) == 1
        assert result[0].name == "Alberto Artasanchez & Prateek Joshi"
        assert result[0].role is None

    def test_last_comma_first_is_a_documented_limit_not_a_bug(self):
        """"Last, First" is indistinguishable from two co-authors from the
        string alone (design §2). This pins the limit: it gives two authors,
        and the fix is editing the stored string, not a smarter parser."""
        result = authors.parse("Williams, Robin")
        assert [p.name for p in result] == ["Williams", "Robin"]

    def test_role_dash_requires_exactly_one_trailing_word(self):
        result = authors.parse("Humble Book Bundle - A.I. by Packt")
        assert len(result) == 1
        assert result[0].name == "Humble Book Bundle - A.I. by Packt"
        assert result[0].role is None

    def test_trailing_dash_with_nothing_after_is_not_a_role(self):
        result = authors.parse(
            "Humble Book Bundle - Arduino  Raspberry Pi presented by MAKE -"
        )
        assert len(result) == 1
        assert result[0].role is None
        # Inner double space is collapsed to one; the dangling " -" is kept
        # as written, since it is not a role.
        assert result[0].name == (
            "Humble Book Bundle - Arduino Raspberry Pi presented by MAKE -"
        )

    def test_unlisted_word_after_dash_is_not_a_role(self):
        """Test-drive Observation 1: 11 prod rows read "· llm" as a role
        under parser version 1."""
        result = authors.parse(
            "Humble Tech Book Bundle - LLM, Agentic AI Career Accelerator Bundle by Packt"
        )
        assert [p.name for p in result] == [
            "Humble Tech Book Bundle - LLM",
            "Agentic AI Career Accelerator Bundle by Packt",
        ]
        assert [p.role for p in result] == [None, None]

    @pytest.mark.parametrize("word", [
        # Every role measured on the prod collection, plus the edit the
        # test drive made.
        "translator", "adaptation", "adaptor", "introduction", "introductions",
        "foreword",
    ])
    def test_measured_role_words_stay_roles(self, word):
        result = authors.parse(f"Jane Doe, John Roe - {word.capitalize()}")
        assert [p.role for p in result] == [None, word]
        assert result[1].name == "John Roe"

    def test_initial_spacing_variants_dedupe_to_one_author(self):
        result = authors.parse("J. R. R. Tolkien, J.R.R. Tolkien")
        assert len(result) == 1
        assert result[0].name == "J. R. R. Tolkien"  # first spelling wins

    @pytest.mark.parametrize("name", [
        "刘慈欣",
        "Лев Толстой",
        "نجيب محفوظ",
    ])
    def test_non_latin_names_parse_to_one_author_with_a_real_key(self, name):
        result = authors.parse(name)
        assert len(result) == 1
        assert result[0].name == name
        assert result[0].name_key != ""

    def test_parse_of_join_names_preserves_order(self):
        names = ["Frank Herbert", "Kevin J. Anderson", "Someone Else"]
        result = authors.parse(authors.join_names(names))
        assert [p.name for p in result] == names
        assert [p.position for p in result] == [0, 1, 2]
