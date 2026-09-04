import gradio as gr
import pandas as pd
import os

ARQUIVO_RESULTADOS = "resultados_anotacao.csv"

# Carrega os dados
if os.path.exists("videos.csv"):
    df = pd.read_csv("videos.csv")
else:
    df = pd.DataFrame({"id": [], "url": [], "target": []})

def render_tiktok(url):
    if pd.isna(url): return "Fim dos vídeos."
    video_id = str(url).split('/')[-1]
    html = f"""
    <blockquote class="tiktok-embed" cite="{url}" data-video-id="{video_id}" style="max-width: 605px;min-width: 325px;">
      <section></section>
    </blockquote>
    <script async src="https://www.tiktok.com/embed.js"></script>
    """
    return html

def get_first_video(nome):
    if not nome:
        return gr.update(visible=True), gr.update(visible=False), "Digite um nome!", 0, "", ""
    if len(df) == 0:
        return gr.update(visible=True), gr.update(visible=False), "Erro: videos.csv vazio ou não encontrado.", 0, "", ""
        
    linha = df.iloc[0]
    html = render_tiktok(linha['url'])
    target = f"**Target:** {linha['target']}"
    return gr.update(visible=False), gr.update(visible=True), f"Anotador: {nome}", 0, html, target

def salvar_e_proximo(nome, index, stance):
    if not stance:
        return index, gr.update(), gr.update(), gr.update(), "⚠️ Selecione uma opção antes de salvar!", gr.update()
        
    nome_limpo = nome.replace("Anotador: ", "")
    linha = df.iloc[index]
    
    # Salvar no CSV
    novo_dado = pd.DataFrame([{
        "anotador": nome_limpo,
        "video_id": linha['id'],
        "target": linha['target'],
        "stance": stance
    }])
    novo_dado.to_csv(ARQUIVO_RESULTADOS, mode='a', header=not os.path.exists(ARQUIVO_RESULTADOS), index=False)
    
    # Avançar para o próximo
    next_idx = index + 1
    if next_idx >= len(df):
         return next_idx, "<h2>Todos os vídeos concluídos!</h2>", "", gr.update(visible=False), "✅ Salvo com sucesso!", ARQUIVO_RESULTADOS
         
    prox_linha = df.iloc[next_idx]
    html = render_tiktok(prox_linha['url'])
    target = f"**Target:** {prox_linha['target']}"
    
    return next_idx, html, target, None, "✅ Salvo com sucesso!", ARQUIVO_RESULTADOS

# Constrói a Interface
with gr.Blocks(theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 📊 Rotulação de Posição (Stance) - TikTok")
    
    with gr.Group(visible=True) as tela_login:
        nome_input = gr.Textbox(label="Digite seu nome (Anotador) para começar:")
        btn_entrar = gr.Button("Entrar", variant="primary")
        msg_login = gr.Markdown()
        
    with gr.Group(visible=False) as tela_anotacao:
        with gr.Row():
            user_lbl = gr.Markdown()
            status_msg = gr.Markdown()
            
        index_state = gr.State(value=0)
        target_lbl = gr.Markdown()
        
        video_html = gr.HTML()
        
        stance_radio = gr.Radio(
            choices=["A Favor", "Contra", "Neutro", "Não Relacionado", "Erro/Vídeo Indisponível"],
            label="Qual a posição expressa neste vídeo em relação ao target?"
        )
        btn_salvar = gr.Button("Salvar e Próximo", variant="primary")
        
        gr.Markdown("---")
        gr.Markdown("### 📥 Backup dos Dados")
        btn_download = gr.File(label="Baixe o CSV de resultados aqui", value=ARQUIVO_RESULTADOS if os.path.exists(ARQUIVO_RESULTADOS) else None)

    # Eventos de clique
    btn_entrar.click(
        fn=get_first_video,
        inputs=[nome_input],
        outputs=[tela_login, tela_anotacao, user_lbl, index_state, video_html, target_lbl]
    )
    
    btn_salvar.click(
        fn=salvar_e_proximo,
        inputs=[user_lbl, index_state, stance_radio],
        outputs=[index_state, video_html, target_lbl, stance_radio, status_msg, btn_download]
    )

demo.launch()