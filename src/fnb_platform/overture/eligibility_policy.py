"""Canonical Overture hierarchy and eligibility policy; no I/O or historical imports."""


def taxonomy_decision(row):
    """Proposed policy v1: hierarchy controls eligibility, alternates trigger review."""
    taxonomy = row.get("taxonomy") or {}
    hierarchy = taxonomy.get("hierarchy") or []
    alternates = taxonomy.get("alternates") or []
    primary = taxonomy.get("primary")
    if hierarchy and primary != hierarchy[-1]:
        return "REVIEW_INVALID_TAXONOMY_PATH"
    if "food_and_drink" in hierarchy:
        return "INCLUDE_HIERARCHY_FNB"
    if "food_and_drink" in alternates:
        return "REVIEW_ALTERNATE_FNB"
    if not primary or not hierarchy:
        return "REVIEW_MISSING_TAXONOMY"
    return "EXCLUDE_NON_FNB_HIERARCHY"


def eligibility(inside, taxonomy_reason, severity, status, presence="PRESENT_IN_RELEASE", confidence=None):
    if presence != "PRESENT_IN_RELEASE":
        return False, "NOT_PRESENT_IN_RELEASE_REVIEW"
    if inside is not True:
        return False, "OUTSIDE_OR_INVALID_GEOMETRY"
    if severity == "CRITICAL":
        return False, "CRITICAL_DQ"
    if confidence is not None and not 0 <= confidence <= 1:
        return False, "INVALID_CONFIDENCE"
    if status == "permanently_closed":
        return False, "SOURCE_REPORTED_CLOSED_REVIEW"
    if status == "temporarily_closed":
        return False, "SOURCE_REPORTED_TEMPORARILY_CLOSED_REVIEW"
    if taxonomy_reason != "INCLUDE_HIERARCHY_FNB":
        return False, taxonomy_reason
    return True, "HIERARCHY_FNB_WITH_VALID_GEOMETRY"
