#!/usr/bin/env bash
# Assemble a runnable Python package from the Catala transpilation.
#
# Catala emits one module per file that uses relative imports (`from . import
# Money_en`) for the stdlib and a top-level `from catala_runtime import *`. The
# stdlib `_en` modules therefore have to be transpiled too, the `_internal`
# externals map to hand-written runtime files, and `catala_runtime`/`dates` must
# sit on sys.path. This script materializes all of that under _build/pyrun so
# `pytest` (and the real runtime) can import `soi.Soi_volume` standalone.
set -euo pipefail

SWITCH=catala
CATALA="opam exec --switch=$SWITCH -- catala"
RT="$(opam var --switch=$SWITCH prefix)/lib/catala/runtime/python"
LIB=_build/libcatala
RUN=_build/pyrun
PKG=$RUN/soi

[ -d "$LIB" ] || { echo "missing $LIB — run 'clerk start' / 'make setup' first"; exit 1; }

rm -rf "$RUN"
mkdir -p "$PKG"

# catala_runtime + dates on sys.path (imported top-level by every module)
cp "$RT/src/catala/catala_runtime.py" "$RT/src/catala/dates.py" "$RUN/"

# `_internal` externals: hand-written files, capitalized to match the module refs
for mod in decimal date list money period; do
  Cap="$(python3 -c "print('$mod'.capitalize())")"
  cp "$RT/${mod}_internal.py" "$PKG/${Cap}_internal.py"
done

# transpile every stdlib `_en` module into the package
for f in "$LIB"/*_en.catala_en; do
  name="$(grep -m1 '^> Module' "$f" | awk '{print $3}')"
  $CATALA python -I "$LIB" "$f" -o "$PKG/${name}.py" >/dev/null 2>&1
done

# transpile the model itself
$CATALA python -I "$LIB" -I . soi_volume.catala_en -o "$PKG/Soi_volume.py" >/dev/null 2>&1

touch "$PKG/__init__.py"
echo "built $PKG"
