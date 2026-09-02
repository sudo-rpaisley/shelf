"""Conservative vinyl/cassette detection regressions."""

import pytest

from app.services.detect import detect_media_type


class TestVinylDetection:
    @pytest.mark.parametrize("title", [
        "Fleetwood Mac - Rumours [Vinyl]",
        "Pink Floyd - Animals (Vinyl)",
        "The Beatles Abbey Road Vinyl LP",
        "Single Title 7-inch Vinyl",
        'Album Title 12" Vinyl',
    ])
    def test_explicit_retail_vinyl_markers_detect_vinyl(self, title):
        d = detect_media_type("upc", "auto", title, None)
        assert d.media_type == "vinyl"
        assert d.signal == "detected"

    @pytest.mark.parametrize("category", [
        "Media > Music & Sound Recordings > Vinyl Records",
        "Music > Vinyl Record",
    ])
    def test_vinyl_record_category_can_decide_without_title_marker(self, category):
        d = detect_media_type("upc", "auto", "Rumours", category)
        assert d.media_type == "vinyl"
        assert d.signal == "detected"

    def test_bare_word_vinyl_does_not_confidently_classify(self):
        # Existing CD-detection adversary: Vinyl is also a film title.
        d = detect_media_type("upc", "auto", "Vinyl", None)
        assert d.media_type == "dvd"
        assert d.signal == "none"

    def test_dvd_bundle_beats_bonus_vinyl(self):
        d = detect_media_type(
            "upc", "auto", "Concert Film [DVD] + Soundtrack Vinyl LP", None
        )
        assert d.media_type == "dvd"
        assert d.signal == "detected"


class TestCassetteDetection:
    @pytest.mark.parametrize("title", [
        "Depeche Mode - Violator [Cassette]",
        "Nirvana Nevermind (Cassette)",
        "Album Title Audio Cassette",
        "Album Title Cassette Tape",
    ])
    def test_explicit_retail_cassette_markers_detect_cassette(self, title):
        d = detect_media_type("upc", "auto", title, None)
        assert d.media_type == "cassette"
        assert d.signal == "detected"

    @pytest.mark.parametrize("category", [
        "Media > Music & Sound Recordings > Music Cassettes",
        "Media > Audio Cassettes",
    ])
    def test_cassette_category_can_decide_without_title_marker(self, category):
        d = detect_media_type("upc", "auto", "Violator", category)
        assert d.media_type == "cassette"
        assert d.signal == "detected"

    def test_dvd_bundle_beats_bonus_cassette(self):
        d = detect_media_type(
            "upc", "auto", "Film Blu-ray + Soundtrack Audio Cassette", None
        )
        assert d.media_type == "dvd"


class TestMusicHints:
    @pytest.mark.parametrize("hint", ["vinyl", "cassette", "digital_music", "music_other"])
    def test_music_hint_survives_when_product_record_has_no_format_signal(self, hint):
        d = detect_media_type("upc", hint, "Untitled Release", "Media > Music")
        assert d.media_type == hint
        assert d.signal == "hinted"
