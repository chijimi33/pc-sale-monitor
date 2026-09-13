import argparse
from pathlib import Path
import shutil

parser = argparse.ArgumentParser()
parser.add_argument("--store", required=True)
args = parser.parse_args()
from sale_monitor.models import STORES
if args.store not in STORES:
    parser.error("unsupported store")
source = Path("state-repo/state/stores") / (args.store + ".json")
if source.exists():
    Path("outgoing/stores").mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, Path("outgoing/stores") / source.name)
if args.store == "koubou" and Path("state-repo/state/flyers").exists():
    shutil.copytree("state-repo/state/flyers", "outgoing/flyers", dirs_exist_ok=True)
