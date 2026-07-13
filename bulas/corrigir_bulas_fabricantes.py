"""
Última tentativa para as bulas que nem o Sara nem a ANVISA resolveram: sites OFICIAIS
dos fabricantes.

Reusa os resolvedores já escritos e testados em bulas_fabricantes/download_bulas_fabricantes.py
(Sanofi, União Química, Novo Nordisk, Sara), mas com duas diferenças que importam:

  1. usa os termos CURADOS de refazer_pendentes.json (marca do ingrediente PURO), e não o
     `brands[]` do medications-db — que está contaminado com marca de composto (Amoxicilina
     lista "Clavulanax", Paracetamol lista "Dorilax DF") e é a origem das bulas erradas;
  2. valida com validar_bula() (detecta INTRUSO): o content_matches() daquele script só
     confere se o ativo esperado APARECE, então aceitaria a bula do Contrave para
     "Bupropiona" numa boa — a bupropiona está lá.

USO:
  python corrigir_bulas_fabricantes.py --seco
  python corrigir_bulas_fabricantes.py
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

from download_bulas import validar_bula
from corrigir_bulas_sara import slug_do_app

THIS = Path(__file__).parent
FABRICANTES = Path.home() / "bulas_fabricantes" / "download_bulas_fabricantes.py"
SECO = "--seco" in sys.argv

if not FABRICANTES.exists():
    sys.exit(f"não achei {FABRICANTES}")

spec = importlib.util.spec_from_file_location("fab", FABRICANTES)
fab = importlib.util.module_from_spec(spec)
sys.modules["fab"] = fab
spec.loader.exec_module(fab)

RESOLVEDORES = [
    ("Sanofi",        getattr(fab, "try_sanofi", None)),
    ("União Química", getattr(fab, "try_uniao_quimica", None)),
    ("Novo Nordisk",  getattr(fab, "try_novo_nordisk", None)),
    ("Sara/EMS",      getattr(fab, "try_ems_sara", None)),
    ("Hypera",        getattr(fab, "try_hypera", None)),
]
RESOLVEDORES = [(nome, fn) for nome, fn in RESOLVEDORES if fn]

pendentes = json.loads((THIS / "refazer_pendentes.json").read_text(encoding="utf-8"))
slugs = slug_do_app(list(pendentes))

print(f"{len(pendentes)} pendentes · {len(RESOLVEDORES)} fabricantes"
      f"{'  [SECO]' if SECO else ''}\n")

resultados = {}
for generico, termos in pendentes.items():
    slug = slugs.get(generico)
    print(f"[{generico}]  → {slug}.pdf")
    achou = None

    for termo in termos:
        for nome_fab, resolver in RESOLVEDORES:
            try:
                r = resolver(termo)
            except Exception as e:
                continue
            if not r or not r.get("content"):
                continue

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(r["content"])
                tmp_path = Path(tmp.name)
            try:
                ok, motivo = validar_bula(tmp_path, generico)
                rotulo = f"{nome_fab}/{termo}"
                if not ok:
                    print(f"    ✗ {rotulo:32} {motivo[:52]}")
                    continue
                if not SECO:
                    (THIS / f"{slug}.pdf").write_bytes(r["content"])
                print(f"    ✓ {rotulo:32} {r.get('full_name','?')[:38]} ({len(r['content'])//1024} KB)")
                achou = {"status": "corrigida", "fabricante": nome_fab, "termo": termo}
                break
            finally:
                tmp_path.unlink(missing_ok=True)
        if achou:
            break

    if not achou:
        print("    — nenhum fabricante tem a bula pura")
        achou = {"status": "sem_bula_valida"}
    resultados[generico] = achou

corrigidas = [g for g, r in resultados.items() if r["status"] == "corrigida"]
print(f"\n{'─' * 70}\nCORRIGIDAS: {len(corrigidas)}/{len(pendentes)}")
for g, r in resultados.items():
    if r["status"] != "corrigida":
        print(f"   • {g}")
