"""Fixed official Hermes scheduler entry; bounded approval defaults off."""
import sys
from datasage_workflow import main
if __name__=="__main__":
    sys.argv=[sys.argv[0],"--mode","live","scheduled-tick","--job","slow-report"]
    raise SystemExit(main())
