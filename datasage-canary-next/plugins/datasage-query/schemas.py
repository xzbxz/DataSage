"""JSON schema exposed by the DataSage Expert query plugin."""

from .capability_contract import (
    ATTRIBUTION_MODES,
    DELIVERY_SCOPES,
    ENTITY_RESOLVE_DEFAULT_LIMIT,
    ENTITY_RESOLVE_HARD_LIMIT,
    ENTITY_TYPES,
    MATCHED_ELAPSED_COVERAGE,
    INVENTORY_SCOPES,
    PREVIOUS_PERIOD_COMPARISON,
    PUBLIC_COMPARISON_KINDS,
    SNAPSHOT_MONTHS_BEFORE_COMPARISON,
    SUPPORTED_DOMAINS,
    YEAR_OVER_YEAR_COMPARISON,
    query_request_schema_conditions,
)
from . import request_contract

DOMAINS = list(SUPPORTED_DOMAINS)

SCALAR = {
    "oneOf": [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
    ]
}

REQUEST = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "request_id": {
            **request_contract.REQUEST_ID.schema(),
            "description": "Stable ID for one sub-question, such as q1 or inventory_1.",
        },
        "domain": {
            "type": "string",
            "enum": DOMAINS,
            "description": "Business domain selected from datasage_catalog.",
        },
        "detail_receipt": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
            "pattern": "^[0-9a-f]{64}$",
            "description": (
                "Opaque receipt copied unchanged from the detail_receipt field of this exact metric's "
                "successful datasage_catalog detail result. It is required "
                "unless the selected metric explicitly supports an exact default lookup and this request "
                "contains no explicit business qualifier. The query runtime revalidates the receipt against "
                "the current catalog contract and the requested governed capabilities before any database access."
            ),
        },
        "attribution_mode": {
            "type": "string",
            "enum": list(ATTRIBUTION_MODES),
            "description": (
                "Required for target-domain metric requests. Use transaction_detail for ordinary, customer, "
                "department, organization, or unqualified salesperson questions. Use salesperson_allocation "
                "only for explicit salesperson target, collaboration, allocation, or allocated-performance questions."
            ),
        },
        "delivery_scope": {
            "type": "string",
            "enum": list(DELIVERY_SCOPES),
            "description": (
                "Delivery-domain metric scope. Omit or use default_net for an unqualified net-delivery request; "
                "use explicit_gross only when the user explicitly asks for gross/before-return delivery; use "
                "order_delivery_alignment on every paired request in the governed order-versus-delivery comparison. "
                "That value authorizes gross delivery only for the registered gross metric. The query tool validates "
                "this field and never infers it from free text."
            ),
        },
        "inventory_scope": {
            "type": "string",
            "enum": list(INVENTORY_SCOPES),
            "description": (
                "Inventory-domain metric scope. Omit for the metric default. total includes statuses 1/2/3; "
                "on_hand includes statuses 1/2; available uses status 1 and excludes missing and defective "
                "warehouses; allocated uses status 2; in_transit uses status 3. The query tool applies the "
                "registered filters and never infers them from free text."
            ),
        },
        "metric": {
            **request_contract.METRIC_CODE.schema(),
            "description": (
                "Exact metric code copied from the selected domain catalog; required. Never "
                "invent a code, derive a new metric, or try code synonyms. If the requested meaning has no exact code, "
                "do not call this tool. Load the selected metric detail first when expert_index returns "
                "requires_metric_detail: true, when exact_default_lookup_supported is false or missing, or when the "
                "request has any explicit business qualifier. An empty dimensions: [] value is not a business "
                "qualifier. Direct query is allowed only for an exact governed default whose "
                "expert_index exact_default_lookup_supported value is true and which has no explicit qualifier. For the delivery "
                "domain, an unqualified delivery/outbound amount, quantity, or count is net delivery. A gross "
                "delivery metric is permitted only when the user explicitly asks for gross delivery or a value "
                "before returns. Words such as raw, detail, table, dataset, or original do not select gross scope."
            ),
        },
        "dimensions": {
            "type": "array",
            "maxItems": request_contract.MAX_GROUP_DIMENSIONS,
            "uniqueItems": True,
            "items": request_contract.DIMENSION_CODE.schema(),
            "description": (
                "Governed dimension codes for the metric request. A code may use a fact field or a predeclared "
                "many-to-one master enrichment; never send table names or join keys. Include only grouping or "
                "ranking dimensions explicitly requested by the user; an overall total has no dimensions. Words "
                "The count must not exceed max_group_dimensions returned by the selected metric detail. "
                "such as raw, detail, source, table, or original are not dimensions. For an original-currency metric, "
                "use the currency dimension when the user did not select exactly one currency."
            ),
        },
        "metric_filters": {
            "type": "object",
            "maxProperties": request_contract.MAX_METRIC_FILTERS,
            "additionalProperties": {
                "oneOf": [
                    *SCALAR["oneOf"],
                    {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": request_contract.MAX_FILTER_VALUES,
                        "items": SCALAR,
                    },
                ]
            },
            "description": (
                "Governed dimension-code filters for the metric request. An original-currency metric must either filter "
                "exactly one currency here or include the currency dimension; never combine currencies. Across "
                "the entire requests batch, use at most ten distinct customer, salesperson, product, warehouse, "
                "or supplier tokens that require master-data preflight; registered department aliases do not use "
                "that budget."
            ),
        },
        "time_range": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "start": {
                    "type": "string",
                    "pattern": r"^\d{4}-\d{2}-\d{2}$",
                },
                "end": {
                    "type": "string",
                    "pattern": r"^\d{4}-\d{2}-\d{2}$",
                },
            },
            "required": ["start", "end"],
            "description": (
                "Start-inclusive and end-exclusive governed metric range using YYYY-MM-DD boundaries. "
                "Use calendar_month for one typed full calendar month. Supplying time_range is an explicit, "
                "non-default qualifier and requires the selected metric detail before datasage_query. The result "
                "returns calendar period state and coverage; its query-date observation is not a source freshness "
                "watermark."
            ),
        },
        "calendar_month": {
            "type": "string",
            "pattern": r"^(?!0000-)(?!9999-12$)[0-9]{4}-(?:0[1-9]|1[0-2])$",
            "description": (
                "Typed full calendar month. The runtime deterministically expands it to a start-inclusive, "
                "end-exclusive range from the first day of this month to the first day of the next month. "
                "The returned period state distinguishes an elapsed month from a month still in progress; "
                "the latter is not a complete-period comparison. "
                "Use either calendar_month or time_range, never both. Supplying calendar_month is an explicit, "
                "non-default qualifier and requires the selected metric detail before datasage_query."
            ),
        },
        "time_bucket": {
            "type": "string",
            "enum": ["day", "month"],
            "description": "Tool-controlled time grouping for a metric with an explicit or governed time field.",
        },
        "comparison": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(PUBLIC_COMPARISON_KINDS),
                },
                "months": {"type": "integer", "minimum": 1, "maximum": 24},
                "coverage": {
                    "type": "string",
                    "enum": [MATCHED_ELAPSED_COVERAGE],
                },
            },
            "required": ["kind"],
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "kind": {"const": SNAPSHOT_MONTHS_BEFORE_COMPARISON}
                        }
                    },
                    "then": {
                        "required": ["months"],
                        "not": {"required": ["coverage"]},
                    },
                },
                {
                    "if": {
                        "properties": {
                            "kind": {"const": PREVIOUS_PERIOD_COMPARISON}
                        }
                    },
                    "then": {
                        "not": {
                            "anyOf": [
                                {"required": ["months"]},
                                {"required": ["coverage"]},
                            ]
                        }
                    },
                },
                {
                    "if": {
                        "properties": {
                            "kind": {"const": YEAR_OVER_YEAR_COMPARISON}
                        }
                    },
                    "then": {
                        "required": ["coverage"],
                        "properties": {
                            "coverage": {"const": MATCHED_ELAPSED_COVERAGE}
                        },
                        "not": {"required": ["months"]},
                    },
                },
            ],
            "description": (
                "Governed comparison. previous_period requires exactly one of time_range or calendar_month; "
                "year_over_year with coverage=matched_elapsed aligns the same calendar window one year earlier "
                "and clips an in-progress current window and its prior-year window to the same elapsed coverage; "
                "snapshot_months_before uses the latest monthly snapshot and a month offset. A previous_period "
                "result already returns current, prior-period, absolute change, and change rate for that metric and "
                "scope; do not add explicit current/prior requests solely to duplicate them. Other metrics or "
                "scopes remain selectable."
            ),
        },
        "decomposition_of_request_id": {
            **request_contract.REQUEST_ID.schema(),
            "description": (
                "For a governed dimension change decomposition, reference the "
                "same-batch overall comparison request. The tool authorizes a "
                "reconciled change relationship only when metric, period, unit, "
                "canonical scope and the full non-truncated delta sum agree."
            ),
        },
        "complete_change_decomposition": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "dimension": {
                    **request_contract.DIMENSION_CODE.schema(),
                    "description": (
                        "Exact governed dimension selected by Hermes for one complete "
                        "change partition. The tool expands this semantic operation into "
                        "a same-scope overall comparison and one full-partition attempt."
                    ),
                },
            },
            "required": ["dimension"],
            "description": (
                "Explicitly request a complete change decomposition. When comparison is omitted, the operation "
                "defaults to previous_period. An explicit comparison may use previous_period or matched-elapsed "
                "year_over_year with one period, or snapshot_months_before with no explicit period. "
                "Do not combine it with dimensions, order_by, limit, or "
                "decomposition_of_request_id. The tool never infers this operation or "
                "chooses its metric or dimension. It already includes the same-scope overall comparison; do not "
                "add an identical overall request solely to duplicate it. Other evidence remains selectable."
            ),
        },
        "complete_target_gap_decomposition": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "dimension": {
                    "type": "string",
                    "enum": ["customer", "department", "organization"],
                    "description": (
                        "Governed transaction-detail dimension used to reconcile a complete "
                        "target, actual, and target-gap partition to the same-scope overall result."
                    ),
                },
            },
            "required": ["dimension"],
            "description": (
                "Explicitly request a complete target-gap composition for delivery_target_completion "
                "or receipt_target_completion. The tool expands it into same-snapshot overall and "
                "full-partition queries, reconciles target, actual, and gap amounts independently, "
                "and never sums completion rates or authorizes causal claims."
            ),
        },
        "order_by": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "field": {
                    "type": "string",
                    "pattern": request_contract.ORDER_BY_FIELD_PATTERN,
                    "description": (
                        "For metric-value ranking use metric_value, not the metric code. A governed dimension "
                        "output or period may be used only when intentionally sorting that output."
                    ),
                },
                "direction": {
                    "type": "string",
                    "enum": list(request_contract.ORDER_BY_DIRECTIONS),
                },
            },
            "required": list(request_contract.ORDER_BY_FIELDS),
            "description": (
                "Ascending or descending output ordering. Metric codes are accepted as compatibility aliases "
                "and normalized by the tool to metric_value."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": request_contract.PUBLIC_ROW_LIMIT_MIN,
            "maximum": request_contract.PUBLIC_ROW_LIMIT_MAX,
            "description": "Maximum returned rows; the environment cap can reduce it further.",
        },
    },
    "required": [
        "request_id",
        "domain",
        "metric",
    ],
    "allOf": [
        {
            "not": {
                "required": ["time_range", "calendar_month"],
            },
        },
        {
            "if": {
                "required": ["comparison"],
                "properties": {
                    "comparison": {
                        "required": ["kind"],
                        "properties": {
                            "kind": {
                                "enum": [
                                    PREVIOUS_PERIOD_COMPARISON,
                                    YEAR_OVER_YEAR_COMPARISON,
                                ]
                            }
                        },
                    }
                },
            },
            "then": {
                "oneOf": [
                    {"required": ["time_range"]},
                    {"required": ["calendar_month"]},
                ],
            },
        },
        {
            "if": {
                "required": ["comparison"],
                "properties": {
                    "comparison": {
                        "required": ["kind"],
                        "properties": {
                            "kind": {
                                "const": SNAPSHOT_MONTHS_BEFORE_COMPARISON
                            }
                        },
                    }
                },
            },
            "then": {
                "not": {
                    "anyOf": [
                        {"required": ["time_range"]},
                        {"required": ["calendar_month"]},
                    ],
                },
            },
        },
        {
            "if": {"required": ["complete_change_decomposition"]},
            "then": {
                "anyOf": [
                    {
                        "required": ["comparison"],
                        "properties": {
                            "comparison": {
                                "properties": {
                                    "kind": {
                                        "const": SNAPSHOT_MONTHS_BEFORE_COMPARISON
                                    }
                                }
                            }
                        },
                    },
                    {
                        "oneOf": [
                            {"required": ["time_range"]},
                            {"required": ["calendar_month"]},
                        ]
                    },
                ],
                "not": {
                    "anyOf": [
                        {"required": ["dimensions"]},
                        {"required": ["order_by"]},
                        {"required": ["limit"]},
                        {"required": ["decomposition_of_request_id"]},
                    ]
                },
            },
        },
        {
            "if": {"required": ["complete_target_gap_decomposition"]},
            "then": {
                "properties": {
                    "domain": {"const": "target"},
                    "metric": {
                        "enum": [
                            "delivery_target_completion",
                            "receipt_target_completion",
                        ]
                    },
                    "attribution_mode": {"const": "transaction_detail"},
                },
                "required": ["attribution_mode"],
                "not": {
                    "anyOf": [
                        {"required": ["dimensions"]},
                        {"required": ["order_by"]},
                        {"required": ["limit"]},
                        {"required": ["comparison"]},
                        {"required": ["decomposition_of_request_id"]},
                        {"required": ["complete_change_decomposition"]},
                    ]
                },
            },
        },
        *query_request_schema_conditions(),
    ],
}

CALCULATION = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "calculation_id": {
            **request_contract.REQUEST_ID.schema(),
            "description": "Unique ID for one governed arithmetic observation.",
        },
        "operation": {
            "type": "string",
            "enum": list(request_contract.CALCULATION_OPERATIONS),
            "description": (
                "Closed arithmetic operation over two successful, untruncated scalar request results. "
                "difference and ratio require the same registered metric, governed non-time scope, filters, "
                "and unit; their periods may differ, and ratio requires a nonzero denominator. Arithmetic remains "
                "visible when calendar coverage differs, but period_compatibility then prevents treating it as "
                "a formal period trend. share requires "
                "the same metric, period, and unit; left must be a proven strict additive-partition subset of "
                "right, right must be positive, and 0 <= left <= right. Free-form formulas are never accepted."
            ),
        },
        "left_request_id": {
            **request_contract.REQUEST_ID.schema(),
        },
        "right_request_id": {
            **request_contract.REQUEST_ID.schema(),
        },
    },
    "required": [
        "calculation_id",
        "operation",
        "left_request_id",
        "right_request_id",
    ],
}

DATASAGE_QUERY = {
    "name": "datasage_query",
    "description": (
        "Execute one to ten fresh, read-only governed metric queries. Load the relevant domains with "
        "datasage_catalog first and copy exact metric and dimension codes from that catalog. The caller may query "
        "directly only when the selected expert_index metric has exact_default_lookup_supported: "
        "true and the request has no explicit business qualifier. Empty dimensions: [] does not count as a qualifier. "
        "If that flag is false or missing, or if calendar_month, "
        "time_range, dimensions, filters, an entity, comparison, decomposition, or ranking is explicit, load the "
        "selected metric detail before calling datasage_query and copy that result's detail_receipt unchanged. "
        "The runtime rejects a missing, stale, tampered, wrong-metric, or capability-incompatible receipt before any "
        "database access. The model-facing surface accepts no SQL, physical "
        "tables, columns, joins, or formulas. Registered entity tokens may be "
        "provided as metric filters and are resolved deterministically inside the query. The response returns "
        "structured values, applied scope, data state, and evidence metadata for Hermes to analyze and summarize. "
        "A non-empty answer_scope_line is a required final-answer scope statement: present it verbatim or faithfully "
        "without changing the actual range. Every sealed disclosure_ledger item with applies: true is validated "
        "internally and batch-deduplicated into the model-facing disclosures list; present every returned disclosure "
        "and never drop one through summarization. The compact response preserves facts, typed states, Top-N status, "
        "limitations, reconciliation, calculations, and guardrails without repeating "
        "row-level seals or scope envelopes. Raw JSON is not required. "
        "Unavailable data affects only this tool call and does not control the surrounding conversation."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "requests": {
                "type": "array",
                "minItems": 1,
                "maxItems": request_contract.PUBLIC_REQUEST_LIMIT,
                "items": REQUEST,
                "description": (
                    "Independent governed requests or explicit semantic operations selected by Hermes for the "
                    "current answer. Batch the adaptively chosen evidence here in one tool call, preserve question "
                    "order, and give each input a unique request_id. A complete_change_decomposition or "
                    "complete_target_gap_decomposition operation expands to two physical requests that "
                    "count against the same ten-request execution budget."
                ),
            },
            "calculations": {
                "type": "array",
                "minItems": 1,
                "maxItems": request_contract.PUBLIC_CALCULATION_LIMIT,
                "items": CALCULATION,
                "description": (
                    "Optional governed difference, ratio, or share operations. Each operand must reference a "
                    "successful scalar request_id from this batch. Results are derived observations, never "
                    "registered KPIs, structural contributions, or causal evidence."
                ),
            },
        },
        "required": ["requests"],
    },
}


DATASAGE_CATALOG = {
    "name": "datasage_catalog",
    "description": (
        "Load the trusted DataSage metric catalog needed to plan an internal business-data query. "
        "Hermes chooses whether data is needed and which domains match the user's request; DataSage does not "
        "classify or control ordinary conversation. Request expert_index for the smallest metric-discovery surface, "
        "then request detail for a selected metric when required. Every expert-index metric declares "
        "requires_metric_detail. Direct "
        "query is allowed only when exact_default_lookup_supported is true and no explicit business qualifier is "
        "present; empty dimensions: [] does not count as a qualifier. "
        "Otherwise request detail only for the selected metric before query. The default model projection is compact; "
        "full/audit are explicit compatibility views. The optional performance_scorecard view returns a governed "
        "operating set of candidate lenses while declaring unavailable capabilities. It is not a prerequisite or "
        "default planner; use it only when the user explicitly asks for that view or Hermes judges it useful. Hermes selects and orders the material "
        "subset. Metric detail returns capability facts and max_group_dimensions; these facts do not prescribe a "
        "metric count, call sequence, interpretation, or conclusion. Physical datasets, fields, filters, "
        "joins and formulas remain plugin-private and are never returned to Hermes. This loader invokes no second model."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "requests": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "domain": {
                            "type": "string",
                            "enum": DOMAINS,
                            "description": "Governed business domain selected by Hermes.",
                        },
                        "metric": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 100,
                            "description": (
                                "Exact metric code from a prior expert index or domain summary. "
                                "Do not combine metric with view."
                            ),
                        },
                        "view": {
                            "type": "string",
                            "enum": ["expert_index", "full", "audit", "performance_scorecard"],
                            "description": (
                                "Use expert_index for compact discovery. Omit view for the default compact "
                                "model projection. Use full or audit only for explicit compatibility/audit "
                                "inspection. Use performance_scorecard without domain or metric only for the "
                                "optional governed cross-domain candidate lenses."
                            ),
                        },
                    },
                    "oneOf": [
                        {
                            "required": ["domain"],
                            "properties": {
                                "view": {"enum": ["expert_index", "full", "audit"]},
                            },
                        },
                        {
                            "properties": {
                                "view": {"const": "performance_scorecard"},
                            },
                            "required": ["view"],
                            "not": {
                                "anyOf": [
                                    {"required": ["domain"]},
                                    {"required": ["metric"]},
                                ]
                            },
                        },
                    ],
                    "allOf": [
                        {
                            "not": {
                                "required": ["metric", "view"],
                            }
                        }
                    ],
                },
                "description": (
                    "One request per relevant domain, or one cross-domain view=performance_scorecard request. "
                    "Use expert_index for discovery, include one exact metric code for detail, and use explicit "
                    "full/audit only when the legacy summary is genuinely required. The scorecard returns a "
                    "optional set of candidate lenses plus independently sealed metric-detail receipts; it is not "
                    "required before ordinary metric discovery or query planning."
                ),
            },
        },
        "required": ["requests"],
        "oneOf": [
            {
                "properties": {
                    "requests": {
                        "maxItems": 1,
                        "items": {
                            "properties": {
                                "view": {"const": "performance_scorecard"},
                            },
                            "required": ["view"],
                        },
                    },
                },
            },
            {
                "properties": {
                    "requests": {
                        "items": {
                            "not": {
                                "properties": {
                                    "view": {"const": "performance_scorecard"},
                                },
                                "required": ["view"],
                            },
                        },
                    },
                },
            },
        ],
    },
}


DATASAGE_ENTITY_RESOLVE = {
    "name": "datasage_entity_resolve",
    "description": (
        "Search or clarify one business-entity token only after datasage_catalog has confirmed that the exact "
        "requested business quantity and operation are supported. Never call this resolver to explore an unsupported "
        "quantity or before capability coverage; an entity match cannot create metric capability. "
        "The resolver itself invokes no second model or embedding "
        "service, while the main Hermes Agent normally continues after receiving its result. This is not a mandatory "
        "pre-query hop: ordinary explicitly labeled metric filters continue through exact query preflight inside "
        "datasage_query. After the catalog has confirmed a metric, a type-neutral pre-query clarification is also "
        "allowed when the user's token is truly unlabeled and multiple metric-compatible entity types or roles remain "
        "reasonable. For that bounded case, omit entity_types, include the selected domain and metric, and resolve "
        "before a business query rather than guessing customer or another type. Registered exact aliases and codes "
        "resolve deterministically; otherwise the tool returns at most ten bounded master-data candidates. Call it "
        "only for that type-neutral clarification, an entity-only/search question, or after query preflight reports "
        "an unresolved or ambiguous identity. Do not repeat the same resolution with unchanged evidence. Resolve "
        "again only after material new information changes the token, selected metric or domain, possible role, or "
        "candidate scope and the result could change the next action. "
        "A unique exact match may be used only with one returned filter role. A metric-incompatible entity fails "
        "closed; multiple exact matches, multiple possible roles, or any prefix/contains matches require user "
        "clarification and must not trigger a business query. Entity-only questions stop after this result."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "token": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": "The unresolved name, code, alias, or short token exactly as the user supplied it.",
            },
            "entity_types": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(ENTITY_TYPES),
                "uniqueItems": True,
                "items": {"type": "string", "enum": list(ENTITY_TYPES)},
                "description": (
                    "Use only an explicit user-labeled entity type; never guess one from the token. Omit entity_types "
                    "for the bounded type-neutral clarification when the token is truly unlabeled."
                ),
            },
            "domain": {
                "type": "string",
                "enum": DOMAINS,
                "description": "Known business domain, used only to return valid semantic filter roles.",
            },
            "metric": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "description": "Optional selected metric code; requires domain and narrows the valid filter role.",
            },
            "attribution_mode": {
                "type": "string",
                "enum": list(ATTRIBUTION_MODES),
                "description": "Optional governed ledger path for path-dependent metrics; requires metric and narrows the valid filter role.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": ENTITY_RESOLVE_HARD_LIMIT,
                "default": ENTITY_RESOLVE_DEFAULT_LIMIT,
            },
        },
        "required": ["token"],
        "dependencies": {
            "metric": ["domain"],
            "attribution_mode": ["metric"],
        },
    },
}


# DeepSeek's documented function-calling schema subset accepts ``anyOf`` but
# rejects several otherwise-valid JSON Schema keywords used by the canonical
# DataSage contracts (for example string/array length bounds, ``oneOf``,
# ``allOf``, conditionals, and dependencies).  Keep the canonical schemas
# above as the single source of truth for runtime validation and tests; expose
# only this lossless-at-runtime projection to the model.  The handlers still
# enforce every omitted constraint before entitlement, readiness, or DB I/O.
_MODEL_SCHEMA_OMIT_KEYS = frozenset(
    {
        "$schema",
        "allOf",
        "contains",
        "dependencies",
        "dependentRequired",
        "else",
        "if",
        "maxItems",
        "maxLength",
        "maxProperties",
        "minItems",
        "minLength",
        "minProperties",
        "not",
        "patternProperties",
        "prefixItems",
        "propertyNames",
        "then",
        "uniqueItems",
    }
)


def _model_schema_node(value):
    if isinstance(value, list):
        return [_model_schema_node(item) for item in value]
    if not isinstance(value, dict):
        return value

    projected = {}
    for key, item in value.items():
        if key in _MODEL_SCHEMA_OMIT_KEYS:
            continue
        if key == "oneOf":
            projected["anyOf"] = _model_schema_node(item)
            continue
        if key == "const":
            projected.setdefault("enum", [item])
            continue
        projected[key] = _model_schema_node(item)
    return projected


def model_tool_schema(canonical_tool_schema: dict) -> dict:
    """Return a DeepSeek-compatible model schema without weakening runtime guards."""

    return _model_schema_node(canonical_tool_schema)
