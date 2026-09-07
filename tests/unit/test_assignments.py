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


def test_dataset_change_creates_new_assignment_version():
    config = {"labelers": ["one", "two"], "overlap_percent": 50}
    original = app.make_assignments(config, sample_videos(4))
    changed = app.make_assignments(config, sample_videos(5))

    assert original["assignment_version"].nunique() == 1
    assert changed["assignment_version"].nunique() == 1
    assert original.iloc[0]["assignment_version"] != changed.iloc[0]["assignment_version"]
    assert original.iloc[0]["assignment_id"] != changed.iloc[0]["assignment_id"]
