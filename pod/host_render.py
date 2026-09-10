"""Run the renderer shipped with the submitted job, not an image-pinned copy."""
import os
import sys

sys.path.insert(0, os.environ['RPFARM_ROOT'])
from rpfarm.host_render import main

if __name__ == '__main__':
    sys.exit(main())
