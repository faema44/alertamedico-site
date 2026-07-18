"""
Agente de download de bulas ANVISA — MedAlert

Estratégia de busca por medicamento:
  1. Tenta os nomes COMERCIAIS (brands) do medications-db.json
  2. Se nenhum retornar resultado, tenta o nome GENÉRICO
  3. Seleciona a entrada com a Data de Publicação mais recente
  4. Baixa a Bula do Profissional (fallback: Bula do Paciente)
  5. Aguarda 2 minutos antes do próximo medicamento (5s se não encontrou nada)

Saída:
  site/bulas/<slug>.pdf     → PDFs das bulas
  site/bulas/index.json     → { "Metformina": { file, brand_used, date, ... } }
  site/bulas/relatorio.md   → relatório final

Uso:
  python download_bulas.py                              # medicamentos, execução completa
  python download_bulas.py --resumir                     # pula os já baixados (checa o disco)
  python download_bulas.py --sem-delay                   # sem espera de 2 min (testes)
  python download_bulas.py --alvo=fitoterapicos          # só a lista de fitoterápicos
  python download_bulas.py --alvo=xr                     # só as versões XR/Retard/CR
  python download_bulas.py --alvo=novos                  # só os genéricos novos (novos_medicamentos.json)
  python download_bulas.py --alvo=todos --resumir        # medicamentos + fitoterápicos + XR + novos, pulando já baixados
  python download_bulas.py --limite=1 --sem-delay        # roda só 1 item (teste rápido)
"""

import asyncio
import json
import re
import sys
import unicodedata
from datetime import datetime

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

# O navegador só é usado pelo fluxo de download; validar_bula/ativos_esperados são
# importados por _crosscheck.py e baixar_generico_anvisa.py em ambientes sem playwright.
try:
    from playwright.async_api import async_playwright, Page, BrowserContext
except ModuleNotFoundError:
    async_playwright = Page = BrowserContext = None  # type: ignore

# ── Configuração ──────────────────────────────────────────────────────────────

ANVISA_FORM    = "https://consultas.anvisa.gov.br/#/bulario/"

THIS_DIR       = Path(__file__).parent
ROOT           = THIS_DIR.parent.parent
MEDS_SIMPLE    = ROOT / "src" / "data" / "medications.json"
MEDS_DB        = ROOT / "src" / "data" / "medications-db.json"
MEDS_NOVOS     = THIS_DIR / "novos_medicamentos.json"
MEDS_NOVOS_TERMOS = THIS_DIR / "novos_termos.json"
REFAZER_TERMOS = THIS_DIR / "refazer_termos.json"
INDEX_FILE     = THIS_DIR / "index.json"
REPORT_FILE    = THIS_DIR / "relatorio.md"

RESUMIR       = "--resumir"    in sys.argv
SEM_DELAY     = "--sem-delay"  in sys.argv

def _flag_value(name: str, default: str) -> str:
    for arg in sys.argv:
        if arg.startswith(f"{name}="):
            return arg.split("=", 1)[1]
    return default

ALVO    = _flag_value("--alvo", "medicamentos")   # medicamentos | fitoterapicos | xr | novos | corrigir | todos
LIMITE  = int(_flag_value("--limite", "0")) or None  # limita a N itens (0 = sem limite)
DELAY_SECONDS = int(_flag_value("--delay", "120"))   # segundos entre itens (achou algo: sucesso/erro/sem-pdf)
NOT_FOUND_DELAY_SECONDS = int(_flag_value("--delay-nao-encontrado", "5"))  # segundos quando não achou nada (name nem generic)
# Quantos resultados da ANVISA tentar antes de desistir. Só faz sentido ser >1 porque agora
# validamos o conteúdo do PDF: se a 1ª for a bula de um composto, cai pra próxima.
MAX_TENTATIVAS = int(_flag_value("--max-tentativas", "6"))

# Fitoterápicos usados no banco de interações (src/data/interactions.json) que
# ainda não têm bula baixada. Nome de busca = termo em português mais provável
# de aparecer no cadastro ANVISA; termos extras funcionam como sinônimos de busca.
PHYTO_ITEMS: list[dict] = [
    {"name": "Ginkgo Biloba",     "terms": ["Ginkgo"]},
    {"name": "Erva de São João",  "terms": ["Hiperico", "Hypericum"]},
    {"name": "Valeriana",         "terms": ["Valeriana Officinalis"]},
    {"name": "Alho",              "terms": ["Allium Sativum", "Alho Medicinal"]},
    {"name": "Camomila",          "terms": ["Matricaria Recutita"]},
    {"name": "Boldo",             "terms": ["Peumus Boldus", "Boldo do Chile"]},
    {"name": "Cha Verde",         "terms": ["Camellia Sinensis", "Chá Verde"]},
    {"name": "Curcuma",           "terms": ["Curcuma Longa", "Açafrão da Terra"]},
    {"name": "Alcachofra",        "terms": ["Cynara Scolymus"]},
    {"name": "Melissa",           "terms": ["Melissa Officinalis", "Erva Cidreira"]},
    {"name": "Hortela Pimenta",   "terms": ["Mentha Piperita"]},
    {"name": "Unha de Gato",      "terms": ["Uncaria Tomentosa"]},
    {"name": "Maracuja",          "terms": ["Passiflora Incarnata", "Passiflora"]},
]

# Versões de liberação prolongada (XR/Retard/CR/MR) de medicamentos já presentes
# na lista principal, cuja bula (posologia, farmacocinética) é distinta da
# versão de liberação imediata. Só inclui os que já estão em medications.json.
XR_ITEMS: list[dict] = [
    {"name": "Metformina XR",           "terms": ["Glifage XR", "Formet XR", "Glicep XR"]},
    {"name": "Gliclazida MR",           "terms": ["Clazi XR", "Gliclazida MR"]},
    {"name": "Nifedipina Retard",       "terms": ["Nifedipina Retard", "Oxcord Retard", "Adalex Retard", "Cardalin Retard"]},
    {"name": "Diltiazem Retard",        "terms": ["Balcor Retard", "Diltiazem Retard"]},
    {"name": "Metoprolol Succinato XR", "terms": ["Emprol XR", "Inephoros XR", "Metoprolol Succinato"]},
    {"name": "Venlafaxina XR",          "terms": ["Alenthus XR", "Venlafaxina XR"]},
    {"name": "Diclofenaco Retard",      "terms": ["Inflaren Retard", "Desinflex Retard", "Diclac SR"]},
    {"name": "Cetoprofeno Retard",      "terms": ["Cetofen Retard"]},
    {"name": "Carbamazepina CR",        "terms": ["Tegretard", "Carbamazepina CR"]},
    {"name": "Quetiapina XR",           "terms": ["Quet XR", "Atip XR"]},
    {"name": "Tramadol Retard",         "terms": ["Timasen SR", "Tramadol Retard"]},
]

# ── Utilitários ───────────────────────────────────────────────────────────────

def _slug_part(name: str) -> str:
    name = name.split("/")[0].strip()
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = re.sub(r"[^\w\s-]", "", name.lower())
    name = re.sub(r"[\s_]+", "-", name)
    return re.sub(r"-+", "-", name).strip("-")


def slugify(name: str) -> str:
    """
    ESPELHO de toSlug/toComboSlug em src/utils/drugSearch.ts — os dois TÊM que gerar
    o mesmo slug, senão o app pede um arquivo que o downloader nunca gravou.

    Medicamento composto ganha slug PRÓPRIO (ingredientes ordenados e unidos por "-").
    Antes isto era `re.split(r"[+/]", name)[0]`, que jogava a bula do composto em cima da
    do primeiro princípio ativo: era assim que "Bupropiona + Naltrexona" (Contrave)
    sobrescrevia bupropiona.pdf, e a Bupropiona pura passava a abrir a bula do Contrave.
    """
    if "+" in name:
        parts = [p for p in (_slug_part(p) for p in name.split("+")) if p]
        return "-".join(sorted(parts))
    return _slug_part(name)


def parse_date(text: str) -> datetime | None:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None


def load_brands_map() -> dict[str, list[str]]:
    """Retorna { genericName: [brand1, brand2, ...] } do medications-db.json."""
    data = json.loads(MEDS_DB.read_text(encoding="utf-8"))
    return {m["genericName"]: m.get("brands", []) for m in data["medications"]}


# ── Validação de conteúdo da bula baixada ─────────────────────────────────────
# A busca da ANVISA é por texto: pesquisar "bupropiona" traz o CONTRAVE (naltrexona +
# bupropiona) junto, e pegar "a mais recente" escolhia ele. O nome do arquivo saía certo
# e o conteúdo errado — nenhuma checagem de slug pega isso. Só lendo o PDF.

# Excipientes: estão no banco como suplemento (estearato de magnésio, manitol…), mas
# aparecem na composição de qualquer comprimido — nunca são "princípio ativo intruso".
EXCIPIENTES = {
    "magnesio", "carmelose", "manitol", "carbonato de calcio", "cloreto de sodio",
    "acido citrico", "acido ascorbico", "simeticona", "dioxido de titanio",
    "bicarbonato de sodio", "sacarose", "lactose", "povidona", "glicose",
}


# Palavras de sal/ligação que aparecem no nome mas NÃO identificam o princípio ativo.
# Sem isto, "Ferro Sulfato" casava com "Sulfato de glicosamina" pela palavra "sulfato".
SAIS = {
    "cloridrato", "dicloridrato", "sulfato", "acetato", "fosfato", "citrato",
    "succinato", "maleato", "mesilato", "besilato", "bromidrato", "tartarato",
    "fumarato", "nitrato", "gluconato", "carbonato", "pidolato", "oxalato",
    "sodico", "sodica", "potassico", "calcico", "calcica", "monoidratado",
    "monoidratada", "hidratado", "hidratada", "acido", "complexo",
}

# Palavras que distinguem PRODUTOS DIFERENTES do mesmo fármaco: o sal e a forma mudam
# via, liberação e dose (benzilpenicilina benzatina é IM de depósito, a potássica é EV).
# Se o genérico pede uma e a bula é de outra, é bula errada — não sinônimo.
QUALIFICADORES = [
    "benzatina", "procaina", "potassica", "cristalina",
    "nph", "regular", "glargina", "lispro", "aspart", "detemir", "degludeca",
    "succinato", "tartarato",
]

# Sinônimos de SUBSTÂNCIA (nunca de marca): a bula certa escreve o mesmo fármaco com
# outra grafia ou DCB. Marca (Invokana↔canagliflozina) NÃO entra: aceitar marca como
# sinônimo esconderia o composto-em-slug-puro que a checagem de intruso pega.
# Espelha SINONIMOS em tools/audit-bulas.js — divergir = gate aprova o que a auditoria
# reprova (conferido por _crosscheck.py). Chaves e valores em forma normalizada (_norm).
SINONIMOS = {
    "metimazol":       ["tiamazol"],              # DCB brasileira do mesmo fármaco
    "amisulprida":     ["amissulprida"],          # grafia com dois esses
    "dimetilfumarato": ["fumarato de dimetila"],  # ordem invertida
    "remdesivir":      ["rendesivir"],            # adaptação PT
    "canacinumabe":    ["canaquinumabe"],
    "alemtuzumabe":    ["alentuzumabe"],
    "alglucosidase":   ["alglicosidase"],         # biológico: alfa-alglicosidase
    "eritropoietina":  ["alfaepoetina", "epoetina"],
    "vitamina d":      ["colecalciferol"],
    "vitamina d3":     ["colecalciferol"],
    "vitamina k":      ["fitomenadiona"],
}


def _expandir(lista: list[str]) -> list[str]:
    return [x for item in lista for x in ([item] + SINONIMOS.get(item, []))]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def _raiz(n: str) -> str:
    return re.sub(r"[aeo]$", "", n)          # casa "bupropiona" com "cloridrato de bupropiona"


def load_vocab_ativos() -> list[str]:
    """Princípios ativos conhecidos (nomes longos), pra detectar um ativo ESTRANHO na bula."""
    data = json.loads(MEDS_DB.read_text(encoding="utf-8"))
    vocab: set[str] = set()
    for m in data["medications"]:
        for part in m["genericName"].split("+"):
            n = _norm(part.split("(")[0].split("/")[0])
            if len(n) >= 7 and not any(e in n or n in e for e in EXCIPIENTES):
                vocab.add(n)
    return sorted(vocab)


VOCAB_ATIVOS = load_vocab_ativos()


def pdf_texto(pdf: Path, ate_pagina: int = 2) -> str:
    import subprocess
    try:
        # -enc UTF-8 não é opcional: sem ele os acentos viram lixo e a checagem falha à toa
        out = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-f", "1", "-l", str(ate_pagina), str(pdf), "-"],
            capture_output=True, timeout=60,
        )
        return out.stdout.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"      [pdftotext] falhou: {e}")
        return ""


def ativos_esperados(generic_name: str) -> list[str]:
    """
    Princípios ativos que a bula TEM que citar.

    Nem todo composto no banco usa "+": "Sulfametoxazol-trimetoprima" une os dois ativos
    por hífen. Sem desmembrar, procuraríamos a string literal — que não existe em bula
    nenhuma — e rejeitaríamos a bula CERTA do Bactrim. Mas hífen nem sempre separa ativo
    ("Interferon beta-1a"), então só desmembra quando dá 2+ pedaços com cara de nome de
    fármaco (>= 6 letras).
    """
    partes = [_norm(p.split("(")[0].split("/")[0]) for p in generic_name.split("+")]
    partes = [p for p in partes if p]

    expandido: list[str] = []
    for p in partes:
        sub = [s for s in (x.strip() for x in p.split("-")) if len(s) >= 6]
        expandido.extend(sub if len(sub) >= 2 else [p])
    return expandido


def validar_bula(pdf: Path, generic_name: str) -> tuple[bool, str]:
    """
    Confere se o PDF baixado é MESMO a bula deste medicamento.
    Rejeita: (a) bula que não cita o princípio ativo; (b) bula de medicamento COMPOSTO
    quando o esperado é o ingrediente puro.
    """
    # 6 páginas, não 2: em bula de marca a capa às vezes traz só o nome comercial
    # (o Combodart só cita "dutasterida" lá pra frente) e daria falso "não cita".
    bruto = pdf_texto(pdf, ate_pagina=6)
    if len(re.sub(r"[^a-z]", "", _norm(bruto))) < 40:
        return False, "PDF sem texto extraível (digitalização?)"

    texto = _norm(bruto)
    esperados = ativos_esperados(generic_name)

    # Aceita também UMA palavra do nome, porque biológico no Brasil inverte e cola o nome
    # ("Interferon beta-1a" → "betainterferona 1a", "Epoetina alfa" → "alfaepoetina") e a
    # ordem das palavras varia ("Ferro Sulfato" → "sulfato ferroso").
    # Palavra de SAL não identifica fármaco — "sulfato" sozinho aceitava "sulfato de
    # glicosamina" como se fosse sulfato ferroso. Só conta palavra que nomeia o princípio ativo.
    tokens = [t for t in re.split(r"[\s\-+]", _norm(generic_name))
              if len(t) >= 5 and t not in SAIS]

    # O sufixo do nome varia ("Zoledronato" vs "ácido zoledrônico"), então em nome longo
    # corta as 4 letras finais. Cortar MAIS que isso abriria a porta pro bug original:
    # com prefixo curto, "eritrom…" casaria com ERITROMAX (que é alfaepoetina, não
    # eritromicina). "eritromi" não casa com "eritromax" — o corte tem que ser conservador.
    def _em(t: str, alvo: str) -> bool:
        return t in alvo or (len(t) >= 9 and t[:-4] in alvo)

    esperados_exp = _expandir(esperados)
    tokens_exp = _expandir(tokens)
    if not any(_raiz(e) in texto for e in esperados_exp) and not any(_em(t, texto) for t in tokens_exp):
        cabeca = " ".join(l for l in bruto.splitlines() if l.strip())[:80]
        return False, f"não cita '{generic_name}' — a bula é de outro medicamento ({cabeca!r})"

    # "Identificação do medicamento": as 3 primeiras linhas, onde aparece
    # "MARCA® (ativo1 + ativo2) / laboratório / forma / concentração". Tem que ser o MESMO
    # recorte de tools/audit-bulas.js — recorte diferente = veredito diferente, e aí o gate
    # de publicação aprova o que a auditoria reprova (conferido por _crosscheck.py).
    ident = _norm(" ".join([l for l in bruto.splitlines() if l.strip()][:3]))
    if len(esperados) == 1:
        # O mesmo fármaco entra no banco com dois nomes ("Epoetina alfa" e "Alfaepoetina",
        # "Zoledronato" e "Ácido Zoledrônico"), e sem isto um acusa o outro de intruso e a
        # bula CERTA seria apagada. Compartilhar o radical do nome ⇒ é o mesmo fármaco.
        intrusos = [
            v for v in VOCAB_ATIVOS
            if _raiz(v) in ident
            and not any(_raiz(e) in v or _raiz(v) in e for e in esperados_exp)
            and not any(_em(t, v) for t in tokens_exp)
        ]
        if intrusos:
            return False, f"bula de medicamento COMPOSTO (traz também: {', '.join(intrusos)})"

    # NÃO tentar deduzir "é composto" de um "+" no texto do PDF: o "+" também aparece em
    # apresentação ("pó liofilizado + diluente"), embalagem e lista de doses, e a regra
    # reprovava ~30 bulas CORRETAS (alprazolam, carbamazepina, ceftazidima…) pra pegar uma
    # errada. Composto com ativo fora do nosso banco é filtrado na origem, pelo NOME do
    # produto no catálogo (dado estruturado), não pelo texto — ver corrigir_bulas_sara.py.

    # Sal/forma erradas são medicamentos DIFERENTES: benzilpenicilina benzatina não é
    # benzilpenicilina potássica (liberação e via mudam), insulina NPH não é regular.
    # Como "penicilina"/"insulina" casam entre si, sem isto a bula do sal errado é aceita.
    for q in QUALIFICADORES:
        if q in " ".join(esperados) and q not in texto:
            return False, f"a bula não é da forma '{q}' — sal/forma diferente do esperado"

    return True, "ok"


def load_index() -> dict:
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_index(index: dict) -> None:
    INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

# ── ANVISA: busca e resultados ────────────────────────────────────────────────

async def search_anvisa(page: Page, term: str) -> list:
    """Pesquisa um termo e retorna as linhas de dados (>= 8 células). Muda para 50/pág."""
    # O bulário é um Angular lento (leva ~12s até o formulário existir) e instável: sob
    # rajada de buscas ele simplesmente não monta a página. Tenta de novo com pausa antes
    # de desistir — uma falha aqui vira "sem resultado" e a bula ficaria sem correção.
    for tentativa in range(3):
        try:
            await page.goto(ANVISA_FORM, timeout=60000)
            await page.wait_for_selector("input.form-control", timeout=45000)
            break
        except Exception:
            if tentativa == 2:
                raise
            print(f"    … bulário não carregou, tentando de novo ({tentativa + 2}/3)")
            await page.wait_for_timeout(20000)
    await page.wait_for_timeout(500)

    inputs = await page.query_selector_all("input.form-control")
    await inputs[0].click()
    await inputs[0].fill(term)
    await page.wait_for_timeout(300)
    await page.click("input[type='submit']")

    # O bulário demora pra responder e às vezes devolve a tabela vazia ("Nenhum registro
    # encontrado") só porque ainda não terminou — esperar a tabela APARECER não basta,
    # tem que esperar aparecer uma LINHA DE DADOS. Sem isto, busca boa ("Paracetamol")
    # voltava como "sem resultado" de forma intermitente.
    try:
        await page.wait_for_selector("table tbody tr td", timeout=30000)
    except Exception:
        return []

    await page.wait_for_timeout(1200)

    # Aumenta para 50 resultados por página
    btn_50 = await page.query_selector("button:has-text('50'), a:has-text('50')")
    if btn_50:
        await btn_50.click()
        await page.wait_for_timeout(800)

    rows = await page.query_selector_all("table tbody tr")
    return [r for r in rows if len(await r.query_selector_all("td")) >= 8]


async def collect_all_rows_metadata(page: Page, terms: list[str]) -> list[dict]:
    """
    Percorre TODOS os termos, coleta metadados de cada linha.
    Deduplica por expediente. Retorna lista de dicts com
    {expediente, name, date, term} ordenados do mais recente ao mais antigo.
    """
    seen: dict[str, dict] = {}  # expediente → entry

    for term in terms:
        print(f"    busca: '{term}'")
        rows = await search_anvisa(page, term)
        if not rows:
            print(f"    ✗ sem resultado")
            continue
        print(f"    ✓ {len(rows)} resultado(s)")
        for row in rows:
            cells = await row.query_selector_all("td")
            expediente = (await cells[3].inner_text()).strip()
            date       = parse_date((await cells[4].inner_text()).strip())
            name       = (await cells[1].inner_text()).strip().replace("\n", " ")
            if expediente not in seen or (date and (seen[expediente]["date"] is None or date > seen[expediente]["date"])):
                seen[expediente] = {"expediente": expediente, "name": name, "date": date, "term": term}

    entries = list(seen.values())
    entries.sort(key=lambda e: e["date"] or datetime.min, reverse=True)
    return entries


async def find_row_by_expediente(page: Page, term: str, expediente: str):
    """Re-pesquisa o termo e localiza a linha pelo número de expediente."""
    rows = await search_anvisa(page, term)
    for row in rows:
        cells = await row.query_selector_all("td")
        if (await cells[3].inner_text()).strip() == expediente:
            return row
    return None

# ── Download de PDF ───────────────────────────────────────────────────────────

async def download_pdf_from_row(page: Page, row, dest: Path) -> str | None:
    """
    Clica no ícone de PDF (Profissional primeiro, Paciente como fallback).
    Retorna 'profissional', 'paciente' ou None.
    """
    cells = await row.query_selector_all("td")

    for col_idx, label in ((5, "paciente"), (6, "profissional")):
        if len(cells) <= col_idx:
            continue
        link = await cells[col_idx].query_selector("a")
        if not link:
            continue
        try:
            async with page.expect_download(timeout=20000) as dl_info:
                await link.click()
            dl = await dl_info.value
            await dl.save_as(str(dest))
            return label
        except Exception as e:
            print(f"      [{label}] erro no download: {e}")

    return None

# ── Processamento por medicamento ─────────────────────────────────────────────

async def process_med(
    page: Page,
    generic_name: str,
    brands: list[str],
    output_dir: Path,
) -> dict:
    slug = slugify(generic_name)
    dest = output_dir / f"{slug}.pdf"

    # Termos: comerciais primeiro, genérico por último, sem duplicatas
    raw_terms = [b for b in brands if b] + [generic_name]
    seen_t: set = set()
    search_terms = [t for t in raw_terms if not (t.lower() in seen_t or seen_t.add(t.lower()))]

    # Pesquisa TODOS os termos e coleta o mais recente globalmente
    entries = await collect_all_rows_metadata(page, search_terms)
    if not entries:
        return {"name": generic_name, "status": "not_found", "file": None}

    # Tenta da mais recente pra mais antiga e VALIDA o conteúdo de cada uma: "a mais
    # recente" sozinha era o que trazia a bula do composto (Contrave para "bupropiona").
    rejeitadas: list[str] = []
    for tentativa, best in enumerate(entries[:MAX_TENTATIVAS], 1):
        date_str = best["date"].strftime("%d/%m/%Y") if best["date"] else "?"
        print(f"    [{tentativa}/{min(len(entries), MAX_TENTATIVAS)}] {best['name']} ({date_str}) via '{best['term']}'")

        # Renavega para a pesquisa que retornou essa linha e a localiza pelo expediente
        row = await find_row_by_expediente(page, best["term"], best["expediente"])
        if not row:
            continue

        bula_type = await download_pdf_from_row(page, row, dest)
        if not bula_type:
            continue

        ok, motivo = validar_bula(dest, generic_name)
        if not ok:
            print(f"      ✗ rejeitada: {motivo}")
            rejeitadas.append(f"{best['name']}: {motivo}")
            dest.unlink(missing_ok=True)   # não deixa bula errada no disco
            continue

        size_kb = dest.stat().st_size // 1024
        print(f"    ✓ {dest.name} ({size_kb} KB) — bula do {bula_type}")

        return {
            "name":        generic_name,
            "status":      "success",
            "file":        f"{slug}.pdf",
            "date":        date_str,
            "full_name":   best["name"],
            "brand_used":  best["term"],
            "bula_type":   bula_type,
            "rejeitadas":  rejeitadas,
        }

    # Nenhum resultado passou na validação. Melhor NÃO ter bula do que ter a errada:
    # o app cai no link quebrado em vez de mostrar a bula de outro medicamento.
    print(f"    ✗ nenhuma bula válida em {len(entries[:MAX_TENTATIVAS])} tentativa(s)")
    return {"name": generic_name, "status": "no_valid_bula", "file": None, "rejeitadas": rejeitadas}

# ── Relatório ─────────────────────────────────────────────────────────────────

def build_report(medications, index, not_found, errors) -> str:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [
        "# Relatório de Download de Bulas ANVISA",
        "",
        f"**Data:** {now}",
        f"**Total:** {len(medications)} medicamentos",
        f"**Baixados:** {len(index)}",
        f"**Não encontrados:** {len(not_found)}",
        f"**Erros:** {len(errors)}",
        "",
        f"## Bulas Baixadas ({len(index)})",
        "",
    ]
    for name, info in sorted(index.items()):
        lines.append(
            f"- **{name}** → `{info['file']}` "
            f"| busca: _{info.get('brand_used','?')}_ "
            f"| pub: {info.get('date','?')}"
        )
    lines += ["", f"## Não Encontrados na ANVISA ({len(not_found)})", ""]
    lines += [f"- {n}" for n in sorted(not_found)] or ["_Nenhum_"]
    if errors:
        lines += ["", f"## Erros ({len(errors)})", ""]
        lines += [f"- **{e['name']}**: {e['reason']}" for e in errors]
    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    brands_map   = load_brands_map()
    index        = load_index()
    not_found: list[str]  = []
    errors: list[dict]    = []

    # Monta a lista de itens (nome, termos de busca) conforme --alvo
    items: list[tuple[str, list[str]]] = []
    if ALVO in ("medicamentos", "todos"):
        medications: list[str] = json.loads(MEDS_SIMPLE.read_text(encoding="utf-8"))
        items += [(m, brands_map.get(m, [])) for m in medications]
    if ALVO in ("fitoterapicos", "todos"):
        items += [(p["name"], p["terms"]) for p in PHYTO_ITEMS]
    if ALVO in ("xr", "todos"):
        items += [(x["name"], x["terms"]) for x in XR_ITEMS]
    if ALVO in ("novos", "todos"):
        novos: list[str] = json.loads(MEDS_NOVOS.read_text(encoding="utf-8"))
        novos_termos: dict[str, list[str]] = {}
        if MEDS_NOVOS_TERMOS.exists():
            novos_termos = json.loads(MEDS_NOVOS_TERMOS.read_text(encoding="utf-8"))
        items += [(m, novos_termos.get(m, brands_map.get(m, []))) for m in novos]
    if ALVO == "pendentes":
        # As que o Sara não tem (Boehringer, Sanofi…) e só existem no bulário da ANVISA.
        # Só medicamento comum — fitoterápico NÃO pode entrar aqui, porque o app resolve o
        # arquivo dele por PHYTO_BULA_MAP e o slugify() daqui gravaria com o nome errado.
        pendentes: dict[str, list[str]] = json.loads(
            (THIS_DIR / "refazer_pendentes.json").read_text(encoding="utf-8"))
        items += list(pendentes.items())
    if ALVO == "corrigir":
        # Bulas com conteúdo ERRADO no ar (auditoria: tools/audit-bulas.js). Aqui os termos
        # de busca são FIXADOS à mão numa marca do ingrediente PURO — buscar pela marca do
        # banco não serve, porque brands[] está contaminado com marca de composto
        # (Amoxicilina lista "Clavulanax", Paracetamol lista "Dorilax DF"…), que é
        # justamente como a bula errada entrou.
        refazer: dict[str, list[str]] = json.loads(REFAZER_TERMOS.read_text(encoding="utf-8"))
        items += list(refazer.items())

    if LIMITE:
        items = items[:LIMITE]

    print(f"MedAlert — Downloader de Bulas ANVISA")
    print(f"Alvo: {ALVO} | itens: {len(items)} | resumir={RESUMIR} | delay={0 if SEM_DELAY else DELAY_SECONDS}s\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        for i, (med, brands) in enumerate(items):
            print(f"\n{'─'*60}")
            print(f"[{i+1}/{len(items)}] {med}")

            dest_by_slug = THIS_DIR / f"{slugify(med)}.pdf"
            if RESUMIR and dest_by_slug.exists():
                print(f"  ↷ já existe ({dest_by_slug.name}), pulando")
                index.setdefault(med, {
                    "name": med, "status": "success", "file": dest_by_slug.name,
                })
                continue

            try:
                result = await process_med(page, med, brands, THIS_DIR)
            except Exception as exc:
                print(f"  ✗ exceção: {exc}")
                result = {"name": med, "status": "error", "file": None}

            status = result.get("status")
            if status == "success":
                index[med] = result
            elif status == "not_found":
                not_found.append(med)
            else:
                errors.append({"name": med, "reason": status or "unknown"})

            save_index(index)

            if i < len(items) - 1 and not SEM_DELAY:
                wait = NOT_FOUND_DELAY_SECONDS if status == "not_found" else DELAY_SECONDS
                remaining = len(items) - i - 1
                print(f"\n  ⏳ aguardando {wait}s… (restam {remaining})")
                await asyncio.sleep(wait)

        await browser.close()

    REPORT_FILE.write_text(build_report([m for m, _ in items], index, not_found, errors), encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"CONCLUÍDO: {len(index)} baixados | {len(not_found)} não encontrados | {len(errors)} erros")
    print(f"Relatório: {REPORT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
