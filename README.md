# Multimodal Stance Annotation Framework

An online data-labeling framework for multiple annotators to label the stance expressed in TikTok videos. The project is designed for a multimodal stance-detection task: annotators watch each video and classify its position in relation to a target claim or topic.

## Features

- Gradio web interface suitable for deployment as a Hugging Face Space
- Annotator identification before labeling begins
- Embedded TikTok videos loaded from `videos.csv`
- Sequential annotation workflow with one video per screen
- Five stance and quality-control labels:
	- `A Favor` (in favor)
	- `Contra` (against)
	- `Neutro` (neutral)
	- `Não Relacionado` (not related)
	- `Erro/Vídeo Indisponível` (error/video unavailable)
- Incremental CSV export to `resultados_anotacao.csv`
- Download button for backing up the collected annotations

## How It Works

1. The annotator enters a name.
2. The app displays the first available TikTok video and its target.
3. The annotator selects the stance expressed by the video toward the target.
4. Clicking **Save and Next** appends the annotation to the results CSV and loads the next video.
5. When all rows have been labeled, the interface displays a completion message.

The current implementation processes videos in the order in which they appear in `videos.csv`. The results file is shared by the running application, so it should be downloaded regularly as a backup.

## Input Data

Place a `videos.csv` file in the project root with the following columns:

```csv
id,url,target
1234567890123456789,https://www.tiktok.com/@creator/video/1234567890123456789,example target
```

- `id`: unique identifier for the video
- `url`: public TikTok URL used to render the embedded video
- `target`: claim, topic, or entity against which stance is annotated

TikTok embeds require internet access in the annotator's browser. Videos that cannot be embedded can be labeled as `Erro/Vídeo Indisponível`.

## Output Data

Annotations are appended to `resultados_anotacao.csv` with these columns:

```text
anotador,video_id,target,stance
```

The output file is generated automatically when the first annotation is saved. It is not included in the repository by default.

## Run Locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

Gradio prints a local URL in the terminal. Open it in a browser to start annotating.

## Deploy on Hugging Face Spaces

1. Create a new Space at [huggingface.co/new-space](https://huggingface.co/new-space).
2. Select **Gradio** as the SDK and Python `3.12` as the runtime.
3. Upload or push `app.py`, `videos.csv`, `requirements.txt`, and this `README.md` to the Space repository.
4. Wait for the Space to build and open the generated public URL.

The Space configuration is stored in the YAML front matter at the top of this README. Hugging Face uses `app.py` as the entry point and installs the dependencies listed in `requirements.txt`.

## Project Structure

```text
.
├── app.py              # Gradio application
├── videos.csv          # Video URLs and annotation targets
├── requirements.txt    # Python dependencies
└── README.md           # Hugging Face Space configuration and documentation
```

## Notes for Multi-Annotator Studies

This version identifies annotators in each output row and stores all annotations in one CSV. For production studies, download the results frequently and consider adding a persistent external database if the Space needs stronger durability, concurrent-write handling, or long-term storage.
