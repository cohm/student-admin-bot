"""Tests for `bot/kth_study_plan.py`, the study-plan cache the weekly scrape
fills. `_fetch_html` is the single network choke point and is always mocked."""

from __future__ import annotations

import json
import time
from urllib.parse import quote

import pytest

from student_bot.bot import kth_study_plan as ksp
from student_bot.bot.web_cache import CachedPage, WebCache
from student_bot.config import get_config


def _programme_html(participations: dict) -> str:
    store = {"curriculumInfos": [{"participations": participations}]}
    return f'<script>window.__compressedApplicationStore__ = "{quote(json.dumps(store))}";</script>'


def _course_html(*versions: tuple[int, int, str]) -> str:
    store = {
        "courseData": {
            "syllabusList": [
                {
                    "course_valid_from": {"year": y, "semesterNumber": n},
                    "course_eligibility": text,
                }
                for y, n, text in versions
            ]
        }
    }
    return f'<script>window.__compressedData__DATA = "{quote(json.dumps(store))}";</script>'


def _part(code: str, cpp: list | None = None) -> dict:
    return {"course": {"courseCode": code, "title": f" {code} name "}, "creditsPerPeriod": cpp}


# --- parsing ------------------------------------------------------------------


def test_parse_programme_year_reads_courses_and_first_period():
    html = _programme_html(
        {
            "O": [_part("SF1673", [0, 0, "3,5", 4, 0, 0])],
            "VV": [_part("SK1104", [0, 0, 0, 0, 0, 0])],
            "X": [_part("AB1234")],
        }
    )
    assert ksp.parse_programme_year(html) == [
        {"code": "SF1673", "name": "SF1673 name", "condition": "O", "first_period": 2},
        {"code": "SK1104", "name": "SK1104 name", "condition": "VV", "first_period": None},
    ]


def test_parse_programme_year_tells_empty_from_unreadable():
    # KTH stops listing past years: a readable page with no courses.
    assert ksp.parse_programme_year(_programme_html({})) == []
    assert ksp.parse_programme_year("<html>no blob</html>") is None


def test_parse_programme_year_term_without_admission_is_empty_not_unreadable():
    """KTH answers HTTP 200 with its own `statusCode` 404 and no curriculum
    (CLMDA 20242, checked 2026-09-24). As a gap it would turn every weekly run
    yellow once a configured programme skips an intake."""
    store = {"statusCode": 404, "curriculumInfos": None}
    html = f'<script>window.__compressedApplicationStore__ = "{quote(json.dumps(store))}";</script>'
    assert ksp.parse_programme_year(html) == []


def test_parse_course_page_newest_version_first_as_plain_text():
    html = _course_html(
        (2017, 2, "<p>SF1673 och SF1674.</p>"), (2019, 2, "<p>Slutförd SF1674.</p>")
    )
    assert ksp.parse_course_page(html) == [
        {"term": 20192, "eligibility": "Slutförd SF1674."},
        {"term": 20172, "eligibility": "SF1673 och SF1674."},
    ]


def test_parse_course_page_none_when_nothing_decodes():
    """A renamed blob must not read as "no requirement"."""
    assert ksp.parse_course_page("<html>no blob</html>") is None
    assert ksp.parse_course_page(_course_html()) is None


# --- the warm -------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path):
    cfg = get_config().model_copy(deep=True)
    cfg.dynamic_web.cache_db = str(tmp_path / "web_cache.sqlite")
    sc = cfg.study_plan_cache
    sc.enabled, sc.programmes, sc.cohorts, sc.years_per_programme = True, ["CTFYS"], 1, 2
    return cfg


@pytest.fixture
def kth(monkeypatch):
    """A fake kth.se: every URL fetched, and pages keyed by URL suffix."""
    pages = {
        "arskurs1": _programme_html({"O": [_part("SF1673", [0, 1, 0, 0, 0, 0])]}),
        "arskurs2": _programme_html({"O": [_part("SF1683", [0, 0, 0, 1, 0, 0])]}),
        "SF1673": _course_html((2020, 2, "Grundläggande behörighet.")),
        "SF1683": _course_html((2019, 2, "Slutförd kurs SF1674.")),
    }
    fetched: list[str] = []

    def _fake_fetch_html(url, _cfg, *, timeout=None):
        assert timeout == _cfg.study_plan_cache.fetch_timeout_seconds
        fetched.append(url)
        page = pages[url.rsplit("/", 1)[-1]]
        if isinstance(page, Exception):
            raise page
        return url, page

    monkeypatch.setattr(ksp, "_fetch_html", _fake_fetch_html)
    return pages, fetched


def _cached(cfg, url: str):
    page = WebCache(cfg).get(ksp.cache_key(url))
    return json.loads(page.content) if page else None


def test_warm_caches_year_pages_and_the_kursplaner_they_name(cfg, kth):
    report = ksp.warm_study_plans(cfg)
    assert (report.programme_pages, report.course_pages, report.gaps) == (2, 2, [])

    cohort = ksp.latest_cohort(time.localtime())
    year2 = _cached(cfg, ksp.programme_year_url("CTFYS", cohort, 2))
    assert [c["code"] for c in year2] == ["SF1683"]
    course = _cached(cfg, ksp.COURSE_PAGE_URL.format(code="SF1683"))
    assert course == [{"term": 20192, "eligibility": "Slutförd kurs SF1674."}]


def test_warm_leaves_the_dynamic_web_entry_for_the_same_url_alone(cfg, kth):
    url = ksp.COURSE_PAGE_URL.format(code="SF1683")
    WebCache(cfg).put(CachedPage(url=url, title="SF1683", content="sanitized text", fetched_at=1))
    ksp.warm_study_plans(cfg)
    assert WebCache(cfg).get(url).content == "sanitized text"


def test_every_run_refetches_every_page(cfg, kth):
    """No freshness check: with a TTL near the weekly interval, a run that
    starts a minute earlier than last week's would skip pages silently."""
    _, fetched = kth
    ksp.warm_study_plans(cfg)
    first = sorted(fetched)
    fetched.clear()
    ksp.warm_study_plans(cfg)
    assert sorted(fetched) == first and len(first) == 4


def test_failed_refetch_keeps_the_older_entry_and_reports_a_gap(cfg, kth):
    pages, _ = kth
    ksp.warm_study_plans(cfg)
    url = ksp.COURSE_PAGE_URL.format(code="SF1683")

    pages["SF1683"] = TimeoutError("kth.se unreachable")
    report = ksp.warm_study_plans(cfg)
    assert report.gaps == ["1 kursplaner unreadable: SF1683"]
    assert _cached(cfg, url) == [{"term": 20192, "eligibility": "Slutförd kurs SF1674."}]


def test_unreadable_year_page_is_a_gap(cfg, kth):
    pages, _ = kth
    pages["arskurs1"] = "<html>blob renamed</html>"
    pages["arskurs2"] = _programme_html({})
    report = ksp.warm_study_plans(cfg)
    # Not also "no courses in any cohort": the page was unreadable, not empty.
    assert report.gaps == [f"CTFYS HT{ksp.latest_cohort(time.localtime())} åk1"]


def test_programme_readable_but_empty_everywhere_is_a_gap(cfg, kth):
    """A discontinued or misspelled programme code."""
    pages, _ = kth
    pages["arskurs1"] = pages["arskurs2"] = _programme_html({})
    assert ksp.warm_study_plans(cfg).gaps == ["CTFYS: no courses in any cohort"]


def test_kth_down_skips_the_kursplaner(cfg, kth):
    """At 30 s a timeout, 500 kursplaner behind an unreachable kth.se would add
    an hour to the run and hammer a host that is already struggling."""
    pages, fetched = kth
    ksp.warm_study_plans(cfg)  # older entries that would name kursplaner
    fetched.clear()
    pages["arskurs1"] = pages["arskurs2"] = TimeoutError("kth.se unreachable")
    report = ksp.warm_study_plans(cfg)
    assert report.gaps[-1] == "no programme page readable: kursplaner not refreshed"
    assert not any("/kurs/" in u for u in fetched)


def test_no_programmes_configured_is_not_an_outage(cfg, kth):
    cfg.study_plan_cache.programmes = []
    assert ksp.warm_study_plans(cfg) == ksp.WarmReport()


def test_an_older_year_page_still_names_the_kursplaner_to_refresh(cfg, kth):
    """The reason an unreadable page keeps its older entry: its courses still
    get their kursplaner refreshed."""
    pages, fetched = kth
    ksp.warm_study_plans(cfg)
    fetched.clear()
    pages["arskurs2"] = TimeoutError("kth.se unreachable")
    ksp.warm_study_plans(cfg)
    assert ksp.COURSE_PAGE_URL.format(code="SF1683") in fetched


@pytest.mark.parametrize("month,latest", [(1, 2026), (7, 2026), (8, 2027), (12, 2027)])
def test_latest_cohort_is_the_last_autumn_intake(month, latest):
    now = time.struct_time((2027, month, 15, 0, 0, 0, 0, 0, -1))
    assert ksp.latest_cohort(now) == latest


def test_warm_covers_the_configured_number_of_cohorts(cfg, kth):
    _, fetched = kth
    cfg.study_plan_cache.cohorts = 3
    ksp.warm_study_plans(cfg)
    latest = ksp.latest_cohort(time.localtime())
    terms = {u.split("/")[-2] for u in fetched if "arskurs" in u}
    assert terms == {f"{y}2" for y in (latest - 2, latest - 1, latest)}


def test_a_reshaped_page_is_one_gap_not_an_aborted_warm(cfg, kth):
    pages, _ = kth
    store = {"curriculumInfos": [{"participations": {"O": ["not a dict"]}}]}
    pages["arskurs1"] = (
        f'<script>window.__compressedApplicationStore__ = "{quote(json.dumps(store))}";</script>'
    )
    report = ksp.warm_study_plans(cfg)
    assert report.gaps == [f"CTFYS HT{ksp.latest_cohort(time.localtime())} åk1"]
    assert report.course_pages == 1  # SF1683 from åk2 still cached


def test_warm_never_raises(cfg, monkeypatch):
    """`maintain.sh` aborts before the reindex on a failed scrape."""
    monkeypatch.setattr(ksp, "_warm", lambda _cfg: 1 / 0)
    assert ksp.warm_study_plans(cfg).gaps == ["warm aborted: division by zero"]


def test_disabled_warm_fetches_nothing(cfg, kth):
    cfg.study_plan_cache.enabled = False
    assert ksp.warm_study_plans(cfg) == ksp.WarmReport()
    assert kth[1] == []


def test_scrape_prints_the_gap_line_maintain_sh_greps_for(cfg, monkeypatch):
    from click.testing import CliRunner

    from scripts import fetch_url_corpus

    monkeypatch.setattr(fetch_url_corpus, "get_config", lambda: cfg)
    monkeypatch.setattr(ksp, "_warm", lambda _cfg: ksp.WarmReport(gaps=["a", "b"]))
    result = CliRunner().invoke(fetch_url_corpus.main, ["--warm-only"])
    assert result.exit_code == 0
    assert "\nWARM GAPS: a; b\n" in result.output
