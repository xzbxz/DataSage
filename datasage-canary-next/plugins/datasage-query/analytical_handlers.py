"""Static integration records for analytical handlers, not a query language.

Business values belong to semantics. Each migrated handler owns its explicit
public-field permission independently of fact-unit annotations. Unmigrated SQL
shapes continue through the existing dispatcher; a migrated shape has no fallback.
"""
from dataclasses import dataclass
from importlib import import_module
from types import MappingProxyType


@dataclass(frozen=True)
class QueryHandler:
    forbidden_parameters: tuple[str, ...] = ()
    baseline_parameters: bool = False
    error_code: str = 'INVALID_PLAN'
    parameter_message: str = '该查询不支持此参数。'
    parameter_error_codes: tuple[tuple[str, str], ...] = ()
    module: str | None = None
    builder: str | None = None
    observer: str | None = None
    public_fields: str | None = None
    time_source: str | None = None
    time_projection: str | None = None
    time_description: str | None = None
    consistent_snapshot: bool = False

    def resolve(self, attribute):
        """Load only statically registered code; never accept names from requests."""
        name = getattr(self, attribute)
        if not self.module or not name:
            raise ValueError('Handler integration is not registered: ' + attribute)
        return getattr(import_module('.' + self.module, __package__), name)

    def validate_parameters(self, request):
        from .analytical_queries import AnalysisQueryError
        for field in self.forbidden_parameters:
            if request.get(field) is not None:
                raise AnalysisQueryError(
                    dict(self.parameter_error_codes).get(field, self.error_code), self.parameter_message,
                    path=field, hint='该查询不接受 ' + field + '；删除该参数后仍须通过其他独立校验。',
                )


_HANDLERS = MappingProxyType({
    'slow_customer_history': QueryHandler(
        forbidden_parameters=('time_range', 'calendar_month', 'time_bucket', 'comparison', 'order_by', 'movement_state', 'inventory_scope'),
        baseline_parameters=True,
        error_code='HISTORY_PARAMETER_UNSUPPORTED',
        parameter_error_codes=(('time_range', 'HISTORY_FIXED_WINDOW'), ('calendar_month', 'HISTORY_FIXED_WINDOW')),
        parameter_message='历史购买固定为读取时点前12个日历月；按客户、产品、部门和单位的稳定关系粒度返回，不接受自定义期间、分组时间或排名。',
        module='customer_history', builder='build_history_query',
        observer='observe_history_result', public_fields='FACT_FIELDS',
        time_source='TIME_SOURCE',
        time_projection='public_history_time', time_description='describe_history_time',
        consistent_snapshot=True,
    ),
    'monthly_slow_pool': QueryHandler(
        forbidden_parameters=('baseline_week', 'comparison', 'time_bucket', 'order_by'),
        parameter_message='独立月报使用完整日历月和月初池，不接受周基线、通用比较或排序。',
    ),
    'frozen_pool_net_outbound': QueryHandler(
        forbidden_parameters=('comparison', 'time_bucket', 'movement_state', 'order_by'),
        baseline_parameters=True,
        parameter_message='基线产品范围净出库按稳定键排序，不接受状态筛选、时间分组或通用比较。',
    ),
    'frozen_pool_comparison': QueryHandler(
        forbidden_parameters=('time_range', 'calendar_month', 'comparison', 'time_bucket', 'order_by'),
        baseline_parameters=True,
        parameter_message='仅比较已有冻结时点与本次读取，按稳定键排序，不用当前池回填历史期末。',
    ),
})


def get_handler(kind):
    return _HANDLERS.get(kind)


def handler_for_time(source):
    return next((h for h in _HANDLERS.values() if h.time_source is not None and h.resolve('time_source') == source), None)


def public_fact_fields():
    """Union explicit permissions; SQL columns and unit metadata grant nothing."""
    return frozenset().union(*(h.resolve('public_fields') for h in _HANDLERS.values() if h.public_fields))


def validate_parameters(kind, request):
    handler = get_handler(kind)
    if handler is not None:
        handler.validate_parameters(request)
