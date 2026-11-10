#!/bin/sh
# Open the SFS window. Extra arguments are passed to `python3 -m sfs`.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m sfs "$@"
