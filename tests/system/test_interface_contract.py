import app


def test_labeler_interface_contract():
    queue = app.user_queue("pi", app.load_config(), app.load_videos())
    assert queue
    _, video_html, controls, question, progress = app.video_outputs(queue, 0, "pi")

    assert controls.get("visible") is True
    assert "player/v1/" in video_html
    assert "Descrição do vídeo" in video_html
    assert "Transcrição do áudio" in video_html
    assert "assignment_version" in queue[0]
    assert "de" in progress
    assert "<strong>" in question
