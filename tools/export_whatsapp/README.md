# export_whatsapp.py

Export WhatsApp chats from a local, unencrypted iPhone backup to HTML, plain text and JSONL.

It is one Python file with no dependencies. It finds the newest backup by itself and only reads from it.

This tool is part of [WhatsApp Archive](../../README.md). Use the full app if you also need voice transcription and search.

> The interface and the message labels in the output are currently in Russian.

## Quick start

1. Back up your iPhone to your computer with Apple Devices, iTunes or Finder. Encryption must be off.
2. Run:

```bash
python export_whatsapp.py
```

3. Open `output/index.html`.

Python 3.10 or newer is required. You can copy this one file anywhere and run it there.

## Options

| Option | What it does |
|---|---|
| `--backup DIR` | Use this backup folder instead of the newest one found |
| `--out DIR` | Output folder. Default: `./output`, or `./output_chat` with `--chat` |
| `--chat TEXT` | Export only chats whose name contains this text |
| `--media link` | Hard-link media files, using almost no extra disk space (default) |
| `--media copy` | Copy media files |
| `--media none` | Skip media files |

## Output

```
output/
  index.html     searchable list of chats
  html/          one page per chat, with media and quoted replies
  txt/           one plain text file per chat
  jsonl/         one JSON object per line, for your own processing
  media/         media files, one folder per chat
  summary.json   export counters
```

Times are shown in the time zone of the computer running the export.

## Privacy

- Everything runs locally. The tool makes no network requests.
- The output contains your private chats and other people's messages. Keep it out of git and cloud folders you share. `output*/` is already in this repository's `.gitignore`.
- Only export backups you have the right to access.
- **With `--media link`, do not edit files in `output/media`.** They are the same files as in the backup, so an edit changes the backup too. Deleting them is safe.

## Known limitations

WhatsApp does not document this database. Message types were identified by inspecting one real backup (iOS 26.6.1, about 700,000 messages), so other versions may differ.

| Item | Current behaviour |
|---|---|
| Encrypted backups | Not supported. The tool stops with an error |
| WhatsApp Business | Not supported |
| Reactions | Not exported |
| Poll questions and options | Not exported, shown as `[опрос]` |
| Forwarded marker | Not shown |
| Contact cards | Name only, without the phone number |
| Call duration | Not shown |
| Unknown message types | Shown as `[тип N]`, with any text or file kept |
| Empty service records | Hidden in HTML and TXT, kept in JSONL |
| One person in two chats | Possible because of WhatsApp's internal IDs. The chats are not merged |

How sure the labels are:

| Type | Label | Evidence |
|---|---|---|
| 59 | call | 207 of 248 records matched the call history within 2 minutes |
| 66 | album | One record compared with the phone: it was followed by the photos |
| 14 | deleted or edited | Unclear. The data suggests deletion, but one record looked like an edit on the phone |
| 12 | possibly a call | Unclear. One record looked like a call on the phone, but no record matched the call history |

## Similar projects

- [WhatsApp-Chat-Exporter](https://github.com/KnugiHK/WhatsApp-Chat-Exporter) is the established, much more capable tool. It supports Android, encrypted backups, WhatsApp Business, call logs and custom templates. Use it if you need any of these.
- [iphone_backup_decrypt](https://github.com/KnugiHK/iphone_backup_decrypt) decrypts encrypted iPhone backups.

This project is for the narrow case: one file, no installation, an unencrypted iPhone backup.

## Development

```bash
python tests/test_export.py
```

The tests build a synthetic backup and need no real data. `pytest` from the repository root runs them too.

**Never attach real chats, databases or backups to issues or pull requests.** To report a bug, describe the message type code and the counts, or add a synthetic example to the tests.

## Disclaimer

This is an independent project, not affiliated with or endorsed by WhatsApp, Meta or Apple.

## License

[MIT](../../LICENSE)
