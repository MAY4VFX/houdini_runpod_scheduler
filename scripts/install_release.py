"""Install a built bundle for the next Houdini session, without a farm API call."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rpfarm import config, houdini_local, releases

if __name__ == '__main__':
    release = releases.install(ROOT, config.home(), houdini_local.find_houdini_installations())
    print('Installed for next Houdini launch:', release)
