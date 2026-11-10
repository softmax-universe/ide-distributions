"""Run in Hermes's interpreter: stage files using its public attachment RPC.

The input is an OpenAI user message. The output is ``{message, outcomes}``: the changed message,
and for each file its type, original size and what was done to it, with no file name or text. Only
``message`` goes on to the model. Hermes owns file names, storage, PDF rendering and document
annotations; the profile supplies no file parser.
"""

import base64
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path


def main() -> dict:
    from gateway.platforms.base import CachedMedia
    from tui_gateway import server

    request = json.load(sys.stdin)
    workspace = Path(request["workspace"])
    workspace.mkdir(parents=True, exist_ok=True)

    def call(method, params):
        response = server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["result"]

    session = call("session.create", {"cwd": str(workspace)})["session_id"]
    content = []
    outcomes = []
    try:
        for part in request["message"]["content"]:
            if part["type"] not in {"file", "input_audio"}:
                content.append(part)
                continue
            file = (
                part["file"]
                if part["type"] == "file"
                else {
                    "filename": "audio." + part["input_audio"]["format"],
                    "file_data": "data:audio/"
                    + part["input_audio"]["format"]
                    + ";base64,"
                    + part["input_audio"]["data"],
                }
            )
            staged = call(
                "file.attach",
                {
                    "session_id": session,
                    "data_url": file["file_data"],
                    "name": file["filename"],
                },
            )
            path = Path(staged["path"])
            outcome = {
                "type": "document",
                "change": "staged",
                "size_bytes": path.stat().st_size,
                "details": {},
            }
            outcomes.append(outcome)
            content.append(
                {
                    "type": "text",
                    "text": CachedMedia(
                        str(path), "application/octet-stream", "document", staged["name"]
                    ).context_note(),
                }
            )
            if path.suffix.lower() == ".pdf":
                pdf = call("pdf.attach", {"session_id": session, "path": str(path)})
                outcome.update(type="pdf", change="rendered", details={"pages": len(pdf["pages"])})
                for page in pdf["pages"]:
                    data = base64.b64encode(Path(page["path"]).read_bytes()).decode()
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64," + data,
                            },
                        }
                    )
            elif path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                call("image.attach", {"session_id": session, "path": str(path)})
                outcome.update(type="image", change="inlined")
                mime = (
                    "image/jpeg"
                    if path.suffix.lower() in {".jpg", ".jpeg"}
                    else ("image/" + path.suffix[1:].lower())
                )
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:"
                            + mime
                            + ";base64,"
                            + base64.b64encode(path.read_bytes()).decode(),
                        },
                    }
                )
            elif path.suffix.lower() in {
                ".wav",
                ".mp3",
                ".m4a",
                ".ogg",
                ".opus",
                ".m2a",
                ".flac",
                ".webm",
            }:
                from tools.transcription_tools import transcribe_audio

                transcript = transcribe_audio(str(path))
                if not transcript["success"]:
                    raise RuntimeError(transcript)
                content.append({"type": "text", "text": transcript["transcript"]})
                outcome.update(
                    type="audio",
                    change="transcribed",
                    details={"transcript_bytes": len(transcript["transcript"].encode("utf-8"))},
                )
    finally:
        call("session.close", {"session_id": session})
    message = {**request["message"], "content": content}
    return {"message": message, "outcomes": outcomes}


if __name__ == "__main__":
    # Native RPC notifications and tool progress belong on stderr, not the JSON result pipe.
    with redirect_stdout(sys.stderr):
        result = main()
    print(json.dumps(result), flush=True)
    sys.stderr.flush()
    # The one-shot worker has closed its RPC session. Hermes's process-wide shutdown
    # can wait on gateway threads that outlive that session.
    os._exit(0)
