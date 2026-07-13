"""
Troca as bulas de MARCA pelas do GENÉRICO, onde o genérico existe.

O PROBLEMA (observação do Fabio, e é decisiva)
----------------------------------------------
O app indexa a bula pelo PRINCÍPIO ATIVO, mas 64% do acervo é bula de MARCA. Quem digita
"acarbose" recebe AGLUCOSE® da EMS; quem digita "abacavir" recebe Ziagenavir® da GSK.

    "Eu tomo maleato de enalapril. Se abrir a bula e aparecer Renitec ou Vasopril, tenho que
     verificar se o sal é o mesmo. Na farmácia podem ter me vendido o Renitec, mas eu vou
     cadastrar maleato de enalapril — e pode aparecer Vasopril."

O app INTRODUZ uma marca que o usuário não tem. Isso é pior que não mostrar nada: cria dúvida
onde não havia. A bula do GENÉRICO é a referência neutra — é o que quem digita o princípio
ativo espera ver.

POR QUE O SARA
--------------
É o catálogo dos fabricantes de genérico, e devolve o produto pelo NOME DO ATIVO quando ele
existe:

    "enalapril"  →  "Maleato de enalapril"     (genérico)   ✓
    "losartana"  →  "Losartana potássica"      (genérico)   ✓
    "acarbose"   →  "Aglucose"                 (só a marca) ✗

E é HTTP simples — a ANVISA só responde de madrugada no fim de semana.

TRÊS TRAVAS, e cada uma existe por um erro já cometido
-------------------------------------------------------
1. SÓ ACEITA PRODUTO DE NOME GENÉRICO. Se o Sara só tem a marca (Aglucose), NÃO troca: trocar
   marca por marca não resolve nada. Sem esta trava o script "consertaria" a acarbose
   substituindo Aglucose por Aglucose.
2. VALIDA O CONTEÚDO (validar_bula, com detecção de INTRUSO). Não basta a bula citar
   "bupropiona": ela não pode declarar naltrexona junto — foi assim que a bula do Contrave
   ocupou o slug da bupropiona.
3. CONFIRMA QUE A NOVA BULA É GENÉRICA ("Medicamento genérico, Lei nº 9.787"). O Sara é
   catálogo da EMS, que também vende marca própria. Sem esta trava, trocaríamos uma marca por
   outra e o relatório diria que deu certo.

USO:
  python trocar_por_generico.py --seco          (não grava)
  python trocar_por_generico.py --limite=20     (testa num punhado)
  python trocar_por_generico.py
"""
import hashlib
import json
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

from download_bulas import validar_bula
from corrigir_bulas_sara import candidatos_sara, baixar_pdf, slug_do_app

THIS = Path(__file__).parent
SECO = "--seco" in sys.argv
LIMITE = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--limite=")), None)
ALVOS = THIS / "trocar_por_generico.json"

GENERICO_RE = re.compile(r"medicamento\s+gen[eé]rico|lei\s*n?[º°.]?\s*9[.\s]?787", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


SAIS = {"maleato", "cloridrato", "sulfato", "potassica", "sodica", "besilato", "mesilato",
        "acetato", "fumarato", "succinato", "tartarato", "bromidrato", "dicloridrato",
        "citrato"}


def identidade(nome: str) -> set[str]:
    """Palavras que IDENTIFICAM o farmaco: fora o sal, o ester e as ligacoes."""
    return {t for t in re.split(r"[\s\-+]+", norm(nome)) if len(t) >= 5 and t not in SAIS}


def eh_nome_generico(nome_produto: str, generico: str) -> bool:
    """
    O produto do Sara e o GENERICO DESTE farmaco - nem marca, nem OUTRO farmaco parecido?

    Exige IGUALDADE do conjunto de identidade, nao continencia. A regra frouxa ("o nome do
    produto contem o ativo") aceitou "Mononitrato de isossorbida" como generico de
    "Isossorbida" - e sobrescreveu a bula do ISORDIL (DINITRATO) pela do MONONITRATO, que e
    outro farmaco, com outra farmacocinetica. A palavra A MAIS e justamente o que distingue.

    Sal e ester nao contam ("Maleato de enalapril" == "Enalapril"; "Losartana potassica" ==
    "Losartana"): ali a palavra a mais e o contra-ion, nao outro farmaco.
    """
    alvo = identidade(generico)
    return bool(alvo) and identidade(nome_produto) == alvo


def eh_bula_generica(pdf: Path) -> bool:
    from subprocess import run
    r = run(["pdftotext", "-enc", "UTF-8", "-f", "1", "-l", "2", str(pdf), "-"],
            capture_output=True, timeout=60)
    return bool(GENERICO_RE.search(r.stdout.decode("utf-8", "ignore")[:2500]))


# sha256 de tudo o que ja esta publicado - para a TRAVA 4
hashes: dict[str, str] = {
    p.stem: hashlib.sha256(p.read_bytes()).hexdigest() for p in THIS.glob("*.pdf")
}

alvos: dict[str, list[str]] = json.loads(ALVOS.read_text(encoding="utf-8"))
itens = list(alvos.items())[:LIMITE] if LIMITE else list(alvos.items())
slugs = slug_do_app([g for g, _ in itens])

print(f"{len(itens)} bulas de MARCA para trocar pelo genérico"
      f"{'  [SECO — não grava]' if SECO else ''}\n")

trocadas, so_marca, sem_generico, reprovadas = [], [], [], []

for i, (generico, termos) in enumerate(itens, 1):
    slug = slugs.get(generico)
    if not slug:
        continue
    print(f"[{i}/{len(itens)}] {generico}  → {slug}.pdf", flush=True)

    achou = False
    for termo in termos:
        for prod in candidatos_sara(termo):
            nome = prod.get("name", "?")
            # TRAVA 1 — só produto de nome genérico. Marca não resolve o problema.
            if not eh_nome_generico(nome, generico):
                continue
            conteudo = baixar_pdf(prod)
            if not conteudo:
                continue

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(conteudo)
                tmp_path = Path(tmp.name)
            try:
                # TRAVA 2 — o conteúdo é mesmo deste fármaco, e sem intruso
                ok, motivo = validar_bula(tmp_path, generico)
                if not ok:
                    print(f"    ✗ {nome}: {motivo[:60]}")
                    reprovadas.append((generico, nome, motivo))
                    continue
                # TRAVA 3 — e é bula de GENÉRICO, não outra marca
                if not eh_bula_generica(tmp_path):
                    print(f"    ✗ {nome}: a bula não se declara genérica — seria trocar marca por marca")
                    so_marca.append((generico, nome))
                    continue
                # TRAVA 4 — não pode virar CÓPIA da bula de OUTRO slug.
                # A busca por "Sildenafila" devolveu o genérico de 20 mg — que é a dose da
                # HIPERTENSÃO PULMONAR, não a do Viagra (25/50/100 mg). O PDF ficou idêntico ao
                # de sildenafila-hp.pdf, e quem toma Viagra passaria a ler a bula do Revatio.
                # A validação de CONTEÚDO não pega isso: o princípio ativo está certo, o texto
                # cita sildenafila, não há intruso. Quem pega é o HASH — se dois slugs
                # diferentes ficam com o mesmo arquivo, um dos dois está mentindo.
                h = hashlib.sha256(conteudo).hexdigest()
                colisao = next((sl for sl, hh in hashes.items() if hh == h and sl != slug), None)
                if colisao:
                    print(f"    ✗ {nome}: ficaria IDÊNTICA a {colisao}.pdf — dose/indicação diferente")
                    reprovadas.append((generico, nome, f"colidiria com {colisao}"))
                    continue
                if not SECO:
                    (THIS / f"{slug}.pdf").write_bytes(conteudo)
                print(f"    ✓ {nome}  ({len(conteudo) // 1024} KB)")
                trocadas.append((generico, nome))
                achou = True
            finally:
                tmp_path.unlink(missing_ok=True)
            if achou:
                break
        if achou:
            break

    if not achou and generico not in [g for g, _ in so_marca] and generico not in [g for g, _, _ in reprovadas]:
        print("    — o Sara não tem genérico deste fármaco (mantém a marca)")
        sem_generico.append(generico)

print(f"\n{'─' * 70}")
print(f"TROCADAS pelo genérico:        {len(trocadas)}")
print(f"só existe MARCA no Sara:       {len(so_marca)}")
print(f"Sara não tem o fármaco:        {len(sem_generico)}")
print(f"reprovadas na validação:       {len(reprovadas)}")
