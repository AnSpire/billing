# Catala formalization of the sample rental agreement.
#
# Prereqs: the `catala` opam switch built with Catala 1.2.0
#   (opam switch create catala 5.2.0 && opam install catala).
# A clerk.toml in this directory makes it the project root, so all build
# artifacts land in ./_build and target names stay short.
#
# Usage:
#   make setup      # one-time: materialize runtime + stdlib into _build/
#   make typecheck  # type-check the model
#   make test       # run the assertion-based test scopes
#   make python     # transpile the model to Python  -> _build/python/Rental_agreement.py
#   make ocaml      # transpile the model to OCaml    -> _build/ocaml/Rental_agreement.ml
#   make c          # transpile the model to C        -> _build/c/Rental_agreement.c
#   make clean

SWITCH := catala
CATALA := opam exec --switch=$(SWITCH) -- catala
CLERK  := opam exec --switch=$(SWITCH) -- clerk

MODEL  := rental_agreement.catala_en
TESTS  := rental_agreement_tests.catala_en
STDLIB := _build/libcatala
# Capitalized module name (from `> Module ...` in the model file).
MODULE := Rental_agreement

.PHONY: test typecheck setup python ocaml c transpile clean

# Materialize the runtime and stdlib under _build/ AND compile the native stdlib
# plugins (.cmxs) that `catala interpret --whole-program` loads. Building the
# model's own .cmxs pulls those in as dependencies.
setup:
	$(CLERK) build ocaml/$(MODULE).cmxs

typecheck: setup
	$(CATALA) typecheck --whole-program -I . --stdlib=$(STDLIB) $(MODEL)

# --whole-program compiles the Rental_agreement module from source together with
# the test file, then the interpreter evaluates every #[test] scope and checks
# its `assertion`s. A failing assertion aborts with a non-zero exit code.
# (Note: `clerk test` cannot link the cross-file module in Catala 1.2.0, hence
# the direct interpreter call here.)
test: setup
	$(CATALA) interpret --whole-program -I . --stdlib=$(STDLIB) $(TESTS)

# Transpilation ("compile to a backend language"). clerk builds the target and
# its dependencies; output paths are printed and land under _build/<backend>/.
python:
	$(CLERK) build python/$(MODULE).py
ocaml:
	$(CLERK) build ocaml/$(MODULE).ml
c:
	$(CLERK) build c/$(MODULE).c

transpile: python ocaml c

clean:
	$(CLERK) clean
