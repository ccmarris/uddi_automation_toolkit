'''Enable ``python -m uddi_toolkit`` to invoke the unified CLI.'''
import sys

from uddi_toolkit.cli import main

if __name__ == '__main__':
    sys.exit(main())
