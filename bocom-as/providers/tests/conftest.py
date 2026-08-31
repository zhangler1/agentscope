"""Pytest fixtures for the bocom-as providers test suite.

``config`` and ``providers`` are top-level packages of the bocom-as
distribution.  When tests run from ``bocom-as/`` (``python -m pytest
providers/tests/``) both packages are already importable; this conftest also
injects ``bocom-as/`` into ``sys.path`` so the suite keeps working when
pytest is invoked from an arbitrary working directory.
"""

import sys
from pathlib import Path

_BOCOM_AS_ROOT = Path(__file__).resolve().parents[2]  # bocom-as/
if str(_BOCOM_AS_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOCOM_AS_ROOT))
