import base64
import os
from database import SessionLocal, Product

img_path = r'C:\Users\alessandro\.gemini\antigravity\brain\a717ce8a-c98d-43d0-983a-bc0f8b28d87c\vestido_elegante_demo_1776660686874.png'

if os.path.exists(img_path):
    with open(img_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    
    db = SessionLocal()
    try:
        # Procuramos o produto (com espaço no fim como apareceu no print anterior)
        p = db.query(Product).filter(Product.name.like('vestido logo%')).first()
        if p:
            p.image_base64 = 'data:image/png;base64,' + b64
            db.commit()
            print(f"✅ Produto '{p.name}' atualizado com imagem!")
        else:
            print("❌ Produto não encontrado.")
    finally:
        db.close()
else:
    print("❌ Arquivo de imagem não encontrado.")
