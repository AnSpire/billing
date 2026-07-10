# Catala formalization of the СОИ (common-property) water-volume model (case3).
#
# Prereqs: the `catala` opam switch with Catala 1.2.0.
# A clerk.toml in this directory makes it the project root, so build artifacts
# land in ./_build and target names stay short.
#
# Usage:
#   make setup      # one-time: build the native stdlib + model .cmxs plugins
#   make typecheck  # type-check the model
#   make test       # run the assertion-based #[test] scopes
#   make python     # transpile the model to Python -> _build/python/Soi_volume.py
#   make python-pkg # assemble a standalone importable package under _build/pyrun
#   make pytest     # run pytest over the generated Python package
#   make ocaml / make c
#   make clean

SWITCH := catala
CATALA := opam exec --switch=$(SWITCH) -- catala
CLERK  := opam exec --switch=$(SWITCH) -- clerk

MODEL  := soi_volume.catala_en
TESTS  := soi_volume_tests.catala_en
STDLIB := _build/libcatala
MODULE := Soi_volume

.PHONY: test typecheck setup python ocaml c transpile python-pkg pytest clean

setup:
	$(CLERK) build ocaml/$(MODULE).cmxs

typecheck: setup
	$(CATALA) typecheck --whole-program -I . --stdlib=$(STDLIB) $(MODEL)

# --whole-program compiles the model module from source together with the test
# file, then evaluates every #[test] scope and checks its assertions. A failing
# assertion aborts with a non-zero exit code (123).
test: setup
	$(CATALA) interpret --whole-program -I . --stdlib=$(STDLIB) $(TESTS)

python:
	$(CLERK) build python/$(MODULE).py
ocaml:
	$(CLERK) build ocaml/$(MODULE).ml
c:
	$(CLERK) build c/$(MODULE).c

transpile: python ocaml c

# Assemble a standalone, importable Python package (model + transpiled stdlib +
# runtime) under _build/pyrun/soi. Depends on the stdlib sources materialized by
# `setup` (clerk start populates _build/libcatala).
python-pkg: setup
	./build_pyrun.sh

# Run pytest over the transpiled Python (the artifact that ships to the runtime).
pytest: python-pkg
	python3 -m pytest -q test_soi_volume.py

clean:
	$(CLERK) clean
