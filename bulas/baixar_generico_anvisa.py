"""
Troca as bulas de MARCA pelas do GENÉRICO — direto da ANVISA, pela API.

POR QUE A API E NÃO O NAVEGADOR
--------------------------------
O download_bulas.py dirige o bulário com Playwright: espera ~12s o Angular montar e precisa de
120s de intervalo entre itens. Para 506 bulas isso seria mais de 17 horas — inviável, e ainda
por cima o site cai.

Mas o Angular é só a casca. Por trás dele há uma API HTTP, e ela é rápida:

  1. AUTOCOMPLETE   GET /api/produto/listaMedicamentoBula/{TERMO}
                    → nomes de produto que casam com o termo. É aqui que se descobre se o
                      fármaco TEM genérico: buscar "LOSARTANA" devolve "LOSARTANA POTASSICA";
                      buscar "ACARBOSE" devolve VAZIO (não há genérico registrado).

  2. BUSCA          GET /api/consulta/bulario?filter[nomeProduto]={NOME EXATO}
                    → produtos, com o campo idBulaPacienteProtegido.
                    O filtro NÃO é substring: "enalapril" devolve 0, "MALEATO DE ENALAPRIL"
                    devolve 19. Por isso o passo 1 é obrigatório.

  3. PDF            GET /api/consulta/medicamentos/arquivo/bula/parecer/{idBulaPacienteProtegido}/?Authorization=
                    O "id" é um JWT que VENCE EM 5 MINUTOS (exp - nbf = 300s). Não dá para
                    guardar a lista hoje e baixar amanhã: o token tem que ser usado na hora.

TODAS as requisições precisam do cabeçalho `Authorization: Guest` — sem ele, 403.

E precisam sair do `requests` (ou curl), NÃO do Node: a proteção do site recusa a impressão TLS
do Node e devolve uma página de desafio. Foi assim que perdi meia hora achando que era bloqueio
de IP.

AS TRAVAS (as mesmas de trocar_por_generico.py, e cada uma existe por um erro já cometido)
------------------------------------------------------------------------------------------
1. Só produto de nome GENÉRICO, por IGUALDADE de identidade. A regra frouxa aceitou
   "Mononitrato de isossorbida" como genérico de "Isossorbida" e sobrescreveu a bula do ISORDIL
   (DINITRATO) — outro fármaco. Sal não conta ("Maleato de enalapril" == "Enalapril").
2. validar_bula(): o conteúdo é deste fármaco, e sem INTRUSO (o caso Bupropiona/Contrave).
3. A bula tem que se declarar GENÉRICA ("Medicamento genérico, Lei nº 9.787").
4. TRAVA DE HASH: não pode ficar idêntica à bula de OUTRO slug. A validação de conteúdo NÃO
   pega dose/indicação — a busca por "Sildenafila" devolveu o genérico de 20mg, que é a dose da
   HIPERTENSÃO PULMONAR, e o PDF ficou igual ao de sildenafila-hp.pdf.

USO:
  python baixar_generico_anvisa.py --seco --limite=10
  python baixar_generico_anvisa.py --delay=4
"""
import hashlib
import json
import re
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

import requests

from download_bulas import validar_bula
from corrigir_bulas_sara import slug_do_app

THIS = Path(__file__).parent
SECO = "--seco" in sys.argv
LIMITE = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--limite=")), None)
DELAY = float(next((a.split("=")[1] for a in sys.argv if a.startswith("--delay=")), "3"))
ALVOS = THIS / "trocar_por_generico.json"

API = "https://consultas.anvisa.gov.br/api"
HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Authorization": "Guest",
    "Referer": "https://consultas.anvisa.gov.br/",
    "Accept": "application/json, text/plain, */*",
}
GENERICO_RE = re.compile(r"medicamento\s+gen[eé]rico|lei\s*n?[º°.]?\s*9[.\s]?787", re.I)
SAIS = {"maleato", "cloridrato", "sulfato", "potassica", "sodica", "besilato", "mesilato",
        "acetato", "fumarato", "succinato", "tartarato", "bromidrato", "dicloridrato", "citrato"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def identidade(nome: str) -> set[str]:
    return {t for t in re.split(r"[\s\-+]+", norm(nome)) if len(t) >= 5 and t not in SAIS}


def eh_generico_de(produto: str, alvo_ident: set[str]) -> bool:
    """IGUALDADE de identidade — ver a trava 1 no cabeçalho."""
    return bool(alvo_ident) and identidade(produto) == alvo_ident


sessao = requests.Session()
sessao.headers.update(HDRS)


def autocomplete(termo: str) -> list[str]:
    """Nomes de produto que casam com o termo. Vazio = a ANVISA não tem genérico deste fármaco."""
    r = sessao.get(f"{API}/produto/listaMedicamentoBula/{termo}", timeout=40)
    if r.status_code != 200:
        return []
    try:
        d = r.json()
    except Exception:
        return []
    return [x for x in d if isinstance(x, str)] if isinstance(d, list) else []


def buscar(nome_produto: str) -> list[dict]:
    r = sessao.get(f"{API}/consulta/bulario", timeout=40, params={
        "column": "", "count": 10, "filter[nomeProduto]": nome_produto, "order": "asc", "page": 1,
    })
    if r.status_code != 200:
        return []
    try:
        return r.json().get("content", [])
    except Exception:
        return []


def baixar(produto: dict) -> bytes | None:
    """O idBulaPacienteProtegido é um JWT de 5 min — usar AGORA, não guardar."""
    jwt = produto.get("idBulaPacienteProtegido")
    if not jwt:
        return None
    r = sessao.get(f"{API}/consulta/medicamentos/arquivo/bula/parecer/{jwt}/?Authorization=",
                   headers={"Accept": "application/pdf,*/*"}, timeout=90)
    return r.content if r.status_code == 200 and r.content[:4] == b"%PDF" else None


def eh_bula_generica(pdf: Path) -> bool:
    from subprocess import run
    r = run(["pdftotext", "-enc", "UTF-8", "-f", "1", "-l", "2", str(pdf), "-"],
            capture_output=True, timeout=60)
    return bool(GENERICO_RE.search(r.stdout.decode("utf-8", "ignore")[:2500]))


# ── execução ────────────────────────────────────────────────────────────────
hashes = {p.stem: hashlib.sha256(p.read_bytes()).hexdigest() for p in THIS.glob("*.pdf")}
alvos: dict[str, list[str]] = json.loads(ALVOS.read_text(encoding="utf-8"))
itens = list(alvos.items())[:LIMITE] if LIMITE else list(alvos.items())
slugs = slug_do_app([g for g, _ in itens])

print(f"{len(itens)} bulas de MARCA · delay {DELAY}s{'  [SECO]' if SECO else ''}\n", flush=True)

trocadas, sem_generico, reprovadas = [], [], []

for i, (generico, _) in enumerate(itens, 1):
    slug = slugs.get(generico)
    if not slug:
        continue
    alvo = identidade(generico)
    if not alvo:
        continue

    time.sleep(DELAY)
    # o autocomplete casa pelo NOME do produto: busca-se pelo maior token do ativo
    termo = max(alvo, key=len).upper()
    candidatos = [n for n in autocomplete(termo) if eh_generico_de(n, alvo)]
    if not candidatos:
        print(f"[{i}/{len(itens)}] {generico}: a ANVISA não tem genérico (mantém a marca)", flush=True)
        sem_generico.append(generico)
        continue

    # o mais curto: "LOSARTANA POTASSICA" antes de "LOSARTANA POTASSICA + HIDROCLOROTIAZIDA"
    escolhido = min(candidatos, key=len)
    print(f"[{i}/{len(itens)}] {generico}  →  {escolhido}", flush=True)

    time.sleep(DELAY)
    produtos = buscar(escolhido)
    gravou = False
    for prod in produtos[:3]:
        conteudo = baixar(prod)
        if not conteudo:
            continue
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(conteudo)
            tmp_path = Path(tmp.name)
        try:
            ok, motivo = validar_bula(tmp_path, generico)
            if not ok:
                print(f"    ✗ {prod.get('razaoSocial','?')[:22]}: {motivo[:48]}", flush=True)
                reprovadas.append((generico, motivo))
                continue
            if not eh_bula_generica(tmp_path):
                print(f"    ✗ não se declara genérica", flush=True)
                continue
            h = hashlib.sha256(conteudo).hexdigest()
            colisao = next((sl for sl, hh in hashes.items() if hh == h and sl != slug), None)
            if colisao:
                print(f"    ✗ ficaria IDÊNTICA a {colisao}.pdf — dose/indicação diferente", flush=True)
                reprovadas.append((generico, f"colidiria com {colisao}"))
                continue
            if not SECO:
                (THIS / f"{slug}.pdf").write_bytes(conteudo)
                hashes[slug] = h
            print(f"    ✓ {prod.get('razaoSocial','?')[:26]}  ({len(conteudo)//1024} KB)", flush=True)
            trocadas.append((generico, escolhido))
            gravou = True
            break
        finally:
            tmp_path.unlink(missing_ok=True)
        time.sleep(DELAY)

    if not gravou and not any(g == generico for g, _ in reprovadas):
        sem_generico.append(generico)

print(f"\n{'─' * 70}")
print(f"TROCADAS pelo genérico:   {len(trocadas)}")
print(f"ANVISA não tem genérico:  {len(sem_generico)}")
print(f"reprovadas nas travas:    {len(reprovadas)}")
