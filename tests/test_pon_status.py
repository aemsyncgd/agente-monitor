# tests/test_pon_status.py
"""Regresiones del estado Up/Down de los PON en el modal de NODOS / OLTs.

Bug original (commit 863b3de): `get_interfaces()` filtro el pivot a
`rx_bps`/`tx_bps` y descartaba `ifOperStatus`, que es el unico campo del que la
UI deduce el estado del PON. Resultado: todos los PON salian "Down" aunque
InfluxDB tuviera `ifOperStatus = 1` para cada uno.

Ademas el criterio pedido es explicito: si hay al menos un cliente/ONU conectado
al PON, ese PON debe mostrarse Up, aunque el registro de interfaz no exista.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from src.api import influx_helper

INDEX = Path(__file__).resolve().parents[1] / "src" / "api" / "static" / "index.html"
HTML = INDEX.read_text(encoding="utf-8")

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node no disponible")


# --------------------------------------------------------------------------
# 1. El query debe traer ifOperStatus
# --------------------------------------------------------------------------

def test_get_interfaces_selecciona_ifoperstatus(monkeypatch):
    """Regresión del bug: sin ifOperStatus en el pivot la UI no puede
    distinguir Up de Down y lo pinta todo como Down."""
    captured = {}

    def fake_query(flux):
        captured["flux"] = flux
        return []

    monkeypatch.setattr(influx_helper, "_query", fake_query)
    influx_helper.get_interfaces(device_name="OLT-SISAL-1")

    flux = captured["flux"]
    assert "ifOperStatus" in flux, (
        "el query de get_interfaces() no pide ifOperStatus; el modal de NODOS "
        "lo necesita para pintar el estado del PON"
    )
    # No debe abandonar los campos de tasa que ya consumian los dashboards.
    assert '"rx_bps"' in flux and '"tx_bps"' in flux


def test_el_docstring_de_get_interfaces_no_promete_solo_rates():
    """El docstring documentaba el filtro original; si el filtro cambia debe
    cambiar el docstring, o el siguiente que lo lea reintroduce el bug."""
    doc = influx_helper.get_interfaces.__doc__ or ""
    assert "Solo se traen los campos" not in doc


# --------------------------------------------------------------------------
# 2. La regla de estado Up en la UI
# --------------------------------------------------------------------------

def _extract_js_function(name: str) -> str:
    """Extrae una funcion JS por nombre equilibrando llaves."""
    marker = f"function {name}("
    start = HTML.index(marker)
    i = HTML.index("{", start)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[start:j + 1]
    raise AssertionError(f"no se cerro la funcion {name}")


def test_existe_la_regla_de_estado_del_pon():
    assert "function ponUp(" in HTML, (
        "la UI debe decidir el estado del PON en un unico lugar (ponUp), "
        "no con un ternario inline que ya quedo desalineado con los datos"
    )


def test_la_regla_del_pon_se_usa_en_el_template():
    assert "ponUp(" in HTML
    # El ternario roto no debe quedar dando vueltas.
    assert "g.ponIface && g.ponIface.ifOperStatus === 1" not in HTML


def test_el_pon_se_marca_como_up_cuando_tiene_onus_online():
    """Criterio del usuario: >=1 cliente conectado implica PON Up."""
    src = _extract_js_function("ponUp")
    assert "ifOperStatus" in src
    assert "online" in src, (
        "sin respaldo en las ONUs online, un PON sin registro de interfaz "
        "seguiria apareciendo Down"
    )


@requires_node
def test_ponUp_comportamiento():
    """Ejecuta la regla real en node con los casos del bug."""
    src = _extract_js_function("ponUp")
    harness = f"""
{src}
const casos = [
  // [descripcion, grupo, esperado]
  ["interfice operativa (1)", {{ponIface: {{ifOperStatus: 1}}, online: 0}}, "UP"],
  ["interfice operativa (string)", {{ponIface: {{ifOperStatus: "1"}}, online: 0}}, "UP"],
  ["interfice caida (2)", {{ponIface: {{ifOperStatus: 2}}, online: 0}}, "DOWN"],
  ["interfice ausente pero 1 ONU online", {{ponIface: null, online: 1}}, "UP"],
  ["interfice ausente y 0 ONUs online", {{ponIface: null, online: 0}}, "DOWN"],
  ["sin informacion de interfaz", {{}}, "DOWN"],
];
let fallos = 0;
for (const [desc, g, esperado] of casos) {{
  const got = ponUp(g);
  const norm = got === true ? "UP" : (got === false ? "DOWN" : String(got));
  if (norm !== esperado) {{ console.log(`FAIL ${{desc}}: got=${{norm}} esperado=${{esperado}}`); fallos++; }}
}}
console.log(fallos === 0 ? "PONUP_OK" : "PONUP_FAIL " + fallos);
"""
    out = subprocess.run(
        [NODE, "-e", harness], capture_output=True, text=True, timeout=30
    )
    assert "PONUP_OK" in out.stdout, out.stdout + out.stderr


def test_pon_up_y_pon_down_siguen_disponibles_en_el_template():
    """No basta con la regla: el template debe seguir pintando los dos estados."""
    assert "bg-emerald-500/20 text-emerald-400" in HTML
    assert "bg-red-500/20 text-red-400" in HTML


def test_ponUp_se_expone_a_vue():
    """Regresión del mismo tipo que `clientDetail`: un helper usado en el
    template pero ausente del `return` de setup() rompe el render."""
    idx = HTML.rindex("\n        return {")
    depth = 0
    for i in range(idx, len(HTML)):
        if HTML[i] == "{":
            depth += 1
        elif HTML[i] == "}":
            depth -= 1
            if depth == 0:
                assert "ponUp" in HTML[idx:i + 1], "ponUp no se expone a Vue"
                return
    raise AssertionError("no se encontró el return de setup()")
