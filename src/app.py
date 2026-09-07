import json
import csv
import hashlib
import math
import threading
import time
import requests
from html import escape
from datetime import datetime, timezone, timedelta
from pathlib import Path

import gradio as gr
import pandas as pd

# ==============================================================================
# CONFIGURAÇÕES E ARQUIVOS BASE
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent.parent
VIDEOS_FILE = BASE_DIR / "videos.csv"
RESULTS_FILE = BASE_DIR / "resultados_anotacao.csv"
ASSIGNMENTS_FILE = BASE_DIR / "atribuicoes.csv"
CONFIG_FILE = BASE_DIR / "configuracao.json"
LOG_FILE = BASE_DIR / "acessos_log.csv"
ERRORS_FILE = BASE_DIR / "erros_videos.csv"
AGREEMENT_FILE = BASE_DIR / "concordancia.json"

FILE_LOCK = threading.Lock()
STANCE_OPTIONS = [
    "Contra",
    "A Favor",
    "Neutro",
    "Vídeo não relacionado ao target"
]
BRT_TZ = timezone(timedelta(hours=-3))

if not VIDEOS_FILE.exists():
    raise FileNotFoundError(f"\n[ERRO CRÍTICO] Arquivo '{VIDEOS_FILE.name}' ausente. A execução foi abortada.")

# ==============================================================================
# FUNÇÕES DE RETAGUARDA (DADOS E LÓGICA)
# ==============================================================================
def clean_users(value):
    users = (value or "").replace(",", "\n").splitlines()
    return list(dict.fromkeys(user.strip() for user in users if user.strip()))

def load_videos():
    videos = pd.read_csv(VIDEOS_FILE, dtype={
        "id": str,
        "url": str,
        "target": str,
        "video_description": str,
        "voice_to_text": str,
        "video_duration": str,
    })
    missing = {"id", "url", "target"}.difference(videos.columns)
    if missing:
        raise ValueError(f"videos.csv não possui as colunas obrigatórias: {', '.join(sorted(missing))}")
    return videos.fillna("")

def save_config(config):
    with CONFIG_FILE.open("w", encoding="utf-8") as config_file:
        json.dump(config, config_file, ensure_ascii=False, indent=2)

def load_config():
    if not CONFIG_FILE.exists():
        default_config = {
            "admins": ["admin"],
            "labelers": [],
            "videos_per_labeler": 10,
            "overlap_percent": 0.0,
            "tarefa": "Detecção de Posição"
        }
        save_config(default_config)
        return default_config
    try:
        with CONFIG_FILE.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError):
        raise ValueError("configuracao.json inválido ou corrompido.")

    config["admins"] = clean_users("\n".join(config.get("admins", [])))
    config["labelers"] = clean_users("\n".join(config.get("labelers", [])))
    config["videos_per_labeler"] = int(config.get("videos_per_labeler", 10))
    config["overlap_percent"] = float(config.get("overlap_percent", 0.0))
    config["tarefa"] = config.get("tarefa", "Detecção de Posição")
    return config

def registrar_log(username, acao, tempo_sessao=0.0):
    row = {
        "timestamp": datetime.now(BRT_TZ).isoformat(),
        "usuario": username,
        "acao": acao,
        "tempo_desde_login_segundos": round(tempo_sessao, 2)
    }
    columns = ["timestamp", "usuario", "acao", "tempo_desde_login_segundos"]
    with FILE_LOCK:
        append_csv_row(LOG_FILE, row, columns)


def assignment_version(config, videos):
    video_signature = videos[["id", "url", "target"]].to_csv(index=False, lineterminator="\n")
    config_signature = json.dumps({
        "labelers": config.get("labelers", []),
        "overlap_percent": float(config.get("overlap_percent", 0)),
    }, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(f"{video_signature}\n{config_signature}".encode("utf-8")).hexdigest()
    return f"v{digest[:12]}"


def assignment_identifier(version, labeler, video_id):
    return f"{version}:{labeler}:{video_id}"

def read_results():
    columns = ["timestamp", "labeler", "video_id", "url", "target", "stance", "tempo_analise_segundos", "video_duration", "assignment_id"]
    if not RESULTS_FILE.exists() or RESULTS_FILE.stat().st_size == 0:
        return pd.DataFrame(columns=columns)

    try:
        df = pd.read_csv(RESULTS_FILE, dtype=str)
        if df.empty:
            return pd.DataFrame(columns=columns)
        if set(columns).issubset(set(df.columns)):
            df = df[columns].fillna("")
            missing_ids = df["assignment_id"].astype(str).str.strip().eq("")
            df.loc[missing_ids, "assignment_id"] = df.loc[missing_ids].apply(
                lambda row: f"legacy:{row['labeler']}:{row['video_id']}", axis=1
            )
            return df.drop_duplicates(subset=["assignment_id"], keep="last")
        if set(columns[:-2]).issubset(set(df.columns)):
            df["video_duration"] = ""
            df["assignment_id"] = df.apply(lambda row: f"legacy:{row['labeler']}:{row['video_id']}", axis=1)
            return df[columns].fillna("").drop_duplicates(subset=["assignment_id"], keep="last")
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        pass

    try:
        df = pd.read_csv(RESULTS_FILE, header=None, dtype=str)
        if df.empty:
            return pd.DataFrame(columns=columns)
        if df.shape[1] >= len(columns) - 2:
            fixed = df.iloc[:, :len(columns) - 2].copy()
            fixed["video_duration"] = ""
            fixed["assignment_id"] = fixed.apply(lambda row: f"legacy:{row['labeler']}:{row['video_id']}", axis=1)
            fixed.columns = columns
            return fixed.fillna("").drop_duplicates(subset=["assignment_id"], keep="last")
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        pass

    return pd.DataFrame(columns=columns)


def read_error_assignments():
    columns = ["timestamp", "video_id", "url", "labeler", "assignment_id"]
    if not ERRORS_FILE.exists() or ERRORS_FILE.stat().st_size == 0:
        return pd.DataFrame(columns=columns)
    with ERRORS_FILE.open(encoding="utf-8", newline="") as error_file:
        rows = list(csv.reader(error_file))
    if not rows:
        return pd.DataFrame(columns=columns)
    if rows[0][:4] == columns[:4]:
        rows = rows[1:]
    normalized = []
    for row in rows:
        if len(row) < 4:
            continue
        timestamp, video_id, url, labeler = row[:4]
        assignment_id = row[4].strip() if len(row) >= 5 and row[4].strip() else f"legacy:{labeler}:{video_id}"
        normalized.append({
            "timestamp": timestamp,
            "video_id": video_id,
            "url": url,
            "labeler": labeler,
            "assignment_id": assignment_id,
        })
    return pd.DataFrame(normalized, columns=columns).drop_duplicates(subset=["assignment_id"], keep="last")


def ensure_results_schema():
    columns = ["timestamp", "labeler", "video_id", "url", "target", "stance", "tempo_analise_segundos", "video_duration", "assignment_id"]
    if not RESULTS_FILE.exists() or RESULTS_FILE.stat().st_size == 0:
        return
    try:
        df = pd.read_csv(RESULTS_FILE, dtype=str).fillna("")
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return
    videos = load_videos().set_index("id")
    if "video_duration" not in df.columns:
        df["video_duration"] = ""
    if "assignment_id" not in df.columns:
        df["assignment_id"] = df.apply(lambda row: f"legacy:{row['labeler']}:{row['video_id']}", axis=1)
    for index, row in df.iterrows():
        if not str(row.get("video_duration", "")).strip():
            df.at[index, "video_duration"] = videos["video_duration"].get(str(row.get("video_id", "")), "")
    df = df.reindex(columns=[column for column in columns if column in df.columns])
    df.to_csv(RESULTS_FILE, index=False)


def append_csv_row(file_path, row, columns):
    file_exists = file_path.exists()
    header = not file_exists or file_path.stat().st_size == 0
    pd.DataFrame([row], columns=columns).to_csv(file_path, mode="a", header=header, index=False)


def initialize_results_file():
    columns = ["timestamp", "labeler", "video_id", "url", "target", "stance", "tempo_analise_segundos", "video_duration", "assignment_id"]
    if not RESULTS_FILE.exists() or RESULTS_FILE.stat().st_size == 0:
        pd.DataFrame(columns=columns).to_csv(RESULTS_FILE, index=False)

def render_tiktok(url):
    video_id = str(url).rstrip("/").split("/")[-1].split("?")[0]
    player_url = f"https://www.tiktok.com/player/v1/{escape(video_id)}?music_info=1&description=1&rel=0"
    html = f"""
    <div style="display: flex; justify-content: center; width: 100%; margin: 0; padding: 0;">
        <iframe
            style="width: min(100%, 600px); height: 720px; border: none; overflow: hidden; display: block; background: #ffffff; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);"
            scrolling="no"
            src="{player_url}"
            title="Player do vídeo TikTok"
            allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
            allowfullscreen
        ></iframe>
    </div>
    """
    return html


def render_video_card(item):
    description = str(item.get("video_description", "")).strip()
    transcription = str(item.get("voice_to_text", "")).strip()
    description_html = escape(description).replace("\n", "<br>") if description else "Descrição não disponível em videos.csv."
    transcription_html = escape(transcription).replace("\n", "<br>") if transcription else "Transcrição indisponível para este vídeo."
    description_class = "video-description" if description else "video-description unavailable"
    transcription_class = "video-transcription" if transcription else "video-transcription unavailable"
    return f"""
    <div class='video-card'>
        <div class='video-meta'><strong>Target:</strong> {escape(str(item.get('target', '')))}</div>
        <div class='video-content-grid'>
            <div class='video-context-column'>
                <div class='{description_class}'><strong>Descrição do vídeo</strong><div>{description_html}</div></div>
                <div class='{transcription_class}'><strong>Transcrição do áudio <span class='transcription-note'>(pode conter erros)</span></strong><div>{transcription_html}</div></div>
            </div>
            <div class='video-player-column'>{render_tiktok(item['url'])}</div>
        </div>
    </div>
    """

def make_assignments(config, videos):
    labelers = config.get("labelers", [])
    overlap_percent = max(0.0, min(100.0, float(config.get("overlap_percent", 0))))
    version = assignment_version(config, videos)
    assignment_columns = ["assignment_version", "assignment_id", "labeler", "video_id", "url", "target", "video_description", "voice_to_text", "video_duration"]

    if not labelers or len(videos) == 0:
        return pd.DataFrame(columns=assignment_columns)

    overlap_count = int((overlap_percent / 100.0) * len(videos))

    assignments = []

    for video_index in range(overlap_count):
        video = videos.iloc[video_index]
        for labeler in labelers:
            assignments.append({
                "assignment_version": version, "assignment_id": assignment_identifier(version, labeler, video["id"]), "labeler": labeler, "video_id": str(video["id"]), "url": video["url"], "target": video["target"], "video_description": video.get("video_description", ""), "voice_to_text": video.get("voice_to_text", ""), "video_duration": video.get("video_duration", "")
            })

    for offset, video_index in enumerate(range(overlap_count, len(videos))):
        labeler = labelers[offset % len(labelers)]
        video = videos.iloc[video_index]
        assignments.append({
            "assignment_version": version, "assignment_id": assignment_identifier(version, labeler, video["id"]), "labeler": labeler, "video_id": str(video["id"]), "url": video["url"], "target": video["target"], "video_description": video.get("video_description", ""), "voice_to_text": video.get("voice_to_text", ""), "video_duration": video.get("video_duration", "")
        })

    result = pd.DataFrame(assignments, columns=assignment_columns)
    result = result.drop_duplicates(subset=["labeler", "video_id"], keep="last").reset_index(drop=True)
    result.to_csv(ASSIGNMENTS_FILE, index=False)
    return result

def load_assignments(config, videos):
    return make_assignments(config, videos)


def calculate_agreement(results):
    labels_by_video = results[results["stance"].isin(STANCE_OPTIONS)].groupby("video_id")["stance"].apply(list)
    usable = labels_by_video[labels_by_video.map(len) >= 2]
    category_order = {label: index for index, label in enumerate(["Contra", "Neutro", "A Favor", "Vídeo não relacionado ao target"])}

    if usable.empty:
        return {"metrica": "Alfa de Krippendorff (ordinal)", "alfa": None, "videos_com_multiplas_anotacoes": 0, "anotacoes_consideradas": 0, "videos_com_discordancia": [], "labelers_com_maior_discordancia": []}

    observed_disagreement = 0.0
    pair_count = 0
    disagreement_videos = []
    for video_id, labels in usable.items():
        distances = []
        for left_index in range(len(labels)):
            for right_index in range(left_index + 1, len(labels)):
                distances.append((category_order[labels[left_index]] - category_order[labels[right_index]]) ** 2)
        observed_disagreement += sum(distances)
        pair_count += len(distances)
        if any(distance > 0 for distance in distances):
            disagreement_videos.append({"video_id": str(video_id), "anotacoes": labels, "discordancia_ordinal": round(sum(distances) / len(distances), 4)})

    all_labels = [label for labels in usable for label in labels]
    expected_disagreement = sum(
        (category_order[all_labels[left_index]] - category_order[all_labels[right_index]]) ** 2
        for left_index in range(len(all_labels))
        for right_index in range(left_index + 1, len(all_labels))
    )
    expected_pairs = len(all_labels) * (len(all_labels) - 1) / 2
    alpha = None if pair_count == 0 or expected_disagreement == 0 else 1 - (observed_disagreement / pair_count) / (expected_disagreement / expected_pairs)
    disagreement_videos.sort(key=lambda item: item["discordancia_ordinal"], reverse=True)
    labeler_disagreements = {}
    valid_results = results[results["video_id"].isin(usable.index)]
    for video_id, video_results in valid_results.groupby("video_id"):
        labels = video_results["stance"].tolist()
        for row in video_results.itertuples():
            other_labels = [label for label in labels if label != row.stance]
            if other_labels and row.stance != max(set(other_labels), key=other_labels.count):
                labeler_disagreements[row.labeler] = labeler_disagreements.get(row.labeler, 0) + 1
    labelers = [{"labeler": labeler, "discordancias": count} for labeler, count in labeler_disagreements.items()]
    labelers.sort(key=lambda item: item["discordancias"], reverse=True)
    return {"metrica": "Alfa de Krippendorff (ordinal)", "alfa": round(alpha, 6) if alpha is not None else None, "videos_com_multiplas_anotacoes": int(len(usable)), "anotacoes_consideradas": int(len(all_labels)), "videos_com_discordancia": disagreement_videos, "labelers_com_maior_discordancia": labelers}


def find_fast_annotations(results, videos, tolerance_seconds=5.0):
    if results.empty or videos.empty:
        return []
    durations = videos.set_index(videos["id"].astype(str))["video_duration"].to_dict()
    alerts = []
    for row in results.itertuples():
        try:
            duration = float(row.video_duration or durations.get(str(row.video_id), ""))
            analysis_time = float(row.tempo_analise_segundos)
        except (TypeError, ValueError):
            continue
        if analysis_time < duration - tolerance_seconds:
            alerts.append({
                "video_id": str(row.video_id),
                "labeler": str(row.labeler),
                "video_duration": round(duration, 2),
                "tempo_analise_segundos": round(analysis_time, 2),
                "diferenca_segundos": round(duration - analysis_time, 2),
            })
    return sorted(alerts, key=lambda item: item["diferenca_segundos"], reverse=True)


def agreement_summary(results):
    agreement = calculate_agreement(results)
    with AGREEMENT_FILE.open("w", encoding="utf-8") as agreement_file:
        json.dump(agreement, agreement_file, ensure_ascii=False, indent=2)
    alpha = "ainda não calculável" if agreement["alfa"] is None else f"{agreement['alfa']:.3f}"
    labeler_rows = "".join(
        f"<tr><td>{item['labeler']}</td><td>{item['discordancias']}</td></tr>"
        for item in agreement["labelers_com_maior_discordancia"][:10]
    ) or "<tr><td colspan='2' class='muted'>Ainda não há discordâncias mensuráveis.</td></tr>"
    video_rows = "".join(
        f"<tr><td>{item['video_id']}</td><td>{' · '.join(item['anotacoes'])}</td><td>{item['discordancia_ordinal']:.2f}</td></tr>"
        for item in agreement["videos_com_discordancia"][:10]
    ) or "<tr><td colspan='3' class='muted'>Ainda não há vídeos com múltiplas anotações discordantes.</td></tr>"
    fast_annotations = find_fast_annotations(results, load_videos())
    fast_warning = ""
    if fast_annotations:
        fast_rows = "".join(
            f"<tr><td>{item['video_id']}</td><td>{item['labeler']}</td><td>{item['video_duration']:.1f}s</td><td>{item['tempo_analise_segundos']:.1f}s</td><td>{item['diferenca_segundos']:.1f}s</td></tr>"
            for item in fast_annotations[:20]
        )
        fast_warning = f"""
        <section class='duration-alert'>
            <h3>⚠️ Anotações mais rápidas que o vídeo</h3>
            <p>Foram sinalizadas análises com mais de 5 segundos abaixo da duração do vídeo.</p>
            <table><thead><tr><th>Vídeo</th><th>Rotulador</th><th>Duração</th><th>Análise</th><th>Diferença</th></tr></thead><tbody>{fast_rows}</tbody></table>
        </section>
        """
    return f"""
    <section class='agreement-dashboard'>
        {fast_warning}
        <div class='agreement-kpis'>
            <div class='kpi'><span>Alfa ordinal</span><strong>{alpha}</strong></div>
            <div class='kpi'><span>Vídeos comparáveis</span><strong>{agreement['videos_com_multiplas_anotacoes']}</strong></div>
            <div class='kpi'><span>Anotações consideradas</span><strong>{agreement['anotacoes_consideradas']}</strong></div>
        </div>
        <div class='dashboard-grid'>
            <section class='dashboard-card'>
                <h3>Rotuladores que mais destoam</h3>
                <table><thead><tr><th>Rotulador</th><th>Discordâncias</th></tr></thead><tbody>{labeler_rows}</tbody></table>
            </section>
            <section class='dashboard-card'>
                <h3>Vídeos com maior discordância</h3>
                <table><thead><tr><th>Vídeo</th><th>Anotações</th><th>Distância</th></tr></thead><tbody>{video_rows}</tbody></table>
            </section>
        </div>
    </section>
    """

def user_queue(username, config, videos):
    assignments = load_assignments(config, videos)
    return assignments.loc[assignments["labeler"] == username].to_dict("records")


def completed_position(username, queue, results, errors=None):
    if not queue:
        return 0
    user_results = results[results["labeler"] == username] if "labeler" in results.columns else pd.DataFrame()
    user_errors = errors[errors["labeler"] == username] if errors is not None and "labeler" in errors.columns else pd.DataFrame()
    completed_ids = set(user_results.get("assignment_id", pd.Series(dtype=str)).astype(str).str.strip())
    completed_ids.update(user_errors.get("assignment_id", pd.Series(dtype=str)).astype(str).str.strip())
    completed_video_ids = set(user_results.get("video_id", pd.Series(dtype=str)).astype(str).str.strip())
    completed_video_ids.update(user_errors.get("video_id", pd.Series(dtype=str)).astype(str).str.strip())
    for position, item in enumerate(queue):
        assignment_id = str(item["assignment_id"]).strip()
        video_id = str(item["video_id"]).strip()
        is_legacy_assignment = assignment_id.startswith("legacy:")
        completed = assignment_id in completed_ids or (is_legacy_assignment and video_id in completed_video_ids)
        if not completed:
            return position
    return len(queue)

# ==============================================================================
# FUNÇÕES DA API DO TIKTOK E BARRA DE PROGRESSO
# ==============================================================================
def check_tiktok_validity(url):
    """
    Exige não apenas código 200, mas a presença do HTML do oEmbed.
    Isso impede que telas de 'overload-protect' (que retornam 200 sem JSON válido) passem.
    """
    try:
        api_url = f"https://www.tiktok.com/oembed?url={url}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        res = requests.get(api_url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if "html" in data and "video_id" in data:
                return True
        return False
    except:
        return False

def generate_progress_bar(position, total):
    percent = int((position / total) * 100) if total > 0 else 100
    html = f"""
    <div style="width: 100%; margin-bottom: 25px;">
        <div style="width: 100%; background-color: #e0e0e0; border-radius: 6px; height: 12px; overflow: hidden; box-shadow: inset 0 1px 3px rgba(0,0,0,0.1);">
            <div style="width: {percent}%; background-color: #9c0014; height: 100%; transition: width 0.4s ease;"></div>
        </div>
        <div style="text-align: right; font-size: 14px; color: #333; font-weight: 600; margin-top: 6px;">
            {position} de {total} ({percent}%)
        </div>
    </div>
    """
    return html

def video_outputs(queue, position, username_clean):
    total = len(queue)

    if position >= total:
        html_fim = '<div class="empty-video"><h2 style="color:#9c0014; margin-top:0;">Fila Concluída</h2><p>Você rotulou todos os vídeos atribuídos a você nesta rodada. Aguarde novas instruções. Obrigado!</p></div>'
        pb = generate_progress_bar(total, total)
        return position, html_fim, gr.update(visible=False), "", pb

    item = queue[position]
    target = str(item["target"])
    pergunta_markdown = f'<div class="target-question">Com relação ao target <strong>"{escape(target)}"</strong>, como você classifica a posição deste vídeo?</div>'
    pb = generate_progress_bar(position, total)

    return position, render_video_card(item), gr.update(visible=True), pergunta_markdown, pb

# ==============================================================================
# EVENTOS DA INTERFACE
# ==============================================================================
def authenticate(username):
    username = (username or "").strip()
    config = load_config()
    videos = load_videos()
    agora = time.time()

    blank_admin = (
        "\n".join(config.get("admins", [])),
        "\n".join(config.get("labelers", [])),
        config.get("tarefa", "Detecção de Posição"),
        config.get("videos_per_labeler", 10),
        config.get("overlap_percent", 0),
        "", pd.DataFrame(), gr.update(), gr.update(), gr.update(), "", gr.update()
    )

    if not username:
        return (gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), "⚠️ Informe seu usuário.", "", [], 0, 0.0, 0.0, "", gr.update(visible=False), "", "", *blank_admin)

    if username in config.get("admins", []):
        registrar_log(username, "LOGIN_ADMIN", 0)
        assignments = load_assignments(config, videos)
        results = read_results()
        counts = assignments.groupby("labeler").size().to_dict() if not assignments.empty else {}
        count_text = ", ".join(f"{labeler}: {count}" for labeler, count in counts.items()) or "nenhuma"
        admin_msg = f"📊 {len(videos)} vídeos carregados; {len(results)} anotações coletadas; {len(assignments)} atribuições ativas. Carga: {count_text}."
        file_res = gr.update(value=str(RESULTS_FILE) if RESULTS_FILE.exists() else None)
        file_log = gr.update(value=str(LOG_FILE) if LOG_FILE.exists() else None)
        file_err = gr.update(value=str(ERRORS_FILE) if ERRORS_FILE.exists() else None)
        agreement_msg = agreement_summary(results)
        file_agreement = gr.update(value=str(AGREEMENT_FILE))

        return (gr.update(visible=False), gr.update(visible=True), gr.update(visible=False), "", "", [], 0, 0.0, 0.0, "", gr.update(visible=False), "", "", "\n".join(config.get("admins", [])), "\n".join(config.get("labelers", [])), config.get("tarefa", "Detecção de Posição"), config.get("videos_per_labeler", 10), config.get("overlap_percent", 0), admin_msg, assignments, file_res, file_log, file_err, agreement_msg, file_agreement)

    if username in config.get("labelers", []):
        registrar_log(username, "LOGIN_LABELER", 0)
        queue = user_queue(username, config, videos)
        position = completed_position(username, queue, read_results(), read_error_assignments())
        if not queue:
            new_position, video_html, controls_visible, pergunta, progress = video_outputs([], 0, username)
            return (gr.update(visible=False), gr.update(visible=False), gr.update(visible=True), "", f"**Rotulador:** {username}", [], new_position, agora, agora, video_html, controls_visible, pergunta, progress, *blank_admin)

        new_position, video_html, controls_visible, pergunta, progress = video_outputs(queue, position, username)
        return (gr.update(visible=False), gr.update(visible=False), gr.update(visible=True), "", f"**Rotulador:** {username}", queue, new_position, agora, agora, video_html, controls_visible, pergunta, progress, *blank_admin)

    return (gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), "⚠️ Usuário não autorizado.", "", [], 0, 0.0, 0.0, "", gr.update(visible=False), "", "", *blank_admin)

def reset_to_login(message="Sessão encerrada. Seu progresso foi salvo e será retomado no próximo login."):
    config = load_config()
    blank_admin = (
        "\n".join(config.get("admins", [])),
        "\n".join(config.get("labelers", [])),
        config.get("tarefa", "Detecção de Posição"),
        config.get("videos_per_labeler", 10),
        config.get("overlap_percent", 0),
        "", pd.DataFrame(), gr.update(), gr.update(), gr.update(), "", gr.update()
    )
    return (gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), message, "", [], 0, 0.0, 0.0, "", gr.update(visible=False), "", "", *blank_admin)

def save_and_exit(username):
    username_clean = str(username).replace("**Rotulador:** ", "").strip()
    if username_clean:
        registrar_log(username_clean, "SALVOU_E_SAIU", 0)
    return reset_to_login()

def save_admin_settings(admin_users, labeler_users, tarefa, videos_per_labeler, overlap_percent):
    videos = load_videos()
    admins = clean_users(admin_users)
    labelers = clean_users(labeler_users)

    if not admins or not labelers:
        return "Erro: Adicione ao menos um administrador e um rotulador.", pd.DataFrame(), gr.update(), gr.update(), gr.update()

    config = {
        "admins": admins,
        "labelers": labelers,
        "videos_per_labeler": 0,
        "overlap_percent": float(overlap_percent),
        "tarefa": str(tarefa).strip()
    }
    save_config(config)
    assignments = make_assignments(config, videos)

    per_labeler = math.ceil(len(assignments) / len(labelers)) if labelers else 0
    config["videos_per_labeler"] = per_labeler
    save_config(config)
    counts = assignments.groupby("labeler").size().to_dict() if not assignments.empty else {}
    count_text = ", ".join(f"{labeler}: {count}" for labeler, count in counts.items()) or "nenhuma"
    return f"✅ {len(videos)} vídeos; {len(assignments)} atribuições geradas. Carga: {count_text}.", assignments, gr.update(value=str(RESULTS_FILE) if RESULTS_FILE.exists() else None), gr.update(value=str(LOG_FILE) if LOG_FILE.exists() else None), gr.update(value=str(ERRORS_FILE) if ERRORS_FILE.exists() else None), gr.update(value=per_labeler)

def process_annotation(username, queue, position, stance, login_time, start_time):
    username_clean = str(username).replace("**Rotulador:** ", "")
    agora = time.time()

    if not queue or position >= len(queue):
        new_pos, video_html, controls_visible, pergunta, progress = video_outputs(queue, position, username_clean)
        return new_pos, agora, video_html, controls_visible, pergunta, progress, "Fila concluída.", None

    if not stance:
        new_pos, video_html, controls_visible, pergunta, progress = video_outputs(queue, position, username_clean)
        return position, start_time, video_html, controls_visible, pergunta, progress, "⚠️ Selecione uma opção antes de salvar.", gr.update()

    item = queue[position]
    tempo_analise = agora - start_time

    if stance == "ERRO_MANUAL_TIMEOUT":
        if tempo_analise < 10:
            new_pos, video_html, controls_visible, pergunta, progress = video_outputs(queue, position, username_clean)
            aviso = f"⏳ Aguarde 10s para selecionar esta opção. O vídeo ainda pode carregar. (Faltam {int(10 - tempo_analise)}s)"
            return position, start_time, video_html, controls_visible, pergunta, progress, aviso, gr.update()
        else:
            error_row = {
                "timestamp": datetime.now(BRT_TZ).isoformat(),
                "video_id": item["video_id"],
                "url": item["url"],
                "labeler": username_clean,
                "assignment_id": item.get("assignment_id", f"legacy:{username_clean}:{item['video_id']}")
            }
            with FILE_LOCK:
                append_csv_row(ERRORS_FILE, error_row, ["timestamp", "video_id", "url", "labeler", "assignment_id"])
    else:
        row = {
            "timestamp": datetime.now(BRT_TZ).isoformat(),
            "labeler": username_clean,
            "video_id": item["video_id"],
            "url": item["url"],
            "target": item["target"],
            "stance": stance,
            "tempo_analise_segundos": round(tempo_analise, 2),
            "video_duration": item.get("video_duration", ""),
            "assignment_id": item.get("assignment_id", f"legacy:{username_clean}:{item['video_id']}")
        }
        with FILE_LOCK:
            ensure_results_schema()
            append_csv_row(RESULTS_FILE, row, ["timestamp", "labeler", "video_id", "url", "target", "stance", "tempo_analise_segundos", "video_duration", "assignment_id"])
            if RESULTS_FILE.stat().st_size == 0:
                raise IOError("Não foi possível persistir a anotação em resultados_anotacao.csv.")

    registrar_log(username_clean, "SALVOU_ANOTACAO", agora - login_time)
    agreement_summary(read_results())

    next_position = position + 1
    new_position, video_html, controls_visible, pergunta, progress = video_outputs(queue, next_position, username_clean)

    return new_position, agora, video_html, controls_visible, pergunta, progress, "", gr.update(value=None)

def save_annotation_normal(username, queue, position, stance, login_time, start_time):
    return process_annotation(username, queue, position, stance, login_time, start_time)

def save_annotation_error(username, queue, position, login_time, start_time):
    return process_annotation(username, queue, position, "ERRO_MANUAL_TIMEOUT", login_time, start_time)


# ==============================================================================
# INTERFACE E ESTILOS HORIZONTAIS ABSOLUTAMENTE BRANCOS
# ==============================================================================
APP_CSS = """
    /* Extermínio Total de Fundos Escuros/Cinzas (Textboxes, Tables, Wrappers) */
    html, body, .gradio-container, .gr-form, .gr-box, .gr-panel, fieldset, .gap {
        background-color: #ffffff !important; background: #ffffff !important; color: #232323 !important; color-scheme: light !important;
    }

    input, textarea, select, .gr-input, table, th, td, tr, tbody, thead {
        background-color: #ffffff !important; background: #ffffff !important; color: #232323 !important;
    }

    .shell {
        width: calc(100vw - 24px) !important; max-width: none !important; margin: 12px auto !important; background: #ffffff !important;
        padding: 28px 36px !important; border-radius: 8px !important; font-size: 17px !important;
        box-shadow: 0 4px 15px rgba(0,0,0,0.08) !important; border-top: 6px solid #9c0014 !important;
    }

    .gradio-container, .gradio-container .main, .gradio-container .contain { max-width: none !important; width: 100% !important; }
    .agreement-dashboard { width: 100%; margin: 10px 0 24px; color: #232323; }
    .agreement-kpis { display: grid; grid-template-columns: repeat(3, minmax(160px, 1fr)); gap: 14px; margin: 12px 0 18px; }
    .kpi { border: 1px solid #e1e1e1; border-left: 4px solid #9c0014; padding: 14px 16px; background: #fffafa; }
    .kpi span { display: block; color: #666; font-size: 15px; text-transform: uppercase; }
    .kpi strong { display: block; color: #9c0014; font-size: 28px; margin-top: 4px; }
    .dashboard-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.5fr); gap: 20px; }
    .dashboard-card { border: 1px solid #e1e1e1; padding: 16px; background: #ffffff; min-width: 0; }
    .dashboard-card h3 { margin: 0 0 12px; color: #333; font-size: 19px; }
    .dashboard-card table { width: 100%; border-collapse: collapse; background: #ffffff !important; }
    .dashboard-card th, .dashboard-card td { padding: 11px 12px; border-bottom: 1px solid #ededed; text-align: left; background: #ffffff !important; color: #232323 !important; font-size: 16px; }
    .dashboard-card th { color: #666 !important; font-weight: 700; }
    .muted { color: #888 !important; }
    .duration-alert { margin: 14px 0 20px; padding: 16px; border: 1px solid #e0a400; border-left: 5px solid #c48700; background: #fff9e6; color: #4a3a00; }
    .duration-alert h3 { margin: 0 0 6px; color: #8a6200; font-size: 20px; }
    .duration-alert p { margin: 0 0 12px; font-size: 16px; }
    .duration-alert table { width: 100%; border-collapse: collapse; background: #fff9e6 !important; }
    .duration-alert th, .duration-alert td { padding: 10px 12px; border-bottom: 1px solid #eadcae; text-align: left; background: #fff9e6 !important; color: #4a3a00 !important; font-size: 16px; }
    .duration-alert th { font-weight: 700; }

    .assignment-table, .assignment-table *,
    .assignment-table .table-wrap, .assignment-table .dataframe,
    .assignment-table [data-testid="dataframe"], .assignment-table [role="grid"], .assignment-table [role="row"], .assignment-table [role="gridcell"] {
        background: #ffffff !important; background-color: #ffffff !important; color: #232323 !important; color-scheme: light !important;
    }
    .assignment-table, .assignment-table [data-testid="dataframe"] { --color-background-fill-primary: #ffffff !important; --color-background-fill-secondary: #f7f7f7 !important; --color-block-background-fill: #ffffff !important; --color-border-primary: #e5e5e5 !important; }
    .assignment-table [role="columnheader"], .assignment-table th { background: #f7f7f7 !important; color: #333333 !important; }
    .assignment-table [role="gridcell"], .assignment-table td { border-color: #e5e5e5 !important; color: #232323 !important; }
    .assignment-table input, .assignment-table textarea { background: #ffffff !important; color: #232323 !important; }
    html, body { overflow-x: hidden !important; }
    .labeler-panel, .labeler-panel .wrap, .labeler-panel .block { overflow-x: visible !important; }
    .labeler-panel { position: static !important; font-size: 19px !important; }
    .video-card { width: 100%; min-width: 0; color: #232323; overflow: visible; }
    .video-card > div:first-child { margin-top: 0; }
    .video-card iframe { display: block; width: min(100%, 600px) !important; max-width: 600px !important; height: 720px !important; overflow: hidden !important; }
    .video-meta { padding: 14px 16px; margin-bottom: 14px; border-left: 4px solid #9c0014; background: #fffafa; font-size: 16px; }
    .video-content-grid { display: grid; grid-template-columns: minmax(360px, 0.8fr) minmax(0, 1.2fr); gap: 28px; align-items: start; }
    .video-player-column, .video-context-column { min-width: 0; }
    .video-player-column { display: flex; justify-content: center; align-items: flex-start; height: 720px; overflow: hidden; }
    .video-description { padding: 18px; margin-bottom: 18px; border: 1px solid #e1e1e1; background: #ffffff; line-height: 1.55; overflow-wrap: anywhere; font-size: 16px; }
    .video-description strong { display: block; color: #9c0014; margin-bottom: 6px; }
    .video-description.unavailable { color: #777; }
    .video-transcription { padding: 18px; margin: 0 0 14px; border: 1px solid #e1e1e1; background: #fbfbfb; line-height: 1.55; overflow-wrap: anywhere; font-size: 16px; }
    .video-transcription strong { display: block; color: #9c0014; margin-bottom: 6px; }
    .transcription-note { color: #666; font-size: 0.82em; font-style: italic; font-weight: 400; }
    .video-transcription.unavailable { color: #777; }
    .labeler-layout { align-items: flex-start !important; }
    .video-column, .labeler-controls-column { min-width: 0 !important; }
    .shell { position: relative !important; }
    .brand-header { text-align: left !important; padding-left: 8px; }
    .labeler-actions { position: absolute !important; top: 24px; right: 36px; width: 440px !important; z-index: 10; }
    .labeler-actions button { margin-top: 0 !important; }
    .admin-actions { position: absolute !important; top: 24px; right: 36px; width: 220px !important; z-index: 10; }
    .admin-actions button { margin-top: 0 !important; }
    .annotation-controls { gap: 0 !important; row-gap: 0 !important; }
    .annotation-controls > .form, .annotation-controls > .block { margin-top: 0 !important; }
    .labeler-warning-stack { font-size: 15px; font-style: italic; font-weight: 400; color: #666; line-height: 1.5; margin: 0 0 8px; padding: 0; }
    .labeler-warning-stack div + div { margin-top: 2px; }
    .target-question { margin: 4px 0 12px; color: #232323; font-size: 18px; line-height: 1.5; font-weight: 400; }
    .target-question strong { font-weight: 700 !important; color: #171717; }

    @media (max-width: 850px) {
        .shell { width: calc(100vw - 24px) !important; padding: 22px 16px !important; margin: 12px auto !important; }
        .agreement-kpis, .dashboard-grid { grid-template-columns: 1fr; }
        .labeler-layout { flex-direction: column !important; }
        .video-content-grid { grid-template-columns: 1fr; }
        .video-card iframe { width: 100% !important; max-width: 650px !important; }
        .labeler-actions { position: static !important; width: 100% !important; margin-bottom: 16px; }
        .admin-actions { position: static !important; width: 100% !important; margin-bottom: 16px; }
    }

    .brand-header { border-bottom: 1px solid #eaeaea; padding-bottom: 25px; margin-bottom: 30px; text-align: left; }
    .brand-header h1 { font-size: 28px !important; font-weight: 700 !important; margin: 0 !important; color: #9c0014 !important; }
    .brand-header p { color: #444444 !important; margin: 10px 0 0 !important; font-size: 16px !important; line-height: 1.5; }

    button.ufmg-btn {
        background: #9c0014 !important; border: none !important; color: #ffffff !important;
        font-weight: 600 !important; font-size: 17px !important; padding: 13px 21px !important; width: 100% !important;
    }
    .admin-panel, .admin-panel label, .admin-panel input, .admin-panel textarea, .admin-panel button { font-size: 18px !important; }
    .admin-panel h2, .admin-panel h3 { font-size: 24px !important; }
    button.ufmg-btn:hover { background: #7a0010 !important; }

    button.error-btn {
        background: #ffffff !important; border: 1px solid #dcdcdc !important; color: #555555 !important;
        font-weight: 500 !important; font-size: 16px !important; padding: 11px !important; margin-top: 10px !important; width: 100% !important;
    }
    button.error-btn:hover { background: #f5f5f5 !important; color: #9c0014 !important; }

    .center-form { max-width: 450px; margin: 0 auto; }
    .empty-video { padding: 60px; text-align: center; color: #888; background: #ffffff; border: 1px dashed #ccc; border-radius: 8px; }

    .loading-note { font-size: 15px; color: #666; margin-bottom: 5px; font-weight: 500; text-align: left; }
    .sound-note { font-size: 15px; color: #9c0014; margin-bottom: 30px; font-weight: 700; text-align: left;}

    .wrap-radio form, .wrap-radio > div, .wrap-radio fieldset { display: flex !important; flex-direction: column !important; width: 100% !important; }
    .wrap-radio label {
        display: flex !important; align-items: center !important; gap: 10px !important; width: 100% !important;
        background: #ffffff !important; color: #000000 !important; border: 1px solid #cccccc !important;
        border-radius: 6px !important; padding: 12px 15px !important; margin-bottom: 10px !important;
        box-sizing: border-box !important; cursor: pointer !important; font-size: 16px !important;
    }
    .wrap-radio label:hover { background: #f5f5f5 !important; }
    .wrap-radio input[type="radio"] {
        -webkit-appearance: radio !important;
        appearance: radio !important;
        width: 18px !important; height: 18px !important; min-width: 18px !important;
        margin: 0 !important; flex-shrink: 0 !important;
        accent-color: #9c0014 !important;
        transform: none !important;
    }
    .wrap-radio label:has(input:checked) {
        background: #fff5f6 !important;
        border-color: #9c0014 !important;
        box-shadow: inset 0 0 0 1px rgba(156, 0, 20, 0.20) !important;
    }

    .warning-text { color: #9c0014; font-weight: bold; font-size: 14px; text-align: center; margin-top: 10px;}
    footer { display: none !important; }
"""

# Configuração nuclear do tema Gradio para limpar todos os resquícios de dark mode (textboxes inclusos)
tema_branco = gr.themes.Base().set(
    body_background_fill="#ffffff", body_background_fill_dark="#ffffff",
    background_fill_primary="#ffffff", background_fill_primary_dark="#ffffff",
    background_fill_secondary="#ffffff", background_fill_secondary_dark="#ffffff",
    block_background_fill="#ffffff", block_background_fill_dark="#ffffff",
    border_color_primary="#cccccc", border_color_primary_dark="#cccccc",
    body_text_color="#232323", body_text_color_dark="#232323",
    block_label_text_color="#232323", block_label_text_color_dark="#232323",
    block_title_text_color="#232323", block_title_text_color_dark="#232323",
    input_background_fill="#ffffff", input_background_fill_dark="#ffffff",
    input_background_fill_focus="#ffffff", input_background_fill_focus_dark="#ffffff",
    button_secondary_background_fill="#ffffff", button_secondary_background_fill_dark="#ffffff"
)

try:
    initial_config = load_config()
except:
    initial_config = {"videos_per_labeler": 10, "overlap_percent": 0.0, "tarefa": "Detecção de Posição"}
initial_videos = load_videos()
initialize_results_file()
ensure_results_schema()
initial_assignments = make_assignments(initial_config, initial_videos)
initial_per_labeler = math.ceil(len(initial_assignments) / len(initial_config.get("labelers", []))) if initial_config.get("labelers") else 0

# Ocultado temporariamente o uso do css dentro do Blocks para inseri-lo no launch, como pede a V6
with gr.Blocks() as demo:
    with gr.Column(elem_classes="shell"):
        gr.HTML(f"""
        <div class='brand-header'>
            <h1>TokStance!</h1>
            <p>Desenvolvido por Gustavo Cunha<br>
            Tarefa: <strong>{initial_config.get('tarefa', 'Detecção de Posição')}</strong></p>
        </div>
        """)

        # --- TELA DE LOGIN ---
        with gr.Column(visible=True) as login_panel:
            gr.HTML("<div style='text-align:center; margin-bottom: 20px;'><h3 style='margin:0; color:#333;'>Acesso ao Sistema</h3><p style='color:#666;'>Insira seu identificador de pesquisador para iniciar.</p></div>")
            with gr.Column(elem_classes="center-form"):
                username_input = gr.Textbox(label="Usuário", placeholder="Seu nome de usuário", autofocus=True)
                login_button = gr.Button("Entrar", elem_classes="ufmg-btn")
                login_message = gr.Markdown()

        # --- TELA DE ADMINISTRAÇÃO (Horizontal) ---
        with gr.Column(visible=False, elem_classes="admin-panel") as admin_panel:
            gr.Markdown("## Administração de Pesquisa")
            with gr.Row(elem_classes="admin-actions"):
                admin_save_exit_button = gr.Button("Salvar e sair", elem_classes="error-btn")

            with gr.Row(elem_classes="labeler-layout"):
                admin_users_input = gr.Textbox(label="Administradores (um por linha)", lines=2)
                labeler_users_input = gr.Textbox(label="Rotuladores (um por linha)", lines=2)
                tarefa_input = gr.Textbox(label="Tarefa da Anotação", value=initial_config["tarefa"])

            with gr.Row():
                videos_per_labeler_input = gr.Number(label="Vídeos por rotulador (calculado)", value=initial_per_labeler, precision=0, minimum=0, interactive=False)
                overlap_input = gr.Number(label="Redundância / Overlap (%)", value=initial_config["overlap_percent"], precision=1, minimum=0, maximum=100)
                save_settings_button = gr.Button("Salvar Configurações e Gerar Atribuições", elem_classes="ufmg-btn")

            admin_message = gr.Markdown()
            gr.Markdown("### Dados Coletados")
            agreement_panel = gr.HTML()
            with gr.Row():
                results_download = gr.File(label="Resultados Anotação (CSV)")
                log_download = gr.File(label="Metadados e Tempos (CSV)")
                errors_download = gr.File(label="Vídeos com Erro (CSV)")
                agreement_download = gr.File(label="Concordância (JSON)")

            gr.Markdown("### Auditoria de Atribuições")
            assignment_table = gr.Dataframe(headers=["assignment_version", "assignment_id", "labeler", "video_id", "url", "target", "video_description", "voice_to_text", "video_duration"], interactive=False, elem_classes="assignment-table", max_height=2000, wrap=True)

        # --- TELA DE ROTULAÇÃO (LAYOUT HORIZONTAL) ---
        with gr.Column(visible=False, elem_classes="labeler-panel") as labeler_panel:
            with gr.Row(elem_classes="labeler-actions"):
                save_exit_button = gr.Button("Salvar e sair", elem_classes="error-btn")

            with gr.Row(elem_classes="labeler-layout"):
                with gr.Column(scale=5, min_width=1000, elem_classes="video-column"):
                    video_html = gr.HTML()

                with gr.Column(scale=3, min_width=600, elem_classes="labeler-controls-column"):
                    labeler_name = gr.Markdown()
                    labeler_progress = gr.HTML()

                    with gr.Column(visible=False, elem_classes="annotation-controls") as annotation_controls:
                        gr.HTML("<div class='labeler-warning-stack'><div>⏳ O player do TikTok pode demorar alguns segundos para carregar.</div><div>🔊 Certifique-se de que som do vídeo está habilitado clicando no ícone de som do player do TikTok.</div></div>")

                        target_question = gr.HTML()
                        stance_radio = gr.Radio(choices=STANCE_OPTIONS, label="Selecione uma opção:", elem_classes="wrap-radio")

                        save_button = gr.Button("Salvar Resposta e Avançar", elem_classes="ufmg-btn")
                        error_button = gr.Button("⚠️ O vídeo não carregou", elem_classes="error-btn")

                        labeler_message = gr.Markdown(elem_classes="warning-text")

            queue_state = gr.State([])
            position_state = gr.State(0)
            login_time_state = gr.State(0.0)
            start_time_state = gr.State(0.0)

    # --- EVENTOS ---
    outputs_login = [
        login_panel, admin_panel, labeler_panel, login_message,
        labeler_name, queue_state, position_state, login_time_state, start_time_state,
        video_html, annotation_controls, target_question, labeler_progress,
        admin_users_input, labeler_users_input, tarefa_input, videos_per_labeler_input, overlap_input, admin_message, assignment_table, results_download, log_download, errors_download, agreement_panel, agreement_download
    ]
    login_button.click(authenticate, inputs=username_input, outputs=outputs_login)
    admin_save_exit_button.click(reset_to_login, outputs=outputs_login)
    save_exit_button.click(save_and_exit, inputs=labeler_name, outputs=outputs_login)

    save_settings_button.click(save_admin_settings, inputs=[admin_users_input, labeler_users_input, tarefa_input, videos_per_labeler_input, overlap_input], outputs=[admin_message, assignment_table, results_download, log_download, errors_download, videos_per_labeler_input])

    outputs_save = [position_state, start_time_state, video_html, annotation_controls, target_question, labeler_progress, labeler_message, stance_radio]

    # Evento de Salvar Normal
    save_button.click(save_annotation_normal, inputs=[labeler_name, queue_state, position_state, stance_radio, login_time_state, start_time_state], outputs=outputs_save)

    # Evento de Reportar Erro (Timer de 30s)
    error_button.click(save_annotation_error, inputs=[labeler_name, queue_state, position_state, login_time_state, start_time_state], outputs=outputs_save)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860, theme=tema_branco, css=APP_CSS)