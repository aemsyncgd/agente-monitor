"""Regresiones de la UI de Buscar Cliente.

El bug original: `clientDetail` y `showClientDetailModal` se usaban en el
template pero NO se devolvían desde setup(), así que "Ver detalle" no
abriría nada. Estos tests fijan ese contrato sin necesidad de navegador.
"""
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "src" / "api" / "static" / "index.html"
HTML = INDEX.read_text(encoding="utf-8")


def _setup_return_block() -> str:
    """Bloque `return { ... }` final de setup()."""
    idx = HTML.rindex("\n        return {")
    depth = 0
    for i in range(idx, len(HTML)):
        if HTML[i] == "{":
            depth += 1
        elif HTML[i] == "}":
            depth -= 1
            if depth == 0:
                return HTML[idx:i + 1]
    raise AssertionError("no se encontró el return de setup()")


def test_el_modal_de_cliente_se_exporta_al_template():
    """Regresión: sin esto, el botón "Ver detalle" no abre nada."""
    ret = _setup_return_block()
    assert "clientDetail" in ret, "clientDetail no se expone a Vue"
    assert "showClientDetailModal" in ret, "showClientDetailModal no se expone a Vue"


def test_los_estados_de_busqueda_se_exportan():
    ret = _setup_return_block()
    for name in ("clientSearchLoading", "clientSearchError",
                 "clientSearchTruncated", "clientSearchSummary",
                 "clientDetailLoading"):
        assert name in ret, f"{name} no se expone a Vue"


def test_los_ayudantes_usados_en_el_template_se_exportan():
    ret = _setup_return_block()
    for name in ("clientSearchQueries", "clientHistoryBars", "infraStatusClass",
                 "fmtNum"):
        assert name in ret, f"{name} se usa en el template pero no se expone"


def test_el_detalle_muestra_las_medidas_de_infraestructura():
    """`ping_check` guarda latencia, pérdida y downtime: si el modal no las
    pinta, el dato se pierde aunque el backend lo devuelva."""
    for campo in ("latencia_ms", "perdida_pct", "downtime_s"):
        assert campo in HTML, f"{campo} no se muestra en el modal"


def test_no_queda_el_parser_por_cliente_antiguo():
    """El parser por cliente quedó obsoleto al clasificar el backend."""
    assert "clientSearchParams" not in HTML


def test_la_busqueda_usa_el_endpoint_batch():
    assert "/api/clientes/vital/batch" in HTML


def test_la_busqueda_es_ligera_y_el_detalle_es_difido():
    """Listar no debe pagar histórico/infra; el modal sí."""
    body = HTML[HTML.index("async function searchClients()"):
                HTML.index("function clientSearchQueries()")]
    assert "include_history: false" in body
    assert "include_infra: false" in body
    detail = HTML[HTML.index("async function openClientDetail(c)"):
                  HTML.index("function closeClientDetail()")]
    assert "include_history: true" in detail
    assert "include_infra: true" in detail


def test_el_template_muestra_los_campos_pedidos():
    for campo in ("c.ficha.olt", "c.ficha.pon_port", "c.ficha.nap",
                  "c.ficha.serial_onu", "c.ficha.rx_power", "c.ficha.tx_power"):
        assert campo in HTML, f"falta {campo} en la tarjeta"
    for bloque in ("clientDetail.historico_30d", "clientDetail.infraestructura"):
        assert bloque in HTML, f"falta {bloque} en el modal"


def test_el_script_principal_no_esta_roto():
    """Guard cheap contra un JS sintácticamente inválido tras editar a mano."""
    const = HTML.index("const clientSearch = ref")
    ini = HTML.rindex("<script>", 0, const)
    fin = HTML.index("</script>", const)
    cuerpo = HTML[ini + len("<script>"):fin]
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(cuerpo)
        ruta = fh.name
    try:
        r = subprocess.run(["node", "--check", ruta], capture_output=True,
                           text=True)
        assert r.returncode == 0, r.stderr
    except FileNotFoundError:
        pass  # node no disponible: el resto de tests siguen siendo válidos


def test_los_bloques_de_tags_estan_balanceados():
    ini = HTML.index("<!-- Client Search -->")
    fin = HTML.index("<!-- LLM Agent Panel -->", ini)
    region = HTML[ini:fin]
    for tag in ("div", "span", "button"):
        assert region.count(f"<{tag}") == region.count(f"</{tag}>"), (
            f"etiquetas <{tag}> desbalanceadas en la región del modal")
