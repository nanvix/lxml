# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

"""Nanvix build script for lxml.

Usage:
    ./z setup     # Download Nanvix sysroot
    ./z build     # Cross-compile lxml C extensions (.a + .so)
    ./z test      # Run functional test suite
    ./z release   # Package release tarball
    ./z clean     # Remove build artifacts
"""

import sys
import tempfile
from pathlib import Path

from nanvix_zutil import (
    CFG_SYSROOT,
    EXIT_MISSING_DEP,
    TOOLCHAIN_CONTAINER_PATH,
    ZScript,
    log,
    make_initrd,
    run,
    load_manifest,
    package,
)
from nanvix_zutil.paths import (
    buildroot,
    dist_dir,
    include_out,
    lib_out,
    nanvix_root,
    out_dir,
    repo_root,
    test_out,
    release_dir,
)

IS_WINDOWS = sys.platform == "win32"

_MAKE_VAR_HOME = "NANVIX_HOME"
_MAKE_VAR_TOOLCHAIN = "NANVIX_TOOLCHAIN"
_MAKE_VAR_BUILDROOT = "BUILDROOT_PATH"
_MAKE_VAR_PLATFORM = "PLATFORM"
_MAKE_VAR_PROCESS_MODE = "PROCESS_MODE"
_MAKE_VAR_MEMORY_SIZE = "MEMORY_SIZE"


class LxmlBuild(ZScript):
    """Build script for nanvix/lxml."""

    def docker_image(self) -> str:
        """Return the Docker image for cross-compilation."""
        return "ghcr.io/nanvix/toolchain-gcc"

    def docker_config(self, image: str):
        """Configure Docker with output files copied back to the workspace.

        On Windows, the build runs in a container-local tmpfs (``/tmp/build``)
        to avoid VirtioFS I/O penalties.  Declare the artifacts produced by
        ``make all`` so they are copied back to the mounted workspace after
        the inner command exits.  Only ``test_lxml.elf`` is load-bearing at
        the repo root (resolved by ``make_initrd`` via ``repo_root()/app``);
        install-staged artifacts for ``./z release`` are listed by
        ``_staged_output_files()``.
        """
        cfg = super().docker_config(image)
        cfg.output_files = ["test_lxml.elf"] + self._staged_output_files()
        return cfg

    def _staged_output_files(self) -> list[str]:
        """Return install-staged artifact paths (relative to repo_root())
        so Windows tar-copy mode also copies them back to the host.
        """
        root = repo_root()
        # PYTHON_OUT is derived in the Makefile as $(OUT_DIR)/release/python-packages.
        python_out = out_dir() / "release" / "python-packages"
        return [
            str((lib_out() / "liblxml_etree.a").relative_to(root)),
        str((lib_out() / "liblxml_etree.so").relative_to(root)),
            str((lib_out() / "liblxml_elementpath.a").relative_to(root)),
        str((lib_out() / "liblxml_elementpath.so").relative_to(root)),
            str((test_out() / "test_lxml.elf").relative_to(root)),
            # Python sources are globbed by the install rule; list the
            # top-level marker so tar-copy round-trips the whole subtree.
            str((python_out / "lxml").relative_to(root)),
        ]

    def _make_args(self, *targets: str) -> list[str]:
        """Build the common make argument list."""
        sysroot = self.config.get(CFG_SYSROOT, "")
        if not sysroot:
            log.fatal(
                f"{CFG_SYSROOT} is not set.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z setup` first to download the sysroot.",
            )
        toolchain_p = str(TOOLCHAIN_CONTAINER_PATH)
        sysroot_p = (
            self.docker.translate_path(Path(sysroot)) if self.docker else Path(sysroot)
        )

        def translate(p: Path):
            return self.docker.translate_path(p) if self.docker else p

        buildroot_p = translate(buildroot())

        args = [
            "make",
            "-f",
            ".nanvix/Makefile.nanvix",
            f"{_MAKE_VAR_HOME}={sysroot_p}",
            f"{_MAKE_VAR_TOOLCHAIN}={toolchain_p}",
            f"{_MAKE_VAR_BUILDROOT}={buildroot_p}",
        ]

        args.extend(
            [
                f"{_MAKE_VAR_PLATFORM}={self.config.machine}",
                f"{_MAKE_VAR_PROCESS_MODE}={self.config.deployment_mode}",
                f"{_MAKE_VAR_MEMORY_SIZE}={self.config.memory_size}",
                f"NANVIX_ROOT={translate(nanvix_root())}",
                f"OUT_DIR={translate(out_dir())}",
                f"DIST_DIR={translate(dist_dir())}",
                f"LIB_OUT={translate(lib_out())}",
                f"INCLUDE_OUT={translate(include_out())}",
                f"TEST_OUT={translate(test_out())}",
            ]
        )

        args.extend(targets)
        return args

    def _get_sysroot(self) -> str:
        """Return the sysroot path or fatal if not set."""
        sysroot = self.config.get(CFG_SYSROOT, "")
        if not sysroot:
            log.fatal(
                f"{CFG_SYSROOT} is not set.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z setup` first.",
            )
        return sysroot

    def build(self) -> None:
        """Cross-compile lxml C extensions (.a + .so) for Nanvix."""
        run(*self._make_args("all"), cwd=repo_root(), docker=self.docker)

    def test(self) -> None:
        """Run the lxml test suite.

        Only functional tests are supported; they cover all test cases.
        In standalone mode (Linux or Windows), the test is run in Python
        via make_initrd so initrd creation is shared across platforms.
        Other deployment modes are delegated to the Makefile.
        """
        if IS_WINDOWS:
            self._run_tests_windows()
            return

        if self.config.deployment_mode == "standalone":
            self._run_functional_standalone()
        else:
            targets = self.targets if self.targets else ["test"]
            run(
                *self._make_args(*targets),
                cwd=repo_root(),
            )

    def _run_functional_standalone(self) -> None:
        """Run standalone functional tests using make_initrd.

        Creates an initrd bundling test_lxml.elf with system daemons via
        make_initrd, and a ramfs providing /tmp for test I/O.
        """
        test_elf = repo_root() / "test_lxml.elf"
        if not test_elf.is_file():
            log.fatal(
                "test_lxml.elf not found.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z build` first.",
            )

        sysroot = self._get_sysroot()
        sysroot_path = Path(sysroot)
        mkramfs = sysroot_path / "bin" / "mkramfs.elf"

        print("=== lxml functional tests ===")
        print("  Running test_lxml.elf via nanvixd standalone...")

        initrd = make_initrd(self, "test_lxml.elf", test=True)

        try:
            with tempfile.TemporaryDirectory(prefix="nanvix_lxml_") as tmpdir:
                tmpdir_path = Path(tmpdir)
                ramfs_dir = tmpdir_path / "ramfs"
                ramfs_dir.mkdir()
                (ramfs_dir / "tmp").mkdir(exist_ok=True)
                ramfs_img = tmpdir_path / "rootfs.img"

                run(
                    str(mkramfs),
                    "-o",
                    str(ramfs_img),
                    str(ramfs_dir),
                )

                run(
                    str(sysroot_path / "bin" / "nanvixd.elf"),
                    "-bin-dir",
                    str(sysroot_path / "bin"),
                    "-ramfs",
                    str(ramfs_img),
                    "--",
                    str(initrd),
                    timeout=120,
                )
        finally:
            if initrd.exists():
                initrd.unlink()

        print("  PASS: test_lxml standalone")
        print("  PASS: lxml functional tests")
        print("=== All lxml tests PASSED ===")

    def _run_tests_windows(self) -> None:
        """Run tests natively on Windows via nanvixd.exe.

        Uses make_initrd to bundle test_lxml.elf with system daemons,
        and a ramfs for test I/O files. Only standalone mode is
        supported on Windows.
        """
        if self.config.deployment_mode != "standalone":
            print(
                f"Skipping tests on Windows for mode '{self.config.deployment_mode}' (requires linuxd)."
            )
            return

        sysroot = self._get_sysroot()
        sysroot_path = Path(sysroot)
        nanvixd = sysroot_path / "bin" / "nanvixd.exe"
        mkramfs = sysroot_path / "bin" / "mkramfs.exe"
        if not nanvixd.is_file():
            log.fatal(
                "nanvixd.exe not found.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z setup` first.",
            )
        if not mkramfs.is_file():
            log.fatal(
                "mkramfs.exe not found.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z setup` first.",
            )

        test_elf = repo_root() / "test_lxml.elf"
        if not test_elf.is_file():
            log.fatal(
                "test_lxml.elf not found.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z build` first.",
            )

        print("=== lxml functional tests ===")
        print("  Running test_lxml.elf via nanvixd.exe standalone...")

        initrd = make_initrd(self, "test_lxml.elf", test=True)

        try:
            with tempfile.TemporaryDirectory(prefix="nanvix_lxml_") as tmpdir:
                tmpdir_path = Path(tmpdir)
                ramfs_dir = tmpdir_path / "ramfs"
                ramfs_dir.mkdir()
                (ramfs_dir / "tmp").mkdir(exist_ok=True)
                ramfs_img = tmpdir_path / "rootfs.img"

                run(
                    str(mkramfs),
                    "-o",
                    str(ramfs_img),
                    str(ramfs_dir),
                    timeout=60,
                )

                run(
                    str(nanvixd),
                    "-bin-dir",
                    str(sysroot_path / "bin"),
                    "-ramfs",
                    str(ramfs_img),
                    "--",
                    str(initrd),
                    timeout=120,
                )
        finally:
            if initrd.exists():
                initrd.unlink()

        print("  PASS: test_lxml standalone")
        print("  PASS: lxml functional tests")
        print("=== All lxml tests PASSED ===")

    def release(self) -> None:
        """Package the release archive named per build configuration.

        The base :meth:`ZScript.release` packages ``release_dir()`` under the
        bare package name, so every matrix configuration emits an
        identically-named archive; in CI these collide and overwrite one
        another, leaving the published release with only generic assets.
        Dependents resolve assets by the pattern
        ``{name}-{machine}-{mode}-{mem}`` (e.g.
        ``{name}-microvm-multi-process-128mb``), so the archive must carry that
        name for dependency installation to succeed.
        """
        manifest = load_manifest()
        name = (
            f"{manifest.name}"
            f"-{self.config.machine}"
            f"-{self.config.deployment_mode}"
            f"-{self.config.memory_size}"
        )
        package([release_dir()], dist_dir(), name)

    def clean(self) -> None:
        """Remove build artifacts."""
        run(
            "make",
            "-f",
            ".nanvix/Makefile.nanvix",
            "clean",
            cwd=repo_root(),
        )


if __name__ == "__main__":
    LxmlBuild.main()
