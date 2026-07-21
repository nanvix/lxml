# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

"""Nanvix build script for lxml.

Usage:
    ./z setup     # Download Nanvix sysroot
    ./z build     # Cross-compile lxml C extensions
    ./z test      # Run functional test suite
    ./z release   # Package release tarball
    ./z clean     # Remove build artifacts
"""

import shutil
import sys
import tarfile
import tempfile
import zipfile
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
from nanvix_zutil.paths import (
    dev_out,
    dist_dir,
    nanvix_root,
    out_dir,
    repo_root,
    sysroot as sysroot_dir,
    test_out,
)

IS_WINDOWS = sys.platform == "win32"

_DEPENDENCY_METADATA = {
    "libxml2": ("libxml-2.0.pc",),
    "libxslt": ("libxslt.pc", "libexslt.pc"),
}

_MAKE_VAR_HOME = "NANVIX_HOME"
_MAKE_VAR_TOOLCHAIN = "NANVIX_TOOLCHAIN"
_MAKE_VAR_BUILDROOT = "BUILDROOT_PATH"
_MAKE_VAR_PLATFORM = "PLATFORM"
_MAKE_VAR_PROCESS_MODE = "PROCESS_MODE"
_MAKE_VAR_MEMORY_SIZE = "MEMORY_SIZE"


class LxmlBuild(ZScript):
    """Build script for nanvix/lxml."""

    # Build-time headers, libraries, startup objects, and linker scripts come
    # from the SDK and sysroot.
    SYSROOT_REQUIRED_FILES = (
        "bin/nanvixd.elf",
        "bin/kernel.elf",
        "bin/mkramfs.elf",
    )
    SYSROOT_REQUIRED_FILES_WINDOWS = (
        "bin/nanvixd.exe",
        "bin/kernel.elf",
        "bin/mkramfs.exe",
    )

    def setup(self) -> bool:
        """Install the runtime sysroot and exact build dependency metadata."""
        degraded = super().setup()
        self._install_dependency_metadata()
        return degraded

    def _install_dependency_metadata(self) -> None:
        """Install relocatable pkg-config files omitted by zutils v0.14.0."""
        cache = nanvix_root() / "cache"
        metadata_out = sysroot_dir() / "lib" / "pkgconfig"
        metadata_out.mkdir(parents=True, exist_ok=True)

        for dependency, filenames in _DEPENDENCY_METADATA.items():
            archives = sorted(
                path for path in cache.glob(f"{dependency}-*") if path.is_file()
            )
            if not archives:
                log.fatal(
                    f"Cached release archive for {dependency} not found.",
                    code=EXIT_MISSING_DEP,
                    hint="Run `./z distclean` and then `./z setup`.",
                )
            archive = archives[-1]
            for filename in filenames:
                destination = metadata_out / filename
                destination.unlink(missing_ok=True)
                if zipfile.is_zipfile(archive):
                    with zipfile.ZipFile(archive) as zf:
                        member = next(
                            (
                                name
                                for name in zf.namelist()
                                if Path(name).name == filename
                            ),
                            None,
                        )
                        if member is not None:
                            with zf.open(member) as src, destination.open("wb") as dst:
                                shutil.copyfileobj(src, dst)
                else:
                    with tarfile.open(archive, "r:*") as tf:
                        member = next(
                            (
                                item
                                for item in tf.getmembers()
                                if item.isfile() and Path(item.name).name == filename
                            ),
                            None,
                        )
                        if member is not None:
                            src = tf.extractfile(member)
                            if src is not None:
                                with src, destination.open("wb") as dst:
                                    shutil.copyfileobj(src, dst)
                if not destination.is_file():
                    log.fatal(
                        f"{filename} not found in {archive.name}.",
                        code=EXIT_MISSING_DEP,
                        hint="Use the SDK-built dependency release for Nanvix 0.20.0.",
                    )

    def docker_config(self, image: str):
        """Configure Docker with output files copied back to the workspace.

        On Windows, the build runs in a container-local tmpfs (``/tmp/build``)
        to avoid VirtioFS I/O penalties.  Declare the artifacts produced by
        ``make all`` so they are copied back to the mounted workspace after
        the inner command exits.  ``test_lxml.elf`` is consumed by the
        standalone Linux test from the repo root; install-staged artifacts
        for ``./z release`` are listed by ``_staged_output_files()``.
        """
        cfg = super().docker_config(image)
        cfg.output_files = ["test_lxml.elf"] + self._staged_output_files()
        return cfg

    def _staged_output_files(self) -> list[str]:
        """Return install-staged artifact paths (relative to repo_root())
        so Windows tar-copy mode also copies them back to the host.
        """
        root = repo_root()
        lib = dev_out() / "lib"
        python_out = dev_out() / "python-packages"
        return [
            str((lib / "liblxml_etree.a").relative_to(root)),
            str((lib / "liblxml_elementpath.a").relative_to(root)),
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

        buildroot_p = sysroot_p

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
                f"LIB_OUT={translate(dev_out() / 'lib')}",
                f"INCLUDE_OUT={translate(dev_out() / 'include')}",
                f"TEST_OUT={translate(test_out())}",
                f"PYTHON_OUT={translate(dev_out() / 'python-packages')}",
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
        run(*self._make_args("all"), cwd=repo_root(), docker=self.docker)

    def test(self) -> None:
        """Run the lxml test suite.

        Only functional tests are supported; they cover all test cases.
        The test is run in Python via make_initrd so initrd creation is
        shared across platforms (Linux and Windows).
        """
        if IS_WINDOWS:
            self._run_tests_windows()
            return

        self._run_functional_standalone()

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

        initrd = make_initrd(test_elf, test_out())

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
        and a ramfs for test I/O files.
        """
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

        test_elf: Path | None = None
        # test_out() is the windows-test artifact overlay.
        for candidate in (test_out(), repo_root()):
            p = candidate / "test_lxml.elf"
            if p.is_file():
                test_elf = p
                break
        if test_elf is None:
            log.fatal(
                "test_lxml.elf not found.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z build` first.",
            )

        print("=== lxml functional tests ===")
        print("  Running test_lxml.elf via nanvixd.exe standalone...")

        initrd = make_initrd(test_elf, test_out())

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
