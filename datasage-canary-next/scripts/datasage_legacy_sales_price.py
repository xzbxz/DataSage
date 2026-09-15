"""Fixed legacy report adapter; automatic execution remains disabled."""
from datasage_slow_report import fixed_workflow_main

if __name__ == "__main__":
    raise SystemExit(fixed_workflow_main('sales_price'))
