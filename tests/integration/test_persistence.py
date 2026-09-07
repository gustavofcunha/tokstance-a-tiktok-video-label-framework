import pandas as pd

import app


def test_progress_uses_current_assignments_only():
    videos = pd.DataFrame({
        "id": ["1", "2", "3"],
        "url": ["u", "u", "u"],
        "target": ["t", "t", "t"],
        "video_description": ["", "", ""],
        "voice_to_text": ["", "", ""],
        "video_duration": ["10", "10", "10"],
    })
    config = {"labelers": ["annotator"], "overlap_percent": 0}
    queue = app.user_queue("annotator", config, videos)
    old_results = pd.DataFrame([{
        "labeler": "annotator",
        "video_id": "1",
        "assignment_id": "legacy:annotator:1",
    }])

    assert app.completed_position("annotator", queue, old_results) == 0

    current_results = pd.DataFrame([{
        "labeler": "annotator",
        "video_id": "1",
        "assignment_id": queue[0]["assignment_id"],
    }])
    assert app.completed_position("annotator", queue, current_results) == 1


def test_error_assignment_counts_as_completed():
    videos = pd.DataFrame({
        "id": ["1", "2"],
        "url": ["u", "u"],
        "target": ["t", "t"],
        "video_description": ["", ""],
        "voice_to_text": ["", ""],
        "video_duration": ["10", "10"],
    })
    queue = app.user_queue("annotator", {"labelers": ["annotator"], "overlap_percent": 0}, videos)
    errors = pd.DataFrame([{
        "labeler": "annotator",
        "video_id": "1",
        "assignment_id": queue[0]["assignment_id"],
    }])

    assert app.completed_position("annotator", queue, pd.DataFrame(), errors) == 1
