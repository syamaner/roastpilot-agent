"""Native-appliance packaging support (E11-S2, issue #138).

This package holds the pieces of the Pi appliance delivery that are pure
Python: the bundled/pinned first-crack model's identity manifest and the
secure placement/verification logic the ``roastpilot-agent appliance model
install`` CLI subcommand drives (PR slice 1). Slice 2 adds the systemd/env/MCP
templates and renderer; slice 3 adds the shell installer; slice 4 adds the
deployment doc. None of that later work lives here yet.
"""
