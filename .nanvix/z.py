# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

"""Nanvix build script for lxml.

Usage:
    ./z setup     # Download Nanvix sysroot
    ./z build     # Cross-compile lxml C extensions
    ./z test      # Run test suite (smoke)
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
        the inner command exits.
        """
        cfg = super().docker_config(image)
        cfg.output_files = [
            "dist/obj/liblxml_etree.a",
            "dist/obj/liblxml_elementpath.a",
            "test_lxml.elf",
        ]
        return cfg

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
        buildroot_host = self.repo_root / ".nanvix" / "buildroot"
        buildroot_p = (
            self.docker.translate_path(buildroot_host)
            if self.docker
            else buildroot_host
        )

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
        """Cross-compile lxml C extensions for Nanvix."""
        run(*self._make_args("all"), cwd=self.repo_root, docker=self.docker)

    def test(self) -> None:
        """Run the lxml test suite.

        Smoke and integration tests are always delegated to the Makefile.
        The functional test in standalone mode is handled in Python via
        make_initrd so that initrd creation is shared across platforms.
        """
        if IS_WINDOWS:
            self._run_tests_windows()
            return

        if self.config.deployment_mode == "standalone":
            targets = self.targets if self.targets else []
            _functional_targets = {"test", "test-functional"}
            needs_functional = not targets or bool(set(targets) & _functional_targets)
            make_targets = [t for t in targets if t not in _functional_targets]
            if not targets:
                make_targets = ["test-smoke", "test-integration"]
            elif needs_functional and not make_targets:
                # When only "test" is requested, run full prerequisites.
                # When only "test-functional" is requested, skip Makefile
                # targets since they already ran as Makefile dependencies
                # (avoids double execution when invoked via `make test`).
                if "test" in targets:
                    make_targets = ["test-smoke", "test-integration"]
            if make_targets:
                run(
                    *self._make_args(*make_targets),
                    cwd=self.repo_root,
                )
            if needs_functional:
                self._run_functional_standalone()
        else:
            targets = self.targets if self.targets else ["test"]
            run(
                *self._make_args(*targets),
                cwd=self.repo_root,
            )

    def _run_functional_standalone(self) -> None:
        """Run standalone functional tests using make_initrd.

        Creates an initrd bundling test_lxml.elf with system daemons via
        make_initrd, and a ramfs providing /tmp for test I/O.
        """
        test_elf = self.repo_root / "test_lxml.elf"
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

        initrd = make_initrd(self, "test_lxml.elf")

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

        test_elf = self.repo_root / "test_lxml.elf"
        if not test_elf.is_file():
            log.fatal(
                "test_lxml.elf not found.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z build` first.",
            )

        print("=== lxml functional tests ===")
        print("  Running test_lxml.elf via nanvixd.exe standalone...")

        initrd = make_initrd(self, "test_lxml.elf")

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
        """Package the lxml release tarball and verify it."""
        run(*self._make_args("package"), cwd=self.repo_root)
        run(*self._make_args("verify-package"), cwd=self.repo_root)

    def clean(self) -> None:
        """Remove build artifacts."""
        run(
            "make",
            "-f",
            ".nanvix/Makefile.nanvix",
            "clean",
            cwd=self.repo_root,
        )


if __name__ == "__main__":
    LxmlBuild.main()
