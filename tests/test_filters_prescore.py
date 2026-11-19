import pytest

from collector import filters, prescore
from collector.filters import check, yearly_salary
from collector.settings import Settings


@pytest.fixture
def s():
    return Settings(
        target_titles=["Project Manager", "Senior Project Manager"],
        exclude_titles=["Technical Project Manager", "Junior", "Program Manager"],
        locations=["Kitchener ON", "Waterloo ON", "Cambridge ON"],
        remote_in=["Canada"], work_model=["Remote", "Hybrid"],
        employment_type=["Full-time", "Contract"], salary_floor=80000,
        must_have=["Jira", "Agile", "SaaS", "PMP", "risk management"],
        dealbreakers=["French required", "US citizens only"],
        excluded_companies=["Evil Corp"])


def job(**kw):
    base = {"title": "Project Manager", "company": "Acme", "location": "Kitchener, ON, Canada",
            "work_model": "hybrid", "employment_type": "full-time", "description": ""}
    base.update(kw)
    return base


def reason(j, s):
    return check(j, s)[1]


def test_passes(s):
    assert check(job(), s) == (True, None)


def test_title_rules(s):
    assert reason(job(title="Senior Technical Project Manager"), s) == "excluded title: Technical Project Manager"
    assert reason(job(title="Junior Project Manager"), s) == "excluded title: Junior"
    assert reason(job(title="Product Manager"), s) == "title not in target titles"
    assert reason(job(title="Project Managers Lead"), s) == "title not in target titles"  # whole word
    assert check(job(title="Lead Project Manager, Insurance"), s)[0]


def test_excluded_company_ignores_suffix(s):
    assert reason(job(company="Evil Corp Inc."), s).startswith("excluded company")


def test_location_and_work_model(s):
    assert check(job(work_model="remote", location="Remote, Canada"), s)[0]
    assert check(job(work_model="remote", location="Remote"), s)[0]       # no country restriction
    assert check(job(work_model="remote", location="Toronto, ON"), s)[0]  # remote anywhere in Canada
    assert reason(job(work_model="remote", location="United States"), s) == "remote restricted to united states"
    assert reason(job(work_model="remote", location="Austin, TX"), s) == "remote restricted to united states"
    assert reason(job(location="Toronto, ON, Canada"), s).startswith("location not matched (hybrid)")
    assert reason(job(location="Waterloo, IA"), s).startswith("location not matched")
    assert reason(job(work_model="on-site"), s) == "work model on-site not wanted"
    assert check(job(work_model="unknown", location="Cambridge, Ontario"), s)[0]
    assert reason(job(work_model="unknown", location="Toronto, ON"), s).startswith("location not matched (unknown)")


def test_detect_country():
    assert filters.detect_country("San Francisco, CA") == "united states"
    assert filters.detect_country("Kitchener, ON, CA") == "canada"
    assert filters.detect_country("London, UK") == "united kingdom"
    assert filters.detect_country("Anywhere") is None


def test_employment_type(s):
    assert check(job(employment_type="contract"), s)[0]
    assert check(job(employment_type=None), s)[0]
    assert reason(job(employment_type="part-time"), s) == "employment type: part-time"


def test_salary(s):
    assert yearly_salary(40, "hourly") == 83200
    assert yearly_salary(6000, "monthly") == 72000
    assert check(job(salary_min=40, salary_interval="hourly", salary_currency="CAD"), s)[0]
    assert reason(job(salary_max=6000, salary_interval="monthly"), s) == "salary below floor: 72,000 CAD/year"
    assert check(job(salary_max=50000, salary_currency="USD"), s)[0]  # not converted
    assert check(job(), s)[0]                                         # unknown passes


def test_dealbreakers(s):
    assert reason(job(description="Fluency: French Required."), s) == "dealbreaker: French required"
    assert reason(job(description="US  citizens\nonly"), s) == "dealbreaker: US citizens only"


def test_filter_order_records_first_reason(s):
    j = job(company="Evil Corp", title="Junior PM", description="French required")
    assert reason(j, s).startswith("excluded company")


def test_prescore_maths(s, monkeypatch):
    j = job(title="Senior Project Manager", description="Jira, agile, SaaS, jira again")
    r = prescore.score(j, s)
    assert r["matched_keywords"] == ["Jira", "Agile", "SaaS"]
    assert r["prescore_keywords"] == 30.0 and r["prescore_title"] == 100
    assert r["prescore_total"] == pytest.approx(30 * 0.7 + 100 * 0.3)
    assert prescore.score(job(title="Lead Project Manager"), s)["prescore_title"] == 70
    monkeypatch.setattr("collector.config.KEYWORD_FULL_MARKS", 2)
    assert prescore.score(j, s)["prescore_keywords"] == 100.0  # capped
    s.must_have = []
    assert prescore.score(j, s)["prescore_total"] == 100
