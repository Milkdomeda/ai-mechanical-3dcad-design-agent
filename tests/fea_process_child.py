"""Owned subprocess fixtures; never touch CAD or unrelated processes."""
import os
from pathlib import Path
import subprocess
import sys
import time


role, directory = sys.argv[1:3]
root = Path(directory)
(root / (role + ".pid")).write_text(str(os.getpid()))
if role == "tree":
    subprocess.Popen([sys.executable, __file__, "leaf", str(root)])
elif role == "leaf":
    subprocess.Popen([sys.executable, __file__, "grandchild", str(root)])
while not (root / "stop").exists():
    time.sleep(0.03)
