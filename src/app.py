import json
import csv
import hashlib
import math
import os
import socket
import subprocess
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
PROJECT_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.getenv("TOKSTANCE_DATA_DIR") or PROJECT_DIR / "data")
VIDEOS_FILE = BASE_DIR / "videos.csv"
RESERVE_VIDEOS_FILE = BASE_DIR / "videos_reserva.csv"
RESULTS_FILE = BASE_DIR / "resultados_anotacao.csv"
ASSIGNMENTS_FILE = BASE_DIR / "atribuicoes.csv"
CONFIG_FILE = BASE_DIR / "configuracao.json"
LOG_FILE = BASE_DIR / "acessos_log.csv"
ERRORS_FILE = BASE_DIR / "erros_videos.csv"
AGREEMENT_FILE = BASE_DIR / "concordancia.json"

FILE_LOCK = threading.Lock()
ACTIVE_USERS = set()
ACTIVE_USERS_LOCK = threading.Lock()
STANCE_OPTIONS = [
    "Contra",
    "A Favor",
    "Neutro",
    "Vídeo não relacionado ao target"
]
BRT_TZ = timezone(timedelta(hours=-3))

# ==============================================================================
# FUNÇÕES DE RETAGUARDA (DADOS E LÓGICA)
# ==============================================================================
def clean_users(value):
    users = (value or "").replace(",", "\n").splitlines()
    return list(dict.fromkeys(user.strip() for user in users if user.strip()))

def load_videos():
    if not VIDEOS_FILE.exists():
        raise FileNotFoundError(f"\n[ERRO CRÍTICO] Arquivo '{VIDEOS_FILE.name}' ausente. A execução foi abortada.")
    videos = pd.read_csv(VIDEOS_FILE, dtype={
        "id": str,
        "url": str,
        "target": str,
        "video_description": str,
        "voice_to_text": str,
        "video_duration": str,
    })
    if videos.empty:
        raise ValueError("videos.csv precisa conter ao menos um registro de vídeo.")
    missing = {"id", "url", "target"}.difference(videos.columns)
    if missing:
        raise ValueError(f"videos.csv não possui as colunas obrigatórias: {', '.join(sorted(missing))}")
    return videos.fillna("")

def load_reserve_videos():
    if not RESERVE_VIDEOS_FILE.exists():
        raise FileNotFoundError(f"\n[ERRO CRÍTICO] Arquivo '{RESERVE_VIDEOS_FILE.name}' ausente. A execução foi abortada.")
    if RESERVE_VIDEOS_FILE.stat().st_size == 0:
        raise ValueError("videos_reserva.csv precisa conter ao menos um registro de vídeo.")
    reserve_videos = pd.read_csv(RESERVE_VIDEOS_FILE, dtype=str).fillna("")
    if reserve_videos.empty:
        raise ValueError("videos_reserva.csv precisa conter ao menos um registro de vídeo.")
    missing = {"id", "url", "target"}.difference(reserve_videos.columns)
    if missing:
        raise ValueError(f"videos_reserva.csv não possui as colunas obrigatórias: {', '.join(sorted(missing))}")
    for column in ("video_description", "voice_to_text", "video_duration"):
        if column not in reserve_videos.columns:
            reserve_videos[column] = ""
    return reserve_videos


def validate_required_input_files():
    load_videos()
    load_reserve_videos()


validate_required_input_files()

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


def claim_active_user(username):
    """Atomically reserves a username for one active browser session."""
    with ACTIVE_USERS_LOCK:
        if username in ACTIVE_USERS:
            return False
        ACTIVE_USERS.add(username)
        return True


def release_active_user(username):
    with ACTIVE_USERS_LOCK:
        ACTIVE_USERS.discard(username)


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
    columns = [
        "timestamp", "video_id", "url", "labeler", "assignment_id",
        "replacement_video_id", "replacement_url", "replacement_assignment_id", "replacement_status",
    ]
    if not ERRORS_FILE.exists() or ERRORS_FILE.stat().st_size == 0:
        return pd.DataFrame(columns=columns)
    with ERRORS_FILE.open(encoding="utf-8", newline="") as error_file:
        rows = list(csv.reader(error_file))
    if not rows:
        return pd.DataFrame(columns=columns)
    if rows[0][:4] == columns[:4]:
        columns_in_file = rows.pop(0)
    else:
        columns_in_file = columns[:5]
    normalized = []
    for row in rows:
        if len(row) < 4:
            continue
        timestamp, video_id, url, labeler = row[:4]
        assignment_id = row[4].strip() if len(row) >= 5 and row[4].strip() else f"legacy:{labeler}:{video_id}"
        values = dict(zip(columns_in_file, row))
        values["assignment_id"] = assignment_id
        normalized.append({column: values.get(column, "") for column in columns})
    return pd.DataFrame(normalized, columns=columns)


def persist_frame_if_changed(frame, file_path, columns):
    content = frame.reindex(columns=columns).to_csv(index=False)
    if file_path.exists() and file_path.read_text(encoding="utf-8") == content:
        return
    file_path.write_text(content, encoding="utf-8")


def ensure_errors_schema():
    if not ERRORS_FILE.exists() or ERRORS_FILE.stat().st_size == 0:
        return
    errors = read_error_assignments()
    persist_frame_if_changed(errors, ERRORS_FILE, list(errors.columns))


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


def render_video_card(item, highlight_target=False):
    description = str(item.get("video_description", "")).strip()
    transcription = str(item.get("voice_to_text", "")).strip()
    description_html = escape(description).replace("\n", "<br>") if description else "Descrição não disponível em videos.csv."
    transcription_html = escape(transcription).replace("\n", "<br>") if transcription else "Transcrição indisponível para este vídeo."
    description_class = "video-description" if description else "video-description unavailable"
    transcription_class = "video-transcription" if transcription else "video-transcription unavailable"
    return f"""
    <div class='video-card'>
        <div class='video-meta{' target-transition' if highlight_target else ''}'><strong>Target:</strong> {escape(str(item.get('target', '')))}</div>
        <div class='video-content-grid'>
            <div class='video-context-column'>
                <div class='{description_class}'><strong>Descrição do vídeo</strong><div>{description_html}</div></div>
                <div class='{transcription_class}'><strong>Transcrição do áudio <span class='transcription-note'>(pode conter erros)</span></strong><div>{transcription_html}</div></div>
            </div>
            <div class='video-player-column'>{render_tiktok(item['url'])}</div>
        </div>
    </div>
    """

def make_assignments(config, videos, persist=True):
    labelers = config.get("labelers", [])
    overlap_percent = max(0.0, min(100.0, float(config.get("overlap_percent", 0))))
    version = assignment_version(config, videos)
    assignment_columns = ["assignment_version", "assignment_id", "labeler", "video_id", "url", "target", "video_description", "voice_to_text", "video_duration"]

    if not labelers or len(videos) == 0:
        return pd.DataFrame(columns=assignment_columns)

    target_buckets = {}
    target_order = []
    for video_index, target in enumerate(videos["target"].astype(str).tolist()):
        target_key = target.strip()
        if target_key not in target_buckets:
            target_buckets[target_key] = []
            target_order.append(target_key)
        target_buckets[target_key].append(video_index)

    balanced_video_order = []
    while any(target_buckets[target_key] for target_key in target_order):
        for target_key in target_order:
            if target_buckets[target_key]:
                balanced_video_order.append(target_buckets[target_key].pop(0))

    overlap_count = int((overlap_percent / 100.0) * len(videos))
    overlap_indices = balanced_video_order[:overlap_count]
    individual_indices = balanced_video_order[overlap_count:]
    labeler_target_counts = {labeler: {target_key: 0 for target_key in target_order} for labeler in labelers}
    labeler_totals = {labeler: 0 for labeler in labelers}
    individual_assignments = {}
    for video_index in individual_indices:
        target_key = str(videos.iloc[video_index]["target"]).strip()
        labeler = min(labelers, key=lambda candidate: (
            labeler_target_counts[candidate][target_key],
            labeler_totals[candidate],
            labelers.index(candidate),
        ))
        individual_assignments[video_index] = labeler
        labeler_target_counts[labeler][target_key] += 1
        labeler_totals[labeler] += 1

    assignments = []
    for video_index in overlap_indices:
        for labeler in labelers:
            assignments.append((labeler, video_index))
    assignments.extend((labeler, video_index) for video_index, labeler in individual_assignments.items())

    target_rank = {target_key: rank for rank, target_key in enumerate(target_order)}
    assignments.sort(key=lambda item: (labelers.index(item[0]), target_rank[str(videos.iloc[item[1]]["target"]).strip()], item[1]))
    result = pd.DataFrame([
        {
            "assignment_version": version,
            "assignment_id": assignment_identifier(version, labeler, videos.iloc[video_index]["id"]),
            "labeler": labeler,
            "video_id": str(videos.iloc[video_index]["id"]),
            "url": videos.iloc[video_index]["url"],
            "target": videos.iloc[video_index]["target"],
            "video_description": videos.iloc[video_index].get("video_description", ""),
            "voice_to_text": videos.iloc[video_index].get("voice_to_text", ""),
            "video_duration": videos.iloc[video_index].get("video_duration", ""),
        }
        for labeler, video_index in assignments
    ], columns=assignment_columns)
    result = result.drop_duplicates(subset=["labeler", "video_id"], keep="last").reset_index(drop=True)
    if persist:
        persist_frame_if_changed(result, ASSIGNMENTS_FILE, assignment_columns)
    return result

def apply_reserve_replacements(assignments):
    if assignments.empty or not ERRORS_FILE.exists():
        return assignments

    errors = read_error_assignments()
    reserve_videos = load_reserve_videos().drop_duplicates(subset=["id"], keep="first")
    if errors.empty:
        return assignments
    if reserve_videos.empty:
        pending_errors = errors["replacement_assignment_id"].astype(str).str.strip().eq("")
        errors.loc[pending_errors, "replacement_status"] = "SEM_RESERVA_DISPONIVEL"
        persist_frame_if_changed(errors, ERRORS_FILE, list(errors.columns))
        return assignments

    reserve_ids = set(reserve_videos["id"].astype(str))
    used_reserve_ids = set(assignments[assignments["video_id"].astype(str).isin(reserve_ids)]["video_id"].astype(str))
    used_reserve_ids.update(errors[errors["video_id"].astype(str).isin(reserve_ids)]["video_id"].astype(str))
    available_reserves = reserve_videos[~reserve_videos["id"].astype(str).isin(used_reserve_ids)]
    results = read_results()
    completed_assignment_ids = set(results.get("assignment_id", pd.Series(dtype=str)).astype(str).str.strip())
    assignments_changed = False
    errors_changed = False

    for error_index, error in errors.iterrows():
        if str(error.get("replacement_assignment_id", "")).strip():
            continue
        assignment_id = str(error.get("assignment_id", "")).strip()
        original_video_id = str(error.get("video_id", "")).strip()
        prior_replacement = errors[
            errors["video_id"].astype(str).eq(original_video_id)
            & errors["replacement_assignment_id"].astype(str).ne("")
        ]
        existing_reserve_id = ""
        prior_status = ""
        if not prior_replacement.empty:
            original_video_id = str(prior_replacement.iloc[0]["video_id"]).strip()
            existing_reserve_id = str(prior_replacement.iloc[0]["replacement_video_id"]).strip()
            prior_status = str(prior_replacement.iloc[0]["replacement_status"]).strip()
        matching_rows = assignments.index[assignments["assignment_id"].astype(str).eq(assignment_id)]
        if matching_rows.empty:
            matching_rows = assignments.index[
                assignments["labeler"].astype(str).eq(str(error.get("labeler", "")))
                & assignments["video_id"].astype(str).eq(str(error.get("video_id", "")))
            ]
        if matching_rows.empty and existing_reserve_id:
            matching_rows = assignments.index[
                assignments["labeler"].astype(str).eq(str(error.get("labeler", "")))
                & assignments["video_id"].astype(str).eq(existing_reserve_id)
            ]
        if matching_rows.empty:
            continue

        source_index = matching_rows[0]
        if prior_status == "SUBSTITUIDO_OVERLAP_REBALANCEADO":
            errors.at[error_index, "replacement_video_id"] = existing_reserve_id
            errors.at[error_index, "replacement_url"] = str(assignments.at[source_index, "url"])
            errors.at[error_index, "replacement_assignment_id"] = str(assignments.at[source_index, "assignment_id"])
            errors.at[error_index, "replacement_status"] = prior_status
            errors_changed = True
            continue

        group_rows = assignments.index[assignments["video_id"].astype(str).eq(original_video_id)]
        replacement_rows = errors[
            errors["video_id"].astype(str).eq(original_video_id)
            & errors["replacement_assignment_id"].astype(str).ne("")
        ]["replacement_assignment_id"].astype(str).tolist()
        if replacement_rows:
            group_rows = group_rows.union(
                assignments.index[assignments["assignment_id"].astype(str).isin(replacement_rows)]
            )
        if existing_reserve_id:
            group_rows = group_rows.union(
                assignments.index[assignments["video_id"].astype(str).eq(existing_reserve_id)]
            )
        group_rows = group_rows.sort_values()
        group_has_completed = any(
            str(assignments.at[index, "assignment_id"]).strip() in completed_assignment_ids
            for index in group_rows
        )
        source_target = str(assignments.at[source_index, "target"]).strip()
        if group_rows.empty:
            group_rows = matching_rows
        compatible_reserves = reserve_videos[
            reserve_videos["target"].astype(str).str.strip().eq(source_target)
        ]
        if existing_reserve_id:
            compatible_reserves = compatible_reserves[
                compatible_reserves["id"].astype(str).eq(existing_reserve_id)
            ]
        else:
            compatible_reserves = compatible_reserves[
                compatible_reserves["id"].astype(str).isin(available_reserves["id"].astype(str))
            ]
        if group_has_completed and not existing_reserve_id:
            video_counts = assignments.groupby(assignments["video_id"].astype(str))["assignment_id"].transform("size")
            candidate_rows = assignments.index[
                video_counts.eq(1)
                & assignments["video_id"].astype(str).ne(original_video_id)
                & assignments["video_id"].astype(str).isin(
                    assignments.loc[assignments["assignment_id"].astype(str).isin(completed_assignment_ids), "video_id"].astype(str)
                )
                & assignments["target"].astype(str).str.strip().eq(source_target)
                & assignments["labeler"].astype(str).ne(str(error.get("labeler", "")))
            ]
            if not candidate_rows.empty:
                candidate_index = candidate_rows[0]
                candidate_video_id = str(assignments.at[candidate_index, "video_id"])
                candidate = assignments.loc[candidate_index].copy()
                source_labeler = str(assignments.at[source_index, "labeler"])
                source_version = str(assignments.at[source_index, "assignment_version"])
                source_replacement_id = assignment_identifier(source_version, source_labeler, candidate_video_id)
                rows_to_promote = [
                    index for index in group_rows
                    if str(assignments.at[index, "assignment_id"]).strip() not in completed_assignment_ids
                ]
                if source_index not in rows_to_promote:
                    rows_to_promote.append(source_index)
                for promotion_index in rows_to_promote:
                    promotion_labeler = str(assignments.at[promotion_index, "labeler"])
                    promotion_version = str(assignments.at[promotion_index, "assignment_version"])
                    assignments.at[promotion_index, "video_id"] = candidate_video_id
                    for column in ("url", "target", "video_description", "voice_to_text", "video_duration"):
                        assignments.at[promotion_index, column] = candidate[column]
                    assignments.at[promotion_index, "assignment_id"] = assignment_identifier(
                        promotion_version, promotion_labeler, candidate_video_id
                    )
                errors.at[error_index, "replacement_video_id"] = candidate_video_id
                errors.at[error_index, "replacement_url"] = str(candidate["url"])
                errors.at[error_index, "replacement_assignment_id"] = source_replacement_id
                errors.at[error_index, "replacement_status"] = "SUBSTITUIDO_OVERLAP_REBALANCEADO"
                assignments_changed = True
                errors_changed = True
                continue
        if compatible_reserves.empty:
            if group_has_completed:
                errors.at[error_index, "replacement_status"] = "CRITICO_OVERLAP_INCONSISTENTE"
            else:
                errors.at[error_index, "replacement_status"] = (
                    "SEM_RESERVA_DISPONIVEL" if available_reserves.empty else "SEM_RESERVA_COMPATIVEL"
                )
            errors_changed = True
            continue

        reserve = compatible_reserves.iloc[0]
        reserve_id = str(reserve["id"])
        rows_to_replace = matching_rows if group_has_completed else group_rows
        replacement_assignment_id = ""
        for replacement_index in rows_to_replace:
            labeler = str(assignments.at[replacement_index, "labeler"])
            version = str(assignments.at[replacement_index, "assignment_version"])
            replacement_assignment_id = assignment_identifier(version, labeler, reserve_id)
            if str(assignments.at[replacement_index, "assignment_id"]).strip() in completed_assignment_ids:
                continue
            assignments.at[replacement_index, "video_id"] = reserve_id
            for column in ("url", "target", "video_description", "voice_to_text", "video_duration"):
                assignments.at[replacement_index, column] = reserve.get(column, "")
            assignments.at[replacement_index, "assignment_id"] = replacement_assignment_id
            assignments_changed = True
        labeler = str(assignments.at[source_index, "labeler"])
        version = str(assignments.at[source_index, "assignment_version"])
        replacement_assignment_id = assignment_identifier(version, labeler, reserve_id)
        errors.at[error_index, "replacement_video_id"] = reserve_id
        errors.at[error_index, "replacement_url"] = str(reserve["url"])
        errors.at[error_index, "replacement_assignment_id"] = replacement_assignment_id
        errors.at[error_index, "replacement_status"] = (
            "CRITICO_OVERLAP_INCONSISTENTE" if group_has_completed else "SUBSTITUIDO_OVERLAP"
        )
        if not group_has_completed and not existing_reserve_id:
            available_reserves = available_reserves[available_reserves["id"].astype(str).ne(reserve_id)]
        errors_changed = True

    if assignments_changed:
        persist_frame_if_changed(assignments, ASSIGNMENTS_FILE, list(assignments.columns))
    if errors_changed:
        persist_frame_if_changed(errors, ERRORS_FILE, list(errors.columns))
    return assignments

def load_assignments(config, videos):
    base_assignments = make_assignments(config, videos, persist=False)
    assignments = base_assignments
    if ASSIGNMENTS_FILE.exists() and not base_assignments.empty:
        try:
            persisted = pd.read_csv(ASSIGNMENTS_FILE, dtype=str).fillna("")
            has_schema = set(base_assignments.columns).issubset(persisted.columns)
            same_version = set(persisted.get("assignment_version", [])) == {base_assignments.iloc[0]["assignment_version"]}
            if has_schema and same_version and len(persisted) == len(base_assignments):
                assignments = persisted[base_assignments.columns].copy()
        except (pd.errors.EmptyDataError, pd.errors.ParserError):
            pass
    if not assignments.empty and not assignments.equals(base_assignments):
        persist_frame_if_changed(assignments, ASSIGNMENTS_FILE, list(base_assignments.columns))
    elif not ASSIGNMENTS_FILE.exists():
        persist_frame_if_changed(base_assignments, ASSIGNMENTS_FILE, list(base_assignments.columns))
    return apply_reserve_replacements(assignments)


LABEL_DISTANCE = {
    frozenset(("Contra", "Neutro")): 1.0,
    frozenset(("Neutro", "A Favor")): 1.0,
    frozenset(("Contra", "A Favor")): 4.0,
    frozenset(("Vídeo não relacionado ao target", "Contra")): 3.0,
    frozenset(("Vídeo não relacionado ao target", "Neutro")): 3.0,
    frozenset(("Vídeo não relacionado ao target", "A Favor")): 3.0,
}


def label_distance(left, right):
    if left == right:
        return 0.0
    return LABEL_DISTANCE.get(frozenset((left, right)), 0.0)


def assignment_versions(assignments, results, errors):
    versions = []
    current_version = None
    if not assignments.empty and "assignment_version" in assignments:
        versions.extend(assignments["assignment_version"].dropna().astype(str).tolist())
        current_version = versions[0] if versions else None
    for frame in (results, errors):
        if not frame.empty and "assignment_id" in frame:
            versions.extend(frame["assignment_id"].astype(str).str.split(":", n=1).str[0].tolist())
    available = {version for version in versions if version and version != "legacy"}
    available.discard(current_version)
    return ([current_version] if current_version else []) + sorted(available, reverse=True)


def filter_by_assignment_version(frame, version):
    if frame.empty or not version or "assignment_id" not in frame:
        return frame.copy()
    return frame[frame["assignment_id"].astype(str).str.startswith(f"{version}:")].copy()


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
                distances.append(label_distance(labels[left_index], labels[right_index]))
        observed_disagreement += sum(distances)
        pair_count += len(distances)
        if any(distance > 0 for distance in distances):
            disagreement_videos.append({"video_id": str(video_id), "anotacoes": labels, "discordancia_ordinal": round(sum(distances) / len(distances), 4)})

    all_labels = [label for labels in usable for label in labels]
    expected_disagreement = sum(
        label_distance(all_labels[left_index], all_labels[right_index])
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
            if other_labels:
                score = sum(label_distance(row.stance, label) for label in other_labels) / len(other_labels)
                labeler_disagreements[row.labeler] = labeler_disagreements.get(row.labeler, 0.0) + score
    labelers = [{"labeler": labeler, "penalidade": round(score, 2)} for labeler, score in labeler_disagreements.items()]
    labelers.sort(key=lambda item: item["penalidade"], reverse=True)
    return {"metrica": "Concordância ponderada por distância", "alfa": round(alpha, 6) if alpha is not None else None, "videos_com_multiplas_anotacoes": int(len(usable)), "anotacoes_consideradas": int(len(all_labels)), "videos_com_discordancia": disagreement_videos, "labelers_com_maior_discordancia": labelers}


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


def load_access_log():
    columns = ["timestamp", "usuario", "acao", "tempo_desde_login_segundos"]
    if not LOG_FILE.exists() or LOG_FILE.stat().st_size == 0:
        return pd.DataFrame(columns=columns)
    try:
        log = pd.read_csv(LOG_FILE, dtype=str).fillna("")
        if not set(columns).issubset(log.columns):
            return pd.DataFrame(columns=columns)
        log["timestamp"] = pd.to_datetime(log["timestamp"], errors="coerce")
        log["tempo_desde_login_segundos"] = pd.to_numeric(log["tempo_desde_login_segundos"], errors="coerce").fillna(0.0)
        return log.dropna(subset=["timestamp"])
    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame(columns=columns)


def access_statistics():
    log = load_access_log()
    if log.empty:
        return {"session_rows": "<p class='muted'>Ainda não há dados de acesso.</p>", "daily_rows": "<p class='muted'>Ainda não há dados de acesso.</p>", "avg_minutes": 0.0}
    login_events = log[log["acao"].eq("LOGIN_LABELER") | log["acao"].eq("LOGIN_ADMIN")].copy()
    daily = login_events.assign(dia=login_events["timestamp"].dt.strftime("%Y-%m-%d")).groupby("dia").size().to_dict()
    daily_max = max(daily.values(), default=1)
    daily_rows = "".join(
        f"<div class='bar-row'><span>{escape(day)}</span><div class='bar-track'><div class='bar-fill' style='width:{count / daily_max:.0%}'></div></div><strong>{count}</strong></div>"
        for day, count in sorted(daily.items())
    ) or "<p class='muted'>Nenhum login registrado.</p>"
    sessions = []
    for index, login in login_events.iterrows():
        next_login = login_events[login_events.index > index]
        end = next_login.iloc[0]["timestamp"] if not next_login.empty else None
        events = log[(log.index >= index) & (log["usuario"] == login["usuario"])]
        events = events[events["timestamp"] >= login["timestamp"]]
        if end is not None:
            events = events[events["timestamp"] < end]
        duration = float(events["tempo_desde_login_segundos"].max()) if not events.empty else 0.0
        sessions.append({"usuario": login["usuario"], "timestamp": login["timestamp"], "duracao": duration})
    session_frame = pd.DataFrame(sessions)
    avg_minutes = session_frame["duracao"].mean() / 60 if not session_frame.empty else 0.0
    user_rows = "".join(
        f"<tr><td>{escape(str(user))}</td><td>{len(rows)}</td><td>{rows['duracao'].mean() / 60:.1f} min</td><td>{rows['timestamp'].dt.strftime('%Y-%m-%d').nunique()}</td></tr>"
        for user, rows in session_frame.groupby("usuario")
    )
    session_rows = f"<table class='progress-table'><thead><tr><th>Usuário</th><th>Logins</th><th>Média logado</th><th>Dias ativos</th></tr></thead><tbody>{user_rows}</tbody></table>"
    return {"session_rows": session_rows, "daily_rows": daily_rows, "avg_minutes": avg_minutes}


def assignment_table_for_version(assignments, results, errors, version):
    if version and not assignments.empty and version in set(assignments["assignment_version"].astype(str)):
        return assignments[assignments["assignment_version"].astype(str).eq(version)]
    rows = pd.concat([filter_by_assignment_version(results, version), filter_by_assignment_version(errors, version)], ignore_index=True)
    return rows


def annotation_daily_chart(results):
    if results.empty or not {"timestamp", "labeler"}.issubset(results.columns):
        return "<p class='muted'>Ainda não há anotações para desenhar a série diária.</p>"
    daily = results.copy()
    daily["timestamp"] = pd.to_datetime(daily["timestamp"], errors="coerce")
    daily = daily.dropna(subset=["timestamp"])
    if daily.empty:
        return "<p class='muted'>Ainda não há anotações para desenhar a série diária.</p>"
    daily["dia"] = daily["timestamp"].dt.strftime("%Y-%m-%d")
    counts = daily.groupby(["dia", "labeler"]).size().reset_index(name="quantidade")
    labelers = sorted(counts["labeler"].astype(str).unique())
    colors = ["#9c0014", "#176b87", "#b56b00", "#4f6f52", "#704c8a", "#8b5e3c"]
    color_map = {labeler: colors[index % len(colors)] for index, labeler in enumerate(labelers)}
    max_count = max(int(counts["quantidade"].max()), 1)
    legend = "".join(
        f"<span class='chart-legend-item'><i style='background:{color_map[labeler]}'></i>{escape(labeler)}</span>"
        for labeler in labelers
    )
    days = []
    for day, day_rows in counts.groupby("dia", sort=True):
        values = dict(zip(day_rows["labeler"].astype(str), day_rows["quantidade"]))
        bars = "".join(
            f"<div class='daily-bar-row'><span>{escape(labeler)}</span><div class='daily-bar-track'><div class='daily-bar-fill' style='width:{values.get(labeler, 0) / max_count:.0%}; background:{color_map[labeler]}'></div></div><strong>{int(values.get(labeler, 0))}</strong></div>"
            for labeler in labelers
        )
        days.append(f"<div class='daily-annotation-day'><div class='daily-day-label'>{escape(day)}<span>{int(day_rows['quantidade'].sum())} no total</span></div>{bars}</div>")
    return f"<div class='chart-legend'>{legend}</div>{''.join(days)}"


def admin_insights_html(assignments, results, errors, videos, agreement, fast_annotations):
    completed_ids = set(results.get("assignment_id", pd.Series(dtype=str)).astype(str).str.strip())
    completed_ids.update(errors.get("assignment_id", pd.Series(dtype=str)).astype(str).str.strip())
    assigned = len(assignments)
    completed = sum(assignments["assignment_id"].astype(str).isin(completed_ids)) if assigned else 0
    pending = assigned - completed
    completion_percent = completed / assigned if assigned else 0
    reserve_videos = load_reserve_videos()
    used_reserve_ids = set(errors.get("replacement_video_id", pd.Series(dtype=str)).astype(str).str.strip()) - {""}
    remaining_reserves = max(len(reserve_videos) - len(used_reserve_ids), 0)
    analysis_times = pd.to_numeric(results.get("tempo_analise_segundos", pd.Series(dtype=str)), errors="coerce").dropna()
    median_analysis = analysis_times.median() if not analysis_times.empty else None
    error_count = len(errors)
    replacement_count = len(used_reserve_ids)
    critical_overlap_count = int(errors.get("replacement_status", pd.Series(dtype=str)).astype(str).str.startswith("CRITICO_").sum())

    kpis = [
        ("Cobertura da rodada", f"{completion_percent:.0%}", f"{completed} concluídos de {assigned}"),
        ("Pendências", str(pending), "atribuições ainda abertas"),
        ("Erros reportados", str(error_count), f"{replacement_count} substituídos por reserva"),
        ("Reservas disponíveis", str(remaining_reserves), f"{len(reserve_videos)} no pool total"),
        ("Tempo mediano", f"{median_analysis:.1f}s" if median_analysis is not None else "n/d", "por anotação registrada"),
        ("Discordâncias", str(len(agreement["videos_com_discordancia"])), "vídeos com posições diferentes"),
    ]
    kpi_html = "".join(
        f"<div class='admin-kpi'><span>{escape(label)}</span><strong>{escape(value)}</strong><small>{escape(note)}</small></div>"
        for label, value, note in kpis
    )

    labeler_rows = []
    if not assignments.empty:
        for labeler, labeler_assignments in assignments.groupby("labeler", sort=False):
            labeler_assigned = len(labeler_assignments)
            labeler_completed = int(labeler_assignments["assignment_id"].astype(str).isin(completed_ids).sum())
            labeler_rows.append((str(labeler), labeler_completed / labeler_assigned if labeler_assigned else 0, labeler_completed, labeler_assigned))
    labeler_rows.sort(key=lambda item: (item[1], item[0]))
    labeler_chart = "".join(
        f"<div class='insight-bar-row'><span>{escape(labeler)}</span><div class='insight-bar-track'><div class='insight-bar-fill' style='width:{percent:.0%}'></div></div><strong>{done}/{total}</strong></div>"
        for labeler, percent, done, total in labeler_rows
    ) or "<p class='muted'>Ainda não há atribuições para comparar.</p>"

    stance_counts = results[results["stance"].isin(STANCE_OPTIONS)]["stance"].value_counts() if "stance" in results else pd.Series(dtype=int)
    stance_max = max(int(stance_counts.max()), 1) if not stance_counts.empty else 1
    stance_colors = {"Contra": "#9c0014", "A Favor": "#176b87", "Neutro": "#b56b00", "Vídeo não relacionado ao target": "#626b73"}
    stance_chart = "".join(
        f"<div class='insight-bar-row'><span>{escape(label)}</span><div class='insight-bar-track'><div class='insight-bar-fill' style='width:{int(stance_counts.get(label, 0)) / stance_max:.0%}; background:{stance_colors[label]}'></div></div><strong>{int(stance_counts.get(label, 0))}</strong></div>"
        for label in STANCE_OPTIONS
    )

    signals = []
    if critical_overlap_count:
        signals.append(("Alerta crítico de overlap", f"{critical_overlap_count} atribuição(ões) não puderam preservar o overlap porque já havia anotação no vídeo original. Revise a rodada antes de continuar.", "critical"))
    if remaining_reserves <= max(1, len(reserve_videos) // 5):
        signals.append(("Atenção", "O estoque de reservas está baixo. Reponha o pool antes que novas falhas parem a rodada.", "warning"))
    if fast_annotations:
        signals.append(("Revisar qualidade", f"{len(fast_annotations)} análise(s) ficaram mais de 5 segundos abaixo da duração do vídeo.", "warning"))
    if agreement["videos_com_discordancia"]:
        signals.append(("Revisar concordância", f"{len(agreement['videos_com_discordancia'])} vídeo(s) têm discordância entre anotadores; priorize os maiores escores.", "info"))
    if pending and assigned and completion_percent < 0.25:
        signals.append(("Ritmo da rodada", "Menos de 25% da rodada foi concluída. Confirme se todos os anotadores conseguiram iniciar a sessão.", "info"))
    if not signals:
        signals.append(("Operação estável", "Nenhum sinal preventivo relevante foi detectado nesta rodada.", "ok"))
    signals_html = "".join(
        f"<div class='insight-signal {kind}'><strong>{escape(title)}</strong><span>{escape(message)}</span></div>"
        for title, message, kind in signals
    )
    return f"""
    <section class='admin-command-center'>
        <div class='admin-section-heading'><div><span class='section-eyebrow'>Painel de controle</span><h3>Leitura operacional da rodada</h3></div><span class='admin-live-dot'>dados locais</span></div>
        <div class='admin-kpi-grid'>{kpi_html}</div>
        <div class='admin-insight-grid'>
            <section class='admin-chart'><h4>Conclusão por rotulador</h4><p class='metric-note'>Carga concluída comparada ao total atribuído.</p>{labeler_chart}</section>
            <section class='admin-chart'><h4>Distribuição das posições</h4><p class='metric-note'>Ajuda a detectar concentração ou classes sem cobertura.</p>{stance_chart}</section>
            <section class='admin-signals'><h4>Sinais para ação</h4>{signals_html}</section>
        </div>
    </section>
    """


def agreement_summary(results, selected_version=None):
    config = load_config()
    videos = load_videos()
    assignments = load_assignments(config, videos)
    errors = read_error_assignments()
    versions = assignment_versions(assignments, results, errors)
    version = selected_version if selected_version in versions else (versions[0] if versions else None)
    version_results = filter_by_assignment_version(results, version)
    version_errors = filter_by_assignment_version(errors, version)
    agreement = calculate_agreement(version_results)
    with AGREEMENT_FILE.open("w", encoding="utf-8") as agreement_file:
        json.dump(agreement, agreement_file, ensure_ascii=False, indent=2)
    selected_assignments = assignments[assignments["assignment_version"].astype(str).eq(version)] if version and not assignments.empty else pd.DataFrame()
    completed_ids = set(version_results.get("assignment_id", pd.Series(dtype=str)).astype(str).str.strip())
    completed_ids.update(version_errors.get("assignment_id", pd.Series(dtype=str)).astype(str).str.strip())
    assignment_progress = ""
    if assignments.empty:
        assignment_progress = "<p class='muted'>Nenhuma atribuição ativa nesta rodada.</p>"
    else:
        progress_rows = []
        progress_source = selected_assignments if not selected_assignments.empty else assignment_table_for_version(assignments, version_results, version_errors, version)
        for labeler, labeler_assignments in progress_source.groupby("labeler", sort=False):
            assigned = len(labeler_assignments)
            completed = int(labeler_assignments["assignment_id"].isin(completed_ids).sum())
            pending = assigned - completed
            progress_rows.append(
                f"<tr><td><strong>{escape(str(labeler))}</strong></td><td>{assigned}</td><td>{completed}</td><td>{pending}</td><td><strong>{completed / assigned:.0%}</strong></td></tr>"
            )
        assignment_progress = f"""
        <table class='progress-table'>
            <thead><tr><th>Rotulador</th><th>Atribuídos</th><th>Concluídos</th><th>Pendentes</th><th>Progresso</th></tr></thead>
            <tbody>{''.join(progress_rows)}</tbody>
        </table>
        """
    assignment_version = version or "nenhuma"
    access = access_statistics()
    daily_annotation_chart = annotation_daily_chart(version_results)
    alpha = "ainda não calculável" if agreement["alfa"] is None else f"{agreement['alfa']:.3f}"
    labeler_rows = "".join(
        f"<tr><td>{escape(str(item['labeler']))}</td><td>{item['penalidade']:.2f}</td></tr>"
        for item in agreement["labelers_com_maior_discordancia"][:10]
    ) or "<tr><td colspan='2' class='muted'>Ainda não há discordâncias mensuráveis.</td></tr>"
    video_rows = "".join(
        f"<tr><td>{item['video_id']}</td><td>{' · '.join(item['anotacoes'])}</td><td>{item['discordancia_ordinal']:.2f}</td></tr>"
        for item in agreement["videos_com_discordancia"][:10]
    ) or "<tr><td colspan='3' class='muted'>Ainda não há vídeos com múltiplas anotações discordantes.</td></tr>"
    fast_annotations = find_fast_annotations(version_results, videos)
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
    insights_assignments = selected_assignments if not selected_assignments.empty else assignment_table_for_version(assignments, version_results, version_errors, version)
    admin_insights = admin_insights_html(insights_assignments, version_results, version_errors, videos, agreement, fast_annotations)
    return f"""
    <section class='agreement-dashboard'>
        {admin_insights}
        <section class='assignment-progress'>
            <div class='section-heading'><div><span class='section-eyebrow'>Rodada ativa</span><h3>Progresso dos rotuladores</h3></div><span class='version-label'>{escape(assignment_version)}</span></div>
            {assignment_progress}
        </section>
        <p class='metric-note'><strong>Como calculamos:</strong> pares iguais = 0; Contra–Neutro e Neutro–A Favor = 1; Contra–A Favor = 4; “não relacionado” contra uma posição = 3. A penalidade do anotador é a média da distância entre sua resposta e as demais no mesmo vídeo.</p>
        {fast_warning}
        <div class='agreement-kpis'>
            <div class='kpi'><span>Concordância ponderada</span><strong>{alpha}</strong></div>
            <div class='kpi'><span>Vídeos comparáveis</span><strong>{agreement['videos_com_multiplas_anotacoes']}</strong></div>
            <div class='kpi'><span>Anotações consideradas</span><strong>{agreement['anotacoes_consideradas']}</strong></div>
        </div>
        <div class='dashboard-grid'>
            <section class='dashboard-card'>
                <h3>Anotadores com maior penalidade</h3>
                <table><thead><tr><th>Anotador</th><th>Penalidade acumulada</th></tr></thead><tbody>{labeler_rows}</tbody></table>
            </section>
            <section class='dashboard-card'>
                <h3>Vídeos com maior discordância</h3>
                <table><thead><tr><th>Vídeo</th><th>Anotações</th><th>Distância</th></tr></thead><tbody>{video_rows}</tbody></table>
            </section>
        </div>
        <section class='analytics-grid'>
            <section class='dashboard-card daily-annotation-chart'><h3>Vídeos concluídos por dia e rotulador</h3><p class='metric-note'>Cada barra mostra quantos vídeos foram registrados por pessoa em cada dia da rodada selecionada.</p>{daily_annotation_chart}</section>
        </section>
        <section class='analytics-grid'>
            <section class='dashboard-card'><h3>Logins por dia</h3><p class='metric-note'>Barras representam a quantidade diária de entradas.</p>{access['daily_rows']}</section>
            <section class='dashboard-card'><h3>Sessões por anotador</h3><p class='metric-note'>Média geral: {access['avg_minutes']:.1f} min por sessão.</p>{access['session_rows']}</section>
        </section>
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

    previous_target = str(queue[position - 1].get("target", "")).strip() if position > 0 else ""
    target_changed = position > 0 and previous_target != target.strip()
    return position, render_video_card(item, target_changed), gr.update(visible=True), pergunta_markdown, pb

# ==============================================================================
# EVENTOS DA INTERFACE
# ==============================================================================
def authenticate(username):
    username = (username or "").strip()
    config = load_config()
    videos = load_videos()
    agora = time.time()

    blank_admin = (
        "", "", gr.update(choices=[], value=None), pd.DataFrame(), gr.update(), gr.update(), gr.update(), "", gr.update(), ""
    )

    if not username:
        return (gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), "⚠️ Informe seu usuário.", "", [], 0, 0.0, 0.0, "", gr.update(visible=False), "", "", *blank_admin, "")

    if username in config.get("admins", []) or username in config.get("labelers", []):
        if not claim_active_user(username):
            return (gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), "⚠️ Este usuário já está logado em outra sessão.", "", [], 0, 0.0, 0.0, "", gr.update(visible=False), "", "", *blank_admin, "")

    if username in config.get("admins", []):
        registrar_log(username, "LOGIN_ADMIN", 0)
        assignments = load_assignments(config, videos)
        results = read_results()
        errors = read_error_assignments()
        versions = assignment_versions(assignments, results, errors)
        current_version = versions[0] if versions else None
        admin_msg = f"{len(videos)} vídeos | {len(results)} anotações | {len(assignments)} atribuições na rodada mais recente."
        config_view = f"**Configuração vigente (somente leitura)**  \nAdministradores: `{', '.join(config.get('admins', []))}`  \nRotuladores: `{', '.join(config.get('labelers', []))}`  \nTarefa: `{config.get('tarefa', 'Detecção de Posição')}`  \nOverlap: `{config.get('overlap_percent', 0)}%`"
        file_res = gr.update(value=str(RESULTS_FILE) if RESULTS_FILE.exists() else None)
        file_log = gr.update(value=str(LOG_FILE) if LOG_FILE.exists() else None)
        file_err = gr.update(value=str(ERRORS_FILE) if ERRORS_FILE.exists() else None)
        agreement_msg = agreement_summary(results, current_version)
        file_agreement = gr.update(value=str(AGREEMENT_FILE))
        assignment_view = assignment_table_for_version(assignments, results, errors, current_version)

        return (gr.update(visible=False), gr.update(visible=True), gr.update(visible=False), "", "", [], 0, 0.0, 0.0, "", gr.update(visible=False), "", "", config_view, admin_msg, gr.update(choices=versions, value=current_version), assignment_view, file_res, file_log, file_err, agreement_msg, file_agreement, username)

    if username in config.get("labelers", []):
        registrar_log(username, "LOGIN_LABELER", 0)
        queue = user_queue(username, config, videos)
        position = completed_position(username, queue, read_results(), read_error_assignments())
        if not queue:
            new_position, video_html, controls_visible, pergunta, progress = video_outputs([], 0, username)
            return (gr.update(visible=False), gr.update(visible=False), gr.update(visible=True), "", f"**Rotulador:** {username}", [], new_position, agora, agora, video_html, controls_visible, pergunta, progress, *blank_admin, username)

        new_position, video_html, controls_visible, pergunta, progress = video_outputs(queue, position, username)
        return (gr.update(visible=False), gr.update(visible=False), gr.update(visible=True), "", f"**Rotulador:** {username}", queue, new_position, agora, agora, video_html, controls_visible, pergunta, progress, *blank_admin, username)

    return (gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), "⚠️ Usuário não autorizado.", "", [], 0, 0.0, 0.0, "", gr.update(visible=False), "", "", *blank_admin, "")

def reset_to_login(session_username="", message="Sessão encerrada. Seu progresso foi salvo e será retomado no próximo login."):
    release_active_user(str(session_username).strip())
    config = load_config()
    blank_admin = (
        "", "", gr.update(choices=[], value=None), pd.DataFrame(), gr.update(), gr.update(), gr.update(), "", gr.update(), ""
    )
    return (gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), message, "", [], 0, 0.0, 0.0, "", gr.update(visible=False), "", "", *blank_admin, "")

def save_and_exit(username):
    username_clean = str(username).replace("**Rotulador:** ", "").strip()
    if username_clean:
        registrar_log(username_clean, "SALVOU_E_SAIU", 0)
    return reset_to_login(username_clean)

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

    retry_replacement = False
    if stance == "ERRO_MANUAL_TIMEOUT":
        if tempo_analise < 10:
            new_pos, video_html, controls_visible, pergunta, progress = video_outputs(queue, position, username_clean)
            aviso = f"⏳ Aguarde 10s para selecionar esta opção. O vídeo ainda pode carregar. (Faltam {int(10 - tempo_analise)}s)"
            return position, start_time, video_html, controls_visible, pergunta, progress, aviso, gr.update()
        else:
            source_error = read_error_assignments()
            source_error = source_error[
                source_error["replacement_video_id"].astype(str).eq(str(item["video_id"]))
                & source_error["replacement_assignment_id"].astype(str).ne("")
            ]
            source_item = source_error.iloc[0] if not source_error.empty else None
            error_row = {
                "timestamp": datetime.now(BRT_TZ).isoformat(),
                "video_id": source_item["video_id"] if source_item is not None else item["video_id"],
                "url": source_item["url"] if source_item is not None else item["url"],
                "labeler": username_clean,
                "assignment_id": item.get("assignment_id", f"legacy:{username_clean}:{item['video_id']}")
            }
            with FILE_LOCK:
                ensure_errors_schema()
                append_csv_row(
                    ERRORS_FILE,
                    error_row,
                    [
                        "timestamp", "video_id", "url", "labeler", "assignment_id",
                        "replacement_video_id", "replacement_url", "replacement_assignment_id", "replacement_status",
                    ],
                )
            updated_queue = user_queue(username_clean, load_config(), load_videos())
            if position < len(updated_queue) and updated_queue[position]["video_id"] != item["video_id"]:
                queue[:] = updated_queue
                retry_replacement = True
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

    next_position = position if retry_replacement else position + 1
    new_position, video_html, controls_visible, pergunta, progress = video_outputs(queue, next_position, username_clean)

    return new_position, agora, video_html, controls_visible, pergunta, progress, "", gr.update(value=None)

def save_annotation_normal(username, queue, position, stance, login_time, start_time):
    return process_annotation(username, queue, position, stance, login_time, start_time)

def save_annotation_error(username, queue, position, login_time, start_time):
    return process_annotation(username, queue, position, "ERRO_MANUAL_TIMEOUT", login_time, start_time)


def release_port(port):
    """Encerra o processo que ocupa a porta antes de iniciar o Gradio."""
    port = int(port)
    port_spec = f"{port}/tcp"
    result = subprocess.run(
        ["fuser", "-k", "-TERM", port_spec],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                pass
        except OSError:
            return
        time.sleep(0.1)

    subprocess.run(
        ["fuser", "-k", "-KILL", port_spec],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


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
    .assignment-progress { margin: 0 0 22px; padding: 20px; border: 1px solid #dedede; border-top: 4px solid #9c0014; background: #ffffff; }
    .section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
    .section-heading h3 { margin: 2px 0 0; color: #232323; font-size: 21px; }
    .section-eyebrow { color: #9c0014; font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
    .version-label { padding: 5px 8px; background: #f7f7f7; color: #666; font-family: monospace; font-size: 12px; }
    .progress-table { width: 100%; border-collapse: collapse; }
    .progress-table th, .progress-table td { padding: 11px 12px; border-bottom: 1px solid #ededed; text-align: left; background: #ffffff !important; color: #232323 !important; }
    .progress-table th { background: #f7f7f7 !important; color: #666 !important; font-size: 13px; text-transform: uppercase; }
    .agreement-kpis { display: grid; grid-template-columns: repeat(3, minmax(160px, 1fr)); gap: 14px; margin: 12px 0 18px; }
    .kpi { border: 1px solid #e1e1e1; border-left: 4px solid #9c0014; padding: 14px 16px; background: #fffafa; }
    .kpi span { display: block; color: #666; font-size: 15px; text-transform: uppercase; }
    .kpi strong { display: block; color: #9c0014; font-size: 28px; margin-top: 4px; }
    .dashboard-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.5fr); gap: 20px; }
    .analytics-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.5fr); gap: 20px; margin-top: 20px; }
    .bar-row { display: grid; grid-template-columns: 92px minmax(0, 1fr) 28px; align-items: center; gap: 8px; margin: 10px 0; font-size: 13px; }
    .bar-track { height: 12px; background: #f0e5e7; overflow: hidden; }
    .bar-fill { height: 100%; background: #9c0014; }
    .chart-legend { display: flex; flex-wrap: wrap; gap: 12px 18px; margin: 4px 0 16px; font-size: 13px; }
    .chart-legend-item { display: inline-flex; align-items: center; gap: 6px; }
    .chart-legend-item i { width: 10px; height: 10px; display: inline-block; }
    .daily-annotation-day { padding: 10px 0 12px; border-top: 1px solid #ededed; }
    .daily-day-label { display: flex; justify-content: space-between; margin-bottom: 7px; color: #333; font-weight: 700; font-size: 13px; }
    .daily-day-label span { color: #888; font-weight: 400; }
    .daily-bar-row { display: grid; grid-template-columns: 86px minmax(0, 1fr) 24px; align-items: center; gap: 8px; margin: 5px 0; font-size: 12px; }
    .daily-bar-track { height: 10px; background: #f0f0f0; overflow: hidden; }
    .daily-bar-fill { height: 100%; min-width: 2px; }
    .dashboard-card { border: 1px solid #e1e1e1; padding: 16px; background: #ffffff; min-width: 0; }
    .dashboard-card h3 { margin: 0 0 12px; color: #333; font-size: 19px; }
    .dashboard-card table { width: 100%; border-collapse: collapse; background: #ffffff !important; }
    .dashboard-card th, .dashboard-card td { padding: 11px 12px; border-bottom: 1px solid #ededed; text-align: left; background: #ffffff !important; color: #232323 !important; font-size: 16px; }
    .dashboard-card th { color: #666 !important; font-weight: 700; }
    .muted { color: #888 !important; }
    .metric-note { margin: 6px 0 12px; color: #666; font-size: 13px; line-height: 1.45; }
    .duration-alert { margin: 14px 0 20px; padding: 16px; border: 1px solid #e0a400; border-left: 5px solid #c48700; background: #fff9e6; color: #4a3a00; }
    .duration-alert h3 { margin: 0 0 6px; color: #8a6200; font-size: 20px; }
    .duration-alert p { margin: 0 0 12px; font-size: 16px; }
    .duration-alert table { width: 100%; border-collapse: collapse; background: #fff9e6 !important; }
    .duration-alert th, .duration-alert td { padding: 10px 12px; border-bottom: 1px solid #eadcae; text-align: left; background: #fff9e6 !important; color: #4a3a00 !important; font-size: 16px; }
    .duration-alert th { font-weight: 700; }
    .admin-command-center { margin: 0 0 22px; padding: 22px; border: 1px solid #d8dde3; border-top: 5px solid #176b87; background: #f7fafb; }
    .admin-section-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
    .admin-section-heading h3 { margin: 3px 0 0; color: #1f2933; font-size: 23px; }
    .admin-live-dot { padding: 6px 10px; border: 1px solid #b8d8df; color: #176b87; background: #eef8fa; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; }
    .admin-kpi-grid { display: grid; grid-template-columns: repeat(6, minmax(130px, 1fr)); gap: 10px; }
    .admin-kpi { min-height: 98px; padding: 13px 14px; border: 1px solid #dfe5e8; background: #ffffff; }
    .admin-kpi span, .admin-kpi small { display: block; color: #66737c; }
    .admin-kpi span { min-height: 30px; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; }
    .admin-kpi strong { display: block; margin: 4px 0 2px; color: #176b87; font-size: 27px; line-height: 1; }
    .admin-kpi small { font-size: 11px; line-height: 1.3; }
    .admin-insight-grid { display: grid; grid-template-columns: 1fr 1fr 1.25fr; gap: 12px; margin-top: 14px; }
    .admin-chart, .admin-signals { min-width: 0; padding: 16px; border: 1px solid #dfe5e8; background: #ffffff; }
    .admin-chart h4, .admin-signals h4 { margin: 0; color: #1f2933; font-size: 16px; }
    .insight-bar-row { display: grid; grid-template-columns: minmax(92px, .8fr) minmax(0, 1.5fr) 42px; align-items: center; gap: 8px; margin: 13px 0; font-size: 12px; }
    .insight-bar-track { height: 11px; overflow: hidden; background: #e8eef0; }
    .insight-bar-fill { height: 100%; min-width: 2px; background: #176b87; transition: width .35s ease; }
    .insight-signal { display: grid; gap: 4px; margin-top: 10px; padding: 10px 11px; border-left: 4px solid #176b87; background: #f2f8fa; color: #33434d; font-size: 12px; line-height: 1.4; }
    .insight-signal strong { color: #176b87; font-size: 12px; }
    .insight-signal.warning { border-left-color: #b56b00; background: #fff8eb; }
    .insight-signal.warning strong { color: #8a5700; }
    .insight-signal.critical { border-left-color: #9c0014; background: #fff0f2; }
    .insight-signal.critical strong { color: #9c0014; }
    .insight-signal.ok { border-left-color: #4f6f52; background: #f2f8f2; }
    .insight-signal.ok strong { color: #4f6f52; }

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
    .target-transition { animation: target-highlight 12s ease-in-out; will-change: background, border-color, box-shadow, transform; }
    @keyframes target-highlight {
        0%, 100% { background: #fffafa; border-left-color: #9c0014; box-shadow: none; transform: translateY(0); }
        8% { background: #ffd6dc; border-left-color: #d34758; box-shadow: 0 0 0 3px rgba(211, 71, 88, 0.28), 0 8px 20px rgba(156, 0, 20, 0.16); transform: translateY(-3px); }
        28% { background: #fff0f2; border-left-color: #b51f35; box-shadow: 0 0 0 2px rgba(211, 71, 88, 0.22), 0 5px 14px rgba(156, 0, 20, 0.12); transform: translateY(0); }
        55% { background: #ffe1e5; border-left-color: #c12d42; box-shadow: 0 0 0 3px rgba(211, 71, 88, 0.24), 0 6px 16px rgba(156, 0, 20, 0.14); }
        78% { background: #fff1f3; border-left-color: #ae1c32; box-shadow: 0 0 0 2px rgba(211, 71, 88, 0.18), 0 4px 12px rgba(156, 0, 20, 0.1); }
    }
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
    .admin-actions { position: absolute !important; top: 24px; right: 36px; width: 220px !important; margin: 0 !important; z-index: 10; }
    .admin-actions button { margin-top: 0 !important; }
    .annotation-controls { gap: 0 !important; row-gap: 0 !important; }
    .annotation-controls > .form, .annotation-controls > .block { margin-top: 0 !important; }
    .labeler-warning-stack { font-size: 15px; font-style: italic; font-weight: 400; color: #666; line-height: 1.5; margin: 0 0 8px; padding: 0; }
    .labeler-warning-stack div + div { margin-top: 2px; }
    .target-question { margin: 4px 0 12px; color: #232323; font-size: 18px; line-height: 1.5; font-weight: 400; }
    .target-question strong { font-weight: 700 !important; color: #171717; }

    @media (max-width: 850px) {
        .shell { width: calc(100vw - 24px) !important; padding: 22px 16px !important; margin: 12px auto !important; }
        .agreement-kpis, .dashboard-grid, .analytics-grid { grid-template-columns: 1fr; }
        .admin-kpi-grid, .admin-insight-grid { grid-template-columns: 1fr; }
        .admin-section-heading { align-items: flex-start; flex-direction: column; }
        .labeler-layout { flex-direction: column !important; }
        .video-content-grid { grid-template-columns: 1fr; }
        .video-card iframe { width: 100% !important; max-width: 650px !important; }
        .labeler-actions { position: static !important; width: 100% !important; margin-bottom: 16px; }
        .admin-actions { position: static !important; width: 100% !important; margin-bottom: 16px !important; }
    }

    .brand-header { border-bottom: 1px solid #eaeaea; padding-bottom: 25px; margin-bottom: 30px; text-align: left; }
    .brand-header h1 { font-size: 28px !important; font-weight: 700 !important; margin: 0 !important; color: #9c0014 !important; }
    .brand-header p { color: #444444 !important; margin: 10px 0 0 !important; font-size: 16px !important; line-height: 1.5; }

    button.ufmg-btn {
        background: #9c0014 !important; border: none !important; color: #ffffff !important;
        font-weight: 600 !important; font-size: 17px !important; padding: 13px 21px !important; width: 100% !important;
    }
    .admin-panel { gap: 10px !important; }
    .admin-panel, .admin-panel label, .admin-panel input, .admin-panel textarea, .admin-panel button { font-size: 15px !important; }
    .admin-panel h2, .admin-panel h3 { font-size: 20px !important; }
    .admin-header { align-items: center !important; justify-content: space-between !important; margin: 0 0 8px !important; min-height: 36px !important; }
    .admin-header h2 { margin: 0 !important; }
    .admin-configuration, .admin-configuration * { color: #33434d !important; }
    .admin-configuration { min-height: 0 !important; padding: 14px 18px; border: 1px solid #d8dde3; border-left: 4px solid #176b87; background: #f7fafb !important; line-height: 1.55; }
    .admin-configuration strong { color: #1f2933 !important; }
    .admin-configuration code { display: inline-block; margin: 2px 5px 2px 2px; padding: 3px 7px; border: 1px solid #c9dce1; border-radius: 3px; background: #eaf5f7 !important; color: #176b87 !important; font-family: inherit; font-weight: 700; }
    .admin-configuration p { margin: 2px 0 !important; }
    .admin-status { margin: 8px 0; padding: 9px 12px; border-left: 4px solid #9c0014; background: #fff5f6; }
    .admin-downloads { margin: 10px 0 16px; padding: 8px; border-top: 1px solid #e5e5e5; border-bottom: 1px solid #e5e5e5; }
    .admin-audit { margin: 16px 0 6px !important; }
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
            with gr.Row(elem_classes="admin-header"):
                gr.Markdown("## Administração de Pesquisa")
                with gr.Row(elem_classes="admin-actions"):
                    admin_save_exit_button = gr.Button("Salvar e sair", elem_classes="ufmg-btn")

            admin_configuration = gr.Markdown(elem_classes="admin-configuration")
            admin_message = gr.Markdown(elem_classes="admin-status")
            gr.Markdown("### Dados Coletados")
            assignment_version_dropdown = gr.Dropdown(label="Rodada de atribuição exibida", choices=[], value=None, interactive=True)
            agreement_panel = gr.HTML()
            with gr.Row(elem_classes="admin-downloads"):
                results_download = gr.File(label="Resultados Anotação (CSV)")
                log_download = gr.File(label="Metadados e Tempos (CSV)")
                errors_download = gr.File(label="Vídeos com Erro (CSV)")
                agreement_download = gr.File(label="Concordância (JSON)")

            gr.Markdown("### Auditoria de Atribuições", elem_classes="admin-audit")
            assignment_table = gr.Dataframe(headers=["assignment_version", "assignment_id", "labeler", "video_id", "url", "target", "video_description", "voice_to_text", "video_duration"], interactive=False, elem_classes="assignment-table", max_height=2000, wrap=True)

        # --- TELA DE ROTULAÇÃO (LAYOUT HORIZONTAL) ---
        with gr.Column(visible=False, elem_classes="labeler-panel") as labeler_panel:
            with gr.Row(elem_classes="labeler-actions"):
                save_exit_button = gr.Button("Salvar e sair", elem_classes="ufmg-btn")

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
            session_username_state = gr.State("")

    # --- EVENTOS ---
    outputs_login = [
        login_panel, admin_panel, labeler_panel, login_message,
        labeler_name, queue_state, position_state, login_time_state, start_time_state,
        video_html, annotation_controls, target_question, labeler_progress,
        admin_configuration, admin_message, assignment_version_dropdown, assignment_table, results_download, log_download, errors_download, agreement_panel, agreement_download, session_username_state
    ]
    login_button.click(authenticate, inputs=username_input, outputs=outputs_login)
    username_input.submit(authenticate, inputs=username_input, outputs=outputs_login)
    admin_save_exit_button.click(reset_to_login, inputs=session_username_state, outputs=outputs_login)
    save_exit_button.click(save_and_exit, inputs=labeler_name, outputs=outputs_login)

    assignment_version_dropdown.change(
        lambda version: (agreement_summary(read_results(), version), assignment_table_for_version(load_assignments(load_config(), load_videos()), read_results(), read_error_assignments(), version)),
        inputs=assignment_version_dropdown,
        outputs=[agreement_panel, assignment_table],
    )

    outputs_save = [position_state, start_time_state, video_html, annotation_controls, target_question, labeler_progress, labeler_message, stance_radio]

    # Evento de Salvar Normal
    save_button.click(save_annotation_normal, inputs=[labeler_name, queue_state, position_state, stance_radio, login_time_state, start_time_state], outputs=outputs_save)

    # Evento de Reportar Erro (Timer de 30s)
    error_button.click(save_annotation_error, inputs=[labeler_name, queue_state, position_state, login_time_state, start_time_state], outputs=outputs_save)

if __name__ == "__main__":
    server_port = int(os.getenv("GRADIO_SERVER_PORT", "7860"))
    release_port(server_port)
    demo.launch(
                #share=True,
                server_name="0.0.0.0", 
                server_port=server_port, 
                theme=tema_branco, 
                css=APP_CSS
    )