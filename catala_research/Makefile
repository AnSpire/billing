# Convenience entrypoint that runs every case's own Makefile.
# Meant to be invoked inside the toolchain container (see docker-compose.yml),
# but works identically on a host with the `catala` opam switch set up.
#
# case1 has no python/pytest wiring yet, so it's excluded from test-all.
PYTEST_CASES := case2 case3

.PHONY: test-all
test-all:
	@for d in $(PYTEST_CASES); do \
		echo "=== $$d ==="; \
		$(MAKE) -C $$d pytest || exit 1; \
	done
