from WPP_Whatsapp import Create
import threading
import logging
import asyncio
import sys
import time
import os
import base64
import uuid
import PlaywrightSafeThread.browser.threadsafe_browser as pst


def patched_pt_sleep(self, val, timeout_=None):
    try:
        time.sleep(val)
    except Exception:
        time.sleep(val)


pst.ThreadsafeBrowser.sleep = patched_pt_sleep

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

logger = logging.getLogger(__name__)


class WhatsAppService:
    def __init__(self):
        self.clients = {}
        self.qr_codes = {}
        self.status = {}
        self.threads = {}
        self.starting_sessions = set()
        self.starting_sessions_time = {}
        self.processed_messages = {}
        self.group_cache = {}
        self.group_cache_time = {}
        self._start_watchdog()
        logger.info("WhatsAppService pronto.")

    def _start_watchdog(self):
        def watchdog():
            while True:
                time.sleep(60)
                try:
                    for cid in list(self.status.keys()):
                        current_status = self.status.get(cid)
                        if current_status in ["DISCONNECTED", "ERROR", "serverClose", "browserClose"] and cid not in self.starting_sessions:
                            logger.info(f"[Watchdog] Reconectando WhatsApp da empresa {cid}.")
                            self.start_session(cid)
                except Exception as e:
                    logger.error(f"[Watchdog] Erro no loop: {e}")

        threading.Thread(target=watchdog, daemon=True, name="WA_Watchdog").start()

    def notify_merchant_new_sale(self, company_id, sale_id, details):
        def _notify():
            merchant_number = details.get("merchant_number")
            if not merchant_number:
                return

            msg = (
                f"*NOVO PEDIDO RECEBIDO!* (# {sale_id})\n\n"
                f"*Cliente:* {details.get('customer_name')}\n"
                f"*Produto:* {details.get('product_name')}\n"
                f"*Total:* R$ {details.get('total'):.2f}\n"
                f"*Pagamento:* {details.get('payment_method')}\n"
                f"*Entrega:* {'Retirada' if details.get('delivery_type') == 'pickup' else 'Delivery'}\n\n"
                "Acesse o painel para processar o pedido."
            )

            self.send_message(company_id, merchant_number, msg)

        threading.Thread(target=_notify).start()

    def notify_merchant_out_of_stock(self, company_id, product_name, variation_size):
        def _notify():
            from database import SessionLocal, Company

            with SessionLocal() as db:
                company = db.query(Company).filter(Company.id == company_id).first()
                if not company or not company.whatsapp_number:
                    return

                msg = (
                    "*ALERTA DE ESTOQUE ZERO*\n\n"
                    f"O produto *{product_name}* (Tamanho {variation_size}) acabou de esgotar via WhatsApp."
                )
                self.send_message(company_id, company.whatsapp_number, msg)

        threading.Thread(target=_notify).start()

    def get_status(self, company_id):
        return {
            "status": self.status.get(company_id, "DISCONNECTED"),
            "qr_code": self.qr_codes.get(company_id),
        }

    def _normalize_jid(self, wa_id):
        jid = str(wa_id or "").strip()
        if not jid:
            return ""
        if "@" in jid:
            return jid
        digits = "".join(ch for ch in jid if ch.isdigit())
        return f"{digits}@c.us" if digits else jid

    def _extract_location_url(self, message):
        if not isinstance(message, dict):
            return None

        def first_value(data, *keys):
            for key in keys:
                value = data.get(key)
                if value is not None and value != "":
                    return value
            return None

        def maps_url(lat, lng):
            if lat is None or lng is None:
                return None
            return f"https://maps.google.com/?q={lat},{lng}"

        loc = message.get("location") or message.get("loc")
        if isinstance(loc, dict):
            url = first_value(loc, "url", "href", "link")
            if url:
                return str(url).strip()
            lat = first_value(loc, "latitude", "lat")
            lng = first_value(loc, "longitude", "lng", "lon")
            url = maps_url(lat, lng)
            if url:
                return url

        lat = first_value(message, "latitude", "lat")
        lng = first_value(message, "longitude", "lng", "lon")
        url = maps_url(lat, lng)
        if url:
            return url

        for key in ("body", "caption", "text", "url"):
            value = message.get(key)
            if isinstance(value, str):
                clean = value.strip()
                lowered = clean.lower()
                if "maps.google" in lowered or "goo.gl/maps" in lowered or "waze.com" in lowered:
                    return clean
        return None

    def clean_session(self, company_id):
        import shutil

        session_path = os.path.join(os.getcwd(), "tokens", f"session_empresa_{company_id}")
        if os.path.exists(session_path):
            try:
                shutil.rmtree(session_path)
                logger.info(f"[WA {company_id}] Sessao antiga removida.")
            except Exception as e:
                logger.warning(f"[WA {company_id}] Falha ao remover sessao: {e}")

        self.clients.pop(company_id, None)
        self.qr_codes.pop(company_id, None)
        self.status.pop(company_id, None)
        if company_id in self.starting_sessions:
            self.starting_sessions.remove(company_id)
        self.starting_sessions_time.pop(company_id, None)
        self.processed_messages.pop(company_id, None)

    def start_session(self, company_id, force_new=False):
        if company_id in self.starting_sessions:
            start_time = self.starting_sessions_time.get(company_id, 0)
            if time.time() - start_time > 300:
                self.starting_sessions.remove(company_id)
            else:
                return

        current_status = self.status.get(company_id)
        if current_status in ["CONNECTED", "isLogged"]:
            return

        if force_new or current_status in ["ERROR", "disconnectedMobile", "browserClose", "serverClose", "deleteToken"]:
            self.clean_session(company_id)

        self.starting_sessions.add(company_id)
        self.starting_sessions_time[company_id] = time.time()
        self.status[company_id] = "STARTING"
        self.qr_codes[company_id] = None

        def catch_qr(qrCode, asciiQR, attempt, urlCode):
            self.qr_codes[company_id] = qrCode
            self.status[company_id] = "WAITING_QR"

        def check_status(statusSession, session):
            logger.info(f"[WA {company_id}] Status server: {statusSession}")
            self.status[company_id] = statusSession
            if statusSession in ["CONNECTED", "isLogged", "inChat"]:
                self.qr_codes[company_id] = None
                self.status[company_id] = "CONNECTED"

        def handle_message(message):
            try:
                if not isinstance(message, dict):
                    if hasattr(message, "__dict__"):
                        message = vars(message)
                    else:
                        return

                from_me = False
                if message.get("fromMe") is True or str(message.get("fromMe")).lower() == "true":
                    from_me = True

                msg_id = message.get("id")
                if isinstance(msg_id, dict):
                    if msg_id.get("fromMe") is True or str(msg_id.get("fromMe")).lower() == "true":
                        from_me = True
                elif isinstance(msg_id, str) and msg_id.startswith("true_"):
                    from_me = True

                if from_me or message.get("isGroupMsg"):
                    return

                from_num = message.get("from")
                if not from_num or "status" in from_num or "@newsletter" in from_num:
                    return

                full_jid = from_num
                celular = from_num.split("@")[0]
                texto = message.get("body", "")
                location_url = self._extract_location_url(message)
                msg_type = str(message.get("type", "")).lower()

                if location_url and (msg_type == "location" or not texto or not isinstance(texto, str)):
                    texto = f"LOCALIZACAO_WHATSAPP {location_url}"
                if not isinstance(texto, str):
                    texto = str(texto or "")
                if not texto.strip():
                    return

                msg_id_serialized = msg_id.get("_serialized") if isinstance(msg_id, dict) else msg_id
                if not msg_id_serialized:
                    msg_id_serialized = f"{celular}_{int(time.time())}"

                now_ts = time.time()
                if company_id not in self.processed_messages:
                    self.processed_messages[company_id] = {}
                if len(self.processed_messages[company_id]) > 100:
                    self.processed_messages[company_id] = {
                        key: ts
                        for key, ts in self.processed_messages[company_id].items()
                        if now_ts - ts < 600
                    }
                if msg_id_serialized in self.processed_messages[company_id]:
                    return
                self.processed_messages[company_id][msg_id_serialized] = now_ts

                logger.info(f"[WA {company_id}] Recebido de {full_jid}: {texto}")

                t_init = time.perf_counter()
                resultado = None
                from database import SessionLocal
                from ai_logic import process_message

                with SessionLocal() as db:
                    resultado = process_message(client_phone=celular, message=texto, db=db, company_id=company_id)

                t_logic = time.perf_counter() - t_init
                logger.info(f"[WA {company_id}] Logica respondeu em {t_logic:.2f}s")

                if resultado:
                    if isinstance(resultado, str):
                        resultado = {"text": resultado}

                    if isinstance(resultado, dict):
                        text_to_send = resultado.get("text", "")
                        img_base64 = resultado.get("image_base64")
                        image_list = resultado.get("image_list")

                        if image_list and isinstance(image_list, list):
                            if text_to_send:
                                self.send_message(company_id, full_jid, text_to_send)
                            for item in image_list:
                                img = item.get("image")
                                caption = item.get("text", "")
                                if img:
                                    self.send_image(company_id, full_jid, img, caption)
                                    time.sleep(0.5)
                        elif text_to_send and "|SPLIT|" in text_to_send:
                            parts = [p.strip() for p in text_to_send.split("|SPLIT|") if p.strip()]
                            for i, part in enumerate(parts):
                                if i == 0 and img_base64:
                                    self.send_image(company_id, full_jid, img_base64, part)
                                else:
                                    self.send_message(company_id, full_jid, part)
                                if i < len(parts) - 1:
                                    time.sleep(0.4)
                        else:
                            if img_base64:
                                self.send_image(company_id, full_jid, img_base64, text_to_send)
                            elif text_to_send:
                                self.send_message(company_id, full_jid, text_to_send)

                t_total = time.perf_counter() - t_init
                logger.info(f"[WA {company_id}] Ciclo total: {t_total:.2f}s")
            except Exception as e:
                logger.error(f"Erro handle_message WA {company_id}: {e}")

        def launch():
            try:
                is_linux = os.name != "nt"
                if not is_linux:
                    try:
                        asyncio.get_event_loop()
                    except Exception:
                        asyncio.set_event_loop(asyncio.new_event_loop())

                browser_args = [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-extensions",
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1920,1080",
                    "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                ]

                launch_kwargs = {
                    "session": f"session_empresa_{company_id}",
                    "catchQR": catch_qr,
                    "statusFind": check_status,
                    "headless": True,
                    "browser_args": browser_args,
                    "autoClose": 0,
                    "useLid": True,
                    "disableWelcome": True,
                    "updatesLog": False,
                }

                if is_linux:
                    launch_kwargs["executable_path"] = "/usr/bin/google-chrome-stable"
                    launch_kwargs["install"] = False

                try:
                    session_path = os.path.join(os.getcwd(), "tokens", f"session_empresa_{company_id}")
                    if os.path.exists(session_path):
                        for lock in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
                            lock_path = os.path.join(session_path, lock)
                            if os.path.exists(lock_path):
                                os.remove(lock_path)
                except Exception:
                    pass

                creator = Create(**launch_kwargs)
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    try:
                        client = executor.submit(creator.start).result(timeout=180)
                        if client:
                            self.clients[company_id] = client
                            self.status[company_id] = "CONNECTED"
                            client.onMessage(handle_message)
                            client.onStateChange(lambda s: self._handle_state_change(company_id, s))

                            while company_id in self.clients:
                                if creator.state in ["ERROR", "browserClose", "serverClose", "deleteToken"]:
                                    self.status[company_id] = "DISCONNECTED"
                                    self.clients.pop(company_id, None)
                                    break
                                time.sleep(5)
                    except Exception as e:
                        logger.error(f"[WA {company_id}] Falha no start: {e}")
                        self.status[company_id] = "ERROR"
            finally:
                if company_id in self.starting_sessions:
                    self.starting_sessions.remove(company_id)

        threading.Thread(target=launch, daemon=True, name=f"WA_Launch_{company_id}").start()

    def _handle_state_change(self, company_id, state):
        logger.info(f"[WA {company_id}] Mudanca de estado: {state}")
        self.status[company_id] = state
        if state in ["DISCONNECTED", "CONFLICT", "UNPAIRED", "UNLAUNCHED"]:
            logger.warning(f"[WA {company_id}] Conexao instavel. Status: {state}")
            if state in ["CONFLICT", "UNPAIRED"]:
                self.clean_session(company_id)
            else:
                self.status[company_id] = "DISCONNECTED"
                self.clients.pop(company_id, None)

    def send_message(self, company_id, wa_id, text):
        client = self.clients.get(company_id)
        if not client or self.status.get(company_id) != "CONNECTED":
            return False

        jid = self._normalize_jid(wa_id)
        if not jid or not text:
            return False

        logger.info(f"[WA {company_id}] Enviando texto para {jid}")
        try:
            client.setPresence("composing", jid)
        except Exception:
            try:
                client.startTyping(jid)
            except Exception:
                pass

        for attempt in range(2):
            try:
                client.sendText(jid, text)
                logger.info(f"[WA {company_id}] Texto enviado para {jid}")
                return True
            except Exception as e:
                err_msg = str(e)
                if "No LID" in err_msg and attempt == 0:
                    logger.warning(f"[WA {company_id}] Erro LID em {jid}; sincronizando contato.")
                    try:
                        sync_script = f"async () => {{ await WPP.contact.asyncSyncContact('{jid}'); }}"
                        client.page.evaluate(sync_script)
                        time.sleep(0.8)
                    except Exception:
                        pass
                    continue

                logger.error(f"[WA {company_id}] Erro texto ({attempt + 1}/2): {err_msg}")
                if "No LID" in err_msg or "Page.evaluate" in err_msg:
                    return False
                return False
        return False

    def get_groups(self, company_id, force_refresh=False):
        client = self.clients.get(company_id)
        if not client or self.status.get(company_id) != "CONNECTED":
            return self.group_cache.get(company_id, [])

        now = time.time()
        if not force_refresh and (now - self.group_cache_time.get(company_id, 0)) < 600:
            return self.group_cache.get(company_id, [])

        try:
            logger.info(f"[WA {company_id}] Buscando grupos.")
            groups = client.getAllGroups()
            res = []
            for g in groups:
                name = "Grupo"
                jid = ""

                if isinstance(g, dict):
                    name = g.get("name") or g.get("contact", {}).get("name") or "Grupo"
                    jid_obj = g.get("id")
                    if isinstance(jid_obj, dict):
                        jid = jid_obj.get("_serialized") or jid_obj.get("server")
                    else:
                        jid = str(jid_obj) if jid_obj else ""
                else:
                    name = getattr(g, "name", "Grupo")
                    jid_obj = getattr(g, "id", "")
                    if hasattr(jid_obj, "_serialized"):
                        jid = jid_obj._serialized
                    elif hasattr(jid_obj, "user"):
                        jid = f"{jid_obj.user}@{jid_obj.server}"
                    else:
                        jid = str(jid_obj)

                if jid and "@g.us" in jid:
                    res.append({"name": name, "jid": jid})

            logger.info(f"[WA {company_id}] {len(res)} grupos localizados.")
            self.group_cache[company_id] = res
            self.group_cache_time[company_id] = now
            return res
        except Exception as e:
            logger.warning(f"[WA {company_id}] Falha ao buscar grupos: {e}")
            return self.group_cache.get(company_id, [])

    def send_image(self, company_id, wa_id, base64_data, caption=""):
        client = self.clients.get(company_id)
        if not client or self.status.get(company_id) != "CONNECTED":
            return False
        if not base64_data:
            return False

        jid = self._normalize_jid(wa_id)
        if not jid:
            return False

        temp_file = None
        try:
            try:
                client.page.evaluate(f"async () => {{ try {{ await WPP.contact.getContact('{jid}'); }} catch(e) {{}} }}")
                client.setPresence("composing", jid)
                time.sleep(0.3)
            except Exception:
                pass

            clean_b64 = base64_data.split(",")[1] if "," in base64_data else base64_data
            temp_dir = os.path.join(os.getcwd(), "tokens", "temp_images")
            os.makedirs(temp_dir, exist_ok=True)
            temp_file = os.path.join(temp_dir, f"img_{company_id}_{uuid.uuid4().hex}.png")
            with open(temp_file, "wb") as f:
                f.write(base64.b64decode(clean_b64))

            for attempt in range(2):
                try:
                    client.sendImage(jid, os.path.abspath(temp_file), "image.png", caption or "")
                    logger.info(f"[WA {company_id}] Imagem enviada para {jid}")
                    return True
                except Exception as e:
                    logger.warning(f"[WA {company_id}] Falha imagem ({attempt + 1}/2) para {jid}: {e}")
                    if attempt == 0:
                        time.sleep(0.8)
            return False
        except Exception as e:
            logger.error(f"[WA {company_id}] Erro imagem: {e}")
            return False
        finally:
            if temp_file:
                try:
                    os.remove(temp_file)
                except Exception:
                    pass

    def send_status_image(self, company_id, base64_data, caption=""):
        client = self.clients.get(company_id)
        if not client or self.status.get(company_id) != "CONNECTED":
            return False
        if not base64_data:
            return False

        temp_file = None
        try:
            clean_b64 = base64_data.split(",")[1] if "," in base64_data else base64_data
            temp_dir = os.path.join(os.getcwd(), "tokens", "temp_images")
            os.makedirs(temp_dir, exist_ok=True)
            temp_file = os.path.join(temp_dir, f"status_{company_id}_{uuid.uuid4().hex}.png")
            with open(temp_file, "wb") as f:
                f.write(base64.b64decode(clean_b64))

            client.sendImage("status@broadcast", os.path.abspath(temp_file), "status.png", caption or "")
            logger.info(f"[WA {company_id}] Publicado no Status.")
            return True
        except Exception as e:
            logger.error(f"[WA {company_id}] Erro Status: {e}")
            return False
        finally:
            if temp_file:
                try:
                    os.remove(temp_file)
                except Exception:
                    pass


whatsapp_manager = WhatsAppService()
