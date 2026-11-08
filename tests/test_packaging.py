"""Guard del sdist.

hatchling no ve `.git/info/exclude`: sin una allowlist explícita en
pyproject, `uv build` puede empaquetar archivos no trackeados del árbol de
trabajo (documentos internos, artefactos locales) en el sdist. Este test no
ejecuta el build (lento para CI): verifica el CONTRATO del pyproject — el
target sdist debe declarar una allowlist y solo rutas públicas pueden ser
alcanzables por ella. La verificación del artefacto real (`tar -tzf` tras
`uv build`) queda como paso manual pre-publicación.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Rutas públicas que deben viajar en el sdist (allowlist exacta).
PUBLICOS = {
    "src",
    "tests",
    "docs",
    ".github",
    "README.md",
    "README.es.md",
    "SECURITY.md",
    "LICENSE",
    "config.example.toml",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    ".gitignore",
}


def _sdist_config() -> dict:  # type: ignore[no-untyped-def]
    with (ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    target = data["tool"]["hatch"]["build"]["targets"].get("sdist")
    assert isinstance(target, dict), (
        "falta [tool.hatch.build.targets.sdist]: sin allowlist, el sdist "
        "empaqueta TODO el árbol de trabajo"
    )
    return target


def test_sdist_allowlist_excluye_internos() -> None:
    include = _sdist_config().get("include")
    assert isinstance(include, list) and include, (
        "el sdist debe usar include= (allowlist): exclude= por lista corta "
        "deja pasar cualquier archivo interno futuro"
    )
    patrones = {str(p).lstrip("/") for p in include}
    sobrantes = patrones - PUBLICOS
    assert not sobrantes, f"rutas no públicas en la allowlist del sdist: {sobrantes}"
    # los fuentes y los assets públicos SÍ viajan
    assert any("src" in str(p) for p in include)
    assert any("README" in str(p) for p in include)


def test_sdist_allowlist_cubre_lo_publico() -> None:
    """La allowlist debe ser exactamente el conjunto de rutas públicas: si se
    añade un archivo público sin tocar esta lista, el sdist lo pierde; si se
    cuela una ruta no pública, el primer test falla (visible, no silencioso)."""
    include = _sdist_config()["include"]
    patrones = {str(p).lstrip("/") for p in include}
    faltan = PUBLICOS - patrones
    assert not faltan, f"archivos públicos fuera de la allowlist del sdist: {faltan}"
