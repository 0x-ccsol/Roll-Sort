#!/bin/bash
# Double-click to start Roll Sort on a Mac.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo ""
  echo "  Roll Sort needs Python 3, which isn't installed yet."
  echo "  Opening the download page: install it, then double-click this file again."
  echo ""
  open "https://www.python.org/downloads/macos/"
  read -r -p "  Press Enter to close this window." _
  exit 1
fi

python3 app/launch.py "$@"
status=$?
if [ $status -ne 0 ]; then
  echo ""
  read -r -p "  Something went wrong (see above). Press Enter to close this window." _
fi
