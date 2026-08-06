"""
Statistical Hypothesis Test & Lift Analysis for Tomba Enrichment.

Evaluates whether adding Tomba Domain Search API enrichment produces a
statistically significant increase in Decision Maker leads per business
compared to standard website crawling alone.

Usage:
    python scripts/analyze_tomba_lift.py
    python scripts/analyze_tomba_lift.py --db-path database/hvac_leads.db --min-score 50
"""

import argparse
import math
import os
import re
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

load_dotenv()

GENERIC_LOCALPARTS = {
    "info", "contact", "sales", "office", "support", "admin", "help",
    "billing", "service", "hello", "mail", "inquiries", "jobs", "careers",
    "team", "general", "webmaster", "privacy", "terms", "marketing",
}

DECISION_MAKER_KEYWORDS = (
    r"owner", r"founder", r"ceo", r"president", r"vp", r"vice president",
    r"manager", r"director", r"chief", r"partner", r"principal",
    r"superintendent", r"estimator", r"project manager", r"operations",
    r"executive", r"decision maker", r"head", r"lead", r"master",
)
_DM_TITLE_RE = re.compile("|".join(DECISION_MAKER_KEYWORDS), re.IGNORECASE)


def is_decision_maker(name: Optional[str], title: Optional[str], email: Optional[str]) -> bool:
    """Classify if a contact is a decision maker vs generic info@ address."""
    if not email or "@" not in email:
        return False

    localpart = email.split("@")[0].lower()
    if localpart in GENERIC_LOCALPARTS:
        return False

    if title and _DM_TITLE_RE.search(title):
        return True

    if name and name.strip() and name.strip().lower() not in ("info/office", "general contact", "decision maker"):
        return True

    # If title is explicitly generic, reject
    if title and title.strip().lower() == "general contact":
        return False

    # Default to True if email localpart looks like a person's name (contains dot/underscore or non-generic)
    return True


def calculate_welch_ttest(
    sample1: List[float], sample2: List[float]
) -> Tuple[float, float, float]:
    """
    Calculate Welch's t-statistic, degrees of freedom, and approx p-value.
    sample1 = control (Web crawl only)
    sample2 = treatment (Tomba enriched)
    """
    n1, n2 = len(sample1), len(sample2)
    if n1 < 2 or n2 < 2:
        return 0.0, 0.0, 1.0

    mean1 = sum(sample1) / n1
    mean2 = sum(sample2) / n2

    var1 = sum((x - mean1) ** 2 for x in sample1) / (n1 - 1)
    var2 = sum((x - mean2) ** 2 for x in sample2) / (n2 - 1)

    se1 = var1 / n1
    se2 = var2 / n2

    se_diff = math.sqrt(se1 + se2)
    if se_diff == 0:
        return 0.0, 0.0, 1.0

    t_stat = (mean2 - mean1) / se_diff

    # Welch-Satterthwaite equation for degrees of freedom
    if (se1 + se2) == 0:
        df = 1.0
    else:
        df = ((se1 + se2) ** 2) / ((se1 ** 2) / (n1 - 1) + (se2 ** 2) / (n2 - 1))

    # Normal approximation for large df (or Student-t survival function approximation)
    p_value = 0.5 * math.erfc(t_stat / math.sqrt(2))

    return t_stat, df, p_value


def calculate_z_test_proportions(
    k1: int, n1: int, k2: int, n2: int
) -> Tuple[float, float]:
    """
    Calculate Z-test for two proportions.
    k1, n1 = control successes, control total
    k2, n2 = treatment successes, treatment total
    """
    if n1 == 0 or n2 == 0:
        return 0.0, 1.0

    p1 = k1 / n1
    p2 = k2 / n2
    p_pooled = (k1 + k2) / (n1 + n2)

    se = math.sqrt(p_pooled * (1 - p_pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0

    z_stat = (p2 - p1) / se
    p_value = 0.5 * math.erfc(z_stat / math.sqrt(2))

    return z_stat, p_value


def analyze_db(db_path: str, min_score: int = 0) -> None:
    """Run lift analysis on database."""
    if not os.path.exists(db_path):
        print(f"Error: Database file not found at {db_path!r}")
        sys.exit(1)

    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from app.db.create_tables import Business, Contact, EmailVerification, Base

    engine = create_engine(f"sqlite:///{db_path}")
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        businesses = session.query(Business).all()
        if not businesses:
            print("No businesses found in database.")
            return

        # Preload contacts & score map
        contacts_by_biz = defaultdict(list)
        all_contacts = session.query(Contact).all()

        # Score lookup
        scores = {}
        verifications = session.query(EmailVerification).all()
        for v in verifications:
            scores[v.contact_id] = v.score

        for c in all_contacts:
            if c.email:
                c_score = scores.get(c.id, 0)
                if min_score > 0 and c_score < min_score:
                    continue
                contacts_by_biz[c.business_id].append(c)

        # Categorize Businesses
        # Control (Web only) vs Treatment (Tomba involved)
        web_only_dm_counts = []
        tomba_dm_counts = []
        combined_dm_counts = []

        tomba_biz_count = 0
        web_biz_count = 0

        total_dm_web = 0
        total_dm_tomba = 0

        for b in businesses:
            biz_contacts = contacts_by_biz.get(b.id, [])

            web_contacts = [
                c for c in biz_contacts
                if (c.title or "") != "Executive / Decision Maker" and c.name != "Decision Maker"
            ]
            tomba_contacts = [
                c for c in biz_contacts
                if (c.title or "") == "Executive / Decision Maker" or c.name == "Decision Maker"
            ]

            web_dms = [c for c in web_contacts if is_decision_maker(c.name, c.title, c.email)]
            tomba_dms = [c for c in tomba_contacts if is_decision_maker(c.name, c.title, c.email)]
            all_dms = [c for c in biz_contacts if is_decision_maker(c.name, c.title, c.email)]

            total_dm_web += len(web_dms)
            total_dm_tomba += len(tomba_dms)

            web_only_dm_counts.append(len(web_dms))
            combined_dm_counts.append(len(all_dms))

            if tomba_contacts:
                tomba_biz_count += 1
                tomba_dm_counts.append(len(all_dms))
            else:
                web_biz_count += 1

        total_biz = len(businesses)
        mean_web = sum(web_only_dm_counts) / total_biz if total_biz else 0.0
        mean_combined = sum(combined_dm_counts) / total_biz if total_biz else 0.0

        # Conversion rates (% businesses with >= 1 Decision Maker)
        k_web = sum(1 for c in web_only_dm_counts if c > 0)
        k_combined = sum(1 for c in combined_dm_counts if c > 0)

        cr_web = (k_web / total_biz * 100) if total_biz else 0.0
        cr_combined = (k_combined / total_biz * 100) if total_biz else 0.0

        # Statistical Tests
        t_stat, df, p_val_t = calculate_welch_ttest(web_only_dm_counts, combined_dm_counts)
        z_stat, p_val_z = calculate_z_test_proportions(k_web, total_biz, k_combined, total_biz)

        lift_yield = ((mean_combined - mean_web) / mean_web * 100) if mean_web > 0 else 0.0
        lift_cr = cr_combined - cr_web

        print("\n" + "=" * 70)
        print("📊 HYPOTHESIS TEST & LIFT ANALYSIS REPORT: TOMBA ENRICHMENT")
        print("=" * 70)
        print(f"Database Path:           {db_path}")
        print(f"Total Businesses:        {total_biz}")
        print(f"Businesses with Tomba:   {tomba_biz_count} ({tomba_biz_count/total_biz*100:.1f}%)")
        print(f"Min Score Filter:        {min_score}")
        print("-" * 70)

        print("\n1. LEAD YIELD COMPARISON (Decision Maker Contacts / Business)")
        print(f"  • Web Crawler Alone:   {mean_web:.3f} DM leads/biz  (Total: {total_dm_web})")
        print(f"  • Web + Tomba Pipeline: {mean_combined:.3f} DM leads/biz  (Total: {total_dm_web + total_dm_tomba})")
        print(f"  • Net Lift in Yield:    +{mean_combined - mean_web:.3f} DM leads/biz ({lift_yield:+.1f}%)")

        print("\n2. BUSINESS CONVERSION RATE (% Businesses with >= 1 Decision Maker)")
        print(f"  • Web Crawler Alone:   {cr_web:.1f}% ({k_web}/{total_biz})")
        print(f"  • Web + Tomba Pipeline: {cr_combined:.1f}% ({k_combined}/{total_biz})")
        print(f"  • Absolute Lift in CR:  {lift_cr:+.1f}% points")

        print("\n3. STATISTICAL SIGNIFICANCE TESTING")
        print(f"  • Welch's t-test (Yield): t = {t_stat:.3f}, df = {df:.1f}, p-value = {p_val_t:.4f}")
        print(f"  • Z-test (Conversion Rate): Z = {z_stat:.3f}, p-value = {p_val_z:.4f}")

        print("\n4. HYPOTHESIS TEST CONCLUSION")
        if p_val_t < 0.05 or p_val_z < 0.05:
            print("  ✅ REJECT NULL HYPOTHESIS (H0)")
            print("  Conclusion: Tomba enrichment delivers a STATISTICALLY SIGNIFICANT POSITIVE LIFT")
            print(f"  in decision-maker lead generation (p < 0.05 at 95% confidence level).")
        else:
            print("  ⚠️ FAIL TO REJECT NULL HYPOTHESIS (H0)")
            print("  Conclusion: The observed lift is not statistically significant yet (p >= 0.05).")
            print("  Consider increasing the sample size by running full-harvest or grid mode across more ZIPs.")

        print("=" * 70 + "\n")

    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(description="Analyze Tomba enrichment statistical lift.")
    parser.add_argument(
        "--db-path",
        type=str,
        default="database/hvac_leads.db",
        help="Path to SQLite database (default: database/hvac_leads.db)",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=0,
        help="Minimum verification score filter (default: 0)",
    )
    args = parser.parse_args()
    analyze_db(args.db_path, min_score=args.min_score)


if __name__ == "__main__":
    main()
