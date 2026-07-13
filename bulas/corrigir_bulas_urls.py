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
    # ── achados pela checagem de HASH (PDFs idênticos em slugs diferentes) ────────
    # A validação de conteúdo só procura intruso nas 3 primeiras linhas e não viu nenhum
    # destes: a capa trazia só o nome comercial, ou o produto errado tinha o mesmo nome-raiz.
    "Sacarato de Hidróxido Férrico": (
        "https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Noripurum-EV-Paciente-Consulta-Remedios.pdf",
        "Noripurum EV — sacarato de hidróxido férrico (EV). Estava com o Noripurum ORAL, "
        "que é ferripolimaltose: outro produto, outra via.",
    ),
    "Sulfato Ferroso + Ácido Fólico": (
        "https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Neutrofer-Folico-Paciente-Consulta-Remedios.pdf",
        "Neutrofer Fólico — sulfato ferroso + ácido fólico. O slug do COMPOSTO estava com a "
        "bula do FURP-Sulfato Ferroso puro, sem ácido fólico nenhum.",
    ),
    "IMUNOGLOBULINA HUMANA ESPECÍFICA ANTI-D": (
        "https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Rhophylac-Paciente-Consulta-Remedios.pdf",
        "Rhophylac — imunoglobulina anti-D (Rh). Estava com a bula da imunoglobulina humana "
        "NORMAL (Imunoglobulin® Blau): indicação completamente diferente.",
    ),
    # ── bulas que FALTAVAM (medicamento sem PDF nenhum publicado) ────────────────
    # Estes 12 aparecem nas interações CRÍTICAS sem fonte, e não tinham bula: o app abria link
    # quebrado E a interação ficava órfã. A ANVISA está fora do ar e os resolvedores de
    # fabricante que temos (Sanofi, União Química, Novo Nordisk, Sara/EMS, Hypera) não cobrem
    # Novartis, Pfizer, Janssen nem Roche — que é de quem são quase todos.
    "Sulpirida":     ("https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Equilid-Paciente-Consulta-Remedios.pdf", "Equilid (Sanofi) — sulpirida"),
    "Amisulprida":   ("https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Socian-Paciente-Consulta-Remedios.pdf", "Socian (Sanofi) — amisulprida"),
    "Selegilina":    ("https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Niar-Paciente-Consulta-Remedios.pdf", "Niar — selegilina"),
    "Maprotilina":   ("https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Ludiomil-Paciente-Consulta-Remedios.pdf", "Ludiomil (Novartis) — maprotilina"),
    "Moclobemida":   ("https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Aurorix-Paciente-Consulta-Remedios.pdf", "Aurorix (Roche) — moclobemida"),
    "Cloxazolam":    ("https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Olcadil-Paciente-Consulta-Remedios.pdf", "Olcadil — cloxazolam"),
    "Pimozida":      ("https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Orap-Paciente-Consulta-Remedios.pdf", "Orap (Janssen) — pimozida"),
    "Reboxetina":    ("https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Prolift-Paciente-Consulta-Remedios.pdf", "Prolift (Pfizer) — reboxetina"),
    "Tofacitinibe":  ("https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Xeljanz-Paciente-Consulta-Remedios.pdf", "Xeljanz (Pfizer) — tofacitinibe"),
    "Asenapina":     ("https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Saphris-Paciente-Consulta-Remedios.pdf", "Saphris — asenapina"),
    "Sacubitril + Valsartana": (
        "https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Entresto-Paciente-Consulta-Remedios.pdf",
        "Entresto (Novartis) — sacubitril + valsartana",
    ),
    # SUSPEITO, e é o validador que decide: o Migraliv parece ser COMPOSTO (dipirona +
    # diidroergotamina + cafeína), e o slug aqui é do ingrediente PURO. Se for, é exatamente o
    # bug do Bupropiona/Contrave — a detecção de intruso do validar_bula() reprova e o PDF não
    # é gravado. Deixado aqui de propósito: a hipótese fica testada, não esquecida.
    "Ergotamina":    ("https://uploads.consultaremedios.com.br/drug_leaflet/Bula-Migraliv-Paciente-Consulta-Remedios.pdf", "Migraliv — ERGOTAMINA? conferir se é composto"),

    "Umeclidínio": (
        "https://br.gsk.com/media/6688/incruse-ellipta.pdf",
        "Incruse Ellipta (GSK, site oficial) — umeclidínio PURO. O slug estava com o Trelegy, "
        "que é TRIPLA (fluticasona + umeclidínio + vilanterol). A capa do Trelegy só traz o nome "
        "comercial e '100mcg + 62,5mcg + 25mcg', sem citar ativo — por isso a validação de "
        "conteúdo (que só lê as 3 primeiras linhas) não viu o intruso. Quem pegou foi o hash.",
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
