import csv
import json
import os
from pathlib import Path


DATA_DIR = Path(__file__).parent / ".test-data"
DATA_DIR.mkdir(exist_ok=True)
os.environ["TOKSTANCE_DATA_DIR"] = str(DATA_DIR)

videos = [
    [
        "id",
        "url",
        "target",
        "video_description",
        "voice_to_text",
        "video_duration",
    ],
    [
        "test-1",
        "https://www.tiktok.com/@test/video/test-1",
        "Test target",
        "Synthetic description",
        "Synthetic transcription",
        "10",
    ],
    [
        "test-2",
        "https://www.tiktok.com/@test/video/test-2",
        "Test target",
        "",
        "",
        "12",
    ],
]

with (DATA_DIR / "videos.csv").open("w", encoding="utf-8", newline="") as videos_file:
    csv.writer(videos_file).writerows(videos)

with (DATA_DIR / "configuracao.json").open("w", encoding="utf-8") as config_file:
    json.dump({
        "admins": ["admin"],
        "labelers": ["pi"],
        "videos_per_labeler": 0,
        "overlap_percent": 0.0,
        "tarefa": "Test annotation",
    }, config_file)
