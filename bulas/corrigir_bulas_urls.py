"""
Bulas que não saem por busca automática (fabricante sem resolvedor, produto que a ANVISA
não devolve): o PDF é apontado À MÃO aqui, mas passa pela MESMA validação de conteúdo que
todas as outras — URL dada por humano também erra, e bula errada é o bug que estamos matando.

USO:
  python corrigir_bulas_urls.py --seco
  python corrigir_bulas_urls.py
"""
import sys
import tempfile
from pathlib import Path

import requests

from download_bulas import validar_bula
from corrigir_bulas_sara import slug_do_app

THIS = Path(__file__).parent
SECO = "--seco" in sys.argv

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"}

# genérico do banco -> (url do PDF, o que é)
FONTES: dict[str, tuple[str, str]] = {
    "Empagliflozina": (
        "https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Jardiance-Paciente-Consulta-Remedios.pdf",
        "Jardiance (Boehringer) — empagliflozina pura",
    ),
    "Dextrometorfano": (
        "https://uploads.consultaremedios.com.br/drug_leaflet/pro/Bula-Bisoltussin-Profissional-Consulta-Remedios.pdf",
        "Bisoltussin — dextrometorfano puro",
    ),
    "Irbesartana": (
        "https://img.drogasil.com.br/raiadrogasil_bula/Aprovel.pdf",
        "Aprovel (Sanofi) — irbesartana pura",
    ),
    # NÃO PÔR O QUINACRIS AQUI. Ele parece a bula da quinina pelo nome ("Quinacris",
    # "difosfato de..."), mas é CLOROQUINA — difosfato de cloroquina 150mg, Cristália.
    # Antimalárico DIFERENTE, com toxicidade cardíaca e retiniana própria. A validação de
    # conteúdo reprovou; foi ela que pegou. Quinina continua sem bula brasileira.
}

slugs = slug_do_app(list(FONTES))
print(f"{len(FONTES)} bulas por URL direta{'  [SECO]' if SECO else ''}\n")

ok_count = 0
for generico, (url, descricao) in FONTES.items():
    slug = slugs.get(generico)
    print(f"[{generico}]  → {slug}.pdf")
    print(f"    {descricao}")

    try:
        r = requests.get(url, headers=UA, timeout=60)
    except Exception as e:
        print(f"    ✗ falhou o download: {e}\n")
        continue
    if r.status_code != 200 or not r.content[:4] == b"%PDF":
        print(f"    ✗ não veio PDF (HTTP {r.status_code})\n")
        continue

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(r.content)
        tmp_path = Path(tmp.name)
    try:
        valido, motivo = validar_bula(tmp_path, generico)
        if not valido:
            print(f"    ✗ REPROVADA na validação: {motivo}\n")
            continue
        if not SECO:
            (THIS / f"{slug}.pdf").write_bytes(r.content)
        print(f"    ✓ {len(r.content) // 1024} KB — gravada\n")
        ok_count += 1
    finally:
        tmp_path.unlink(missing_ok=True)

print(f"{'─' * 70}\nCORRIGIDAS: {ok_count}/{len(FONTES)}")
