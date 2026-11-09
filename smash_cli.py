"""smash-cli: run Kentskooking's MiniMax H3 context smashing workflows on a RunPod GPU.

Commands: up, status, canvas, blend, down. See README.md.
"""

import argparse
import base64
import json
import math
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
VENDOR = ROOT / "vendor/kentskooking"
CANVAS_WORKFLOW = VENDOR / "MiniMax_H3_Concentric_RedGreen_Canvas_8step.json"
BLEND_WORKFLOW = VENDOR / "MiniMax_H3_Omni_Video_Blend_NVFP4_8step.json"
DEFAULT_MASK = VENDOR / "Concentric_Center_Circle_RedGreen_150f.mp4"
BOOT = ROOT / "pod/boot.sh"
STATE = Path.home() / ".smash-cli/pod.json"

RUNPOD_API = "https://rest.runpod.io/v1"
IMAGE = "runpod/pytorch:1.6.0-cu1281-torch2130-ubuntu2404"
# 96 GB Blackwell cards: the workflows' NVFP4 weights need Blackwell.
GPUS = [
    "NVIDIA RTX PRO 6000 Blackwell Server Edition",
    "NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
]
FPS = 24
SKIP_TYPES = {"MarkdownNote", "Note"}

# Node ids shared by both of Kent's workflows.
N_LENGTH, N_SEED, N_SIGMA_SHIFT = "5", "15", "8"
N_VIDEO_DECODE, N_AUDIO_DECODE = "20", "21"
# Every run changes Kent's graphs in the same way:
# - 9, 10: optional SageAttention and tiny-VAE preview patches; his example
#   README says to bypass both by wiring Sigma Shift straight through.
# - 22, 23: CreateVideo/SaveVideo widgets differ between ComfyUI versions;
#   we save decoded video (20) and audio (21) with VideoHelperSuite instead.
# - comfyui_essentials "hold last frame" nodes: we pad clips with ffmpeg, so
#   the loaders feed their consumers directly.
COMMON_REROUTE = {"9": (N_SIGMA_SHIFT, 0), "10": (N_SIGMA_SHIFT, 0)}
COMMON_DROP = {"22", "23"}

# Canvas (MiniMax_H3_Concentric_RedGreen_Canvas_8step.json): 3 images + mask.
C_RED_PROMPT, C_GREEN_PROMPT, C_CANVAS_PROMPT = "4", "216", "212"
C_WIDTH, C_HEIGHT, C_MASK_VIDEO, C_CANVAS_STRENGTH = "200", "201", "51", "214"
C_RED_IMAGE, C_GREEN_IMAGE, C_CANVAS_IMAGE = "110", "140", "211"
C_REROUTE = {**COMMON_REROUTE, "209": (C_MASK_VIDEO, 0)}

# Blend (MiniMax_H3_Omni_Video_Blend_NVFP4_8step.json): 2 videos.
B_A_PROMPT, B_B_PROMPT, B_A_VIDEO, B_B_VIDEO = "4", "42", "60", "61"
B_OUT_W, B_OUT_H, B_REF_W, B_REF_H = "100", "101", "102", "103"
B_REF_SIZE = 768
# 65 is a PreviewAny debug display of the token mix; not needed headless.
B_REROUTE = {**COMMON_REROUTE, "62": (B_A_VIDEO, 0), "63": (B_B_VIDEO, 0)}
B_DROP = COMMON_DROP | {"65"}


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
        "containerDiskInGb": 140,
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
    print("installing ComfyUI and downloading ~57 GB of weights; waiting for ComfyUI …")
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


def image_source(src, work, name):
    """Local image for a path or `gen:<prompt>` (fal image)."""
    if src.startswith("gen:"):
        return fal_image(src[4:], work / f"{name}-gen")
    if src.startswith(("http://", "https://")):
        die(f"--{name} must be an image file or gen:<prompt>; video links go to `smash-cli blend`")
    path = Path(src).expanduser()
    if not path.exists():
        die(f"no such file: {src}")
    return path


def video_source(src, work, name, start, seconds, cookies=None):
    """Local video and seek time for a path or a URL (yt-dlp, only the needed section)."""
    if not src.startswith(("http://", "https://")):
        path = Path(src).expanduser()
        if not path.exists():
            die(f"no such file: {src}")
        return path, start
    out = work / f"{name}-dl"
    auth = ["--cookies", str(Path(cookies).expanduser())] if cookies else []
    p = subprocess.run(
        ["yt-dlp", "-q", "--no-warnings", "--no-playlist", *auth, "-f", "bv*[height<=1080]/b",
         "--download-sections", f"*{start}-{start + seconds}", "-o", f"{out}.%(ext)s", src],
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


SQUARE = "crop='min(iw,ih)':'min(iw,ih)'"


def square_image(src, out, size):
    """Center-cropped size x size PNG."""
    run("ffmpeg", "-y", "-v", "error", "-i", str(src), "-frames:v", "1",
        "-vf", f"{SQUARE},scale={size}:{size}", str(out))
    return out


def square_clip(src, out, size, frames, start=0, mask=False):
    """size x size clip at FPS with exactly `frames` frames (last frame held if short)."""
    scale = f"scale={size}:{size}" + (":flags=neighbor" if mask else "")
    crop = "" if mask else f"{SQUARE},"
    vf = f"fps={FPS},{crop}{scale},tpad=stop_mode=clone:stop={frames}"
    seek = ["-ss", str(start)] if start else []
    run("ffmpeg", "-y", "-v", "error", *seek, "-i", str(src), "-an", "-vf", vf, "-frames:v", str(frames),
        "-c:v", "libx264", "-crf", "0" if mask else "12", "-pix_fmt", "yuv444p" if mask else "yuv420p", str(out))
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


def to_api(ui, object_info, reroute, drop):
    """Convert one of Kent's UI-format workflows to ComfyUI's API (prompt) format."""
    links = {l[0]: l for l in ui["links"]}
    dropped = set(reroute) | drop
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


def comfy(url, method, path, **kw):
    r = requests.request(method, url + path, timeout=300, **kw)
    if r.status_code >= 400:
        die(f"ComfyUI {method} {path}: {r.status_code} {r.text[:2000]}")
    return r


def upload(url, path, name):
    with open(path, "rb") as f:
        comfy(url, "POST", "/upload/image", files={"image": (name, f)}, data={"overwrite": "true"})
    return name


def generate(url, api, out, what):
    """Queue the prompt, wait, and download the one MP4 it writes."""
    prompt_id = comfy(url, "POST", "/prompt", json={"prompt": api}).json()["prompt_id"]
    print(f"queued {prompt_id}; generating {what} …")
    start = time.time()
    while True:
        hist = comfy(url, "GET", f"/history/{prompt_id}").json().get(prompt_id)
        if hist and hist.get("status", {}).get("completed"):
            break
        if hist and hist.get("status", {}).get("status_str") == "error":
            msgs = [m for m in hist["status"].get("messages", []) if m[0] == "execution_error"]
            die(f"generation failed: {json.dumps(msgs)[:2000]}")
        time.sleep(10)
    files = [f for o in hist["outputs"].values() for v in o.values() if isinstance(v, list)
             for f in v if isinstance(f, dict) and str(f.get("filename", "")).endswith(".mp4")]
    if len(files) != 1:
        die(f"expected one mp4 output, got {files}")
    f = files[0]
    video = comfy(url, "GET", "/view", params={"filename": f["filename"], "subfolder": f["subfolder"], "type": f["type"]})
    Path(out).write_bytes(video.content)
    print(f"done in {(time.time() - start) / 60:.1f} min → {out}")


def set_seed(api, seed):
    if seed is not None:
        api[N_SEED]["inputs"]["noise_seed"] = seed


# ---------------------------------------------------------------- commands


def cmd_canvas(a):
    url = base_url(load_state()["id"])
    tag = uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        names = {}
        for role in ("canvas", "red", "green"):
            png = square_image(image_source(getattr(a, role), work, role), work / f"{role}.png", a.size)
            names[role] = upload(url, png, f"smash-{tag}-{role}.png")
            print(f"{role}: {getattr(a, role)}")
        mask = square_clip(Path(a.mask).expanduser(), work / "mask.mp4", a.size, a.frames, mask=True)
        mask_name = upload(url, mask, f"smash-{tag}-mask.mp4")
    object_info = comfy(url, "GET", "/object_info").json()
    api = to_api(json.loads(CANVAS_WORKFLOW.read_text()), object_info, C_REROUTE, COMMON_DROP)

    api[C_CANVAS_IMAGE]["inputs"]["image"] = names["canvas"]
    api[C_RED_IMAGE]["inputs"]["image"] = names["red"]
    api[C_GREEN_IMAGE]["inputs"]["image"] = names["green"]
    for nid, text in ((C_CANVAS_PROMPT, a.canvas_prompt), (C_RED_PROMPT, a.red_prompt), (C_GREEN_PROMPT, a.green_prompt)):
        if text:
            api[nid]["inputs"]["value"] = text
    api[C_MASK_VIDEO]["inputs"].update(video=mask_name, custom_width=a.size, custom_height=a.size,
                                       force_rate=FPS, frame_load_cap=a.frames)
    api[C_WIDTH]["inputs"]["value"] = a.size
    api[C_HEIGHT]["inputs"]["value"] = a.size
    api[N_LENGTH]["inputs"]["value"] = a.frames
    api[C_CANVAS_STRENGTH]["inputs"]["strength"] = a.canvas_strength
    set_seed(api, a.seed)
    generate(url, api, a.out, f"{a.frames} frames at {a.size}x{a.size}")


def cmd_blend(a):
    url = base_url(load_state()["id"])
    tag = uuid.uuid4().hex[:8]
    seconds = math.ceil(a.frames / FPS) + 1
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        names = {}
        for role, start in (("a", a.a_start), ("b", a.b_start)):
            src, seek = video_source(getattr(a, role), work, role, start, seconds, a.cookies)
            clip = square_clip(src, work / f"{role}.mp4", B_REF_SIZE, a.frames, start=seek)
            names[role] = upload(url, clip, f"smash-{tag}-{role}.mp4")
            print(f"{role}: {getattr(a, role)} from {start}s")
    object_info = comfy(url, "GET", "/object_info").json()
    api = to_api(json.loads(BLEND_WORKFLOW.read_text()), object_info, B_REROUTE, B_DROP)

    api[B_A_VIDEO]["inputs"]["video"] = names["a"]
    api[B_B_VIDEO]["inputs"]["video"] = names["b"]
    for nid, text in ((B_A_PROMPT, a.a_prompt), (B_B_PROMPT, a.b_prompt)):
        if text:
            api[nid]["inputs"]["value"] = text
    api[B_OUT_W]["inputs"]["value"] = a.size
    api[B_OUT_H]["inputs"]["value"] = a.size
    api[B_REF_W]["inputs"]["value"] = B_REF_SIZE
    api[B_REF_H]["inputs"]["value"] = B_REF_SIZE
    api[N_LENGTH]["inputs"]["value"] = a.frames
    set_seed(api, a.seed)
    generate(url, api, a.out, f"{a.frames} frames at {a.size}x{a.size}")


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

    c = sub.add_parser("canvas", help="3 images + red/green mask -> 1 video (Kent's concentric canvas)",
                       description="Each source is an image file or gen:<prompt> (image made on fal).")
    c.add_argument("--canvas", required=True, help="global background image")
    c.add_argument("--red", required=True, help="image for the red mask region")
    c.add_argument("--green", required=True, help="image for the green mask region")
    c.add_argument("--canvas-prompt")
    c.add_argument("--red-prompt")
    c.add_argument("--green-prompt")
    c.add_argument("--mask", default=str(DEFAULT_MASK), help="red/green mask video (default: Kent's concentric circle)")
    c.add_argument("--size", type=int, default=512)
    c.add_argument("--frames", type=int, default=158)
    c.add_argument("--canvas-strength", type=float, default=0.65)
    c.add_argument("--seed", type=int)
    c.add_argument("--out", default="smash-canvas.mp4")
    c.set_defaults(fn=cmd_canvas)

    b = sub.add_parser("blend", help="2 videos -> 1 video (Kent's Omni video blend; no spatial mask)",
                       description="Each source is a video file or a URL (downloaded locally with yt-dlp).")
    b.add_argument("--a", required=True, help="first reference video")
    b.add_argument("--b", required=True, help="second reference video")
    b.add_argument("--a-start", type=float, default=0, help="start time in seconds")
    b.add_argument("--b-start", type=float, default=0, help="start time in seconds")
    b.add_argument("--a-prompt", help="describe clip A and the wanted output (Kent's default refers to <Video 1>)")
    b.add_argument("--b-prompt")
    b.add_argument("--cookies", help="cookies.txt for yt-dlp when YouTube asks to sign in")
    b.add_argument("--size", type=int, default=1024)
    b.add_argument("--frames", type=int, default=124)
    b.add_argument("--seed", type=int)
    b.add_argument("--out", default="smash-blend.mp4")
    b.set_defaults(fn=cmd_blend)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
