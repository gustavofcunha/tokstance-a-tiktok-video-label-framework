import pandas as pd

import app


def sample_videos(count=36):
    return pd.DataFrame({
        "id": [str(index) for index in range(count)],
        "url": ["https://www.tiktok.com/@creator/video/1"] * count,
        "target": ["target"] * count,
        "video_description": [""] * count,
        "voice_to_text": [""] * count,
        "video_duration": ["10"] * count,
    })


def test_overlap_assignments_cover_all_videos():
    assignments = app.make_assignments(
        {"labelers": ["one", "two"], "overlap_percent": 50},
        sample_videos(),
    )

    assert len(assignments) == 54
    assert assignments["video_id"].nunique() == 36
    assert assignments.groupby("labeler").size().to_dict() == {"one": 27, "two": 27}
    assert (assignments.groupby("video_id").size() == 2).sum() == 18


def test_assignments_balance_targets_and_group_targets_per_labeler():
    videos = pd.DataFrame({
        "id": [str(index) for index in range(12)],
        "url": ["u"] * 12,
        "target": ["B", "A", "C", "B", "A", "C", "B", "A", "C", "B", "A", "C"],
        "video_description": [""] * 12,
        "voice_to_text": [""] * 12,
        "video_duration": ["10"] * 12,
    })
    assignments = app.make_assignments({"labelers": ["one", "two"], "overlap_percent": 50}, videos)

    for labeler, labeler_assignments in assignments.groupby("labeler"):
        target_sequence = labeler_assignments["target"].tolist()
        assert target_sequence == sorted(target_sequence, key={"B": 0, "A": 1, "C": 2}.get)

    target_counts = assignments.groupby(["labeler", "target"]).size().unstack(fill_value=0)
    assert (target_counts.max(axis=0) - target_counts.min(axis=0)).max() <= 1


def test_dataset_change_creates_new_assignment_version():
    config = {"labelers": ["one", "two"], "overlap_percent": 50}
    original = app.make_assignments(config, sample_videos(4))
    changed = app.make_assignments(config, sample_videos(5))

    assert original["assignment_version"].nunique() == 1
    assert changed["assignment_version"].nunique() == 1
    assert original.iloc[0]["assignment_version"] != changed.iloc[0]["assignment_version"]
    assert original.iloc[0]["assignment_id"] != changed.iloc[0]["assignment_id"]


def test_rebuilding_same_inputs_reuses_assignment_version():
    config = {"labelers": ["one", "two"], "overlap_percent": 50}
    first = app.make_assignments(config, sample_videos(6))
    second = app.make_assignments(config, sample_videos(6))

    assert first.iloc[0]["assignment_version"] == second.iloc[0]["assignment_version"]
    assert set(first["assignment_id"]) == set(second["assignment_id"])


def test_active_username_can_only_be_claimed_once():
    username = "single-session-user"
    app.release_active_user(username)
    assert app.claim_active_user(username) is True
    assert app.claim_active_user(username) is False

    app.release_active_user(username)
    assert app.claim_active_user(username) is True
    app.release_active_user(username)


def test_loading_assignments_does_not_rewrite_existing_manifest():
    config = {"labelers": ["one"], "overlap_percent": 0}
    videos = sample_videos(2)
    first = app.load_assignments(config, videos)
    manifest_before = app.ASSIGNMENTS_FILE.read_text(encoding="utf-8")
    second = app.load_assignments(config, videos)

    assert first.equals(second)
    assert app.ASSIGNMENTS_FILE.read_text(encoding="utf-8") == manifest_before


def test_error_gets_replaced_by_reserve_without_changing_assignment_count(monkeypatch, tmp_path):
    config = {"labelers": ["one"], "overlap_percent": 0}
    videos = sample_videos(2)
    assignments_file = tmp_path / "atribuicoes.csv"
    errors_file = tmp_path / "erros_videos.csv"
    reserve_file = tmp_path / "videos_reserva.csv"
    monkeypatch.setattr(app, "ASSIGNMENTS_FILE", assignments_file)
    monkeypatch.setattr(app, "ERRORS_FILE", errors_file)
    monkeypatch.setattr(app, "RESERVE_VIDEOS_FILE", reserve_file)

    reserve_videos = sample_videos(1).assign(id=["reserve-1"])
    reserve_videos.to_csv(reserve_file, index=False)
    initial = app.make_assignments(config, videos)
    errors_file.write_text(
        "timestamp,video_id,url,labeler,assignment_id\n"
        f"2026-09-07T10:00:00-03:00,0,u,one,{initial.iloc[0]['assignment_id']}\n",
        encoding="utf-8",
    )

    active = app.load_assignments(config, videos)
    errors = app.read_error_assignments()

    assert len(active) == len(initial)
    assert active.iloc[0]["video_id"] == "reserve-1"
    assert errors.iloc[0]["replacement_video_id"] == "reserve-1"
    assert errors.iloc[0]["replacement_assignment_id"] == active.iloc[0]["assignment_id"]
    assert app.load_assignments(config, videos).iloc[0]["video_id"] == "reserve-1"


def test_a_reserve_video_cannot_fill_two_error_slots(monkeypatch, tmp_path):
    config = {"labelers": ["one"], "overlap_percent": 0}
    videos = sample_videos(2)
    assignments_file = tmp_path / "atribuicoes.csv"
    errors_file = tmp_path / "erros_videos.csv"
    reserve_file = tmp_path / "videos_reserva.csv"
    monkeypatch.setattr(app, "ASSIGNMENTS_FILE", assignments_file)
    monkeypatch.setattr(app, "ERRORS_FILE", errors_file)
    monkeypatch.setattr(app, "RESERVE_VIDEOS_FILE", reserve_file)

    sample_videos(1).assign(id=["reserve-1"]).to_csv(reserve_file, index=False)
    initial = app.make_assignments(config, videos)
    pd.DataFrame([
        {"timestamp": "2026-09-07T10:00:00-03:00", "video_id": "0", "url": "u", "labeler": "one", "assignment_id": initial.iloc[0]["assignment_id"]},
        {"timestamp": "2026-09-07T10:01:00-03:00", "video_id": "1", "url": "u", "labeler": "one", "assignment_id": initial.iloc[1]["assignment_id"]},
    ]).to_csv(errors_file, index=False)

    active = app.load_assignments(config, videos)
    errors = app.read_error_assignments()

    assert active["video_id"].tolist().count("reserve-1") == 1
    assert errors["replacement_video_id"].tolist().count("reserve-1") == 1
    assert errors.iloc[1]["replacement_status"] == "SEM_RESERVA_DISPONIVEL"


def test_reserve_must_have_the_same_target_as_error_slot(monkeypatch, tmp_path):
    config = {"labelers": ["one"], "overlap_percent": 0}
    videos = sample_videos(1)
    assignments_file = tmp_path / "atribuicoes.csv"
    errors_file = tmp_path / "erros_videos.csv"
    reserve_file = tmp_path / "videos_reserva.csv"
    monkeypatch.setattr(app, "ASSIGNMENTS_FILE", assignments_file)
    monkeypatch.setattr(app, "ERRORS_FILE", errors_file)
    monkeypatch.setattr(app, "RESERVE_VIDEOS_FILE", reserve_file)

    sample_videos(1).assign(id=["reserve-other-target"], target=["different target"]).to_csv(reserve_file, index=False)
    initial = app.make_assignments(config, videos)
    pd.DataFrame([{
        "timestamp": "2026-09-07T10:00:00-03:00",
        "video_id": "0",
        "url": "u",
        "labeler": "one",
        "assignment_id": initial.iloc[0]["assignment_id"],
    }]).to_csv(errors_file, index=False)

    active = app.load_assignments(config, videos)
    errors = app.read_error_assignments()

    assert active.iloc[0]["video_id"] == "0"
    assert errors.iloc[0]["replacement_video_id"] == ""
    assert errors.iloc[0]["replacement_status"] == "SEM_RESERVA_COMPATIVEL"


def test_overlap_error_replaces_all_users_with_one_shared_reserve(monkeypatch, tmp_path):
    config = {"labelers": ["one", "two"], "overlap_percent": 100}
    videos = sample_videos(1)
    assignments_file = tmp_path / "atribuicoes.csv"
    errors_file = tmp_path / "erros_videos.csv"
    reserve_file = tmp_path / "videos_reserva.csv"
    monkeypatch.setattr(app, "ASSIGNMENTS_FILE", assignments_file)
    monkeypatch.setattr(app, "ERRORS_FILE", errors_file)
    monkeypatch.setattr(app, "RESERVE_VIDEOS_FILE", reserve_file)

    sample_videos(1).assign(id=["reserve-shared"]).to_csv(reserve_file, index=False)
    initial = app.make_assignments(config, videos)
    pd.DataFrame([{
        "timestamp": "2026-09-07T10:00:00-03:00",
        "video_id": "0",
        "url": "u",
        "labeler": "one",
        "assignment_id": initial.iloc[0]["assignment_id"],
    }]).to_csv(errors_file, index=False)

    first_load = app.load_assignments(config, videos)
    assert first_load["video_id"].tolist() == ["reserve-shared", "reserve-shared"]

    second_error = pd.DataFrame([{
        "timestamp": "2026-09-07T10:01:00-03:00",
        "video_id": "0",
        "url": "u",
        "labeler": "two",
        "assignment_id": initial.iloc[1]["assignment_id"],
    }])
    pd.concat([pd.read_csv(errors_file, dtype=str), second_error], ignore_index=True).to_csv(errors_file, index=False)
    second_load = app.load_assignments(config, videos)
    errors = app.read_error_assignments()

    assert second_load["video_id"].tolist() == ["reserve-shared", "reserve-shared"]
    assert errors["replacement_video_id"].tolist() == ["reserve-shared", "reserve-shared"]
    assert errors["replacement_status"].tolist() == ["SUBSTITUIDO_OVERLAP", "SUBSTITUIDO_OVERLAP"]


def test_overlap_after_existing_annotation_rebalances_before_critical_alert(monkeypatch, tmp_path):
    config = {"labelers": ["one", "two"], "overlap_percent": 50}
    videos = sample_videos(2)
    assignments_file = tmp_path / "atribuicoes.csv"
    errors_file = tmp_path / "erros_videos.csv"
    reserve_file = tmp_path / "videos_reserva.csv"
    results_file = tmp_path / "resultados_anotacao.csv"
    monkeypatch.setattr(app, "ASSIGNMENTS_FILE", assignments_file)
    monkeypatch.setattr(app, "ERRORS_FILE", errors_file)
    monkeypatch.setattr(app, "RESERVE_VIDEOS_FILE", reserve_file)
    monkeypatch.setattr(app, "RESULTS_FILE", results_file)

    sample_videos(1).assign(id=["reserve-rebalance"]).to_csv(reserve_file, index=False)
    initial = app.make_assignments(config, videos)
    overlap_video = initial.groupby("video_id").size().idxmax()
    individual_video = initial.groupby("video_id").size().idxmin()
    completed_individual = initial[initial["video_id"] == individual_video].iloc[0]
    completed_overlap = initial[initial["video_id"] == overlap_video].iloc[0]
    errored_overlap = initial[initial["video_id"] == overlap_video].iloc[1]
    pd.DataFrame([
        {
            "timestamp": "2026-09-07T10:00:00-03:00",
            "labeler": completed_overlap["labeler"],
            "video_id": overlap_video,
            "url": "u",
            "target": "target",
            "stance": "Contra",
            "tempo_analise_segundos": "20",
            "video_duration": "10",
            "assignment_id": completed_overlap["assignment_id"],
        },
        {
            "timestamp": "2026-09-07T10:01:00-03:00",
            "labeler": completed_individual["labeler"],
            "video_id": individual_video,
            "url": "u",
            "target": "target",
            "stance": "A Favor",
            "tempo_analise_segundos": "20",
            "video_duration": "10",
            "assignment_id": completed_individual["assignment_id"],
        },
    ]).to_csv(results_file, index=False)
    pd.DataFrame([{
        "timestamp": "2026-09-07T10:01:00-03:00",
        "video_id": overlap_video,
        "url": "u",
        "labeler": errored_overlap["labeler"],
        "assignment_id": errored_overlap["assignment_id"],
    }]).to_csv(errors_file, index=False)

    active = app.load_assignments(config, videos)
    errors = app.read_error_assignments()

    assert (active["video_id"] == individual_video).sum() == 2
    assert (active["video_id"] == "reserve-rebalance").sum() == 0
    assert errors.iloc[0]["replacement_status"] == "SUBSTITUIDO_OVERLAP_REBALANCEADO"


def test_rebalanced_video_is_promoted_to_all_pending_overlap_users(monkeypatch, tmp_path):
    config = {"labelers": ["one", "two", "three"], "overlap_percent": 50}
    videos = sample_videos(2)
    assignments_file = tmp_path / "atribuicoes.csv"
    errors_file = tmp_path / "erros_videos.csv"
    reserve_file = tmp_path / "videos_reserva.csv"
    results_file = tmp_path / "resultados_anotacao.csv"
    monkeypatch.setattr(app, "ASSIGNMENTS_FILE", assignments_file)
    monkeypatch.setattr(app, "ERRORS_FILE", errors_file)
    monkeypatch.setattr(app, "RESERVE_VIDEOS_FILE", reserve_file)
    monkeypatch.setattr(app, "RESULTS_FILE", results_file)

    sample_videos(1).assign(id=["unused-reserve"]).to_csv(reserve_file, index=False)
    initial = app.make_assignments(config, videos)
    overlap_video = initial.groupby("video_id").size().idxmax()
    individual_video = initial.groupby("video_id").size().idxmin()
    overlap_rows = initial[initial["video_id"] == overlap_video]
    individual_row = initial[initial["video_id"] == individual_video].iloc[0]
    completed_overlap = overlap_rows.iloc[0]
    errored_overlap = overlap_rows.iloc[1]
    pd.DataFrame([
        {
            "timestamp": "2026-09-07T10:00:00-03:00",
            "labeler": completed_overlap["labeler"],
            "video_id": overlap_video,
            "url": "u",
            "target": "target",
            "stance": "Contra",
            "tempo_analise_segundos": "20",
            "video_duration": "10",
            "assignment_id": completed_overlap["assignment_id"],
        },
        {
            "timestamp": "2026-09-07T10:01:00-03:00",
            "labeler": individual_row["labeler"],
            "video_id": individual_video,
            "url": "u",
            "target": "target",
            "stance": "A Favor",
            "tempo_analise_segundos": "20",
            "video_duration": "10",
            "assignment_id": individual_row["assignment_id"],
        },
    ]).to_csv(results_file, index=False)
    pd.DataFrame([{
        "timestamp": "2026-09-07T10:02:00-03:00",
        "video_id": overlap_video,
        "url": "u",
        "labeler": errored_overlap["labeler"],
        "assignment_id": errored_overlap["assignment_id"],
    }]).to_csv(errors_file, index=False)

    active = app.load_assignments(config, videos)

    assert (active["video_id"] == individual_video).sum() == 3
    assert (active["video_id"] == overlap_video).sum() == 1


def test_overlap_critical_alert_remains_when_no_valid_rebalance_exists(monkeypatch, tmp_path):
    config = {"labelers": ["one", "two"], "overlap_percent": 100}
    videos = sample_videos(1)
    assignments_file = tmp_path / "atribuicoes.csv"
    errors_file = tmp_path / "erros_videos.csv"
    reserve_file = tmp_path / "videos_reserva.csv"
    results_file = tmp_path / "resultados_anotacao.csv"
    monkeypatch.setattr(app, "ASSIGNMENTS_FILE", assignments_file)
    monkeypatch.setattr(app, "ERRORS_FILE", errors_file)
    monkeypatch.setattr(app, "RESERVE_VIDEOS_FILE", reserve_file)
    monkeypatch.setattr(app, "RESULTS_FILE", results_file)

    sample_videos(1).assign(id=["reserve-critical"]).to_csv(reserve_file, index=False)
    initial = app.make_assignments(config, videos)
    pd.DataFrame([{
        "timestamp": "2026-09-07T10:00:00-03:00",
        "labeler": "one",
        "video_id": "0",
        "url": "u",
        "target": "target",
        "stance": "Contra",
        "tempo_analise_segundos": "20",
        "video_duration": "10",
        "assignment_id": initial.iloc[0]["assignment_id"],
    }]).to_csv(results_file, index=False)
    pd.DataFrame([{
        "timestamp": "2026-09-07T10:01:00-03:00",
        "video_id": "0",
        "url": "u",
        "labeler": "two",
        "assignment_id": initial.iloc[1]["assignment_id"],
    }]).to_csv(errors_file, index=False)

    app.load_assignments(config, videos)
    errors = app.read_error_assignments()

    assert errors.iloc[0]["replacement_status"] == "CRITICO_OVERLAP_INCONSISTENTE"
    dashboard = app.admin_insights_html(
        app.load_assignments(config, videos),
        app.read_results(),
        errors,
        videos,
        {"videos_com_discordancia": []},
        [],
    )
    assert "Alerta crítico de overlap" in dashboard


def test_weighted_distance_penalizes_polar_disagreement_more():
    assert app.label_distance("Contra", "A Favor") == 4
    assert app.label_distance("Contra", "Neutro") == 1
    assert app.label_distance("Neutro", "A Favor") == 1
