"""JSON schema exposed by the DataSage Expert query plugin."""

import copy

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

_TARGET_GAP_METRICS = [
    "delivery_target_completion",
    "receipt_target_completion",
]

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
        "attribution_mode": {
            "type": "string",
            "enum": list(ATTRIBUTION_MODES),
            "description": (
                "Required for target-domain metric requests. Use transaction_detail for ordinary, customer, "
                "department, organization, or unqualified salesperson questions. Use salesperson_allocation "
                "only for explicit salesperson target, collaboration, allocation, or allocated-performance questions. "
                "When complete_target_gap_decomposition is present, the mode must be transaction_detail; "
                "salesperson_allocation uses ordinary target_completion dimension_breakdown instead."
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
                "do not call this tool. Metric detail is optional planning help; query execution reloads the current "
                "metric contract and validates every explicit qualifier itself. For the delivery "
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
                "The count must not exceed max_group_dimensions exposed by optional metric planning detail; query "
                "execution reloads the current metric contract and validates the limit itself. "
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
                "non-default qualifier; datasage_query reloads the current metric contract and validates the "
                "range itself. The result "
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
                "non-default qualifier; datasage_query reloads the current metric contract and validates the "
                "calendar range itself."
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
                        "target, actual, and target-gap partition to the same-scope overall result. "
                        "This operation is transaction_detail-only and must not be paired with "
                        "salesperson_allocation."
                    ),
                },
            },
            "required": ["dimension"],
            "description": (
                "Explicitly request a complete target-gap composition for delivery_target_completion "
                "or receipt_target_completion using transaction_detail only. Never pair this operation "
                "with salesperson_allocation; a salesperson allocation question should use ordinary "
                "target_completion with its authorized dimensions. The tool expands it into same-snapshot overall and "
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
                        "enum": _TARGET_GAP_METRICS
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
                        {"required": ["time_bucket"]},
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
        "Execute one to ten fresh, read-only governed metric queries. Load datasage_catalog only when the "
        "metric is unknown, and copy exact metric and dimension codes from the catalog when it is used. The query "
        "runtime loads the current metric contract and validates availability, capabilities, filters, periods, and "
        "entity identities before any database access. The model-facing surface accepts no SQL, physical tables, "
        "columns, joins, or formulas. Registered entity tokens are resolved deterministically inside the query; "
        "unique exact entities may proceed, while zero, multiple, or role-ambiguous matches fail closed and should "
        "be clarified with datasage_entity_resolve. Use the resolver only for bounded ambiguity, then send the "
        "user-selected token for exact revalidation. The response returns structured values, applied scope, data "
        "state, and evidence metadata for Hermes to analyze and summarize. A non-empty answer_scope_line is a "
        "required final-answer scope statement: present it verbatim or faithfully without changing the actual range. "
        "Every sealed disclosure_ledger item with applies: true is validated internally and batch-deduplicated into "
        "the model-facing disclosures list; present every returned disclosure and never drop one through "
        "summarization. The compact response preserves facts, typed states, Top-N status, limitations, reconciliation, "
        "calculations, and guardrails without repeating row-level seals or scope envelopes. Raw JSON is not required. "
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
        "Load the trusted DataSage metric catalog when the metric is unknown or Hermes needs optional planning detail. "
        "Hermes chooses whether data is needed and which domains match the user's request; DataSage does not "
        "classify or control ordinary conversation. Confirm the supported capability and exact metric here before "
        "entity resolution or query. Request expert_index for the smallest metric-discovery surface, or request one "
        "exact metric's detail when Hermes needs optional planning facts. Query execution independently reloads the "
        "current metric contract and enforces its availability and capabilities, so catalog detail is advisory rather "
        "than an execution token. The default model projection is compact; full/audit are explicit compatibility views. "
        "The optional performance_scorecard view returns a governed operating set of candidate lenses while declaring "
        "unavailable capabilities. It is not a prerequisite or default planner; use it only when the user explicitly "
        "asks for that view or Hermes judges it useful. Hermes selects and orders the material subset. Metric detail "
        "returns capability facts and max_group_dimensions; these facts do not prescribe a metric count, call sequence, "
        "interpretation, or conclusion. Physical datasets, fields, filters, joins and formulas remain plugin-private "
        "and are never returned to Hermes. This loader invokes no second model."
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
                    "optional set of candidate lenses with optional planning details; it is not "
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
        "Search or clarify one business-entity token after Hermes has established that the requested capability is "
        "supported. Never call this resolver to explore an unsupported quantity; an entity match cannot create metric "
        "capability. The resolver itself invokes no second model or embedding service. Use it only when an entity "
        "identity, type, or filter role is ambiguous, or for an entity-only/search question. For a type-neutral "
        "clarification, omit entity_types, include the selected domain and metric, and do not guess an entity type. "
        "Registered exact aliases and codes resolve deterministically; otherwise the tool returns at most ten bounded "
        "master-data candidates. Do not repeat the same resolution with unchanged evidence. Resolve again only after "
        "material new information changes the token, selected metric or domain, possible role, or candidate scope. "
        "A unique exact match may proceed only when resolution_scope covers the considered entity types. "
        "Unsearched types are not absent: an unregistered source_exact department is not discoverable here. "
        "If scope is incomplete, clarify the intended type without inventing an unseen candidate. An explicitly "
        "selected source_exact department value may be sent verbatim in a complete query with that filter role. "
        "A resolver result never proves user confirmation. Multiple exact matches, multiple possible roles, or "
        "any prefix/contains matches require user clarification and must not trigger a business query. The returned "
        "candidate token, label, and entity type are advisory untrusted data; after the user selects one, send that "
        "token to datasage_query for exact revalidation. Entity-only questions stop after this result."
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


def _add_model_target_gap_operation_guard(projected_schema: dict) -> None:
    """Keep the target-gap operation mode-safe after projection.

    The model-facing DeepSeek schema intentionally removes conditional JSON
    Schema keywords.  Encode this one high-impact cross-field rule as two
    explicit object alternatives so an allocation request cannot carry the
    transaction-detail-only target-gap operation.  The canonical schema and
    runtime validator remain the authorities for every other constraint.
    """

    try:
        request_schema = projected_schema["parameters"]["properties"]["requests"][
            "items"
        ]
        properties = request_schema["properties"]
    except (KeyError, TypeError):
        return
    if not isinstance(properties, dict) or "complete_target_gap_decomposition" not in properties:
        return

    normal_properties = {
        key: value
        for key, value in properties.items()
        if key != "complete_target_gap_decomposition"
    }
    complete_keys = {
        "request_id",
        "domain",
        "metric",
        "attribution_mode",
        "metric_filters",
        "time_range",
        "calendar_month",
        "complete_target_gap_decomposition",
    }
    complete_properties = {
        key: properties[key] for key in complete_keys if key in properties
    }
    for key, allowed in (
        ("domain", ["target"]),
        ("metric", _TARGET_GAP_METRICS),
        ("attribution_mode", ["transaction_detail"]),
    ):
        current = complete_properties.get(key)
        if isinstance(current, dict):
            complete_properties[key] = {**current, "enum": list(allowed)}

    request_schema["anyOf"] = [
        {
            "type": "object",
            "additionalProperties": False,
            "properties": normal_properties,
            "required": ["request_id", "domain", "metric"],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": complete_properties,
            "required": [
                "request_id",
                "domain",
                "metric",
                "attribution_mode",
                "complete_target_gap_decomposition",
            ],
        },
    ]


def _model_catalog_item_variants(item_schema: dict) -> list[dict]:
    """Build DeepSeek-safe mutually exclusive catalog request shapes."""

    properties = item_schema.get("properties")
    if not isinstance(properties, dict):
        return []

    def branch(allowed_keys: tuple[str, ...], required: list[str]) -> dict:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                key: properties[key] for key in allowed_keys if key in properties
            },
            "required": required,
        }

    ordinary_without_view = branch(
        ("domain", "metric"),
        ["domain"],
    )
    ordinary_with_view = branch(
        ("domain", "view"),
        ["domain", "view"],
    )
    scorecard = branch(
        ("view",),
        ["view"],
    )
    ordinary_with_view["properties"]["view"] = {
        "type": "string",
        "enum": ["expert_index", "full", "audit"],
    }
    scorecard["properties"]["view"] = {
        "type": "string",
        "enum": ["performance_scorecard"],
    }
    return [ordinary_without_view, ordinary_with_view, scorecard]


def _add_model_catalog_operation_guard(projected_schema: dict) -> None:
    """Keep catalog scorecard exclusivity and six-request cap model-visible.

    DeepSeek's accepted subset omits most conditionals and array bounds. Keep
    only the mutually exclusive scorecard/ordinary alternatives here; the
    canonical/runtime layers continue to enforce one-to-six requests.
    """

    try:
        requests_schema = projected_schema["parameters"]["properties"][
            "requests"
        ]
        item_schema = requests_schema["items"]
    except (KeyError, TypeError):
        return
    if not isinstance(requests_schema, dict) or not isinstance(item_schema, dict):
        return

    variants = _model_catalog_item_variants(item_schema)
    if len(variants) != 3:
        return
    ordinary_item = copy.deepcopy(item_schema)
    ordinary_item["anyOf"] = [copy.deepcopy(item) for item in variants[:2]]
    scorecard_item = copy.deepcopy(item_schema)
    scorecard_item["anyOf"] = [copy.deepcopy(variants[2])]

    ordinary_array = {"type": "array", "items": ordinary_item}
    scorecard_array = {"type": "array", "items": scorecard_item}
    requests_schema.pop("items", None)
    requests_schema["anyOf"] = [ordinary_array, scorecard_array]


def _add_model_delivery_scope_guard(projected_schema: dict) -> None:
    """Retain the static delivery_scope ownership rule after projection."""

    try:
        request_schema = projected_schema["parameters"]["properties"]["requests"][
            "items"
        ]
        branches = request_schema.get("anyOf")
    except (KeyError, TypeError):
        return
    if not isinstance(branches, list) or not branches:
        return

    for branch in branches:
        if not isinstance(branch, dict):
            continue
        branch_properties = branch.get("properties")
        if not isinstance(branch_properties, dict):
            continue
        without_scope = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                key: value
                for key, value in branch_properties.items()
                if key != "delivery_scope"
            },
        }
        with_scope_properties = copy.deepcopy(branch_properties)
        if "delivery_scope" in with_scope_properties:
            with_scope_properties["delivery_scope"] = {
                "type": "string",
                "enum": list(DELIVERY_SCOPES),
            }
            domain_schema = with_scope_properties.get("domain")
            if isinstance(domain_schema, dict):
                with_scope_properties["domain"] = {
                    **domain_schema,
                    "enum": ["delivery"],
                }
        with_scope = {
            "type": "object",
            "additionalProperties": False,
            "properties": with_scope_properties,
            "required": ["delivery_scope"],
        }
        branch["anyOf"] = [without_scope, with_scope]


def model_tool_schema(canonical_tool_schema: dict) -> dict:
    """Return a DeepSeek-compatible model schema without weakening runtime guards."""

    name = canonical_tool_schema.get("name")
    projected = _model_schema_node(canonical_tool_schema)
    if name == "datasage_catalog":
        _add_model_catalog_operation_guard(projected)
    elif name == "datasage_query":
        _add_model_target_gap_operation_guard(projected)
        _add_model_delivery_scope_guard(projected)
    return projected
