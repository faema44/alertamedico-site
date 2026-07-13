"""Confere se o audit (JS) e o validador (Python) dão o MESMO veredito em cada PDF.

Os dois implementam a mesma regra em linguagens diferentes — se divergirem, uma bula errada
passa por um e não pelo outro, e o gate de publicação vira teatro. Este script é a prova.
"""
import subprocess
import sys
from pathlib import Path

import download_bulas as d
from corrigir_bulas_sara import slug_do_app

TXT = sys.argv[1]
THIS = Path(__file__).parent

saida = subprocess.run(
    ["node", str(THIS.parent.parent / "tools" / "audit-bulas.js"), TXT],
    capture_output=True, text=True, encoding="utf-8",
)
js_reprova = {l.split(".pdf")[0].strip() for l in saida.stdout.splitlines() if ".pdf  ←" in l}

import json
meds = json.loads((THIS.parent.parent / "src/data/medications-db.json").read_text(encoding="utf-8"))["medications"]
slugs = slug_do_app([m["genericName"] for m in meds])   # generico -> slug

divergencias = []
conferidos = 0
for generico, slug in slugs.items():
    pdf = THIS / f"{slug}.pdf"
    if not pdf.exists():
        continue
    conferidos += 1
    py_ok, motivo = d.validar_bula(pdf, generico)
    js_ok = slug not in js_reprova
    if py_ok != js_ok:
        lado = "JS aprova / PY reprova" if js_ok else "JS reprova / PY aprova"
        divergencias.append((slug, generico, lado, motivo))

print(f"PDFs conferidos: {conferidos}")
print(f"DIVERGÊNCIAS   : {len(divergencias)}")
for slug, gen, lado, motivo in divergencias:
    print(f"  {slug:36} {lado:24} {motivo[:46]}")
