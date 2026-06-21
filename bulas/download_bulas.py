"""
Agente de download de bulas ANVISA — MedAlert

Estratégia de busca por medicamento:
  1. Tenta os nomes COMERCIAIS (brands) do medications-db.json
  2. Se nenhum retornar resultado, tenta o nome GENÉRICO
  3. Seleciona a entrada com a Data de Publicação mais recente
  4. Baixa a Bula do Profissional (fallback: Bula do Paciente)
  5. Registra monitoramento com evidencemedai@gmail.com (Semanalmente)
  6. Aguarda 2 minutos antes do próximo medicamento

Saída:
  site/bulas/<slug>.pdf     → PDFs das bulas
  site/bulas/index.json     → { "Metformina": { file, brand_used, date, ... } }
  site/bulas/relatorio.md   → relatório final

Uso:
  python download_bulas.py              # execução completa
  python download_bulas.py --resumir    # pula os já baixados
  python download_bulas.py --sem-delay  # sem espera de 2 min (testes)
"""

import asyncio
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

# ── Configuração ──────────────────────────────────────────────────────────────

MONITOR_EMAIL  = "evidencemedai@gmail.com"
DELAY_SECONDS  = 120
ANVISA_FORM    = "https://consultas.anvisa.gov.br/#/bulario/"

THIS_DIR       = Path(__file__).parent
ROOT           = THIS_DIR.parent.parent
MEDS_SIMPLE    = ROOT / "src" / "data" / "medications.json"
MEDS_DB        = ROOT / "src" / "data" / "medications-db.json"
INDEX_FILE     = THIS_DIR / "index.json"
REPORT_FILE    = THIS_DIR / "relatorio.md"

RESUMIR       = "--resumir"    in sys.argv
SEM_DELAY     = "--sem-delay"  in sys.argv

# ── Utilitários ───────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    name = re.split(r"[+/]", name)[0].strip()
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = re.sub(r"[^\w\s-]", "", name.lower())
    name = re.sub(r"[\s_]+", "-", name)
    return re.sub(r"-+", "-", name).strip("-")


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
    await page.goto(ANVISA_FORM, timeout=20000)
    await page.wait_for_selector("input.form-control", timeout=10000)
    await page.wait_for_timeout(500)

    inputs = await page.query_selector_all("input.form-control")
    await inputs[0].click()
    await inputs[0].fill(term)
    await page.wait_for_timeout(300)
    await page.click("input[type='submit']")

    try:
        await page.wait_for_selector("table tbody tr", timeout=12000)
    except Exception:
        return []

    await page.wait_for_timeout(600)

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

# ── Monitoramento ─────────────────────────────────────────────────────────────

async def register_monitor(page: Page, row) -> bool:
    """
    Fluxo correto:
      1. Marca o checkbox da linha
      2. Clica no link <a modal-anvisa="modalMonitoramento"> (canto inferior esquerdo)
      3. Preenche e-mail e seleciona Mensalmente
      4. Clica Confirmar
    Deve ser chamado ANTES de baixar o PDF.
    """
    try:
        cells = await row.query_selector_all("td")
        checkbox = await cells[0].query_selector("input[type='checkbox']")
        if not checkbox:
            print("      checkbox não encontrado")
            return False
        if not await checkbox.is_checked():
            await checkbox.check()
        await page.wait_for_timeout(400)

        # Botão Monitorar é um <a> com atributo modal-anvisa
        monitor_link = await page.query_selector("a[modal-anvisa='modalMonitoramento']")
        if not monitor_link:
            # Fallback: qualquer link/botão com texto Monitorar
            monitor_link = await page.query_selector("a.btn:has-text('Monitorar')")
        if not monitor_link:
            print("      link Monitorar não encontrado")
            return False

        await monitor_link.click()
        await page.wait_for_timeout(1500)

        # Preenche e-mail no modal
        email_input = await page.query_selector(
            "input[type='email'], input[placeholder*='mail'], input[placeholder*='E-mail']"
        )
        if not email_input:
            print("      campo e-mail não encontrado no modal")
            return False
        await email_input.click()
        await email_input.fill(MONITOR_EMAIL)

        # Seleciona Mensalmente
        radios = await page.query_selector_all("input[type='radio']")
        for r in radios:
            val = (await r.get_attribute("value") or "").lower()
            if "mensal" in val:
                await r.check()
                break

        confirm = await page.query_selector("button:has-text('Confirmar')")
        if confirm:
            await confirm.click()
            await page.wait_for_timeout(1000)

        # Fecha modal residual se ainda aberto
        close = await page.query_selector("button.close, button[aria-label='Close']")
        if close:
            await close.click()

        return True
    except Exception as e:
        print(f"      monitor erro: {e}")
        return False

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

    best = entries[0]  # já ordenado do mais recente ao mais antigo
    date_str = best["date"].strftime("%d/%m/%Y") if best["date"] else "?"
    print(f"    melhor global: {best['name']} ({date_str}) via '{best['term']}'")

    # Renavega para a pesquisa que retornou essa linha e a localiza pelo expediente
    row = await find_row_by_expediente(page, best["term"], best["expediente"])
    if not row:
        return {"name": generic_name, "status": "no_pdf", "file": None}

    # Monitorar ANTES de baixar o PDF (checkbox → modal → email → Mensalmente → Confirmar)
    monitored = await register_monitor(page, row)
    print(f"    {'✓' if monitored else '~'} monitoramento: {MONITOR_EMAIL if monitored else 'falhou'}")

    bula_type = await download_pdf_from_row(page, row, dest)
    if not bula_type:
        return {"name": generic_name, "status": "no_pdf", "file": None}

    size_kb = dest.stat().st_size // 1024
    print(f"    ✓ {dest.name} ({size_kb} KB) — bula do {bula_type}")

    return {
        "name":        generic_name,
        "status":      "success",
        "file":        f"{slug}.pdf",
        "date":        date_str,
        "full_name":   row_name,
        "brand_used":  term_used,
        "bula_type":   bula_type,
        "monitored":   monitored,
    }

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
        mon = "✓ monit." if info.get("monitored") else "—"
        lines.append(
            f"- **{name}** → `{info['file']}` "
            f"| busca: _{info.get('brand_used','?')}_ "
            f"| pub: {info.get('date','?')} "
            f"| {mon}"
        )
    lines += ["", f"## Não Encontrados na ANVISA ({len(not_found)})", ""]
    lines += [f"- {n}" for n in sorted(not_found)] or ["_Nenhum_"]
    if errors:
        lines += ["", f"## Erros ({len(errors)})", ""]
        lines += [f"- **{e['name']}**: {e['reason']}" for e in errors]
    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    medications: list[str] = json.loads(MEDS_SIMPLE.read_text(encoding="utf-8"))
    brands_map   = load_brands_map()
    index        = load_index()
    not_found: list[str]  = []
    errors: list[dict]    = []

    print(f"MedAlert — Downloader de Bulas ANVISA")
    print(f"Medicamentos: {len(medications)} | resumir={RESUMIR} | delay={0 if SEM_DELAY else DELAY_SECONDS}s\n")

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

        for i, med in enumerate(medications):
            print(f"\n{'─'*60}")
            print(f"[{i+1}/{len(medications)}] {med}")

            if RESUMIR and med in index and index[med].get("status") == "success":
                dest = THIS_DIR / index[med].get("file", "")
                if dest.exists():
                    print("  ↷ já baixado, pulando")
                    continue

            brands = brands_map.get(med, [])

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

            if i < len(medications) - 1 and not SEM_DELAY:
                remaining = len(medications) - i - 1
                eta = (remaining * DELAY_SECONDS) // 60
                print(f"\n  ⏳ aguardando {DELAY_SECONDS}s… (restam {remaining}, ~{eta} min)")
                await asyncio.sleep(DELAY_SECONDS)

        await browser.close()

    REPORT_FILE.write_text(build_report(medications, index, not_found, errors), encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"CONCLUÍDO: {len(index)} baixados | {len(not_found)} não encontrados | {len(errors)} erros")
    print(f"Relatório: {REPORT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
