"""Materialize distributor-provided third-party notice sources for a build.

This helper intentionally records package metadata and license/notice files made
available by the installed distributions.  It does not select a project license,
determine license compatibility, or replace a legal review.
"""

from __future__ import annotations

from argparse import ArgumentParser
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from pathlib import Path, PurePosixPath
from shutil import copyfile

BUNDLED_DISTRIBUTIONS = (
    "PySide6",
    "PySide6_Essentials",
    "PySide6_Addons",
    "shiboken6",
    "pypdfium2",
    "opencv-python",
    "numpy",
    "openpyxl",
    "et-xmlfile",
    "tzdata",
    "PyInstaller",
)
NOTICE_FILENAMES = ("license", "licence", "copying", "notice", "copyright")


def _safe_relative_path(path: Path) -> Path:
    pure_path = PurePosixPath(path.as_posix())
    if pure_path.is_absolute() or ".." in pure_path.parts:
        raise ValueError(f"Distribution metadata has an unsafe path: {path}")
    return Path(*pure_path.parts)


def _is_notice_source(path: Path) -> bool:
    lower_parts = tuple(part.lower() for part in path.parts)
    name = path.name.lower()
    return name == "metadata" or any(token in name for token in NOTICE_FILENAMES) or (
        "licenses" in lower_parts
    )


def _copy_distribution_sources(distribution_info: Distribution, destination: Path) -> list[Path]:
    copied: list[Path] = []
    for item in distribution_info.files or ():
        candidate_path = Path(item)
        if not _is_notice_source(candidate_path):
            continue
        relative_path = _safe_relative_path(candidate_path)
        source = Path(str(distribution_info.locate_file(item)))
        if not source.is_file():
            continue
        target = (
            destination / "METADATA"
            if relative_path.name.lower() == "metadata"
            else destination / relative_path
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        copyfile(source, target)
        copied.append(target)
    return copied


def _qt_source_offer() -> list[str]:
    """Where the LGPL-3.0 Qt and Qt for Python sources of this build can be obtained."""
    version = distribution("PySide6").version
    major_minor = ".".join(version.split(".")[:2])
    return [
        "Qt and Qt for Python (PySide6, PySide6_Essentials, PySide6_Addons, shiboken6)",
        "  are used under the GNU Lesser General Public License v3.0. They are shipped as",
        "  separate libraries under _internal/PySide6 and _internal/shiboken6 and may be",
        "  replaced with compatible builds of the same version.",
        f"  Qt {version} source: https://download.qt.io/official_releases/qt/"
        f"{major_minor}/{version}/single/",
        f"  Qt for Python {version} source: https://download.qt.io/official_releases/"
        f"QtForPython/pyside6/PySide6-{version}-src/",
        "",
        "OpenCV's optional FFmpeg video plugin (opencv_videoio_ffmpeg) is not shipped;",
        "  the application reads still images only.",
        "",
    ]


def materialize_notices(output_directory: Path) -> Path:
    """Write an index plus the package-supplied metadata and notice sources."""
    output_directory.mkdir(parents=True, exist_ok=True)
    sources_directory = output_directory / "THIRD_PARTY_NOTICES_SOURCES"
    index_lines = [
        "OMR Grader third-party notice source index",
        "",
        "This bundle is generated from the installed build distributions.",
        "It records their supplied metadata and license/notice source files.",
        "It is not a project license selection or legal-compliance determination.",
        "",
    ]
    for distribution_name in BUNDLED_DISTRIBUTIONS:
        try:
            distribution_info = distribution(distribution_name)
        except PackageNotFoundError as error:
            raise RuntimeError(
                f"Required bundled distribution is unavailable: {distribution_name}"
            ) from error
        destination = sources_directory / distribution_name
        copied = _copy_distribution_sources(distribution_info, destination)
        license_source_count = sum(path.name.lower() != "metadata" for path in copied)
        if license_source_count == 0:
            raise RuntimeError(
                f"Required bundled distribution has no installed license or notice source: "
                f"{distribution_name}"
            )
        if distribution_name == "PyInstaller" and not any(
            path.name.lower() == "copying.txt" for path in copied
        ):
            raise RuntimeError(
                "PyInstaller bootloader COPYING.txt source was not provided by the build distribution"
            )
        metadata = distribution_info.metadata
        declared_license = metadata.get("License", "not declared").splitlines()[0]
        index_lines.extend(
            (
                f"{distribution_name} {distribution_info.version}",
                f"  Metadata license: {declared_license}",
                f"  Source directory: THIRD_PARTY_NOTICES_SOURCES/{distribution_name}",
                f"  License/notice source files: {license_source_count}",
                f"  Captured files including metadata: {len(copied)}",
                "",
            )
        )
    index_lines.extend(_qt_source_offer())
    index_path = output_directory / "THIRD_PARTY_NOTICES.txt"
    index_path.write_text("\n".join(index_lines), encoding="utf-8")
    return index_path


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    materialize_notices(args.output_directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
