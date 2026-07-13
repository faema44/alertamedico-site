"""
Corrige as bulas de CONTEÚDO ERRADO já publicadas em site/bulas, usando a plataforma
Sara (sara.com.br — EMS e outros fabricantes) como fonte.

Por que o Sara e não a ANVISA: o problema a resolver é bula de medicamento COMPOSTO
ocupando o slug do ingrediente PURO (Bupropiona abrindo a bula do Contrave). O Sara é
catálogo de fabricante de GENÉRICO, onde o produto é justamente o princípio ativo puro —
e é HTTP, sem o Angular lento/instável do bulário.

O que este script faz de diferente do download_bulas_fabricantes.py:
  1. percorre VÁRIOS resultados da busca, não só o primeiro (results[0] às vezes é o
     composto — foi assim que a bula errada entrou);
  2. valida o conteúdo com detecção de INTRUSO (validar_bula de download_bulas.py):
     não basta a bula citar "bupropiona", ela não pode declarar naltrexona junto.
  Só grava depois de passar nas duas.

USO:
  python corrigir_bulas_sara.py            # aplica (grava em site/bulas/)
  python corrigir_bulas_sara.py --seco     # só mostra o que faria, não grava
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

import requests

from download_bulas import validar_bula

THIS_DIR = Path(__file__).parent
ROOT     = THIS_DIR.parent.parent
REFAZER  = THIS_DIR / "refazer_termos.json"
SECO     = "--seco" in sys.argv

MAX_CANDIDATOS = 6
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"}


def slug_do_app(genericos: list[str]) -> dict[str, str]:
    """
    Pergunta ao TS de produção qual arquivo o app pede para cada genérico, em vez de
    reimplementar a regra aqui.

    Não dá pra usar o slugify() do download_bulas.py: fitoterápico não segue o nome
    genérico, o app resolve por PHYTO_BULA_MAP ("Hypericum perforatum (Erva de São João)"
    → erva-de-sao-joao.pdf, não hypericum-perforatum-…). Reimplementar essa regra aqui
    seria criar uma TERCEIRA cópia do slug pra divergir — que é a causa raiz deste bug todo.
    """
    script = """
      const path = require('path'), fs = require('fs'), ts = require('typescript');
      const { Module } = require('module');
      const p = path.join(process.argv[1], 'src/utils/drugSearch.ts');
      const js = ts.transpileModule(fs.readFileSync(p, 'utf8'),
        { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
      const m = { exports: {} };
      new Function('module','exports','require', js)(m, m.exports, Module.createRequire(p));
      const { medications } = require(path.join(process.argv[1], 'src/data/medications-db.json'));
      const BASE = 'https://www.alertamedico.ia.br/bulas';
      const out = {};
      for (const nome of JSON.parse(process.argv[2])) {
        const e = medications.find(x => x.genericName === nome);
        if (!e) continue;
        const url = (e.category === 'Fitoterápico' ? m.exports.getPhytoBulaUrl
                                                   : m.exports.getBulaUrl)(nome, undefined);
        if (url.startsWith(BASE)) out[nome] = url.slice(BASE.length + 1, -4);
      }
      process.stdout.write(JSON.stringify(out));
    """
    r = subprocess.run(["node", "-e", script, str(ROOT), json.dumps(genericos, ensure_ascii=False)],
                       capture_output=True, cwd=str(ROOT), timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"não consegui obter o slug do app: {r.stderr.decode()[:300]}")
    return json.loads(r.stdout.decode("utf-8"))


def get(url: str, **kw):
    try:
        r = requests.get(url, headers=UA, timeout=30, **kw)
        return r if r.status_code == 200 else None
    except Exception:
        return None


def candidatos_sara(termo: str) -> list[dict]:
    """
    Resultados da busca do Sara, do mais provável de ser o ingrediente PURO ao menos.

    A ordem da API não serve: buscar "Xylestesin" devolve "Xylestesin Pesada" (lidocaína
    + glicose) na frente da "Xylestesin" pura, e pegar results[0] cegamente foi como a
    bula errada entrou. O produto puro é o de nome EXATO; a variante composta sempre
    carrega uma palavra a mais ("Buscopan Composto", "Galvus Met", "Dramin B6").
    """
    r = get(f"https://www.sara.com.br/api/products/search?q={quote(termo)}&limit={MAX_CANDIDATOS}")
    if not r:
        return []
    try:
        dados = r.json().get("data", [])[:MAX_CANDIDATOS]
    except Exception:
        return []

    alvo = termo.strip().lower()
    dados.sort(key=lambda p: (p.get("name", "").strip().lower() != alvo, len(p.get("name", ""))))
    return dados


def baixar_pdf(produto: dict) -> bytes | None:
    slug, ean = produto.get("url"), produto.get("ean")
    if not slug or not ean:
        return None
    detail = get(f"https://www.sara.com.br/produto/{slug}", allow_redirects=True)
    canonical = slug
    if detail is not None:
        final = detail.url.rstrip("/").rsplit("/", 1)[-1]
        if final:
            canonical = final
    pdf = get(f"https://www.sara.com.br/bula-do-paciente-{canonical}/{ean}/pdf")
    if not pdf or "pdf" not in pdf.headers.get("Content-Type", "").lower():
        return None
    return pdf.content


def corrigir(generico: str, termos: list[str], slug: str) -> dict:
    destino = THIS_DIR / f"{slug}.pdf"
    vistos: set[str] = set()

    puro = "+" not in generico

    for termo in termos:
        for prod in candidatos_sara(termo):
            nome = prod.get("name", "?")
            if nome in vistos:
                continue
            vistos.add(nome)

            # Composto se declara no NOME do produto ("Bronfeniramina + Fenilefrina").
            # Filtrar aqui, no dado estruturado do catálogo, pega até ativo que não existe
            # no nosso banco — coisa que a leitura do PDF não pega (o validador só conhece
            # os ativos do medications-db). Procurar "+" no texto do PDF não serve: lá ele
            # também significa "pó + diluente" e reprova bula boa.
            if puro and "+" in nome:
                print(f"      ✗ {nome[:45]:45} nome de produto COMPOSTO")
                continue

            conteudo = baixar_pdf(prod)
            if not conteudo:
                continue

            # valida no temporário: bula reprovada não pode encostar no arquivo publicado
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(conteudo)
                tmp_path = Path(tmp.name)
            try:
                ok, motivo = validar_bula(tmp_path, generico)
                if not ok:
                    print(f"      ✗ {nome[:45]:45} {motivo[:58]}")
                    continue
                if not SECO:
                    destino.write_bytes(conteudo)
                print(f"      ✓ {nome[:45]:45} → {destino.name} ({len(conteudo)//1024} KB)")
                return {"status": "corrigida", "produto": nome, "termo": termo,
                        "arquivo": destino.name}
            finally:
                tmp_path.unlink(missing_ok=True)

    return {"status": "sem_bula_valida"}


def main():
    refazer: dict[str, list[str]] = json.loads(REFAZER.read_text(encoding="utf-8"))
    slugs = slug_do_app(list(refazer))
    print(f"Corrigindo {len(refazer)} bulas via Sara{'  [SECO — não grava]' if SECO else ''}\n")

    resultados: dict[str, dict] = {}
    for i, (generico, termos) in enumerate(refazer.items(), 1):
        slug = slugs.get(generico)
        print(f"[{i}/{len(refazer)}] {generico}" + (f"  → {slug}.pdf" if slug else ""))
        if not slug:
            print("      ! o app não pede bula para este genérico — pulando")
            resultados[generico] = {"status": "sem_slug_no_app"}
            continue
        resultados[generico] = corrigir(generico, termos, slug)
        if resultados[generico]["status"] != "corrigida":
            print(f"      — nenhuma bula válida no Sara")

    corrigidas = [g for g, r in resultados.items() if r["status"] == "corrigida"]
    faltando   = [g for g, r in resultados.items() if r["status"] != "corrigida"]

    print(f"\n{'─' * 70}")
    print(f"CORRIGIDAS: {len(corrigidas)}/{len(refazer)}")
    print(f"SEM BULA VÁLIDA NO SARA: {len(faltando)}")
    for g in faltando:
        print(f"   • {g}")
    (THIS_DIR / "correcao_sara.json").write_text(
        json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDetalhe → site/bulas/correcao_sara.json")


if __name__ == "__main__":
    main()
