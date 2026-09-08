"""Native-appliance packaging support (E11-S2, issue #138).

This package holds the pieces of the Pi appliance delivery that are pure
Python: the bundled/pinned first-crack model's identity manifest and the
secure placement/verification logic the ``roastpilot-agent appliance model
install`` CLI subcommand drives (PR slice 1); the systemd unit / operator
env file / ``pi_inference`` MCP YAML templates and the ``roastpilot-agent
appliance render`` renderer (PR slice 2, :mod:`roastpilot_agent.appliance.render`).
Slice 3 adds the shell installer; slice 4 adds the deployment doc. Neither of
those later pieces lives here yet.
"""
