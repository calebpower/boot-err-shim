# boot-err-shim
#
# The install targets are deliberately plain: this program has no runtime
# dependencies, so installing it is copying files and choosing an init system.

PYTHON      ?= python3

# Absolute path to the interpreter, baked into the zipapp's shebang.
#
# `#!/usr/bin/env python3` looks portable and breaks under rc(8) and cron,
# whose PATH is /sbin:/bin:/usr/sbin:/usr/bin. FreeBSD keeps python3 in
# /usr/local/bin, which is not on that list, so the daemon starts fine from an
# interactive shell and fails at boot with "env: python3: No such file or
# directory". This is the same reason FreeBSD ports carry USES=shebangfix.
#
# Resolved by name rather than from sys.executable, so it lands on the stable
# /usr/local/bin/python3 symlink instead of a versioned binary that a minor
# upgrade would move out from under us.
#
# Empty means "work it out at build time". Override when cross-installing:
# building on Linux for a FreeBSD target needs
# INTERPRETER=/usr/local/bin/python3.
#
# Resolved inside the recipe rather than with $(shell ...). FreeBSD's make is
# bmake, where $(shell ...) is not a function but an undefined variable, so it
# expanded to nothing and `zipapp -p ''` wrote a bundle with no shebang at
# all -- which then failed at run time complaining about an interpreter made
# of ZIP header bytes. Recipes are shell in both makes; make functions are
# not portable between them.
INTERPRETER ?=

IMAGE       ?= boot-err-shim-e2e
STAGES      ?= all
DESTDIR     ?=
PREFIX      ?= /usr/local
SERVICE_USER  ?= boot-err-shim
SERVICE_GROUP ?= $(SERVICE_USER)

# Numeric ids for the service account. Empty means "pick a free one in the
# system range".
#
# FreeBSD reserves everything below 1000 for system accounts, and `pw useradd`
# with no -u allocates from 1000 upwards -- so the obvious command puts a
# daemon account in with the humans. Ports avoid this by drawing from the
# registry in /usr/ports/UIDs; there is no entry for this, so `make
# freebsd-user` finds the first free pair from 200 up and tells you what it
# chose.
SERVICE_UID   ?=
SERVICE_GID   ?=

.PHONY: help
help:
	@echo 'Targets:'
	@echo '  test            unit suite (tiers 1-5, 7-9)'
	@echo '  test-hostile    the same under LANG=C with PYTHONUTF8=0'
	@echo '  test-mutants    break the code deliberately; every mutant must be caught'
	@echo '  test-e2e        tier 6, in podman, against a real VNC server'
	@echo '  test-e2e-build  rebuild the tier 6 image'
	@echo '  bundle          single-file boot-err-shim.pyz'
	@echo '  install-freebsd / install-linux'
	@echo '  lint            ruff, if available'
	@echo
	@echo '  make test-e2e STAGES="calibrate detect"   # one or more stages'

# -- tests -------------------------------------------------------------

.PHONY: test
test:
	$(PYTHON) -m unittest discover -s tests -t .

.PHONY: test-hostile
test-hostile:
	LANG=C LC_ALL=C PYTHONUTF8=0 $(PYTHON) -m unittest discover -s tests -t .

.PHONY: test-mutants
test-mutants:
	$(PYTHON) tools/mutate.py

.PHONY: test-e2e-build
test-e2e-build:
	podman build -t $(IMAGE) -f containers/Containerfile .

# Podman only, and the repository is mounted read-only: a stage cannot write
# to the working tree even by accident.
.PHONY: test-e2e
test-e2e: test-e2e-build
	podman run --rm -v "$$(pwd):/src:ro" $(IMAGE) $(STAGES)

.PHONY: lint
lint:
	@command -v ruff >/dev/null 2>&1 && ruff check . || \
		echo 'ruff not installed; skipping (uv sync installs it)'

# -- packaging ---------------------------------------------------------

# Valid only because nothing outside the standard library is imported at
# runtime; tests/structural/test_stdlib_only.py enforces that.
.PHONY: bundle
bundle:
	rm -rf build/pyz
	mkdir -p build/pyz
	cp -r src/boot_err_shim build/pyz/
	printf 'from boot_err_shim.cli import main\nraise SystemExit(main())\n' \
		> build/pyz/__main__.py
	@interp='$(INTERPRETER)'; \
	explicit=yes; \
	if [ -z "$$interp" ]; then \
		explicit=no; \
		interp=$$(command -v $(PYTHON) 2>/dev/null || true); \
	fi; \
	if [ -z "$$interp" ]; then \
		echo "cannot resolve $(PYTHON) to a path; pass INTERPRETER=/usr/local/bin/python3" >&2; \
		exit 1; \
	fi; \
	case "$$interp" in /*) ;; *) \
		echo "INTERPRETER must be an absolute path, got '$$interp'" >&2; \
		echo "an env shebang fails under rc(8) and cron, which is the whole point" >&2; \
		exit 1 ;; \
	esac; \
	if [ "$$explicit" = no ] && [ ! -x "$$interp" ]; then \
		echo "$$interp is not executable" >&2; exit 1; \
	fi; \
	$(PYTHON) -m zipapp build/pyz -o boot-err-shim.pyz -p "$$interp" -c; \
	case "$$(head -1 boot-err-shim.pyz)" in \
	'#!'*) ;; \
	*) echo "the bundle came out with no shebang -- refusing to ship it" >&2; \
	   echo "(this is what a make without \$$(shell) support produces)" >&2; \
	   rm -f boot-err-shim.pyz; exit 1 ;; \
	esac; \
	echo "wrote boot-err-shim.pyz (interpreter: $$interp)"

# -- installation ------------------------------------------------------

.PHONY: install-common
install-common: bundle
	install -d $(DESTDIR)$(PREFIX)/sbin
	install -m 0755 boot-err-shim.pyz $(DESTDIR)$(PREFIX)/sbin/boot-err-shim

# Create the service account in the system id range. Idempotent: an existing
# account is reported, never modified, because changing a uid out from under
# files that reference it numerically is not something a build target should
# do behind your back.
.PHONY: freebsd-user
freebsd-user:
	@user='$(SERVICE_USER)'; group='$(SERVICE_GROUP)'; \
	uid='$(SERVICE_UID)'; gid='$(SERVICE_GID)'; \
	free_id() { \
		n=$$2; \
		while grep -q "^[^:]*:[^:]*:$$n:" "$$1" 2>/dev/null; do \
			n=$$((n + 1)); \
		done; \
		echo "$$n"; \
	}; \
	if pw usershow "$$user" > /dev/null 2>&1; then \
		cur=$$(pw usershow "$$user" | awk -F: '{print $$3}'); \
		echo "$$user already exists with uid $$cur"; \
		if [ "$$cur" -ge 1000 ]; then \
			echo; \
			echo "  That is outside the system range. To move it:"; \
			echo "    service boot_err_shim stop"; \
			echo "    pw groupmod $$group -g <NEW_GID>"; \
			echo "    pw usermod $$user -u <NEW_UID> -g $$group"; \
			echo "    chown -R $$user:$$group /var/db/boot-err-shim"; \
			echo "    chown root:$$group $(PREFIX)/etc/boot-err-shim.conf"; \
			echo "    service boot_err_shim start"; \
			echo; \
			echo "  The chowns matter: pw changes the id, not the files that"; \
			echo "  already carry the old one, and they would be orphaned."; \
		fi; \
		exit 0; \
	fi; \
	[ -n "$$gid" ] || gid=$$(free_id /etc/group 200); \
	[ -n "$$uid" ] || uid=$$(free_id /etc/passwd "$$gid"); \
	pw groupshow "$$group" > /dev/null 2>&1 || pw groupadd "$$group" -g "$$gid"; \
	pw useradd "$$user" -u "$$uid" -g "$$group" \
		-d /nonexistent -s /usr/sbin/nologin \
		-c 'boot-err-shim daemon'; \
	echo "created $$user (uid $$uid) and group $$group (gid $$gid)"; \
	echo "if you later install a port wanting those ids, pw will say so"

.PHONY: install-freebsd
install-freebsd: install-common
	install -d $(DESTDIR)$(PREFIX)/etc
	install -m 0644 boot-err-shim.conf.sample \
		$(DESTDIR)$(PREFIX)/etc/boot-err-shim.conf.sample
	install -d $(DESTDIR)$(PREFIX)/etc/rc.d
	install -m 0755 init/rc.d/boot_err_shim \
		$(DESTDIR)$(PREFIX)/etc/rc.d/boot_err_shim
	install -d -m 0750 $(DESTDIR)/var/db/boot-err-shim
	@# Re-assert ownership, because the line above runs against an existing
	@# directory on every update and install(1) may reset it to root -- which
	@# would leave the daemon unable to write its calibration or lock file.
	@# Prefixed with - so a first install, or a staged build where the account
	@# does not exist, is not a failure.
	-chown -R $(SERVICE_USER):$(SERVICE_GROUP) $(DESTDIR)/var/db/boot-err-shim
	@echo
	@echo 'Installed. Next (or on an update, only if start complains):'
	@echo '  # 1. the service user, first: everything below refers to it.'
	@echo '  #    Uses the system id range; a plain pw useradd would not.'
	@echo '  make freebsd-user'
	@echo '  chown -R $(SERVICE_USER):$(SERVICE_GROUP) /var/db/boot-err-shim'
	@echo
	@echo '  # 2. the config. It must be READABLE BY $(SERVICE_USER), which'
	@echo '  #    root-owned 0600 is not -- that is the usual first failure.'
	@echo '  cp $(PREFIX)/etc/boot-err-shim.conf.sample $(PREFIX)/etc/boot-err-shim.conf'
	@echo '  $$EDITOR $(PREFIX)/etc/boot-err-shim.conf'
	@echo '  chown root:$(SERVICE_GROUP) $(PREFIX)/etc/boot-err-shim.conf'
	@echo '  chmod 640 $(PREFIX)/etc/boot-err-shim.conf'
	@echo
	@echo '  # 3. start it'
	@echo '  sysrc boot_err_shim_enable=YES && service boot_err_shim start'

.PHONY: install-linux
install-linux:
	$(MAKE) install-common PREFIX=/usr/local
	install -d $(DESTDIR)/etc
	install -m 0644 boot-err-shim.conf.sample \
		$(DESTDIR)/etc/boot-err-shim.conf.sample
	install -d $(DESTDIR)/etc/systemd/system
	install -m 0644 init/boot-err-shim.service \
		$(DESTDIR)/etc/systemd/system/boot-err-shim.service
	@echo
	@echo 'Installed. Next:'
	@echo '  cp /etc/boot-err-shim.conf.sample /etc/boot-err-shim.conf'
	@echo '  $$EDITOR /etc/boot-err-shim.conf && chmod 600 /etc/boot-err-shim.conf'
	@echo '  systemctl daemon-reload && systemctl enable --now boot-err-shim'
	@echo
	@echo 'The unit uses DynamicUser and StateDirectory, so systemd creates'
	@echo 'and owns /var/lib/boot-err-shim itself.'

.PHONY: clean
clean:
	rm -rf build boot-err-shim.pyz .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
