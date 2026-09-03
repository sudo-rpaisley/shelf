from app.services import periodicals


# Popular Science print ISSN 0161-7370 encoded with serial variant 00.
POPULAR_SCIENCE_EAN = "9770161737008"


def test_issn_check_character_is_reconstructed_from_977_stem():
    assert periodicals.issn_from_seven_digits("0161737") == "0161-7370"


def test_issn_check_character_can_be_x():
    assert periodicals.issn_from_seven_digits("1000002") == "1000-002X"


def test_valid_977_ean_decodes_to_periodical_identity():
    serial = periodicals.parse_barcode(POPULAR_SCIENCE_EAN)

    assert serial is not None
    assert serial.ean13 == POPULAR_SCIENCE_EAN
    assert serial.issn == "0161-7370"
    assert serial.variant == "00"


def test_invalid_ean_check_digit_is_not_treated_as_a_periodical():
    assert periodicals.parse_barcode("9770161737009") is None


def test_non_977_ean_is_not_treated_as_a_periodical():
    assert periodicals.parse_barcode("4006381333931") is None


def test_supplemental_digits_are_not_silently_folded_into_the_main_barcode():
    # The browser scanner currently reports the carrier symbol separately; a
    # future supplemental-code feature must model that explicitly rather than
    # accidentally accepting a 15/18-digit concatenation here.
    assert periodicals.parse_barcode(POPULAR_SCIENCE_EAN + "05") is None
