"""Create every farm node, and construct the scheduler. Run under hython.

The hole this fills, found the hard way on 2026-09-07: a thousand green
tests while the scheduler could not be instantiated at all. Every test we
had lifted METHODS out of the asset and drove them with stand-ins; not one
asked the only question an artist asks first -- does the node come into
existence?

    hython scripts/node_creation_smoke.py

Prints one line per node and exits non-zero on the first failure.
"""

from __future__ import annotations

import sys
import traceback

TYPES = ("runpodfarmscheduler", "runpodfarmupload",
         "runpodfarmdownload", "runpodfarmstats")


def main():
    import hou

    failures = []
    topnet = hou.node("/obj").createNode("topnet", "rpfarm_smoke")

    for type_name in TYPES:
        try:
            node = topnet.createNode(type_name, type_name.replace("runpodfarm", ""))
        except Exception as e:  # noqa: BLE001 - the report IS the point
            failures.append((type_name, "createNode: {}".format(e)))
            traceback.print_exc()
            continue
        errors = node.errors()
        # A node that exists but is red is not a working node.
        if errors:
            failures.append((type_name, "node errors: {}".format(" ".join(errors))))
        elif not node.matchesCurrentDefinition():
            failures.append((type_name, "created unlocked / modified (Ruling R53)"))
        else:
            print("[OK]   {} created clean".format(type_name))

    # And the class itself, which is what PDG instantiates behind the node.
    try:
        import pdg

        node = topnet.node("scheduler")
        cls = node.type().hdaModule().RunPodFarmScheduler
        instance = cls(pdg.TypeRegistry.types().schedulerType("runpodfarmscheduler"),
                       "rpfarm_smoke_instance")
        print("[OK]   RunPodFarmScheduler() constructed ({})".format(type(instance).__name__))
    except Exception as e:  # noqa: BLE001
        failures.append(("RunPodFarmScheduler.__init__", str(e)))
        traceback.print_exc()

    for name, why in failures:
        print("[FAIL] {}: {}".format(name, why))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
