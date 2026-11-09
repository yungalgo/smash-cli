"""smash-cli: run Kentskooking's MiniMax H3 "context smashing" workflow on a RunPod GPU.

Commands: up, status, run, down. See README.md.
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
WORKFLOW = ROOT / "vendor/kentskooking/MiniMax_H3_Concentric_RedGreen_Canvas_8step.json"
DEFAULT_MASK = ROOT / "vendor/kentskooking/Concentric_Center_Circle_RedGreen_150f.mp4"
BOOT = ROOT / "pod/boot.sh"
STATE = Path.home() / ".smash-cli/pod.json"

RUNPOD_API = "https://rest.runpod.io/v1"
IMAGE = "runpod/pytorch:1.6.0-cu1281-torch2130-ubuntu2404"
# 96 GB Blackwell cards: the workflow's text encoder is NVFP4 (Blackwell only).
GPUS = [
    "NVIDIA RTX PRO 6000 Blackwell Server Edition",
    "NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
]
FPS = 24

# Node ids in Kent's workflow (vendor/kentskooking/…Canvas_8step.json).
N_RED_PROMPT, N_GREEN_PROMPT, N_CANVAS_PROMPT = "4", "216", "212"
N_LENGTH, N_WIDTH, N_HEIGHT = "5", "200", "201"
N_SEED, N_MASK_VIDEO, N_CANVAS_STRENGTH = "15", "51", "214"
N_RED_IMAGE, N_GREEN_IMAGE, N_CANVAS_IMAGE = "110", "140", "211"
N_SIGMA_SHIFT = "8"
# Optional speed/preview patches (SageAttention, tiny-VAE preview). Kent's
# example README says to bypass both by wiring Sigma Shift straight through.
BYPASS_TO_SIGMA_SHIFT = {"9", "10"}
# "Hold last mask frame" node from comfyui_essentials; we pad the mask with
# ffmpeg instead, so the loader feeds the guider directly.
N_MASK_EXPAND = "209"
# Kent's CreateVideo/SaveVideo widgets differ between ComfyUI versions; we save
# the decoded video (20) and audio (21) with VideoHelperSuite instead.
N_VIDEO_DECODE, N_AUDIO_DECODE, OUTPUT_NODES = "20", "21", {"22", "23"}
SKIP_TYPES = {"MarkdownNote", "Note"}


def die(msg):
    sys.exit(f"smash-cli: {msg}")


def env(name):
    value = os.environ.get(name)
    if not value:
        die(f"{name} is not set")
    return value


# ---------------------------------------------------------------- RunPod pod


def runpod(method, path, **kw):
    r = requests.request(
        method, RUNPOD_API + path, timeout=60,
        headers={"Authorization": f"Bearer {env('RUNPOD_API_KEY')}"}, **kw,
    )
    if r.status_code >= 400:
        die(f"RunPod {method} {path}: {r.status_code} {r.text[:500]}")
    return r.json() if r.text else None


def load_state():
    if not STATE.exists():
        die("no pod; run `smash-cli up` first")
    return json.loads(STATE.read_text())


def base_url(pod_id):
    return f"https://{pod_id}-8188.proxy.runpod.net"


def cmd_up(a):
    if STATE.exists():
        die(f"a pod is already recorded in {STATE}; run `smash-cli down` first")
    boot = base64.b64encode(BOOT.read_bytes()).decode()
    body = {
        "name": "smash-cli",
        "imageName": IMAGE,
        "gpuTypeIds": a.gpu or GPUS,
        "gpuTypePriority": "custom",
        "cloudType": a.cloud,
        "gpuCount": 1,
        "containerDiskInGb": 120,
        "ports": ["8188/http"],
        "env": {"SMASH_BOOT": boot, "SMASH_IDLE_MINUTES": str(a.idle_minutes)},
        "dockerStartCmd": ["bash", "-c", 'echo "$SMASH_BOOT" | base64 -d > /boot.sh && bash /boot.sh'],
    }
    pod = runpod("POST", "/pods", json=body)
    pod = pod[0] if isinstance(pod, list) else pod
    STATE.parent.mkdir(parents=True, exist_ok=True)
    state = {"id": pod["id"], "created": time.time(), "costPerHr": pod.get("costPerHr")}
    STATE.write_text(json.dumps(state))
    gpu = (pod.get("machine") or {}).get("gpuTypeId")
    print(f"pod {pod['id']} on {gpu} ({a.cloud}), ${pod.get('costPerHr')}/h")
    print(f"auto-delete after {a.idle_minutes} idle minutes; `smash-cli down` deletes it now")
    print("installing ComfyUI and downloading ~42 GB of weights; waiting for ComfyUI …")
    url = base_url(pod["id"])
    start = time.time()
    while True:
        try:
            if requests.get(url + "/system_stats", timeout=10).status_code == 200:
                break
        except requests.RequestException:
            pass
        if time.time() - start > a.timeout * 60:
            die(f"ComfyUI not ready after {a.timeout} min; pod is still running (`smash-cli down`)")
        time.sleep(20)
    print(f"ready after {(time.time() - start) / 60:.1f} min: {url}")


def cmd_status(a):
    s = load_state()
    pod = runpod("GET", f"/pods/{s['id']}")
    hours = (time.time() - s["created"]) / 3600
    cost = (s["costPerHr"] or 0) * hours
    print(f"pod {s['id']}: {pod.get('desiredStatus')}, up {hours * 60:.0f} min, ~${cost:.2f} so far")


def cmd_down(a):
    s = load_state()
    r = requests.delete(
        f"{RUNPOD_API}/pods/{s['id']}", timeout=60,
        headers={"Authorization": f"Bearer {env('RUNPOD_API_KEY')}"},
    )
    if r.status_code not in (200, 204, 404):
        die(f"delete failed: {r.status_code} {r.text[:300]}")
    STATE.unlink()
    print(f"pod {s['id']} deleted" if r.status_code != 404 else f"pod {s['id']} was already gone")


# ---------------------------------------------------------------- inputs


def run(*args):
    p = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        die(f"{args[0]} failed: {p.stderr.strip()[-1500:]}")


def fetch_source(src, work, name, at, cookies=None):
    """Local file and seek time for a path, a URL (yt-dlp), or `gen:<prompt>` (fal image)."""
    if src.startswith("gen:"):
        return fal_image(src[4:], work / f"{name}-gen"), 0
    if src.startswith(("http://", "https://")):
        # Only the few seconds around the frame we need, not the whole video.
        out = work / f"{name}-dl"
        auth = ["--cookies", str(Path(cookies).expanduser())] if cookies else []
        p = subprocess.run(
            ["yt-dlp", "-q", "--no-warnings", "--no-playlist", *auth, "-f", "bv*[height<=1080]/b",
             "--download-sections", f"*{at}-{at + 2}", "-o", f"{out}.%(ext)s", src],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        files = list(work.glob(f"{name}-dl.*"))
        if p.returncode != 0 or len(files) != 1:
            reason = p.stderr.strip().splitlines()[-1] if p.stderr.strip() else f"{len(files)} files written"
            die(
                f"could not download --{name} {src}\n  yt-dlp: {reason}\n"
                "  Fix: download the video yourself (browser or `yt-dlp <url>`) and pass the file path,\n"
                "  or export YouTube cookies to cookies.txt and add --cookies cookies.txt."
            )
        return files[0], 0
    path = Path(src).expanduser()
    if not path.exists():
        die(f"no such file: {src}")
    return path, at


def fal_image(prompt, out):
    key = env("FAL_KEY")
    model = os.environ.get("SMASH_IMAGE_MODEL", "alibaba/qwen-image-3/text-to-image")
    r = requests.post(
        f"https://fal.run/{model}", json={"prompt": prompt}, timeout=300,
        headers={"Authorization": f"Key {key}"},
    )
    if r.status_code >= 400:
        die(f"fal {model}: {r.status_code} {r.text[:300]}")
    url = r.json()["images"][0]["url"]
    data = requests.get(url, timeout=120)
    data.raise_for_status()
    path = out.with_suffix(".img")
    path.write_bytes(data.content)
    return path


def is_video(path):
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=nb_frames,duration",
         "-of", "json", str(path)], capture_output=True, text=True, check=True,
    )
    s = (json.loads(p.stdout).get("streams") or [{}])[0]
    return float(s.get("duration") or 0) > 0.1


def square_frame(src, out, size, at):
    """One center-cropped size x size PNG from an image or a video frame at `at` s."""
    crop = f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size}"
    seek = ["-ss", str(at)] if is_video(src) else []
    run("ffmpeg", "-y", "-v", "error", *seek, "-i", str(src), "-frames:v", "1", "-vf", crop, str(out))
    return out


def fit_mask(src, out, size, frames):
    """Mask video at size x size, FPS, exactly `frames` frames (last frame held)."""
    vf = f"fps={FPS},scale={size}:{size}:flags=neighbor,tpad=stop_mode=clone:stop={frames}"
    run("ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", vf, "-frames:v", str(frames),
        "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv444p", str(out))
    return out


# ---------------------------------------------------------------- workflow


def widget_slots(info):
    """Input names in widgets_values order; None marks frontend-only extra values."""
    slots = []
    for section in ("required", "optional"):
        for name, spec in info["input"].get(section, {}).items():
            kind = spec[0]
            opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            if opts.get("forceInput"):
                continue
            if not (isinstance(kind, list) or kind in ("INT", "FLOAT", "STRING", "BOOLEAN", "COMBO")):
                continue
            slots.append(name)
            if opts.get("control_after_generate") or (kind == "INT" and name in ("seed", "noise_seed")):
                slots.append(None)
            if opts.get("image_upload") or opts.get("video_upload"):
                slots.append(None)
    return slots


def to_api(ui, object_info):
    """Convert Kent's UI-format workflow to ComfyUI's API (prompt) format."""
    links = {l[0]: l for l in ui["links"]}
    dropped = BYPASS_TO_SIGMA_SHIFT | {N_MASK_EXPAND} | OUTPUT_NODES
    reroute = {n: (N_SIGMA_SHIFT, 0) for n in BYPASS_TO_SIGMA_SHIFT}
    reroute[N_MASK_EXPAND] = (N_MASK_VIDEO, 0)
    api = {}
    for node in ui["nodes"]:
        nid = str(node["id"])
        if node["type"] in SKIP_TYPES or nid in dropped or node.get("mode", 0) != 0:
            continue
        info = object_info.get(node["type"])
        if info is None:
            die(f"pod ComfyUI has no node type {node['type']}")
        inputs = {}
        values = node.get("widgets_values")
        if isinstance(values, dict):
            names = set(widget_slots(info))
            inputs.update({k: v for k, v in values.items() if k in names})
        elif values:
            slots = widget_slots(info)
            if len(slots) != len(values):
                die(f"node {nid} {node['type']}: {len(values)} widget values, expected {slots}")
            inputs.update({k: v for k, v in zip(slots, values) if k is not None})
        for inp in node.get("inputs", []):
            if inp.get("link") is None:
                continue
            _, src, slot, *_ = links[inp["link"]]
            src, slot = reroute.get(str(src), (str(src), slot))
            inputs[inp["name"]] = [src, slot]
        api[nid] = {"class_type": node["type"], "inputs": inputs}
    api["smash_out"] = {"class_type": "VHS_VideoCombine", "inputs": {
        "images": [N_VIDEO_DECODE, 0], "audio": [N_AUDIO_DECODE, 0], "frame_rate": FPS,
        "loop_count": 0, "filename_prefix": "smash", "format": "video/h264-mp4",
        "pingpong": False, "save_output": True,
    }}
    return api


# ---------------------------------------------------------------- run


def comfy(url, method, path, **kw):
    r = requests.request(method, url + path, timeout=300, **kw)
    if r.status_code >= 400:
        die(f"ComfyUI {method} {path}: {r.status_code} {r.text[:2000]}")
    return r


def upload(url, path, name):
    with open(path, "rb") as f:
        comfy(url, "POST", "/upload/image", files={"image": (name, f)}, data={"overwrite": "true"})
    return name


def cmd_run(a):
    url = base_url(load_state()["id"])
    tag = uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        names = {}
        for role in ("canvas", "red", "green"):
            src, seek = fetch_source(getattr(a, role), work, role, a.frame_at, a.cookies)
            png = square_frame(src, work / f"{role}.png", a.size, seek)
            names[role] = upload(url, png, f"smash-{tag}-{role}.png")
            print(f"{role}: {getattr(a, role)}")
        mask = fit_mask(Path(a.mask).expanduser(), work / "mask.mp4", a.size, a.frames)
        mask_name = upload(url, mask, f"smash-{tag}-mask.mp4")
        object_info = comfy(url, "GET", "/object_info").json()
        api = to_api(json.loads(WORKFLOW.read_text()), object_info)

    api[N_CANVAS_IMAGE]["inputs"]["image"] = names["canvas"]
    api[N_RED_IMAGE]["inputs"]["image"] = names["red"]
    api[N_GREEN_IMAGE]["inputs"]["image"] = names["green"]
    for nid, text in ((N_CANVAS_PROMPT, a.canvas_prompt), (N_RED_PROMPT, a.red_prompt), (N_GREEN_PROMPT, a.green_prompt)):
        if text:
            api[nid]["inputs"]["value"] = text
    api[N_MASK_VIDEO]["inputs"].update(video=mask_name, custom_width=a.size, custom_height=a.size,
                                       force_rate=FPS, frame_load_cap=a.frames)
    api[N_WIDTH]["inputs"]["value"] = a.size
    api[N_HEIGHT]["inputs"]["value"] = a.size
    api[N_LENGTH]["inputs"]["value"] = a.frames
    api[N_CANVAS_STRENGTH]["inputs"]["strength"] = a.canvas_strength
    if a.seed is not None:
        api[N_SEED]["inputs"]["noise_seed"] = a.seed

    prompt_id = comfy(url, "POST", "/prompt", json={"prompt": api}).json()["prompt_id"]
    print(f"queued {prompt_id}; generating {a.frames} frames at {a.size}x{a.size} …")
    start = time.time()
    while True:
        hist = comfy(url, "GET", f"/history/{prompt_id}").json().get(prompt_id)
        if hist and hist.get("status", {}).get("completed"):
            break
        if hist and hist.get("status", {}).get("status_str") == "error":
            msgs = [m for m in hist["status"].get("messages", []) if m[0] == "execution_error"]
            die(f"generation failed: {json.dumps(msgs)[:2000]}")
        time.sleep(10)
    files = [f for out in hist["outputs"].values() for v in out.values() if isinstance(v, list)
             for f in v if isinstance(f, dict) and str(f.get("filename", "")).endswith(".mp4")]
    if len(files) != 1:
        die(f"expected one mp4 output, got {files}")
    f = files[0]
    video = comfy(url, "GET", "/view", params={"filename": f["filename"], "subfolder": f["subfolder"], "type": f["type"]})
    Path(a.out).write_bytes(video.content)
    print(f"done in {(time.time() - start) / 60:.1f} min → {a.out}")


def main():
    for tool in ("ffmpeg", "ffprobe", "yt-dlp"):
        if shutil.which(tool) is None:
            die(f"{tool} is not on PATH")
    p = argparse.ArgumentParser(prog="smash-cli", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="create the GPU pod and wait until ComfyUI is ready")
    up.add_argument("--gpu", action="append", help="RunPod GPU type id (repeatable); default: RTX PRO 6000 Blackwell")
    up.add_argument("--cloud", choices=["COMMUNITY", "SECURE"], default="SECURE")
    up.add_argument("--idle-minutes", type=int, default=20, help="pod deletes itself after this long with no job")
    up.add_argument("--timeout", type=int, default=45, help="minutes to wait for ComfyUI")
    up.set_defaults(fn=cmd_up)

    sub.add_parser("status", help="pod state and cost so far").set_defaults(fn=cmd_status)
    sub.add_parser("down", help="delete the pod").set_defaults(fn=cmd_down)

    r = sub.add_parser("run", help="generate one video",
                       description="Each source is an image or video file, a URL (yt-dlp), or gen:<prompt> (fal image).")
    r.add_argument("--canvas", required=True, help="global background source")
    r.add_argument("--red", required=True, help="source for the red mask region")
    r.add_argument("--green", required=True, help="source for the green mask region")
    r.add_argument("--canvas-prompt")
    r.add_argument("--red-prompt")
    r.add_argument("--green-prompt")
    r.add_argument("--mask", default=str(DEFAULT_MASK), help="red/green mask video (default: Kent's concentric circle)")
    r.add_argument("--frame-at", type=float, default=1.0, help="seconds into video sources to take the frame")
    r.add_argument("--cookies", help="cookies.txt for yt-dlp when YouTube asks to sign in")
    r.add_argument("--size", type=int, default=512)
    r.add_argument("--frames", type=int, default=158)
    r.add_argument("--canvas-strength", type=float, default=0.65)
    r.add_argument("--seed", type=int)
    r.add_argument("--out", default="smash.mp4")
    r.set_defaults(fn=cmd_run)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
