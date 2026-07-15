"""
Downloader de bulas POR FORMA FARMACÊUTICA — via API da ANVISA (não Playwright).

Um princípio ativo (Dexametasona) tem bulas diferentes por apresentação: comprimido,
creme, gotas, xarope, colírio... Hoje é 1 slug por sal (a forma publicada por último
ganha — foi assim que a dexametasona virou creme no lugar do comprimido). Este script
baixa a bula de CADA forma e salva <slug>-<forma>.pdf. As formas-alvo de cada sal vêm de
analise-bulas-med-br/manifesto-multiforma.tsv.

POR QUE A API (e não o Playwright do download_bulas.py): a API é headless, rápida e não
sofre com o "limita por rajada" do bulário Angular. Mesmo fluxo do baixar_generico_anvisa.py:
  1. autocomplete  GET /api/produto/listaMedicamentoBula/{TERMO}  → nomes de produto
  2. busca         GET /api/consulta/bulario?filter[nomeProduto]={NOME EXATO}
  3. PDF           GET /api/consulta/medicamentos/arquivo/bula/parecer/{jwt}/?Authorization=
Header `Authorization: Guest` obrigatório; JWT vence em 5 min; TEM que ser requests, não Node.

A forma NÃO vem no JSON (só idProduto/nomeProduto/razaoSocial/jwt...) — por isso a forma é
lida do PDF (campo "FORMA FARMACÊUTICA"/capa). Combo é filtrado por validar_bula (conteúdo),
NÃO por '+' no texto (isso reprova bula correta de "pó + diluente").

USO:
  python download_multiforma.py --so=dexametasona --seco     # 1 sal, não grava
  python download_multiforma.py --limite=5
  python download_multiforma.py --resumir                     # pula formas já no disco
  python download_multiforma.py --delay=4
"""
import re
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

import requests

from download_bulas import validar_bula   # filtro de combo/intruso por conteúdo (import-safe)

THIS = Path(__file__).parent               # site/bulas — onde ficam os PDFs publicados
MEDALERT = THIS.parent.parent
MANIFESTO = MEDALERT / "analise-bulas-med-br" / "manifesto-multiforma.tsv"

def _flag(name, default=""):
    return next((a.split("=", 1)[1] for a in sys.argv if a.startswith(f"{name}=")), default)

SO       = _flag("--so").lower()
SECO     = "--seco" in sys.argv            # não grava nada
RESUMIR  = "--resumir" in sys.argv
LIMITE   = int(_flag("--limite", "0")) or None
DELAY    = float(_flag("--delay", "3"))    # segundos entre CADA requisição à ANVISA
MAX_CAND = int(_flag("--max-cand", "16"))  # teto de PDFs baixados por sal

# ── API ANVISA ────────────────────────────────────────────────────────────────
API = "https://consultas.anvisa.gov.br/api"
sessao = requests.Session()
sessao.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Authorization": "Guest",
    "Referer": "https://consultas.anvisa.gov.br/",
    "Accept": "application/json, text/plain, */*",
})

def autocomplete(termo):
    try:
        d = sessao.get(f"{API}/produto/listaMedicamentoBula/{termo}", timeout=40).json()
        return [x for x in d if isinstance(x, str)] if isinstance(d, list) else []
    except Exception:
        return []

def buscar(nome_produto):
    try:
        r = sessao.get(f"{API}/consulta/bulario", timeout=40, params={
            "column": "", "count": 20, "filter[nomeProduto]": nome_produto,
            "order": "asc", "page": 1,
        })
        return r.json().get("content", []) if r.status_code == 200 else []
    except Exception:
        return []

def baixar(produto):
    """idBulaPacienteProtegido é JWT de 5 min — usar agora."""
    jwt = produto.get("idBulaPacienteProtegido")
    if not jwt:
        return None
    try:
        r = sessao.get(f"{API}/consulta/medicamentos/arquivo/bula/parecer/{jwt}/?Authorization=",
                       headers={"Accept": "application/pdf,*/*"}, timeout=90)
        return r.content if r.status_code == 200 and r.content[:4] == b"%PDF" else None
    except Exception:
        return None

# ── identidade (réplica da regra do app: sal/forma/íon-contra não é identidade) ──
def _norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    return re.sub(r"\s+", " ", "".join(c for c in s if unicodedata.category(c) != "Mn")).strip()

SEM_IDENTIDADE = {"acido","acida","cloridrato","dicloridrato","bromidrato","mesilato","besilato",
    "maleato","tartarato","succinato","fumarato","valerato","propionato","dipropionato","furoato",
    "pamoato","oxalato","gluconato","acetato","citrato","lactato","nitrato","sulfato","cloreto",
    "carbonato","bicarbonato","fosfato","hidroxido","oxido","dissodico","dissodica","monoidratado",
    "monoidratada","diidratado","anidro"}
IONS = {"sodio","sodica","sodico","potassio","potassica","potassico","calcio","calcica","calcico",
    "magnesio","aluminio","zinco","ferro","ferroso","ferrica","ferrico"}
GEN = {"de","do","da"}

def identidade(nome):
    seq = [t for t in re.split(r"[\s,]+", _norm(nome)) if t]
    palavras = [t for t in seq if len(t) >= 3 and t not in SEM_IDENTIDADE]
    contra = {seq[k] for k in range(1, len(seq)) if seq[k] in IONS and seq[k-1] in GEN}
    resto = [t for t in palavras if t not in contra]
    return frozenset(resto or palavras)

# ── forma farmacêutica a partir do texto do PDF ───────────────────────────────
def pdf_texto(pdf):
    from subprocess import run
    try:
        r = run(["pdftotext", "-enc", "UTF-8", "-f", "1", "-l", "2", str(pdf), "-"],
                capture_output=True, timeout=40)
        return r.stdout.decode("utf-8", "replace")
    except Exception:
        return ""

# (sufixo, regex) — ORDEM = prioridade; específico antes de genérico (oftálmica antes de solução).
FORMA_REGRAS = [
    ("ocular",      r"oftalmic|colirio|ocular"),   # convenção do site: dexametasona-ocular.pdf
    ("injetavel",   r"injetavel|infus[ãa]o|intraven|liofilizad|p[óo] para solu[çc][aã]o inj"),
    ("spray",       r"aerossol|\bspray\b|inala[çc]|nebuliz|nasal"),
    ("supositorio", r"supositorio"),
    ("adesivo",     r"adesivo|transderm"),
    ("creme",       r"\bcreme\b"),
    ("pomada",      r"\bpomada\b"),
    ("locao",       r"lo[çc][aã]o"),
    ("gel",         r"\bgel\b"),
    ("gotas",       r"\bgotas\b"),
    ("xarope",      r"xarope|elixir"),
    ("suspensao",   r"suspens[aã]o oral|solu[çc][aã]o oral"),
    ("po",          r"granulad|efervescente|p[óo] para"),
    ("capsula",     r"c[áa]psula"),
    ("comprimido",  r"comprimid|dr[áa]gea|pastilha|sublingual"),
]

def classificar(texto):
    # A forma é DECLARADA na capa/topo ("Dexametasona elixir", "Creme dermatológico").
    # A prioridade da lista resolve "específico antes de genérico" (gotas vence
    # solução oral; pó para suspensão vence suspensão). A janela PÁRA antes das
    # INDICAÇÕES: lá reaparecem palavras como "ocular"/"injetável" que são doença
    # tratada, não a forma (foi o que fez a dexametasona comprimido virar "ocular").
    bloco = _norm(texto)[:800]
    for suf, rx in FORMA_REGRAS:
        if re.search(rx, bloco):
            return suf
    return None

# ── manifesto ─────────────────────────────────────────────────────────────────
def carregar_manifesto():
    """{ principio_ativo: { sufixo: 'slug-forma.pdf' } }."""
    alvos = {}
    for ln in MANIFESTO.read_text(encoding="utf-8").splitlines()[1:]:
        p = ln.split("\t")
        if len(p) < 3:
            continue
        nome, slug_pdf = p[0].strip(), p[2].strip()
        alvos.setdefault(nome, {})[slug_pdf[:-4].rsplit("-", 1)[-1]] = slug_pdf
    return alvos

# ── processamento de um sal ───────────────────────────────────────────────────
def processar_sal(generico, alvo):
    pendentes = dict(alvo)
    if RESUMIR:
        for suf, slug in list(pendentes.items()):
            if (THIS / slug).exists():
                del pendentes[suf]
    if not pendentes:
        print("    ↷ todas as formas já existem")
        return {}

    ident = identidade(generico)
    if not ident:
        return {}
    # O autocomplete casa por PREFIXO e diferencia acento ('TRANEXAMICO'→0, 'ÁCIDO
    # TRANEXÂMICO'→ok). Então busca por dois ângulos: o nome COMPLETO acentuado (pega
    # 'Ácido X', 'Furoato de X') e o maior token isolado (pega o produto puro 'X').
    termos, vt = [], set()
    for t in (generico.upper(), max(ident, key=len).upper()):
        if t not in vt:
            vt.add(t); termos.append(t)

    nomes = []
    for t in termos:
        time.sleep(DELAY)
        nomes += [n for n in autocomplete(t) if "+" not in n and identidade(n) == ident]
    # dedup preservando ordem, do mais curto (puro) pro mais longo
    vistos, candidatos = set(), []
    for n in sorted(nomes, key=len):
        k = n.upper()
        if k not in vistos:
            vistos.add(k); candidatos.append(n)
    if not candidatos:
        print(f"    ⚠ a ANVISA não devolveu produto puro para '{generico}'")
        return {}

    obtidos, baixados = {}, 0
    for nome in candidatos:
        if not pendentes or baixados >= MAX_CAND:
            break
        time.sleep(DELAY)
        for prod in buscar(nome):
            if not pendentes or baixados >= MAX_CAND:
                break
            time.sleep(DELAY)
            conteudo = baixar(prod)
            baixados += 1
            if not conteudo:
                continue
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as t:
                t.write(conteudo); tmp = Path(t.name)
            try:
                ok, motivo = validar_bula(tmp, generico)
                if not ok:
                    print(f"      ✗ {prod.get('razaoSocial','?')[:20]}: {motivo[:44]}")
                    continue
                suf = classificar(pdf_texto(tmp))
                if suf and suf in pendentes:
                    slug = pendentes.pop(suf)
                    if not SECO:
                        (THIS / slug).write_bytes(conteudo)
                    obtidos[suf] = {"file": slug, "produto": nome,
                                    "razao": prod.get("razaoSocial", "?")}
                    print(f"      ✓ {suf:11s} → {slug}  ({prod.get('razaoSocial','?')[:24]})")
                # forma repetida ou fora do alvo: descarta em silêncio
            finally:
                tmp.unlink(missing_ok=True)

    if pendentes:
        print(f"    ⚠ faltaram: {', '.join(pendentes)}")
    return obtidos

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    alvos = carregar_manifesto()
    itens = [(n, a) for n, a in alvos.items() if not SO or SO in n.lower()]
    if LIMITE:
        itens = itens[:LIMITE]

    total = sum(len(a) for _, a in itens)
    print(f"Multiforma via API ANVISA — {len(itens)} sais · {total} formas-alvo · "
          f"delay {DELAY}s{'  [SECO]' if SECO else ''}\n", flush=True)

    feitas = 0
    for i, (generico, alvo) in enumerate(itens, 1):
        print(f"[{i}/{len(itens)}] {generico}  ({', '.join(alvo)})", flush=True)
        try:
            feitas += len(processar_sal(generico, alvo))
        except Exception as exc:
            print(f"    ✗ exceção: {exc}", flush=True)

    print(f"\n{'─'*60}\nCONCLUÍDO: {feitas} PDFs por forma gravados{'  (SECO — nada gravado)' if SECO else ''}")

if __name__ == "__main__":
    main()
