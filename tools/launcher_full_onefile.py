try:
    import pyi_splash
except Exception:
    pyi_splash = None

from gui_app import main

raise SystemExit(main())
