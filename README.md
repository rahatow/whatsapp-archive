# WhatsApp Archive

A local app for your own WhatsApp chats from an iPhone backup. One window, three steps:

1. **Export:** one button reads all chats and media from the backup.
2. **Transcribe:** voice messages and videos become text, on your GPU or through an API.
3. **RAG:** search and ask questions over the whole history, transcripts included, locally or through an API.

[Русская версия](README.ru.md)

> The app interface is currently in Russian.

Based on [WhatsApp-Chat-Exporter](https://github.com/KnugiHK/WhatsApp-Chat-Exporter) by Knugi (MIT). Its guide and all command-line options are in [README.upstream.md](README.upstream.md). The `wtsexporter` command from that project is still included.

## Quick start

You need Python 3.10–3.12. Transcription through an API also needs `ffmpeg`.

1. Back up your iPhone to your computer with Apple Devices, iTunes or Finder. Encryption must be off.
2. Create a virtual environment:

```bash
python -m venv .venv
```

3. Install the app (on macOS and Linux use `.venv/bin/` instead of `.venv\Scripts\`):

```bash
.venv\Scripts\pip install -e ".[app]"
```

4. Start it:

```bash
.venv\Scripts\whatsapp-archive
```

The app opens `http://127.0.0.1:8765`.

For local mode you need:
- an NVIDIA GPU for fast transcription. Without it, transcription runs on the CPU and is much slower;
- [Ollama](https://ollama.com) with the `bge-m3` model for the search index;
- any Ollama chat model for answers. Without it, only the «Только найти» (search only) button works.

For API mode, set the `OPENAI_API_KEY` environment variable and restart the app. Any OpenAI-compatible service works: change the address and model names in the settings. The app reads the key only from the environment and never writes it to disk.

## Stop and resume

You can stop any step with the stop button, by closing the window, or even by a power cut. Finished work stays, and the button changes to «Продолжить» (continue).

| Step | What is kept | What runs again |
|---|---|---|
| Export | Each of the 4 stages, finished files and chat pages | The stage that reads messages or loads them into the archive, if it was interrupted |
| Transcription | Each finished file | Only the file that was in progress |
| Index | Each batch of 32 chunks | Only the current batch. Chunks are rebuilt, finished vectors stay |

If you choose another or a newer backup, the export starts again, but finished transcripts stay.

## Without installation: `tools/export_whatsapp`

[`tools/export_whatsapp/export_whatsapp.py`](tools/export_whatsapp/) is one Python file with no dependencies. It exports chats to HTML, plain text and JSONL, with no app, transcription or search. Use it when you only need the chats as files.

```bash
python tools/export_whatsapp/export_whatsapp.py
```

## Privacy

- All data is in the `data/` folder, which is excluded from git. Do not put it in shared cloud folders: it holds other people's private messages.
- The app listens only on `127.0.0.1` and rejects requests from other websites.
- In local mode, data does not leave your computer. In API mode, the app shows what will be sent and where, and sends only after you confirm.
- With hard links on, files in `data/` are the same files as in the backup. Do not edit them. Deleting them is safe.
- Only export backups you have the right to access.

## Settings by environment variable

| Variable | What it does |
|---|---|
| `WA_ARCHIVE_DATA` | Data folder. Default: `./data` |
| `WA_MEDIA_FALLBACK` | Optional second copy of the backup media tree, for example on a NAS. The app reads a file from there when it is missing in `data/`. Several folders: separate with `;` |
| `OPENAI_API_KEY` | Key for API mode. The variable name can be changed in the settings |

## Changes compared with WhatsApp-Chat-Exporter

- An app with a UI, transcription and RAG (the `whatsapp_archive` package).
- `--link-media` flag: hard links instead of copying media.
- `--resume-html` flag, and pages are written through a temporary file: an interrupted export leaves no cut-off pages.
- Replies to messages with an id of any length, not only 20 characters.
- Names of group members known only by an internal `@lid`.
- Groups with the same name no longer overwrite each other's pages.

## Limitations

- Encrypted backups and WhatsApp Business are not supported in the app. The `wtsexporter` command supports them.
- Reactions are not exported.
- API transcription accepts files up to 25 MB after compression, about 100 minutes of audio.
- API modes are tested against a fake service, not a real one.

## Tests

```bash
.venv\Scripts\python -m pytest
```

The tests use only made-up data and make no network requests. On Windows, set the environment variable `PYTHONUTF8=1` and put `.venv\Scripts` in `PATH`.

**Never attach real chats, databases or backups to issues or pull requests.**

## Disclaimer

This is an independent project, not affiliated with or endorsed by WhatsApp, Meta or Apple.

## License

[MIT](LICENSE)
