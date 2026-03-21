import os, io, threading, time, uuid, logging
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import pandas as pd
import requests as http

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ── Config ────────────────────────────────────────────────────────────────────
API_TOKEN    = os.getenv("API4COM_TOKEN", "")
API_BASE     = "https://api.api4com.com/api/v1"
RAMAL        = os.getenv("RAMAL", "1005")
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "3"))

HEADERS = {
    "Authorization": API_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# ── Estado global da campanha ─────────────────────────────────────────────────
campaign = {
    "running":   False,
    "paused":    False,
    "leads":     [],        # lista completa
    "results":   [],        # chamadas realizadas
    "current":   [],        # leads discando agora
    "answered":  None,      # lead que atendeu
    "stats":     {"answered": 0, "cancelled": 0, "voicemail": 0, "invalid": 0, "total": 0},
}
campaign_lock = threading.Lock()


# ── Frontend ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", ramal=RAMAL)


# ── API: upload da planilha ───────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400

    file = request.files["file"]
    content = file.read()

    try:
        if file.filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content), dtype=str)
        else:
            df = pd.read_excel(io.BytesIO(content), dtype=str)
    except Exception as e:
        return jsonify({"error": f"Erro ao ler planilha: {e}"}), 400

    df.columns = [_norm(c) for c in df.columns]
    leads = _parse_leads(df)

    if not leads:
        return jsonify({"error": "Nenhum lead com telefone válido encontrado"}), 400

    with campaign_lock:
        campaign["leads"]   = leads
        campaign["results"] = []
        campaign["stats"]   = {"answered": 0, "cancelled": 0, "voicemail": 0, "invalid": 0, "total": 0}
        campaign["answered"] = None
        campaign["running"]  = False

    return jsonify({"ok": True, "total": len(leads),
                    "preview": [{"name": l["name"], "phones": len(l["phones"])} for l in leads[:5]]})


# ── API: iniciar campanha ─────────────────────────────────────────────────────
@app.route("/api/start", methods=["POST"])
def start():
    global MAX_PARALLEL
    if not campaign["leads"]:
        return jsonify({"error": "Carregue uma planilha primeiro"}), 400
    if campaign["running"]:
        return jsonify({"error": "Campanha já está rodando"}), 400

    data = request.get_json(silent=True) or {}
    if "paralelo" in data:
        MAX_PARALLEL = int(data["paralelo"])

    campaign["running"] = True
    campaign["paused"]  = False
    campaign["answered"] = None

    t = threading.Thread(target=_run_campaign, daemon=True)
    t.start()
    return jsonify({"ok": True, "paralelo": MAX_PARALLEL})


# ── API: pausar / retomar ─────────────────────────────────────────────────────
@app.route("/api/resume", methods=["POST"])
def resume():
    campaign["paused"]  = False
    campaign["answered"] = None
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def stop():
    campaign["running"] = False
    campaign["paused"]  = False
    return jsonify({"ok": True})


# ── API: estado atual ─────────────────────────────────────────────────────────
@app.route("/api/state")
def state():
    with campaign_lock:
        answered = campaign["answered"]
        status = (
            "atendido"  if answered else
            "pausado"   if campaign["paused"] else
            "discando"  if campaign["running"] else
            "aguardando"
        )
        return jsonify({
            "status":    status,
            "answered":  answered,
            "current":   campaign["current"],
            "stats":     campaign["stats"],
            "results":   campaign["results"][-50:],
            "remaining": len([l for l in campaign["leads"] if not l.get("done")]),
            "total":     len(campaign["leads"]),
        })




# ── Webhook da Api4Com ────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Recebe eventos em tempo real da Api4Com v1.4.
    Campos reais: called, eventType, hangupCause, duration, metadata
    """
    try:
        data = request.get_json(silent=True) or {}
        logger.info(f"Webhook recebido: {data}")

        event_type = data.get("eventType", "")

        # Só processa eventos de fim de chamada
        # channel-answer = atendeu, channel-hangup = desligou
        if event_type not in ("channel-answer", "channel-hangup"):
            return jsonify({"ok": True, "ignored": True, "event": event_type})

        # Campos reais da Api4Com v1.4
        phone     = data.get("called") or data.get("to") or data.get("phone") or ""
        cause     = data.get("hangupCause") or data.get("hangup_cause") or ""
        duration  = int(data.get("duration") or 0)

        # Metadata com nome do lead
        meta = data.get("metadata") or {}
        if isinstance(meta, str):
            import json as _json
            try: meta = _json.loads(meta)
            except: meta = {}
        lead_name = meta.get("lead_name", "") if isinstance(meta, dict) else ""

        # Determina status
        if event_type == "channel-answer":
            status = "answered"
        elif duration > 0:
            status = "answered"
        else:
            status = _cause_to_status(cause)

        phone_clean = _fmt_phone(phone)
        logger.info(f"Webhook processado: {event_type} | {lead_name} | {phone_clean} | {status}")

        with campaign_lock:
            # Atualiza linha na tabela
            for r in reversed(campaign["results"]):
                if _fmt_phone(r.get("phone", "")) == phone_clean and r["status"] == "discando":
                    r["status"] = status
                    if not lead_name:
                        lead_name = r.get("name", "")
                    break

            # Stats (só no hangup para não duplicar)
            if event_type == "channel-hangup":
                campaign["stats"][status] = campaign["stats"].get(status, 0) + 1
                campaign["stats"]["total"] = campaign["stats"].get("total", 0) + 1

            # Marca atendido e pausa discagem
            if status == "answered" and not campaign["answered"]:
                campaign["answered"] = {
                    "name":  lead_name or phone_clean,
                    "phone": phone_clean,
                }
                campaign["paused"] = True
                logger.info(f"ATENDIDO via webhook: {lead_name} {phone_clean}")

        return jsonify({"ok": True, "status": status, "phone": phone_clean})

    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/webhook", methods=["GET"])
def webhook_health():
    return jsonify({"ok": True, "message": "Webhook endpoint ativo"})

# ── Motor de discagem ─────────────────────────────────────────────────────────
def _run_campaign():
    leads = [l for l in campaign["leads"] if not l.get("done")]
    logger.info(f"Iniciando campanha: {len(leads)} leads")

    i = 0
    while i < len(leads) and campaign["running"]:
        # Aguarda se pausado
        while campaign["paused"] and campaign["running"]:
            time.sleep(1)

        batch = leads[i:i + MAX_PARALLEL]
        i += MAX_PARALLEL

        campaign["current"] = [l["name"] for l in batch]

        threads = []
        for lead in batch:
            t = threading.Thread(target=_dial_lead, args=(lead,), daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        campaign["current"] = []

        # Se alguém atendeu, pausa até usuário clicar em retomar
        if campaign["answered"]:
            campaign["paused"] = True
            while campaign["paused"] and campaign["running"]:
                time.sleep(1)

        time.sleep(2)

    campaign["running"] = False
    campaign["current"] = []
    logger.info("Campanha finalizada")


def _dial_lead(lead: dict):
    for phone in lead["phones"]:
        if not campaign["running"] or campaign["paused"]:
            return

        logger.info(f"[{lead['name']}] → {phone}")
        _add_result(lead["name"], phone, "discando")

        call_id = _originate(phone, lead["name"])
        if not call_id:
            _update_result(lead["name"], phone, "error")
            continue

        # Polling de status via GET /calls
        result = _wait_result(phone, lead["name"], call_id)
        _update_result(lead["name"], phone, result)

        with campaign_lock:
            campaign["stats"]["total"] += 1
            if result in campaign["stats"]:
                campaign["stats"][result] += 1

        if result == "answered":
            lead["done"] = True
            with campaign_lock:
                campaign["answered"] = {"name": lead["name"], "phone": phone}
            return

        if result == "invalid":
            continue  # pula número inválido

        time.sleep(2)

    lead["done"] = True


def _originate(phone: str, name: str) -> str | None:
    phone = _fmt_phone(phone)
    try:
        r = http.post(f"{API_BASE}/dialer", headers=HEADERS, json={
            "extension": RAMAL,
            "phone":     phone,
            "metadata":  {"lead_name": name, "source": "discador_cloud"},
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        return str(data.get("id") or data.get("call_id") or "")
    except Exception as e:
        logger.error(f"Erro ao originar {phone}: {e}")
        return None


def _wait_result(phone: str, name: str, call_id: str) -> str:
    phone_fmt = _fmt_phone(phone)
    for _ in range(20):  # até ~100s
        time.sleep(5)
        try:
            r = http.get(f"{API_BASE}/calls", headers=HEADERS,
                         params={"page": 1}, timeout=10)
            calls = r.json().get("data", [])
            for c in calls:
                if c.get("to", "").replace(" ", "") == phone_fmt:
                    cause    = (c.get("hangup_cause") or "").upper()
                    duration = int(c.get("duration") or 0)
                    if duration > 0:
                        return "answered"
                    return _cause_to_status(cause)
        except Exception:
            pass
    return "cancelled"


def _cause_to_status(cause: str) -> str:
    # Api4Com retorna em inglês via API e em português no painel
    mapping = {
        # Inglês (via API)
        "NORMAL_CLEARING":       "answered",
        "ORIGINATOR_CANCEL":     "cancelled",
        "NO_ANSWER":             "cancelled",
        "USER_BUSY":             "cancelled",
        "CALL_REJECTED":         "cancelled",
        "SUBSCRIBER_ABSENT":     "cancelled",
        "VOICEMAIL":             "voicemail",
        "UNALLOCATED_NUMBER":    "invalid",
        "INVALID_NUMBER_FORMAT": "invalid",
        "USER_NOT_REGISTERED":   "invalid",
        # Português (painel Api4Com)
        "ATENDIDA":              "answered",
        "CANCELADA":             "cancelled",
        "CAIXA POSTAL":          "voicemail",
        "NAO FOI POSSIVEL COMPLETAR": "invalid",
        "NAO FOI POSSIVEL":      "invalid",
        "NUMERO INVALIDO":       "invalid",
    }
    return mapping.get(cause.upper().strip(), "cancelled")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _add_result(name, phone, status):
    with campaign_lock:
        campaign["results"].append({
            "id": str(uuid.uuid4())[:8],
            "name": name, "phone": phone,
            "status": status, "ts": time.strftime("%H:%M:%S"),
        })

def _update_result(name, phone, status):
    with campaign_lock:
        for r in reversed(campaign["results"]):
            if r["name"] == name and r["phone"] == phone:
                r["status"] = status
                break

def _fmt_phone(raw: str) -> str:
    import re
    d = re.sub(r"\D", "", raw)
    if d.startswith("55") and len(d) > 11:
        d = d[2:]
    if len(d) == 12 and d.startswith("0"):
        d = d[1:]
    return d

def _norm(col: str) -> str:
    import unicodedata, re
    col = unicodedata.normalize("NFKD", str(col).strip().lower())
    col = "".join(c for c in col if not unicodedata.combining(c))
    return re.sub(r"\s+", "_", col)

def _parse_leads(df) -> list:
    import re
    NAME_COLS  = ["nome","name","contato","cliente"]
    PHONE_COLS = ["fone1","fone2","fone3","fone4","fone5",
                  "telefone1","telefone2","telefone3","telefone4","telefone5",
                  "telefone","celular","tel1","tel2","tel3","phone"]
    leads = []
    for idx, row in df.iterrows():
        name = next((str(row[c]).strip() for c in NAME_COLS if c in row and pd.notna(row[c]) and str(row[c]).strip()), f"Lead {idx+1}")
        phones = []
        for c in PHONE_COLS:
            if c not in row: continue
            val = str(row[c]).strip() if pd.notna(row[c]) else ""
            d = re.sub(r"\D", "", val)
            if d.startswith("55") and len(d) > 11: d = d[2:]
            if len(d) == 12 and d.startswith("0"): d = d[1:]
            if 10 <= len(d) <= 11 and d not in phones:
                phones.append(d)
        if phones:
            leads.append({"name": name, "phones": phones, "done": False})
    return leads


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
