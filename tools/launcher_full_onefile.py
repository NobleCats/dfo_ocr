try:
    import pyi_splash
except Exception:
    pyi_splash = None

import sys

if any(arg in ("--test-image", "--list-windows") for arg in sys.argv[1:]):
    from app import main
else:
    from gui_app import main

raise SystemExit(main())
