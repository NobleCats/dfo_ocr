"""Bootstrap entry point for the protected onedir release.

The release build replaces every src/*.py module with a Cython-compiled .pyd.
PyInstaller then bundles this launcher together with those .pyd files. We keep
the launcher tiny on purpose so the actual application code stays inside the
compiled extensions.
"""

from gui_app import main

raise SystemExit(main())
