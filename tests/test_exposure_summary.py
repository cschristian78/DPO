"""Exposure summary ranges and unknown values."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


def labels(numbers, unit=""):
    bins = app._exposure_bins(numbers)
    return [app._exposure_bin_label(start, end, unit) for start, end in bins]


def placed(numbers):
    bins = app._exposure_bins(numbers)
    found = {}
    for number in numbers:
        index = app._exposure_bin_index(number, bins)
        found[number] = None if index is None else app._exposure_bin_label(*bins[index], "")
    return found


def test_stories_one_through_five_keep_each_value():
    assert labels([1, 2, 3, 4, 5]) == ["1", "2", "3", "4", "5"]


def test_stories_one_through_ten_use_five_ranges():
    assert labels(list(range(1, 11))) == ["1–2", "3–4", "5–6", "7–8", "9–10"]
    slots = placed(list(range(1, 11)))
    assert slots[1] == "1–2"
    assert slots[2] == "1–2"
    assert slots[3] == "3–4"
    assert slots[10] == "9–10"


def test_wide_span_uses_five_ranges():
    years = list(range(1950, 2021))
    assert len(labels(years)) == 5
    assert placed([1950, 2020])[1950] != placed([1950, 2020])[2020]


def test_zero_is_unknown_and_not_in_the_span():
    assert app._exposure_number(0) is None
    assert app._exposure_number(None) is None
    assert app._exposure_text(0) == "Unknown"
    assert app._exposure_text("  ") == "Unknown"
    assert app._exposure_number(-4) is None
    assert app._exposure_number(-4, allow_negative=True) == -4


def test_story_bands_match_the_exhibit():
    rows = [
        (None, None, 0, None, None, None, None, None, None, None, 10),
        (None, None, None, None, None, None, None, None, None, None, 5),
        (None, None, 2, None, None, None, None, None, None, None, 20),
        (None, None, 6, None, None, None, None, None, None, None, 40),
        (None, None, 10, None, None, None, None, None, None, None, 80),
        (None, None, 16, None, None, None, None, None, None, None, 100),
    ]
    chart = app._stories_exhibit(rows, 2)
    assert [row["label"] for row in chart["rows"]] == [
        "Unknown",
        "1 to 3 stories—Low-rise",
        "4 to 7 stories—Mid-rise",
        "8 to 14 stories—High-rise",
        "15 or more stories—Tall",
    ]
    assert chart["rows"][0]["risks"] == "2"
    assert chart["rows"][1]["risks"] == "1"
    assert chart["rows"][1]["average"] == "20.0"
    assert chart["total"]["risks"] == "6"
    assert chart["total"]["risk_share"] == "100.0%"


def test_year_built_uses_fixed_bands():
    def row(year, value):
        return (None, None, None, year, None, None, None, None, None, None, value)

    rows = [
        row(0, 10),
        row(9999, 5),
        row(1800, 20),
        row(1937, 30),
        row(1973, 1),
        row(1974, 40),
        row(1988, 1),
        row(1989, 50),
        row(2000, 1),
        row(2001, 60),
        row(2024, 1),
    ]
    chart = app._year_built_exhibit(rows, 3)
    assert [item["label"] for item in chart["rows"]] == [
        "Unknown",
        "Pre-1937",
        "1937-1973",
        "1974-1988",
        "1989-2000",
        "Post-2000",
    ]
    assert [item["risks"] for item in chart["rows"]] == ["2", "1", "2", "2", "2", "2"]
    assert chart["rows"][1]["average"] == "20.0"
    assert chart["total"]["risks"] == "11"
    assert chart["total"]["risk_share"] == "100.0%"
    assert chart["total"]["tiv_share"] == "100.0%"


def test_other_profiles_use_the_same_table():
    rows = [
        ("Frame", "Retail", 1, 1990, 100, 1, 5, "FL", "Miami-Dade", "33101", 100),
        ("Frame", "Retail", 1, 1990, 100, 1, 5, "FL", "Miami-Dade", "33102", 50),
        ("Masonry", "", 0, 0, 0, 0, 0, "LA", "", "0", 25),
    ]
    construction = app._exposure_category_chart("Construction", rows, 0)
    assert construction["kind"] == "table"
    assert construction["rows"][0]["label"] == "Frame"
    assert construction["rows"][0]["risks"] == "2"
    assert construction["rows"][0]["tiv"] == "150"
    assert construction["rows"][0]["average"] == "75.0"
    assert construction["total"]["risk_share"] == "100.0%"
    occupancy = app._exposure_category_chart("Occupancy", rows, 1)
    assert occupancy["rows"][-1]["label"] == "Unknown"
    floor_area = app._exposure_range_chart("Floor area", rows, 4)
    assert floor_area["rows"][0]["label"] == "Unknown"
    assert floor_area["total"]["risks"] == "3"


def test_top_profile_folds_the_rest_into_other():
    rows = [
        (None, None, None, None, None, None, None, "S%d" % index, None, None, 100 - index)
        for index in range(12)
    ]
    chart = app._exposure_category_chart("State", rows, 7, mode="top")
    assert len(chart["rows"]) == 12
    assert chart["rows"][0]["rank"] == 1
    assert chart["other"]["risks"] == "2"
    assert chart["total"]["risks"] == "12"
    assert chart["total"]["tiv_share"] == "100.0%"


def test_year_sentinel_is_unknown():
    assert app._exposure_number(9999, unknown_at=9999) is None
    assert app._exposure_number(1990, unknown_at=9999) == 1990


def test_fractional_values_use_five_ranges():
    bins = app._exposure_bins([0.5, 2, 4, 7, 9, 10])
    assert len(bins) == 5
    assert app._exposure_bin_index(0.5, bins) == 0
    assert app._exposure_bin_index(10, bins) == 4


if __name__ == "__main__":
    test_stories_one_through_five_keep_each_value()
    test_stories_one_through_ten_use_five_ranges()
    test_wide_span_uses_five_ranges()
    test_zero_is_unknown_and_not_in_the_span()
    test_story_bands_match_the_exhibit()
    test_year_built_uses_fixed_bands()
    test_other_profiles_use_the_same_table()
    test_top_profile_folds_the_rest_into_other()
    test_year_sentinel_is_unknown()
    test_fractional_values_use_five_ranges()
    print("ok")
