# Sonoryx Photo Intelligence 2.1

Author: Dāvids Krūmiņš. Local photos remain unchanged.

## Features
- Atomic private SQLite metadata inventory, stable IDs, protected source scanning.
- Offline Tesseract OCR (Russian, Latvian, English), with SQLite FTS5 text index.
- Filename/folder search is combined with already-indexed OCR text. This is not visual semantic scene understanding.
- New Photo Intelligence MCP tools can also be accessed from the long-lived read_text tool.

## Virtual MCP paths
- gallery://health
- gallery://search?query=keyword&limit=20
- gallery://ocr/status
- gallery://ocr/index?ids=123&max_images=1&max_seconds=20
- gallery://ocr/index?max_images=8&max_seconds=48
- gallery://ocr/search?query=слово&limit=20
- gallery://ocr/text/123?max_chars=4000

No bulk OCR at low battery. A single explicitly selected photo may be processed.

## Optional scheduled maintenance
Requires Termux:API app. Register the persistent Android job:

    termux-job-scheduler --script "$HOME/termux-mcp-bridge/current/gallery_maintenance.py" --job-id 38039 --period-ms 14400000 --network none --charging true --battery-not-low true --storage-not-low true --persisted true

The job independently checks battery >=35%, charging state, and >=1 GiB disk free. Metadata updates occur at most once every six hours; OCR processes at most eight photos with a 48-second processing budget per run. All images stay local.

Check scheduler and status:

    termux-job-scheduler -p
    cat "$HOME/.termux-mcp-bridge/gallery_maintenance_status.json"

Cancel only the gallery maintenance task:

    termux-job-scheduler --cancel 38039

## OCR dependencies

    pkg install tesseract
    tesseract --list-langs

English, Russian and Latvian traineddata may be installed from tesseract-ocr/tessdata_fast under $PREFIX/share/tessdata.

## Security limits
This does not automatically read inaccessible app-private sandboxes or cloud photos.
It does not include biometric identification or image embeddings.
OCR text is private to the Termux UID but is not encrypted on disk (same as the legacy index).
Do not share MCP access with untrusted persons. It never modifies or uploads source photos.
