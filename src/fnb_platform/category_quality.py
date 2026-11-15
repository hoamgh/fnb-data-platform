"""Versioned source category features and identity-aware, offline category audit.

This publishes a derived quality release, never source Silver or Canonical Gold.
Accepted mappings are an in-memory contract for a future integration, not a human
decision importer. The CLI deliberately has no accepted-mapping input.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
import os
from pathlib import Path
import shutil
import uuid

import pyarrow as pa
import pyarrow.parquet as pq

from .categories import PRODUCT_CATEGORY_GROUPS
from . import google_silver as silver
from .overture_incremental import _digest

VERSION = "category-quality-v1"
ER_VERSION = "ov-google-er-proposal-v3-experimental"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MAPPING = ROOT / "config/category_taxonomy_v1.json"
GROUPS = frozenset(["restaurant", "cafe_coffee", "beverage", "bakery_dessert",
                    "fast_food", "bar", "other_food", "non_fnb", "unknown"])
FNB = GROUPS - {"non_fnb", "unknown"}
KEY = ("google_id_kind", "google_identity_value")
NORMAL_SCHEMA = pa.schema([
    *[(k, pa.string()) for k in ("source", "source_id", *KEY, "name_raw", "address_raw",
        "raw_categories_json", "source_provenance_json", "category_evidence_json", "dq_details_json",
        "candidate_classification", "category_validation_status", "mapping_version", "rule_version",
        "eligibility_reason", "eligibility_policy_version")],
    *[(k, pa.list_(pa.string())) for k in ("normalized_categories", "unknown_values", "dq_flags", "reason_codes")],
    ("baseline_fnb_candidate", pa.bool_()), ("inside_ward", pa.bool_()),
    ("broad_fnb_evidence", pa.bool_()), ("taxonomy_path_valid", pa.bool_()),
])
AUDIT_SCHEMA = pa.schema([
    *[(k, pa.string()) for k in ("overture_id", *KEY, "overture_name", "google_name",
        "overture_address", "google_address", "overture_categories_raw", "google_categories_raw",
        "comparison_status", "category_relation", "classification_scope", "er_classification", "er_status",
        "reason", "evidence_json", "observation_ref", "source_provenance_json", "mapping_version",
        "rule_version", "store_id", "canonical_category", "canonical_category_group",
        "category_validation_status", "category_source", "category_rule_version", "category_provenance")],
    *[(k, pa.list_(pa.string())) for k in ("overture_categories", "google_categories", "reason_codes")],
    ("shared_google_identity", pa.bool_()), ("multiple_google_candidates", pa.bool_()),
    ("baseline_fnb_candidate", pa.bool_()), ("overture_inside_ward", pa.bool_()),
])


def require(condition, message):
    if not condition:
        raise ValueError(message)


def google_key(row):
    return tuple(row.get(k) for k in KEY)


def link_key(row):
    return (row["overture_id"], *google_key(row))


def load_mapping(path=DEFAULT_MAPPING):
    mapping = json.loads(Path(path).read_text(encoding="utf-8"))
    overture = {silver.normalized(k): v for k, v in PRODUCT_CATEGORY_GROUPS.items()}
    google = {}
    for target, field in ((overture, "overture_additions"), (google, "google_aliases")):
        for group, aliases in mapping[field].items():
            require(group in GROUPS - {"unknown"}, "Invalid mapping category")
            for alias in aliases:
                alias = silver.normalized(alias)
                require(alias and (alias not in target or target[alias] == group), "Conflicting alias: " + str(alias))
                target[alias] = group
    # Existing taxonomy keys contain underscores; comparison uses shared Unicode normalization.
    mapping["overture"] = overture
    mapping["google"] = google
    require(bool(mapping.get("mapping_version")), "Missing mapping version")
    return mapping


def values(value, field, invalid):
    """Do not split an opaque category label on punctuation or guess its meaning."""
    if value is None:
        return []
    if isinstance(value, str):
        return [(field, value)] if value.strip() else []
    if isinstance(value, list):
        result = []
        for i, item in enumerate(value):
            if isinstance(item, str) and item.strip():
                result.append((f"{field}[{i}]", item))
            else:
                invalid.append(f"{field}[{i}]")
        return result
    invalid.append(field)
    return []


def normalize_source(row, source, mapping, reference):
    invalid, flags, unknown, evidence = [], [], [], []
    groups = set()
    broad, path_valid = False, None
    if source == "overture":
        raw = {k: row.get(k) for k in ("taxonomy_primary", "taxonomy_hierarchy", "taxonomy_alternates", "basic_category")}
        parts = {k: values(v, k, invalid) for k, v in raw.items()}
        hierarchy = [v for _, v in parts["taxonomy_hierarchy"]]
        primary = row.get("taxonomy_primary")
        path_valid = bool(hierarchy and primary and hierarchy[-1] == primary) if hierarchy or primary else None
        if path_valid is False:
            flags.append("INVALID_TAXONOMY_PATH")
        broad = "food_and_drink" in hierarchy
        lookup = mapping["overture"]
        # Primary and explicit alternates are independent labels. Ancestors/basic
        # are fallbacks, not extra subcategories (bakery is not also a restaurant).
        chosen = parts["taxonomy_primary"] + parts["taxonomy_alternates"]
        if not any(silver.normalized(v) in lookup for _, v in parts["taxonomy_primary"]):
            # A generic food root does not repair an unrecognized leaf subtype.
            fallback = [(f, v) for f, v in parts["taxonomy_hierarchy"]
                        if v != "food_and_drink" and silver.normalized(v) in lookup]
            chosen += fallback[-1:] or parts["basic_category"]
        for field, value in chosen:
            group = lookup.get(silver.normalized(value))
            evidence.append(dict(field=field, raw_value=value, comparison_value=silver.normalized(value), group=group or "unknown"))
            if group:
                groups.add(group)
            elif value not in unknown:
                unknown.append(value)
        root = hierarchy[0] if hierarchy else None
        if root in mapping["overture_non_fnb_roots"]:
            groups.add("non_fnb")
            evidence.append(dict(field="taxonomy_hierarchy[0]", raw_value=root, group="non_fnb"))
        if broad and "non_fnb" in groups:
            flags.append("FNB_HIERARCHY_NON_FNB_LABEL")
        provenance = dict(input=reference, source_release=row.get("source_release"),
            source_manifest_sha256=row.get("source_manifest_sha256"), source_id=row["source_id"])
        identity = dict(source_id=row["source_id"], google_id_kind=None, google_identity_value=None)
    else:
        raw = {k: row.get(k) for k in ("category_display", "category_normalized")}
        display = values(raw["category_display"], "category_display", invalid)
        normalized = values(raw["category_normalized"], "category_normalized", invalid)
        for field, value in display or normalized:
            group = mapping["google"].get(silver.normalized(value))
            evidence.append(dict(field=field, raw_value=value, comparison_value=silver.normalized(value), group=group or "unknown"))
            if group:
                groups.add(group)
            else:
                unknown.append(value)
        if display and normalized and {silver.normalized(v) for _, v in display} != {silver.normalized(v) for _, v in normalized}:
            flags.append("CATEGORY_DISPLAY_NORMALIZED_DISAGREEMENT")
        field_refs = json.loads(row.get("field_provenance_json") or "{}")
        conflicts = json.loads(row.get("conflicting_values_json") or "{}")
        category_conflicts = {k: v for k, v in conflicts.items() if k in raw}
        if category_conflicts:
            flags.append("HISTORICAL_CATEGORY_METADATA_CONFLICT")
        provenance = dict(input=reference, google_identity=dict(zip(KEY, google_key(row))),
            current_observation_id=row.get("current_observation_id"), source_run=row.get("source_run"),
            observed_at=row.get("observed_at"), metadata_status=row.get("metadata_status"),
            category_field_provenance={k: v for k, v in field_refs.items() if k in raw},
            historical_category_conflicts=category_conflicts)
        identity = dict(source_id=None, **dict(zip(KEY, google_key(row))))
    if invalid:
        flags.append("INVALID_CATEGORY_VALUE")
    if not invalid and not any(values(v, k, []) for k, v in raw.items()):
        flags.append("MISSING_CATEGORY")
    if unknown:
        flags.append("UNRECOGNIZED_CATEGORY")
    if groups & FNB and "non_fnb" in groups:
        flags.append("CONFLICTING_SOURCE_CATEGORIES")
    if source == "overture" and not row["is_fnb_candidate"] and not broad:
        name_tokens = " " + (silver.normalized(row.get("name")) or "") + " "
        hints = [t for t in mapping["food_name_audit_terms"] if " " + silver.normalized(t) + " " in name_tokens]
        if hints:
            flags.append("FNB_NAME_HINT_TAXONOMY_EXCLUDED")
            evidence.append(dict(field="name", raw_value=row.get("name"), audit_only_terms=hints,
                                 conclusion="Not category evidence; inspect possible source mislabel"))
    classification = ("UNKNOWN_CATEGORY" if "CONFLICTING_SOURCE_CATEGORIES" in flags or "FNB_HIERARCHY_NON_FNB_LABEL" in flags
                      else "FNB_CANDIDATE" if broad or groups & FNB
                      else "NON_FNB_CANDIDATE" if "non_fnb" in groups else "UNKNOWN_CATEGORY")
    descriptions = {
        "INVALID_TAXONOMY_PATH": "Taxonomy primary does not terminate the supplied hierarchy",
        "FNB_HIERARCHY_NON_FNB_LABEL": "Food hierarchy and explicit non-F&B label disagree",
        "CATEGORY_DISPLAY_NORMALIZED_DISAGREEMENT": "Display and stored normalized category values disagree after comparison normalization",
        "HISTORICAL_CATEGORY_METADATA_CONFLICT": "M1 retains different category observations; these are historical values, not a current ER conclusion",
        "INVALID_CATEGORY_VALUE": "Category contains non-string or malformed values",
        "MISSING_CATEGORY": "No usable category values in the observed category fields",
        "UNRECOGNIZED_CATEGORY": "No explicit mapping for these source labels; recognized ancestors do not verify the leaf category",
        "CONFLICTING_SOURCE_CATEGORIES": "The same source supplies both F&B and non-F&B category evidence",
        "FNB_NAME_HINT_TAXONOMY_EXCLUDED": "Business name contains a food-related term but taxonomy did not select F&B; this is a possible mislabel, not proof",
    }
    detail_evidence = dict(raw_categories=raw, matches=evidence, invalid_fields=invalid,
                          unrecognized_values=sorted(set(unknown)), normalized_categories=sorted(groups or {"unknown"}),
                          mapping_version=mapping["mapping_version"])
    details = [dict(flag_code=code, severity="WARNING", reason=descriptions[code] + ": " + silver.dump(detail_evidence),
        evidence_json=silver.dump(detail_evidence), observation_ref=silver.dump(provenance), rule_version=VERSION,
        recommended_action="Inspect the referenced source category/name observation and mapping; retain the record and existing eligibility.")
        for code in sorted(set(flags))]
    return dict(source=source, **identity, name_raw=row.get("name" if source == "overture" else "name_display"),
        address_raw=row.get("address" if source == "overture" else "address_display"),
        raw_categories_json=silver.dump(raw), normalized_categories=sorted(groups or {"unknown"}),
        dq_details_json=silver.dump(details),
        unknown_values=sorted(set(unknown)), category_evidence_json=silver.dump(dict(matches=evidence, invalid_fields=invalid)),
        source_provenance_json=silver.dump(provenance), candidate_classification=classification,
        baseline_fnb_candidate=row.get("is_fnb_candidate") if source == "overture" else None,
        inside_ward=row.get("inside_ward") if source == "overture" else None,
        broad_fnb_evidence=broad, taxonomy_path_valid=path_valid,
        eligibility_reason=row.get("eligibility_reason") if source == "overture" else None,
        eligibility_policy_version=row.get("eligibility_policy_version") if source == "overture" else None,
        category_validation_status="SOURCE_LABEL_UNVERIFIED", mapping_version=mapping["mapping_version"],
        rule_version=VERSION, dq_flags=sorted(set(flags)), reason_codes=sorted(set(flags) or {"RECOGNIZED_SOURCE_CATEGORY"}))


def unique_index(rows, key, label):
    result = {}
    for row in rows:
        k = key(row)
        parts = k if isinstance(k, tuple) else (k,)
        require(all(isinstance(v, str) and v.strip() for v in parts), "Missing " + label + " key")
        require(k not in result, "Duplicate " + label + " key: " + str(k))
        result[k] = row
    return result


def accepted_index(mappings, links):
    """Future dependency contract only. No CSV reader, persistence or decisions."""
    result = unique_index(mappings, link_key, "accepted mapping")
    source_stores, google_stores = defaultdict(set), defaultdict(set)
    for k, m in result.items():
        require(k in links, "Accepted mapping references an unknown relationship")
        require(m.get("identity_status") == "MATCH" and all(isinstance(m.get(f), str) and m[f].strip()
            for f in ("store_id", "decision_ref", "mapping_version")), "Accepted mapping requires explicit MATCH, store ID and provenance")
        source_stores[k[0]].add(m["store_id"])
        google_stores[k[1:]].add(m["store_id"])
    require(all(len(s) == 1 for s in [*source_stores.values(), *google_stores.values()]), "Inconsistent canonical mapping")
    return result


def relation(left, right):
    a = set(left["normalized_categories"]) - {"unknown"} if left else set()
    b = set(right["normalized_categories"]) - {"unknown"} if right else set()
    if not a and not b:
        return "UNKNOWN"
    if not a or not b:
        return "SOURCE_ONLY"
    if any(s & FNB and "non_fnb" in s for s in (a, b)) or ((bool(a & FNB) and "non_fnb" in b) or (bool(b & FNB) and "non_fnb" in a)):
        return "CATEGORY_CONFLICT"
    if a == b:
        return "CONSISTENT"
    return "SUBCATEGORY_DIFFERENCE" if a & FNB and b & FNB else "UNKNOWN"


def reconcile(left, right, er, refs, accepted, sources_by_google, google_by_source, mapping_version):
    oid = left["source_id"] if left else None
    gkey = google_key(right) if right else (None, None)
    lk = (oid, *gkey)
    identity = accepted.get(lk)
    related_sources = sources_by_google.get(gkey, set())
    related_google = google_by_source.get(oid, set())
    shared = bool(right and len(related_sources) > 1)
    multiple = bool(left and len(related_google) > 1)
    base = relation(left, right)
    codes = ["CATEGORY_" + base]
    for source, item in (("OVERTURE", left), ("GOOGLE", right)):
        if item:
            codes.extend(source + "_" + code for code in item["dq_flags"])
    if shared:
        codes.append("GOOGLE_ID_SHARED_BY_OVERTURE_IDS")
    if multiple:
        codes.append("OVERTURE_MULTIPLE_GOOGLE_CANDIDATES")
    if er and not identity:
        codes.append("IDENTITY_NOT_ACCEPTED")
    status = "NEEDS_IDENTITY_RECHECK" if er and not identity and (shared or multiple or base == "CATEGORY_CONFLICT" or er["classification"] == "NON_MATCH_CANDIDATE") else base
    lg = left["normalized_categories"] if left else []
    rg = right["normalized_categories"] if right else []
    reason = f"Overture categories {lg}; Google categories {rg}: {base}."
    if shared:
        reason += f" Google identity {gkey} is selected by {sorted(related_sources)}."
    if multiple:
        reason += f" Overture {oid} selects {sorted(related_google)}."
    if er and not identity:
        reason += f" ER V3 is {er['classification']}/{er['er_status']}; no accepted identity mapping."
    if left and "FNB_NAME_HINT_TAXONOMY_EXCLUDED" in left["dq_flags"]:
        reason += f" Name {left['name_raw']!r} has food-related terms but raw taxonomy is {left['raw_categories_json']}; category requires inspection."
    clean = all(not r or not r["dq_flags"] for r in (left, right))
    selected = bool(identity and base == "CONSISTENT" and len(lg) == 1 and lg != ["unknown"] and clean)
    evidence = dict(overture=dict(raw=json.loads(left["raw_categories_json"]), normalized=lg) if left else None,
        google=dict(raw=json.loads(right["raw_categories_json"]), normalized=rg) if right else None,
        category_relation=base, related_overture_ids=sorted(related_sources),
        related_google_candidates=[list(x) for x in sorted(related_google)],
        er=dict(classification=er["classification"], er_status=er["er_status"], rule_version=er["rule_version"],
                reason=er.get("reason"), observation_ref=er.get("observation_ref")) if er else None,
        source_flags={"overture": left["dq_flags"] if left else [], "google": right["dq_flags"] if right else []},
        accepted_mapping=identity, category_is_not_identity_evidence=True)
    provenance = dict(overture=json.loads(left["source_provenance_json"]) if left else None,
        google=json.loads(right["source_provenance_json"]) if right else None, er=refs["er"] if er else None,
        mapping_version=mapping_version, accepted_mapping=identity)
    return dict(overture_id=oid, **dict(zip(KEY, gkey)), overture_name=left["name_raw"] if left else None,
        google_name=right["name_raw"] if right else None, overture_address=left["address_raw"] if left else None,
        google_address=right["address_raw"] if right else None,
        overture_categories_raw=left["raw_categories_json"] if left else None,
        google_categories_raw=right["raw_categories_json"] if right else None,
        overture_categories=lg, google_categories=rg, comparison_status=status, category_relation=base,
        classification_scope="VERIFIED_IDENTITY" if identity else "PROVISIONAL",
        er_classification=er["classification"] if er else None, er_status=er["er_status"] if er else None,
        reason=reason, reason_codes=codes, evidence_json=silver.dump(evidence),
        observation_ref=silver.dump(dict(inputs=refs, overture_id=oid, google_identity=list(gkey))),
        source_provenance_json=silver.dump(provenance), mapping_version=mapping_version, rule_version=VERSION,
        shared_google_identity=shared, multiple_google_candidates=multiple,
        baseline_fnb_candidate=left["baseline_fnb_candidate"] if left else None,
        overture_inside_ward=left["inside_ward"] if left else None,
        store_id=identity["store_id"] if identity else None,
        canonical_category=lg[0] if selected else "unknown", canonical_category_group=lg[0] if selected else "unknown",
        category_validation_status="SOURCE_AGREEMENT_ACCEPTED_IDENTITY" if selected else "PENDING",
        category_source="overture+google" if selected else None, category_rule_version=VERSION,
        category_provenance=silver.dump(provenance) if selected else None)


def build(overture, places, er_rows, mapping, refs, *, accepted_mappings=()):
    ov = unique_index(overture, lambda r: r["source_id"], "Overture")
    gp = unique_index(places, google_key, "Google")
    require(all(silver.typed_id(k[1]) == k for k in gp), "Invalid Google typed identity")
    er = unique_index(er_rows, link_key, "ER")
    require(all(r["classification"] in {"MATCH_CANDIDATE", "NON_MATCH_CANDIDATE", "REVIEW", "INSUFFICIENT_DATA"}
                and r["er_status"] == "ER_PENDING" and r["rule_version"] == ER_VERSION for r in er_rows), "Unexpected ER V3 contract")
    accepted = accepted_index(accepted_mappings, er)
    by_google, by_source = defaultdict(set), defaultdict(set)
    for oid, kind, value in er:
        require(oid in ov and (kind, value) in gp, "Relationship references missing source record")
        by_google[(kind, value)].add(oid)
        by_source[oid].add((kind, value))
    on = {k: normalize_source(v, "overture", mapping, refs["overture"]) for k, v in sorted(ov.items())}
    gn = {k: normalize_source(v, "google", mapping, refs["places"]) for k, v in sorted(gp.items())}
    audits = [reconcile(on[k[0]], gn[k[1:]], v, refs, accepted, by_google, by_source, mapping["mapping_version"]) for k, v in sorted(er.items())]
    audits += [reconcile(v, None, None, refs, accepted, by_google, by_source, mapping["mapping_version"]) for k, v in on.items() if k not in by_source]
    audits += [reconcile(None, v, None, refs, accepted, by_google, by_source, mapping["mapping_version"]) for k, v in gn.items() if k not in by_google]
    # Category selection is also checked across accepted relationships of a store.
    stores = defaultdict(list)
    for row in audits:
        if row["store_id"]:
            stores[row["store_id"]].append(row)
    for rows in stores.values():
        categories = {tuple(r[s]) for r in rows for s in ("overture_categories", "google_categories") if r[s] and r[s] != ["unknown"]}
        if len(categories) > 1 or any(r["category_relation"] == "CATEGORY_CONFLICT" for r in rows):
            for row in rows:
                row.update(canonical_category="unknown", canonical_category_group="unknown", category_validation_status="PENDING", category_source=None, category_provenance=None)
                row["reason_codes"].append("CANONICAL_STORE_CATEGORY_DISAGREEMENT")
                row["reason"] += " Accepted store has incompatible category sets; no category selected."
    normalized = [*on.values(), *gn.values()]
    validate(normalized, audits, set(ov), set(gp), set(er), accepted)
    counts = lambda rows, field: dict(sorted(Counter(r.get(field) or "MISSING" for r in rows).items()))
    summary = dict(rule_version=VERSION, mapping_version=mapping["mapping_version"], inputs=refs,
        coverage=dict(overture_pois=len(ov), google_pois=len(gp), candidate_links=len(er),
            baseline_candidates=sum(r["is_fnb_candidate"] for r in overture),
            baseline_zero_google_candidates=sum(r["is_fnb_candidate"] and k not in by_source for k, r in ov.items()),
            overture_without_links=sum(k not in by_source for k in ov), google_without_links=sum(k not in by_google for k in gp),
            normalized_rows=len(normalized), audit_rows=len(audits), accepted_mappings=len(accepted)),
        source_metrics={source: dict(poi_classification=counts([r for r in normalized if r["source"] == source], "candidate_classification"),
            category_membership_counts=dict(sorted(Counter(c for r in normalized if r["source"] == source for c in r["normalized_categories"]).items())),
            warning_counts=dict(sorted(Counter(f for r in normalized if r["source"] == source for f in r["dq_flags"]).items()))) for source in ("overture", "google")},
        eligibility=dict(selected_reasons=counts([r for r in overture if r["is_fnb_candidate"]], "eligibility_reason"),
            excluded_reasons=counts([r for r in overture if not r["is_fnb_candidate"]], "eligibility_reason"),
            food_hierarchy_total=sum(r["broad_fnb_evidence"] for r in on.values()),
            food_hierarchy_outside_baseline=sum(r["broad_fnb_evidence"] and not r["baseline_fnb_candidate"] for r in on.values()),
            no_food_hierarchy_total=sum(not r["broad_fnb_evidence"] for r in on.values()),
            excluded_taxonomy_name_hints=sum("FNB_NAME_HINT_TAXONOMY_EXCLUDED" in r["dq_flags"] for r in on.values()),
            excluded_taxonomy_name_hints_inside_ward=sum("FNB_NAME_HINT_TAXONOMY_EXCLUDED" in r["dq_flags"] and r["inside_ward"] is True for r in on.values()),
            baseline_category_membership=dict(sorted(Counter(g for r in on.values() if r["baseline_fnb_candidate"] for g in r["normalized_categories"]).items()))),
        linked_comparison_status=counts(audits[:len(er)], "comparison_status"),
        linked_category_relation=counts(audits[:len(er)], "category_relation"),
        all_comparison_status=counts(audits, "comparison_status"),
        shared_google_identity_links=sum(r["shared_google_identity"] for r in audits),
        multiple_google_candidate_links=sum(r["multiple_google_candidates"] for r in audits),
        hard_failures=0, warnings_do_not_remove_records=True,
        limitations=["Counts are source observations, not verified physical stores.",
            "Membership counts are non-additive for multi-category POIs.",
            "Unmapped leaf labels can coexist with a recognized broad taxonomy group; unknown_values tracks this incomplete mapping.",
            "Google coverage exists only for discovered candidates; unlinked taxonomy exclusions are unaudited across sources.",
            "Name hints identify possible mislabels only; no category or eligibility changes.",
            "Accepted identity does not independently verify the real-world category."])
    return {"category_normalization.parquet": pa.Table.from_pylist(normalized, schema=NORMAL_SCHEMA),
            "category_audit.parquet": pa.Table.from_pylist(audits, schema=AUDIT_SCHEMA)}, summary


def validate(normalized, audits, ov_ids, google_ids, er_keys, accepted):
    require({r["source_id"] for r in normalized if r["source"] == "overture"} == ov_ids, "Overture coverage loss")
    require({google_key(r) for r in normalized if r["source"] == "google"} == google_ids, "Google coverage loss")
    unique_index(normalized, lambda r: (r["source"], r["source_id"] or silver.dump(google_key(r))), "normalized")
    audit_keys = [(r["overture_id"], *google_key(r)) for r in audits]
    require(len(set(audit_keys)) == len(audit_keys), "Duplicate category audit key")
    require({k for k in audit_keys if k[0] and k[1]} == er_keys, "ER link coverage loss")
    require({r["overture_id"] for r in audits if r["overture_id"]} == ov_ids, "Audit Overture coverage loss")
    require({google_key(r) for r in audits if r["google_id_kind"]} == google_ids, "Audit Google coverage loss")
    for row in normalized:
        require(bool(row["normalized_categories"]) and set(row["normalized_categories"]) <= GROUPS, "Invalid normalized category")
        require(bool(json.loads(row["source_provenance_json"]).get("input")), "Missing source provenance")
        details = json.loads(row["dq_details_json"])
        require({d["flag_code"] for d in details} == set(row["dq_flags"]), "Missing DQ flag evidence")
        require(all(all(d.get(k) for k in ("reason", "evidence_json", "observation_ref", "rule_version", "recommended_action")) for d in details), "Incomplete DQ explanation")
    for row in audits:
        require(row["canonical_category"] in GROUPS and row["canonical_category_group"] in GROUPS, "Invalid canonical category")
        require(row["canonical_category"] == row["canonical_category_group"], "Inconsistent category group at this mapping granularity")
        require(bool(row["reason"] and row["reason_codes"] and row["evidence_json"] and row["observation_ref"]), "Missing audit evidence")
        if row["store_id"]:
            require(link_key(row) in accepted and accepted[link_key(row)]["store_id"] == row["store_id"], "Canonical ID without accepted mapping")
        if row["canonical_category"] != "unknown":
            require(row["store_id"] and row["category_provenance"] and row["category_source"] == "overture+google" and row["category_relation"] == "CONSISTENT" and row["category_validation_status"] == "SOURCE_AGREEMENT_ACCEPTED_IDENTITY", "Unsafe canonical category selection")
        else:
            require(row["category_validation_status"] == "PENDING" and row["category_source"] is None and row["category_provenance"] is None, "Unknown category presented as selected")
        require(not (row["classification_scope"] == "PROVISIONAL" and row["store_id"]), "Provisional canonical mapping")


def _read_table(path, columns):
    require(set(columns) <= set(pq.read_schema(path).names), "Missing required schema fields: " + str(path))
    return pq.read_table(path, columns=columns).to_pylist()


def publish(eligibility_root, google_root, er_root, output_root, mapping_path=DEFAULT_MAPPING, *, fault=lambda stage: None):
    # Reuse the existing M1 eligibility verifier; do not replay/rebuild ingestion.
    from .overture import eligibility

    roots = [Path(p).resolve() for p in (eligibility_root, google_root, er_root)]
    out, mapping_path = Path(output_root).resolve(), Path(mapping_path).resolve()
    require(all(not out.is_relative_to(p) and not p.is_relative_to(out) for p in roots), "Separate input/output roots required")
    el, em = eligibility.read_active(roots[0])
    gs, ers = silver.read_active(roots[1]), silver.read_active(roots[2])
    require(gs is not None and ers is not None, "Published Google/ER releases required")
    gm, erm = silver.verify_release(gs), silver.verify_release(ers)
    require(erm["signature"]["rule_version"] == ER_VERSION, "Expected ER V3 release")
    ov_paths = [Path(p) for p in gm["signature"]["inputs_sha256"] if Path(p).name == "current_state.parquet"]
    require(len(ov_paths) == 1, "Cannot identify authoritative Overture Silver")
    ov_path = ov_paths[0].resolve()
    require(not out.is_relative_to(ov_path.parent) and not ov_path.is_relative_to(out), "Output overlaps original Silver")
    fingerprints = [h for p, h in em["signature"]["inputs_sha256"].items() if Path(p).name == "current_state.parquet"]
    require(len(fingerprints) == 1, "Cannot identify eligibility Silver lineage")
    expected_ov = fingerprints[0]
    require(expected_ov and silver.sha(ov_path) == expected_ov == gm["signature"]["inputs_sha256"][str(ov_paths[0])], "Overture/eligibility/Silver lineage mismatch")
    ep, pp, lp, erp = el / "eligibility.parquet", gs / "silver/google_places.parquet", gs / "silver/source_links.parquet", ers / "er_results.parquet"
    for path in (ov_path, pp, lp):
        require(erm["signature"]["inputs_sha256"].get(str(path)) == silver.sha(path), "ER/Silver lineage mismatch: " + str(path))
    paths = [ep, pp, lp, erp, ov_path, mapping_path, el / "manifest.json", gs / "manifest.json", ers / "manifest.json", *[r / "current.json" for r in roots]]
    hashes = {str(p): silver.sha(p) for p in paths}
    refs = {name: dict(path=str(p), sha256=hashes[str(p)]) for name, p in (("overture", ep), ("places", pp), ("er", erp))}
    refs["overture_silver"] = dict(path=str(ov_path), sha256=expected_ov)
    refs["releases"] = dict(eligibility=em["run_id"], google=gm["release_id"], er=erm["release_id"])
    ov_cols = ["source_id", "name", "address", "taxonomy_primary", "taxonomy_hierarchy", "taxonomy_alternates", "basic_category", "is_fnb_candidate", "inside_ward", "source_release", "source_manifest_sha256", "eligibility_reason", "eligibility_policy_version"]
    google_cols = [*KEY, "name_display", "address_display", "category_display", "category_normalized", "field_provenance_json", "conflicting_values_json", "metadata_status", "current_observation_id", "observed_at", "source_run"]
    overture = _read_table(ep, ov_cols)
    silver_ov = unique_index(_read_table(ov_path, ["source_id", "name", "address", "taxonomy_primary", "basic_category"]), lambda r: r["source_id"], "Silver")
    require(set(silver_ov) == {r["source_id"] for r in overture}, "Eligibility/Overture key mismatch")
    require(all(all(r[k] == silver_ov[r["source_id"]][k] for k in ("name", "address", "taxonomy_primary", "basic_category")) for r in overture), "Eligibility/Silver value mismatch")
    require(sum(r["is_fnb_candidate"] for r in overture) == em["metrics"]["counts"]["candidates"], "Baseline candidate count changed")
    er_rows = _read_table(erp, ["overture_id", *KEY, "classification", "er_status", "rule_version", "reason", "observation_ref"])
    require(set(unique_index(er_rows, link_key, "ER")) == set(unique_index(_read_table(lp, ["overture_id", *KEY]), link_key, "source links")), "Lost ER links")
    tables, summary = build(overture, _read_table(pp, google_cols), er_rows, load_mapping(mapping_path), refs)
    summary["baseline_reconciliation"] = em["metrics"]["counts"]
    code = [Path(__file__), Path(silver.__file__), Path(__file__).with_name("categories.py"), Path(__file__).with_name("overture_incremental.py"), Path(eligibility.__file__), Path(eligibility.policy.__file__)]
    signature = dict(rule_version=VERSION, mapping_version=summary["mapping_version"], inputs_sha256=hashes,
        code_sha256={str(p.resolve()): silver.sha(p) for p in code}, pyarrow_version=pa.__version__)
    rid = _digest(signature)[:24]
    require(not out.is_relative_to(mapping_path) and not mapping_path.is_relative_to(out), "Output contains mapping input")
    if (out / "current.json").exists():
        active = silver.read_active(out)
        require(silver.verify_release(active)["signature"]["rule_version"] == VERSION, "Different dataset at output root")
    out.mkdir(parents=True, exist_ok=True)
    lock, stage = out / ".publish.lock", None
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    try:
        dest = out / "releases" / rid
        if not dest.exists():
            stage = out / (".stage-" + uuid.uuid4().hex)
            stage.mkdir()
            artifacts = {}
            for name, table in tables.items():
                pq.write_table(table, stage / name, compression="zstd")
                artifacts[name] = dict(rows=table.num_rows, sha256=silver.sha(stage / name), logical_sha256=_digest(table.to_pylist()), schema=str(table.schema))
            inspection = [r for r in tables["category_audit.parquet"].to_pylist() if r["comparison_status"] in ("CATEGORY_CONFLICT", "NEEDS_IDENTITY_RECHECK", "SUBCATEGORY_DIFFERENCE")
                or any(x in r["reason_codes"] for x in ("OVERTURE_FNB_NAME_HINT_TAXONOMY_EXCLUDED", "OVERTURE_INVALID_TAXONOMY_PATH", "OVERTURE_CONFLICTING_SOURCE_CATEGORIES", "GOOGLE_CONFLICTING_SOURCE_CATEGORIES"))]
            fields = ["overture_name", "google_name", "overture_address", "google_address", "overture_categories_raw", "google_categories_raw", "overture_categories", "google_categories", "comparison_status", "category_relation", "reason", "reason_codes", "overture_id", *KEY, "baseline_fnb_candidate", "overture_inside_ward", "classification_scope", "er_classification", "source_provenance_json", "mapping_version", "rule_version", "observation_ref"]
            with (stage / "category_conflicts_for_review.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for row in inspection:
                    writer.writerow({k: silver.dump(row[k]) if isinstance(row[k], list) else row[k] for k in fields})
            summary["inspection_rows"] = len(inspection)
            (stage / "category_dq_summary.json").write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            for name in ("category_conflicts_for_review.csv", "category_dq_summary.json"):
                artifacts[name] = dict(sha256=silver.sha(stage / name))
            manifest = dict(release_id=rid, signature=signature, artifacts=artifacts,
                grain=dict(category_normalization="one per Overture source ID or Google typed identity", category_audit="one per ER composite link plus unlinked source singletons"),
                publication_kind="CATEGORY_QUALITY_ONLY_NOT_GOLD")
            (stage / "manifest.json").write_text(silver.dump(manifest), encoding="utf-8")
            silver.verify_release(stage)
            fault("after_stage_validation")
            for path, digest in hashes.items():
                require(silver.sha(path) == digest, "Input changed during processing: " + path)
            dest.parent.mkdir(exist_ok=True)
            os.replace(stage, dest)
            stage = None
        else:
            require(silver.verify_release(dest)["signature"] == signature, "Release signature mismatch")
            summary = json.loads((dest / "category_dq_summary.json").read_text(encoding="utf-8"))
        for path, digest in hashes.items():
            require(silver.sha(path) == digest, "Input changed before activation: " + path)
        fault("before_pointer_replace")
        temporary = out / (".current-" + uuid.uuid4().hex)
        temporary.write_text(silver.dump(dict(release_id=rid, manifest_sha256=silver.sha(dest / "manifest.json"))), encoding="utf-8")
        os.replace(temporary, out / "current.json")
        silver.read_active(out)
        return dest, summary
    finally:
        if stage and stage.exists():
            require(stage.resolve().parent == out and stage.name.startswith(".stage-"), "Unsafe stage cleanup path")
            shutil.rmtree(stage)
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eligibility-root", type=Path, required=True)
    parser.add_argument("--google-root", type=Path, required=True)
    parser.add_argument("--er-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    args = parser.parse_args()
    dest, summary = publish(args.eligibility_root, args.google_root, args.er_root, args.output_root, args.mapping)
    print(silver.dump(dict(release=str(dest), coverage=summary["coverage"], comparisons=summary["linked_comparison_status"])))


if __name__ == "__main__":
    main()
