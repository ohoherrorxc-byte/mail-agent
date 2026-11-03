"""对照表《国家清单-大区分类对照表.xlsx》→ Region。"""
from __future__ import annotations

from agent.region_catalog import apply_to_triage, lookup_region, refine_region
from agent.schemas import TriageResult


def _triage(region: str, country: str | None = None) -> TriageResult:
    return TriageResult(
        intent="other",
        urgency="P2",
        region=region,
        language="en",
        country=country,
        requires_recall_check=False,
        requires_warranty_lookup=False,
        red_flags=[],
    )


def test_xlsx_overrides_llm_guesses():
    cases = [
        ("Israel", "Middle East"),
        ("IL", "Middle East"),
        ("Saudi Arabia", "Middle East"),
        ("UAE", "Middle East"),
        ("Mexico", "South America"),
        ("MX", "South America"),
        ("Russia", "Europe"),
        ("Turkey", "Europe"),
        ("Thailand", "Asia"),
        ("United States", "North America"),
        ("Canada", "North America"),
        ("United Kingdom", "Europe"),
        ("GB", "Europe"),
        ("Australia", "Oceania"),
        ("South Africa", "Africa"),
        ("以色列", "Middle East"),
        ("墨西哥", "South America"),
        ("俄罗斯", "Europe"),
    ]
    for raw, want in cases:
        hit = lookup_region(raw)
        assert hit is not None, raw
        assert hit.region == want, (raw, hit.region, want)


def test_does_not_treat_mg_as_madagascar_or_na_as_namibia():
    assert lookup_region("MG") is None
    assert lookup_region("NA") is None


def test_refine_from_web_form_country_field():
    body = "Name: Dan\nCountry/Region: Israel\nVIN: LSJW74099PZ099787\n"
    region, country = refine_region("Europe", None, body)
    assert region == "Middle East"
    assert country == "Israel"


def test_apply_to_triage_fixes_israel_and_keeps_uk_as_europe():
    t = apply_to_triage(_triage("Europe", "Israel"), "")
    assert t.region == "Middle East"
    assert t.country == "Israel"

    t2 = apply_to_triage(_triage("Europe", "GB"), "")
    assert t2.region == "Europe"
    assert t2.country == "GB"


def test_keyword_classify_reads_country_from_body():
    from agent.template_reply import classify_intent

    class _NoLLM:
        pass

    body = "Country/Region: Mexico\nThe app will not bind.\n"
    t = classify_intent(body, llm=_NoLLM(), use_llm=False)
    assert t.region == "South America"
    assert t.country == "Mexico"
