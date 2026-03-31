import os
import io
import time
import uuid
import logging
import threading

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import pandas as pd
import requests as http

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ── Config ────────────────────────────────────────────────────────────────────
API_TOKEN = os.getenv("API4COM_TOKEN", "")
API_BASE = "https://api.api4com.com/api/v1"
RAMAL = os.getenv("RAMAL", "1005")

# Tempo entre uma ligação e outra
CALL_DELAY = float(os.getenv("CALL_DELAY", "0.5"))

# Duração mínima para considerar que houve atendimento humano
MIN_HUMAN_DURATION = int(os.getenv("MIN_HUMAN_DURATION", "8"))

HEADERS = {
    "Authorization": API_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# ── Estado global da campanha ─────────────────────────────────────────────────
campaign = {
    "running": False,
    "paused": False,
    "leads": [],
    "results": [],
    "current": [],
    "answered": None,
    "stats": {
        "answered": 0,
        "cancelled": 0,
        "voicemail": 0,
        "invalid": 0,
        "error": 0,
        "total": 0,
    },
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
        if file.filename.lower().endswith(".csv"):
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
        campaign["leads"] = leads
        campaign["results"] = []
        campaign["stats"] = {
            "answered": 0,
            "cancelled": 0,
            "voicemail": 0,
            "invalid": 0,
            "error": 0,
            "total": 0,
        }
        campaign["answered"] = None
        campaign["running"] = False
        campaign["paused"] = False
        campaign["current"] = []

    return jsonify({
        "ok": True,
        "total": len(leads),
        "preview": [{"name": l["name"], "phones": l["phones"]} for l in leads[:5]],
    })


# ── API: iniciar campanha ─────────────────────────────────────────────────────
@app.route("/api/start", methods=["POST"])
def start():
    global CALL_DELAY, MIN_HUMAN_DURATION

    with campaign_lock:
        if not campaign["leads"]:
            return jsonify({"error": "Carregue uma planilha primeiro"}), 400
        if campaign["running"]:
            return jsonify({"error": "Campanha já está rodando"}), 400

    data = request.get_json(silent=True) or {}

    if "call_delay" in data:
        try:
            CALL_DELAY = max(0.0, float(data["call_delay"]))
        except Exception:
            return jsonify({"error": "call_delay inválido"}), 400

    if "min_human_duration" in data:
        try:
            MIN_HUMAN_DURATION = max(1, int(data["min_human_duration"]))
        except Exception:
            return jsonify({"error": "min_human_duration inválido"}), 400

    with campaign_lock:
        campaign["running"] = True
        campaign["paused"] = False
        campaign["answered"] = None

        # opcional: recomeçar só os não concluídos
        for lead in campaign["leads"]:
            if "done" not in lead:
                lead["done"] = False

    t = threading.Thread(target=_run_campaign_sync, daemon=True)
    t.start()

    return jsonify({
        "ok": True,
        "mode": "sequencial",
        "call_delay": CALL_DELAY,
        "min_human_duration": MIN_HUMAN_DURATION,
    })


@app.route("/api/resume", methods=["POST"])
def resume():
    with campaign_lock:
        campaign["paused"] = False
        campaign["answered"] = None
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def stop():
    with campaign_lock:
        campaign["running"] = False
        campaign["paused"] = False
        campaign["answered"] = None
        campaign["current"] = []
    return jsonify({"ok": True})


# ── API: estado atual ─────────────────────────────────────────────────────────
@app.route("/api/state")
def state():
    with campaign_lock:
        answered = campaign["answered"]

        status = (
            "atendido" if answered else
            "pausado" if campaign["paused"] else
            "discando" if campaign["running"] else
            "aguardando"
        )

        remaining = len([l for l in campaign["leads"] if not l.get("done")])

        return jsonify({
            "status": status,
            "answered": answered,
            "current": campaign["current"],
            "stats": campaign["stats"],
            "results": campaign["results"][-50:],
            "remaining": remaining,
            "total": len(campaign["leads"]),
            "call_delay": CALL_DELAY,
            "min_human_duration": MIN_HUMAN_DURATION,
            "mode": "sequencial",
        })


# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Mantido para debug/saúde. A classificação principal está sendo feita
    pelo polling em _wait_result().
    """
    try:
        data = request.get_json(silent=True) or {}
        logger.info(f"Webhook recebido: {data}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/webhook", methods=["GET"])
def webhook_health():
    return jsonify({"ok": True, "message": "Webhook endpoint ativo"})


# ── Motor sequencial ──────────────────────────────────────────────────────────
def _run_campaign_sync():
    logger.info(f"Iniciando campanha sequencial: {len(campaign['leads'])} leads")

    for lead in campaign["leads"]:
        with campaign_lock:
            if not campaign["running"]:
                break

        if lead.get("done"):
            continue

        while True:
            with campaign_lock:
                paused = campaign["paused"]
                running = campaign["running"]
            if not running or not paused:
                break
            time.sleep(0.2)

        with campaign_lock:
            if not campaign["running"]:
                break
            campaign["current"] = [lead["name"]]

        result = _dial_lead_sequential(lead)

        with campaign_lock:
            campaign["current"] = []

        if result == "answered":
            with campaign_lock:
                campaign["paused"] = True

            while True:
                with campaign_lock:
                    paused = campaign["paused"]
                    running = campaign["running"]
                if not running or not paused:
                    break
                time.sleep(0.2)

        with campaign_lock:
            still_running = campaign["running"]

        if still_running:
            time.sleep(CALL_DELAY)

    with campaign_lock:
        campaign["running"] = False
        campaign["current"] = []

    logger.info("Campanha finalizada")


def _dial_lead_sequential(lead: dict) -> str:
    """
    Liga número por número do mesmo lead.
    Só passa para o próximo lead quando:
    - terminar todos os números do lead atual, ou
    - encontrar atendimento humano.
    """
    for phone in lead["phones"]:
        with campaign_lock:
            if not campaign["running"]:
                return "stopped"

        while True:
            with campaign_lock:
                paused = campaign["paused"]
                running = campaign["running"]
            if not running or not paused:
                break
            time.sleep(0.2)

        with campaign_lock:
            if not campaign["running"]:
                return "stopped"

        logger.info(f"[{lead['name']}] -> discando {phone}")
        _add_result(lead["name"], phone, "discando")

        call_id = _originate(phone, lead["name"])
        if not call_id:
            _update_result(lead["name"], phone, "error")
            with campaign_lock:
                campaign["stats"]["error"] += 1
                campaign["stats"]["total"] += 1
            time.sleep(CALL_DELAY)
            continue

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
            logger.info(f"Humano atendeu: {lead['name']} {phone}")
            return "answered"

        # voicemail / cancelled / invalid / error -> próximo número do mesmo lead
        with campaign_lock:
            still_running = campaign["running"]

        if still_running:
            time.sleep(CALL_DELAY)

    lead["done"] = True
    return "finished_lead"


def _originate(phone: str, name: str) -> str | None:
    phone = _fmt_phone(phone)

    try:
        r = http.post(
            f"{API_BASE}/dialer",
            headers=HEADERS,
            json={
                "extension": RAMAL,
                "phone": phone,
                "metadata": {
                    "lead_name": name,
                    "source": "discador_cloud",
                },
            },
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        return str(data.get("id") or data.get("call_id") or "")
    except Exception as e:
        logger.error(f"Erro ao originar {phone}: {e}")
        return None


def _wait_result(phone: str, name: str, call_id: str) -> str:
    """
    Espera o resultado final da chamada.
    Regras:
    - caixa postal / robô / URA curta -> voicemail
    - inválido -> invalid
    - não atendeu / ocupado / cancelada -> cancelled
    - duração suficiente -> answered
    """
    phone_fmt = _fmt_phone(phone)

    for _ in range(20):  # até ~100s
        with campaign_lock:
            if not campaign["running"]:
                return "cancelled"

        time.sleep(5)

        try:
            r = http.get(
                f"{API_BASE}/calls",
                headers=HEADERS,
                params={"page": 1},
                timeout=10
            )

            data = r.json()
            calls = data.get("data", []) if isinstance(data, dict) else []

            for c in calls:
                to_number = _fmt_phone(c.get("to", ""))
                current_call_id = str(c.get("id") or c.get("call_id") or "")

                # tenta casar por telefone e/ou id
                if to_number == phone_fmt or (call_id and current_call_id == call_id):
                    cause = (c.get("hangup_cause") or c.get("hangupCause") or "").upper().strip()
                    duration = int(c.get("duration") or 0)

                    # se ainda está em andamento, continua esperando
                    if not cause and duration == 0:
                        continue

                    status = _final_call_status(cause, duration)
                    logger.info(
                        f"Resultado chamada: lead={name} phone={phone_fmt} "
                        f"call_id={call_id} cause={cause} duration={duration} status={status}"
                    )
                    return status

        except Exception as e:
            logger.warning(f"Erro consultando status da chamada {phone_fmt}: {e}")

    return "cancelled"


def _final_call_status(cause: str, duration: int) -> str:
    cause_u = (cause or "").upper().strip()

    # inválidos
    if cause_u in {
        "UNALLOCATED_NUMBER",
        "INVALID_NUMBER_FORMAT",
        "USER_NOT_REGISTERED",
        "NAO FOI POSSIVEL COMPLETAR",
        "NAO FOI POSSIVEL",
        "NUMERO INVALIDO",
    }:
        return "invalid"

    # caixa postal / secretária / robô detectado pela operadora
    if cause_u in {
        "VOICEMAIL",
        "CAIXA POSTAL",
    }:
        return "voicemail"

    # não atendeu / ocupado / recusou / cancelada
    if cause_u in {
        "ORIGINATOR_CANCEL",
        "NO_ANSWER",
        "USER_BUSY",
        "CALL_REJECTED",
        "SUBSCRIBER_ABSENT",
        "CANCELADA",
    }:
        return "cancelled"

    # se durou pouco demais, tende a ser robô/ura/caixa
    if duration > 0 and duration < MIN_HUMAN_DURATION:
        return "voicemail"

    # se teve duração suficiente, assume humano
    if duration >= MIN_HUMAN_DURATION:
        return "answered"

    return "cancelled"


# ── Helpers ───────────────────────────────────────────────────────────────────
def _add_result(name, phone, status):
    with campaign_lock:
        campaign["results"].append({
            "id": str(uuid.uuid4())[:8],
            "name": name,
            "phone": phone,
            "status": status,
            "ts": time.strftime("%H:%M:%S"),
        })


def _update_result(name, phone, status):
    with campaign_lock:
        for r in reversed(campaign["results"]):
            if r["name"] == name and r["phone"] == phone:
                r["status"] = status
                break


def _fmt_phone(raw: str) -> str:
    import re
    d = re.sub(r"\D", "", str(raw or ""))
    if d.startswith("55") and len(d) > 11:
        d = d[2:]
    if len(d) == 12 and d.startswith("0"):
        d = d[1:]
    return d


def _norm(col: str) -> str:
    import unicodedata
    import re
    col = unicodedata.normalize("NFKD", str(col).strip().lower())
    col = "".join(c for c in col if not unicodedata.combining(c))
    return re.sub(r"\s+", "_", col)


def _parse_leads(df) -> list:
    import re

    NAME_COLS = ["nome", "name", "contato", "cliente"]
    PHONE_COLS = [
        "fone1", "fone2", "fone3", "fone4", "fone5", "fone6", "fone7", "fone8",
        "telefone1", "telefone2", "telefone3", "telefone4", "telefone5", "telefone6", "telefone7", "telefone8",
        "telefone", "celular", "tel1", "tel2", "tel3", "tel4", "tel5", "tel6", "tel7", "tel8", "phone"
    ]

    leads = []

    for idx, row in df.iterrows():
        name = next(
            (
                str(row[c]).strip()
                for c in NAME_COLS
                if c in row and pd.notna(row[c]) and str(row[c]).strip()
            ),
            f"Lead {idx + 1}"
        )

        phones = []
        for c in PHONE_COLS:
            if c not in row:
                continue

            val = str(row[c]).strip() if pd.notna(row[c]) else ""
            d = re.sub(r"\D", "", val)

            if d.startswith("55") and len(d) > 11:
                d = d[2:]
            if len(d) == 12 and d.startswith("0"):
                d = d[1:]

            if 10 <= len(d) <= 11 and d not in phones:
                phones.append(d)

        if phones:
            leads.append({
                "name": name,
                "phones": phones,
                "done": False,
            })

    return leads


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
