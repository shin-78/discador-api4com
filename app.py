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
API_BASE  = "https://api.api4com.com/api/v1"
RAMAL     = os.getenv("RAMAL", "1005")

CALL_DELAY        = float(os.getenv("CALL_DELAY", "0.5"))
MIN_HUMAN_DURATION = int(os.getenv("MIN_HUMAN_DURATION", "8"))

# Timeout máximo aguardando resultado de uma chamada (segundos)
CALL_TIMEOUT = int(os.getenv("CALL_TIMEOUT", "45"))

# Intervalo de polling de status (segundos) — menor = mais rápido, mais requisições
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.5"))

HEADERS = {
    "Authorization": API_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# ── Estado global ─────────────────────────────────────────────────────────────
campaign = {
    "running": False,
    "paused": False,
    "skip_current": False,   # sinaliza para pular o número/lead atual
    "leads": [],
    "results": [],
    "current": [],
    "answered": None,
    "last_error": None,
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


# ── Upload ────────────────────────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400

    file    = request.files["file"]
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
        campaign["leads"]       = leads
        campaign["results"]     = []
        campaign["stats"]       = {k: 0 for k in ("answered","cancelled","voicemail","invalid","error","total")}
        campaign["answered"]    = None
        campaign["running"]     = False
        campaign["paused"]      = False
        campaign["skip_current"]= False
        campaign["current"]     = []
        campaign["last_error"]  = None

    logger.info(f"Upload OK | leads={len(leads)} | colunas={list(df.columns)}")

    return jsonify({
        "ok": True,
        "total": len(leads),
        "count": len(leads),
        "preview": [{"name": l["name"], "phones": l["phones"]} for l in leads[:5]],
    })


# ── Iniciar ───────────────────────────────────────────────────────────────────
@app.route("/api/start", methods=["POST"])
def start():
    global CALL_DELAY, MIN_HUMAN_DURATION, CALL_TIMEOUT, POLL_INTERVAL

    with campaign_lock:
        if not campaign["leads"]:
            return jsonify({"error": "Carregue uma planilha primeiro"}), 400
        if campaign["running"]:
            return jsonify({"error": "Campanha já está rodando"}), 400

    data = request.get_json(silent=True) or {}

    try:
        if "call_delay"          in data: CALL_DELAY         = max(0.0,  float(data["call_delay"]))
        if "min_human_duration"  in data: MIN_HUMAN_DURATION = max(1,    int(data["min_human_duration"]))
        if "call_timeout"        in data: CALL_TIMEOUT       = max(10,   int(data["call_timeout"]))
        if "poll_interval"       in data: POLL_INTERVAL      = max(0.5,  float(data["poll_interval"]))
    except Exception as e:
        return jsonify({"error": f"Parâmetro inválido: {e}"}), 400

    with campaign_lock:
        campaign["running"]      = True
        campaign["paused"]       = False
        campaign["skip_current"] = False
        campaign["answered"]     = None
        campaign["last_error"]   = None
        campaign["current"]      = []
        for lead in campaign["leads"]:
            lead["done"] = False

    logger.info(f"Iniciando | leads={len(campaign['leads'])} delay={CALL_DELAY} timeout={CALL_TIMEOUT} poll={POLL_INTERVAL} human_min={MIN_HUMAN_DURATION}")

    threading.Thread(target=_run_campaign_safe, daemon=True).start()

    return jsonify({
        "ok": True,
        "call_delay":         CALL_DELAY,
        "min_human_duration": MIN_HUMAN_DURATION,
        "call_timeout":       CALL_TIMEOUT,
        "poll_interval":      POLL_INTERVAL,
        "total":              len(campaign["leads"]),
    })


@app.route("/api/resume", methods=["POST"])
def resume():
    with campaign_lock:
        campaign["paused"]       = False
        campaign["answered"]     = None
        campaign["skip_current"] = False
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def stop():
    with campaign_lock:
        campaign["running"]      = False
        campaign["paused"]       = False
        campaign["answered"]     = None
        campaign["skip_current"] = False
        campaign["current"]      = []
    return jsonify({"ok": True})


# ── Pular número/lead atual ───────────────────────────────────────────────────
@app.route("/api/skip", methods=["POST"])
def skip():
    """
    Força a thread a abandonar a chamada/lead atual e avançar para o próximo.
    """
    with campaign_lock:
        if not campaign["running"]:
            return jsonify({"error": "Campanha não está rodando"}), 400
        campaign["skip_current"] = True
    logger.info("Skip solicitado pelo usuário")
    return jsonify({"ok": True})


# ── Estado ────────────────────────────────────────────────────────────────────
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
        remaining = len([l for l in campaign["leads"] if not l.get("done")])

        return jsonify({
            "status":             status,
            "answered":           answered,
            "current":            campaign["current"],
            "stats":              campaign["stats"],
            "results":            campaign["results"][-50:],
            "remaining":          remaining,
            "total":              len(campaign["leads"]),
            "call_delay":         CALL_DELAY,
            "min_human_duration": MIN_HUMAN_DURATION,
            "call_timeout":       CALL_TIMEOUT,
            "poll_interval":      POLL_INTERVAL,
            "last_error":         campaign["last_error"],
        })


# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True) or {}
        logger.info(f"Webhook: {data}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/webhook", methods=["GET"])
def webhook_health():
    return jsonify({"ok": True, "message": "Webhook ativo"})


# ── Motor da campanha ─────────────────────────────────────────────────────────
def _run_campaign_safe():
    try:
        _run_campaign_sync()
    except Exception as e:
        logger.exception("Erro fatal na thread da campanha")
        with campaign_lock:
            campaign["running"]  = False
            campaign["paused"]   = False
            campaign["current"]  = []
            campaign["last_error"] = str(e)


def _run_campaign_sync():
    with campaign_lock:
        leads = list(campaign["leads"])

    for idx, lead in enumerate(leads, 1):
        with campaign_lock:
            if not campaign["running"]:
                break

        if lead.get("done"):
            continue

        _wait_unpause()

        with campaign_lock:
            if not campaign["running"]:
                break
            campaign["current"]      = [f"{lead['name']} ({idx}/{len(leads)})"]
            campaign["skip_current"] = False

        result = _dial_lead_sequential(lead, idx, len(leads))

        with campaign_lock:
            campaign["current"] = []

        if result == "answered":
            with campaign_lock:
                campaign["paused"] = True

            _wait_unpause()

        with campaign_lock:
            still_running = campaign["running"]

        if still_running and CALL_DELAY > 0:
            time.sleep(CALL_DELAY)

    with campaign_lock:
        campaign["running"] = False
        campaign["current"] = []

    logger.info("Campanha finalizada")


def _wait_unpause():
    while True:
        with campaign_lock:
            paused  = campaign["paused"]
            running = campaign["running"]
        if not running or not paused:
            break
        time.sleep(0.1)


def _dial_lead_sequential(lead: dict, idx: int, total: int) -> str:
    for phone in lead["phones"]:
        with campaign_lock:
            if not campaign["running"]:
                return "stopped"
            if campaign["skip_current"]:
                # pular o lead inteiro
                campaign["skip_current"] = False
                lead["done"] = True
                logger.info(f"[{lead['name']}] lead pulado pelo usuário")
                return "skipped"

        _wait_unpause()

        with campaign_lock:
            if not campaign["running"]:
                return "stopped"
            campaign["current"]      = [f"{lead['name']} — {phone} ({idx}/{total})"]
            campaign["skip_current"] = False

        logger.info(f"[{lead['name']}] discando {phone}")
        _add_result(lead["name"], phone, "discando")

        call_id = _originate(phone, lead["name"])

        if not call_id:
            _update_result(lead["name"], phone, "error")
            with campaign_lock:
                campaign["stats"]["error"] += 1
                campaign["stats"]["total"] += 1
            if CALL_DELAY > 0:
                time.sleep(CALL_DELAY)
            continue

        result = _wait_result(phone, lead["name"], call_id)
        _update_result(lead["name"], phone, result)

        with campaign_lock:
            campaign["stats"]["total"] += 1
            if result in campaign["stats"]:
                campaign["stats"][result] += 1

        logger.info(f"[{lead['name']}] {phone} → {result}")

        if result == "answered":
            lead["done"] = True
            with campaign_lock:
                campaign["answered"] = {"name": lead["name"], "phone": phone}
            return "answered"

        if result == "skipped":
            lead["done"] = True
            return "skipped"

        if result == "invalid":
            time.sleep(0.05)
        elif result in ("voicemail", "cancelled", "error"):
            if CALL_DELAY > 0:
                time.sleep(CALL_DELAY)

    lead["done"] = True
    return "finished_lead"


# ── Originar chamada ──────────────────────────────────────────────────────────
def _originate(phone: str, name: str) -> str | None:
    phone = _fmt_phone(phone)
    payload = {
        "extension": RAMAL,
        "phone":     phone,
        "metadata":  {"lead_name": name, "source": "discador_cloud"},
    }

    try:
        logger.info(f"POST /dialer | {payload}")
        r = http.post(f"{API_BASE}/dialer", headers=HEADERS, json=payload, timeout=20)
        logger.info(f"originate → {r.status_code} {r.text[:500]}")
        r.raise_for_status()
        data = r.json()
        return str(data.get("id") or data.get("call_id") or "")
    except Exception:
        logger.exception(f"Erro ao originar {phone}")
        return None


# ── Aguardar resultado da chamada (mais rápido + suporte a skip) ──────────────
def _wait_result(phone: str, name: str, call_id: str) -> str:
    """
    Polling com POLL_INTERVAL entre tentativas.
    Sai assim que encontra resultado, skip solicitado, ou CALL_TIMEOUT atingido.

    Estratégia de busca:
    1. Procura na lista /calls pelo call_id ou número exato.
    2. Se não achar em 3 ciclos seguidos após ~10s, assume que a chamada
       já foi processada e encerrada sem registro → cancelled.
    """
    phone_fmt      = _fmt_phone(phone)
    elapsed        = 0.0
    not_found_streak = 0

    while elapsed < CALL_TIMEOUT:
        with campaign_lock:
            if not campaign["running"]:
                return "cancelled"
            if campaign["skip_current"]:
                campaign["skip_current"] = False
                logger.info(f"[{name}] chamada pulada pelo usuário (skip durante wait)")
                return "skipped"

        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        try:
            r = http.get(
                f"{API_BASE}/calls",
                headers=HEADERS,
                params={"page": 1},
                timeout=10
            )

            if not r.ok:
                logger.warning(f"GET /calls → {r.status_code}")
                not_found_streak += 1
                continue

            data  = r.json()
            calls = data.get("data", []) if isinstance(data, dict) else []

            found = False
            for c in calls:
                to_num   = _fmt_phone(c.get("to", ""))
                c_id     = str(c.get("id") or c.get("call_id") or "")
                matches  = (call_id and c_id == call_id) or (to_num == phone_fmt)

                if not matches:
                    continue

                found    = True
                cause    = (c.get("hangup_cause") or c.get("hangupCause") or "").upper().strip()
                duration = int(c.get("duration") or 0)
                status_r = c.get("status", "").lower()

                # Chamada ainda ativa / tocando
                if not cause and duration == 0 and status_r in ("", "ringing", "active", "in_progress"):
                    not_found_streak = 0
                    continue

                result = _final_call_status(cause, duration)
                logger.info(
                    f"[{name}] phone={phone_fmt} call_id={call_id} "
                    f"cause={cause} duration={duration}s → {result}"
                )
                return result

            if not found:
                not_found_streak += 1
                # Se passou tempo suficiente e a chamada sumiu da lista,
                # provavelmente já terminou (ex.: ocupado rápido)
                if elapsed > 8 and not_found_streak >= 3:
                    logger.info(f"[{name}] chamada não encontrada após {elapsed:.0f}s → cancelled")
                    return "cancelled"
            else:
                not_found_streak = 0

        except Exception as e:
            logger.warning(f"Erro polling {phone_fmt}: {e}")
            not_found_streak += 1

    logger.info(f"[{name}] timeout {CALL_TIMEOUT}s → cancelled")
    return "cancelled"


# ── Classificar resultado ─────────────────────────────────────────────────────
def _final_call_status(cause: str, duration: int) -> str:
    cause_u = (cause or "").upper().strip()

    INVALID_CAUSES = {
        "UNALLOCATED_NUMBER", "INVALID_NUMBER_FORMAT", "USER_NOT_REGISTERED",
        "NAO FOI POSSIVEL COMPLETAR", "NAO FOI POSSIVEL", "NUMERO INVALIDO",
        "NO_ROUTE_DESTINATION", "NO_ROUTE_TRANSIT_IO",
    }
    VOICEMAIL_CAUSES = {
        "VOICEMAIL", "CAIXA POSTAL",
    }
    CANCELLED_CAUSES = {
        "ORIGINATOR_CANCEL", "NO_ANSWER", "USER_BUSY", "CALL_REJECTED",
        "SUBSCRIBER_ABSENT", "CANCELADA", "NORMAL_CLEARING",
    }

    if cause_u in INVALID_CAUSES:
        return "invalid"

    if cause_u in VOICEMAIL_CAUSES:
        return "voicemail"

    if cause_u in CANCELLED_CAUSES:
        # Duração 0 + NORMAL_CLEARING = desligou antes de atender
        if cause_u == "NORMAL_CLEARING" and duration >= MIN_HUMAN_DURATION:
            return "answered"
        return "cancelled"

    # Duração < mínimo → provavelmente URA/caixa postal
    if 0 < duration < MIN_HUMAN_DURATION:
        return "voicemail"

    if duration >= MIN_HUMAN_DURATION:
        return "answered"

    return "cancelled"


# ── Helpers ───────────────────────────────────────────────────────────────────
def _add_result(name, phone, status):
    with campaign_lock:
        campaign["results"].append({
            "id":     str(uuid.uuid4())[:8],
            "name":   name,
            "phone":  phone,
            "status": status,
            "ts":     time.strftime("%H:%M:%S"),
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
    import unicodedata, re
    col = unicodedata.normalize("NFKD", str(col).strip().lower())
    col = "".join(c for c in col if not unicodedata.combining(c))
    return re.sub(r"\s+", "_", col)


def _parse_leads(df) -> list:
    import re

    NAME_COLS  = ["nome", "name", "contato", "cliente"]
    PHONE_COLS = [
        "fone1","fone2","fone3","fone4","fone5","fone6","fone7","fone8",
        "telefone1","telefone2","telefone3","telefone4","telefone5","telefone6","telefone7","telefone8",
        "telefone","celular","tel1","tel2","tel3","tel4","tel5","tel6","tel7","tel8","phone",
    ]

    leads = []
    for idx, row in df.iterrows():
        name = next(
            (str(row[c]).strip() for c in NAME_COLS if c in row and pd.notna(row[c]) and str(row[c]).strip()),
            f"Lead {idx + 1}"
        )

        phones = []
        for c in PHONE_COLS:
            if c not in row:
                continue
            val = str(row[c]).strip() if pd.notna(row[c]) else ""
            d   = re.sub(r"\D", "", val)
            if d.startswith("55") and len(d) > 11:
                d = d[2:]
            if len(d) == 12 and d.startswith("0"):
                d = d[1:]
            if 10 <= len(d) <= 11 and d not in phones:
                phones.append(d)

        if phones:
            leads.append({"name": name, "phones": phones, "done": False})

    return leads


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
