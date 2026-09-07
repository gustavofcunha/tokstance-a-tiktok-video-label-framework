# TokStance! 
##  A Open-Source Multimodal Stance Annotation Framework

TokStance is an open-source Gradio framework for collaborative stance annotation of TikTok videos. Annotators classify the creator's position in relation to a target while considering the post description, transcription, audio, visual elements, and context.

The framework is intentionally adaptable. You can add or remove input features, change the annotation task, replace or extend the label options, customize the assignment policy, and adapt the persistence layer to the needs of your study.

## Features

- Separate administrator and annotator access.
- Official TikTok player using the `player/v1` endpoint.
- Optional video description and audio transcription displayed beside the player.
- Assignment of every video with configurable overlap.
- Versioned assignments with stable `assignment_version` and `assignment_id` values.
- Assignment regeneration when the dataset or assignment configuration changes.
- Progress recovery between sessions, including videos reported as unavailable.
- Configurable stance labels. The default labels are `Contra`, `A Favor`, `Neutro`, and `Vídeo não relacionado ao target`.
- Administrator dashboard with assignment auditing, agreement metrics, duration alerts, and downloads.
- Alerts for analyses more than five seconds shorter than the video duration.
- Ordinal Krippendorff's alpha for videos with multiple annotations.
- Local CSV and JSON persistence, suitable for prototypes and small studies.

## Adaptability

The application is not tied to one research task or one fixed annotation scheme.

You can adapt:

- **Video features:** add, remove, or rename fields such as descriptions, transcriptions, duration, language, country, creator metadata, or extracted multimodal features.
- **Annotation task:** change the target question, replace stance detection with another labeling task, or display task-specific instructions.
- **Label options:** edit `STANCE_OPTIONS` in `src/app.py` or load labels from a configuration file.
- **Assignment policy:** change overlap, balancing, ordering, sampling, or annotator eligibility rules.
- **Storage:** replace CSV/JSON files with a database, object storage, or an API-backed repository for concurrent production use.
- **Interface:** customize the Gradio layout, CSS, player, dashboard, and administrative controls.

## How It Works

1. An authorized user enters an identifier.
2. An administrator configures annotators, task text, and overlap percentage.
3. The application assigns every video: the overlap portion is assigned to every annotator and the remaining videos are distributed in round-robin order.
4. The annotator selects a label or reports that the video did not load.
5. A normal annotation is saved with `assignment_version`, `assignment_id`, video duration, and analysis time. An unavailable-video report is saved in the error log with the same assignment identity.
6. When the annotator returns, the application reads both annotation and error records and opens the first pending assignment while keeping the original queue total.

For `N` videos and overlap percentage `p`, the number of shared videos is `floor(N * p / 100)`. The remaining videos receive one assignment each. For example, 36 videos with 50% overlap and two annotators produce 18 shared videos, 18 single-annotator videos, and 54 total assignments, with 27 assignments per annotator.

## Assignment Management and Versioning

Assignments are versioned from the ordered dataset and assignment configuration. A version changes when relevant input changes, including:

- video IDs, URLs, or targets;
- the annotator list;
- the overlap percentage.

The current version is written to `atribuicoes.csv`. Each row contains an `assignment_version` and an `assignment_id` in the form:

```text
<assignment_version>:<labeler>:<video_id>
```

This prevents annotations from an old dataset or old assignment configuration from incorrectly completing assignments in a new round. Existing result and error files remain available for audit, while the current assignment version defines the active queue.

### Rebuilding Assignments Safely

To start a new assignment round:

1. Prepare the new `videos.csv` and review its order and IDs.
2. Update annotators or overlap in `configuracao.json`, or use the administrator panel.
3. Save the configuration or restart the application so the assignment version is regenerated.
4. Review `atribuicoes.csv` and confirm its `assignment_version`, row count, and per-annotator distribution.
5. Keep previous results as historical data. Do not delete them unless the study protocol explicitly requires it.

For a major study round, keep an external snapshot of the input dataset, configuration, assignments, results, and errors together with the assignment version.

## Input File: `videos.csv`

The required columns are:

```csv
id,url,target
1234567890123456789,https://www.tiktok.com/@creator/video/1234567890123456789,example target
```

The following columns are optional:

```csv
id,url,target,video_description,voice_to_text,video_duration
1234567890123456789,https://www.tiktok.com/@creator/video/1234567890123456789,example target,Post description,Audio transcription,30
```

- `id`: required unique video identifier.
- `url`: required public TikTok URL used by the player.
- `target`: required topic, claim, or entity used as the annotation target.
- `video_description`: optional description or caption published by the creator.
- `voice_to_text`: optional audio transcription. It may contain recognition errors.
- `video_duration`: optional duration in seconds. It is used for analysis-time alerts when available.

If an optional column is absent, the interface displays an appropriate unavailable message and the rest of the annotation flow continues.

The player requires internet access in the annotator's browser. A video that does not load can be reported in the interface; that event is persisted and counts toward progress recovery.

## Sensitive and Generated Files

These files may contain research data, URLs, user identifiers, annotations, configuration, or access logs. They are local runtime files and are listed in `.gitignore`. Do not publish or share them without authorization.

### Input and Configuration

- `videos.csv`: sensitive input dataset. Template columns are `id,url,target`; optional feature columns are `video_description,voice_to_text,video_duration`. The application accepts additional columns for future adaptations, but only configured fields are displayed or persisted automatically.
- `configuracao.json`: JSON configuration template containing `admins`, `labelers`, `videos_per_labeler`, `overlap_percent`, and `tarefa`. `videos_per_labeler` is calculated from the active assignment set and should not be treated as the primary assignment control.

Example configuration:

```json
{
  "admins": ["admin"],
  "labelers": ["annotator-1", "annotator-2"],
  "videos_per_labeler": 0,
  "overlap_percent": 50.0,
  "tarefa": "Detecção de Posição"
}
```

### Generated Assignment and Annotation Files

- `atribuicoes.csv`: current assignment manifest. It contains `assignment_version`, `assignment_id`, `labeler`, `video_id`, `url`, `target`, optional feature fields, and `video_duration`. It is regenerated from the current dataset and configuration.
- `resultados_anotacao.csv`: normal annotation records. Template columns are `timestamp,labeler,video_id,url,target,stance,tempo_analise_segundos,video_duration,assignment_id`. The application migrates legacy rows when possible.
- `erros_videos.csv`: unavailable-video records. Template columns are `timestamp,video_id,url,labeler,assignment_id`. Legacy four-column rows are also read for compatibility.
- `acessos_log.csv`: access and timing events with `timestamp,usuario,acao,tempo_desde_login_segundos`.
- `concordancia.json`: generated report containing the agreement metric, comparable videos, discordant videos, and annotators with the highest disagreement.

## Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 src/app.py
```

Open the URL printed by Gradio. The default port is `7860`. If the port is already in use, set `GRADIO_SERVER_PORT` or change `server_port` in `src/app.py`.

## Tests

The repository separates tests by scope:

```text
tests/
├── unit/        # assignment math and versioning
├── integration/ # persistence and progress recovery
└── system/      # application/interface contracts
```

### Unit tests

Unit tests in `tests/unit/` validate deterministic assignment logic without opening the web interface. They verify:

- all videos are covered;
- overlap produces the expected number of shared assignments;
- workload is balanced between annotators;
- a changed dataset creates a new `assignment_version` and new assignment IDs.

Run only unit tests with:

```bash
pytest -q tests/unit
```

### Integration tests

Integration tests in `tests/integration/` validate behavior across assignment generation and persistence data. They verify:

- results from an old assignment version do not complete a current assignment;
- a normal annotation resumes at the first current pending assignment;
- an unavailable-video record counts as completed for the current assignment.

Run only integration tests with:

```bash
pytest -q tests/integration
```

### System tests

System tests in `tests/system/` validate the application-facing contract without requiring an external TikTok login or a live production server. They verify that:

- the labeler queue can be constructed from the configured dataset;
- the video HTML uses the official TikTok player endpoint;
- description, transcription, assignment version, progress, and target question are exposed to the interface.

Run only system tests with:

```bash
pytest -q tests/system
```

### Full suite

Run compilation and all tests with:

```bash
python3 -m py_compile src/app.py
pytest -q
```

`pytest.ini` adds the repository root to the Python path and restricts discovery to `tests/`.

### Continuous integration

`.github/workflows/ci.yml` runs automatically on every `push` and `pull_request`. The workflow tests Python `3.10`, `3.11`, and `3.12`, installs `requirements.txt`, compiles `src/app.py`, and runs the full pytest suite. A commit or pull request is considered healthy only when all matrix jobs pass.

## Deployment

The framework can be deployed to any hosting service compatible with Python and Gradio, including managed application platforms, containers, virtual machines, or institutional infrastructure. The hosting choice is intentionally left to the project owner.

Regardless of the provider:

1. Install the dependencies in `requirements.txt`.
2. Expose the application on the provider's host and port.
3. Provide `videos.csv` and `configuracao.json` through protected deployment storage.
4. Use persistent storage for generated CSV and JSON files.
5. Restrict administrator downloads and protect the annotation data.
6. Configure HTTPS, authentication, backups, monitoring, and retention according to the research protocol.

For long-running or concurrent production use, replace local CSV/JSON persistence with a transactional database and object storage, or implement a provider-specific persistence adapter.

## Project Structure

```text
.
├── src/
│   ├── __init__.py            # source package marker
│   └── app.py                 # Gradio application and annotation logic
├── requirements.txt          # Python dependencies
├── pytest.ini                # pytest import path and discovery configuration
├── README.md                 # Project documentation
├── .github/
│   └── workflows/
│       └── ci.yml            # GitHub Actions test workflow on push and PR
├── tests/
│   ├── unit/                 # assignment math and versioning tests
│   ├── integration/          # persistence and progress recovery tests
│   └── system/               # application/interface contract tests
├── videos.csv                # sensitive input, ignored by Git
├── configuracao.json         # sensitive configuration, ignored by Git
├── atribuicoes.csv           # generated assignment manifest, ignored by Git
├── resultados_anotacao.csv   # generated annotations, ignored by Git
├── erros_videos.csv          # generated unavailable-video records, ignored by Git
├── acessos_log.csv           # generated access logs, ignored by Git
└── concordancia.json         # generated agreement report, ignored by Git
```

## Security and Operations

- The framework is open source, but research data remains the responsibility of the study owner.
- `.gitignore` prevents new local data files from being added to Git. It does not erase sensitive files from Git history.
- Remove already tracked sensitive files from the index deliberately with `git rm --cached <file>` after confirming the impact.
- Do not store credentials, real participant names, or un-anonymized research data in the repository.
- Keep external snapshots of each dataset and assignment version for reproducibility.
- Download results regularly and maintain protected backups according to the study protocol and applicable privacy requirements.
