"""Without Playwright (it is not a dev dependency) the browser tests are not even collected, so a
bare `pytest` stays hermetic and green; the CI e2e job installs it and selects them with `-m e2e`
(a run that collects nothing there fails with exit code 5)."""

from __future__ import annotations

import importlib.util

collect_ignore_glob = [] if importlib.util.find_spec("playwright") else ["test_*.py"]
