"""Entry script for agent-crew-chief.

Invoked as ``python -I <path>/crew-chief.py``; under ``-I`` the script
directory is not on ``sys.path``, so it is inserted here explicitly.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from crew_chief.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
