"""Run only trusted, explicitly selected plugins; this is not a security sandbox."""
from __future__ import annotations

import importlib
import json
import sys

from ..attachment_worker import _disable_network, _set_limits
from .api import Definition, canonical


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read(5 * 1024 * 1024 + 1))
        _disable_network()
        _set_limits(10, 1024 * 1024)
        implementation = request["implementation"]
        if implementation == "rescuetime-weekly":
            implementation = "mailkeep.processors.rescuetime:create"
        module, factory = implementation.split(":")
        plugin = getattr(importlib.import_module(module), factory)()
        definition = Definition.model_validate(plugin.definition)
        if definition != Definition.model_validate(request["definition"]):
            raise ValueError("plugin definition differs from configured declaration")
        response = canonical(plugin.process(request["message"], request["options"]))
        if len(response.encode()) > 1024 * 1024:
            raise ValueError("processor output limit exceeded")
        sys.stdout.write(response)
        return 0
    except BaseException as exc:
        # Do not expose arbitrary exception strings from custom plugins (could contain secrets).
        sys.stderr.write(f"processor worker failed: {type(exc).__name__}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
