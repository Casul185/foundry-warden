#!/usr/bin/env python
"""Entry point for the Foundry-Warden game-mode daemon (also the Scheduled Task target).

Running this script puts its own directory (the project root) at the front of
sys.path, so `import foundry_warden` resolves without PYTHONPATH or a specific
working directory. Examples:

    python run_warden.py install        # create the logon task + default config
    python run_warden.py start          # start the daemon (detached)
    python run_warden.py status         # show state + recent log
    python run_warden.py run            # run in foreground (Ctrl-C to stop)
    python run_warden.py run --dry-run  # log intended actions, take none
    python run_warden.py stop           # stop and restore everything
"""

import sys

from foundry_warden.__main__ import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
