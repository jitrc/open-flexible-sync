#!/bin/sh
# Open the OFS window. Extra arguments are passed to `python3 -m ofs`.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m ofs "$@"
