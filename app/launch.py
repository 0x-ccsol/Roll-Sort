"""
Starts Roll Merge. On the first run it sets up a private Python environment
next to the app (needs internet once, takes about a minute), then reuses it.
Only uses the standard library, so it runs before anything is installed.
"""
import os
import subprocess
import sys
import venv

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(HERE, ".runtime")
REQS = os.path.join(HERE, "requirements.txt")
APP = os.path.join(HERE, "rollmerge.py")


def env_python():
    if os.name == "nt":
        return os.path.join(ENV, "Scripts", "python.exe")
    return os.path.join(ENV, "bin", "python")


def ready(python):
    return subprocess.call([python, "-c", "import PIL, piexif"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def setup():
    py = env_python()
    if os.path.exists(py) and ready(py):
        return py
    print("\n  First-time setup: installing what Roll Merge needs (about a minute, needs internet)...\n")
    if not os.path.exists(py):
        venv.create(ENV, with_pip=True, clear=True)
    subprocess.check_call([py, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", REQS])
    print("  Setup done.\n")
    return py


def main():
    if sys.version_info < (3, 8):
        print("Roll Merge needs Python 3.8 or newer. Get it from https://www.python.org/downloads/")
        return 1
    try:
        py = setup()
    except Exception as exc:
        print("\n  Setup failed: %s" % exc)
        print("  Check the internet connection and try again.")
        print("  If it keeps failing, delete the '.runtime' folder inside 'app' and retry.\n")
        return 1
    try:
        return subprocess.call([py, APP] + sys.argv[1:])
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
