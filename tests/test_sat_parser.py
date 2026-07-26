"""The Avaya SAT terminal-form parser.

These fixtures reproduce the column-aligned layout of real CM screens. The
parser must survive values that contain spaces, commas, and words that look like
labels, because that is where a per-field regex silently returns the wrong
coverage path.
"""

from __future__ import annotations

import pytest

from ucm_bridge.vendor.sat import (
    SatParseError,
    parse_sat_form,
    parse_sat_table,
)

STATION_SCREEN = """\
display station 5101                                            Page   1 of   5
                                     STATION

Extension: 5101                          Lock Messages? n               BCC: 0
     Type: 9611                          Security Code: *                TN: 1
     Port: S00012                        Coverage Path 1: 12            COR: 1
     Name: Mueller, Anna                 Coverage Path 2:                COS: 1
                                         Hunt-to Station:

                                 FEATURE OPTIONS

  LWC Reception: spe                     Auto Select Any Idle Appearance? n
 LWC Log External Calls? n               Coverage Msg Retrieval? y
        CDR Privacy? n                   Direct IP-IP Audio Connections? y
"""

STATION_PAGE_FOUR = """\
display station 5101                                            Page   4 of   5
                                     STATION
                                SITE DATA
      Room: 3-114                        Headset? n
      Jack:                              Speaker? n
     Cable:                              Mounting: d
      Floor: 3                           Cord Length: 0
   Building: Leopoldstrasse 7            Set Color:
"""

LIST_STATION = """\
list station

                                    STATIONS

Ext        Port      Name                      Room        Cv1  Cv2  COR  COS
5101       S00012    Mueller, Anna             3-114       12        1    1
5102       S00013    Schmidt, Bruno            3-116       12        1    1
7301       S00104    Jones, Cerys              1-002                 2    1
5900       S00250    Warehouse Handset                               3    1
"""

COVERAGE_PATH = """\
display coverage path 12                                        Page   1 of   1
                              COVERAGE PATH

                  Coverage Path Number: 12
                    Hunt after Coverage? n
                            Next Path Number:      Linkage

COVERAGE CRITERIA

     Station/Group Status         Inside Call        Outside Call
              Active?                  n                  n
                Busy?                  y                  y
       Don't Answer?                   y                  y            Number of Rings: 3
                 All?                  n                  n
"""


# --------------------------------------------------------------------------- #
# Form parsing
# --------------------------------------------------------------------------- #


def test_parses_labelled_fields_across_columns() -> None:
    form = parse_sat_form(STATION_SCREEN)

    assert form.command == "display station 5101"
    assert form.title == "STATION"
    assert form.page_count == 5

    assert form.get("Extension") == "5101"
    assert form.get("Type") == "9611"
    assert form.get("Port") == "S00012"
    assert form.get("COR") == "1"
    assert form.get("TN") == "1"


def test_a_value_containing_a_comma_and_spaces_is_not_truncated() -> None:
    """'Mueller, Anna' must survive; this is where naive splitting fails."""
    form = parse_sat_form(STATION_SCREEN)
    assert form.get("Name") == "Mueller, Anna"


def test_the_field_to_the_right_of_a_long_value_is_still_found() -> None:
    form = parse_sat_form(STATION_SCREEN)
    assert form.get("Coverage Path 1") == "12"
    assert form.get("Coverage Path 2") == ""
    assert form.get("COS") == "1"


def test_boolean_fields_use_the_question_mark_terminator() -> None:
    form = parse_sat_form(STATION_SCREEN)
    assert form.get_bool("Lock Messages") is False
    assert form.get_bool("Coverage Msg Retrieval") is True
    assert form.get_bool("Direct IP-IP Audio Connections") is True
    # A non-boolean field returns the default rather than guessing.
    assert form.get_bool("Extension") is None


def test_section_headings_are_tracked() -> None:
    form = parse_sat_form(STATION_SCREEN)
    assert "FEATURE OPTIONS" in form.sections
    labels = {f.label for f in form.fields_in_section("FEATURE OPTIONS")}
    assert "LWC Reception" in labels
    assert "Extension" not in labels


def test_integers_are_converted_only_when_they_are_integers() -> None:
    form = parse_sat_form(STATION_SCREEN)
    assert form.get_int("Extension") == 5101
    assert form.get_int("Port") is None
    assert form.get_int("Nonexistent") is None


def test_unmapped_fields_are_reportable_for_the_fidelity_assessment() -> None:
    form = parse_sat_form(STATION_SCREEN)
    leftover = form.unmapped({"Extension", "Type", "Port", "Name", "COR", "COS", "TN"})
    assert "Coverage Path 1" in leftover
    assert "Extension" not in leftover


def test_a_multi_page_form_merges_its_pages() -> None:
    form = parse_sat_form(STATION_SCREEN + "\n" + STATION_PAGE_FOUR)
    assert form.get("Extension") == "5101"      # page 1
    assert form.get("Room") == "3-114"          # page 4
    assert form.get("Building") == "Leopoldstrasse 7"
    assert {f.page for f in form.fields} == {1, 4}


def test_coverage_criteria_grid_is_parsed() -> None:
    form = parse_sat_form(COVERAGE_PATH)
    assert form.get("Coverage Path Number") == "12"
    assert form.get_bool("Hunt after Coverage") is False
    assert form.get_int("Number of Rings") == 3


def test_a_two_column_boolean_grid_reads_the_inside_call_flag() -> None:
    """'Busy?  y   y' is inside-call and outside-call, not one value."""
    form = parse_sat_form(COVERAGE_PATH)

    assert form.get_bool("Busy") is True
    assert form.get_bool("Don't Answer") is True
    assert form.get_bool("Active") is False
    assert form.get_bool("All") is False

    busy = next(f for f in form.fields if f.label == "Busy")
    assert busy.columns == ["y", "y"], "both columns stay available to callers that need them"


def test_empty_output_fails_loudly() -> None:
    with pytest.raises(SatParseError, match="Empty SAT output"):
        parse_sat_form("   \n  \n")


def test_output_with_no_fields_fails_loudly_rather_than_returning_nothing() -> None:
    with pytest.raises(SatParseError, match="No fields were found"):
        parse_sat_form("SOME BANNER TEXT\nMORE BANNER TEXT\n")


# --------------------------------------------------------------------------- #
# Table parsing
# --------------------------------------------------------------------------- #


def test_list_output_is_parsed_by_column_position() -> None:
    table = parse_sat_table(LIST_STATION)

    assert table.title == "STATIONS"
    assert table.columns[:4] == ["Ext", "Port", "Name", "Room"]
    assert len(table) == 4

    first = table.rows[0]
    assert first["Ext"] == "5101"
    assert first["Name"] == "Mueller, Anna"
    assert first["Room"] == "3-114"
    assert first["Cv1"] == "12"


def test_blank_cells_stay_blank_and_do_not_shift_columns() -> None:
    """The row with no coverage path must not pull COR into the Cv1 column."""
    table = parse_sat_table(LIST_STATION)
    warehouse = next(row for row in table.rows if row["Ext"] == "5900")

    assert warehouse["Name"] == "Warehouse Handset"
    assert warehouse["Room"] == ""
    assert warehouse["Cv1"] == ""
    assert warehouse["COR"] == "3"


def test_a_table_without_a_header_fails_loudly() -> None:
    with pytest.raises(SatParseError, match="No column header row found"):
        parse_sat_table("5101 S00012 Mueller\n")
