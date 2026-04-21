from database import Company, Product, ProductVariation, Category, Customer, Sale, SaleItem, get_manaus_time
import json
import logging

logger = logging.getLogger(__name__)

rule_states = {}

def get_state_key(company_id: int, client_phone: str) -> str:
    return f"{company_id}:{client_phone}"

def get_category_emoji(name: str):
    """Retorna um emoji baseado no nome da categoria para não ficar tudo com vestido."""
    name = name.lower()
    if any(k in name for k in ['masc', 'homem', 'men', 'papai', 'social']):
        return "👕"
    if any(k in name for k in ['fem', 'mulher', 'vestido', 'saia', 'feminino']):
        return "👗"
    if any(k in name for k in ['infantil', 'kids', 'criança', 'bebe', 'baby']):
        return "🧒"
    if any(k in name for k in ['calcado', 'sapato', 'tenis', 'pe', 'chinelo']):
        return "👟"
    if any(k in name for k in ['acessorio', 'bolsa', 'bone', 'relogio']):
        return "🕶️"
    return "🏷️"

def process_with_rules(client_phone: str, msg: str, db, company_id: int) -> str:
    """Regras de negócio para o Bot da Loja de Roupas."""
    state_key = get_state_key(company_id, client_phone)
    is_new = state_key not in rule_states
    state = rule_states.get(state_key, {'active': True, 'step': 0, 'product_id': None})
    text = msg.strip().lower()

    # BUSCAR DADOS DA EMPRESA (Obrigatório para todos os passos)
    company = db.query(Company).filter(Company.id == company_id).first()
    company_name = company.name if company else "Nossa Loja"
    
    # Comandos Globais
    if text in ["oi", "olá", "ola", "voltar", "menu"] or is_new:
        state['step'] = 0
        rule_states[state_key] = state
        
        now_h = get_manaus_time().hour
        greet = "Bom dia"
        if 12 <= now_h < 18: greet = "Boa tarde"
        elif now_h >= 18 or now_h < 5: greet = "Boa noite"
        
        menu = (f"{greet}! 👗✨ Bem-vinda(o) à *{company_name}*.\n\n"
                f"É um prazer ter você aqui! Como podemos te ajudar hoje?\n\n"
                "1️⃣ *Ver Coleções (Categorias)*\n"
                "2️⃣ *Falar com um Atendente*\n"
                "3️⃣ *Meus Pedidos*\n"
                "4️⃣ *Ver Catálogo Completo (Tudo)*\n"
                "5️⃣ *Localização e Endereço*")
        
        if company and company.logo_base64:
            return {"text": menu, "image_base64": company.logo_base64}
        return menu

    if state['step'] == 0:
        if text == "1":
            state['step'] = 1
            # Busca categorias com produtos ativos
            categories = db.query(Category).filter(Category.company_id == company_id).all()
            if not categories:
                return "No momento não temos categorias cadastradas. Por favor, fale com um de nossos atendentes (Opção 2)."
            
            menu_cat = "📂 *NOSSAS CATEGORIAS*\n\nSelecione uma coleção para ver os modelos disponíveis:\n\n"
            state['cat_map'] = {}
            for i, c in enumerate(categories, 1):
                menu_cat += f"{i}️⃣ *{c.name}*\n"
                state['cat_map'][str(i)] = c.id
            
            menu_cat += "\nDigite o número da categoria ou o que você está procurando (ex: 'vestidos pretos')."
            rule_states[state_key] = state
            return menu_cat
            
        elif text == "2":
            return "📣 *Entendido!* Um de nossos consultores de moda já foi notificado e falará com você em instantes. Por favor, aguarde só um momento. 🙏"
        
        elif text == "3":
            return "📦 *Em breve:* Você poderá consultar o status dos seus pedidos por aqui! No momento, peça essa informação para um atendente (Opção 2)."

        elif text == "4":
            # Listar TODOS os produtos de forma visual sequencial
            products = db.query(Product).filter(Product.company_id == company_id, Product.active == True, Product.show_on_whatsapp == True).all()
            if not products:
                return "Ainda não temos produtos cadastrados no catálogo."
            
            state['step'] = 2
            state['prod_map'] = {}
            
            # Montamos o mapa de IDs para o cliente poder escolher depois
            for i, p in enumerate(products, 1):
                state['prod_map'][str(i)] = p.id
            
            # Criamos a lista de imagens sequenciais (Limite de 15 para segurança)
            msg_list = []
            count = 0
            for i, p in enumerate(products, 1):
                if p.image_base64 and count < 15:
                    msg_list.append({
                        "image": p.image_base64,
                        "text": f"{i}️⃣ *{p.name}*\n💰 Valor: R$ {p.base_price:.2f}"
                    })
                    count += 1
            
            summary_text = (f"🛍️ *CATÁLOGO COMPLETO*\n\n"
                            f"Encontramos {len(products)} modelos incríveis para você!\n"
                            f"Estou enviando as fotos logo abaixo... 👇\n\n"
                            f"Para comprar, basta digitar o *Número* que aparece em cima da foto.")
            
            rule_states[state_key] = state
            
            if msg_list:
                return {
                    "text": summary_text,
                    "image_list": msg_list
                }
            
            # Fallback se não tiver fotos
            menu_all = "🛍️ *CATÁLOGO (Sem Fotos)*\n\n"
            for i, p in enumerate(products, 1):
                menu_all += f"{i}️⃣ *{p.name}* - R$ {p.base_price:.2f}\n"
            return menu_all

        elif text == "5":
            # Localização e Endereço
            address = company.address if company.address else "Ainda não cadastramos nosso endereço físico."
            location = company.location_link if company.location_link else ""
            
            msg = f"📍 *NOSSA LOCALIZAÇÃO*\n\n🏠 *Endereço:* {address}\n"
            if location:
                msg += f"\n🗺️ *Link do GPS (Google Maps):*\n{location}\n"
            
            msg += "\nEsperamos sua visita! ✨\n\nDigite *Menu* para voltar as opções."
            return msg

    elif state['step'] == 1:
        # Escolha da Categoria (Número)
        cat_id = state.get('cat_map', {}).get(text)
        if cat_id:
            state['step'] = 2
            state['selected_cat'] = cat_id
            
            category = db.query(Category).filter(Category.id == cat_id).first()
            products = db.query(Product).filter(Product.category_id == cat_id, Product.active == True, Product.show_on_whatsapp == True).all()
            if not products:
                return "Desculpe, não encontramos peças disponíveis nesta coleção agora. Quer ver outra? Digite *Menu*."
            
            state['step'] = 2
            state['prod_map'] = {}
            msg_list = []
            
            for i, p in enumerate(products, 1):
                state['prod_map'][str(i)] = p.id
                if p.image_base64:
                    msg_list.append({
                        "image": p.image_base64,
                        "text": f"{i}️⃣ *{p.name}*\n💰 Valor: R$ {p.base_price:.2f}"
                    })
            
            emoji = get_category_emoji(category.name)
            summary_text = (f"{emoji} *COLEÇÃO: {category.name.upper()}*\n\n"
                            f"Encontramos {len(products)} modelos nesta coleção! "
                            f"Estou enviando as fotos logo abaixo... 👇\n\n"
                            f"Para ver os tamanhos e detalhes, basta digitar o *Número* que aparece em cima da foto.")
            
            rule_states[state_key] = state
            
            if msg_list:
                return {
                    "text": summary_text,
                    "image_list": msg_list
                }
            
            # Fallback se não tiver fotos (improvável em moda, mas seguro)
            menu_prod = f"{emoji} *COLEÇÃO: {category.name.upper()}*\n\nEscolha o modelo:\n\n"
            for i, p in enumerate(products, 1):
                menu_prod += f"{i}️⃣ *{p.name}* - R$ {p.base_price:.2f}\n"
            menu_prod += "\nDigite o número para ver mais detalhes."
            return menu_prod
        return "❌ *Opção inválida.* Por favor, escolha um dos números da lista ou descreva o que procura."

    elif state['step'] == 2:
        # Escolha do Produto
        prod_id = state.get('prod_map', {}).get(text)
        if prod_id:
            product = db.query(Product).filter(Product.id == prod_id).first()
            state['product_id'] = prod_id
            
            # Busca variações (Tamanhos/Cores) com estoque
            variations = db.query(ProductVariation).filter(ProductVariation.product_id == prod_id, ProductVariation.stock_quantity > 0).all()
            
            if not variations:
                return (f"✨ *{product.name}*\n\n"
                        "😔 Poxa, no momento este modelo está sem estoque nos tamanhos disponíveis.\n\n"
                        "Gostaria de ver outro modelo? Basta digitar *Catálogo* ou escolher outro número da lista anterior.")

            state['step'] = 3
            detail = (f"✨ *{product.name}*\n"
                      f"💰 *Valor:* R$ {product.base_price:.2f}\n"
                      f"📝 *Descrição:* {product.description or 'Peça exclusiva da nossa coleção.'}\n\n"
                      f"📍 *Selecione o seu tamanho:*\n\n")
            
            state['var_map'] = {}
            for i, v in enumerate(variations, 1):
                var_text = f"Tamanho: *{v.size}*"
                if v.color: var_text += f" | Cor: {v.color}"
                # Preço diferenciado se houver override
                price = v.price_override if v.price_override else product.base_price
                if price != product.base_price:
                    var_text += f" (R$ {price:.2f})"
                
                detail += f"{i}️⃣ {var_text}\n"
                state['var_map'][str(i)] = v.id
            
            detail += "\n👉 *Digite o número da opção desejada para reservar.*"
            detail += "\n\n━━━━━━━━━━━━━━━\n"
            detail += "↩️ Digite *Menu* para voltar."
            
            rule_states[state_key] = state
            
            if product.image_base64:
                return {"text": detail, "image_base64": product.image_base64}
            return detail
        
        # Se não digitou um número válido do mapa, mas digitou um texto, talvez queira voltar
        if text == "catálogo" or text == "voltar":
            return process_with_rules(client_phone, "menu", db, company_id)

        return "❌ *Opção inválida.* Escolha um dos números do catálogo acima ou digite *Menu* para recomeçar."

    elif state['step'] == 3:
        # Escolha da Variação
        var_id = state.get('var_map', {}).get(text)
        if var_id:
            variation = db.query(ProductVariation).filter(ProductVariation.id == var_id).first()
            product = variation.product
            
            state['variation_id'] = var_id
            state['step'] = 4
            
            rule_states[state_key] = state
            return (f"✅ *Excelente escolha!*\n\n"
                    f"Você selecionou: *{product.name}* (Tamanho {variation.size})\n"
                    f"Valor: R$ {variation.price_override if variation.price_override else product.base_price:.2f}\n\n"
                    "Para finalizar o pedido ou reservar, por favor me diga seu *Nome Completo*.")
        return "❌ *Escolha uma das opções acima.*"

    elif state['step'] == 4:
        # Recebe o nome e pergunta Entrega vs Retirada
        client_name = text.strip()
        if len(client_name) < 3:
            return "❌ *Nome muito curto.* Por favor, digite seu nome completo."
        
        state['client_name'] = client_name
        state['step'] = 5
        
        fee = company.delivery_fee if company else 0.0
        
        rule_states[state_key] = state
        return (f"Legal, *{client_name}*! Como você prefere receber seu pedido?\n\n"
                f"1️⃣ **Retirada na Loja** (Grátis)\n"
                f"2️⃣ **Entrega** (Taxa de R$ {fee:.2f})\n\n"
                "Digite o número da opção desejada.")

    elif state['step'] == 5:
        # Escolha de Entrega vs Retirada
        if text == "1":
            state['delivery_type'] = 'pickup'
            state['delivery_fee'] = 0.0
            state['address'] = "Retirada na Loja"
            state['payment_method'] = 'PIX' # Volta a ser direto PIX
            return finalize_whatsapp_sale(client_phone, state, db, company_id, state_key)
        elif text == "2":
            state['delivery_type'] = 'delivery'
            
            mode = company.delivery_mode or 'fixed'
            if mode == 'fixed':
                state['delivery_fee'] = company.delivery_fee or 0.0
                state['step'] = 6
                rule_states[state_key] = state
                return "📍 *Ótimo!* Por favor, digite o **endereço completo** para a entrega (Rua, Número, Bairro e Ponto de Referência):"
            else:
                # MODO BAIRROS
                from database import Neighborhood
                neighborhoods = db.query(Neighborhood).filter(Neighborhood.company_id == company_id).all()
                if not neighborhoods:
                    # Se não tiver bairros, volta pro fixo ou erro
                    state['delivery_fee'] = company.delivery_fee or 0.0
                    state['step'] = 6
                    rule_states[state_key] = state
                    return "📍 *Ótimo!* Por favor, digite o **endereço completo** para a entrega:"
                
                menu_bairros = "📍 *Selecione seu bairro para entrega:*\n\n"
                for idx, nb in enumerate(neighborhoods, 1):
                    menu_bairros += f"{idx}️⃣ **{nb.name}** (R$ {nb.fee:.2f})\n"
                
                state['neighborhoods_list'] = [{'name': n.name, 'fee': n.fee} for n in neighborhoods]
                state['step'] = 5.5
                rule_states[state_key] = state
                return menu_bairros + "\nDigite o número do seu bairro."
        return "❌ *Opção inválida.* Digite 1 para Retirada ou 2 para Entrega."

    elif state['step'] == 5.5:
        # Seleção de Bairro
        try:
            choice = int(text)
            nb_list = state.get('neighborhoods_list', [])
            if 1 <= choice <= len(nb_list):
                selected = nb_list[choice - 1]
                state['delivery_fee'] = selected['fee']
                state['neighborhood_name'] = selected['name']
                state['step'] = 6
                rule_states[state_key] = state
                return f"✅ Bairro *{selected['name']}* selecionado.\n\nAgora, por favor, digite o seu **endereço completo** (Rua, Número e Ponto de Referência):"
        except: pass
        return "❌ *Opção inválida.* Digite o número correspondente ao seu bairro."

    elif state['step'] == 6:
        # Recebe o endereço
        address = text.strip()
        if len(address) < 5:
            return "❌ *Endereço muito curto.* Por favor, digite o endereço completo."
        
        # Se veio de bairro, concatena
        nb_name = state.get('neighborhood_name')
        if nb_name:
            state['address'] = f"{address} - Bairro: {nb_name}"
        else:
            state['address'] = address
            
        state['payment_method'] = 'PIX' # Direto PIX como solicitado
        return finalize_whatsapp_sale(client_phone, state, db, company_id, state_key)

    return "Não entendi sua opção. Digite *Menu* para ver as opções."

def finalize_whatsapp_sale(client_phone, state, db, company_id, state_key):
    """Refatora a finalização para ser infalível."""
    try:
        # 1. Carregar Dados Essenciais
        var_id = state.get('variation_id')
        variation = db.query(ProductVariation).filter(ProductVariation.id == var_id).first()
        if not variation:
            return "❌ *Erro:* Produto não localizado. Digite *Menu* para recomeçar."
        
        product = variation.product
        company = db.query(Company).filter(Company.id == company_id).first()
        client_name = state.get('client_name', 'Cliente')
        
        # 2. Cliente
        customer = db.query(Customer).filter(Customer.phone == client_phone, Customer.company_id == company_id).first()
        if not customer:
            customer = Customer(name=client_name, phone=client_phone, company_id=company_id)
            db.add(customer)
            db.flush()
        
        # 3. Calcular Valores
        item_price = variation.price_override if variation.price_override else product.base_price
        delivery_fee = state.get('delivery_fee', 0.0)
        total = item_price + delivery_fee
        payment_method = state.get('payment_method', 'PIX')
        
        # 4. Criar Venda
        new_sale = Sale(
            company_id=company_id,
            customer_id=customer.id,
            total_amount=total,
            delivery_type=state.get('delivery_type'),
            delivery_fee=delivery_fee,
            delivery_address=state.get('address'),
            payment_method=payment_method,
            status="pending"
        )
        db.add(new_sale)
        db.flush()
        
        # 5. Criar Item
        sale_item = SaleItem(
            sale_id=new_sale.id,
            product_id=product.id,
            variation_id=variation.id,
            quantity=1,
            unit_price=item_price
        )
        db.add(sale_item)
        
        # Commit imediato para garantir que a venda existe
        db.commit()
        
        # 6. Notificar Dono
        try:
            from whatsapp_service import whatsapp_manager
            if company.whatsapp_number:
                whatsapp_manager.notify_merchant_new_sale(company_id, new_sale.id, {
                    "merchant_number": company.whatsapp_number,
                    "customer_name": client_name,
                    "product_name": product.name,
                    "total": total,
                    "payment_method": payment_method,
                    "delivery_type": state.get('delivery_type')
                })
        except: pass

        # 7. Gerar Resposta PIX
        pix_info = ""
        pix_qr_image = None
        if payment_method == "PIX":
            from payments import generate_pix_payment
            try:
                pix_res = generate_pix_payment(total, f"Pedido #{new_sale.id}", static_key=company.pix_key, company_name=company.name)
                pix_qr_image = pix_res.get('qr_code_base64')
                pix_info = (f"|SPLIT|🔑 *CHAVE PIX (Copia e Cola):*\n\n"
                            f"{pix_res['qr_code']}\n\n"
                            f"💳 *Valor:* R$ {total:.2f}\n\n"
                            "💡 *Dica:* Copie o código acima e pague no seu banco.")
            except Exception as e:
                logger.error(f"Erro ao gerar PIX: {e}")
                pix_info = "\n\n⚠️ *Aviso:* Chame um atendente para receber a chave PIX."
        
        # 8. Localização
        store_info = ""
        if company.address:
            store_info = f"\n\n📍 *Endereço da Loja:*\n{company.address}"
            if company.location_link:
                store_info += f"\n🗺️ *GPS:* {company.location_link}"

        is_pickup = state.get('delivery_type') == 'pickup'
        resumo_logistica = "✅ *Retirada na Loja agendada!*" if is_pickup else f"🚚 *Entrega em:* {state.get('address')}"
        
        final_emoji = get_category_emoji(product.name) if not "vestido" in product.name.lower() else "👗"
        
        resp = (f"🥳 *Pedido Recebido, {client_name}!*\n\n"
                f"{final_emoji} *Item:* {product.name} ({variation.size})\n"
                f"💰 *Subtotal:* R$ {item_price:.2f}\n"
                f"📦 *Frete:* R$ {delivery_fee:.2f}\n"
                f"⭐ *TOTAL:* R$ {total:.2f}\n\n"
                f"{resumo_logistica}\n"
                f"💳 *Pagamento:* {payment_method}"
                f"{store_info}\n\n"
                "⚠️ *IMPORTANTE:* Seu pedido está aguardando confirmação. Assim que virmos seu pagamento, confirmaremos tudo aqui! Obrigado! 🛍️"
                f"{pix_info}")
        
        # Limpa Estado NO FINAL
        if state_key in rule_states:
            del rule_states[state_key]

        return resp
        
    except Exception as e:
        logger.error(f"FATAL FINALIZE: {e}")
        db.rollback()
        return "❌ *Erro ao processar pedido.* Por favor, tente novamente digitando *Menu*."

    return "Não entendi sua opção. Digite *Menu* para ver as opções."

def process_message(client_phone: str, message: str, db=None, company_id: int = None) -> str:
    state_key = get_state_key(company_id, client_phone)
    # Comandos globais que limpam o estado para recomeçar
    if message.strip().lower() in ["oi", "olá", "ola", "sair", "cancelar", "menu"]:
        if state_key in rule_states: 
            del rule_states[state_key]
    return process_with_rules(client_phone, message, db, company_id)
