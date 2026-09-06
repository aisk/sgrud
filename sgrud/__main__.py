"""Let ``python -m sgrud`` behave like the ``sgrud`` script."""

import sys

from .cli import main

sys.exit(main())
