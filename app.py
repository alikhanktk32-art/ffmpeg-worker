"""
FFmpeg Worker for the n8n AI Documentary Video Editing System.

n8n Cloud cannot run FFmpeg, so this small service does the actual editing:
n8n sends it a video URL + an edit decision list (from the Gemini analysis),
and gets back the finished MP4 as the HTTP response body.

Endpoints:
  GET  /health  -> {"status": "ok"}
  POST /edit    -> MP4 file (video/mp4)

POST /edit JSON body:
  {
    "video_url":    "https://.../source.mp4",          (required, direct download link)
    "edit_order":   [                                   (required, at least 1 segment)
      {"start": 0.0, "end": 4.2, "zoom": "slow-in", "transition": "cut"},
      ...
    ],
    "music_url":    "https://.../music.mp3" or null,    (optional)
    "music_volume": 0.10                                (optional, default 0.10)
  }

Segment fields:
  start / end   seconds in the SOURCE video
  zoom          "none" | "slow-in" (1.00 -> 1.15) | "slow-out" (1.15 -> 1.00)
  transition    how THIS segment joins the NEXT one: "cut" (hard join) or
                "fade" (0.5s crossfade). Ignored on the last segment.

Editing approach (kept deliberately simple + robust):
  1. Each segment is rendered on its own (input-seek + re-encode) with the
     trim and zoom applied, normalised to the source resolution, 30 fps,
     yuv420p, 48 kHz stereo AAC.
     Zoom is done with zoompan on a 2x upscale (which halves its rounding
     steps and keeps the motion smooth), then scaled back to full size.
     (An animated crop cannot do this: FFmpeg evaluates crop's w/h only
     once, at filter init -- verified the hard way.)
  2. Consecutive segments joined by "cut" are concatenated losslessly.
     Groups joined by "fade" are then crossfaded pairwise with xfade
     (video) + acrossfade (audio), 0.5s each.
  3. Optional music is looped, lowered to music_volume and mixed UNDER the
     original audio (original stays primary, mix length = video length).
  4. Final file: MP4, libx264, AAC, yuv420p, faststart.
"""

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

FADE_SECONDS = 0.5
ZOOM_MAX = 1.15
TARGET_FPS = 30
FFMPEG_TIMEOUT = 900

app = FastAPI(title="Documentary FFmpeg Worker")


# ---------------------------------------------------------------- helpers

def run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a command, raising a clean HTTP 500 with the tool's own words on failure."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise HTTPException(status_code=500, detail="ffmpeg failed: " + " | ".join(tail))
    return proc


def probe(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail=f"ffprobe could not read a video file: {proc.stderr[-300:]}")
    return json.loads(proc.stdout)


def duration_of(path: Path) -> float:
    info = probe(path)
    return float(info["format"]["duration"])


def download(url: str, dest: Path) -> None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ffmpeg-worker/1.0"})
        with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    except Exception as exc:  # noqa: BLE001 - surface any download problem as a 400
        raise HTTPException(status_code=400, detail=f"Could not download {url!r}: {exc}")
    if dest.stat().st_size == 0:
        raise HTTPException(status_code=400, detail=f"Downloaded file is empty: {url!r}")


def zoom_filter(zoom: str, seg_dur: float, width: int, height: int) -> str:
    """Video filter chain for one segment: optional smooth zoom, then
    normalise size / fps / pixel format / timebase.

    Zoom uses zoompan on a 2x upscale (halves zoompan's rounding steps, so the
    motion is smooth), one output frame per input frame, stepping the zoom by
    (ZOOM_MAX-1)/total_frames each frame. NOTE: an animated `crop` cannot be
    used for this -- FFmpeg evaluates crop's w/h only once at filter init.
    """
    chain = ["setpts=PTS-STARTPTS", f"fps={TARGET_FPS}"]
    if zoom in ("slow-in", "slow-out"):
        frames = max(int(round(seg_dur * TARGET_FPS)), 1)
        step = (ZOOM_MAX - 1) / frames
        if zoom == "slow-in":
            z = f"if(eq(on,0),1,min(zoom+{step:.6f},{ZOOM_MAX}))"
        else:
            z = f"if(eq(on,0),{ZOOM_MAX},max(zoom-{step:.6f},1))"
        big_w, big_h = width * 2, height * 2
        chain += [
            f"scale={big_w}:{big_h}:flags=lanczos",
            f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={big_w}x{big_h}:fps={TARGET_FPS}",
        ]
    chain += [
        f"scale={width}:{height}:flags=lanczos",
        "setsar=1",
        "settb=AVTB",
        "format=yuv420p",
    ]
    return ",".join(chain)


def render_segment(src: Path, seg: dict, has_audio: bool,
                   width: int, height: int, out: Path) -> None:
    start, end = float(seg["start"]), float(seg["end"])
    dur = end - start
    vchain = zoom_filter(str(seg.get("zoom", "none")), dur, width, height)
    cmd = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src)]
    if has_audio:
        filt = f"[0:v]{vchain}[v];[0:a]aresample=48000,aformat=channel_layouts=stereo[a]"
    else:
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        filt = f"[0:v]{vchain}[v];[1:a]asetpts=N/SR/TB[a]"
    cmd += ["-filter_complex", filt, "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "160k", str(out)]
    run(cmd)


def concat_copy(parts: list[Path], out: Path, workdir: Path) -> None:
    """Lossless join of identically-encoded segments (the 'cut' joins)."""
    if len(parts) == 1:
        shutil.copyfile(parts[0], out)
        return
    listfile = workdir / "concat.txt"
    listfile.write_text("".join(f"file '{p.name}'\n" for p in parts))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c", "copy", str(out)])


def crossfade_blocks(blocks: list[Path], out: Path) -> None:
    """Join blocks with 0.5s xfade (video) + acrossfade (audio), pairwise."""
    if len(blocks) == 1:
        shutil.copyfile(blocks[0], out)
        return
    durations = [duration_of(b) for b in blocks]
    cmd = ["ffmpeg", "-y"]
    for b in blocks:
        cmd += ["-i", str(b)]
    filters, v_label, a_label = [], "0:v", "0:a"
    elapsed = durations[0]
    for i in range(1, len(blocks)):
        offset = max(elapsed - FADE_SECONDS, 0)
        nv, na = f"vx{i}", f"ax{i}"
        filters.append(
            f"[{v_label}][{i}:v]xfade=transition=fade:duration={FADE_SECONDS}:offset={offset:.3f}[{nv}]")
        filters.append(f"[{a_label}][{i}:a]acrossfade=d={FADE_SECONDS}[{na}]")
        v_label, a_label = nv, na
        elapsed = elapsed + durations[i] - FADE_SECONDS
    # xfade's output negotiates up to yuv444p on its own; lock the final
    # video back to yuv420p (required for maximum player compatibility).
    filters.append(f"[{v_label}]format=yuv420p[vfinal]")
    v_label = "vfinal"
    cmd += ["-filter_complex", ";".join(filters),
            "-map", f"[{v_label}]", "-map", f"[{a_label}]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "160k", str(out)]
    run(cmd)


def add_music(video: Path, music: Path, volume: float, out: Path) -> None:
    filt = (f"[0:a]volume=1.0[a0];[1:a]volume={volume:.3f}[a1];"
            f"[a0][a1]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
    run(["ffmpeg", "-y", "-i", str(video), "-stream_loop", "-1", "-i", str(music),
         "-filter_complex", filt, "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart", str(out)])


def faststart_copy(video: Path, out: Path) -> None:
    run(["ffmpeg", "-y", "-i", str(video), "-c", "copy",
         "-movflags", "+faststart", str(out)])


# ---------------------------------------------------------------- endpoints

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/edit")
async def edit(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON.")

    video_url = body.get("video_url")
    edit_order = body.get("edit_order")
    music_url = body.get("music_url") or None
    try:
        music_volume = float(body.get("music_volume", 0.10))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="music_volume must be a number (e.g. 0.10).")

    if not video_url or not isinstance(video_url, str):
        raise HTTPException(status_code=400, detail="video_url is required (direct download URL of the source MP4).")
    if not edit_order or not isinstance(edit_order, list):
        raise HTTPException(status_code=400, detail="edit_order is required (non-empty list of segments).")
    for i, seg in enumerate(edit_order):
        try:
            s, e = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"edit_order[{i}] needs numeric start and end.")
        if s < 0 or e <= s:
            raise HTTPException(status_code=400, detail=f"edit_order[{i}]: end must be greater than start (got {s}..{e}).")
        if seg.get("zoom", "none") not in ("none", "slow-in", "slow-out"):
            raise HTTPException(status_code=400, detail=f"edit_order[{i}]: zoom must be none | slow-in | slow-out.")

    workdir = Path(tempfile.mkdtemp(prefix="ffworker-"))
    try:
        src = workdir / "source.mp4"
        download(video_url, src)
        info = probe(src)
        vstream = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
        if vstream is None:
            raise HTTPException(status_code=400, detail="Downloaded file has no video stream.")
        width = int(vstream.get("width") or 1280)
        height = int(vstream.get("height") or 720)
        has_audio = any(s["codec_type"] == "audio" for s in info["streams"])

        # 1) render every segment on its own
        seg_files = []
        for i, seg in enumerate(edit_order):
            seg_out = workdir / f"seg_{i:03d}.mp4"
            render_segment(src, seg, has_audio, width, height, seg_out)
            seg_files.append(seg_out)

        # 2) group consecutive 'cut' joins, concat each group, crossfade groups
        groups: list[list[Path]] = [[seg_files[0]]]
        for i in range(1, len(seg_files)):
            prev_transition = str(edit_order[i - 1].get("transition", "cut"))
            if prev_transition == "fade":
                groups.append([seg_files[i]])
            else:
                groups[-1].append(seg_files[i])
        blocks = []
        for g, group in enumerate(groups):
            block = workdir / f"block_{g:02d}.mp4"
            concat_copy(group, block, workdir)
            blocks.append(block)
        joined = workdir / "joined.mp4"
        crossfade_blocks(blocks, joined)

        # 3) optional music bed under the original audio
        final = workdir / "final.mp4"
        if music_url:
            music = workdir / "music.bin"
            download(music_url, music)
            add_music(joined, music, music_volume, final)
        else:
            faststart_copy(joined, final)

        return FileResponse(
            str(final), media_type="video/mp4", filename="edited.mp4",
            background=BackgroundTask(shutil.rmtree, workdir, ignore_errors=True),
        )
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except subprocess.TimeoutExpired:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="ffmpeg timed out while editing.")
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")


@app.exception_handler(HTTPException)
async def http_error_handler(_, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
