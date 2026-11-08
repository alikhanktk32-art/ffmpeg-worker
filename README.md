# FFmpeg Worker — n8n AI Documentary Video Editing System

n8n Cloud cannot run FFmpeg (there is no Execute Command node), so this small
web service does the actual cutting/rendering. n8n calls it over HTTP once the
Gemini analysis + parse nodes have produced the edit decision list.

The service:

1. Downloads the source MP4 from `video_url`.
2. Renders each `edit_order` segment on its own (trim + optional slow zoom),
   normalised to the source resolution, 30 fps, yuv420p, 48 kHz stereo AAC.
3. Joins the segments: `"cut"` = hard join (lossless concat), `"fade"` = 0.5s
   crossfade (xfade for video + acrossfade for audio).
4. Optionally mixes background music under the original audio at
   `music_volume` (default `0.10`); the original audio stays primary.
5. Returns the finished MP4 (libx264, AAC, yuv420p, faststart) as the
   response body.

Zoom behaviour: `slow-in` = gradual zoom 1.00 → 1.15 across the segment,
`slow-out` = 1.15 → 1.00, `none` = no zoom. Implemented with zoompan on a 2×
upscale (which halves zoompan's rounding steps, keeping the motion smooth),
then scaled back to full size. (An animated `crop` can't do this — FFmpeg
evaluates crop's width/height only once, at filter init.)

It is synchronous and deliberately simple: demo-scale files (< 100 MB) are
edited inside the request and returned directly. No database, no queue.

---

## API contract

### `GET /health`

```json
{ "status": "ok" }
```

### `POST /edit`

Request — `Content-Type: application/json`:

```json
{
  "video_url": "https://drive.google.com/uc?export=download&id=<FILE_ID>",
  "edit_order": [
    { "start": 0.0,  "end": 4.2,  "zoom": "slow-in",  "transition": "cut"  },
    { "start": 9.8,  "end": 14.4, "zoom": "none",     "transition": "fade" },
    { "start": 19.9, "end": 24.0, "zoom": "slow-out", "transition": "cut"  }
  ],
  "music_url": null,
  "music_volume": 0.10
}
```

| Field          | Required | Meaning |
|----------------|----------|---------|
| `video_url`    | yes      | Direct download URL of the source MP4. (For Drive: the file must be shared "Anyone with the link — Viewer"; use the `uc?export=download&id=...` form.) |
| `edit_order`   | yes      | Non-empty list of segments, in final story order. |
| `start`, `end` | yes      | Seconds in the source video; `end > start`. |
| `zoom`         | no       | `none` (default) \| `slow-in` \| `slow-out`. |
| `transition`   | no       | How this segment joins the **next** one: `cut` (default) \| `fade` (0.5s). Ignored on the last segment. |
| `music_url`    | no       | Direct URL of a music file (mp3/wav/...). Looped if shorter than the video. `null` = no music. |
| `music_volume` | no       | Music level under the narration, default `0.10`. |

Response:

- **200** — body is the finished video, `Content-Type: video/mp4`.
  Final duration ≈ sum of segment durations − 0.5s per `fade` join.
- **400** — bad request (missing/invalid fields, URL could not be downloaded,
  file has no video stream). Body: `{"error": "..."}`.
- **500** — FFmpeg failed or timed out. Body: `{"error": "..."}` with the
  last lines of FFmpeg's own output.

### How n8n calls it

HTTP Request node → Method `POST`, URL `https://<your-service>.onrender.com/edit`,
Body Content Type `JSON`, JSON body built from the parsed analysis, e.g.:

```javascript
{{ JSON.stringify({
     video_url: 'https://drive.google.com/uc?export=download&id=' + $('Upload file').first().json.id,
     edit_order: $json.edit_order,
     music_url: null,
     music_volume: 0.10
   }) }}
```

Set the node's **Response Format** to **File** so the returned MP4 arrives as
binary data, then save it with a Google Drive "Upload file" node.

---

## Run locally

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
curl http://localhost:8000/health
```

(FFmpeg and FFprobe must be installed and on the PATH.)

---

## Deploy free on Render (Docker web service)

Goal: a public URL like `https://documentary-ffmpeg-worker.onrender.com`,
so n8n calls `https://documentary-ffmpeg-worker.onrender.com/edit`.

1. Put this folder (`ffmpeg-worker/` with `app.py`, `requirements.txt`,
   `Dockerfile`) in a GitHub repo — it can be the repo root, or a subfolder.
2. Go to **render.com** → sign up / log in (free account is fine).
3. Click **New +** → **Web Service**.
4. Choose **Build and deploy from a Git repository** → connect the repo.
5. Settings:
   - **Name:** `documentary-ffmpeg-worker` (this becomes the subdomain)
   - **Language / Runtime:** **Docker**
   - **Root Directory:** the folder containing the `Dockerfile`
     (leave empty if the Dockerfile is at the repo root)
   - **Instance Type:** **Free**
6. Click **Create Web Service**. The first build takes a few minutes
   (it installs Python + FFmpeg from the Dockerfile).
7. When the deploy says **Live**, open
   `https://documentary-ffmpeg-worker.onrender.com/health`
   — you should see `{"status":"ok"}`.
8. Use `https://documentary-ffmpeg-worker.onrender.com/edit` as the URL in
   the n8n HTTP Request node.

Free-tier notes (honest limits):

- The free instance **sleeps after ~15 minutes idle**; the first request
  after that takes ~30–60s to wake up. For a demo this is fine — if n8n
  times out on the very first call, just run it again.
- Free instances have limited CPU/RAM: keep source videos short/small
  (demo scale, < 100 MB) and expect rendering to take longer than locally.
- Nothing is stored on the service — every request downloads, edits and
  returns the video, then deletes its temp files.

---

## Simplifications / fallbacks (by design)

- **Fade placement:** fades are applied between *blocks* of cut-joined
  segments. A `fade` on segment *i* crossfades the join between segment *i*
  and *i+1*; consecutive `cut` joins inside a block are lossless.
- **Fade length** is fixed at 0.5s (not per-segment configurable).
- **Zoom** maxes at 1.15× and is centre-framed (no pan/focal-point control).
- Synchronous, single-request editing — no job queue or progress polling.
- If the source has **no audio track**, silent audio is generated so the
  output always has a consistent AAC track.
