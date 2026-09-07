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


def test_weighted_distance_penalizes_polar_disagreement_more():
    assert app.label_distance("Contra", "A Favor") == 4
    assert app.label_distance("Contra", "Neutro") == 1
    assert app.label_distance("Neutro", "A Favor") == 1
