"""Unit tests for the job filters in watch.py.

These exist because the filters got it wrong in a way that mattered: six
German/Austrian postings reached the digest. The city in those postings lived
only in the feed's URL slug, never in a structured location field, and
"Werkstudent" roles were not blocked at all, so they looked remote-friendly.
Both are covered below as regressions.

Run:  python -m pytest tests/ -q      (or: python tests/test_filters.py)
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watch import relevance  # noqa: E402


def job(**kw):
    """A posting that passes every filter unless a test says otherwise."""
    base = {
        "title": "Full Stack Developer",
        "description": "Work with TypeScript, Node.js and React.",
        "company": "Acme",
        "tags": ["typescript", "node", "react"],
        "location": "Remote",
        "date": datetime.now(timezone.utc).isoformat(),
    }
    base.update(kw)
    return base


class TestKeepsRealRoles(unittest.TestCase):
    def test_plain_remote_fullstack_passes(self):
        self.assertGreaterEqual(relevance(job()), 3)

    def test_react_node_typescript_passes(self):
        j = job(title="Full Stack Engineer (React & Node.js)")
        self.assertGreaterEqual(relevance(j), 3)

    def test_fresh_posting_scores_higher_than_stale(self):
        fresh = relevance(job())
        old = relevance(job(date="2020-01-01T00:00:00+00:00"))
        self.assertGreater(fresh, old)

    def test_worldwide_is_not_geo_blocked(self):
        j = job(location="Anywhere in the World")
        self.assertGreaterEqual(relevance(j), 3)


class TestDropsOffStack(unittest.TestCase):
    def test_seniority_filter(self):
        self.assertEqual(relevance(job(title="Senior Full Stack Developer")), 0)

    def test_lead_and_principal(self):
        self.assertEqual(relevance(job(title="Lead Full Stack Developer")), 0)
        self.assertEqual(relevance(job(title="Principal Engineer")), 0)

    def test_unrelated_stack_named_in_title(self):
        # The trap case: advertises one stack he has, inside a posting for another.
        self.assertEqual(relevance(job(title="Web3 Engineer (Rust/Solidity)")), 0)

    def test_no_web_stack_at_all(self):
        self.assertEqual(
            relevance(job(title="Embedded Firmware Engineer", tags=["c", "rtos"])), 0
        )

    def test_tags_can_veto(self):
        j = job(title="Software Engineer", description="TypeScript and Node.js work.",
                tags=["rust", "solidity"])
        self.assertEqual(relevance(j), 0)


class TestDropsGeoLocked(unittest.TestCase):
    def test_region_field_blocks(self):
        self.assertEqual(relevance(job(location="Berlin, Germany")), 0)

    def test_us_state_blocks(self):
        self.assertEqual(relevance(job(location="Austin, TX")), 0)

    def test_country_in_title_blocks_even_with_empty_location(self):
        j = job(title="Frontend Engineer | Singapore", location="")
        self.assertEqual(relevance(j), 0)


class TestGermanLocalPostingsRegressions(unittest.TestCase):
    """The six that leaked on 2026-09-29. All were German and all unreachable."""

    def test_werkstudent_in_title(self):
        self.assertEqual(relevance(job(title="Werkstudent Softwareentwickler (m/w/d)")), 0)

    def test_working_student_in_title(self):
        self.assertEqual(relevance(job(title="Working Student Frontend Developer")), 0)

    def test_praktikant_in_title(self):
        self.assertEqual(relevance(job(title="Praktikant Full Stack Developer")), 0)

    def test_ausbildung_is_blocked(self):
        self.assertEqual(relevance(job(title="Ausbildung Fachinformatiker")), 0)

    def test_mwd_marker_alone_is_enough(self):
        # "(m/w/d)" appears on essentially every DE/AT/CH local posting.
        for suffix in ("(m/w/d)", "(m/w/x)", "(w/m/d)"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    relevance(job(title=f"Software Engineer {suffix}")), 0
                )

    def test_city_only_in_url_slug_still_blocks(self):
        # The real failure mode: "Grünwald" was only ever in the link, never in
        # a structured field, so no location check could ever see it.
        self.assertEqual(
            relevance(job(location="", tags=["typescript", "node"],
                          description="TypeScript and Node.js. Grunwald office.")),
            0,
        )

    def test_grunwald_spelling_variants(self):
        for spelling in ("Grünwald", "Grunwald", "Gruenwald"):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    relevance(job(location="", description=f"TypeScript Node.js {spelling}")),
                    0,
                )

    def test_other_added_cities(self):
        for city in ("Stuttgart", "Köln", "Cologne", "Dresden", "Leipzig", "Ulm"):
            with self.subTest(city=city):
                self.assertEqual(
                    relevance(job(location="", description=f"TypeScript Node.js {city}")),
                    0,
                )

    def test_german_language_marker_still_blocked(self):
        # Pre-existing behaviour that must not regress.
        self.assertEqual(relevance(job(title="Softwareentwickler (m/w/d)")), 0)


class TestGenericTitleNeedsBodyProof(unittest.TestCase):
    def test_generic_title_with_stack_in_body_passes(self):
        j = job(title="Software Engineer",
                description="You will build services in Node.js and TypeScript.")
        self.assertGreaterEqual(relevance(j), 3)

    def test_generic_title_without_stack_in_body_fails(self):
        # Tags count as body evidence (text_of joins them), so they have to go
        # too -- otherwise this test passes for the wrong reason.
        j = job(title="Software Engineer",
                description="You will support a large internal tooling team.",
                tags=[])
        self.assertEqual(relevance(j), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
