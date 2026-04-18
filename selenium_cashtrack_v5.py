# selenium_cashtrack_v4.py
# Python 3.10+
#
# Dependências:
#   pip install playwright python-dotenv pyyaml
#
# O que faz:
# - Login no Cashtrack
# - (Opcional) Importa o OFX mais recente da pasta 01_EntradaOFX/ofx
# - Abre /ofxreview
# - Para cada linha (N primeiras):
#     Ações -> "Gasto"
#     Lê "detalhes" (histórico)
#     Aplica regras YAML (when_all) -> seta IDs (tipo_id, categoria_id, fornecedor_id, centro_id, forma_pagamento)
#     Preenche os campos (Virtual Select vscomp) na ORDEM correta: Tipo -> Categoria -> Fornecedor -> Pagamento -> Centro
#     Espera o botão "Salvar" (Livewire wire:click=salvarGasto) habilitar e clica
#
# .env esperado (exemplos):
#   CASH_EMAIL=seuemail
#   CASH_SENHA=suasenha
#   EDGE_DRIVER_PATH=C:\drivers_edge\msedgedriver.exe
#   CASH_RULES_YAML=C:\Users\Usuario\Desktop\PYTHON TRABALHOS\concilia_cashtrack\Regras\regras_auto.yaml
#   CASH_USE_YAML_RULES=true
#   CASH_MAX_LINHAS=20
#   CASH_DO_IMPORT_OFX=true
#   CASH_BANCO=SICOOB WAGNER
#
# Labels (se no seu Cashtrack estiver diferente, ajuste no .env):
#   CASH_LABEL_TIPO=Tipo de gasto
#   CASH_LABEL_CATEGORIA=Categoria
#   CASH_LABEL_FORNECEDOR=Fornecedor
#   CASH_LABEL_FORMA_PGTO=Forma de pagamento
#   CASH_LABEL_CENTRO_CUSTO=Centro de custo
#
# IMPORTANTE (Cashtrack):
# - Categoria depende do Tipo de gasto. Se tentar setar categoria sem setar tipo, dá timeout.
# - Por isso, se houver categoria_id na regra, esta automação EXIGE tipo_id.
#
# ---- Estrutura recomendada do YAML ----
# rules:
#   - id: R005
#     when_all: ["STG", "PECAS"]
#     set:
#       tipo_id: "2"           # fixo/variavel/impostos/pessoal (ID do Cashtrack)
#       categoria_id: "130946"
#       fornecedor_id: "12365"
#       centro_id: "1624"
#       forma_pagamento: "1"   # ex: 1=PIX / 2=BOLETO (ajuste conforme seus IDs)

import os, re, json, traceback, copy
import time
import signal
import sys
from pathlib import Path
from typing import Optional, Dict, List, Any
import datetime
import unicodedata
from decimal import Decimal, InvalidOperation
from dotenv import load_dotenv
import yaml  # pip install pyyaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rpa.playwright_compat import (
    ActionChains,
    By,
    EC,
    EdgeOptions,
    EdgeService,
    Keys,
    WebDriverWait,
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
    start_playwright_driver,
    webdriver,
)

from engine_regras.coletor_aprendizado import (
    ColetorAprendizado,
    montar_payload_coleta,
)

from engine_regras.tratador_ofx import (
    encontrar_ofx_mais_recente,
    tratar_arquivo_ofx,
)
from engine_regras.normalizacao import (
    tokens_para_regra,
)

# =========================
# PATHS / ENV
# =========================

ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)
APRENDIZADO_DIR = PROJECT_ROOT / "aprendizado"
DIAGNOSTICS_DIR = PROJECT_ROOT / "diagnostics"

URL_LOGIN = "https://cashtrack.com.br/login"
LANCA_URL = "https://cashtrack.com.br/lancamento"
OFXREVIEW_URL = "https://cashtrack.com.br/ofxreview"

EMAIL = (os.getenv("CASH_EMAIL") or "").strip()
SENHA = (os.getenv("CASH_SENHA") or "").strip()

EDGE_DRIVER_PATH = os.getenv("EDGE_DRIVER_PATH") or r"C:\drivers_edge\msedgedriver.exe"
PLAYWRIGHT_CHANNEL = (os.getenv("CASH_BROWSER_CHANNEL") or "msedge").strip() or None
HEADLESS = (os.getenv("CASH_HEADLESS") or "false").lower() in ("1", "true", "yes", "y")

DO_IMPORT_OFX = (os.getenv("CASH_DO_IMPORT_OFX") or "true").lower() in ("1", "true", "yes", "y")
KEEP_BROWSER_OPEN = (os.getenv("CASH_KEEP_BROWSER_OPEN") or "true").lower() in ("1","true","yes","y")
BANCO_NOME = os.getenv("CASH_BANCO") or "SICOOB WAGNER"

TIMEOUT = int(os.getenv("CASH_TIMEOUT") or "20")
PAGELOAD_TIMEOUT = int(os.getenv("CASH_PAGELOAD_TIMEOUT") or "60")
MAX_LINHAS = int(os.getenv("CASH_MAX_LINHAS") or "10")

AUTO_PREENCHER_E_SALVAR = (os.getenv("CASH_AUTO_SALVAR") or "true").lower() in ("1", "true", "yes", "y")
PULAR_SE_SALVAR_NAO_HABILITAR = (os.getenv("CASH_PULAR_SE_NAO_HABILITAR") or "true").lower() in ("1", "true", "yes", "y")

RULES_YAML_PATH = (os.getenv("CASH_RULES_YAML") or "").strip()
USE_YAML_RULES = (os.getenv("CASH_USE_YAML_RULES") or "true").lower() in ("1", "true", "yes", "y")
AUTO_APRENDER_PENDENTES = (os.getenv("CASH_AUTO_APRENDER_PENDENTES") or "false").lower() in ("1", "true", "yes", "y")
RULES_APRENDIDAS_YAML_PATH = (os.getenv("CASH_RULES_APRENDIDAS_YAML") or str(PROJECT_ROOT / "Regras" / "regras_aprendidas_auto.yaml")).strip()
CURRENT_OFX_TRATADO_PATH: Optional[Path] = None
OFX_MATCH_CACHE: Dict[str, Dict[str, Any]] = {}
DIAG_SESSION_TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
DIAG_SESSION_DIR = DIAGNOSTICS_DIR / f"session_{DIAG_SESSION_TS}"
DIAG_ARTIFACTS_DIR = DIAG_SESSION_DIR / "artifacts"
DIAG_TIMELINE_PATH = DIAG_SESSION_DIR / "timeline.jsonl"
DIAG_SUMMARY_PATH = DIAG_SESSION_DIR / "summary.json"
DIAG_REPLAY_PATH = DIAG_SESSION_DIR / "replay_cases.jsonl"
REPLAY_TXID = (os.getenv("CASH_REPLAY_TXID") or "").strip()
REPLAY_DESCRICAO_RAW = (os.getenv("CASH_REPLAY_DESCRICAO") or "").strip()

# Export opcional: salva o resultado do match (uma linha por transação) para uso na conversão do OFX
EXPORT_MATCHES_CSV = False
EXPORT_MATCHES_CSV_PATH = "export_matches.csv"


# Mapa opcional: nome extraído do PIX -> fornecedor_id
FORNECEDOR_MAP_XLSX = (os.getenv("CASH_FORNECEDOR_MAP_XLSX") or "").strip()
# Categoria padrão de abastecimento (do seu histórico): 130950
ABAST_CATEGORIA_ID = (os.getenv("CASH_ABAST_CATEGORIA_ID") or "130950").strip()
ABAST_CENTRO_ID = (os.getenv("CASH_ABAST_CENTRO_ID") or "1626").strip()
ABAST_TIPO_ID = (os.getenv("CASH_ABAST_TIPO_ID") or "1").strip()
ABAST_FORMA_PGTO = (os.getenv("CASH_ABAST_FORMA_PGTO") or "1").strip()
ABAST_FORNECEDOR_MOVIDA_CD = os.getenv("CASH_ABAST_FORNECEDOR_MOVIDA_CD_ID", "").strip()
ABAST_FORNECEDOR_STG = os.getenv("CASH_ABAST_FORNECEDOR_STG_ID", "").strip()
ABAST_MATCH_TOKENS = [t.strip().upper() for t in (os.getenv("CASH_ABAST_MATCH_TOKENS") or "ABASTECIMENTO,GALAO").split(',') if t.strip()]
ABAST_ANOTAR_DETALHES = (os.getenv("CASH_ABAST_ANOTAR_DETALHES") or "false").lower() in ("1","true","yes","y")

LABEL_TIPO = os.getenv("CASH_LABEL_TIPO") or "Tipo de gasto"
LABEL_CATEGORIA = os.getenv("CASH_LABEL_CATEGORIA") or "Categoria"
LABEL_FORNECEDOR = os.getenv("CASH_LABEL_FORNECEDOR") or "Fornecedor"
LABEL_FORMA_PGTO = os.getenv("CASH_LABEL_FORMA_PGTO") or "Forma de pagamento"
LABEL_CENTRO_CUSTO = os.getenv("CASH_LABEL_CENTRO_CUSTO") or "Centro de custo"

# Defaults (fallback apenas para texto; para automação robusta use IDs no YAML)
VAL_CATEGORIA = os.getenv("CASH_VAL_CATEGORIA") or ""
VAL_FORNECEDOR = os.getenv("CASH_VAL_FORNECEDOR") or ""
VAL_FORMA_PGTO = os.getenv("CASH_VAL_FORMA_PGTO") or ""
VAL_CENTRO_CUSTO = os.getenv("CASH_VAL_CENTRO_CUSTO") or ""

BTN_SALVAR_LIVEWIRE = (By.CSS_SELECTOR, "button[wire\\:click^='salvarGasto']")

# =========================
# LOG
# =========================

def alpine_select_by_data_value(driver, button_id: str, option_value: str, timeout: int = 12, label: str = ""):
    """
    Dropdown Alpine/Livewire:
    - clica no botão (id fixo)
    - acha opção pelo data-value
    - clica e espera aplicar
    """
    btn_xpath = f"//*[@id='{button_id}']"
    opt_xpath = f"//*[@data-value='{option_value}']"

    # 1) abre dropdown
    btn = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.XPATH, btn_xpath)))
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
    time.sleep(0.15)
    try:
        btn.click()
    except Exception:
        driver.execute_script("arguments[0].click();", btn)

    # 2) espera opção aparecer
    opt = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.XPATH, opt_xpath)))
    WebDriverWait(driver, timeout).until(lambda d: opt.is_displayed())

    # 3) clica na opção
    try:
        opt.click()
    except Exception:
        driver.execute_script("arguments[0].click();", opt)

    # 4) dá tempo do Alpine/Livewire atualizar o texto selecionado e re-render
    time.sleep(0.25)

    if label:
        log(f"✅ {label} selecionado: {option_value}")

    return True

def _repair_mojibake(texto: str) -> str:
    s = str(texto or "")
    suspicious = ("Ã", "â", "ð", "ï", "Â", "œ", "Ÿ")
    if not any(tok in s for tok in suspicious):
        return s

    candidates = {s}
    queue = [s]
    for _ in range(3):
        if not queue:
            break
        cur = queue.pop(0)
        for enc in ("cp1252", "latin-1"):
            try:
                fixed = cur.encode(enc, errors="strict").decode("utf-8", errors="strict")
            except Exception:
                continue
            if fixed not in candidates:
                candidates.add(fixed)
                queue.append(fixed)

    def bad_score(t: str) -> int:
        return sum(t.count(x) for x in ("Ã", "â", "ð", "ï", "Â", "œ", "Ÿ"))
    best = min(candidates, key=bad_score)
    fixes = {
        "✅": "✅",
        "⚠️": "⚠️",
        "⚠️": "⚠️",
        "❌": "❌",
        "🔦": "🔦",
        "🧪": "🧪",
        "🧾": "🧾",
        "💰": "💰",
        "🔎": "🔎",
        "📊": "📊",
        "📈": "📈",
        "📌": "📌",
        "🧩": "🧩",
        "⏱️": "⏱️",
        "🔁": "🔁",
        "⏭️": "⏭️",
        "🌐": "🌐",
        "📂": "📂",
        "📘": "📘",
        "📏": "📏",
        "🔐": "🔐",
        "💾": "💾",
        "🏦": "🏦",
        "🛑": "🛑",
        "🧠": "🧠",
        "📝": "📝",
        "🆔": "🆔",
        "📅": "📅",
        "👁️": "👁️",
        "🚨": "🚨",
        "➡️": "➡️",
        "→": "→",
        "✅": "✅",
        "✅ ": "✅ ",
        "✅ Processo terminou.": "✅ Processo terminou.",
        "✅ Processo finalizado com sucesso.": "✅ Processo finalizado com sucesso.",
        "Vá": "Vá",
        "faça": "faça",
        "exclusões": "exclusões",
        "permissao": "permissão",
        "ficará": "ficará",
        "Ã ": "à",
        "á": "á",
        "â": "â",
        "ã": "ã",
        "ç": "ç",
        "é": "é",
        "ê": "ê",
        "í": "í",
        "ó": "ó",
        "ô": "ô",
        "õ": "õ",
        "ú": "ú",
        "Á": "Á",
        "É": "É",
        "Ó": "Ó",
        "Ú": "Ú",
    }
    for src, dst in fixes.items():
        best = best.replace(src, dst)
    return best

def log(msg: str) -> None:
    texto = _repair_mojibake(msg)
    try:
        print(texto, flush=True)
    except Exception:
        # fallback seguro para consoles limitados: remove apenas símbolos não renderizáveis
        seguro = str(texto).encode("ascii", errors="ignore").decode("ascii", errors="ignore")
        if not seguro.strip():
            seguro = str(texto).encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        try:
            print(seguro, flush=True)
        except Exception:
            pass

 
def _safe_filename(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(s))
    return s[:140]

def dump_diag(driver, tag: str, context: dict, out_dir="diagnostics"):
    if out_dir == "diagnostics":
        out_dir = str(DIAG_ARTIFACTS_DIR)
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = _safe_filename(tag)
    base = os.path.join(out_dir, f"{ts}__{tag}")

    # context
    payload = {
        "ts": ts,
        "tag": tag,
        "url": getattr(driver, "current_url", None),
        "title": getattr(driver, "title", None),
        "context": context,
    }
    with open(base + "__context.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # screenshot
    try:
        driver.save_screenshot(base + "__screen.png")
    except Exception as e:
        with open(base + "__screenshot_error.txt", "w", encoding="utf-8") as f:
            f.write(str(e))

    # page html
    try:
        with open(base + "__page.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
    except Exception as e:
        with open(base + "__page_error.txt", "w", encoding="utf-8") as f:
            f.write(str(e))

    # row html (opcional)
    row_xpath = context.get("row_xpath")
    if row_xpath:
        try:
            rows = driver.find_elements(By.XPATH, row_xpath)
            if rows:
                with open(base + "__row.html", "w", encoding="utf-8") as f:
                    f.write(rows[0].get_attribute("outerHTML"))
            else:
                with open(base + "__row_not_found.txt", "w", encoding="utf-8") as f:
                    f.write(row_xpath)
        except Exception as e:
            with open(base + "__row_error.txt", "w", encoding="utf-8") as f:
                f.write(str(e))

    # erro / stack (opcional)
    err = context.get("exception")
    if err:
        with open(base + "__error.txt", "w", encoding="utf-8") as f:
            f.write("".join(traceback.format_exception(type(err), err, err.__traceback__)))

    return base    

def ensure_diag_session() -> None:
    try:
        os.makedirs(DIAG_SESSION_DIR, exist_ok=True)
        os.makedirs(DIAG_ARTIFACTS_DIR, exist_ok=True)
    except Exception:
        pass

def _json_safe(value: Any):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return str(value)

def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    try:
        ensure_diag_session()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_json_safe(payload), ensure_ascii=False) + "\n")
    except Exception:
        pass

def timeline_event(event: str, **data: Any) -> None:
    payload = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "data": _json_safe(data),
    }
    append_jsonl(DIAG_TIMELINE_PATH, payload)

def write_summary_json(payload: Dict[str, Any]) -> None:
    try:
        ensure_diag_session()
        with open(DIAG_SUMMARY_PATH, "w", encoding="utf-8") as f:
            json.dump(_json_safe(payload), f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def append_replay_case(payload: Dict[str, Any]) -> None:
    append_jsonl(DIAG_REPLAY_PATH, payload)

def _deep_get(obj: Any, path: str) -> Any:
    cur = obj
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(cur, list):
            if cur and isinstance(cur[0], dict):
                cur = cur[0]
            else:
                return None
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    if isinstance(cur, list) and cur and isinstance(cur[0], dict):
        return cur[0]
    return cur

def get_livewire_state_pack(driver) -> Dict[str, Any]:
    try:
        pack = driver.execute_script(
            """
            const isVisible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const hasSalvarGasto = (el) => {
              const wc = (el && el.getAttribute) ? (el.getAttribute('wire:click') || '') : '';
              return wc.includes('salvarGasto');
            };
            const visibleBtn = Array.from(document.querySelectorAll('button')).find(el => isVisible(el) && hasSalvarGasto(el)) || null;
            const findWireHost = (el) => {
              let cur = el || null;
              while (cur && cur !== document.body) {
                if (cur.getAttribute && cur.getAttribute('wire:id')) return cur;
                cur = cur.parentElement;
              }
              return null;
            };
            const wireHost = findWireHost(visibleBtn);
            const wireId = wireHost ? (wireHost.getAttribute('wire:id') || '') : '';
            const comps = (window.Livewire && Livewire.all) ? Livewire.all() : [];
            let comp = null;
            if (wireId) {
              comp = comps.find(c => String(c?.id || '') === wireId || String(c?.snapshot?.memo?.id || '') === wireId) || null;
            }
            if (!comp) {
              comp = comps.find(c => {
                const d = c?.snapshot?.data || {};
                return d && Object.prototype.hasOwnProperty.call(d, 'openedTransaction') &&
                  Object.prototype.hasOwnProperty.call(d, 'listaTransacoes');
              }) || null;
            }
            const pick = (obj) => {
              try { return JSON.parse(JSON.stringify(obj ?? null)); } catch (e) { return null; }
            };
            return {
              wire_id: wireId || (comp?.snapshot?.memo?.id || comp?.id || ''),
              component_name: comp?.snapshot?.memo?.name || '',
              button_disabled_attr: visibleBtn ? visibleBtn.getAttribute('disabled') : null,
              button_disabled_expr: visibleBtn ? (visibleBtn.getAttribute(':disabled') || visibleBtn.getAttribute('x-bind:disabled') || '') : '',
              button_wire_click: visibleBtn ? (visibleBtn.getAttribute('wire:click') || '') : '',
              snapshot_data: pick(comp?.snapshot?.data),
              reactive: pick(comp?.reactive),
              ephemeral: pick(comp?.ephemeral),
              canonical: pick(comp?.canonical),
            };
            """,
        )
        return pack if isinstance(pack, dict) else {}
    except Exception as e:
        return {"erro": f"{type(e).__name__}: {e}"}

def _state_focus(pack: Dict[str, Any]) -> Dict[str, Any]:
    focused_paths = [
        "openedTransaction", "type",
        "tipogasto", "categoriaGasto", "fornecedor", "formaPagamento", "centro",
        "bulkTipogasto", "bulkFornecedor", "bulkFormaPagamento", "bulkCentro", "bulkCategoria",
        "form.tipogasto", "form.fornecedor", "form.formapagamento", "form.centro_id", "form.categoria",
        "gastoForm.tipogasto", "gastoForm.fornecedor_id", "gastoForm.formapagamento", "gastoForm.centro_id", "gastoForm.categoria_id",
    ]
    out: Dict[str, Any] = {}
    for root_name in ("snapshot_data", "reactive", "ephemeral", "canonical"):
        root = pack.get(root_name)
        if root is None:
            continue
        out[root_name] = {path: _deep_get(root, path) for path in focused_paths}
    out["meta"] = {
        "wire_id": pack.get("wire_id"),
        "component_name": pack.get("component_name"),
        "button_disabled_attr": pack.get("button_disabled_attr"),
        "button_disabled_expr": pack.get("button_disabled_expr"),
        "button_wire_click": pack.get("button_wire_click"),
    }
    return out

def _flat_map(obj: Any, prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            return _flat_map(obj[0], prefix)
        out[prefix or "$"] = obj
        return out
    if isinstance(obj, dict):
        if not obj:
            out[prefix or "$"] = {}
            return out
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                out.update(_flat_map(v, key))
            else:
                out[key] = v
        return out
    out[prefix or "$"] = obj
    return out

def diff_state(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    a = _flat_map(before or {})
    b = _flat_map(after or {})
    keys = sorted(set(a) | set(b))
    changed = []
    for key in keys:
        if a.get(key) != b.get(key):
            changed.append({"path": key, "before": _json_safe(a.get(key)), "after": _json_safe(b.get(key))})
    return {"changed": changed[:200], "changed_count": len(changed)}

def build_consistency_report(driver, valores: Optional[Dict[str, str]] = None, livewire_pack: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    valores = valores or {}
    field_snapshot = get_form_field_snapshot(driver)
    pack = livewire_pack or get_livewire_state_pack(driver)
    focus = _state_focus(pack)
    expected = {
        "tipo": str(valores.get("tipo_id") or "").strip(),
        "categoria": str(valores.get("categoria_id") or "").strip(),
        "fornecedor": str(valores.get("fornecedor_id") or "").strip(),
        "forma_pgto": str(valores.get("forma_pagamento") or "").strip(),
        "centro": str(valores.get("centro_id") or "").strip(),
    }
    report = {}
    mapping = {
        "tipo": ["snapshot_data.bulkTipogasto", "snapshot_data.tipogasto", "reactive.bulkTipogasto", "reactive.tipogasto"],
        "categoria": ["snapshot_data.bulkCategoria", "snapshot_data.categoriaGasto", "reactive.bulkCategoria", "reactive.categoriaGasto"],
        "fornecedor": ["snapshot_data.bulkFornecedor", "snapshot_data.fornecedor", "reactive.bulkFornecedor", "reactive.fornecedor"],
        "forma_pgto": ["snapshot_data.bulkFormaPagamento", "snapshot_data.formaPagamento", "reactive.bulkFormaPagamento", "reactive.formaPagamento"],
        "centro": ["snapshot_data.bulkCentro", "snapshot_data.centro", "reactive.bulkCentro", "reactive.centro", "snapshot_data.gastoForm.centro_id"],
    }
    for key, expected_value in expected.items():
        snap = (field_snapshot.get(key) or {}) if isinstance(field_snapshot, dict) else {}
        lw_values = []
        for path in mapping.get(key, []):
            root, _, sub = path.partition(".")
            lw_values.append({"path": path, "value": _deep_get(pack.get(root) or {}, sub)})
        report[key] = {
            "expected": expected_value,
            "ui_value": str(snap.get("value") or "").strip(),
            "ui_text": str(snap.get("text") or "").strip(),
            "livewire_candidates": lw_values,
        }
    return report

def _row_matches_replay(txid: int, desc_tabela: str) -> bool:
    desc_norm = normalizar_texto(desc_tabela or "")
    if REPLAY_TXID and str(txid) != str(REPLAY_TXID):
        return False
    if REPLAY_DESCRICAO_RAW and normalizar_texto(REPLAY_DESCRICAO_RAW) not in desc_norm:
        return False
    return bool(REPLAY_TXID or REPLAY_DESCRICAO_RAW)

# =========================
# SIGNAL
# =========================

_SIGINT_HIT = {"hit": False, "when": None}

def _sigint_handler(signum, frame):
    _SIGINT_HIT["hit"] = True
    _SIGINT_HIT["when"] = time.strftime("%H:%M:%S")
    log(f"🚨 SIGINT recebido às {_SIGINT_HIT['when']} (Ctrl+C).")

def install_sigint_debug():
    signal.signal(signal.SIGINT, _sigint_handler)

# =========================
# DRIVER
# =========================

def start_driver_edge():
    log("🌐 Criando Edge driver (driver local)...")
    if not os.path.exists(EDGE_DRIVER_PATH):
        raise FileNotFoundError(f"msedgedriver.exe não encontrado em: {EDGE_DRIVER_PATH}")

    options = EdgeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.set_capability("browserName", "MicrosoftEdge")

    service = EdgeService(executable_path=EDGE_DRIVER_PATH)
    driver = webdriver.Edge(service=service, options=options)

    driver.set_page_load_timeout(PAGELOAD_TIMEOUT)
    driver.set_script_timeout(30)
    driver.implicitly_wait(0)

    log("✅ Edge driver iniciado com sucesso!")
    return driver

# =========================
# HELPERS
# =========================

FORNECEDOR_MAP: Dict[str, str] = {}

def _norm_txt(s: str) -> str:
    import unicodedata, re
    if s is None:
        return ""
    s = str(s).strip().upper()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s)
    return s

_DOC_PAT = re.compile(r"(\d{2,3}[.\s]?\d{3}[.\s]?\d{3}[/-]?\d{2,4}|\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\*{2,}\.?\d{2,3}\.?\d{3}\.?\d{3}[- ]\*{2,})")


def normalizar_texto(s: str) -> str:
    if not s:
        return ""
    s = s.upper().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))  # remove acentos
    s = " ".join(s.split())  # remove espaços duplicados
    return s


def extrair_nome_pix(detalhes: str) -> str:
    """
    Tenta extrair o 'nome' que aparece no texto do OFX/linha, depois do documento,
    nos padrões comuns do Cashtrack/OFX:
      - '... PAGAMENTO PIX <doc> NOME - PIX'
      - '... PAGAMENTO PIX ***.123.456-** NOME - PIX'
    Retorna string normalizada (MAIÚSCULA).
    """
    d = _norm_txt(detalhes)
    if "PAGAMENTO PIX" in d:
        part = d.split("PAGAMENTO PIX", 1)[1].strip()
    else:
        # fallback: tenta após 'PIX ' (última ocorrência)
        idx = d.rfind("PIX")
        part = d[idx+3:].strip() if idx >= 0 else d

    # remove docs/códigos no início
    part = re.sub(r"^[0-9.\-\/ ]{5,}\s+", "", part).strip()
    part = re.sub(r"^\*+[\d.\-\/ ]+\*+\s+", "", part).strip()
    part = _DOC_PAT.sub("", part, count=1).strip()

    # corta antes de "- PIX" ou "-"
    part = re.split(r"\s+-\s+PIX|\s+-\s+|\s+PIX$", part)[0].strip()

    # limpa chars
    part = re.sub(r"[^A-Z0-9 ]+", " ", part)
    part = re.sub(r"\s+", " ", part).strip()

    # remove tokens lixo
    if part in ("PIX", "PAGAMENTO", "PAGTO"):
        return ""
    if len(part) < 3:
        return ""
    return part[:60]


def extrair_descricao_usuario_para_detalhes(detalhes: str) -> str:
    """
    Extrai somente a descrição digitada pelo usuário no pagamento PIX,
    removendo o prefixo do banco, CPF/CNPJ e o sufixo '- PIX'.

    Exemplos:
      'PIX EMITIDO ... ***.025.653-** ROSE SEMANAL - PIX' -> 'ROSE SEMANAL'
      'PIX EMITIDO ... 10.861.044 0001-60 CASA DO PINTOR - PIX' -> 'CASA DO PINTOR'

    Se não houver descrição útil após o documento, devolve vazio para
    deixar o preenchimento manual.
    """
    bruto = (detalhes or "").strip()
    if not bruto:
        return ""

    s = normalizar_texto(bruto)

    if "PIX EMITIDO" not in s or "PAGAMENTO PIX" not in s:
        return ""

    part = s.split("PAGAMENTO PIX", 1)[1].strip()
    part = re.sub(r"\s*-\s*PIX\s*$", "", part, flags=re.IGNORECASE).strip()

    # remove documento mascarado ou numérico do início
    patterns = [
        r"^\*{3}\.\d{3}\.\d{3}-\*{2}\s+",
        r"^\d{2}\.\d{3}\.\d{3}\s+\d{4}-\d{2}\s+",
        r"^\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\s+",
        r"^\d{2,3}\.\d{3}\.\d{3}-\d{2}\s+",
    ]
    original_part = part
    for pat in patterns:
        novo = re.sub(pat, "", part).strip()
        if novo != part:
            part = novo
            break

    # fallback usando o mesmo extrator do nome PIX
    if part == original_part:
        part = extrair_nome_pix(bruto)

    part = re.sub(r"\s*-\s*PIX\s*$", "", part, flags=re.IGNORECASE).strip()
    part = re.sub(r"\s+", " ", part).strip()

    # se não sobrou descrição útil, deixa manual
    if not part:
        return ""
    if _DOC_PAT.fullmatch(part):
        return ""
    if len(part) < 3:
        return ""

    return part[:120]

def limpar_descricao_movimentacao(texto: str) -> str:
    texto = (texto or "").strip()
    if not texto:
        return ""

    extraido_pix = extrair_descricao_usuario_para_detalhes(texto)
    if extraido_pix:
        return extraido_pix

    cleaned = texto
    substitutions = [
        (r"^\s*-\s*", ""),
        (r"^\s*PIX EMITIDO.*?PAGAMENTO PIX\s*", ""),
        (r"^\s*PAGAMENTO PIX\s*", ""),
        (r"^\s*D[ÉE]B\.?\s*TIT\.?COMPE\s*EFETIVADO\s*-\s*", ""),
        (r"^\s*D[ÉE]B\.?\s*PAGAMENTO DE BOLETO INTERCREDIS\s*-\s*", ""),
        (r"^\s*PAGAMENTO DE BOLETO INTERCREDIS\s*-\s*", ""),
        (r"^\s*PAGAMENTO DE BOLETO\s*-\s*", ""),
        (r"\s*-\s*PIX\s*$", ""),
        (r"\s*-\s*\d{6,}\s*$", ""),
        (r"^\*{3}\.\d{3}\.\d{3}-\*{2}\s*", ""),
        (r"^\d{2}\.\d{3}\.\d{3}[ /]?\d{4}-\d{2}\s*", ""),
        (r"^\d{2,3}\.\d{3}\.\d{3}-\d{2}\s*", ""),
        (r"^\d{2}\.\d{3}\.\d{3}\s+\d{4}-\d{2}\s*", ""),
    ]
    for pattern, repl in substitutions:
        cleaned = re.sub(pattern, repl, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned or texto


def limpar_detalhes_duplicados(texto: str) -> str:
    texto = " ".join((texto or "").strip().split())
    if not texto:
        return ""

    def _norm(valor: str) -> str:
        valor = (valor or "").upper().strip()
        valor = re.sub(r"\s+", " ", valor)
        return valor

    # Caso clássico: TEXTO - PIX TEXTO
    match = re.match(r"^(.*?)\s*-\s*PIX\s+(.*?)$", texto, flags=re.IGNORECASE)
    if match:
        esquerda = match.group(1).strip()
        direita = match.group(2).strip()

        if _norm(esquerda) == _norm(direita):
            return esquerda

    # Caso metade repetida
    partes = texto.split()
    if len(partes) >= 4:
        meio = len(partes) // 2
        primeira = " ".join(partes[:meio]).strip()
        segunda = " ".join(partes[meio:]).strip()

        if _norm(primeira) == _norm(segunda):
            return primeira

    return texto


def preencher_detalhes_exatos(driver, texto: str) -> bool:
    """Substitui o campo detalhes pelo texto limpo do usuário."""
    texto = (texto or "").strip()
    if not texto:
        return False
    try:
        ta = WebDriverWait(driver, 8).until(
            EC.visibility_of_element_located((By.ID, "detalhes"))
        )
        driver.execute_script(
            "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', {bubbles:true}));",
            ta,
            texto,
        )
        return True
    except Exception:
        return False

def carregar_mapa_fornecedor() -> None:
    """
    Carrega um XLSX (duas colunas) com:
      - nome_extraido_pix
      - fornecedor_id_mode
    Gerado a partir do seu histórico.
    """
    global FORNECEDOR_MAP
    if not FORNECEDOR_MAP_XLSX:
        return
    try:
        import pandas as pd
        df = pd.read_excel(FORNECEDOR_MAP_XLSX)
        cols = [c.strip().lower() for c in df.columns]
        df.columns = cols
        if "nome_extraido_pix" not in cols or "fornecedor_id_mode" not in cols:
            log("⚠️ Mapa XLSX não tem colunas esperadas: nome_extraido_pix, fornecedor_id_mode")
            return
        m = {}
        for _, r in df.iterrows():
            k = _norm_txt(r["nome_extraido_pix"])
            v = str(r["fornecedor_id_mode"]).replace(".0","").strip()
            if k and v:
                m[k] = v
        FORNECEDOR_MAP = m
        log(f"✅ Mapa fornecedor PIX carregado: {len(FORNECEDOR_MAP)} entradas")
    except Exception as e:
        log(f"⚠️ Falha ao carregar mapa fornecedor PIX: {type(e).__name__} | {e}")

def infer_fornecedor_id_por_pix(detalhes: str) -> str:
    """
    Usa o mapa histórico para converter o nome extraído do PIX em fornecedor_id.
    """
    if not FORNECEDOR_MAP:
        return ""
    nome = extrair_nome_pix(detalhes)
    if not nome:
        return ""
    return FORNECEDOR_MAP.get(_norm_txt(nome), "")

def anotar_detalhes(driver, texto: str) -> None:
    """
    Acrescenta observação no campo 'detalhes' para auditoria (sem apagar o original).
    """
    if not texto:
        return
    try:
        ta = driver.find_element(By.ID, "detalhes")
        atual = (ta.get_attribute("value") or "").strip()
        if texto in atual:
            return
        novo = (atual + " | " + texto).strip() if atual else texto
        driver.execute_script("arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', {bubbles:true}));", ta, novo)
    except Exception:
        pass

def wait_ready(driver, timeout: int = 30) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
    )

def safe_click(driver, el, pause: float = 0.10) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    try:
        ActionChains(driver).move_to_element(el).pause(pause).click(el).perform()
        return
    except (ElementClickInterceptedException, StaleElementReferenceException):
        pass
    try:
        el.click()
        return
    except Exception:
        driver.execute_script("arguments[0].click();", el)

def close_overlays(driver) -> None:
    xpaths = [
        "//button[contains(.,'Aceitar')]",
        "//button[contains(.,'Entendi')]",
        "//button[contains(.,'OK')]",
        "//button[contains(.,'Fechar')]",
        "//*[@role='dialog']//button[normalize-space()='×' or normalize-space()='x' or normalize-space()='X']",
        "//*[@role='dialog']//*[@aria-label='Close' or @aria-label='Fechar']",
    ]
    for xp in xpaths:
        try:
            el = WebDriverWait(driver, 1).until(EC.element_to_be_clickable((By.XPATH, xp)))
            driver.execute_script("arguments[0].click();", el)
            time.sleep(0.15)
        except Exception:
            pass

def dump_import_auditoria(driver, tag: str = "importar") -> None:
    try:
        ensure_diag_session()
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = DIAG_ARTIFACTS_DIR / f"{ts}__{tag}__import_buttons.json"
        html_path = DIAG_ARTIFACTS_DIR / f"{ts}__{tag}__page.html"
        payload = driver.execute_script("""
            const nodes = [...document.querySelectorAll("button, a, [role='button']")];
            return nodes.map((el, idx) => ({
                idx,
                text: (el.innerText || el.textContent || '').trim().slice(0, 200),
                id: el.id || '',
                classes: String(el.className || ''),
                aria: el.getAttribute('aria-label') || '',
                role: el.getAttribute('role') || '',
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                disabled: !!el.disabled
            }));
        """)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        html_path.write_text(driver.page_source, encoding="utf-8")
        log(f"Auditoria de botoes salva em: {path}")
    except Exception as e:
        log(f"Falha ao salvar auditoria de botoes: {type(e).__name__} | {e}")

def accept_cookies(driver, timeout: int = 3) -> None:
    xpaths = [
        "//button[normalize-space()='Aceitar']",
        "//button[contains(normalize-space(.), 'Aceitar')]",
        "//*[@role='button'][contains(normalize-space(.), 'Aceitar')]",
        "//button[contains(normalize-space(.), 'Accept')]",
        "//*[@id='onetrust-accept-btn-handler']",
    ]
    for xp in xpaths:
        try:
            btn = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xp))
            )
            safe_click(driver, btn)
            time.sleep(0.4)
            log("Cookies aceitos")
            return
        except Exception:
            continue

def get_current_url_safe(driver) -> Optional[str]:
    try:
        return driver.current_url
    except WebDriverException:
        return None


def _extrair_tag_bloco(texto: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", texto, flags=re.IGNORECASE | re.DOTALL)
    return (m.group(1).strip() if m else "")


def _normalizar_data_chave(texto: str) -> str:
    s = (texto or "").strip()
    if not s:
        return ""

    m = re.match(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"

    m = re.match(r"(\d{2})/(\d{2})/(\d{2,4})", s)
    if m:
        ano = m.group(3)
        if len(ano) == 2:
            ano = f"20{ano}"
        return f"{m.group(1)}/{m.group(2)}/{ano}"

    return s


def _normalizar_valor_chave(texto: str) -> str:
    s = (texto or "").strip()
    if not s:
        return ""
    s = s.replace("R$", "").replace("\u00a0", " ").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return f"{Decimal(s):.2f}"
    except (InvalidOperation, ValueError):
        return s



def carregar_mapa_ofx_tratado(caminho_ofx: Optional[Path]) -> Dict[str, Any]:
    """Indexa o OFX tratado para busca rápida por data/valor e reuso controlado de FITID."""
    if not caminho_ofx:
        return {"rows": [], "by_key": {}, "used_fitids": set()}

    chave_cache = str(Path(caminho_ofx).resolve())
    if chave_cache in OFX_MATCH_CACHE:
        return OFX_MATCH_CACHE[chave_cache]

    try:
        conteudo_ofx = Path(caminho_ofx).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        conteudo_ofx = Path(caminho_ofx).read_text(encoding="latin-1", errors="ignore")

    transacoes_indexadas: List[Dict[str, str]] = []
    transacoes_por_data_valor: Dict[str, List[Dict[str, str]]] = {}

    blocos_transacao = re.findall(r"<STMTTRN>(.*?)</STMTTRN>", conteudo_ofx, flags=re.IGNORECASE | re.DOTALL)
    for bloco_transacao in blocos_transacao:
        transacao_ofx = {
            "data": _normalizar_data_chave(_extrair_tag_bloco(bloco_transacao, "DTPOSTED")),
            "valor": _normalizar_valor_chave(_extrair_tag_bloco(bloco_transacao, "TRNAMT")),
            "fitid": _extrair_tag_bloco(bloco_transacao, "FITID"),
            "memo": _extrair_tag_bloco(bloco_transacao, "MEMO"),
            "name": _extrair_tag_bloco(bloco_transacao, "NAME"),
        }
        transacao_ofx["detalhes"] = (transacao_ofx["name"] or transacao_ofx["memo"] or "").strip()
        transacoes_indexadas.append(transacao_ofx)

        chave_data_valor = f"{transacao_ofx['data']}|{transacao_ofx['valor']}"
        transacoes_por_data_valor.setdefault(chave_data_valor, []).append(transacao_ofx)

    mapa_indexado = {
        "rows": transacoes_indexadas,
        "by_key": transacoes_por_data_valor,
        "used_fitids": set(),
    }
    OFX_MATCH_CACHE[chave_cache] = mapa_indexado
    log(f"🧾 OFX tratado indexado: {Path(caminho_ofx).name} | transações={len(transacoes_indexadas)}")
    return mapa_indexado


def obter_detalhes_origem_ofx(row_data: Dict[str, str], caminho_ofx: Optional[Path]) -> Dict[str, str]:
    """
    Localiza a transação mais compatível no OFX tratado.

    Regra de prioridade:
    1) mesmo par data + valor
    2) maior aderência textual com a descrição da linha da tela
    3) evita reutilizar o mesmo FITID quando possível
    """
    mapa_ofx = carregar_mapa_ofx_tratado(caminho_ofx)
    data_linha = _normalizar_data_chave((row_data or {}).get("data", ""))
    valor_linha = _normalizar_valor_chave((row_data or {}).get("valor", ""))
    chave_data_valor = f"{data_linha}|{valor_linha}"

    candidatos_mesma_chave = list((mapa_ofx.get("by_key") or {}).get(chave_data_valor, []))
    fitids_utilizados = mapa_ofx.get("used_fitids", set())

    descricao_tela_limpa = normalizar_texto((row_data or {}).get("descricao", ""))
    descricao_tela_original = normalizar_texto((row_data or {}).get("descricao_raw", ""))
    tokens_relevantes = {
        token
        for token in re.findall(r"[A-Z0-9]+", f"{descricao_tela_original} {descricao_tela_limpa}")
        if len(token) >= 4 and token not in _STOP_CONTEXT and token not in _STOP_CONTEXT_GENERIC
    }

    def pontuar_candidato(transacao_ofx: Dict[str, str]) -> tuple:
        detalhes_candidato = normalizar_texto(transacao_ofx.get("detalhes", ""))
        score_texto = 0

        if descricao_tela_original and descricao_tela_original in detalhes_candidato:
            score_texto += 100
        if descricao_tela_limpa and descricao_tela_limpa in detalhes_candidato:
            score_texto += 80

        score_texto += sum(10 for token in tokens_relevantes if token in detalhes_candidato)

        fitid_candidato = transacao_ofx.get("fitid", "")
        fitid_livre = 1 if not fitid_candidato or fitid_candidato not in fitids_utilizados else 0
        return (fitid_livre, score_texto)

    if candidatos_mesma_chave:
        candidatos_ordenados = sorted(candidatos_mesma_chave, key=pontuar_candidato, reverse=True)
        for transacao_ofx in candidatos_ordenados:
            fitid_candidato = transacao_ofx.get("fitid", "")
            if fitid_candidato and fitid_candidato in fitids_utilizados:
                continue
            if fitid_candidato:
                fitids_utilizados.add(fitid_candidato)
            return transacao_ofx

        transacao_ofx_escolhida = candidatos_ordenados[0]
        fitid_candidato = transacao_ofx_escolhida.get("fitid", "")
        if fitid_candidato:
            fitids_utilizados.add(fitid_candidato)
        return transacao_ofx_escolhida

    return {
        "data": data_linha,
        "valor": valor_linha,
        "fitid": "",
        "memo": "",
        "name": "",
        "detalhes": "",
    }

def get_visible_txids(driver):
    """Retorna lista de txids visíveis na tabela /ofxreview, na ordem."""
    rows = driver.find_elements(By.XPATH, "//tr[@*[name()='wire:key'] and starts-with(@*[name()='wire:key'],'row-')]")
    txids = []
    for r in rows:
        k = r.get_attribute("wire:key") or r.get_attribute("wire:key".replace(":", "\\:"))
        # às vezes o Selenium pega pelo get_attribute normal; se não, tenta via JS
        if not k:
            try:
                k = driver.execute_script("return arguments[0].getAttribute('wire:key')", r)
            except Exception:
                k = None
        if not k:
            continue
        m = re.search(r"row-(\d+)", k)
        if m:
            txids.append(int(m.group(1)))
    return txids

def scroll_row_into_view(driver, txid: int):
    row_xpath = f"//tr[@*[name()='wire:key']='row-{txid}']"
    row = WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.XPATH, row_xpath)))
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", row)
    return row

def get_row_details_text(driver, txid: int):
    """
    Ajuste o xpath se necessário.
    Pega o texto da coluna 'Detalhes' da linha.
    """
    row_xpath = f"//tr[@*[name()='wire:key']='row-{txid}']"
    # tenta pegar a primeira célula de texto (detalhes) — ajuste se sua tabela mudar
    el = WebDriverWait(driver, 6).until(
        EC.presence_of_element_located((By.XPATH, row_xpath + "//td[2]"))
    )
    return (el.text or "").strip()

def has_gasto_action(driver, txid: int):
    """Checa se existe botão/ação 'Gasto' para esse txid."""
    action_value = f"openTransaction({txid}, 'gasto')"
    xpath = f"//*[@*[name()='wire:click']=\"{action_value}\"]"
    els = driver.find_elements(By.XPATH, xpath)
    return any(e.is_displayed() for e in els) if els else False


def limpar_descricao_pix(texto: str) -> str:
    """
    Limpa a descrição PIX sem mexer na lógica principal do fluxo.
    Prioriza a extração do texto digitado pelo usuário; se não encontrar,
    aplica apenas uma limpeza conservadora.
    """
    texto = (texto or "").strip()
    if not texto:
        return ""

    return limpar_descricao_movimentacao(texto)


# =========================
# Dropdown: Ações -> openTransaction(txid,'gasto')
# =========================

def open_transaction_dropdown_action(driver, txid: int, tipo: str, timeout: int = 12):
    script = f"""
    const txid = {txid};
    const tipo = {json.dumps(tipo)};
    const row = document.querySelector("tr[wire\\\\:key='row-" + txid + "']");
    if (!row) return {{ ok: false, stage: 'row' }};

    const isVisible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    const clickEl = (el) => {{
        if (!el) return false;
        try {{ el.scrollIntoView({{ block: 'center', inline: 'center' }}); }} catch (e) {{}}
        try {{ el.click(); return true; }} catch (e) {{}}
        try {{
            el.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true, cancelable: true }}));
            el.dispatchEvent(new MouseEvent('mouseup', {{ bubbles: true, cancelable: true }}));
            el.dispatchEvent(new MouseEvent('click', {{ bubbles: true, cancelable: true }}));
            return true;
        }} catch (e) {{}}
        return false;
    }};

    const actionWire = "openTransaction(" + txid + ", '" + tipo + "')";
    const actionSelector = [
        "[wire\\\\:click=\\"" + actionWire + "\\"]",
        "[wire\\\\:click*=\\"" + actionWire + "\\"]",
    ];

    // 1) tenta encontrar a ação já renderizada na linha
    for (const sel of actionSelector) {{
        const direct = row.querySelector(sel);
        if (direct) {{
            clickEl(direct);
            return {{ ok: true, stage: 'action_direct', text: (direct.innerText || '').trim() }};
        }}
    }}

    // 2) abre o botão "Ações" da própria linha
    const toggleCandidates = [
        ...row.querySelectorAll("#acoes"),
        ...row.querySelectorAll("td#acao button"),
        ...row.querySelectorAll("td#acoes button"),
        ...row.querySelectorAll("button[aria-haspopup='true']"),
        ...row.querySelectorAll("button[aria-expanded]"),
        ...row.querySelectorAll("td:last-child button"),
        ...row.querySelectorAll("button"),
    ];

    let clickedToggle = null;
    for (const btn of toggleCandidates) {{
        const txt = (btn.innerText || '').trim().toUpperCase();
        const title = (btn.getAttribute('title') || '').trim().toUpperCase();
        const id = (btn.id || '').trim().toUpperCase();
        const cls = (btn.className || '').toString();
        const seemsAction =
            id === 'ACOES' ||
            txt === '' ||
            txt.includes('ACOES') ||
            title.includes('ACOES') ||
            cls.includes('inline-flex items-center justify-center w-full px-2 py-1');
        if (!seemsAction) continue;
        if (clickEl(btn)) {{
            clickedToggle = btn;
            break;
        }}
    }}

    // 3) tenta clicar no item desejado depois do menu abrir
    const allCandidates = [];
    for (const sel of actionSelector) {{
        allCandidates.push(...row.querySelectorAll(sel));
        allCandidates.push(...document.querySelectorAll(sel));
    }}
    allCandidates.push(
        ...row.querySelectorAll("[role='menuitem']"),
        ...document.querySelectorAll("[role='menuitem']")
    );

    for (const el of allCandidates) {{
        const wc = (el.getAttribute('wire:click') || '').trim();
        const txt = (el.innerText || '').trim();
        if (wc.includes(actionWire) || txt.toUpperCase().includes(tipo.toUpperCase())) {{
            clickEl(el);
            return {{
                ok: true,
                stage: clickedToggle ? 'menu_action_click' : 'action_fallback_click',
                text: txt,
                wireClick: wc,
            }};
        }}
    }}

    return {{
        ok: false,
        stage: clickedToggle ? 'menu_opened_but_action_missing' : 'toggle_missing',
        availableWireClicks: [...row.querySelectorAll("[wire\\\\:click]")]
            .map(el => (el.getAttribute('wire:click') || '').trim())
            .filter(Boolean)
            .slice(0, 20),
        visibleTexts: [...row.querySelectorAll("button, a, [role='menuitem']")]
            .map(el => (el.innerText || '').trim())
            .filter(Boolean)
            .slice(0, 20),
    }};
    """

    result = driver.execute_script(script)
    log(f"🧪 open_transaction result txid={txid}: {result}")

    if not result or not result.get("ok"):
        raise TimeoutException(f"Falha abrindo ação '{tipo}' do txid={txid}: {result}")

    time.sleep(0.5)
    return True


def debug_estado_formulario(driver, txid: int):
    """
    Diagnóstico do estado do form logo após abrir a linha.
    """
    try:
        snap = driver.execute_script("""
            const comps = (window.Livewire && Livewire.all) ? Livewire.all() : [];
            for (const c of comps) {
                const d = c?.snapshot?.data || {};
                const hasKeys =
                    Object.prototype.hasOwnProperty.call(d, 'openedTransaction') &&
                    Object.prototype.hasOwnProperty.call(d, 'selectedTransactionId') &&
                    Object.prototype.hasOwnProperty.call(d, 'listaTransacoes');
                if (hasKeys) {
                    return {
                        openedTransaction: d.openedTransaction ?? null,
                        selectedTransactionId: d.selectedTransactionId ?? null,
                        selectedTransaction: d.selectedTransaction ?? null,
                        detalhe: d.detalhe ?? null,
                        detalhes: d.detalhes ?? null,
                        valor: d.valor ?? null,
                        type: d.type ?? null,
                        tipomovimentacao: d.tipomovimentacao ?? null,
                    };
                }
            }
            return {};
        """)
        log(f"🧪 SNAPSHOT txid={txid}: {snap}")
    except Exception as e:
        log(f"⚠️ SNAPSHOT txid={txid} falhou: {type(e).__name__} | {e}")

    try:
        campos = driver.execute_script("""
            const nodes = [...document.querySelectorAll("input, textarea, select")];
            return nodes.map(el => ({
                tag: el.tagName,
                id: el.id || "",
                name: el.name || "",
                type: el.type || "",
                value: (el.value || "").slice(0, 120),
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            })).filter(x =>
                x.visible &&
                (
                    x.id.toLowerCase().includes('detal') ||
                    x.name.toLowerCase().includes('detal') ||
                    x.id.toLowerCase().includes('valor') ||
                    x.name.toLowerCase().includes('valor')
                )
            );
        """)
        log(f"🧪 CAMPOS VISÍVEIS txid={txid}: {campos}")
    except Exception as e:
        log(f"⚠️ CAMPOS txid={txid} falhou: {type(e).__name__} | {e}")

    try:
        comp = get_active_gasto_livewire_snapshot(driver)
        resumo = {
            "wire_id": comp.get("wire_id"),
            "name": comp.get("name"),
            "openedTransaction": comp.get("data", {}).get("openedTransaction"),
            "type": comp.get("data", {}).get("type"),
            "tipogasto": comp.get("data", {}).get("tipogasto"),
            "categoriaGasto": comp.get("data", {}).get("categoriaGasto"),
            "fornecedor": comp.get("data", {}).get("fornecedor"),
            "centro": comp.get("data", {}).get("centro"),
            "formaPagamento": comp.get("data", {}).get("formaPagamento"),
            "buttonWireClick": comp.get("buttonWireClick"),
        }
        log(f"[DBG] COMPONENTE ATIVO txid={txid}: {resumo}")
    except Exception as e:
        log(f"[WARN] COMPONENTE ATIVO txid={txid} falhou: {type(e).__name__} | {e}")


def _find_visible_action_menu_for_row(driver, txid: int):
    """
    Procura o menu visível da própria linha.
    Estrutura observada:
      td#acao -> div.inline-block.text-left -> button(seta) + div(menu)
    """
    candidates = driver.find_elements(
        By.XPATH,
        f"//tr[@*[name()='wire:key']='row-{txid}']//td[@id='acao']//div[contains(@class,'inline-block')]//div[@role='menu' or contains(@class,'shadow-lg')]"
    )

    for el in candidates:
        try:
            if el.is_displayed():
                return el
        except Exception:
            pass

    # fallback: menu visível dentro da célula de ação da linha
    candidates = driver.find_elements(
        By.XPATH,
        f"//tr[@*[name()='wire:key']='row-{txid}']//td[@id='acao']//*[self::div or self::section][@role='menu' or contains(@class,'shadow-lg')]"
    )
    for el in candidates:
        try:
            if el.is_displayed():
                return el
        except Exception:
            pass

    return False


def _find_action_button_in_menu(menu, txid: int, tipos_candidatos: list[str]):
    """
    Acha o botão certo dentro do menu aberto.
    Usa @*[name()='wire:click'] para evitar erro de namespace no XPath.
    """
    for tipo in tipos_candidatos:
        xpath = (
            f".//*[@role='menuitem' and "
            f"contains(@*[name()='wire:click'], \"openTransaction({txid}, '{tipo}')\")]"
        )

        els = menu.find_elements(By.XPATH, xpath)
        for el in els:
            try:
                if el.is_displayed():
                    return el
            except Exception:
                pass

    return None


def _list_menu_actions(menu):
    """
    Retorna lista para debug:
    texto + wire:click disponível no menu.
    """
    out = []
    try:
        items = menu.find_elements(By.XPATH, ".//*[@role='menuitem']")
        for it in items:
            try:
                txt = (it.text or "").strip()
                wc = (it.get_attribute("wire:click") or "").strip()
                out.append({"texto": txt, "wire:click": wc})
            except Exception:
                pass
    except Exception:
        pass
    return out


# =========================
# YAML RULES
# =========================

def normalize_text(s: str) -> str:
    import unicodedata
    import re

    if s is None:
        return ""
    s = str(s).strip().upper()

    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))

    s = re.sub(r"\s+", " ", s)
    return s



def load_yaml_rules(path: str) -> List[Dict[str, Any]]:
    """Carrega, normaliza e ordena regras YAML por especificidade."""
    if not os.path.exists(path):
        log(f"⚠️ YAML não encontrado em: {path}")
        return []

    try:
        with open(path, "r", encoding="utf-8") as arquivo_yaml:
            conteudo_yaml = yaml.safe_load(arquivo_yaml) or {}

        regras_yaml = conteudo_yaml.get("rules") or []
        regras_normalizadas: List[Dict[str, Any]] = []

        for indice_regra, regra_bruta in enumerate(regras_yaml, start=1):
            if not isinstance(regra_bruta, dict):
                continue

            regra_normalizada = dict(regra_bruta)
            regra_normalizada.setdefault("id", f"R{indice_regra:03d}")

            tokens_when_all = regra_normalizada.get("when_all") or []
            tokens_when_all = [str(token).strip() for token in tokens_when_all if str(token).strip()]
            regra_normalizada["when_all"] = tokens_when_all

            definicoes_regra = regra_normalizada.get("set") or {}
            definicoes_normalizadas: Dict[str, str] = {}
            if isinstance(definicoes_regra, dict):
                for nome_campo, valor_campo in definicoes_regra.items():
                    if valor_campo is None:
                        continue
                    valor_texto = str(valor_campo).strip()
                    if valor_texto.endswith(".0"):
                        valor_texto = valor_texto[:-2]
                    definicoes_normalizadas[str(nome_campo)] = valor_texto
            regra_normalizada["set"] = definicoes_normalizadas

            regras_normalizadas.append(regra_normalizada)

        def total_uso_regra(regra: Dict[str, Any]) -> float:
            estatisticas = regra.get("stats") or {}
            try:
                return float(estatisticas.get("n", 0))
            except Exception:
                return 0.0

        regras_normalizadas.sort(
            key=lambda regra: (len(regra.get("when_all") or []), total_uso_regra(regra)),
            reverse=True,
        )
        log(f"📘 YAML carregado: {len(regras_normalizadas)} regras (ordenadas por especificidade).")
        return regras_normalizadas

    except Exception as erro:
        log(f"❌ Falha ao ler YAML: {erro}")
        return []


def match_rule(detalhes: str, rules: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Escolhe a melhor regra YAML cujo when_all esteja contido no texto informado."""
    if not detalhes or not rules:
        return None

    texto_normalizado = normalize_text(detalhes)
    melhor_regra: Optional[Dict[str, Any]] = None
    melhor_pontuacao = -1

    for regra_atual in rules:
        tokens_obrigatorios = regra_atual.get("when_all") or []
        if not tokens_obrigatorios:
            continue

        todos_tokens_presentes = True
        for token_regra in tokens_obrigatorios:
            token_normalizado = normalize_text(str(token_regra))
            if token_normalizado and token_normalizado not in texto_normalizado:
                todos_tokens_presentes = False
                break

        if not todos_tokens_presentes:
            continue

        pontuacao_regra = len(tokens_obrigatorios)
        estatisticas_regra = regra_atual.get("stats") or {}
        try:
            pontuacao_regra = pontuacao_regra * 100000 + int(float(estatisticas_regra.get("n", 0)))
        except Exception:
            pontuacao_regra = pontuacao_regra * 100000

        if pontuacao_regra > melhor_pontuacao:
            melhor_regra = regra_atual
            melhor_pontuacao = pontuacao_regra

    return melhor_regra

def do_login(driver) -> None:
    if not EMAIL or not SENHA:
        raise RuntimeError("CASH_EMAIL ou CASH_SENHA não carregou do .env")

    log("🔐 Abrindo login...")
    driver.get(URL_LOGIN)
    wait_ready(driver, timeout=PAGELOAD_TIMEOUT)
    accept_cookies(driver)

    email_input = WebDriverWait(driver, TIMEOUT).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='email']"))
    )
    senha_input = WebDriverWait(driver, TIMEOUT).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='password']"))
    )

    email_input.clear()
    email_input.send_keys(EMAIL)
    senha_input.clear()
    senha_input.send_keys(SENHA)

    entrar_btn = WebDriverWait(driver, TIMEOUT).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))
    )
    driver.execute_script("arguments[0].click();", entrar_btn)
    log("✅ Cliquei em Entrar")

    WebDriverWait(driver, 60).until(
        EC.any_of(
            EC.url_contains("dashboard"),
            EC.url_contains("/lancamento"),
            EC.url_contains("/ofxreview"),
            EC.presence_of_element_located((By.XPATH, "//*[contains(.,'Lançamentos') or contains(.,'Lancamentos')]")),
        )
    )
    log(f"✅ Pós-login. URL: {get_current_url_safe(driver)}")

# =========================
# IMPORT OFX (opcional)
# =========================

def find_latest_ofx() -> str:
    """
    Agora não devolve mais o OFX bruto.
    Localiza o OFX bruto mais recente, gera o tratado e devolve o caminho do tratado.
    """
    pasta_entrada = PROJECT_ROOT / "01_EntradaOFX" / "ofx"
    pasta_saida = PROJECT_ROOT / "01_EntradaOFX" / "ofx_tratado"

    caminho_bruto = encontrar_ofx_mais_recente(pasta_entrada)
    log(f"📂 OFX bruto encontrado: {caminho_bruto}")

    caminho_tratado = tratar_arquivo_ofx(caminho_bruto, pasta_saida)
    global CURRENT_OFX_TRATADO_PATH
    CURRENT_OFX_TRATADO_PATH = Path(caminho_tratado)
    log(f"📂 OFX tratado que será importado: {caminho_tratado}")

    return str(caminho_tratado)


def abrir_importar(driver, timeout: int = 30) -> None:
    log("📂 Abrindo Importar...")
    accept_cookies(driver, timeout=3)
    close_overlays(driver)
    dump_import_auditoria(driver, tag="antes_abrir_importar")
    for by, sel in [
        (By.XPATH, "//button[normalize-space()='Importar']"),
        (By.XPATH, "//button[contains(., 'Importar')]"),
        (By.XPATH, "//a[contains(., 'Importar')]"),
        (By.XPATH, "//*[self::button or self::a][contains(normalize-space(.), 'Importar')]"),
    ]:
        try:
            el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((by, sel)))
            safe_click(driver, el)
            time.sleep(0.6)
            break
        except Exception:
            pass

    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, "//button[contains(normalize-space(.), 'Novo OFX')]"))
        )
    except Exception:
        dump_import_auditoria(driver, tag="falha_abrir_importar")
        raise
    dump_import_auditoria(driver, tag="apos_abrir_importar")
    log("✅ Importar pronto (Novo OFX visível)")


def abrir_modal_novo_ofx(driver, timeout: int = 30) -> None:
    for _ in range(3):
        btn = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, "//button[contains(normalize-space(.), 'Novo OFX')]"))
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        driver.execute_script("arguments[0].click();", btn)
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.ID, "selectBanco"))
            )
            log("✅ Modal Novo OFX abriu")
            return
        except Exception:
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except Exception:
                pass
            time.sleep(0.6)

    raise TimeoutException("Não consegui abrir modal Novo OFX")


def abrir_menu_importar_para_navegacao(driver, timeout: int = 20) -> None:
    for by, sel in [
        (By.XPATH, "//button[@id='importLan']"),
        (By.XPATH, "//button[.//span[normalize-space()='Importar']]"),
        (By.XPATH, "//button[contains(normalize-space(.), 'Importar')]"),
        (By.XPATH, "//*[@role='button'][contains(normalize-space(.), 'Importar')]"),
    ]:
        try:
            btn = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, sel)))
            safe_click(driver, btn)
            time.sleep(0.4)
            return
        except Exception:
            continue
    raise TimeoutException("Nao consegui reabrir o menu Importar")


def estado_modal_ofx(driver) -> Dict[str, Any]:
    try:
        return driver.execute_script(
            """
            const isVisible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const modal = document.querySelector('#modal');
            const banco = document.querySelector('#selectBanco');
            const spinner = document.querySelector("[x-show='loading']");

            let modalOfxAberta = null;
            for (const el of document.querySelectorAll("[wire\\:snapshot]")) {
                const snap = el.getAttribute("wire:snapshot") || "";
                if (snap.includes("modalOfxAberta")) {
                    modalOfxAberta = snap.includes('"modalOfxAberta":1');
                    break;
                }
            }

            return {
                modal_present: !!modal,
                modal_visible: isVisible(modal),
                banco_present: !!banco,
                banco_visible: isVisible(banco),
                spinner_visible: isVisible(spinner),
                modal_ofx_aberta: modalOfxAberta,
            };
            """
        ) or {}
    except Exception:
        return {}


def modal_ofx_realmente_fechado(driver) -> bool:
    url_atual = get_current_url_safe(driver) or ""
    if "/ofxreview" in url_atual:
        return True

    estado = estado_modal_ofx(driver)
    if not estado:
        return False
    return (
        not estado.get("modal_present")
        and not estado.get("banco_present")
        and estado.get("modal_ofx_aberta") is not True
        and not estado.get("spinner_visible")
    )


def ir_para_continuar_conciliacao(driver, timeout: int = 30) -> None:
    accept_cookies(driver, timeout=2)
    close_overlays(driver)

    # 1) caminho preferencial: usar o próprio wire:navigate da página
    try:
        ok = driver.execute_script(
            """
            const alvo =
                document.querySelector("li[wire\\:navigate][href*='/ofxreview']") ||
                document.querySelector("[wire\\:navigate][href*='/ofxreview']") ||
                document.querySelector("a[href*='/ofxreview']");
            if (!alvo) return false;
            alvo.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
            return true;
            """
        )
        if ok:
            WebDriverWait(driver, timeout).until(lambda d: "/ofxreview" in (get_current_url_safe(d) or ""))
            return
    except Exception:
        pass

    # 2) fallback simples: navegar direto para a revisão
    try:
        driver.get(OFXREVIEW_URL)
        WebDriverWait(driver, timeout).until(lambda d: "/ofxreview" in (get_current_url_safe(d) or ""))
        return
    except Exception:
        pass

    # 3) fallback: tentar pelo menu Importar, se estiver disponível
    try:
        abrir_menu_importar_para_navegacao(driver, timeout=10)
    except Exception:
        dump_import_auditoria(driver, tag="falha_reabrir_importar")
        raise

    for by, sel in [
        (By.XPATH, "//button[contains(normalize-space(.), 'Continuar Concilia')]"),
        (By.XPATH, "//li[contains(normalize-space(.), 'Continuar Concilia')]"),
        (By.XPATH, "//*[@href='https://cashtrack.com.br/ofxreview']"),
    ]:
        try:
            item = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, sel)))
            safe_click(driver, item)
            WebDriverWait(driver, timeout).until(lambda d: "/ofxreview" in (get_current_url_safe(d) or ""))
            return
        except Exception:
            continue

    dump_import_auditoria(driver, tag="falha_continuar_conciliacao")
    raise TimeoutException("Nao consegui acionar 'Continuar Conciliacao'")


def selecionar_banco(driver, banco_nome: str, timeout: int = 20) -> None:
    log(f"🏦 Selecionando banco: {banco_nome}")
    gatilho = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, "selectBanco")))
    driver.execute_script("arguments[0].click();", gatilho)
    WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.XPATH, "//ul[@role='listbox' and not(contains(@style,'display: none'))]"))
    )
    opcao = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.XPATH, f"//ul[@role='listbox']//li[@role='option'][.//span[normalize-space()='{banco_nome}']]"))
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", opcao)
    driver.execute_script("arguments[0].click();", opcao)
    log("✅ Banco selecionado")

def anexar_ofx(driver, caminho_arquivo: str, timeout: int = 20) -> None:
    input_file = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
    )
    input_file.send_keys(caminho_arquivo)
    log("✅ OFX anexado")


def submeter_modal_ofx(driver, timeout: int = 20) -> None:
    btn_salvar = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "#modal button[type='submit']"))
    )
    try:
        safe_click(driver, btn_salvar)
        time.sleep(0.6)
    except Exception:
        pass

    try:
        driver.execute_script(
            """
            const btn = arguments[0];
            const form = btn ? btn.closest('form') : null;
            if (!form) return false;

            if (typeof form.requestSubmit === 'function') {
                form.requestSubmit(btn);
            } else {
                form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
            }
            return true;
            """,
            btn_salvar,
        )
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", btn_salvar)
        except Exception:
            pass

    WebDriverWait(driver, max(timeout, 45)).until(lambda d: modal_ofx_realmente_fechado(d))
    if "/ofxreview" in (get_current_url_safe(driver) or ""):
        log("✅ Importação já redirecionou para /ofxreview")
    else:
        log("✅ Modal OFX fechado")


def importar_ofx(driver, banco_nome: str, caminho_ofx: str) -> None:
    driver.get(LANCA_URL)
    wait_ready(driver, timeout=PAGELOAD_TIMEOUT)
    accept_cookies(driver, timeout=4)
    close_overlays(driver)
    abrir_importar(driver)
    abrir_modal_novo_ofx(driver)
    selecionar_banco(driver, banco_nome)
    anexar_ofx(driver, caminho_ofx)

    log("💾 Salvando modal OFX...")
    try:
        submeter_modal_ofx(driver, timeout=30)
    except Exception:
        dump_import_auditoria(driver, tag="modal_novo_ofx_ainda_aberto")
        raise

    time.sleep(1.5)
    ir_para_continuar_conciliacao(driver, timeout=40)
    WebDriverWait(driver, 60).until(EC.presence_of_element_located((By.CSS_SELECTOR, "tbody tr")))
    log("✅ /ofxreview carregado")

# =========================
# Row utils
# =========================

def limpar_descricao_pix(texto: str) -> str:
    """
    Limpa a descrição PIX sem mexer na lógica principal do fluxo.
    Prioriza a extração do texto digitado pelo usuário; se não encontrar,
    aplica apenas uma limpeza conservadora.
    """
    texto = (texto or "").strip()
    if not texto:
        return ""

    return limpar_descricao_movimentacao(texto)


def extract_row_data(row) -> Dict[str, str]:
    tds = row.find_elements(By.CSS_SELECTOR, "td")

    def norm_header(txt: str) -> str:
        base = normalizar_texto(txt or "")
        base = re.sub(r"[^A-Z0-9]+", " ", base).strip()
        return base

    def header_indexes() -> Dict[str, int]:
        aliases = {
            "descricao": {"DETALHES", "DESCRICAO", "DESCRICAO HISTORICO", "HISTORICO"},
            "valor": {"VALOR"},
            "data": {"DATA", "VENCIMENTO", "DATA VENCIMENTO"},
            "conta": {"BANCO", "CONTA", "CONTA BANCARIA"},
            "situacao": {"SITUACAO", "STATUS"},
            "acoes": {"ACOES"},
        }
        found: Dict[str, int] = {}
        try:
            tabela = row.find_element(By.XPATH, "./ancestor::table[1]")
            headers = tabela.find_elements(By.CSS_SELECTOR, "thead th")
            for idx, th in enumerate(headers):
                texto = norm_header(th.text or th.get_attribute("title") or "")
                for key, names in aliases.items():
                    if texto in names and key not in found:
                        found[key] = idx
        except Exception:
            pass
        return found

    def safe_text(i: int) -> str:
        try:
            return (tds[i].get_attribute("title") or tds[i].text or "").strip()
        except Exception:
            return ""

    idx = header_indexes()
    descricao_raw = safe_text(idx.get("descricao", 3))
    descricao_limpa = limpar_descricao_pix(descricao_raw)

    return {
        "descricao_raw": descricao_raw,
        "descricao": descricao_limpa,
        "valor": safe_text(idx.get("valor", 4)),
        "data": safe_text(idx.get("data", 1)),
        "conta": safe_text(idx.get("conta", 0)),
        "situacao": safe_text(idx.get("situacao", 1)),
    }

def preencher_detalhes_limpo(driver, texto: str, timeout: int = 10):
    campo = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.ID, "detalhes"))
    )

    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", campo)
    driver.execute_script("arguments[0].value = '';", campo)
    campo.clear()
    campo.send_keys(texto)

    driver.execute_script("""
        arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
        arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
        arguments[0].dispatchEvent(new Event('blur', { bubbles: true }));
    """, campo)


def get_txid_from_row(row) -> int:
    wk = row.get_attribute("wire:key") or ""
    m = re.search(r"row-(\d+)", wk)
    if not m:
        raise RuntimeError(f"wire:key não encontrado na row: {wk}")
    return int(m.group(1))


def row_tem_acao_gasto(driver, txid: int) -> bool:
    """
    Se a linha NÃO tiver o botão openTransaction(txid,'gasto'), é quase certo que é receita
    (ou um tipo diferente). Assim a gente pula antes de tentar abrir modal.
    """
    row_xpath = f"//tr[@*[name()='wire:key']='row-{txid}']"
    action_value = f"openTransaction({txid}, 'gasto')"
    btn_xpath = row_xpath + f"//button[@*[name()='wire:click']=\"{action_value}\"]"
    try:
        els = driver.find_elements(By.XPATH, btn_xpath)
        return len(els) > 0
    except Exception:
        return False


def linha_parece_gasto(row_data: Dict[str, str]) -> bool:
    situacao = normalizar_texto((row_data or {}).get("situacao", ""))
    return "PAGAR" in situacao
    

# =========================
# Form wait
# =========================

def get_active_gasto_livewire_snapshot(driver) -> dict:
    """
    Retorna metadados do componente Livewire associado ao formulário de gasto visível.
    Prioriza o componente ancestral do botão "Salvar" ativo.
    """
    try:
        data = driver.execute_script(
            """
            const isVisible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const hasSalvarGasto = (el) => {
              const wc = (el && el.getAttribute) ? (el.getAttribute('wire:click') || '') : '';
              return wc.includes('salvarGasto');
            };
            const visibleButton = Array.from(document.querySelectorAll("button"))
              .find(el => isVisible(el) && hasSalvarGasto(el));
            const findWireHost = (el) => {
              let cur = el || null;
              while (cur && cur !== document.body) {
                if (cur.getAttribute && cur.getAttribute('wire:id')) return cur;
                cur = cur.parentElement;
              }
              return null;
            };
            const wireHost = findWireHost(visibleButton);
            const wireId = wireHost ? (wireHost.getAttribute('wire:id') || '') : '';
            const comps = (window.Livewire && Livewire.all) ? Livewire.all() : [];
            let comp = null;
            if (wireId) {
              comp = comps.find(c =>
                (String(c?.id || '') === wireId) ||
                (String(c?.snapshot?.memo?.id || '') === wireId)
              ) || null;
            }
            if (!comp) {
              comp = comps.find(c => {
                const d = c?.snapshot?.data || {};
                return d
                  && Object.prototype.hasOwnProperty.call(d, 'openedTransaction')
                  && Object.prototype.hasOwnProperty.call(d, 'selectedTransactionId')
                  && Object.prototype.hasOwnProperty.call(d, 'listaTransacoes');
              }) || null;
            }
            const snap = (comp && comp.snapshot && comp.snapshot.data) ? comp.snapshot.data : {};
            return {
              wire_id: wireId || (comp?.snapshot?.memo?.id || comp?.id || ''),
              name: comp?.snapshot?.memo?.name || '',
              buttonWireClick: visibleButton ? (visibleButton.getAttribute('wire:click') || '') : '',
              buttonDisabledAttr: visibleButton ? visibleButton.getAttribute('disabled') : null,
              buttonDisabledExpr: visibleButton ? (visibleButton.getAttribute(':disabled') || visibleButton.getAttribute('x-bind:disabled') || '') : '',
              data: snap || {},
            };
            """
        )
        return data or {}
    except Exception:
        return {}


def get_ofxreview_livewire_snapshot(driver) -> dict:
    """
    Retorna o snapshot.data do componente principal/ativo da tela /ofxreview.
    """
    active = get_active_gasto_livewire_snapshot(driver)
    if isinstance(active, dict):
        data = active.get("data")
        if isinstance(data, dict) and data:
            return data
    return {}
    

def wait_formulario_detalhes(driver, timeout: int = 20):
    """
    Espera sinais reais do formulário aberto.
    Aceita tanto 'detalhe' quanto 'detalhes'.
    """
    def _achar_form(d):
        candidatos = [
            (By.ID, "detalhe"),
            (By.ID, "detalhes"),
            (By.XPATH, "//textarea[@id='detalhe' or @name='detalhe']"),
            (By.XPATH, "//textarea[@id='detalhes' or @name='detalhes']"),
            (By.XPATH, "//input[@id='detalhe' or @name='detalhe']"),
            (By.XPATH, "//input[@id='detalhes' or @name='detalhes']"),
            (By.XPATH, "//*[self::textarea or self::input][contains(@wire:model,'detalhe')]"),
            (By.XPATH, "//*[self::textarea or self::input][contains(@wire:model,'detalhes')]"),
            (By.XPATH, "//button[contains(@wire:click,'salvarGasto')]"),
            (By.XPATH, "//button[contains(@wire:click,'salvarMovimentacao')]"),
        ]

        for by, sel in candidatos:
            try:
                els = d.find_elements(by, sel)
                for el in els:
                    try:
                        if el.is_displayed():
                            return el
                    except Exception:
                        pass
            except Exception:
                pass

        # fallback pelo estado Livewire
        snap = get_ofxreview_livewire_snapshot(d)
        if snap.get("openedTransaction") and str(snap.get("type") or "").strip().lower() == "gasto":
            return True

        return False

    return WebDriverWait(driver, timeout).until(_achar_form)


def wait_formulario_preenchido(driver, timeout: int = 12):
    """
    Espera o formulário ter conteúdo real.
    """
    def _pronto(d):
        detalhe, valor = ler_form_detalhes_e_valor(d)
        return bool((detalhe or "").strip() or (valor or "").strip())

    return WebDriverWait(driver, timeout).until(_pronto)


def perf_now() -> float:
    return time.perf_counter()

def perf_elapsed(start: float) -> float:
    return time.perf_counter() - start

def perf_log(label: str, start: float, bucket: Optional[Dict[str, float]] = None, key: Optional[str] = None) -> float:
    sec = perf_elapsed(start)
    log(f"⏱️ {label}: {sec:.2f}s")
    if bucket is not None and key:
        bucket[key] = sec
    return sec

def _norm_money(s: str) -> str:
    if not s:
        return ""
    s = str(s).strip()
    s = s.replace("R$", "").replace("\u00a0", " ").strip()
    s = s.replace(".", "").replace(",", ".")
    s = s.replace(" ", "")
    return s

_STOP_CONTEXT = {
    "PIX", "EMITIDO", "RECEBIDO", "OUTRA", "MESMA", "IF", "PAGAMENTO", "RECEBIMENTO",
    "TRANSFERENCIA", "TRANSF", "CONTA", "CONTAS", "BANCO", "OUTRO", "OUTRAIF", "PESSOA",
    "PAGTO", "TED", "DOC", "CHAVE", "CPF", "CNPJ", "LTDA", "S", "A"
}

_STOP_CONTEXT_GENERIC = {
    "PRODUCAO", "PREPARADOR", "PINTOR", "LANTERNAGEM", "LANTERNEIRO", "EMPAPELADOR",
    "MECANICO", "LAVA", "JATO", "ADM", "NOTE", "CARRO", "CARROS", "ABASTECIMENTO",
    "ABAST", "SERV", "EXTRA", "MESADA", "COMPRA", "CARTAO", "CONTROLE", "OFICINA"
}

WRAPPER_LABEL_KEYS = {
    "categoria": [LABEL_CATEGORIA, "Categoria"],
    "fornecedor": [LABEL_FORNECEDOR, "Fornecedor"],
    "forma_pgto": [LABEL_FORMA_PGTO, "Forma de pagamento", "Forma Pgto"],
    "centro": [LABEL_CENTRO_CUSTO, "Centro de custo", "Centro"],
}
VS_FIELD_ID_KEYS = {
    "categoria": ["categorias", "categoria"],
    "fornecedor": ["fornecedores", "fornecedor"],
    "forma_pgto": ["formapagamento", "forma_pagamento"],
    "centro": ["selectCentro", "centros", "centro"],
}

def extrair_tokens_fortes_contexto(texto: str) -> List[str]:
    toks = re.findall(r"[A-Z0-9]+", normalizar_texto(texto))
    fortes: List[str] = []
    has_alpha = any(any(ch.isalpha() for ch in t) for t in toks)

    for t in toks:
        if t in _STOP_CONTEXT:
            continue
        if t.isdigit():
            if has_alpha:
                # Quando a descrição já possui texto forte, ignorar números curtos/data
                continue
            if len(t) >= 5:
                fortes.append(t)
        else:
            if t in _STOP_CONTEXT_GENERIC:
                continue
            if len(t) >= 4:
                fortes.append(t)

    return fortes[:8]

def _tokens_distintivos_contexto(texto: str) -> List[str]:
    toks = re.findall(r"[A-Z0-9]+", normalizar_texto(texto))
    out: List[str] = []
    for t in toks:
        if t in _STOP_CONTEXT or t in _STOP_CONTEXT_GENERIC:
            continue
        if t.isdigit():
            continue
        if len(t) >= 4:
            out.append(t)
    return out[:6]

def contexto_form_confere(desc_tabela: str, detalhes_form: str) -> bool:
    dt = normalizar_texto(desc_tabela)
    df = normalizar_texto(detalhes_form)
    if not dt or not df:
        return False
    if dt in df:
        return True

    distintivos = _tokens_distintivos_contexto(desc_tabela)
    if distintivos:
        hits_dist = [t for t in distintivos if t in df]
        # Exige pelo menos um token distintivo (nome, fornecedor, marca)
        return len(hits_dist) >= 1

    fortes = extrair_tokens_fortes_contexto(desc_tabela)
    if not fortes:
        return False
    hits = [t for t in fortes if t in df]
    needed = 1 if len(fortes) <= 2 else 2
    return len(hits) >= needed

def contexto_txid_confere(driver, txid: int) -> bool:
    try:
        active = get_active_gasto_livewire_snapshot(driver)
        opened = str((active.get("data") or {}).get("openedTransaction") or "").strip()
        return opened == str(txid)
    except Exception:
        return False

def ler_form_detalhes_e_valor(driver) -> tuple[str, str]:
    detalhe = ""
    valor_form = ""

    # 1) DOM
    candidatos_detalhe = [
        (By.ID, "detalhe"),
        (By.ID, "detalhes"),
        (By.XPATH, "//textarea[contains(@id,'detal') or contains(@name,'detal')]"),
        (By.XPATH, "//input[contains(@id,'detal') or contains(@name,'detal')]"),
        (By.XPATH, "//*[self::textarea or self::input][contains(@wire:model,'detalhe') or contains(@wire:model,'detalhes')]"),
    ]

    for by, sel in candidatos_detalhe:
        try:
            for el in driver.find_elements(by, sel):
                try:
                    if el.is_displayed():
                        detalhe = (el.get_attribute("value") or el.text or "").strip()
                        if detalhe:
                            break
                except Exception:
                    pass
            if detalhe:
                break
        except Exception:
            pass

    candidatos_valor = [
        (By.XPATH, "//input[contains(@id,'valor') or contains(@name,'valor')]"),
        (By.XPATH, "//*[self::input][contains(@wire:model,'valor')]"),
    ]

    for by, sel in candidatos_valor:
        try:
            for el in driver.find_elements(by, sel):
                try:
                    if el.is_displayed():
                        valor_form = (el.get_attribute("value") or "").strip()
                        if valor_form:
                            break
                except Exception:
                    pass
            if valor_form:
                break
        except Exception:
            pass

    # 2) fallback Livewire
    if not detalhe or not valor_form:
        try:
            snap = get_ofxreview_livewire_snapshot(driver)

            if not detalhe:
                detalhe = str(
                    snap.get("detalhe")
                    or snap.get("detalhes")
                    or ""
                ).strip()

            if not valor_form:
                valor_form = str(
                    snap.get("valor")
                    or ""
                ).strip()
        except Exception:
            pass

    return detalhe, valor_form


def fechar_formulario_aberto(driver) -> None:
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        body.send_keys(Keys.ESCAPE)
        time.sleep(0.12)
        body.send_keys(Keys.ESCAPE)
    except Exception:
        pass
    time.sleep(0.12)

def abrir_gasto_confirmado(driver, txid: int, desc_tabela: str, valor_linha: str, timeout: int = TIMEOUT, tentativas: int = 3) -> Dict[str, str]:
    last_err: Optional[Exception] = None

    for attempt in range(1, tentativas + 1):
        try:
            fechar_formulario_aberto(driver)

            row_xpath = f"//tr[@*[name()='wire:key']='row-{txid}']"
            row = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.XPATH, row_xpath))
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", row)
            time.sleep(0.15)

            open_transaction_dropdown_action(driver, txid, "gasto")
            time.sleep(0.6)

            debug_estado_formulario(driver, txid)

            wait_formulario_detalhes(driver, timeout=timeout)
            time.sleep(0.25)

            if not contexto_txid_confere(driver, txid):
                log(f"[WARN] openedTransaction ainda nao confirmou txid={txid}; seguindo com validacao por descricao/valor.")

            detalhes, valor_form = ler_form_detalhes_e_valor(driver)

            if not detalhes:
                # se o formulário abrir vazio, usa a descrição da linha como contexto inicial
                detalhes = (desc_tabela or "").strip()

            if not valor_form:
                valor_form = (valor_linha or "").strip()

            if not detalhes:
                # fallback extra: tenta achar algum texto do formulário aberto
                try:
                    painel = driver.find_element(By.XPATH, "//button[contains(@wire:click,'salvarGasto')]/ancestor::*[self::div or self::form][1]")
                    painel_txt = (painel.text or "").strip()
                    detalhes = painel_txt[:200]
                except Exception:
                    pass

            if not contexto_form_confere(desc_tabela, detalhes):
                raise RuntimeError(f"contexto_descricao_divergente | tabela='{desc_tabela}' | form='{detalhes}'")

            if valor_linha and valor_form and _norm_money(valor_linha) != _norm_money(valor_form):
                raise RuntimeError(f"contexto_valor_divergente | tabela='{valor_linha}' | form='{valor_form}'")

            return {
                "detalhes": detalhes,
                "valor_form": valor_form,
                "attempt": str(attempt),
            }

        except Exception as e:
            last_err = e
            log(f"⚠️ Contexto do formulário não confirmou para txid={txid} (tentativa {attempt}/{tentativas}): {type(e).__name__}")

            try:
                detalhes_atual, valor_atual = ler_form_detalhes_e_valor(driver)
                log(f"🧾 Form(detalhes lido): {detalhes_atual[:180] if detalhes_atual else '[vazio]'}")
                log(f"💰 Form(valor lido): {valor_atual if valor_atual else '[vazio]'}")
            except Exception:
                pass

            fechar_formulario_aberto(driver)
            time.sleep(0.40 * attempt)

    raise last_err if last_err else TimeoutException("Falha ao confirmar contexto do formulário")


def novo_perfil_linha(txid: int, step: int) -> Dict[str, Any]:
    return {
        "txid": txid,
        "step": step,
        "abrir_form_total": 0.0,
        "match_regra_total": 0.0,
        "tipo_total": 0.0,
        "categoria_total": 0.0,
        "fornecedor_total": 0.0,
        "wrapper_lookup_total": 0.0,
        "forma_pgto_total": 0.0,
        "centro_total": 0.0,
        "preencher_total": 0.0,
        "salvar_total": 0.0,
        "linha_total": 0.0,
        "status": "",
        "rule_id": "",
    }

def capture_line_state(driver, *, txid: int, passo: int, desc_tabela: str = "", valor_linha: str = "", valores: Optional[Dict[str, Any]] = None, row_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    livewire_pack = get_livewire_state_pack(driver)
    return {
        "txid": txid,
        "passo": passo,
        "descricao": desc_tabela,
        "valor": valor_linha,
        "valores": copy.deepcopy(valores or {}),
        "row_data": copy.deepcopy(row_data or {}),
        "field_snapshot": get_form_field_snapshot(driver),
        "active_gasto_probe": get_active_gasto_probe(driver),
        "livewire_focus": _state_focus(livewire_pack),
        "consistency_report": build_consistency_report(driver, valores=valores or {}, livewire_pack=livewire_pack),
    }

def build_error_bundle(driver, tag: str, *, txid: int, passo: int, desc_tabela: str = "", valor_linha: str = "", valores: Optional[Dict[str, Any]] = None, row_data: Optional[Dict[str, Any]] = None, before_state: Optional[Dict[str, Any]] = None, after_state: Optional[Dict[str, Any]] = None, extra: Optional[Dict[str, Any]] = None) -> None:
    current_state = after_state or capture_line_state(
        driver,
        txid=txid,
        passo=passo,
        desc_tabela=desc_tabela,
        valor_linha=valor_linha,
        valores=valores,
        row_data=row_data,
    )
    context = {
        "txid": txid,
        "passo": passo,
        "descricao": desc_tabela,
        "valor": valor_linha,
        "valores": copy.deepcopy(valores or {}),
        "row_data": copy.deepcopy(row_data or {}),
        "line_context": {
            "txid": txid,
            "passo": passo,
            "descricao": desc_tabela,
            "valor": valor_linha,
        },
        "before_state": before_state or {},
        "after_state": current_state,
        "state_diff": diff_state(before_state or {}, current_state),
    }
    if extra:
        context.update(copy.deepcopy(extra))
    dump_active_gasto_diag(driver, tag, context)
    append_replay_case({
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "tag": tag,
        "txid": txid,
        "passo": passo,
        "descricao": desc_tabela,
        "valor": valor_linha,
        "valores": copy.deepcopy(valores or {}),
        "row_data": copy.deepcopy(row_data or {}),
        "before_state": before_state or {},
        "after_state": current_state,
        "state_diff": diff_state(before_state or {}, current_state),
    })

def log_resumo_linha(perfil: Dict[str, Any]) -> None:
    log("📊 RESUMO DA LINHA")
    for key, label in [
        ("abrir_form_total", "Abrir/confirmar form"),
        ("match_regra_total", "Match regra"),
        ("tipo_total", "Tipo"),
        ("categoria_total", "Categoria"),
        ("fornecedor_total", "Fornecedor"),
        ("wrapper_lookup_total", "Localizar wrappers"),
        ("forma_pgto_total", "Forma pgto"),
        ("centro_total", "Centro"),
        ("preencher_total", "Preencher total"),
        ("salvar_total", "Salvar"),
        ("linha_total", "Linha total"),
    ]:
        val = float(perfil.get(key) or 0.0)
        log(f"   ⏱️ {label}: {val:.2f}s")
    if perfil.get("status"):
        log(f"   📌 Status: {perfil['status']}")
    if perfil.get("rule_id"):
        log(f"   🧩 Regra: {perfil['rule_id']}")


# =========================
# VSComp (Virtual Select)
# =========================

def _get_visible_vscomp_wrappers(driver):
    try:
        wrappers = driver.find_elements(By.CSS_SELECTOR, "div.vscomp-ele-wrapper")
        return [w for w in wrappers if w.is_displayed()]
    except Exception:
        return []

def _get_active_gasto_root(driver):
    try:
        return driver.execute_script(
            """
            const isVisible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const hasSalvarGasto = (el) => {
              const wc = (el && el.getAttribute) ? (el.getAttribute('wire:click') || '') : '';
              return wc.includes('salvarGasto');
            };
            const scoreNode = (node) => {
              if (!node) return 0;
              let score = 0;
              if (node.querySelector('#selectTipoGasto')) score += 1;
              if (node.querySelector('[id="categorias"], [id="categoria"]')) score += 1;
              if (node.querySelector('[id="fornecedores"], [id="fornecedor"]')) score += 1;
              if (node.querySelector('#selectFormaPagamento, [id="formapagamento"], [id="forma_pagamento"]')) score += 1;
              if (node.querySelector('[id="selectCentro"], [id="centros"], [id="centro"]')) score += 1;
              if (node.querySelector('#detalhes, #detalhe')) score += 1;
              if (Array.from(node.querySelectorAll('button')).some(hasSalvarGasto)) score += 2;
              return score;
            };
            const getById = (id) => {
              const el = document.getElementById(id);
              return isVisible(el) ? el : null;
            };
            const salvar = Array.from(document.querySelectorAll('button')).find(el => isVisible(el) && hasSalvarGasto(el));
            const seeds = [
              getById('selectTipoGasto'),
              getById('detalhes'),
              getById('detalhe'),
              getById('searchDetalhes'),
              getById('searchValor'),
              salvar,
            ].filter(Boolean);

            if (!seeds.length) return null;

            let best = null;
            let bestScore = -1;
            for (const seed of seeds) {
              let cur = seed;
              while (cur && cur !== document.body) {
                const score = scoreNode(cur);
                if (score > bestScore) {
                  best = cur;
                  bestScore = score;
                }
                if (score >= 4) {
                  return cur;
                }
                cur = cur.parentElement;
              }
            }
            return bestScore >= 2 ? best : null;
            """
        )
    except Exception:
        return None

def get_active_gasto_probe(driver) -> Dict[str, Any]:
    try:
        probe = driver.execute_script(
            """
            const isVisible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const hasSalvarGasto = (el) => {
              const wc = (el && el.getAttribute) ? (el.getAttribute('wire:click') || '') : '';
              return wc.includes('salvarGasto');
            };
            const byId = (id) => {
              const el = document.getElementById(id);
              return !!(el && isVisible(el));
            };
            const salvar = Array.from(document.querySelectorAll('button')).filter(el => isVisible(el) && hasSalvarGasto(el)).length;
            const tipo = byId('selectTipoGasto');
            const detalhes = byId('detalhes') || byId('detalhe');
            const searchDetalhes = byId('searchDetalhes');
            const searchValor = byId('searchValor');
            const ids = ['categorias','categoria','fornecedores','fornecedor','formapagamento','forma_pagamento','selectFormaPagamento','selectCentro','centros','centro'];
            const visibleFieldIds = ids.filter(byId);

            return {
              tipo,
              detalhes,
              searchDetalhes,
              searchValor,
              salvarGastoVisibleCount: salvar,
              visibleFieldIds,
            };
            """
        )
        return probe if isinstance(probe, dict) else {}
    except Exception:
        return {}

def wait_active_gasto_root(driver, timeout: int = 8):
    end = time.time() + max(1, int(timeout))
    last = None
    while time.time() < end:
        last = _get_active_gasto_root(driver)
        if last is not None:
            return last
        time.sleep(0.20)
    return last

def _get_vscomp_wrapper_from_host(host):
    try:
        if host is None:
            return None
        if "vscomp-ele-wrapper" in ((host.get_attribute("class") or "").lower()):
            if host.is_displayed():
                return host
        wrappers = host.find_elements(By.CSS_SELECTOR, ".vscomp-ele-wrapper")
        for wrapper in wrappers:
            if wrapper.is_displayed():
                return wrapper
        sib = host.find_elements(By.XPATH, "./following-sibling::*[contains(@class,'vscomp-ele-wrapper')]")
        for wrapper in sib:
            if wrapper.is_displayed():
                return wrapper
        up = host.find_elements(By.XPATH, "./ancestor::*[1]//div[contains(@class,'vscomp-ele-wrapper')]")
        for wrapper in up:
            if wrapper.is_displayed():
                return wrapper
    except Exception:
        return None
    return None

def _find_visible_host_by_ids(scope, candidate_ids: List[str]):
    for field_id in candidate_ids:
        try:
            hosts = scope.find_elements(By.ID, field_id)
        except Exception:
            hosts = []
        for host in hosts:
            wrapper = _get_vscomp_wrapper_from_host(host)
            if wrapper is not None:
                return wrapper, field_id
    return None

def _wrapper_matches_field_ids(driver, wrapper, field_ids: List[str]) -> bool:
    if wrapper is None:
        return False
    ids = [str(x or "").strip() for x in (field_ids or []) if str(x or "").strip()]
    if not ids:
        return True
    try:
        ok = driver.execute_script(
            """
            const wrapper = arguments[0];
            const ids = arguments[1] || [];
            if (!wrapper) return false;
            const hasAnyId = (node) => {
              if (!node || !node.querySelector) return false;
              for (const fid of ids) {
                if (node.querySelector('[id="' + fid + '"]')) return true;
              }
              return false;
            };
            if (hasAnyId(wrapper)) return true;
            let cur = wrapper.parentElement;
            for (let i = 0; i < 6 && cur; i += 1, cur = cur.parentElement) {
              if (hasAnyId(cur)) return true;
            }
            return false;
            """,
            wrapper,
            ids,
        )
        return bool(ok)
    except Exception:
        return False

def get_form_field_snapshot(driver) -> Dict[str, Any]:
    try:
        snap = driver.execute_script(
            """
            const isVisible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const hasSalvarGasto = (el) => {
              const wc = (el && el.getAttribute) ? (el.getAttribute('wire:click') || '') : '';
              return wc.includes('salvarGasto');
            };
            const scoreNode = (node) => {
              if (!node) return 0;
              let score = 0;
              if (node.querySelector('#selectTipoGasto')) score += 1;
              if (node.querySelector('[id="categorias"], [id="categoria"]')) score += 1;
              if (node.querySelector('[id="fornecedores"], [id="fornecedor"]')) score += 1;
              if (node.querySelector('#selectFormaPagamento, [id="formapagamento"], [id="forma_pagamento"]')) score += 1;
              if (node.querySelector('[id="selectCentro"], [id="centros"], [id="centro"]')) score += 1;
              if (node.querySelector('#detalhes, #detalhe')) score += 1;
              if (Array.from(node.querySelectorAll('button')).some(hasSalvarGasto)) score += 2;
              return score;
            };
            const getByIdVisible = (id) => {
              const el = document.getElementById(id);
              return isVisible(el) ? el : null;
            };
            const findRoot = () => {
              const salvar = Array.from(document.querySelectorAll('button')).find(el => isVisible(el) && hasSalvarGasto(el));
              const seeds = [
                getByIdVisible('selectTipoGasto'),
                getByIdVisible('detalhes'),
                getByIdVisible('detalhe'),
                getByIdVisible('searchDetalhes'),
                getByIdVisible('searchValor'),
                salvar,
              ].filter(Boolean);
              let best = null;
              let bestScore = -1;
              for (const seed of seeds) {
                let cur = seed;
                while (cur && cur !== document.body) {
                  const score = scoreNode(cur);
                  if (score > bestScore) {
                    best = cur;
                    bestScore = score;
                  }
                  if (score >= 4) {
                    return cur;
                  }
                  cur = cur.parentElement;
                }
              }
              return bestScore >= 2 ? best : document;
            };

            const root = findRoot();

            const readField = (ids, labels) => {
              for (const fieldId of ids || []) {
                const hosts = Array.from(root.querySelectorAll('[id="' + fieldId + '"]'));
                for (const host of hosts) {
                  if (!host) continue;

                  const select = host.matches('select') ? host : host.querySelector('select');
                  if (select) {
                    const opt = select.options && select.selectedIndex >= 0 ? select.options[select.selectedIndex] : null;
                    return {
                      by: 'id',
                      fieldId,
                      tag: 'select',
                      value: (select.value || '').trim(),
                      text: (opt && (opt.text || '').trim()) || '',
                    };
                  }

                  const input = host.matches('input,textarea') ? host : host.querySelector('input,textarea');
                  if (input) {
                    return {
                      by: 'id',
                      fieldId,
                      tag: input.tagName.toLowerCase(),
                      value: (input.value || '').trim(),
                      text: (input.value || '').trim(),
                    };
                  }

                  const button = host.matches('button') ? host : host.querySelector('button');
                  if (button && isVisible(button)) {
                    return {
                      by: 'id',
                      fieldId,
                      tag: 'button',
                      value: '',
                      text: (button.innerText || button.textContent || '').replace(/\\s+/g, ' ').trim(),
                    };
                  }

                  const wrapper = (
                    (host.classList && host.classList.contains('vscomp-ele-wrapper') ? host : null) ||
                    host.querySelector('.vscomp-ele-wrapper') ||
                    (host.closest ? host.closest('.vscomp-ele-wrapper') : null) ||
                    host.parentElement?.querySelector('.vscomp-ele-wrapper')
                  );
                  const vsValue = (wrapper && wrapper.querySelector('.vscomp-value')) || host.querySelector('.vscomp-value');
                  const vsHidden = (wrapper && wrapper.querySelector('.vscomp-hidden-input')) || host.querySelector('.vscomp-hidden-input');
                  if (vsValue && isVisible(vsValue)) {
                    return {
                      by: 'id',
                      fieldId,
                      tag: 'vscomp',
                      value: ((vsHidden && vsHidden.value) || '').trim(),
                      text: (vsValue.innerText || vsValue.textContent || '').trim(),
                    };
                  }

                  const tsItem = host.querySelector('.ts-control .item');
                  if (tsItem && isVisible(tsItem)) {
                    return {
                      by: 'id',
                      fieldId,
                      tag: 'tomselect',
                      value: '',
                      text: (tsItem.innerText || tsItem.textContent || '').trim(),
                    };
                  }
                }
              }

              const labelNodes = Array.from(root.querySelectorAll('label, span, div, p, strong, b'))
                .filter(isVisible);
              for (const lb of labels || []) {
                const anchor = labelNodes.find(el => ((el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim()).includes(lb));
                if (!anchor) continue;
                let box = anchor.parentElement;
                for (let i = 0; i < 4 && box; i += 1, box = box.parentElement) {
                  if (!box) break;
                  const select = box.querySelector('select');
                  if (select && isVisible(select)) {
                    const opt = select.options && select.selectedIndex >= 0 ? select.options[select.selectedIndex] : null;
                    return {
                      by: 'label',
                      label: lb,
                      tag: 'select',
                      value: (select.value || '').trim(),
                      text: (opt && (opt.text || '').trim()) || '',
                    };
                  }
                  const vsValue = box.querySelector('.vscomp-value');
                  const vsHidden = box.querySelector('.vscomp-hidden-input');
                  if (vsValue && isVisible(vsValue)) {
                    return {
                      by: 'label',
                      label: lb,
                      tag: 'vscomp',
                      value: ((vsHidden && vsHidden.value) || '').trim(),
                      text: (vsValue.innerText || vsValue.textContent || '').trim(),
                    };
                  }
                  const tsItem = box.querySelector('.ts-control .item');
                  if (tsItem && isVisible(tsItem)) {
                    return {
                      by: 'label',
                      label: lb,
                      tag: 'tomselect',
                      value: '',
                      text: (tsItem.innerText || tsItem.textContent || '').trim(),
                    };
                  }
                }
              }
              return {};
            };

            try {
              return {
                tipo: readField(['selectTipoGasto'], ['Tipo de Gasto', 'Tipo de gasto']),
                categoria: readField(['categorias', 'categoria'], ['Categoria']),
                fornecedor: readField(['fornecedores', 'fornecedor'], ['Fornecedor']),
                forma_pgto: readField(['selectFormaPagamento', 'formapagamento', 'forma_pagamento'], ['Forma de Pagamento', 'Forma de pagamento']),
                centro: readField(['selectCentro', 'centros', 'centro'], ['Centro de Custo', 'Centro de custo']),
                detalhes: readField(['detalhes', 'detalhe'], ['Detalhes']),
              };
            } catch (e) {
              return { __error: String(e) };
            }
            """,
        )
        return snap if isinstance(snap, dict) else {}
    except Exception:
        return {}

def dump_active_gasto_diag(driver, tag: str, extra: Optional[Dict[str, Any]] = None) -> None:
    livewire_pack = get_livewire_state_pack(driver)
    context = {
        "current_url": get_current_url_safe(driver),
        "active_gasto_probe": get_active_gasto_probe(driver),
        "field_snapshot": get_form_field_snapshot(driver),
        "livewire_active": get_active_gasto_livewire_snapshot(driver),
        "ofxreview_snapshot": get_ofxreview_livewire_snapshot(driver),
        "livewire_pack": livewire_pack,
        "livewire_focus": _state_focus(livewire_pack),
    }
    if extra:
        context.update(extra)
    context["consistency_report"] = build_consistency_report(driver, valores=context.get("valores") or {}, livewire_pack=livewire_pack)
    base = dump_diag(driver, tag, context)
    timeline_event("diagnostic_dumped", tag=tag, base=base, txid=(context.get("txid") or (context.get("line_context") or {}).get("txid")))

def assert_field_visible_selected(driver, field_key: str, required: bool = True) -> Dict[str, Any]:
    snap = get_form_field_snapshot(driver)
    field = (snap.get(field_key) or {}) if isinstance(snap, dict) else {}

    text = str(field.get("text") or "").strip()
    value = str(field.get("value") or "").strip()

    # textos que NÃO contam como seleção real
    placeholders = {
        "",
        "Selecione",
        "Selecionar",
        "Escolha",
        "Tipo de gasto",
        "Categoria",
        "Fornecedor",
        "Forma de pagamento",
        "Centro de custo",
        "Centro",
    }

    # normalização simples para comparação
    text_norm = " ".join(text.split()).strip().lower()
    placeholders_norm = {" ".join(p.split()).strip().lower() for p in placeholders}

    # é válido se houver value real
    has_real_value = bool(value)

    # ou texto real que não seja placeholder
    has_real_text = bool(text_norm) and text_norm not in placeholders_norm

    if required and not (has_real_value or has_real_text):
        raise TimeoutException(
            f"Campo visível não refletiu seleção real: {field_key} | snapshot={field}"
        )

    return field

def assert_field_matches_expected_id(driver, field_key: str, expected_id: str, required: bool = True) -> Dict[str, Any]:
    field = assert_field_visible_selected(driver, field_key, required=required)
    exp = str(expected_id or "").strip()
    got = str(field.get("value") or "").strip()
    if exp and got and got != exp:
        raise TimeoutException(
            f"Campo {field_key} divergiu do ID esperado: esperado={exp} obtido={got} | snapshot={field}"
        )
    return field

def log_field_state(driver, field_key: str, label: str) -> None:
    snap = get_form_field_snapshot(driver)
    field = (snap.get(field_key) or {}) if isinstance(snap, dict) else {}
    tag = str(field.get("tag") or "").strip()
    value = str(field.get("value") or "").strip()
    text = str(field.get("text") or "").strip()
    log(f"👁️ {label} -> tag={tag or '-'} | value={value or '-'} | text={text or '-'}")

def try_set_field_by_candidate_ids(driver, field_ids: List[str], value: str, timeout: int = 15) -> str:
    result = driver.execute_script(
        """
        const ids = arguments[0] || [];
        const rawValue = String(arguments[1] ?? '').trim();
        const candidates = [rawValue];
        if (/^\\d+$/.test(rawValue)) candidates.push(rawValue + '.0');
        const isVisible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);

        for (const fieldId of ids) {
          const host = document.getElementById(fieldId);
          if (!host || !isVisible(host)) continue;

          const select = host.matches('select') ? host : host.querySelector('select');
          const ts = (host.tomselect || (select && select.tomselect) || null);

          if (ts) {
            for (const cand of candidates) {
              try {
                ts.setValue(cand, true);
                if (select) {
                  select.dispatchEvent(new Event('input', { bubbles: true }));
                  select.dispatchEvent(new Event('change', { bubbles: true }));
                }
                return { ok: true, mode: 'tomselect', fieldId, value: cand };
              } catch (e) {}
            }
          }

          if (select) {
            for (const cand of candidates) {
              const exists = Array.from(select.options || []).some(o => String(o.value || '').trim() === cand);
              if (!exists) continue;
              select.value = cand;
              select.dispatchEvent(new Event('input', { bubbles: true }));
              select.dispatchEvent(new Event('change', { bubbles: true }));
              return { ok: true, mode: 'native-select', fieldId, value: cand };
            }
          }
        }
        return { ok: false };
        """,
        field_ids,
        str(value or "").strip(),
    )
    if result and result.get("ok"):
        return str(result.get("mode") or "unknown")
    raise TimeoutException(f"Falha ao setar campo por ids candidatos={field_ids} value={value}")

def _find_wrapper_fast_by_labels(driver, labels: List[str], root=None):
    # Busca sem waits longos, limitada ao formulário aberto
    search_xpaths = []
    for lb in labels:
        search_xpaths.extend([
            f".//label[normalize-space()='{lb}']",
            f".//label[contains(normalize-space(), '{lb}')]",
            f".//span[normalize-space()='{lb}']",
            f".//span[contains(normalize-space(), '{lb}')]",
            f".//*[self::div or self::p or self::strong or self::b][normalize-space()='{lb}']",
            f".//*[self::div or self::p or self::strong or self::b][contains(normalize-space(), '{lb}')]",
        ])

    scope = root or driver
    for xp in search_xpaths:
        try:
            anchors = scope.find_elements(By.XPATH, xp)
        except Exception:
            anchors = []
        for anchor in anchors:
            try:
                if not anchor.is_displayed():
                    continue
            except Exception:
                continue
            for up in [".", "..", "../..", "../../..", "../../../.."]:
                try:
                    container = anchor.find_element(By.XPATH, f"{up}//div[contains(@class,'vscomp-ele-wrapper')][1]")
                    if container.is_displayed():
                        return container
                except Exception:
                    pass
            try:
                container = anchor.find_element(By.XPATH, ".//following::div[contains(@class,'vscomp-ele-wrapper')][1]")
                if container.is_displayed():
                    return container
            except Exception:
                pass
    return None

def _get_wrapper_by_key(driver, label_key: str, timeout_each: int = 1):
    root = wait_active_gasto_root(driver, timeout=max(2, int(timeout_each)))
    field_ids = VS_FIELD_ID_KEYS.get(label_key, [])
    if root is not None and field_ids:
        found = _find_visible_host_by_ids(root, field_ids)
        if found is not None:
            wrapper, field_id = found
            return wrapper, f"root-id:{field_id}"

    labels = WRAPPER_LABEL_KEYS.get(label_key, [])
    fast = _find_wrapper_fast_by_labels(driver, labels, root=root)
    if fast is not None and _wrapper_matches_field_ids(driver, fast, field_ids):
        return fast, f"root-fast:{label_key}"

    wrapper, src = _get_wrapper_any_label(driver, labels, timeout_each=timeout_each)
    if wrapper is not None and field_ids and not _wrapper_matches_field_ids(driver, wrapper, field_ids):
        wrapper = None
    return wrapper, src

def get_vscomp_container_by_label_text(driver, label_text: str, timeout: int = 20):
    if not label_text or not label_text.strip():
        raise ValueError("label_text vazio")

    wait = WebDriverWait(driver, timeout)
    xpaths = [
        f"//label[normalize-space()='{label_text}']",
        f"//label[contains(normalize-space(), '{label_text}')]",
        f"//span[normalize-space()='{label_text}']",
        f"//span[contains(normalize-space(), '{label_text}')]",
        f"//*[self::div or self::p or self::strong or self::b][normalize-space()='{label_text}']",
        f"//*[self::div or self::p or self::strong or self::b][contains(normalize-space(), '{label_text}')]",
    ]

    anchor = None
    last_err = None
    for xp in xpaths:
        try:
            anchor = wait.until(EC.presence_of_element_located((By.XPATH, xp)))
            break
        except Exception as e:
            last_err = e

    if anchor is None:
        raise TimeoutException(f"Não encontrei label/span/texto para: '{label_text}'") from last_err

    for up in [".", "..", "../..", "../../..", "../../../.."]:
        try:
            container = anchor.find_element(By.XPATH, f"{up}//div[contains(@class,'vscomp-ele-wrapper')][1]")
            return container
        except Exception:
            pass

    return anchor.find_element(By.XPATH, ".//following::div[contains(@class,'vscomp-ele-wrapper')][1]")

def _get_visible_vscomp_dropbox(driver, timeout: int = 10):
    """
    VSComp cria um container do dropdown FORA do wrapper:
      <div class="vscomp-dropbox-container"> ... options ... </div>
    Aqui pegamos o que estiver visível (display != none).
    """
    def _visible(el):
        try:
            return el.is_displayed()
        except Exception:
            return False

    end = time.time() + timeout
    last = None
    while time.time() < end:
        boxes = driver.find_elements(By.CSS_SELECTOR, "div.vscomp-dropbox-container")
        vis = [b for b in boxes if _visible(b)]
        if vis:
            # normalmente só tem 1 visível
            return vis[-1]
        last = boxes
        time.sleep(0.15)

    raise TimeoutException("Não encontrei vscomp-dropbox visível (dropdown não abriu).")

def open_vscomp(driver, wrapper, timeout: int = 10):
    close_overlays(driver)
    toggle = wrapper.find_element(By.CSS_SELECTOR, ".vscomp-toggle-button")
    safe_click(driver, toggle)
    # espera aparecer um dropbox visível
    _get_visible_vscomp_dropbox(driver, timeout=timeout)

def _close_vscomp(driver):
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        pass
    time.sleep(0.2)


def close_vscomp(driver, wrapper=None):
    """
    Alias para compatibilidade com versões antigas do script.
    Fecha o dropdown do VSComp via ESC.
    """
    _close_vscomp(driver)


def _set_select_via_js(driver, wrapper, value: str) -> bool:
    # tenta setar direto no <select> interno (quando existir)
    try:
        select = wrapper.find_element(By.TAG_NAME, "select")
        # valida se o value existe nas options do select
        exists = driver.execute_script(
            "return Array.from(arguments[0].options||[]).some(o => (o.value||'')==arguments[1]);",
            select, value
        )
        if not exists:
            return False

        driver.execute_script(
            """
            arguments[0].value = arguments[1];
            arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
            arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
            """,
            select,
            value,
        )
        return True
    except Exception:
        return False


def select_vscomp_value(driver, wrapper, value, timeout=20):
    value = str(value).strip()
    candidates = [value]
    # alguns ids aparecem como "13145.0"
    if value.isdigit():
        candidates.append(f"{value}.0")

    # 1) tenta via JS no <select> (mais rápido)
    for v in candidates:
        if _set_select_via_js(driver, wrapper, v):
            return

    # 2) abre o VSComp (às vezes só depois de abrir as opções aparecerem)
    open_vscomp(driver, wrapper, timeout=timeout)
    time.sleep(0.12)

    # 2.1) tenta de novo via JS (safe e rápido)
    for v in candidates:
        if _set_select_via_js(driver, wrapper, v):
            close_vscomp(driver)
            return

    # 3) acha o container de opções VISÍVEL (dropdown aberto)
    end_time = time.time() + timeout
    last_seen = {"values": [], "texts": []}

    while time.time() < end_time:
        try:
            containers = driver.find_elements(By.CSS_SELECTOR, ".vscomp-options-container")
            containers = [c for c in containers if c.is_displayed()]
            if not containers:
                time.sleep(0.08)
                continue

            cont = containers[-1]

            # tenta encontrar a opção por data-value (visível)
            found = None
            for v in candidates:
                opts = cont.find_elements(By.CSS_SELECTOR, f".vscomp-option[data-value='{v}']")
                opts = [o for o in opts if o.is_displayed()]
                if opts:
                    found = opts[0]
                    break

            # guarda amostra p/ debug
            try:
                vis_opts = cont.find_elements(By.CSS_SELECTOR, ".vscomp-option")
                vis_opts = [o for o in vis_opts if o.is_displayed()]
                last_seen["values"] = [o.get_attribute("data-value") for o in vis_opts[:12]]
                last_seen["texts"] = [(o.text or "").strip() for o in vis_opts[:12]]
            except Exception:
                pass

            if found:
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", found)
                except Exception:
                    pass
                try:
                    driver.execute_script("arguments[0].click();", found)
                except Exception:
                    try:
                        found.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", found)

                time.sleep(0.08)
                close_vscomp(driver)
                return

            # scroll incremental (necessário para carregar listas grandes)
            try:
                driver.execute_script("arguments[0].scrollTop = arguments[0].scrollTop + 260;", cont)
            except Exception:
                pass

            time.sleep(0.10)
        except Exception:
            time.sleep(0.10)

    close_vscomp(driver)
    raise TimeoutException(
        f"Não encontrei opção data-value={value} (tentou {candidates}) em {timeout}s | "
        f"amostra_values={last_seen['values']} | amostra_texts={last_seen['texts']}"
    )


def get_vscomp_selected_text(wrapper) -> str:
    try:
        return (wrapper.find_element(By.CSS_SELECTOR, ".vscomp-value").text or "").strip()
    except Exception:
        try:
            return (wrapper.text or "").strip()
        except Exception:
            return ""
        

TIPO_ID_TO_TEXTO = {
    "1": "Fixo",
    "2": "Variável",
    "3": "Impostos",
    "4": "Pessoal",
}

_TIPO_TEXTO_TO_ID = {normalizar_texto(v): k for k, v in TIPO_ID_TO_TEXTO.items()}
_FORMA_TEXTO_TO_ID = {
    "PIX": "1",
    "BOLETO": "2",
    "CARTAO": "3",
    "DINHEIRO": "4",
    "TRANSFERENCIA": "5",
    "CHEQUE": "6",
    "DEBITO AUTOMATICO": "7",
}

def _safe_load_yaml_rules(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        rules = data.get("rules", [])
        return rules if isinstance(rules, list) else []
    except Exception:
        return []

def _append_rule_to_yaml(path: str, rule: Dict[str, Any]) -> bool:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rules = _safe_load_yaml_rules(path)
    sig_new = (
        tuple(rule.get("when_all") or []),
        str((rule.get("set") or {}).get("tipo_id") or ""),
        str((rule.get("set") or {}).get("categoria_id") or ""),
        str((rule.get("set") or {}).get("fornecedor_id") or ""),
        str((rule.get("set") or {}).get("centro_id") or ""),
        str((rule.get("set") or {}).get("forma_pagamento") or ""),
    )
    for r in rules:
        sig_old = (
            tuple(r.get("when_all") or []),
            str((r.get("set") or {}).get("tipo_id") or ""),
            str((r.get("set") or {}).get("categoria_id") or ""),
            str((r.get("set") or {}).get("fornecedor_id") or ""),
            str((r.get("set") or {}).get("centro_id") or ""),
            str((r.get("set") or {}).get("forma_pagamento") or ""),
        )
        if sig_old == sig_new:
            return False
    rules.append(rule)
    with p.open("w", encoding="utf-8") as f:
        yaml.safe_dump({"rules": rules}, f, allow_unicode=True, sort_keys=False)
    return True

def _capturar_classificacao_manual(driver) -> Dict[str, str]:
    snap = get_form_field_snapshot(driver)
    tipo_txt = normalizar_texto((snap.get("tipo") or {}).get("text", ""))
    forma_txt = normalizar_texto((snap.get("forma_pgto") or {}).get("text", ""))
    return {
        "tipo_id": _TIPO_TEXTO_TO_ID.get(tipo_txt, ""),
        "categoria_id": str((snap.get("categoria") or {}).get("value") or "").strip(),
        "fornecedor_id": str((snap.get("fornecedor") or {}).get("value") or "").strip(),
        "centro_id": str((snap.get("centro") or {}).get("value") or "").strip(),
        "forma_pagamento": _FORMA_TEXTO_TO_ID.get(forma_txt, ""),
    }

def _montar_regra_aprendida(texto_regra: str, ids: Dict[str, str]) -> Optional[Dict[str, Any]]:
    if not all((ids.get("tipo_id"), ids.get("categoria_id"), ids.get("fornecedor_id"), ids.get("centro_id"), ids.get("forma_pagamento"))):
        return None
    tokens = tokens_para_regra(texto_regra or "")
    if len(tokens) < 2:
        return None
    rid = f"AUTO_LEARN_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    return {
        "id": rid,
        "when_all": tokens[:4],
        "set": {
            "tipo_id": ids["tipo_id"],
            "categoria_id": ids["categoria_id"],
            "fornecedor_id": ids["fornecedor_id"],
            "centro_id": ids["centro_id"],
            "forma_pagamento": ids["forma_pagamento"],
        },
    }

def get_vscomp_wrapper_by_field_id(driver, field_id: str, timeout: int = 10):
    """
    Localiza o host do campo VSComp pelo id fixo do campo e,
    dentro dele, pega o wrapper real do componente.
    
    Exemplo:
      field_id='categorias'
    """
    host = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.ID, field_id))
    )

    wrapper = host.find_element(By.CSS_SELECTOR, ".vscomp-ele-wrapper")
    return host, wrapper


def open_vscomp_by_field_id(driver, field_id: str, timeout: int = 10):
    """
    Abre um VSComp a partir do id fixo do campo.
    Retorna:
        host, wrapper
    """
    host, wrapper = get_vscomp_wrapper_by_field_id(driver, field_id, timeout=timeout)

    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});",
        host
    )
    time.sleep(0.12)

    toggle = wrapper.find_element(By.CSS_SELECTOR, ".vscomp-toggle-button")

    try:
        toggle.click()
    except Exception:
        driver.execute_script(
            "arguments[0].click();",
            toggle
        )

    def _dropdown_abriu(d):
        try:
            expanded = (wrapper.get_attribute("aria-expanded") or "").lower()
            if expanded == "true":
                return True

            boxes = d.find_elements(By.CSS_SELECTOR, ".vscomp-dropbox-container")
            return any(b.is_displayed() for b in boxes)

        except Exception:
            return False

    WebDriverWait(driver, timeout).until(_dropdown_abriu)

    return host, wrapper


def select_vscomp_value_by_field_id(driver, field_id: str, value: str, timeout: int = 15):
    """
    Seleciona uma opção do VSComp usando o id fixo do campo + data-value da opção
    e confirma que o valor foi realmente aplicado no host.
    """
    value = str(value).replace(".0", "").strip()

    host, wrapper = open_vscomp_by_field_id(driver, field_id, timeout=timeout)

    end_time = time.time() + timeout
    last_seen = []

    while time.time() < end_time:
        try:
            boxes = driver.find_elements(By.CSS_SELECTOR, ".vscomp-dropbox-container")
            visible_boxes = [b for b in boxes if b.is_displayed()]

            if visible_boxes:
                box = visible_boxes[-1]

                opts = box.find_elements(By.CSS_SELECTOR, f".vscomp-option[data-value='{value}']")
                opts = [o for o in opts if o.is_displayed()]

                if opts:
                    opt = opts[0]
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", opt)

                    try:
                        opt.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", opt)

                    time.sleep(0.20)

                    # força eventos no host para o componente reagir
                    try:
                        driver.execute_script(
                            """
                            const host = arguments[0];
                            const val = arguments[1];

                            try {
                                if (typeof host.setValue === 'function') {
                                    host.setValue(String(val));
                                } else if ('value' in host) {
                                    host.value = String(val);
                                }
                            } catch (e) {}

                            host.dispatchEvent(new Event('input', { bubbles: true }));
                            host.dispatchEvent(new Event('change', { bubbles: true }));
                            host.dispatchEvent(new Event('blur', { bubbles: true }));
                            """,
                            host,
                            value,
                        )
                    except Exception:
                        pass

                    # tenta fechar dropdown
                    try:
                        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                    except Exception:
                        pass

                    # confirmação real de aplicação
                    def _valor_aplicado(d):
                        try:
                            host_now, wrapper_now = get_vscomp_wrapper_by_field_id(d, field_id, timeout=3)

                            host_value = str(host_now.get_attribute("value") or "").strip()
                            
                            if host_value == value:
                                return True

                            # fallback: às vezes texto do wrapper reflete a escolha
                            selected_values = wrapper_now.find_elements(By.CSS_SELECTOR, ".vscomp-value")
                            for sv in selected_values:
                                txt = str(sv.text or "").strip()
                                tooltip = str(sv.get_attribute("data-tooltip") or "").strip()
                                if txt or tooltip:
                                    return True

                            return False
                        except Exception:
                            return False

                    WebDriverWait(driver, 4).until(_valor_aplicado)
                    return True

                # amostra para debug
                try:
                    amostra = box.find_elements(By.CSS_SELECTOR, ".vscomp-option")
                    last_seen = [
                        ((o.get_attribute("data-value") or "").strip(), (o.text or "").strip())
                        for o in amostra[:10]
                    ]
                except Exception:
                    pass

                # scroll na lista
                try:
                    scroll_area = box.find_element(By.CSS_SELECTOR, ".vscomp-options-container")
                    driver.execute_script(
                        "arguments[0].scrollTop = arguments[0].scrollTop + 260;",
                        scroll_area
                    )
                except Exception:
                    pass

            time.sleep(0.10)

        except Exception:
            time.sleep(0.10)

    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        pass

    raise TimeoutException(
        f"Não encontrei/apliquei value={value} no campo '{field_id}' | amostra={last_seen}"
    )


def select_vscomp_value_by_candidate_ids(driver, field_ids: List[str], value: str, timeout: int = 15):
    last_err = None
    for field_id in field_ids:
        try:
            return select_vscomp_value_by_field_id(driver, field_id, value, timeout=timeout)
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    raise TimeoutException(f"Nenhum field_id candidato informado para value={value}")

def select_tipo_gasto_listbox(driver, tipo_id: str, timeout: int = 15) -> None:
    """
    Seleciona o Tipo de gasto num listbox (Alpine/Tailwind), ex:
    <button id="selectTipoGasto" aria-haspopup="listbox" aria-expanded="...">...</button>
    Opções aparecem como <ul role="listbox"> ... <li role="option">TEXTO</li>
    """
    tipo_id = str(tipo_id).replace(".0", "").strip()
    texto = TIPO_ID_TO_TEXTO.get(tipo_id, "").strip()
    if not texto:
        raise ValueError(f"tipo_id inválido/sem mapeamento: {tipo_id}")

    btn = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.ID, "selectTipoGasto"))
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
    safe_click(driver, btn)

    # espera abrir o listbox
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.XPATH, "//*[@role='listbox']"))
    )

    # clica na opção pelo texto
    opt_xpath = (
        "//*[@role='listbox']"
        f"//*[(@role='option' or self::li or self::button or self::div)"
        f" and normalize-space(.)='{texto}']"
    )
    opt = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.XPATH, opt_xpath))
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", opt)
    driver.execute_script("arguments[0].click();", opt)

    # confirma que fechou (opcional, mas deixa mais estável)
    WebDriverWait(driver, timeout).until(
        lambda d: (d.find_element(By.ID, "selectTipoGasto").get_attribute("aria-expanded") in (None, "false"))
    )


def selecionar_tipo_e_categoria(driver, tipo_wrapper, cat_wrapper, tipo_value: str, categoria_value: str, timeout: int = 12):
    """
    Seleciona Tipo e depois Categoria no VSComp sem esperar 60s.
    Estratégia:
      1) seleciona tipo
      2) espera curta pela categoria ficar "pronta" (habilitada / opções disponíveis)
      3) seleciona categoria com retry rápido
    """
    # 1) seleciona tipo (normal)
    select_vscomp_value(driver, tipo_wrapper, tipo_value, timeout=timeout)

    # 2) espera categoria "pronta": tenta 3 sinais rápidos (sem depender de texto mudar)
    def _categoria_pronta(_d):
        try:
            # sinal A: wrapper ainda existe
            if cat_wrapper is None:
                return False

            # sinal B: botão do vscomp da categoria está habilitado/clicável
            btn = cat_wrapper.find_element(By.CSS_SELECTOR, "button")
            if not btn.is_enabled():
                return False

            # sinal C: ao abrir, aparece lista/opções (muitos VSComp geram div[role=listbox] / li[role=option])
            # Tentativa leve: abre e fecha rápido
            try:
                btn.click()
            except Exception:
                driver.execute_script("arguments[0].click();", btn)
            time.sleep(0.15)

            opts = driver.find_elements(By.CSS_SELECTOR, "[role='option'], li[role='option']")
            # fecha clicando fora (evita menu preso)
            driver.execute_script("document.body.click();")
            return len(opts) > 0
        except Exception:
            return False

    try:
        WebDriverWait(driver, min(timeout, 8)).until(_categoria_pronta)
    except TimeoutException:
        # não trava: só segue e tenta selecionar mesmo assim (com retry)
        log("⚠️ Categoria demorou para ficar pronta; tentando selecionar mesmo assim...")

    # 3) selecionar categoria com retry rápido
    last = None
    for attempt in range(1, 3):
        try:
            select_vscomp_value(driver, cat_wrapper, categoria_value, timeout=min(timeout, 8))
            return
        except (TimeoutException, StaleElementReferenceException) as e:
            last = e
            log(f"⚠️ Falha selecionando Categoria (tentativa {attempt}/2); retry rápido...")
            time.sleep(0.25 * attempt)

    raise TimeoutException(f"Falha ao selecionar categoria após retries: {last}")


def _get_wrapper_any_label(driver, labels: List[str], timeout_each: int = 3):
    last_err = None
    for lb in labels:
        try:
            wrapper = get_vscomp_container_by_label_text(driver, lb, timeout=timeout_each)
            return wrapper, lb
        except Exception as e:
            last_err = e
    return None, last_err

# =========================
# Salvar Livewire
# =========================

def wait_btn_salvar_habilitar(driver, timeout: int = 25):
    def _find_visible_btn(d):
        for el in d.find_elements(*BTN_SALVAR_LIVEWIRE):
            try:
                if el.is_displayed():
                    return el
            except Exception:
                pass
        return False

    def _enabled(d):
        try:
            btn = _find_visible_btn(d)
            if not btn:
                return False
            disabled = btn.get_attribute("disabled")
            if disabled not in (None, "", "false"):
                return False
            return btn
        except StaleElementReferenceException:
            return False

    return WebDriverWait(driver, timeout).until(_enabled)

def is_btn_salvar_habilitado(driver) -> bool:
    try:
        for el in driver.find_elements(*BTN_SALVAR_LIVEWIRE):
            try:
                if not el.is_displayed():
                    continue
                disabled = el.get_attribute("disabled")
                return disabled in (None, "", "false")
            except Exception:
                continue
    except Exception:
        return False
    return False


def centro_de_custo_confirmado(driver, centro_id: str) -> bool:
    centro_id = str(centro_id or "").strip()
    if not centro_id:
        return True

    try:
        snapshot_campos = get_form_field_snapshot(driver)
        campo_centro = (snapshot_campos.get("centro") or {}) if isinstance(snapshot_campos, dict) else {}

        valor_visual = str(campo_centro.get("value") or "").strip()
        texto_visual = str(campo_centro.get("text") or "").strip()

        # 1) confirmação visual direta pelo value
        if valor_visual == centro_id:
            return True

        # 2) confirmação visual mínima por texto não vazio pode ser usada só para log,
        #    mas NÃO é suficiente para aprovar sozinho sem o ID correto
        if texto_visual:
            log(f"ℹ️ Centro com texto visível, mas sem confirmação por ID. text='{texto_visual}' esperado_id='{centro_id}'")

        # 3) confirmação Livewire
        livewire = get_active_gasto_livewire_snapshot(driver)
        dados = livewire.get("data", {}) if isinstance(livewire, dict) else {}

        candidatos = [
            dados.get("centro"),
            dados.get("bulkCentro"),
            dados.get("centro_id"),
        ]

        form_data = dados.get("form") if isinstance(dados.get("form"), dict) else {}
        gasto_form = dados.get("gastoForm") if isinstance(dados.get("gastoForm"), dict) else {}

        candidatos.extend([
            form_data.get("centro"),
            form_data.get("centro_id"),
            gasto_form.get("centro"),
            gasto_form.get("centro_id"),
        ])

        candidatos_normalizados = {
            str(x).strip()
            for x in candidatos
            if x not in (None, "", "None")
        }

        if centro_id in candidatos_normalizados:
            return True

        log(
            f"❌ Centro de custo NÃO confirmado | esperado={centro_id} "
            f"| visual_value='{valor_visual}' | visual_text='{texto_visual}' "
            f"| livewire={sorted(candidatos_normalizados)}"
        )
        return False

    except Exception as e:
        log(f"❌ Falha ao confirmar centro de custo: {type(e).__name__} | {e}")
        return False
    

def fornecedor_confirmado(driver, fornecedor_id: str) -> bool:
    fornecedor_id = str(fornecedor_id or "").strip()
    if not fornecedor_id:
        return True

    try:
        snapshot_campos = get_form_field_snapshot(driver)
        campo_fornecedor = (snapshot_campos.get("fornecedor") or {}) if isinstance(snapshot_campos, dict) else {}

        valor_visual = str(campo_fornecedor.get("value") or "").strip()
        texto_visual = str(campo_fornecedor.get("text") or "").strip()

        # 1) confirmação visual direta
        if valor_visual == fornecedor_id:
            return True

        # 2) confirmação Livewire
        livewire = get_active_gasto_livewire_snapshot(driver)
        dados = livewire.get("data", {}) if isinstance(livewire, dict) else {}

        candidatos = [
            dados.get("fornecedor"),
            dados.get("bulkFornecedor"),
            dados.get("fornecedor_id"),
            dados.get("bulkFornecedorId"),
        ]

        form_data = dados.get("form") if isinstance(dados.get("form"), dict) else {}
        gasto_form = dados.get("gastoForm") if isinstance(dados.get("gastoForm"), dict) else {}

        candidatos.extend([
            form_data.get("fornecedor"),
            form_data.get("fornecedor_id"),
            gasto_form.get("fornecedor"),
            gasto_form.get("fornecedor_id"),
        ])

        candidatos_normalizados = {
            str(x).strip()
            for x in candidatos
            if x not in (None, "", "None")
        }

        if fornecedor_id in candidatos_normalizados:
            return True

        log(
            f"❌ Fornecedor NÃO confirmado | esperado={fornecedor_id} "
            f"| visual_value='{valor_visual}' | visual_text='{texto_visual}' "
            f"| livewire={sorted(candidatos_normalizados)}"
        )
        return False

    except Exception as e:
        log(f"❌ Falha ao confirmar fornecedor: {type(e).__name__} | {e}")
        return False
    

def sync_gasto_livewire_state(driver, valores: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    log("🔥 NOVA sync_gasto_livewire_state executada")
    valores = valores or {}
    before_pack = get_livewire_state_pack(driver)

    payload = {
        "tipo_id": str(valores.get("tipo_id") or "").strip(),
        "categoria_id": str(valores.get("categoria_id") or "").strip(),
        "fornecedor_id": str(valores.get("fornecedor_id") or "").strip(),
        "forma_pagamento": str(valores.get("forma_pagamento") or "").strip(),
        "centro_id": str(valores.get("centro_id") or "").strip(),
        "detalhe": str(valores.get("_detalhes") or "").strip(),
    }

    try:
        out = driver.execute_async_script(
            """
            const vals = arguments[0] || {};
            const done = arguments[arguments.length - 1];

            const asIntOrNull = (v) => {
                const s = String(v ?? '').trim();
                if (!s) return null;
                const n = Number(s);
                return Number.isFinite(n) ? n : s;
            };

            const isVisible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);

            const findComp = () => {
                const all = (window.Livewire && typeof window.Livewire.all === 'function')
                    ? window.Livewire.all()
                    : [];

                // tenta achar pelo botão salvar visível
                const btn = Array.from(document.querySelectorAll("button")).find(el => {
                    const wc = (el.getAttribute && el.getAttribute("wire:click")) || "";
                    return isVisible(el) && wc.includes("salvarGasto");
                });

                let wireId = "";
                if (btn) {
                    let cur = btn;
                    while (cur && cur !== document.body) {
                        if (cur.getAttribute && cur.getAttribute("wire:id")) {
                            wireId = cur.getAttribute("wire:id") || "";
                            break;
                        }
                        cur = cur.parentElement;
                    }
                }

                let comp = null;

                if (wireId) {
                    comp = all.find(c =>
                        String(c?.id || '') === wireId ||
                        String(c?.snapshot?.memo?.id || '') === wireId
                    ) || null;
                }

                if (!comp) {
                    comp = all.find(c => {
                        const d = c?.snapshot?.data || {};
                        return d && Object.prototype.hasOwnProperty.call(d, "openedTransaction");
                    }) || null;
                }

                return { comp, wireId };
            };

            const { comp, wireId } = findComp();

            if (!comp || !comp.$wire || typeof comp.$wire.set !== "function") {
                done({
                    ok: false,
                    erro: "Componente Livewire não encontrado ou sem $wire.set",
                    wireId
                });
                return;
            }

            const setKeys = async (keys, value) => {
                if (value === null || value === undefined || value === "") return;
                for (const key of (keys || [])) {
                    try {
                        const result = comp.$wire.set(key, value);
                        if (result && typeof result.then === "function") {
                            await result;
                        }
                    } catch (e) {
                        // continua tentando as outras chaves
                    }
                }
            };

            const dispatchToHost = (id, value) => {
                if (value === null || value === undefined || value === "") return false;
                const host = document.getElementById(id);
                if (!host) return false;

                try {
                    if (typeof host.setValue === "function") {
                        host.setValue(String(value));
                    } else if ("value" in host) {
                        host.value = String(value);
                    }

                    host.dispatchEvent(new Event("input", { bubbles: true }));
                    host.dispatchEvent(new Event("change", { bubbles: true }));
                    host.dispatchEvent(new Event("blur", { bubbles: true }));
                    return true;
                } catch (e) {
                    return false;
                }
            };

            (async () => {
                // ajuda o DOM / componentes visuais
                dispatchToHost("categorias", vals.categoria_id);
                dispatchToHost("categoria", vals.categoria_id);

                dispatchToHost("fornecedores", vals.fornecedor_id);
                dispatchToHost("fornecedor", vals.fornecedor_id);

                dispatchToHost("selectCentro", vals.centro_id);
                dispatchToHost("centros", vals.centro_id);
                dispatchToHost("centro", vals.centro_id);

                dispatchToHost("selectFormaPagamento", vals.forma_pagamento);

                // sync do estado Livewire
                await setKeys(["bulkTipogasto", "bulkTipo", "bulkTipoGasto"], asIntOrNull(vals.tipo_id));
                await setKeys(["bulkCategoria", "bulkCategoriaId", "bulkcategoria"], asIntOrNull(vals.categoria_id));
                await setKeys(["bulkFornecedor", "bulkFornecedorId", "bulkfornecedor"], asIntOrNull(vals.fornecedor_id));
                await setKeys(["bulkFormaPagamento", "bulkFormaPagamentoId", "bulkformapagamento"], asIntOrNull(vals.forma_pagamento));
                await setKeys(["bulkCentro", "bulkCentroId", "bulkcentro"], (vals.centro_id || "").trim() || null);

                await setKeys(["tipogasto", "tipoGasto", "tipo_id", "tipoGastoId"], asIntOrNull(vals.tipo_id));
                await setKeys(["categoriaGasto", "categoria", "categoriaId", "categoria_id"], asIntOrNull(vals.categoria_id));
                await setKeys(["fornecedor", "fornecedorId", "fornecedor_id"], asIntOrNull(vals.fornecedor_id));
                await setKeys(["formaPagamento", "forma_pagamento", "formaPagamentoId"], asIntOrNull(vals.forma_pagamento));
                await setKeys(["centro", "centroId", "centro_id"], (vals.centro_id || "").trim() || null);

                await setKeys(["form.tipogasto", "form.tipoGasto", "form.tipo_id"], asIntOrNull(vals.tipo_id));
                await setKeys(["form.categoria", "form.categoria_id"], asIntOrNull(vals.categoria_id));
                await setKeys(["form.fornecedor", "form.fornecedor_id"], asIntOrNull(vals.fornecedor_id));
                await setKeys(["form.formapagamento", "form.formaPagamento"], asIntOrNull(vals.forma_pagamento));
                await setKeys(["form.centro", "form.centro_id", "form.centroId"], (vals.centro_id || "").trim() || null);

                await setKeys(["gastoForm.tipogasto", "gastoForm.tipoGasto", "gastoForm.tipo_id"], asIntOrNull(vals.tipo_id));
                await setKeys(["gastoForm.categoria", "gastoForm.categoria_id"], asIntOrNull(vals.categoria_id));
                await setKeys(["gastoForm.fornecedor", "gastoForm.fornecedor_id"], asIntOrNull(vals.fornecedor_id));
                await setKeys(["gastoForm.formapagamento", "gastoForm.formaPagamento"], asIntOrNull(vals.forma_pagamento));
                await setKeys(["gastoForm.centro", "gastoForm.centro_id", "gastoForm.centroId"], (vals.centro_id || "").trim() || null);

                if ((vals.detalhe || "").trim()) {
                    await setKeys(["detalhe", "detalhes"], String(vals.detalhe));
                    await setKeys(["form.detalhe", "form.detalhes"], String(vals.detalhe));
                    await setKeys(["gastoForm.detalhe", "gastoForm.detalhes"], String(vals.detalhe));
                }

                setTimeout(() => {
                    const data = comp?.snapshot?.data || {};
                    const btn = Array.from(document.querySelectorAll("button")).find(el => {
                        const wc = (el.getAttribute && el.getAttribute("wire:click")) || "";
                        return isVisible(el) && wc.includes("salvarGasto");
                    });

                    done({
                        ok: true,
                        wireId,
                        componentName: comp?.snapshot?.memo?.name || "",
                        buttonWireClick: btn ? (btn.getAttribute("wire:click") || "") : "",
                        disabledAttr: btn ? btn.getAttribute("disabled") : null,
                        disabledExpr: btn ? (btn.getAttribute(":disabled") || btn.getAttribute("x-bind:disabled") || "") : "",
                        livewire: {
                            bulkTipogasto: data.bulkTipogasto ?? data.bulkTipo ?? null,
                            bulkCategoria: data.bulkCategoria ?? null,
                            bulkFornecedor: data.bulkFornecedor ?? null,
                            bulkFormaPagamento: data.bulkFormaPagamento ?? null,
                            bulkCentro: data.bulkCentro ?? null,
                            tipogasto: data.tipogasto ?? null,
                            categoriaGasto: data.categoriaGasto ?? null,
                            fornecedor: data.fornecedor ?? null,
                            formaPagamento: data.formaPagamento ?? null,
                            centro: data.centro ?? null,
                            detalhe: data.detalhe ?? null,
                            form: data.form ?? null,
                            gastoForm: data.gastoForm ?? null,
                            openedTransaction: data.openedTransaction ?? null,
                            type: data.type ?? null,
                        }
                    });
                }, 500);
            })().catch((e) => {
                done({
                    ok: false,
                    erro: String(e),
                    wireId
                });
            });
            """,
            payload,
        )

        result = out if isinstance(out, dict) else {}
        after_pack = get_livewire_state_pack(driver)

        result["before_focus"] = _state_focus(before_pack)
        result["after_focus"] = _state_focus(after_pack)
        result["state_diff"] = diff_state(result["before_focus"], result["after_focus"])

        timeline_event(
            "livewire_sync",
            ok=result.get("ok"),
            valores=valores,
            wire_id=result.get("wireId"),
            state_diff=result.get("state_diff"),
        )

        log(
            f"🧪 Sync Livewire | ok={result.get('ok')} "
            f"| fornecedor={((result.get('livewire') or {}).get('fornecedor'))} "
            f"| bulkFornecedor={((result.get('livewire') or {}).get('bulkFornecedor'))} "
            f"| disabledAttr={result.get('disabledAttr')} "
            f"| disabledExpr={result.get('disabledExpr')}"
        )

        return result

    except Exception as e:
        result = {
            "ok": False,
            "erro": f"{type(e).__name__}: {e}",
            "before_focus": _state_focus(before_pack),
        }
        timeline_event("livewire_sync_erro", erro=result["erro"], valores=valores)
        log(f"❌ sync_gasto_livewire_state falhou: {result['erro']}")
        return result
    

def clicar_salvar_livewire(driver, timeout: int = 25, valores: Optional[Dict[str, str]] = None) -> bool:
    valores = valores or {}

    centro_id_esperado = str(valores.get("centro_id") or "").strip()
    fornecedor_id_esperado = str(valores.get("fornecedor_id") or "").strip()

    # trava fornecedor
    if fornecedor_id_esperado:
        if not fornecedor_confirmado(driver, fornecedor_id_esperado):
            log(f"❌ Salvar bloqueado na validação final: fornecedor não confirmado | esperado={fornecedor_id_esperado}")
            dump_active_gasto_diag(
                driver,
                "salvar_bloqueado_validacao_final_fornecedor",
                {
                    "valores": valores,
                    "fornecedor_id_esperado": fornecedor_id_esperado,
                    "motivo": "fornecedor_confirmado retornou False imediatamente antes do clique",
                },
            )
            return False

    # trava centro
    if centro_id_esperado:
        if not centro_de_custo_confirmado(driver, centro_id_esperado):
            log(f"❌ Salvar bloqueado: centro de custo não confirmado | esperado={centro_id_esperado}")
            dump_active_gasto_diag(
                driver,
                "salvar_bloqueado_centro_nao_confirmado",
                {
                    "valores": valores,
                    "centro_id_esperado": centro_id_esperado,
                    "motivo": "centro_de_custo_confirmado retornou False antes do clique em Salvar",
                },
            )
            return False

    # 2) Aguarda botão habilitar
    try:
        btn = wait_btn_salvar_habilitar(driver, timeout=timeout)
    except Exception:
        sync = sync_gasto_livewire_state(driver, valores=valores)
        log(f"⚠️ Salvar desabilitado; após sync Livewire: {sync}")

        # Revalida fornecedor após sync
        if fornecedor_id_esperado and not fornecedor_confirmado(driver, fornecedor_id_esperado):
            log(f"❌ Salvar bloqueado na tentativa {t}: fornecedor não confirmado | esperado={fornecedor_id_esperado}")
            dump_active_gasto_diag(
                driver,
                f"salvar_bloqueado_tentativa_{t}_fornecedor_nao_confirmado",
                {
                    "valores": valores,
                    "fornecedor_id_esperado": fornecedor_id_esperado,
                    "tentativa": t,
                },
            )
            return False

        # Revalida centro após sync
        if centro_id_esperado and not centro_de_custo_confirmado(driver, centro_id_esperado):
            log(f"❌ Salvar bloqueado após sync: centro de custo não confirmado | esperado={centro_id_esperado}")
            dump_active_gasto_diag(
                driver,
                "salvar_bloqueado_centro_nao_confirmado_pos_sync",
                {
                    "valores": valores,
                    "sync": sync,
                    "centro_id_esperado": centro_id_esperado,
                    "motivo": "centro_de_custo_confirmado retornou False após sync do Livewire",
                },
            )
            return False

        try:
            time.sleep(0.5)
            btn = wait_btn_salvar_habilitar(driver, timeout=min(8, timeout))
        except Exception as e2:
            log(f"❌ Salvar NÃO habilitou: {type(e2).__name__} | {e2}")
            dump_active_gasto_diag(
                driver,
                "salvar_nao_habilitou_analise",
                {
                    "valores": valores,
                    "sync": sync,
                    "erro_salvar": f"{type(e2).__name__} | {e2}",
                },
            )
            return False

    # 3) Última trava antes do clique
    if centro_id_esperado:
        if not centro_de_custo_confirmado(driver, centro_id_esperado):
            log(f"❌ Salvar bloqueado na validação final: centro de custo não confirmado | esperado={centro_id_esperado}")
            dump_active_gasto_diag(
                driver,
                "salvar_bloqueado_validacao_final_centro",
                {
                    "valores": valores,
                    "centro_id_esperado": centro_id_esperado,
                    "motivo": "centro_de_custo_confirmado retornou False imediatamente antes do clique",
                },
            )
            return False

    # 4) Clique no botão com retry controlado
    clicou = False
    ultimo_erro = None

    for t in range(1, 4):
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)

            # revalida centro em toda tentativa
            if centro_id_esperado and not centro_de_custo_confirmado(driver, centro_id_esperado):
                log(f"❌ Salvar bloqueado após sync: fornecedor não confirmado | esperado={fornecedor_id_esperado}")
                dump_active_gasto_diag(
                    driver,
                    "salvar_bloqueado_fornecedor_nao_confirmado_pos_sync",
                    {
                        "valores": valores,
                        "sync": sync,
                        "fornecedor_id_esperado": fornecedor_id_esperado,
                        "motivo": "fornecedor_confirmado retornou False após sync do Livewire",
                    },
                )
                return False

            driver.execute_script("arguments[0].click();", btn)
            clicou = True
            break

        except (StaleElementReferenceException, ElementClickInterceptedException) as e:
            ultimo_erro = e
            log(f"⚠️ Clique Salvar falhou {t}/3: {type(e).__name__} | {e}")
            time.sleep(0.5)
            try:
                btn = wait_btn_salvar_habilitar(driver, timeout=timeout)
            except Exception:
                pass

        except Exception as e:
            ultimo_erro = e
            log(f"⚠️ Clique Salvar falhou {t}/3: {type(e).__name__} | {e}")
            time.sleep(0.5)

    if not clicou:
        log(f"❌ Não foi possível clicar em Salvar após 3 tentativas.")
        dump_active_gasto_diag(
            driver,
            "salvar_falha_clique_final",
            {
                "valores": valores,
                "centro_id_esperado": centro_id_esperado,
                "erro_final": f"{type(ultimo_erro).__name__} | {ultimo_erro}" if ultimo_erro else "sem detalhe",
            },
        )
        return False

    # 5) Confirma efeito do clique
    try:
        WebDriverWait(driver, 10).until(EC.staleness_of(btn))
        log("✅ Salvar (re-render detectado).")
    except Exception:
        log("✅ Salvar clicado.")

    time.sleep(0.4)
    return True

# =========================
# Preencher form via YAML
# =========================


def build_valores_por_regra(detalhes: str, yaml_rules: List[Dict[str, Any]]) -> Dict[str, str]:
    """
    Converte a regra YAML escolhida para o dicionário usado no preenchimento do formulário.
    Também aplica a regra automática de abastecimento quando o contexto indicar isso.
    """
    valores_preenchimento: Dict[str, str] = {
        "categoria": VAL_CATEGORIA,
        "fornecedor": VAL_FORNECEDOR,
        "forma_pgto": VAL_FORMA_PGTO,
        "centro_custo": VAL_CENTRO_CUSTO,
        "rule_id": "",
    }

    texto_normalizado = normalize_text(detalhes)

    # Regra "ABASTECIMENTO" (sempre automática)
    # - Mantém o campo detalhes exatamente como está no OFX
    # - Fornecedor vem de quem você pagou no PIX (inferido depois em preencher_form_gasto)
    # - Categoria/Centro/Tipo/Forma seguem os padrões de abastecimento veicular
    if any(token in texto_normalizado for token in ABAST_MATCH_TOKENS) and "ABASTEC" in texto_normalizado:
        valores_preenchimento["tipo_id"] = ABAST_TIPO_ID
        valores_preenchimento["categoria_id"] = ABAST_CATEGORIA_ID
        valores_preenchimento["centro_id"] = ABAST_CENTRO_ID
        valores_preenchimento["forma_pagamento"] = ABAST_FORMA_PGTO
        valores_preenchimento["rule_id"] = "AUTO_ABAST"
        return valores_preenchimento

    if not yaml_rules:
        return valores_preenchimento

    regra_encontrada = match_rule(detalhes, yaml_rules)
    if not regra_encontrada:
        log("ℹ️ Nenhuma regra YAML bateu — usando defaults.")
        return valores_preenchimento

    definicoes_regra = regra_encontrada.get("set") or {}

    def limpar_valor_regra(valor: Any) -> str:
        valor_texto = str(valor or "").strip()
        return valor_texto[:-2] if valor_texto.endswith(".0") else valor_texto

    valores_preenchimento["tipo_id"] = limpar_valor_regra(
        definicoes_regra.get("tipo_id") or definicoes_regra.get("tipo_gasto_id") or definicoes_regra.get("tipo")
    )
    valores_preenchimento["categoria_id"] = limpar_valor_regra(definicoes_regra.get("categoria_id"))
    valores_preenchimento["fornecedor_id"] = limpar_valor_regra(definicoes_regra.get("fornecedor_id"))
    valores_preenchimento["forma_pagamento"] = limpar_valor_regra(definicoes_regra.get("forma_pagamento"))
    valores_preenchimento["centro_id"] = limpar_valor_regra(definicoes_regra.get("centro_id"))
    valores_preenchimento["rule_id"] = str(regra_encontrada.get("id") or "")

    log(
        f"🎯 Regra YAML aplicada: id={valores_preenchimento['rule_id']} "
        f"when_all={regra_encontrada.get('when_all')} set={definicoes_regra}"
    )
    return valores_preenchimento

def select_forma_pagamento_alpine(driver, forma_id: str, timeout: int = TIMEOUT) -> None:
    '''
    Seleciona Forma de pagamento no componente Alpine (botão id=selectFormaPagamento).
    Estratégia:
      1) Clica no botão.
      2) Procura lista visível (role=listbox) e opções (role=option).
      3) Tenta casar por data-value/data-id == forma_id.
      4) Se não existir data-*, usa heurística:
         - forma_id == '1' -> tenta opção contendo 'PIX' (senão 1ª opção visível)
         - forma_id numérico -> tenta posição (int-1) se existir
    '''
    forma_id = str(forma_id).strip()
    if not forma_id:
        return

    btn = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.ID, "selectFormaPagamento"))
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
    time.sleep(0.15)
    try:
        btn.click()
    except Exception:
        driver.execute_script("arguments[0].click();", btn)

    # espera uma lista/option aparecer
    def _visible_options(d):
        opts = d.find_elements(By.XPATH, "//*[@role='option']")
        opts = [o for o in opts if o.is_displayed()]
        return opts if opts else False

    opts = WebDriverWait(driver, timeout).until(_visible_options)

    # 1) tenta por data-value/data-id
    for attr in ("data-value", "data-id", "data-key"):
        cand = [o for o in opts if (o.get_attribute(attr) or "").strip() == forma_id]
        if cand:
            driver.execute_script("arguments[0].click();", cand[0])
            time.sleep(0.25)
            return

    # 2) tenta por texto (PIX) quando id=1
    texts = [(o.text or "").strip().upper() for o in opts]
    if forma_id == "1":
        for i, t in enumerate(texts):
            if "PIX" in t:
                driver.execute_script("arguments[0].click();", opts[i])
                time.sleep(0.25)
                return

    # 3) tenta por posição (id numérico)
    if forma_id.isdigit():
        idx = int(forma_id) - 1
        if 0 <= idx < len(opts):
            driver.execute_script("arguments[0].click();", opts[idx])
            time.sleep(0.25)
            return

    # 4) fallback: primeira opção visível
    if opts:
        driver.execute_script("arguments[0].click();", opts[0])
        time.sleep(0.25)
        return

    raise TimeoutException(f"Forma de pagamento: não encontrei opções visíveis (forma_id={forma_id}).")



def preencher_form_gasto(driver, valores: Dict[str, str]) -> Dict[str, float]:
    """
    Preenche campos na ordem correta e devolve tempos reais por etapa.
    """
    perfil: Dict[str, float] = {
        "tipo_total": 0.0,
        "categoria_total": 0.0,
        "fornecedor_total": 0.0,
        "wrapper_lookup_total": 0.0,
        "forma_pgto_total": 0.0,
        "centro_total": 0.0,
        "detalhes_total": 0.0,
        "preencher_total": 0.0,
    }
    inicio_preencher = perf_now()

    close_overlays(driver)

    detalhes_ctx = valores.get("_detalhes", "") or ""
    detalhes_usuario = limpar_descricao_movimentacao(detalhes_ctx)
    detalhes_usuario = limpar_detalhes_duplicados(detalhes_usuario)
    det_norm = _norm_txt(detalhes_ctx)
    is_abast = ("ABASTEC" in det_norm) and any(tok in det_norm for tok in ABAST_MATCH_TOKENS)

    tipo_id = (valores.get("tipo_id") or "").strip()
    categoria_id = (valores.get("categoria_id") or "").strip()
    fornecedor_id = (valores.get("fornecedor_id") or "").strip()
    forma_pagamento = (valores.get("forma_pagamento") or "").strip()
    centro_id = (valores.get("centro_id") or "").strip()

    log(f"➡️ Selecionando TIPO: {tipo_id}")
    log(f"➡️ Selecionando CATEGORIA: {categoria_id}")
    log(f"➡️ Selecionando FORNECEDOR: {fornecedor_id}")
    log(f"➡️ Selecionando FORMA PGTO: {forma_pagamento}")
    log(f"➡️ Selecionando CENTRO: {centro_id}")

    if is_abast:
        valores["tipo_id"] = tipo_id = (tipo_id or ABAST_TIPO_ID)
        valores["categoria_id"] = categoria_id = ABAST_CATEGORIA_ID
        valores["forma_pagamento"] = forma_pagamento = (forma_pagamento or ABAST_FORMA_PGTO)
        valores["centro_id"] = centro_id = (centro_id or ABAST_CENTRO_ID)

        nd = det_norm
        fornecedor_id_fixo = ""

        if ("CD" in nd) and ("ABASTECIMENTO" in nd) and ("CARRO" in nd) and ("STG" not in nd):
            fornecedor_id_fixo = ABAST_FORNECEDOR_MOVIDA_CD
            if fornecedor_id_fixo:
                log(f"⛽ Abastecimento (CD) detectado â€” fornecedor fixo: {fornecedor_id_fixo}")

        if (not fornecedor_id_fixo) and ("STG" in nd) and (("ABASTECIMENTO" in nd) or ("GALAO" in nd)):
            fornecedor_id_fixo = ABAST_FORNECEDOR_STG
            if fornecedor_id_fixo:
                log(f"⛽ Abastecimento (STG) detectado â€” fornecedor fixo: {fornecedor_id_fixo}")

        if fornecedor_id_fixo:
            fornecedor_id = fornecedor_id_fixo
            valores["fornecedor_id"] = fornecedor_id
        else:
            fornecedor_inferido = infer_fornecedor_id_por_pix(detalhes_ctx)
            if fornecedor_inferido:
                fornecedor_id = fornecedor_inferido
                valores["fornecedor_id"] = fornecedor_id
                log(f"🧠 Fornecedor inferido no abastecimento: {fornecedor_id}")
            else:
                log("⚠️ Abastecimento detectado, mas não consegui inferir fornecedor (fixo/PIX) â€” ficará manual.")

    if categoria_id and not tipo_id:
        raise TimeoutException(
            "Regra inválida: veio categoria_id mas não veio tipo_id. "
            "No Cashtrack, Categoria depende do Tipo de gasto."
        )

    if tipo_id:
        t0 = perf_now()
        select_tipo_gasto_listbox(driver, tipo_id, timeout=TIMEOUT)
        log(f"✅ Tipo de gasto (listbox): {tipo_id}")
        time.sleep(0.35)
        close_overlays(driver)
        perf_log("Tempo TIPO", t0, perfil, "tipo_total")

    if detalhes_usuario:
        # ==========================
        # DETALHES
        # Novo layout do Cashtrack:
        # o detalhe já vem gravado pelo OFX tratado.
        # Não reescrever o campo detalhes.
        # ==========================
        t0 = perf_now()
        log("ℹ️ Detalhes preservados do OFX tratado; campo não será reescrito.")
        perf_log("Tempo DETALHES", t0, perfil, "detalhes_total")

    wrappers_start = perf_now()
    cat_wrapper = None
    forn_wrapper = None
    pg_wrapper = None
    cc_wrapper = None
    err_cat = None
    err_f = None
    if not wait_active_gasto_root(driver, timeout=min(TIMEOUT, 8)):
        dump_active_gasto_diag(driver, "falha_root_form_gasto", {"valores": valores})
        raise TimeoutException("Formulário ativo de gasto não encontrado para preencher campos.")
    perf_log("Tempo localizar wrappers", wrappers_start, perfil, "wrapper_lookup_total")

    if categoria_id:
        t0 = perf_now()
        last = None
        for attempt in range(1, 4):
            try:
                try:
                    try_set_field_by_candidate_ids(
                        driver,
                        VS_FIELD_ID_KEYS.get("categoria", ["categorias"]),
                        categoria_id,
                        timeout=TIMEOUT,
                    )
                except Exception:
                    try:
                        select_vscomp_value_by_candidate_ids(
                            driver,
                            VS_FIELD_ID_KEYS.get("categoria", ["categorias", "categoria"]),
                            categoria_id,
                            timeout=TIMEOUT,
                        )
                    except Exception:
                        if not cat_wrapper:
                            cat_wrapper, err_cat = _get_wrapper_by_key(driver, "categoria", timeout_each=1)
                        if not cat_wrapper:
                            raise TimeoutException(f"Campo obrigatório não encontrado: {LABEL_CATEGORIA}") from err_cat
                        select_vscomp_value(driver, cat_wrapper, categoria_id, timeout=TIMEOUT)
                log(f"✅ {LABEL_CATEGORIA} (ID): {categoria_id}")
                assert_field_matches_expected_id(driver, "categoria", categoria_id, required=True)
                log_field_state(driver, "categoria", LABEL_CATEGORIA)
                break
            except (TimeoutException, StaleElementReferenceException, ElementClickInterceptedException) as e:
                last = e
                close_overlays(driver)
                time.sleep(0.30)
                if attempt == 3:
                    dump_active_gasto_diag(driver, "falha_categoria", {"categoria_id": categoria_id, "erro": str(last)})
                    raise TimeoutException(f"Falha em CATEGORIA: {type(last).__name__} | {last}")
        perf_log("Tempo CATEGORIA", t0, perfil, "categoria_total")

    if fornecedor_id:
        t0 = perf_now()
        last = None
        for attempt in range(1, 4):
            try:
                try:
                    try_set_field_by_candidate_ids(
                        driver,
                        VS_FIELD_ID_KEYS.get("fornecedor", ["fornecedores", "fornecedor"]),
                        fornecedor_id,
                        timeout=TIMEOUT,
                    )
                except Exception:
                    if not forn_wrapper:
                        forn_wrapper, err_f = _get_wrapper_by_key(driver, "fornecedor", timeout_each=1)
                    if not forn_wrapper:
                        raise TimeoutException(f"Campo obrigatório não encontrado: {LABEL_FORNECEDOR}") from err_f
                    select_vscomp_value(driver, forn_wrapper, fornecedor_id, timeout=TIMEOUT)
                log(f"✅ {LABEL_FORNECEDOR} (ID): {fornecedor_id}")
                assert_field_matches_expected_id(driver, "fornecedor", fornecedor_id, required=True)
                log_field_state(driver, "fornecedor", LABEL_FORNECEDOR)
                if ABAST_ANOTAR_DETALHES and is_abast:
                    nome_pix = extrair_nome_pix(detalhes_ctx)
                    if nome_pix:
                        anotar_detalhes(driver, f"Abasteceu: {nome_pix}")
                break
            except (TimeoutException, StaleElementReferenceException, ElementClickInterceptedException) as e:
                last = e
                close_overlays(driver)
                time.sleep(0.30)
                forn_wrapper, err_f = _get_wrapper_by_key(driver, "fornecedor", timeout_each=1)
                if not forn_wrapper:
                    raise TimeoutException(f"Campo obrigatório não encontrado: {LABEL_FORNECEDOR}") from err_f
                if attempt == 3:
                    dump_active_gasto_diag(driver, "falha_fornecedor", {"fornecedor_id": fornecedor_id, "erro": str(last)})
                    raise TimeoutException(f"Falha em FORNECEDOR: {type(last).__name__} | {last}")
        perf_log("Tempo FORNECEDOR", t0, perfil, "fornecedor_total")
    else:
        val_txt = (valores.get("fornecedor") or "").strip()
        if not val_txt:
            raise TimeoutException("Fornecedor obrigatório: não veio fornecedor_id nem fornecedor (texto).")
        t0 = perf_now()
        log(f"⚠️ {LABEL_FORNECEDOR}: sem ID no YAML (texto='{val_txt}').")
        if not forn_wrapper:
            forn_wrapper, err_f = _get_wrapper_by_key(driver, "fornecedor", timeout_each=1)
        if not forn_wrapper:
            raise TimeoutException(f"Campo obrigatório não encontrado: {LABEL_FORNECEDOR}") from err_f
        open_vscomp(driver, forn_wrapper, timeout=TIMEOUT)
        try:
            inp = WebDriverWait(driver, 6).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, "input.vscomp-search-input"))
            )
            inp.clear()
            inp.send_keys(val_txt)
            time.sleep(0.25)

            opt = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((
                    By.XPATH,
                    f"//span[contains(@class,'vscomp-option-text') and contains(normalize-space(), '{val_txt}')]"
                ))
            )
            driver.execute_script("arguments[0].click();", opt)
            log(f"✅ {LABEL_FORNECEDOR} (texto): {val_txt}")
            assert_field_visible_selected(driver, "fornecedor", required=True)
            log_field_state(driver, "fornecedor", LABEL_FORNECEDOR)
        finally:
            close_vscomp(driver)
        perf_log("Tempo FORNECEDOR", t0, perfil, "fornecedor_total")

    if forma_pagamento:
        t0 = perf_now()
        try:
            if driver.find_elements(By.ID, "selectFormaPagamento"):
                select_forma_pagamento_alpine(driver, forma_pagamento, timeout=TIMEOUT)
                log(f"✅ {LABEL_FORMA_PGTO} (Alpine): {forma_pagamento}")
            elif pg_wrapper:
                select_vscomp_value(driver, pg_wrapper, forma_pagamento, timeout=TIMEOUT)
                log(f"✅ {LABEL_FORMA_PGTO} (VSComp ID): {forma_pagamento}")
            assert_field_visible_selected(driver, "forma_pgto", required=True)
            log_field_state(driver, "forma_pgto", LABEL_FORMA_PGTO)
        except Exception as e:
            try:
                opts = driver.find_elements(By.XPATH, "//*[@role='option']")
                opts = [o for o in opts if o.is_displayed()]
                amostra_txt = [(o.text or "").strip() for o in opts[:10]]
            except Exception:
                amostra_txt = []
            raise TimeoutException(
                f"Falha em FORMA DE PAGAMENTO: {type(e).__name__} | {e} | opcoes_visiveis(amostra)={amostra_txt}"
            )
        perf_log("Tempo FORMA PGTO", t0, perfil, "forma_pgto_total")

    if centro_id:
        t0 = perf_now()
        try:
            try:
                try_set_field_by_candidate_ids(
                    driver,
                    VS_FIELD_ID_KEYS.get("centro", ["centros", "centro"]),
                    centro_id,
                    timeout=TIMEOUT,
                )
            except Exception:
                try:
                    select_vscomp_value_by_candidate_ids(
                        driver,
                        VS_FIELD_ID_KEYS.get("centro", ["centros", "centro"]),
                        centro_id,
                        timeout=TIMEOUT,
                    )
                except Exception:
                    if not cc_wrapper:
                        cc_wrapper, _ = _get_wrapper_by_key(driver, "centro", timeout_each=1)
                    if not cc_wrapper:
                        raise TimeoutException(f"Campo obrigatório não encontrado: {LABEL_CENTRO_CUSTO}")
                    select_vscomp_value(driver, cc_wrapper, centro_id, timeout=TIMEOUT)

            log(f"✅ {LABEL_CENTRO_CUSTO} (ID): {centro_id}")

            centro_ok = False

            for tentativa_confirmacao in range(1, 11):
                if centro_de_custo_confirmado(driver, centro_id):
                    centro_ok = True
                    break
                time.sleep(0.20)

            if not centro_ok:
                raise TimeoutException(
                    f"Centro de custo não confirmou antes do salvar | esperado={centro_id}"
                )

            log_field_state(driver, "centro", LABEL_CENTRO_CUSTO)
            perf_log("Tempo CENTRO", t0, perfil, "centro_total")

        except Exception as e:
            dump_active_gasto_diag(
                driver,
                "falha_centro_nao_confirmado",
                {"erro": f"Falha em CENTRO: {type(e).__name__} | {e}", "valores": valores},
            )
            raise

    try:
        assert_field_visible_selected(driver, "tipo", required=bool(tipo_id))
        assert_field_visible_selected(driver, "categoria", required=bool(categoria_id))
        assert_field_visible_selected(driver, "fornecedor", required=bool(fornecedor_id or (valores.get("fornecedor") or "").strip()))
        assert_field_visible_selected(driver, "forma_pgto", required=bool(forma_pagamento))
        try:
            assert_field_visible_selected(driver, "centro", required=bool(centro_id))
        except Exception:
            if not is_btn_salvar_habilitado(driver):
                raise
            log(f"⚠️ {LABEL_CENTRO_CUSTO} não confirmou na leitura visual final, mas Salvar já está habilitado.")
        log_field_state(driver, "tipo", LABEL_TIPO)
        log_field_state(driver, "categoria", LABEL_CATEGORIA)
        log_field_state(driver, "fornecedor", LABEL_FORNECEDOR)
        log_field_state(driver, "forma_pgto", LABEL_FORMA_PGTO)
        try:
            log_field_state(driver, "centro", LABEL_CENTRO_CUSTO)
        except Exception:
            pass
    except Exception as e:
        dump_active_gasto_diag(driver, "falha_validacao_visual", {"erro": str(e), "valores": valores})
        raise

    perfil["preencher_total"] = perf_elapsed(inicio_preencher)
    log(f"⏱️ Tempo PREENCHER TOTAL: {perfil['preencher_total']:.2f}s")
    return perfil



def _paginacao_candidatos_next(driver):
    xpaths = [
        "//a[@rel='next' or contains(@aria-label,'Próx') or contains(@aria-label,'próx') or contains(@aria-label,'Next') or normalize-space()='Próximo' or normalize-space()='Proximo' or normalize-space()='Next' or normalize-space()='›' or normalize-space()='»']",
        "//button[contains(@aria-label,'Próx') or contains(@aria-label,'próx') or contains(@aria-label,'Next') or normalize-space()='Próximo' or normalize-space()='Proximo' or normalize-space()='Next' or normalize-space()='›' or normalize-space()='»']",
        "//*[self::a or self::button][contains(@class,'pagination') and (contains(.,'Próximo') or contains(.,'Proximo') or contains(.,'Next') or contains(.,'›') or contains(.,'»'))]",
    ]
    els = []
    for xp in xpaths:
        try:
            els.extend(driver.find_elements(By.XPATH, xp))
        except Exception:
            pass
    uniq = []
    seen = set()
    for el in els:
        try:
            rid = el.id
            if rid in seen or not el.is_displayed():
                continue
            seen.add(rid)
            cls = (el.get_attribute("class") or "").lower()
            aria = (el.get_attribute("aria-disabled") or "").lower()
            disabled = el.get_attribute("disabled")
            if "disabled" in cls or aria == "true" or disabled not in (None, "", "false"):
                continue
            uniq.append(el)
        except Exception:
            continue
    return uniq

def tentar_ir_proxima_pagina_ofxreview(driver, processed_txids: set[int], timeout: int = 12) -> bool:
    try:
        linhas_antes = driver.find_elements(By.XPATH, "//tbody//tr[@*[name()='wire:key'] and starts-with(@*[name()='wire:key'],'row-')]")
        txids_antes = []
        for r in linhas_antes:
            try:
                tx = get_txid_from_row(r)
                if tx:
                    txids_antes.append(tx)
            except Exception:
                pass
        primeiro_antes = txids_antes[0] if txids_antes else None
    except Exception:
        primeiro_antes = None

    candidatos = _paginacao_candidatos_next(driver)
    if not candidatos:
        return False

    for btn in candidatos:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.15)
            try:
                safe_click(driver, btn)
            except Exception:
                driver.execute_script("arguments[0].click();", btn)

            WebDriverWait(driver, timeout).until(
                lambda d: len(d.find_elements(By.XPATH, "//tbody//tr[@*[name()='wire:key'] and starts-with(@*[name()='wire:key'],'row-')]")) > 0
            )

            def _mudou(d):
                try:
                    rows = d.find_elements(By.XPATH, "//tbody//tr[@*[name()='wire:key'] and starts-with(@*[name()='wire:key'],'row-')]")
                    novos = []
                    for rr in rows:
                        try:
                            tx = get_txid_from_row(rr)
                            if tx:
                                novos.append(tx)
                        except Exception:
                            pass
                    if not novos:
                        return False
                    if primeiro_antes is not None and novos[0] != primeiro_antes:
                        return True
                    return any(tx not in processed_txids for tx in novos)
                except Exception:
                    return False

            WebDriverWait(driver, timeout).until(_mudou)
            log("➡️ Avançou para a próxima página do /ofxreview.")
            return True
        except Exception:
            continue
    return False

def processar_conciliacao_ofx(driver) -> None:
    ensure_diag_session()
    coletor = ColetorAprendizado(APRENDIZADO_DIR)
    arquivo_ofx_origem = str(CURRENT_OFX_TRATADO_PATH or "")
    mapa_ofx = carregar_mapa_ofx_tratado(CURRENT_OFX_TRATADO_PATH)
    total_linhas_ofx = len((mapa_ofx or {}).get("rows") or [])
    total_passos_ref = total_linhas_ofx if total_linhas_ofx > 0 else MAX_LINHAS

    log("🧾 Abrindo /ofxreview...")
    driver.get(OFXREVIEW_URL)
    wait_ready(driver, timeout=PAGELOAD_TIMEOUT)
    if "/ofxreview" not in (get_current_url_safe(driver) or ""):
        dump_diag(
            driver,
            "redirecionado_antes_processar",
            {"url_atual": get_current_url_safe(driver), "esperado": OFXREVIEW_URL},
        )
        raise RuntimeError(f"Tela incorreta para conciliacao: {get_current_url_safe(driver)}")
    WebDriverWait(driver, 60).until(EC.presence_of_element_located((By.CSS_SELECTOR, "tbody tr")))
    log("✅ /ofxreview pronto")

    yaml_rules: List[Dict[str, Any]] = []
    if USE_YAML_RULES and RULES_YAML_PATH:
        yaml_rules = load_yaml_rules(RULES_YAML_PATH)
        log(f"✅ Regras YAML carregadas: {len(yaml_rules)}")
    learned_rules = _safe_load_yaml_rules(RULES_APRENDIDAS_YAML_PATH)
    if learned_rules:
        yaml_rules = learned_rules + yaml_rules
        log(f"🧠 Regras aprendidas carregadas: {len(learned_rules)} ({RULES_APRENDIDAS_YAML_PATH})")

    export_rows: List[Dict[str, str]] = []

    processed_txids: set[int] = set()
    idle_rounds = 0
    MAX_IDLE = 3
    perf_lote_start = perf_now()
    perf_linhas: List[Dict[str, Any]] = []
    timeline_event(
        "lote_start",
        total_linhas_ofx=total_linhas_ofx,
        max_linhas=MAX_LINHAS,
        replay_txid=REPLAY_TXID,
        replay_descricao=REPLAY_DESCRICAO_RAW,
    )

    if total_linhas_ofx > 0:
        log(f"📏 Total de linhas no OFX tratado: {total_linhas_ofx}")

    for _ in range(1, MAX_LINHAS + 1):

        try:
            linhas = driver.find_elements(
                By.XPATH,
                "//tbody//tr[@*[name()='wire:key'] and starts-with(@*[name()='wire:key'],'row-')]"
            )
        except Exception as e:
            msg = str(e).lower()
            if "no such window" in msg or "web view not found" in msg:
                log("🛑 Janela do navegador fechou. Encerrando processamento para evitar crash.")
                return
            raise

        if not linhas:
            log("✅ Não há mais linhas visíveis.")
            break

        next_row = None
        next_txid = None
        for r in linhas:
            try:
                tx = get_txid_from_row(r)
            except Exception:
                continue
            if tx and tx not in processed_txids:
                next_row = r
                next_txid = tx
                break

        if next_txid is None:
            idle_rounds += 1
            if idle_rounds >= MAX_IDLE:
                if tentar_ir_proxima_pagina_ofxreview(driver, processed_txids, timeout=TIMEOUT):
                    idle_rounds = 0
                    time.sleep(0.50)
                    continue
                log("✅ Não há mais linhas novas visíveis.")
                break
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'end'});", linhas[-1])
            except Exception:
                pass
            time.sleep(0.25)
            continue

        idle_rounds = 0
        txid = next_txid
        row = next_row
        processed_txids.add(txid)
        passo_atual = len(processed_txids)
        log(f"\n--- 📌 Passo {passo_atual}/{total_passos_ref} ---")

        perfil = novo_perfil_linha(txid, passo_atual)
        linha_start = perf_now()

        if total_linhas_ofx > 0 and len(processed_txids) >= total_linhas_ofx:
            # Continua processando a linha atual; as próximas iterações encerram por falta de novas linhas.
            pass

        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", row)
            time.sleep(0.15)
        except Exception:
            pass

        try:
            row_data = extract_row_data(row)
        except Exception:
            row_data = {}

        valor_linha = (row_data.get("valor") or "").strip() if isinstance(row_data, dict) else ""
        data_linha = (row_data.get("data") or "").strip() if isinstance(row_data, dict) else ""
        desc_tabela = (row_data.get("descricao") or "").strip() if isinstance(row_data, dict) else ""
        conta_tabela = (row_data.get("conta") or "").strip() if isinstance(row_data, dict) else ""
        origem_ofx = obter_detalhes_origem_ofx(row_data if isinstance(row_data, dict) else {}, CURRENT_OFX_TRATADO_PATH)
        detalhes_ofx = (origem_ofx.get("detalhes") or "").strip()
        before_state = capture_line_state(
            driver,
            txid=txid,
            passo=passo_atual,
            desc_tabela=desc_tabela,
            valor_linha=valor_linha,
            row_data=row_data if isinstance(row_data, dict) else {},
        )
        timeline_event(
            "linha_detectada",
            txid=txid,
            passo=passo_atual,
            descricao=desc_tabela,
            valor=valor_linha,
            row_data=row_data if isinstance(row_data, dict) else {},
        )

        if (REPLAY_TXID or REPLAY_DESCRICAO_RAW) and not _row_matches_replay(txid, desc_tabela):
            perfil["status"] = "skip_replay_filter"
            perfil["linha_total"] = perf_elapsed(linha_start)
            timeline_event("linha_skip_replay_filter", txid=txid, passo=passo_atual, descricao=desc_tabela)
            log_resumo_linha(perfil)
            perf_linhas.append(perfil)
            continue

        if valor_linha or data_linha or desc_tabela:
            log(f"💰 Valor tabela: {valor_linha} | 📅 Data: {data_linha} | 🆔 TXID: {txid}")
            if desc_tabela:
                log(f"🧾 Tabela(desc): {desc_tabela[:140]}")

        tem_acao_gasto = row_tem_acao_gasto(driver, txid)
        if not tem_acao_gasto and linha_parece_gasto(row_data):
            log("Linha sem botao 'Gasto' detectavel, mas situacao indica despesa. Vou tentar abrir mesmo assim.")
            tem_acao_gasto = True

        if not tem_acao_gasto:
            log("⏭️ Linha parece RECEITA (sem ação 'Gasto'). Pulando.")
            perfil["status"] = "receita_pulada"
            timeline_event("linha_receita_pulada", txid=txid, passo=passo_atual, descricao=desc_tabela)

            try:
                payload = montar_payload_coleta(
                    arquivo_ofx=arquivo_ofx_origem,
                    txid=txid,
                    row_data=row_data,
                    resultado_regra={},
                    status="receita_pulada",
                    observacao="linha sem ação gasto",
                )
                coletor.registrar(payload)
            except Exception as e:
                log(f"⚠️ Falha ao registrar aprendizado (receita_pulada): {type(e).__name__} | {e}")

            perfil["linha_total"] = perf_elapsed(linha_start)
            log_resumo_linha(perfil)
            perf_linhas.append(perfil)
            continue

        detalhes = ""
        valor_form = ""
        try:
            t0 = perf_now()
            ctx = abrir_gasto_confirmado(driver, txid, desc_tabela, valor_linha, timeout=TIMEOUT, tentativas=3)
            perfil["abrir_form_total"] = perf_elapsed(t0)
            log(f"🧾 Form(detalhes): {ctx['detalhes'][:140]}")
            if ctx.get("valor_form"):
                log(f"🔎 Conferência valor → Tabela: {valor_linha} | Form: {ctx['valor_form']}")
            detalhes = (detalhes_ofx or ctx["detalhes"] or desc_tabela).strip()
            valor_form = ctx.get("valor_form", "")
        except Exception as e:
            log(f"⚠️ Falha abrindo/confirmando Gasto do txid={txid}: {type(e).__name__} | {e}")
            perfil["status"] = "erro_contexto_form"
            build_error_bundle(
                driver,
                "erro_contexto_form_bundle",
                txid=txid,
                passo=passo_atual,
                desc_tabela=desc_tabela,
                valor_linha=valor_linha,
                row_data=row_data if isinstance(row_data, dict) else {},
                before_state=before_state,
                extra={"erro": f"{type(e).__name__} | {e}"},
            )

            try:
                payload = montar_payload_coleta(
                    arquivo_ofx=arquivo_ofx_origem,
                    txid=txid,
                    row_data=row_data,
                    resultado_regra={},
                    status="erro",
                    observacao=f"erro_contexto_form: {type(e).__name__} | {e}",
                )
                coletor.registrar(payload)
            except Exception as reg_err:
                log(f"⚠️ Falha ao registrar aprendizado (erro): {type(reg_err).__name__} | {reg_err}")

            fechar_formulario_aberto(driver)
            perfil["linha_total"] = perf_elapsed(linha_start)
            log_resumo_linha(perfil)
            perf_linhas.append(perfil)
            continue

        if not AUTO_PREENCHER_E_SALVAR:
            log("⏭️ AUTO_PREENCHER_E_SALVAR=false (não vai preencher/salvar).")
            perfil["status"] = "somente_aberto"
            fechar_formulario_aberto(driver)
            perfil["linha_total"] = perf_elapsed(linha_start)
            log_resumo_linha(perfil)
            perf_linhas.append(perfil)
            continue

        texto_para_match_regra = (desc_tabela or "").strip()
        if not texto_para_match_regra:
            texto_para_match_regra = (detalhes or "").strip()
        if not texto_para_match_regra:
            texto_para_match_regra = (detalhes_ofx or "").strip()

        if desc_tabela:
            texto_match_maiusculo = texto_para_match_regra.upper()
            if (len(texto_para_match_regra) < 10) or ("PIX EMITIDO OUTRA IF" in texto_match_maiusculo):
                texto_para_match_regra = f"{texto_para_match_regra} | {desc_tabela}".strip(" |")

        log(f"🔎 TEXTO USADO NO MATCH: {texto_para_match_regra}")

        t0 = perf_now()
        valores = build_valores_por_regra(texto_para_match_regra, yaml_rules)
        perfil["match_regra_total"] = perf_elapsed(t0)
        #NOVO LAYOUT: O DETALHE JÁ VEM GRAVADO PELO OFX TRATADO.
        #NÃO REESCREVER NO FORMULÁRIO.
        valores["_detalhes"] = ""
        perfil["rule_id"] = valores.get("rule_id", "")

        if not valores or not any(
            valores.get(k) for k in ("categoria_id", "fornecedor_id", "centro_id", "forma_pagamento", "tipo_id")
        ):
            log("⏭️ Sem regra aplicável (sem campos-chave). Pulando.")
            perfil["status"] = "sem_regra"
            timeline_event("linha_sem_regra", txid=txid, passo=passo_atual, descricao=desc_tabela)

            try:
                payload = montar_payload_coleta(
                    arquivo_ofx=arquivo_ofx_origem,
                    txid=txid,
                    row_data=row_data,
                    resultado_regra=valores or {},
                    status="sem_regra",
                    observacao="nenhuma regra aplicável com campos-chave",
                )
                coletor.registrar(payload)
            except Exception as e:
                log(f"⚠️ Falha ao registrar aprendizado (sem_regra): {type(e).__name__} | {e}")

            if AUTO_APRENDER_PENDENTES:
                try:
                    log("📝 Modo aprender pendentes ativo: preencha manualmente e salve na tela; depois pressione ENTER aqui.")
                    _ = input("Pressione ENTER para capturar classificação manual (ou digite SKIP): ").strip().upper()
                    if _ != "SKIP":
                        manual_ids = _capturar_classificacao_manual(driver)
                        if all(manual_ids.get(k) for k in ("tipo_id", "categoria_id", "fornecedor_id", "centro_id", "forma_pagamento")):
                            coletor.registrar_manual_confirmado(
                                arquivo_ofx=arquivo_ofx_origem,
                                txid=txid,
                                descricao_original=(row_data.get("descricao_raw", "") or row_data.get("descricao", "")),
                                valor=row_data.get("valor", ""),
                                data_lancamento=row_data.get("data", ""),
                                conta=row_data.get("conta", ""),
                                tipo_id=manual_ids["tipo_id"],
                                categoria_id=manual_ids["categoria_id"],
                                fornecedor_id=manual_ids["fornecedor_id"],
                                centro_id=manual_ids["centro_id"],
                                forma_pagamento=manual_ids["forma_pagamento"],
                                observacao="capturado manualmente no ofxreview",
                            )
                            nova_regra = _montar_regra_aprendida(texto_para_match_regra, manual_ids)
                            if nova_regra and _append_rule_to_yaml(RULES_APRENDIDAS_YAML_PATH, nova_regra):
                                log(f"🧠 Nova regra aprendida salva em: {RULES_APRENDIDAS_YAML_PATH}")
                                yaml_rules.insert(0, nova_regra)
                            perfil["status"] = "manual_confirmado"
                        else:
                            log(f"⚠️ Captura manual incompleta (faltaram IDs): {manual_ids}")
                except Exception as e:
                    log(f"⚠️ Falha no modo aprender pendentes: {type(e).__name__} | {e}")

            fechar_formulario_aberto(driver)
            perfil["linha_total"] = perf_elapsed(linha_start)
            log_resumo_linha(perfil)
            perf_linhas.append(perfil)
            continue

        if (valores.get("categoria_id") or "").strip() and not (valores.get("tipo_id") or "").strip():
            log("⏭️ Regra tem categoria_id mas não tem tipo_id (Categoria depende do Tipo). Pulando.")
            perfil["status"] = "regra_invalida_sem_tipo"
            timeline_event("linha_regra_invalida", txid=txid, passo=passo_atual, descricao=desc_tabela, valores=valores)

            try:
                payload = montar_payload_coleta(
                    arquivo_ofx=arquivo_ofx_origem,
                    txid=txid,
                    row_data=row_data,
                    resultado_regra=valores or {},
                    status="erro",
                    observacao="regra inválida: categoria_id sem tipo_id",
                )
                coletor.registrar(payload)
            except Exception as e:
                log(f"⚠️ Falha ao registrar aprendizado (regra_invalida_sem_tipo): {type(e).__name__} | {e}")

            fechar_formulario_aberto(driver)
            perfil["linha_total"] = perf_elapsed(linha_start)
            log_resumo_linha(perfil)
            perf_linhas.append(perfil)
            continue

        export_rows.append({
            "passo": str(passo_atual),
            "transaction_id": str(txid),
            "descricao_tabela": desc_tabela,
            "detalhes_form": detalhes,
            "texto_regra": texto_para_match_regra,
            "valor": valor_linha,
            "data": data_linha,
            "conta": conta_tabela,
            "rule_id": valores.get("rule_id", ""),
            "tipo_id": valores.get("tipo_id", ""),
            "categoria_id": valores.get("categoria_id", ""),
            "fornecedor_id": valores.get("fornecedor_id", ""),
            "forma_pagamento": valores.get("forma_pagamento", ""),
            "centro_id": valores.get("centro_id", ""),
        })

        try:
            ok_preench = False
            for attempt in range(1, 4):
                try:
                    timeline_event("preencher_inicio", txid=txid, passo=passo_atual, attempt=attempt, valores=valores)
                    if attempt > 1:
                        log(f"🔁 Reabrindo contexto antes do retry {attempt}/3...")
                        ctx = abrir_gasto_confirmado(driver, txid, desc_tabela, valor_linha, timeout=TIMEOUT, tentativas=2)
                        detalhes = ctx["detalhes"]
                        valor_form = ctx.get("valor_form", "")
                    perfil_fill = preencher_form_gasto(driver, valores)
                    for k in (
                        "tipo_total",
                        "categoria_total",
                        "fornecedor_total",
                        "wrapper_lookup_total",
                        "forma_pgto_total",
                        "centro_total",
                        "preencher_total",
                    ):
                        perfil[k] = float(perfil_fill.get(k) or 0.0)
                    ok_preench = True
                    timeline_event("preencher_ok", txid=txid, passo=passo_atual, attempt=attempt, perfil_fill=perfil_fill)
                    break

                except (TimeoutException, StaleElementReferenceException, RuntimeError) as e:
                    log(f"⚠️ Retry preencher_form_gasto ({attempt}/3) txid={txid}: {type(e).__name__} | {e}")
                    timeline_event("preencher_retry", txid=txid, passo=passo_atual, attempt=attempt, erro=f"{type(e).__name__} | {e}")
                    fechar_formulario_aberto(driver)
                    time.sleep(0.45)
                    continue

            if not ok_preench:
                raise TimeoutException("preencher_form_gasto falhou após 3 tentativas")

        except Exception as e:
            log(f"⚠️ Falha preenchendo form txid={txid}: {type(e).__name__} | {repr(e)}")
            perfil["status"] = "erro_preenchimento"
            build_error_bundle(
                driver,
                "erro_preenchimento_bundle",
                txid=txid,
                passo=passo_atual,
                desc_tabela=desc_tabela,
                valor_linha=valor_linha,
                valores=valores,
                row_data=row_data if isinstance(row_data, dict) else {},
                before_state=before_state,
                extra={"erro": f"{type(e).__name__} | {e}"},
            )

            try:
                payload = montar_payload_coleta(
                    arquivo_ofx=arquivo_ofx_origem,
                    txid=txid,
                    row_data=row_data,
                    resultado_regra=valores,
                    status="erro",
                    observacao=f"erro_preenchimento: {type(e).__name__} | {e}",
                )
                coletor.registrar(payload)
            except Exception as reg_err:
                log(f"⚠️ Falha ao registrar aprendizado (erro_preenchimento): {type(reg_err).__name__} | {reg_err}")

            fechar_formulario_aberto(driver)
            perfil["linha_total"] = perf_elapsed(linha_start)
            log_resumo_linha(perfil)
            perf_linhas.append(perfil)
            continue

        if (valores.get("centro_id") or "").strip():
            if not centro_de_custo_confirmado(driver, valores["centro_id"]):
                raise TimeoutException(
                    f"Bloqueado antes do salvar: centro de custo não confirmado | esperado={valores['centro_id']}"
                )

        t0 = perf_now()
        timeline_event("salvar_inicio", txid=txid, passo=passo_atual, valores=valores)
        ok = clicar_salvar_livewire(driver, timeout=TIMEOUT, valores=valores)
        perfil["salvar_total"] = perf_elapsed(t0)
        log(f"⏱️ Tempo SALVAR: {perfil['salvar_total']:.2f}s")


        if not ok:
            if PULAR_SE_SALVAR_NAO_HABILITAR:
                log("⏭️ Pulando linha (Salvar não habilitou).")
                perfil["status"] = "salvar_nao_habilitou"
                after_state = capture_line_state(
                    driver,
                    txid=txid,
                    passo=passo_atual,
                    desc_tabela=desc_tabela,
                    valor_linha=valor_linha,
                    valores=valores,
                    row_data=row_data if isinstance(row_data, dict) else {},
                )
                build_error_bundle(
                    driver,
                    "salvar_nao_habilitou_bundle",
                    txid=txid,
                    passo=passo_atual,
                    desc_tabela=desc_tabela,
                    valor_linha=valor_linha,
                    valores=valores,
                    row_data=row_data if isinstance(row_data, dict) else {},
                    before_state=before_state,
                    after_state=after_state,
                )
                timeline_event("salvar_nao_habilitou", txid=txid, passo=passo_atual, valores=valores, after_state=after_state)

                try:
                    payload = montar_payload_coleta(
                        arquivo_ofx=arquivo_ofx_origem,
                        txid=txid,
                        row_data=row_data,
                        resultado_regra=valores,
                        status="erro",
                        observacao="salvar nao habilitou",
                    )
                    coletor.registrar(payload)
                except Exception as e:
                    log(f"⚠️ Falha ao registrar aprendizado (erro_salvar): {type(e).__name__} | {e}")

                fechar_formulario_aberto(driver)
                time.sleep(0.6)
                fechar_formulario_aberto(driver)
                perfil["linha_total"] = perf_elapsed(linha_start)
                log_resumo_linha(perfil)
                perf_linhas.append(perfil)
                continue

            raise RuntimeError("Salvar não habilitou e PULAR_SE_SALVAR_NAO_HABILITAR=false")

        time.sleep(0.8)
        perfil["status"] = "match_ok"
        timeline_event("linha_salva", txid=txid, passo=passo_atual, valores=valores, tempo_salvar=perfil["salvar_total"])

        try:
            payload = montar_payload_coleta(
                arquivo_ofx=arquivo_ofx_origem,
                txid=txid,
                row_data=row_data,
                resultado_regra=valores,
                status="match_ok",
                observacao="salvo com regra automatica",
            )
            coletor.registrar(payload)
        except Exception as e:
            log(f"⚠️ Falha ao registrar aprendizado (match_ok): {type(e).__name__} | {e}")

        perfil["linha_total"] = perf_elapsed(linha_start)
        log_resumo_linha(perfil)
        perf_linhas.append(perfil)

    if EXPORT_MATCHES_CSV and export_rows:
        try:
            import csv
            with open(EXPORT_MATCHES_CSV_PATH, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(export_rows[0].keys()), delimiter=";")
                w.writeheader()
                w.writerows(export_rows)
            log(f"🧾 CSV exportado: {EXPORT_MATCHES_CSV_PATH} ({len(export_rows)} linhas)")
        except Exception as e:
            log(f"⚠️ Falha exportando CSV: {e}")

    if perf_linhas:
        total_lote = perf_elapsed(perf_lote_start)
        salvas = [p for p in perf_linhas if p.get("status") in ("salvo", "match_ok")]
        linhas_lidas = len(perf_linhas)

        def media(chave: str) -> float:
            vals = [float(p.get(chave) or 0.0) for p in perf_linhas if float(p.get(chave) or 0.0) > 0]
            return (sum(vals) / len(vals)) if vals else 0.0

        pior = max(perf_linhas, key=lambda p: float(p.get("linha_total") or 0.0))
        log("\n📈 RESUMO DO LOTE")
        log(f"   Linhas avaliadas: {linhas_lidas}")
        log(f"   Linhas salvas: {len(salvas)}")
        if total_linhas_ofx > 0:
            diff = linhas_lidas - total_linhas_ofx
            status = "OK" if diff == 0 else "DIVERGENTE"
            log(f"   Linhas OFX (tratado): {total_linhas_ofx}")
            log(f"   Linhas lidas no /ofxreview: {linhas_lidas}")
            log(f"   Conferência OFX x leitura: {status} (diferença={diff:+d})")
        log(f"   Tempo total lote: {total_lote:.2f}s")
        log(f"   Média abrir/confirmar form: {media('abrir_form_total'):.2f}s")
        log(f"   Média categoria: {media('categoria_total'):.2f}s")
        log(f"   Média fornecedor: {media('fornecedor_total'):.2f}s")
        log(f"   Média localizar wrappers: {media('wrapper_lookup_total'):.2f}s")
        log(f"   Média centro: {media('centro_total'):.2f}s")
        log(f"   Média preencher total: {media('preencher_total'):.2f}s")
        log(f"   Média salvar: {media('salvar_total'):.2f}s")
        log(f"   Média linha total: {media('linha_total'):.2f}s")
        log(
            f"   Pior linha: TXID {pior.get('txid')} com "
            f"{float(pior.get('linha_total') or 0.0):.2f}s ({pior.get('status')})"
        )
        rule_failures: Dict[str, int] = {}
        for item in perf_linhas:
            if item.get("status") in ("match_ok", "salvo", "receita_pulada", "sem_regra", "skip_replay_filter"):
                continue
            rid = str(item.get("rule_id") or "")
            if rid:
                rule_failures[rid] = rule_failures.get(rid, 0) + 1
        summary = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "session_dir": str(DIAG_SESSION_DIR),
            "artifacts_dir": str(DIAG_ARTIFACTS_DIR),
            "timeline_path": str(DIAG_TIMELINE_PATH),
            "replay_cases_path": str(DIAG_REPLAY_PATH),
            "linhas_avaliadas": linhas_lidas,
            "linhas_salvas": len(salvas),
            "linhas_ofx_tratado": total_linhas_ofx,
            "tempo_total_lote": total_lote,
            "status_counts": {status: sum(1 for p in perf_linhas if p.get("status") == status) for status in sorted({str(p.get("status") or "") for p in perf_linhas})},
            "regras_que_mais_falharam": dict(sorted(rule_failures.items(), key=lambda kv: (-kv[1], kv[0]))[:20]),
            "medias": {
                "abrir_form_total": media("abrir_form_total"),
                "categoria_total": media("categoria_total"),
                "fornecedor_total": media("fornecedor_total"),
                "wrapper_lookup_total": media("wrapper_lookup_total"),
                "centro_total": media("centro_total"),
                "preencher_total": media("preencher_total"),
                "salvar_total": media("salvar_total"),
                "linha_total": media("linha_total"),
            },
            "pior_linha": pior,
            "linhas": perf_linhas,
        }
        write_summary_json(summary)
        timeline_event("lote_fim", resumo=summary)


def main() -> None:
    print("🔥 RODANDO selenium_cashtrack_v5 atualizado")
    install_sigint_debug()
    try:
        if os.name == "nt":
            os.system("chcp 65001 > nul")
            try:
                import ctypes
                ctypes.windll.kernel32.SetConsoleOutputCP(65001)
                ctypes.windll.kernel32.SetConsoleCP(65001)
            except Exception:
                pass
            os.environ["PYTHONIOENCODING"] = "utf-8"
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    log(f"📂 CWD (onde vai salvar diagnostics): {os.getcwd()}")

    if not EMAIL or not SENHA:
        raise RuntimeError("CASH_EMAIL ou CASH_SENHA não carregou do .env")

    log("script iniciou")
    log(f"Email carregado? {bool(EMAIL)} | Senha carregada? {bool(SENHA)}")
    log(f"EDGE_DRIVER_PATH: {EDGE_DRIVER_PATH}")
    log(f"DO_IMPORT_OFX: {DO_IMPORT_OFX} | BANCO: {BANCO_NOME}")
    log(f"USE_YAML_RULES: {USE_YAML_RULES} | YAML: {RULES_YAML_PATH}")
    ensure_diag_session()
    log(f"🧪 Sessão de diagnostics: {DIAG_SESSION_DIR}")
    log(f"🧪 Artifacts da sessão: {DIAG_ARTIFACTS_DIR}")
    if REPLAY_TXID or REPLAY_DESCRICAO_RAW:
        log(f"🧪 Replay ativo | txid={REPLAY_TXID or '-'} | descricao={REPLAY_DESCRICAO_RAW or '-'}")

    driver = start_driver_edge()
    try:
        do_login(driver)

        if DO_IMPORT_OFX:
            caminho = find_latest_ofx()
            log(f"📂 OFX tratado pronto para importação: {caminho}")
            importar_ofx(driver, BANCO_NOME, caminho)

        processar_conciliacao_ofx(driver)
        log("Itens sem regra devem permanecer pendentes.")
        log("Nenhuma finalizacao deve ser feita sem sua permissao explicita.")
        prompt_final = _repair_mojibake(
            "✅ Processo terminou. Vá no navegador, clique em CONVERTER e faça suas exclusões.\n"
            "Quando terminar, volte aqui e aperte ENTER para fechar o navegador..."
        )
        input("\n" + prompt_final)
        log("✅ Processo finalizado com sucesso.")
    finally:
        if KEEP_BROWSER_OPEN:
            log("🧷 KEEP_BROWSER_OPEN=true — navegador ficará aberto.")
            return
        try:
            driver.quit()
        except Exception:
            pass

if __name__ == "__main__":
    main()
