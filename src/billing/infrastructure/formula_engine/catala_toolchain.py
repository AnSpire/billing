"""Тонкая обёртка над тулчейном Catala (`catala`/`clerk`) — вызывается из
``application/tariff_validation.py`` на шаге ``Validate`` (PLAN.md, фаза 7:
«Validate — здесь происходит сборка: clerk build (компиляция)...»,
billing_aggregates.md §1).

Пайплайн — тот же, что отработан руками в ``catala_research/case3``
(``Makefile`` + ``build_pyrun.sh``; см. также ``BECARE.md`` §3 про то, почему
транспиляция в Python не самодостаточна: сгенерированный модуль импортирует
рантайм и stdlib относительными импортами, их нужно собрать в один пакет).
Здесь тот же пайплайн вызывается программно из Python вместо `make`.

Два уровня кэша под ``build_root()`` (по умолчанию ``<repo>/_build/catala``,
переопределяется ``BILLING_CATALA_BUILD_DIR``):

- ``stdlib_src/`` и ``stdlib_py/`` — стандартная библиотека Catala, одна на
  все тарифы (не зависит от конкретной модели), строится один раз;
- ``artifacts/<source_hash>/`` — собранный пакет конкретного тарифа;
  переисполнение ``compile_source`` с тем же текстом — no-op (артефакт уже
  на диске).

Пакет каждого артефакта называется ``t_<hash12>`` (не общим именем вроде
``pkg``) — так разные тарифы, импортированные в одном процессе, не
сталкиваются в ``sys.modules``.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_OPAM_SWITCH = "catala"
# _internal externals (BECARE.md §3) — рукописные рантайм-шимы, транспиляции
# не подлежат, только копируются с заглавной буквы под именем модуля.
_INTERNAL_MODULES = ("decimal", "date", "list", "money", "period")


class CatalaCompilationError(Exception):
    """``catala typecheck``/``catala python`` упали — ошибка формализации
    (PLAN.md, фаза 7: «падать она должна там, где это ошибка формализации,
    а не в момент публикации»), а не инфраструктурная случайность."""

    def __init__(self, message: str, *, stderr: str) -> None:
        super().__init__(message)
        self.stderr = stderr


@dataclass(frozen=True)
class CompiledArtifact:
    package_dir: Path
    package_name: str
    module_name: str
    source_hash: str
    compiler_version: str
    runtime_version: str


def build_root() -> Path:
    import os

    override = os.environ.get("BILLING_CATALA_BUILD_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[4] / "_build" / "catala"


@lru_cache(maxsize=1)
def _opam_prefix() -> Path:
    result = subprocess.run(
        ["opam", "var", f"--switch={_OPAM_SWITCH}", "prefix"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def _catala_bin() -> str:
    return str(_opam_prefix() / "bin" / "catala")


def _clerk_bin() -> str:
    return str(_opam_prefix() / "bin" / "clerk")


def _runtime_python_root() -> Path:
    """``catala_runtime.py``/``dates.py`` живут в ``src/catala/`` этого
    каталога; рукописные ``*_internal.py`` шимы — прямо в нём (раскладка
    пакета ``catala`` из ``opam install catala``, а не так, как в
    ``BECARE.md`` — там для версии 1.2.0 они были рядом с ``catala_runtime.py``;
    в 1.2.1 разложены по-другому, проверено запуском, не по памяти)."""
    return _opam_prefix() / "lib" / "catala" / "runtime" / "python"


@lru_cache(maxsize=1)
def compiler_version() -> str:
    result = subprocess.run(
        [_catala_bin(), "--version"], capture_output=True, text=True, check=True
    )
    return f"catala-{result.stdout.strip()}"


def runtime_version() -> str:
    """Один тулчейн собирает и компилятор, и рантайм-библиотеку — здесь это
    одна связка. billing_aggregates.md разводит ``compiler_version`` и
    ``runtime_version`` на случай их рассинхронизации в будущем (например,
    рантайм с закреплённой версией при обновлении компилятора) — тогда это
    поле перестанет быть равно ``compiler_version``, интерфейс уже готов."""
    return compiler_version()


def source_hash(source_text: str) -> str:
    """sha256 **исходника Catala**, не сгенерированного Python
    (billing_aggregates.md §1: кодогенерация может быть недетерминированной
    в мелочах, хеш от неё давал бы разные значения при идентичной семантике)."""
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()


def _module_name(source_text: str) -> str:
    match = re.search(r"^>\s*Module\s+(\S+)", source_text, re.MULTILINE)
    if not match:
        raise CatalaCompilationError(
            "source has no '> Module <Name>' declaration", stderr=""
        )
    return match.group(1)


def _run(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def _stdlib_source_dir() -> Path:
    """``clerk start`` в общем кэш-каталоге — исходники стандартной
    библиотеки Catala одни на все тарифы."""
    root = build_root() / "stdlib_src"
    lib = root / "_build" / "libcatala"
    if (lib / "stdlib_en.catala_en").exists():
        return lib
    root.mkdir(parents=True, exist_ok=True)
    (root / "clerk.toml").touch(exist_ok=True)
    result = _run([_clerk_bin(), "start"], cwd=root)
    if result.returncode != 0:
        raise CatalaCompilationError(
            "clerk start failed to set up the Catala stdlib", stderr=result.stderr
        )
    return lib


def _stdlib_python_cache() -> Path:
    """Транспилирует стандартную библиотеку в Python один раз в
    ``stdlib_py/`` (``catala_runtime.py``/``dates.py`` — для sys.path;
    ``pkg/*.py`` — заготовка для копирования в пакет каждого артефакта, см.
    docstring модуля)."""
    out = build_root() / "stdlib_py"
    marker = out / ".built"
    if marker.exists():
        return out

    stdlib_src = _stdlib_source_dir()
    runtime_root = _runtime_python_root()
    pkg = out / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)

    for name in ("catala_runtime.py", "dates.py"):
        (out / name).write_bytes((runtime_root / "src" / "catala" / name).read_bytes())

    for mod in _INTERNAL_MODULES:
        capitalized = mod.capitalize()
        (pkg / f"{capitalized}_internal.py").write_bytes(
            (runtime_root / f"{mod}_internal.py").read_bytes()
        )

    for source_file in sorted(stdlib_src.glob("*_en.catala_en")):
        module_name = _module_name(source_file.read_text())
        result = _run(
            [
                _catala_bin(),
                "python",
                "-I",
                str(stdlib_src),
                "--stdlib",
                str(stdlib_src),
                str(source_file),
                "-o",
                str(pkg / f"{module_name}.py"),
            ],
            cwd=build_root(),
        )
        if result.returncode != 0:
            raise CatalaCompilationError(
                f"failed to transpile stdlib module {source_file.name}", stderr=result.stderr
            )

    marker.touch()
    return out


def compile_source(source_text: str) -> CompiledArtifact:
    """``catala typecheck`` -> (если успешно) транспиляция в Python + сборка
    standalone-пакета. Поднимает ``CatalaCompilationError`` на любой стадии.

    Идемпотентно по содержимому: повторный вызов с тем же ``source_text``
    находит уже собранный артефакт под ``artifacts/<hash>/`` и не
    пересобирает его."""
    digest = source_hash(source_text)
    module_name = _module_name(source_text)
    package_name = f"t_{digest[:12]}"
    artifact_dir = build_root() / "artifacts" / digest

    if (artifact_dir / package_name / f"{module_name}.py").exists():
        return CompiledArtifact(
            package_dir=artifact_dir,
            package_name=package_name,
            module_name=module_name,
            source_hash=digest,
            compiler_version=compiler_version(),
            runtime_version=runtime_version(),
        )

    stdlib_src = _stdlib_source_dir()
    stdlib_py = _stdlib_python_cache()

    work_dir = build_root() / "_tmp_compile" / digest
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    try:
        (work_dir / "clerk.toml").touch()
        source_file = work_dir / f"{module_name.lower()}.catala_en"
        source_file.write_text(source_text)

        typecheck = _run(
            [_catala_bin(), "typecheck", "-I", str(work_dir), "--stdlib", str(stdlib_src), str(source_file)],
            cwd=work_dir,
        )
        if typecheck.returncode != 0:
            raise CatalaCompilationError(
                "catala typecheck failed", stderr=typecheck.stdout + typecheck.stderr
            )

        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
        pkg_dir = artifact_dir / package_name
        shutil.copytree(stdlib_py / "pkg", pkg_dir)
        for name in ("catala_runtime.py", "dates.py"):
            shutil.copy(stdlib_py / name, artifact_dir / name)

        transpile = _run(
            [
                _catala_bin(),
                "python",
                "-I",
                str(stdlib_src),
                "-I",
                str(work_dir),
                "--stdlib",
                str(stdlib_src),
                str(source_file),
                "-o",
                str(pkg_dir / f"{module_name}.py"),
            ],
            cwd=work_dir,
        )
        if transpile.returncode != 0:
            shutil.rmtree(artifact_dir, ignore_errors=True)
            raise CatalaCompilationError(
                "catala python transpilation failed", stderr=transpile.stdout + transpile.stderr
            )
        (pkg_dir / "__init__.py").touch()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return CompiledArtifact(
        package_dir=artifact_dir,
        package_name=package_name,
        module_name=module_name,
        source_hash=digest,
        compiler_version=compiler_version(),
        runtime_version=runtime_version(),
    )
