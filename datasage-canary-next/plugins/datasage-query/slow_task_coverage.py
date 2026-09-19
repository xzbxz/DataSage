"""Independent slow-task customer coverage and exception evidence.

This module is deliberately side-effect free and does not import workflow_io,
workflow_inputs, or legacy_workflow.  It keeps customer population counts
separate from customer-product relation counts and produces region-scoped
report tables for the audit phase.  The existing task/customer/ZIP formats are
left untouched; callers may add these tables as separate audit components.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping


_REGION_KEYS = ("product_dept", "personnel_region")
_PERSONNEL_REGIONS = {"HCM", "HN", "BKK", "IDK", "HCM-HT", "HN-HT", "BKK-HT", "IDK-HT"}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _assignment(info: Mapping[str, Any] | None, mapping: Mapping[str, Any]) -> dict[str, Any]:
    info = dict(info or {})
    sales = _text(info.get("sales"))
    customer_no = _text(info.get("customer_no"))
    name = _text(info.get("name"))
    account = _text((mapping.get("wecomBySales") or {}).get(sales))
    employee = dict((mapping.get("employeeBySales") or {}).get(sales) or {})
    reason = (
        "Missing Customer No" if not customer_no else
        "Missing Customer Name" if not name else
        "Missing Sales Owner" if not sales else
        "Missing WeCom Account" if not account else
        "Sales Owner Not Active" if not employee else
        "Personnel Account Mismatch" if _text(employee.get("wecom_account")) != account else
        "Personnel Region Mismatch" if _text(employee.get("region")) not in _PERSONNEL_REGIONS else
        None
    )
    return {
        "customer_no": customer_no,
        "name": name,
        "sales": sales,
        "account": account,
        "employee": employee,
        "personnel_region": _text(employee.get("region")),
        "reason": reason,
    }


def _product_pool(baseline: Iterable[Mapping[str, Any]]) -> tuple[dict[tuple[str, str, str], Decimal], list[dict[str, Any]]]:
    products: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    invalid = []
    for index, row in enumerate(baseline):
        key = (_text(row.get("whse_dept")), _text(row.get("goods_no")), _text(row.get("attr_val")))
        if not key[0] or not key[1]:
            invalid.append({"source_index": index, "reason": "Missing Product Department or Goods No"})
            continue
        rolls = _decimal(row.get("total_piece"))
        if rolls is None:
            invalid.append({"source_index": index, "product_dept": key[0], "goods_no": key[1], "color": key[2], "reason": "Invalid Rolls"})
            continue
        products[key] += rolls
    return dict(products), invalid


def _customer_products(products: Mapping[tuple[str, str, str], Decimal], purchased: Iterable[Mapping[str, Any]]) -> list[tuple[tuple[str, str, str], Decimal]]:
    pairs = {(_text(row.get("whse_dept")), _text(row.get("goods_no"))) for row in (purchased or ()) if isinstance(row, Mapping)}
    return [(key, rolls) for key, rolls in products.items() if key[:2] in pairs]


def build_coverage(
    baseline: Iterable[Mapping[str, Any]],
    mapping: Mapping[str, Any],
    plan: Mapping[str, Any] | None = None,
    *,
    generated_customer_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build independent customer population and exception evidence.

    ``relation_count`` counts distinct customer/product/color relations;
    ``customer_count`` counts distinct customer IDs.  Neither is inferred by
    summing rows in a package.  ``plan`` is optional and, when supplied, is
    checked against the independent expected population.
    """
    products, invalid_baseline = _product_pool(baseline)
    products_by_customer = mapping.get("productsByCustomer") or {}
    customer_info = mapping.get("customerInfo") or {}
    relation_rows: list[dict[str, Any]] = []
    customer_records: dict[str, dict[str, Any]] = {}
    matched_keys: set[tuple[str, str, str]] = set()
    expected_audit: Counter[tuple[Any, ...]] = Counter()
    assigned_customer_ids: set[str] = set()
    unassigned_customer_ids: set[str] = set()
    for raw_customer_id, purchased in sorted(products_by_customer.items(), key=lambda item: _text(item[0])):
        customer_id = _text(raw_customer_id)
        selected = _customer_products(products, purchased)
        if not selected:
            continue
        selected_keys = {key for key, _rolls in selected}
        matched_keys.update(selected_keys)
        info = customer_info.get(raw_customer_id) or customer_info.get(customer_id) or {}
        assignment = _assignment(info, mapping)
        reason = assignment["reason"]
        if reason:
            unassigned_customer_ids.add(customer_id)
        else:
            assigned_customer_ids.add(customer_id)
        record = customer_records.setdefault(customer_id, {
            "customer_id": customer_id,
            "customer_no": assignment["customer_no"],
            "customer_name": assignment["name"],
            "sales_owner": assignment["sales"],
            "account": assignment["account"],
            "personnel_region": assignment["personnel_region"],
            "reasons": [],
            "relations": [],
        })
        if reason and reason not in record["reasons"]:
            record["reasons"].append(reason)
        for (product_dept, goods_no, color), rolls in selected:
            item = {
                "customer_id": customer_id,
                "product_dept": product_dept,
                "goods_no": goods_no,
                "color": color,
                "customer_no": assignment["customer_no"],
                "customer_name": assignment["name"],
                "sales_owner": assignment["sales"],
                "account": assignment["account"],
                "personnel_region": assignment["personnel_region"],
                "exception": reason,
                "rolls": str(rolls),
                "status": "assigned" if reason is None else "unassigned",
            }
            relation_rows.append(item)
            record["relations"].append({"product_dept": product_dept, "goods_no": goods_no, "color": color, "rolls": str(rolls)})
            expected_audit[(product_dept, goods_no, color, assignment["customer_no"], assignment["name"], assignment["sales"], assignment["account"], reason or "")] += 1
    for product_dept, goods_no, color in sorted(set(products) - matched_keys):
        item = {
            "customer_id": "",
            "product_dept": product_dept,
            "goods_no": goods_no,
            "color": color,
            "customer_no": "",
            "customer_name": "",
            "sales_owner": "",
            "account": "",
            "personnel_region": "",
            "exception": "No Matched Customer",
            "rolls": str(products[(product_dept, goods_no, color)]),
            "status": "unmatched_product",
        }
        relation_rows.append(item)
        expected_audit[(product_dept, goods_no, color, "", "", "", "", "No Matched Customer")] += 1

    plan_checks = _check_plan(products, customer_records, assigned_customer_ids, plan) if plan is not None else {
        "provided": False,
        "passed": None,
    }
    by_product_dept: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in relation_rows:
        by_product_dept[row["product_dept"]].append(row)
    for rows in by_product_dept.values():
        rows.sort(key=lambda row: (_text(row.get("goods_no")), _text(row.get("color")), _text(row.get("customer_no")), _text(row.get("customer_id"))))
    reason_relation_counts = Counter(row["exception"] for row in relation_rows if row.get("exception"))
    reason_customer_sets: dict[str, set[str]] = defaultdict(set)
    for row in relation_rows:
        reason = row.get("exception")
        customer_id = _text(row.get("customer_id"))
        if reason and customer_id:
            reason_customer_sets[reason].add(customer_id)
    reason_customer_counts = {reason: len(customer_ids) for reason, customer_ids in sorted(reason_customer_sets.items())}
    relations_by_customer: dict[str, set[str]] = defaultdict(set)
    for row in relation_rows:
        if row.get("status") != "unmatched_product" and row.get("customer_id"):
            relations_by_customer[_text(row["customer_id"])].add(_text(row.get("product_dept")))
    cross_product_dept_customer_count = sum(len(departments) > 1 for departments in relations_by_customer.values())
    plan_customer_ids = _plan_customer_ids(plan)
    generated_ids = None if generated_customer_ids is None else {_text(value) for value in generated_customer_ids}
    if generated_ids is not None and (plan is None or generated_ids != plan_customer_ids):
        raise ValueError('CUSTOMER_GENERATED_POPULATION_MISMATCH')
    return {
        "version": 1,
        "relationship_window": dict(mapping.get('window') or {}),
        "population": {
            "candidate_product_grains": len(products),
            "matched_product_grains": len(matched_keys),
            "unmatched_product_grains": len(set(products) - matched_keys),
            "candidate_customer_ids": len(products_by_customer),
            "matched_customer_ids": len(customer_records),
            "mapped_customer_ids": len(customer_records),
            "assigned_customer_ids": len(assigned_customer_ids),
            "unassigned_customer_ids": len(unassigned_customer_ids),
            "planned_customer_ids": len(plan_customer_ids) if plan is not None else None,
            "generated_customer_ids": len(generated_ids) if generated_ids is not None else None,
            "relation_count": len([row for row in relation_rows if row["status"] != "unmatched_product"]),
            "unmatched_product_relation_count": len([row for row in relation_rows if row["status"] == "unmatched_product"]),
            "invalid_baseline_rows": len(invalid_baseline),
            "cross_product_dept_customer_count": cross_product_dept_customer_count,
        },
        "reason_counts": dict(sorted(reason_relation_counts.items())),
        "reason_relation_counts": dict(sorted(reason_relation_counts.items())),
        "reason_customer_counts": reason_customer_counts,
        "reason_counts_semantics": "reason_counts and reason_relation_counts count relation/product rows; reason_customer_counts counts distinct customer IDs and excludes No Matched Customer",
        "relations": relation_rows,
        "customers": sorted(customer_records.values(), key=lambda row: (_text(row.get("customer_no")), _text(row.get("customer_id")))),
        "unmatched_products": [row for row in relation_rows if row["status"] == "unmatched_product"],
        "audit_rows_expected": [
            {
                "product_dept": key[0], "goods_no": key[1], "color": key[2],
                "customer_no": key[3], "customer_name": key[4], "sales_owner": key[5],
                "account": key[6], "exception": key[7], "count": count,
            }
            for key, count in sorted(expected_audit.items(), key=lambda item: tuple(_text(value) for value in item[0]))
        ],
        "by_product_dept": dict(by_product_dept),
        "plan_checks": plan_checks,
        "independent": True,
    }


def _check_plan(products, customer_records, assigned_customer_ids, plan):
    packages = list((plan or {}).get("sales_packages") or [])
    expected_accounts = {record["account"] for record in customer_records.values() if record["account"] and not record["reasons"]}
    actual_accounts = [str(package.get("account") or "") for package in packages]
    duplicate_accounts = sorted(account for account, count in Counter(actual_accounts).items() if account and count > 1)
    expected_customer_ids = {cid for cid in assigned_customer_ids}
    actual_customer_ids = []
    duplicate_customer_ids: list[str] = []
    for package in packages:
        seen: set[str] = set()
        for customer in package.get("customers") or []:
            cid = _text(customer.get("customer_id"))
            if cid in seen:
                duplicate_customer_ids.append(cid)
            seen.add(cid)
            actual_customer_ids.append(cid)
    actual_customer_set = set(actual_customer_ids)
    cross_package_duplicates = sorted(cid for cid, count in Counter(actual_customer_ids).items() if cid and count > 1)
    missing_customers = sorted(expected_customer_ids - actual_customer_set)
    extra_customers = sorted(actual_customer_set - expected_customer_ids)
    product_mismatches = []
    metadata_mismatches = []
    for package in packages:
        account = _text(package.get("account"))
        for customer in package.get("customers") or []:
            cid = _text(customer.get("customer_id"))
            expected = customer_records.get(cid)
            if expected is None:
                continue
            if account != expected["account"] or _text(package.get("region")) != expected["personnel_region"]:
                metadata_mismatches.append(cid)
            expected_rolls: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
            for relation in expected["relations"]:
                expected_rolls[(relation["goods_no"], relation["color"])] += _decimal(relation["rolls"])
            expected_products = sorted([
                [goods_no, color, int(rolls.quantize(Decimal("1"), rounding=ROUND_HALF_UP))]
                for (goods_no, color), rolls in expected_rolls.items()
            ])
            actual_products = sorted(customer.get("products") or [])
            if expected_products != actual_products:
                product_mismatches.append(cid)
    return {
        "provided": True,
        "expected_package_accounts": len(expected_accounts),
        "actual_package_accounts": len(set(actual_accounts)),
        "duplicate_package_accounts": duplicate_accounts,
        "expected_customer_ids": len(expected_customer_ids),
        "actual_customer_ids": len(actual_customer_set),
        "missing_customer_ids": missing_customers,
        "extra_customer_ids": extra_customers,
        "duplicate_customer_ids": sorted(set(duplicate_customer_ids)),
        "cross_package_duplicate_customer_ids": cross_package_duplicates,
        "product_mismatches": sorted(set(product_mismatches)),
        "metadata_mismatches": sorted(set(metadata_mismatches)),
        "passed": not (duplicate_accounts or missing_customers or extra_customers or duplicate_customer_ids or cross_package_duplicates or product_mismatches or metadata_mismatches) and set(actual_accounts) == expected_accounts,
    }


def _plan_customer_ids(plan: Mapping[str, Any] | None) -> set[str]:
    if not plan:
        return set()
    return {
        _text(customer.get("customer_id"))
        for package in (plan.get("sales_packages") or [])
        for customer in (package.get("customers") or [])
        if _text(customer.get("customer_id"))
    }


def coverage_summary(coverage: Mapping[str, Any], product_dept: str | None = None) -> dict[str, Any]:
    """Return counts without collapsing relations into customer counts."""
    if product_dept is None:
        return dict(coverage.get("population") or {})
    rows = list((coverage.get("by_product_dept") or {}).get(product_dept, []))
    return {
        "product_dept": product_dept,
        "relation_count": sum(row.get("status") != "unmatched_product" for row in rows),
        "customer_id_count": len({_text(row.get("customer_id")) for row in rows if row.get("customer_id")}),
        "assigned_customer_id_count": len({_text(row.get("customer_id")) for row in rows if row.get("status") == "assigned"}),
        "unassigned_customer_id_count": len({_text(row.get("customer_id")) for row in rows if row.get("status") == "unassigned"}),
        "unmatched_product_grain_count": sum(row.get("status") == "unmatched_product" for row in rows),
        "reason_relation_counts": dict(Counter(row.get("exception") for row in rows if row.get("exception"))),
        "reason_customer_counts": {
            reason: len({_text(row.get("customer_id")) for row in rows if row.get("exception") == reason and row.get("customer_id")})
            for reason in {row.get("exception") for row in rows if row.get("exception")}
        },
        "note": "Customer IDs are distinct within this product department; do not sum department sheets for a global customer count.",
    }


def coverage_sheets(coverage: Mapping[str, Any], product_dept: str | None = None) -> list[tuple[str, list[str], list[list[Any]]]]:
    """Build audit-only sheets; callers can pass them to the existing XLSX writer."""
    rows = coverage.get("relations") or [] if product_dept is None else (coverage.get("by_product_dept") or {}).get(product_dept, [])
    rows = sorted(rows, key=lambda row: (_text(row.get("product_dept")), _text(row.get("goods_no")), _text(row.get("customer_no")), _text(row.get("customer_id"))))
    headers = ["Product Dept", "Personnel Region", "Goods No", "Color", "Customer No", "Customer", "Sales Owner", "Account", "Status", "Exception", "Rolls"]
    coverage_rows = [[row.get("product_dept"), row.get("personnel_region"), row.get("goods_no"), row.get("color"), row.get("customer_no"), row.get("customer_name"), row.get("sales_owner"), row.get("account"), row.get("status"), row.get("exception"), row.get("rolls")] for row in rows]
    exceptions = [row for row in rows if row.get("exception")]
    exception_rows = [[row.get("product_dept"), row.get("goods_no"), row.get("color"), row.get("customer_no"), row.get("customer_name"), row.get("sales_owner"), row.get("personnel_region"), row.get("exception")] for row in exceptions]
    unmatched = [row for row in rows if row.get("status") == "unmatched_product"]
    unmatched_rows = [[row.get("product_dept"), row.get("goods_no"), row.get("color"), row.get("rolls"), row.get("exception")] for row in unmatched]
    summary = coverage_summary(coverage, product_dept)
    labels = {
        'product_dept':'Product Department','relation_count':'Customer-product-color relations',
        'customer_id_count':'Matched customers (distinct)','assigned_customer_id_count':'Customers included in packages',
        'unassigned_customer_id_count':'Unassigned customers (distinct)','unmatched_product_grain_count':'Products without matched customers',
        'candidate_product_grains':'Candidate product groups','matched_product_grains':'Matched product groups',
        'unmatched_product_grains':'Unmatched product groups','candidate_customer_ids':'Candidate customers (distinct)',
        'matched_customer_ids':'Matched customers (distinct)','mapped_customer_ids':'Matched customers (distinct)',
        'assigned_customer_ids':'Assigned customers (distinct)','unassigned_customer_ids':'Unassigned customers (distinct)',
        'planned_customer_ids':'Customers included in plan','generated_customer_ids':'Customers in generated packages',
        'unmatched_product_relation_count':'Unmatched product rows','invalid_baseline_rows':'Invalid baseline rows',
        'cross_product_dept_customer_count':'Customers appearing in multiple product departments','note':'Count interpretation',
    }
    summary_rows = [[labels[key], value, "See Notes for counting rules"] for key, value in summary.items() if key in labels and key!='mapped_customer_ids']
    summary_rows += [[f'Unassigned customers: {reason}', count, 'Distinct customer IDs; one customer may have multiple product rows'] for reason,count in summary.get('reason_customer_counts',{}).items()]
    notes_rows = [
        ["Relation count", "Counts distinct customer-product-color relations; it is not a customer count."],
        ["Customer count", "Customer IDs are deduplicated within the selected product department."],
        ["Cross-department customers", "A customer may appear in more than one product department; department counts cannot be summed for a global customer count."],
        ["No Matched Customer", "This is an unmatched product grain and is excluded from customer counts."],
        ["Region fields", "Product Dept is the product/warehouse grain; Personnel Region is the assigned employee region."],
        ["Pool rolls", "Rolls on Coverage rows repeat the matched pool product quantity for each customer relationship. They are not allocated quantities or sales; do not sum them across customers."],
        ["Package membership", "Included/generated package counts do not establish platform acceptance, human receipt or onward customer sharing."],
        ["Master data", "Customer and personnel records are from the plan read snapshot, not historical records as of the inventory freeze."],
    ]
    window=coverage.get('relationship_window') or {}
    notes_rows.append(['Transaction window', f"{window['start']} <= delivery time < {window['end']}" if window.get('start') and window.get('end') else 'Window not recorded in the supplied mapping; not inferred from the inventory freeze.'])
    return [
        ("Summary", ["Metric", "Value", "Meaning"], summary_rows),
        ("Coverage", headers, coverage_rows),
        ("Exceptions", ["Product Dept", "Goods No", "Color", "Customer No", "Customer", "Sales Owner", "Personnel Region", "Exception"], exception_rows),
        ("Unmatched Products", ["Product Dept", "Goods No", "Color", "Rolls", "Reason"], unmatched_rows),
        ("Notes", ["Topic", "Meaning"], notes_rows),
    ]


__all__ = ["build_coverage", "coverage_summary", "coverage_sheets"]
