import os
import mercadopago
import segno
import io
import base64
from dotenv import load_dotenv

load_dotenv()

def generate_pix_payment(amount: float, description: str, token: str = None, static_key: str = None, company_name: str = "Barbearia", merchant_city: str = "Manaus"):
    """
    Gera um pagamento PIX. 
    1. Tenta usar Mercado Pago (Dinâmico) se o token for fornecido.
    2. Caso contrário, gera um PIX Estático se a chave for fornecida.
    """
    
    # --- 1. MERCADO PAGO (DINÂMICO) ---
    if token and len(token) > 20:
        try:
            sdk = mercadopago.SDK(token)
            payment_data = {
                "transaction_amount": float(amount),
                "description": description,
                "payment_method_id": "pix",
                "payer": {
                    "email": "cliente@barbearia.com",
                }
            }
            payment_response = sdk.payment().create(payment_data)
            payment = payment_response.get("response")
            
            if payment_response.get("status") == 201:
                return {
                    "type": "dynamic",
                    "id": payment.get("id"),
                    "qr_code": payment.get("point_of_interaction", {}).get("transaction_data", {}).get("qr_code"),
                    "qr_code_base64": payment.get("point_of_interaction", {}).get("transaction_data", {}).get("qr_code_base64")
                }
        except Exception as e:
            print(f"Erro Mercado Pago: {e}")

    # --- 2. PIX ESTÁTICO (CHAVE FIXA) ---
    if static_key:
        payload = generate_static_pix_payload(static_key, amount, company_name, merchant_city)
        # Gera QR Code em Base64 usando Segno
        out = io.BytesIO()
        segno.make_qr(payload).save(out, kind='png', scale=5)
        qr_base64 = base64.b64encode(out.getvalue()).decode('utf-8')
        
        return {
            "type": "static",
            "qr_code": payload,
            "qr_code_base64": qr_base64
        }

    # --- 3. FALLBACK (SIMULADO) ---
    mock_payload = f"00020126480014br.gov.bcb.pix0114+55929999999990208PAGAMENTO5204000053039865405{amount:.2f}5802BR5915AlessandroBarba6006Manaus62070503***6304ABCD"
    
    # Gera QR Code em Base64 para o Mock também
    out = io.BytesIO()
    segno.make_qr(mock_payload).save(out, kind='png', scale=5)
    qr_base64 = base64.b64encode(out.getvalue()).decode('utf-8')

    return {
        "type": "mock",
        "qr_code": mock_payload,
        "qr_code_base64": qr_base64
    }

def generate_static_pix_payload(key: str, amount: float, name: str, city: str):
    """Gera um payload básico de PIX Estático (BRCode)"""
    def f(tag, value):
        return f"{tag}{len(str(value)):02d}{value}"

    # Merchant Account Info
    account_info = f("00", "br.gov.bcb.pix") + f("01", key)
    
    payload = f("00", "01") # Payload Format Indicator
    payload += f("26", account_info)
    payload += f("52", "0000") # Merchant Category Code
    payload += f("53", "986") # Transaction Currency (Real)
    payload += f("54", f"{amount:.2f}") # Amount
    payload += f("58", "BR") # Country Code
    payload += f("59", name[:25]) # Merchant Name
    payload += f("60", city[:15]) # Merchant City
    payload += "6304" # CRC16 Placeholder
    
    # Cálculo de CRC16 simplificado
    crc = 0xFFFF
    for char in payload:
        crc ^= ord(char) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
    crc &= 0xFFFF
    return payload + f"{crc:04X}"
