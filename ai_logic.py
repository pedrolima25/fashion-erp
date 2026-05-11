from database import Company, Product, ProductVariation, Category, Customer, Sale, SaleItem, get_manaus_time
import json
import logging

logger = logging.getLogger(__name__)

rule_states = {}

def get_state_key(company_id: int, client_phone: str) -> str:
    return f"{company_id}:{client_phone}"

def get_category_emoji(name: str):
    name = name.lower()
    if any(k in name for k in ['masc', 'homem', 'men', 'papai', 'social']): return "👕"
    if any(k in name for k in ['fem', 'mulher', 'vestido', 'saia', 'feminino']): return "👗"
    if any(k in name for k in ['infantil', 'kids', 'criança', 'bebe', 'baby']): return "🧒"
    if any(k in name for k in ['calcado', 'sapato', 'tenis', 'pe', 'chinelo']): return "👟"
    if any(k in name for k in ['acessorio', 'bolsa', 'bone', 'relogio']): return "🕶️"
    return "🏷️"

def process_with_rules(client_phone: str, msg: str, db, company_id: int) -> str:
    state_key = get_state_key(company_id, client_phone)
    is_new = state_key not in rule_states
    state = rule_states.get(state_key, {'active': True, 'step': 0, 'cart': []})
    text = msg.strip().lower()

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company: return "❌ *Erro de configuração.*"
    
    comp_name = company.name or "Nossa Loja"
    comp_fee = company.delivery_fee or 0.0
    
    # Comandos Globais
    if text in ["oi", "olá", "ola", "voltar", "menu", "cancelar"] or is_new:
        state['step'] = 0
        rule_states[state_key] = state
        
        now_h = get_manaus_time().hour
        greet = "Bom dia" if 5 <= now_h < 12 else ("Boa tarde" if 12 <= now_h < 18 else "Boa noite")
        
        menu = (f"{greet}! 👗✨ Bem-vinda(o) à *{comp_name}*.\n\n"
                "Como podemos te ajudar hoje?\n\n"
                "1️⃣ *Ver Coleções*\n"
                "2️⃣ *Falar com Atendente*\n"
                "3️⃣ *Meus Pedidos*\n"
                "4️⃣ *Catálogo Completo*\n"
                "5️⃣ *Endereço da Loja*\n"
                "6️⃣ *Sugestões Personalizadas* (IA)")
        
        if state.get('cart'):
            menu += f"\n\n🛒 *Seu Carrinho possui {len(state['cart'])} item(s).*\nDigite *Carrinho* para ver ou finalizar."

        if company.logo_base64 and is_new:
            return {"text": menu, "image_base64": company.logo_base64}
        return menu

    # Carrinho (Atalho)
    if text == "carrinho":
        if not state.get('cart'): return "Seu carrinho está vazio! 🛍️"
        return show_cart(state)

    if state['step'] == 0:
        if text == "1":
            state['step'] = 1
            categories = db.query(Category).filter(Category.company_id == company_id).all()
            if not categories: return "Sem coleções no momento."
            
            menu_cat = "📂 *NOSSAS COLEÇÕES*\n\n"
            state['cat_map'] = {}
            for i, c in enumerate(categories, 1):
                menu_cat += f"{i}️⃣ *{c.name}*\n"
                state['cat_map'][str(i)] = c.id
            rule_states[state_key] = state
            return menu_cat
            
        elif text == "2":
            return "📣 *Entendido!* Um consultor falará com você em instantes. 🙏"
        
        elif text == "3":
            # MEUS PEDIDOS
            sales = db.query(Sale).join(Customer).filter(Customer.phone == client_phone, Sale.company_id == company_id).order_by(Sale.date.desc()).limit(5).all()
            if not sales: return "Você ainda não possui pedidos conosco. 🛍️"
            
            msg_pedidos = "📦 *SEUS ÚLTIMOS PEDIDOS*\n\n"
            for s in sales:
                status_traducao = {"pending": "⏳ Pendente", "paid": "✅ Pago", "completed": "🚀 Enviado/Pronto", "cancelled": "❌ Cancelado"}
                msg_pedidos += f"🔹 *#{s.id}* - R$ {s.total_amount:.2f}\nStatus: {status_traducao.get(s.status, s.status)}\nData: {s.date.strftime('%d/%m/%Y')}\n\n"
            return msg_pedidos + "Digite *Menu* para voltar."

        elif text == "4":
            products = db.query(Product).filter(Product.company_id == company_id, Product.active == True, Product.show_on_whatsapp == True).all()
            if not products: return "Catálogo vazio."
            state['step'] = 2
            state['prod_map'] = {}
            msg_list = []
            for i, p in enumerate(products, 1):
                state['prod_map'][str(i)] = p.id
                if p.image_base64 and i <= 15:
                    msg_list.append({"image": p.image_base64, "text": f"{i}️⃣ *{p.name}*\n💰 R$ {p.base_price:.2f}"})
            rule_states[state_key] = state
            return {"text": "🛍️ *CATÁLOGO COMPLETO*\nDigite o número para ver detalhes.", "image_list": msg_list}

        elif text == "5":
            msg = f"📍 *NOSSA LOCALIZAÇÃO*\n\n🏠 {company.address or 'Não informado'}"
            if company.location_link: msg += f"\n🗺️ {company.location_link}"
            return msg

        elif text == "6":
            sugg = get_ai_suggestions(client_phone, db, company_id)
            if not sugg: return "Você ainda não tem histórico de compras suficiente para sugestões. Continue comprando para que eu conheça seu estilo! 👗✨"
            return sugg

    elif state['step'] == 1:
        cat_id = state.get('cat_map', {}).get(text)
        if cat_id:
            products = db.query(Product).filter(Product.category_id == cat_id, Product.active == True, Product.show_on_whatsapp == True).all()
            if not products: return "Sem itens nesta coleção."
            state['step'] = 2
            state['prod_map'] = {}
            msg_list = []
            for i, p in enumerate(products, 1):
                state['prod_map'][str(i)] = p.id
                if p.image_base64: msg_list.append({"image": p.image_base64, "text": f"{i}️⃣ *{p.name}*\n💰 R$ {p.base_price:.2f}"})
            rule_states[state_key] = state
            return {"text": "✨ *ITENS DISPONÍVEIS*\nEscolha pelo número:", "image_list": msg_list}

    elif state['step'] == 2:
        prod_id = state.get('prod_map', {}).get(text)
        if prod_id:
            product = db.query(Product).filter(Product.id == prod_id).first()
            variations = db.query(ProductVariation).filter(ProductVariation.product_id == prod_id, ProductVariation.stock_quantity > 0).all()
            if not variations: return "Infelizmente este modelo esgotou. 😔"
            state['step'] = 3
            state['var_map'] = {}
            detail = f"✨ *{product.name}*\n💰 R$ {product.base_price:.2f}\n\n📍 *Escolha o Tamanho:*\n"
            for i, v in enumerate(variations, 1):
                detail += f"{i}️⃣ {v.size} {('('+v.color+')') if v.color else ''}\n"
                state['var_map'][str(i)] = v.id
            rule_states[state_key] = state
            if product.image_base64: return {"text": detail, "image_base64": product.image_base64}
            return detail

    elif state['step'] == 3:
        var_id = state.get('var_map', {}).get(text)
        if var_id:
            variation = db.query(ProductVariation).filter(ProductVariation.id == var_id).first()
            item = {
                "product_id": variation.product_id,
                "variation_id": variation.id,
                "name": variation.product.name,
                "size": variation.size,
                "price": variation.price_override or variation.product.base_price,
                "quantity": 1
            }
            state['cart'].append(item)
            state['step'] = 3.5
            rule_states[state_key] = state
            return (f"✅ *{item['name']} (Tam {item['size']})* adicionado!\n\n"
                    "Deseja fazer mais o quê?\n"
                    "1️⃣ *Ver mais roupas (Continuar)*\n"
                    "2️⃣ *Finalizar Pedido (Checkout)*\n"
                    "3️⃣ *Limpar Carrinho*")

    elif state['step'] == 3.5:
        if text == "1":
            state['step'] = 0
            return process_with_rules(client_phone, "menu", db, company_id)
        elif text == "2":
            return show_cart(state)
        elif text == "3":
            state['cart'] = []
            state['step'] = 0
            return "Carrinho limpo! Voltando ao menu..."

    elif state['step'] == 4: # Checkout: Nome
        state['client_name'] = msg.strip()
        state['step'] = 5
        rule_states[state_key] = state
        return (f"Ótimo, *{msg.strip()}*! Como prefere receber?\n\n"
                f"1️⃣ *Retirada na Loja* (Grátis)\n"
                f"2️⃣ *Entrega* (R$ {comp_fee:.2f})")

    elif state['step'] == 5: # Checkout: Entrega
        if text == "1":
            state['delivery_type'] = 'pickup'; state['delivery_fee'] = 0.0; state['address'] = "Retirada na Loja"
            return finalize_cart_sale(client_phone, state, db, company_id, state_key)
        elif text == "2":
            state['delivery_type'] = 'delivery'; state['delivery_fee'] = comp_fee
            state['step'] = 6
            rule_states[state_key] = state
            return "📍 Digite seu *Endereço Completo* (Rua, Número e Bairro):"

    elif state['step'] == 6: # Checkout: Endereço
        state['address'] = msg.strip()
        state['step'] = 7
        rule_states[state_key] = state
        return "Agora envie um *ponto de referência* para facilitar a entrega. Ex: casa azul, perto da padaria. Se não tiver, digite *pular*."

    elif state['step'] == 7: # Checkout: Referencia
        state['delivery_reference'] = "" if text in ["pular", "nao", "não", "sem"] else msg.strip()
        state['step'] = 8
        rule_states[state_key] = state
        return ("Por fim, envie sua *localização pelo WhatsApp*.\n\n"
                "Toque no clipe/anexo > *Localização* > *Localização atual*.\n"
                "Se preferir, cole o link do Google Maps. Se não conseguir, digite *pular*.")

    elif state['step'] == 8: # Checkout: Localizacao
        location_text = msg.strip()
        if text in ["pular", "nao", "não", "sem"]:
            state['delivery_location_link'] = ""
        elif location_text.startswith("LOCALIZACAO_WHATSAPP "):
            state['delivery_location_link'] = location_text.replace("LOCALIZACAO_WHATSAPP ", "", 1).strip()
        else:
            state['delivery_location_link'] = location_text
        return finalize_cart_sale(client_phone, state, db, company_id, state_key)

    return "Não entendi. Digite *Menu*."

def show_cart(state):
    msg = "🛒 *SEU CARRINHO*\n\n"
    total = 0
    for i, item in enumerate(state['cart'], 1):
        msg += f"{i}. {item['name']} (Tam {item['size']}) - R$ {item['price']:.2f}\n"
        total += item['price']
    msg += f"\n💰 *Subtotal: R$ {total:.2f}*\n\nDigite *Sim* para fechar o pedido ou *Menu* para continuar comprando."
    state['step'] = 3.9
    return msg

def finalize_cart_sale(client_phone, state, db, company_id, state_key):
    try:
        customer = db.query(Customer).filter(Customer.phone == client_phone, Customer.company_id == company_id).first()
        if not customer:
            customer = Customer(name=state.get('client_name', 'Cliente'), phone=client_phone, company_id=company_id)
            db.add(customer); db.flush()
        
        total_items = sum(i['price'] for i in state['cart'])
        total = total_items + state.get('delivery_fee', 0.0)
        
        new_sale = Sale(
            company_id=company_id,
            customer_id=customer.id,
            total_amount=total,
            delivery_type=state.get('delivery_type'),
            delivery_fee=state.get('delivery_fee'),
            delivery_address=state.get('address'),
            delivery_reference=state.get('delivery_reference'),
            delivery_location_link=state.get('delivery_location_link'),
            delivery_status="waiting" if state.get('delivery_type') == "delivery" else None,
            status="pending"
        )
        db.add(new_sale); db.flush()
        
        for i in state['cart']:
            si = SaleItem(sale_id=new_sale.id, product_id=i['product_id'], variation_id=i['variation_id'], quantity=1, unit_price=i['price'])
            db.add(si)
        
        db.commit()
        
        # Notificação Vendedor
        from whatsapp_service import whatsapp_manager
        company = db.query(Company).filter(Company.id == company_id).first()
        if company.whatsapp_number:
            merchant_msg = (
                f"🛍️ *NOVA VENDA #{new_sale.id}*\n"
                f"Cliente: {customer.name}\n"
                f"WhatsApp: {client_phone}\n"
                f"Total: R$ {total:.2f}\n"
                f"Entrega: {'Retirada na loja' if state.get('delivery_type') == 'pickup' else 'Delivery'}"
            )
            if state.get('delivery_type') == 'delivery':
                merchant_msg += f"\nEndereço: {state.get('address') or '-'}"
                merchant_msg += f"\nReferência: {state.get('delivery_reference') or '-'}"
                merchant_msg += f"\nLocalização: {state.get('delivery_location_link') or '-'}"
            whatsapp_manager.send_message(company_id, company.whatsapp_number, merchant_msg)

        # PIX
        from payments import generate_pix_payment
        pix_code = generate_pix_payment(total, f"Pedido #{new_sale.id}", static_key=company.pix_key, company_name=company.name).get('qr_code', 'Erro PIX')
        
        msg = (f"🥳 *Pedido #{new_sale.id} Recebido!*\n\n"
               f"💰 *Total: R$ {total:.2f}*\n"
               f"📍 *Logística:* {'Retirada' if state['delivery_type']=='pickup' else 'Entrega'}")
        if state.get('delivery_type') == 'delivery':
            msg += (f"\n🏠 *Endereço:* {state.get('address') or '-'}"
                    f"\n📌 *Referência:* {state.get('delivery_reference') or '-'}"
                    f"\n🗺️ *Localização:* {state.get('delivery_location_link') or '-'}")
        msg += f"\n\n🔑 *PIX Copia e Cola:*\n`{pix_code}`\n\nAguardamos seu pagamento! ✨"
        
        del rule_states[state_key]
        return msg
    except Exception as e:
        logger.error(f"Erro checkout: {e}"); db.rollback(); return "Erro ao finalizar."

def get_ai_suggestions(client_phone: str, db, company_id: int):
    """Sugere produtos com base no histórico do cliente."""
    customer = db.query(Customer).filter(Customer.phone == client_phone, Customer.company_id == company_id).first()
    if not customer: return None
    
    # Busca últimas compras
    last_sales = db.query(Sale).filter(Sale.customer_id == customer.id).order_by(Sale.date.desc()).limit(3).all()
    if not last_sales: return None
    
    # Pega as categorias que ele mais compra
    cat_ids = []
    for s in last_sales:
        for item in s.items:
            cat_ids.append(item.product.category_id)
    
    if not cat_ids: return None
    
    # Busca itens novos ou diferentes nessas categorias
    suggestions = db.query(Product).filter(
        Product.company_id == company_id,
        Product.category_id.in_(cat_ids),
        Product.active == True,
        Product.show_on_whatsapp == True
    ).order_by(Product.created_at.desc()).limit(3).all()
    
    if not suggestions: return None
    
    msg = "✨ *SUGESTÕES PARA VOCÊ* ✨\nCom base no seu estilo, acho que vai amar estes itens:\n\n"
    img_list = []
    for i, p in enumerate(suggestions, 1):
        msg += f"🔹 *{p.name}* - R$ {p.base_price:.2f}\n"
        if p.image_base64:
            img_list.append({"image": p.image_base64, "text": f"*{p.name}*\n💰 R$ {p.base_price:.2f}"})
            
    return {"text": msg + "\nDigite o nome de um item para ver detalhes ou *Menu*.", "image_list": img_list}

def process_message(client_phone: str, message: str, db=None, company_id: int = None) -> str:
    state_key = get_state_key(company_id, client_phone)
    if message.strip().lower() in ["menu", "sair", "cancelar"]:
        if state_key in rule_states: del rule_states[state_key]
    
    # Lógica de Checkout (Sim para finalizar)
    state = rule_states.get(state_key)
    if state and state['step'] == 3.9 and message.strip().lower() in ["sim", "finalizar", "fechar"]:
        state['step'] = 4
        return "Para finalizar, qual o seu *Nome Completo*?"
        
    return process_with_rules(client_phone, message, db, company_id)
