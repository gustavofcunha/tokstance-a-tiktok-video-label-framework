import csv
import os
import subprocess
import sys
from pathlib import Path


def write_videos(path, filename, rows):
    with (path / filename).open("w", encoding="utf-8", newline="") as data_file:
        csv.writer(data_file).writerows(rows)


def run_app_import(data_dir):
    environment = os.environ.copy()
    environment["TOKSTANCE_DATA_DIR"] = str(data_dir)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    return subprocess.run(
        [sys.executable, "-c", "import app"],
        capture_output=True,
        text=True,
        env=environment,
    )


def valid_rows(video_id):
    return [["id", "url", "target"], [video_id, "https://example.test/video", "target"]]


def test_startup_aborts_when_videos_csv_is_missing(tmp_path):
    write_videos(tmp_path, "videos_reserva.csv", valid_rows("reserve-1"))

    result = run_app_import(tmp_path)

    assert result.returncode != 0
    assert "videos.csv" in result.stderr
    assert "ausente" in result.stderr


def test_startup_aborts_when_videos_reserva_csv_is_missing(tmp_path):
    write_videos(tmp_path, "videos.csv", valid_rows("video-1"))

    result = run_app_import(tmp_path)

    assert result.returncode != 0
    assert "videos_reserva.csv" in result.stderr
    assert "ausente" in result.stderr


def test_startup_aborts_when_required_csv_has_no_records(tmp_path):
    write_videos(tmp_path, "videos.csv", valid_rows("video-1"))
    write_videos(tmp_path, "videos_reserva.csv", [["id", "url", "target"]])

    result = run_app_import(tmp_path)

    assert result.returncode != 0
    assert "videos_reserva.csv" in result.stderr
    assert "ao menos um registro" in result.stderr


def test_startup_aborts_when_videos_csv_has_no_records(tmp_path):
    write_videos(tmp_path, "videos.csv", [["id", "url", "target"]])
    write_videos(tmp_path, "videos_reserva.csv", valid_rows("reserve-1"))

    result = run_app_import(tmp_path)

    assert result.returncode != 0
    assert "videos.csv" in result.stderr
    assert "ao menos um registro" in result.stderr
