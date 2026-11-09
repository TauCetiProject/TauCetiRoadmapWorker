"""Library near-matches: declarations and docstrings are read from a Lean file, Loogle is asked for the
constants of each conclusion (three, then two, then one), LeanSearch for the docstring, and the report
marks a same-name hit; a failing service is a note, never an exception."""

import json

from tauceti_roadmap_worker import libsearch

LEAN = '''import Mathlib

/-- The product of two modules is flat iff both are. -/
theorem Module.Flat.prod_iff {R M N : Type*} [CommSemiring R] [AddCommMonoid M] [Module R M]
    [AddCommMonoid N] [Module R N] : Module.Flat R (M × N) ↔ Module.Flat R M ∧ Module.Flat R N := by
  sorry

theorem Polynomial.isCoprime_X_sub_C_iff {R : Type*} [CommRing R] {a : R} {q : R[X]} :
    IsCoprime (X - C a) q ↔ IsUnit (q.eval a) := by
  sorry

noncomputable def foo (n : ℕ) : ℕ := n
'''


def test_declarations_statements_and_docstrings():
    ds = libsearch.declarations(LEAN)
    assert [(d.kind, d.name) for d in ds] == [("theorem", "Module.Flat.prod_iff"),
                                               ("theorem", "Polynomial.isCoprime_X_sub_C_iff"), ("def", "foo")]
    assert ds[0].docstring == "The product of two modules is flat iff both are."
    assert libsearch.conclusion(ds[0].statement) == "Module.Flat R (M × N) ↔ Module.Flat R M ∧ Module.Flat R N"
    assert libsearch.conclusion(ds[1].statement) == "IsCoprime (X - C a) q ↔ IsUnit (q.eval a)"
    assert libsearch.loogle_queries(ds[0]) == ["Module.Flat"]          # `prod` is a lemma-name stop word
    assert libsearch.loogle_queries(ds[1]) == ["Polynomial.isCoprime", "Polynomial.isCoprime, IsCoprime", "IsCoprime, IsUnit"]
    assert libsearch.leansearch_query(ds[0]) == "The product of two modules is flat iff both are."
    assert libsearch.leansearch_query(ds[2]).startswith("foo: ℕ")


def fake_fetch(url, data=None):
    if url.startswith(libsearch.LOOGLE):
        if "IsCoprime" in url:
            return json.dumps({"hits": [{"name": "Polynomial.isCoprime_X_sub_C_iff", "module": "Mathlib.Algebra.Polynomial.Roots",
                                         "type": "IsCoprime (X - C a) q ↔ q.eval a ≠ 0"}]})
        if "Module.Flat" in url:
            return json.dumps({"hits": [{"name": "Module.Flat.prod", "module": "Mathlib.RingTheory.Flat.Basic", "type": "Flat R (M × N)"}]})
        return json.dumps({"hits": []})
    if url == libsearch.LEANSEARCH:
        q = json.loads(data)["query"][0]
        if "flat" in q:
            return json.dumps([[{"result": {"name": ["Module", "Flat", "prod"], "module_name": ["Mathlib", "RingTheory", "Flat", "Basic"],
                                            "signature": "Flat R (M × N)"}}]])
        raise OSError("leansearch down")
    raise AssertionError(url)


def test_search_and_report_with_a_fake_network():
    found, note = libsearch.search(LEAN, fetch=fake_fetch)
    assert note == "" and [f.decl.name for f in found] == ["Module.Flat.prod_iff", "Polynomial.isCoprime_X_sub_C_iff", "foo"]
    flat, coprime, foo = found
    assert flat.loogle_query == "Module.Flat" and [h.name for h in flat.loogle] == ["Module.Flat.prod"]
    assert [h.name for h in flat.leansearch] == ["Module.Flat.prod"] and flat.leansearch[0].module == "Mathlib.RingTheory.Flat.Basic"
    assert coprime.loogle_query == "Polynomial.isCoprime, IsCoprime" and coprime.same_name == coprime.loogle
    assert coprime.errors == ["LeanSearch: OSError: leansearch down"]
    text, strong = libsearch.report(found, note, "TauCetiRoadmap/X/Suggested.lean")
    assert strong == 1 and "## `Polynomial.isCoprime_X_sub_C_iff` — **same-name hit**" in text
    assert "- Loogle `Module.Flat`:\n  - `Module.Flat.prod` (Mathlib.RingTheory.Flat.Basic): `Flat R (M × N)`" in text
    assert "- not searched: LeanSearch: OSError: leansearch down" in text
    assert "Leads, not verdicts" in text


def test_the_time_budget_stops_the_pass():
    clock = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0]).__next__
    found, note = libsearch.search(LEAN, fetch=fake_fetch, clock=clock, budget=10)
    assert len(found) == 1 and note.startswith("time budget spent after 1 of 3 declarations")
