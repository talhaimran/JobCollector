import logging

import pytest

from collector import settings

PROFILE = """# Candidate
Some resume text. Target titles: should be ignored

Job Search Settings
Target titles:   Project Manager ,Senior Project Manager,,
EXCLUDE TITLES: Technical Project Manager, Junior
Locations: Kitchener ON, Waterloo ON
Remote in: Canada
Work model: Remote, Hybrid
Employment type:
Salary floor (CAD/year):   85,000
Must-have keywords: Jira, AI/ML, Monday.com
Dealbreaker keywords: French required
Excluded companies:
Target companies: Acme | Greenhouse | acme ; Bad | workday | x; Beta|lever|beta
Base resume: master-resume.md
Email cutoff (pre-score): 60
Favourite colour: blue

## Other section
Locations: Toronto ON
"""


def test_parses_values_and_odd_spacing():
    s = settings.parse(PROFILE)
    assert s.target_titles == ["Project Manager", "Senior Project Manager"]
    assert s.exclude_titles == ["Technical Project Manager", "Junior"]
    assert s.locations == ["Kitchener ON", "Waterloo ON"]  # stops at next heading
    assert s.salary_floor == 85000
    assert s.email_cutoff == 60
    assert s.must_have == ["Jira", "AI/ML", "Monday.com"]
    assert s.base_resume == "master-resume.md"


def test_blank_fields_are_off():
    s = settings.parse(PROFILE)
    assert s.employment_type == [] and s.excluded_companies == []
    assert set(s.filters_off()) == {"employment type", "excluded companies"}


def test_target_companies_and_bad_entries_warn(caplog):
    with caplog.at_level(logging.WARNING):
        s = settings.parse(PROFILE)
    assert [(c.name, c.system, c.board) for c in s.target_companies] == [
        ("Acme", "greenhouse", "acme"), ("Beta", "lever", "beta")]
    assert "Bad" in caplog.text and "Favourite colour" in caplog.text


def test_hashed_heading_and_missing_settings():
    s = settings.parse("## Job Search Settings:\nTarget titles: PM\n")
    assert s.target_titles == ["PM"] and s.salary_floor is None and s.email_cutoff is None
    assert "salary floor" in s.filters_off() and "email cutoff" in s.filters_off()


def test_missing_section_raises():
    with pytest.raises(ValueError):
        settings.parse("# nothing here")


def test_bad_number_warns(caplog):
    s = settings.parse("Job Search Settings\nSalary floor (CAD/year): lots\n")
    assert s.salary_floor is None and "not a number" in caplog.text
