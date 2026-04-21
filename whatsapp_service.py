from WPP_Whatsapp import Create
import threading
import logging
import asyncio
import sys
import time
import os
import base64
import PlaywrightSafeThread.browser.threadsafe_browser as pst

# Fix para Python 3.13: Resolve o erro InvalidStateError no sleep da biblioteca PlaywrightSafeThread
def patched_pt_sleep(self, val, timeout_=None):
    try:
        time.sleep(val)
    except:
        time.sleep(val)

pst.ThreadsafeBrowser.sleep = patched_pt_sleep

# Fix para Windows: força o ProactorEventLoop globalmente (obrigatório para Playwright)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

logger = logging.getLogger(__name__)

class WhatsAppService:
    def __init__(self):
        self.clients = {}     # company_id -> client instance
        self.qr_codes = {}    # company_id -> base64 qr code
        self.status = {}      # company_id -> string status
        self.threads = {}     # company_id -> launch thread
        self.starting_sessions = set()      # Trava de concorrência para inicialização
        self.starting_sessions_time = {}   # company_id -> timestamp da última inicialização
        self.processed_messages = {}       # company_id -> {msg_id -> timestamp}
        self.group_cache = {}              # company_id -> list of groups
        self.group_cache_time = {}         # company_id -> timestamp of last fetch
        self._start_watchdog()
        logger.info("✅ WhatsAppService Pronto (Versão Estabilidade Blindada).")
        
    def _start_watchdog(self):
        """Inicia um monitor de saúde das conexões em um thread separado."""
        def watchdog():
            while True:
                time.sleep(60) # Verifica a cada minuto
                try:
                    for cid in list(self.status.keys()):
                        current_status = self.status.get(cid)
                        # Se estiver desconectado mas não estamos em processo de início, tenta reconectar
                        if current_status in ["DISCONNECTED", "ERROR", "serverClose", "browserClose"] and cid not in self.starting_sessions:
                            logger.info(f"🐕 [Watchdog] Detectada queda na empresa {cid}. Tentando reconectar...")
                            self.start_session(cid)
                except Exception as e:
                    logger.error(f"❌ [Watchdog] Erro no loop: {e}")

        threading.Thread(target=watchdog, daemon=True, name="WA_Watchdog").start()
        
    def notify_merchant_new_sale(self, company_id, sale_id, details):
        """Notifica o dono da loja sobre uma nova venda pendente."""
        client = self.clients.get(company_id)
        if not client or self.status.get(company_id) != "CONNECTED":
            return
            
        try:
            merchant_number = details.get("merchant_number")
            if not merchant_number: return
            
            msg = (f"💰 *NOVO PEDIDO RECEBIDO!* 💰\n\n"
                   f"🆔 *Pedido:* #{sale_id}\n"
                   f"👤 *Cliente:* {details.get('customer_name')}\n"
                   f"🛍️ *Item:* {details.get('product_name')}\n"
                   f"💵 *Total:* R$ {details.get('total'):.2f}\n"
                   f"💳 *Pagamento:* {details.get('payment_method')}\n"
                   f"🚚 *Tipo:* {details.get('delivery_type')}\n\n"
                   f"⚠️ *Status:* PENDENTE\n"
                   f"Acesse seu painel para confirmar o pagamento!")
            
            self.send_message(company_id, merchant_number, msg)
        except Exception as e:
            logger.error(f"❌ [WA {company_id}] Falha ao notificar mercador: {e}")

    def get_status(self, company_id):
        return {
            "status": self.status.get(company_id, "DISCONNECTED"),
            "qr_code": self.qr_codes.get(company_id)
        }

    def clean_session(self, company_id):
        """Remove os arquivos de sessão antigos para forçar um novo QR Code"""
        import os, shutil
        session_path = os.path.join(os.getcwd(), "tokens", f"session_empresa_{company_id}")
        if os.path.exists(session_path):
            try:
                shutil.rmtree(session_path)
                logger.info(f"🧹 [WA {company_id}] Sessão antiga removida com sucesso.")
            except Exception as e:
                logger.warning(f"⚠️ [WA {company_id}] Falha ao remover sessão: {e}")
        # Limpa referências no memory
        self.clients.pop(company_id, None)
        self.qr_codes.pop(company_id, None)
        self.status.pop(company_id, None)
        if company_id in self.starting_sessions:
            self.starting_sessions.remove(company_id)
        self.starting_sessions_time.pop(company_id, None)
        self.processed_messages.pop(company_id, None)

    def start_session(self, company_id, force_new=False):
        import time
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
            logger.info(f"Status WA Server {company_id}: {statusSession}")
            self.status[company_id] = statusSession
            if statusSession in ["CONNECTED", "isLogged", "inChat"]:
                self.qr_codes[company_id] = None
                self.status[company_id] = "CONNECTED"

        def handle_message(message):
            try:
                if not isinstance(message, dict):
                    if hasattr(message, '__dict__'): message = vars(message)
                    else: return
                
                from_me = False
                if message.get("fromMe") is True or str(message.get("fromMe")).lower() == "true": from_me = True
                msg_id = message.get("id")
                if isinstance(msg_id, dict):
                    if msg_id.get("fromMe") is True or str(msg_id.get("fromMe")).lower() == "true": from_me = True
                elif isinstance(msg_id, str) and msg_id.startswith("true_"): from_me = True
                
                if from_me or message.get("isGroupMsg"): return

                from_num = message.get("from")
                if not from_num or "status" in from_num or "@newsletter" in from_num: return

                full_jid = from_num
                celular = from_num.split("@")[0]
                texto = message.get("body", "")
                if not texto or not isinstance(texto, str): return

                msg_id_serialized = msg_id.get("_serialized") if isinstance(msg_id, dict) else msg_id
                if not msg_id_serialized: msg_id_serialized = f"{celular}_{int(time.time())}"

                now_ts = time.time()
                if company_id not in self.processed_messages: self.processed_messages[company_id] = {}
                if len(self.processed_messages[company_id]) > 100:
                    self.processed_messages[company_id] = {k: v for k, v in self.processed_messages[company_id].items() if now_ts - v < 600}

                if msg_id_serialized in self.processed_messages[company_id]: return
                self.processed_messages[company_id][msg_id_serialized] = now_ts

                logger.info(f"📥 Recebido WA [Empresa {company_id}] de {full_jid}: {texto}")

                from database import SessionLocal
                from ai_logic import process_message
                db = SessionLocal()
                try:
                    resultado = process_message(client_phone=celular, message=texto, db=db, company_id=company_id)
                    if isinstance(resultado, dict):
                        text_to_send = resultado.get("text", "")
                        img_base64 = resultado.get("image_base64")
                        image_list = resultado.get("image_list")
                        
                        if image_list and isinstance(image_list, list):
                            if text_to_send: self.send_message(company_id, full_jid, text_to_send)
                            for item in image_list:
                                img = item.get("image")
                                cap = item.get("text", "")
                                if img:
                                    self.send_image(company_id, full_jid, img, cap)
                                    time.sleep(1.5)
                        elif text_to_send and "|SPLIT|" in text_to_send:
                            parts = [p.strip() for p in text_to_send.split("|SPLIT|") if p.strip()]
                            for i, part in enumerate(parts):
                                try:
                                    if i == 0 and img_base64: self.send_image(company_id, full_jid, img_base64, part)
                                    else: self.send_message(company_id, full_jid, part)
                                    time.sleep(0.8)
                                except Exception as e:
                                    logger.error(f"❌ [WA {company_id}] Erro ao enviar part {i}: {e}")
                        else:
                            if img_base64: self.send_image(company_id, full_jid, img_base64, text_to_send)
                            elif text_to_send: self.send_message(company_id, full_jid, text_to_send)
                    elif isinstance(resultado, str) and resultado:
                        self.send_message(company_id, full_jid, resultado)
                except Exception as e:
                    logger.error(f"Erro IA WA {company_id}: {e}")
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"Erro handle_message WA {company_id}: {e}")

        def launch():
            try:
                import os
                is_linux = os.name != 'nt'
                if not is_linux:
                    try: asyncio.get_event_loop()
                    except: asyncio.set_event_loop(asyncio.new_event_loop())
                
                b_args = [
                    "--no-sandbox", 
                    "--disable-setuid-sandbox", 
                    "--disable-dev-shm-usage", 
                    "--disable-gpu", 
                    "--single-process",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-site-isolation-trials",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-extensions"
                ]
                
                launch_kwargs = {
                    "session": f"session_empresa_{company_id}",
                    "catchQR": catch_qr,
                    "statusFind": check_status,
                    "headless": True,
                    "browser_args": b_args,
                    "autoClose": 0, # Mantém aberto permanentemente
                    "useLid": True # Habilita suporte nativo a IDs modernos (LID)
                }
                
                if is_linux:
                    launch_kwargs["executable_path"] = "/usr/bin/google-chrome-stable"
                    launch_kwargs["install"] = False
                
                # Limpeza de locks de segurança
                try:
                    session_path = os.path.join(os.getcwd(), "tokens", f"session_empresa_{company_id}")
                    if os.path.exists(session_path):
                        for lock in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
                            l_p = os.path.join(session_path, lock)
                            if os.path.exists(l_p): os.remove(l_p)
                except: pass

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
                                    # Força limpeza do cliente para o watchdog pegar
                                    self.clients.pop(company_id, None) 
                                    break
                                time.sleep(5)
                    except Exception as e:
                        logger.error(f"❌ [WA {company_id}] Falha no Start: {e}")
                        self.status[company_id] = "ERROR"
            finally:
                if company_id in self.starting_sessions: self.starting_sessions.remove(company_id)

        threading.Thread(target=launch, daemon=True, name=f"WA_Launch_{company_id}").start()

    def _handle_state_change(self, company_id, state):
        logger.info(f"🔄 [WA {company_id}] Mudança de Estado: {state}")
        self.status[company_id] = state
        if state in ["DISCONNECTED", "CONFLICT", "UNPAIRED", "UNLAUNCHED"]:
            logger.warning(f"⚠️ [WA {company_id}] Conexão instável détectada. Status: {state}")
            # Se for conflito ou desemparelhado, limpamos a sessão para permitir novo QR
            if state in ["CONFLICT", "UNPAIRED"]:
                self.clean_session(company_id)
            else:
                self.status[company_id] = "DISCONNECTED"
                self.clients.pop(company_id, None)

    def send_message(self, company_id, to_number, text):
        client = self.clients.get(company_id)
        if not client or self.status.get(company_id) != "CONNECTED": return False
        
        # Estratégia de JID Inteligente
        to_number_str = str(to_number)
        if "@lid" in to_number_str:
            # Mantém LID exatamente como veio
            wa_id = to_number_str
        elif "@" in to_number_str:
            # Caso tenha outro sufixo, mantém
            wa_id = to_number_str
        else:
            # Número puro vira @c.us
            digits = "".join(filter(str.isdigit, to_number_str))
            wa_id = f"{digits}@c.us"
        
        logger.info(f"📤 [WA {company_id}] Tentando enviar para JID: {wa_id} (to_number: {to_number})")
        try:
            # Priming: Força o WhatsApp Web a localizar o contato antes de enviar
            # Isso resolve muitos erros de "No LID for user"
            try:
                client.setPresence("composing", wa_id)
                time.sleep(0.3)
            except: pass

            client.sendText(wa_id, text)
            logger.info(f"📤 [WA {company_id}] Msg enviada para {wa_id}")
            return True
        except Exception as e:
            logger.error(f"❌ [WA {company_id}] Erro Msg: {e}")
            self.status[company_id] = "DISCONNECTED"
            return False

    def get_groups(self, company_id, force_refresh=False):
        client = self.clients.get(company_id)
        if not client or self.status.get(company_id) != "CONNECTED": return self.group_cache.get(company_id, [])
        now = time.time()
        if not force_refresh and (now - self.group_cache_time.get(company_id, 0)) < 600:
            return self.group_cache.get(company_id, [])
        try:
            logger.info(f"🔍 [WA {company_id}] Buscando grupos no WhatsApp...")
            groups = client.getAllGroups()
            res = []
            for g in groups:
                name = "Grupo"
                jid = ""
                
                # Caso venha como dicionário
                if isinstance(g, dict):
                    name = g.get("name") or g.get("contact", {}).get("name") or "Grupo"
                    jid_obj = g.get("id")
                    if isinstance(jid_obj, dict):
                        jid = jid_obj.get("_serialized") or jid_obj.get("server") # fallback
                    else:
                        jid = str(jid_obj) if jid_obj else ""
                else:
                    # Caso venha como objeto da biblioteca
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
            
            logger.info(f"✅ [WA {company_id}] {len(res)} grupos localizados.")
            self.group_cache[company_id] = res
            self.group_cache_time[company_id] = now
            return res
        except: return self.group_cache.get(company_id, [])

    def send_image(self, company_id, to_number, base64_data, caption=""):
        client = self.clients.get(company_id)
        if not client or self.status.get(company_id) != "CONNECTED": return False
        
        # Preserva o JID original se já tiver @
        to_number_str = str(to_number)
        if "@" in to_number_str:
            wa_id = to_number_str
        else:
            digits = "".join(filter(str.isdigit, to_number_str))
            wa_id = f"{digits}@c.us"
        
        try:
            # Priming para imagens também
            try:
                client.setPresence("composing", wa_id)
                time.sleep(0.3)
            except: pass

            clean_b64 = base64_data.split(",")[1] if "," in base64_data else base64_data
            temp_dir = os.path.join(os.getcwd(), "tokens", "temp_images")
            os.makedirs(temp_dir, exist_ok=True)
            temp_file = os.path.join(temp_dir, f"t_{company_id}_{int(time.time())}.png")
            with open(temp_file, "wb") as f: f.write(base64.b64decode(clean_b64))
            client.sendImage(wa_id, os.path.abspath(temp_file), "image.png", caption)
            logger.info(f"✅ [WA {company_id}] Imagem enviada para {wa_id}")
            try: os.remove(temp_file)
            except: pass
            return True
        except Exception as e:
            logger.error(f"❌ [WA {company_id}] Erro Imagem: {e}")
            return False

    def send_status_image(self, company_id, base64_data, caption=""):
        """Recurso Desativado por Estabilidade."""
        logger.warning(f"⚠️ [WA {company_id}] Postagem no Status desativada para priorizar estabilidade dos grupos.")
        return False

whatsapp_manager = WhatsAppService()
