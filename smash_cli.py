"""smash-cli: run Kentskooking's MiniMax H3 context smashing workflow on a RunPod GPU.

Commands: up, status, canvas, down. See README.md.
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
VENDOR = ROOT / "vendor/kentskooking"
WORKFLOW = VENDOR / "MiniMax_H3_Concentric_RedGreen_Canvas_8step.json"
KENT_MASK = VENDOR / "Concentric_Center_Circle_RedGreen_150f.mp4"
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
SKIP_TYPES = {"MarkdownNote", "Note"}

# Region colors, in mask order. Kent's workflow has red and green branches;
# the others are clones of the red branch with their own color and image.
COLORS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "magenta": (255, 0, 255),
}
MODELS = {
    "pruned": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",  # Kent's choice
    "full": "minimax_h3_fl2va_int8_convrot.safetensors",
}
VAES = {
    "int8": "minimax_h3_video_vae_int8_convrot.safetensors",  # Kent's choice
    "fp16": "minimax_h3_video_vae_fp16.safetensors",
}

# Node ids in Kent's workflow (vendor/kentskooking/…Canvas_8step.json).
N_LENGTH, N_SEED, N_UNET, N_SIGMA_SHIFT, N_VAE = "5", "15", "6", "8", "11"
N_GUIDER, N_VIDEO_DECODE, N_AUDIO_DECODE = "18", "20", "21"
N_CANVAS_PROMPT, N_CANVAS_IMAGE, N_CANVAS_STRENGTH = "212", "211", "214"
N_WIDTH, N_HEIGHT, N_MASK_VIDEO = "200", "201", "51"
N_COLOR_COMBINE, N_FINAL_COMBINE = "180", "215"
# Per color: prompt, image, image-to-video, color-to-mask, mask profile, set-mask.
BRANCHES = {
    "red": {"prompt": "4", "image": "110", "i2v": "111", "color": "112", "profile": "113", "setmask": "114"},
    "green": {"prompt": "216", "image": "140", "i2v": "141", "color": "142", "profile": "143", "setmask": "144"},
}
# Changes applied to Kent's graph in every run:
# - 9, 10: optional SageAttention and tiny-VAE preview patches; his example
#   README says to bypass both by wiring Sigma Shift straight through.
# - 209: comfyui_essentials "hold last mask frame"; the CLI pads the mask.
# - 22, 23: CreateVideo/SaveVideo widgets differ between ComfyUI versions;
#   decoded video (20) and audio (21) are saved with VideoHelperSuite instead.
REROUTE = {"9": (N_SIGMA_SHIFT, 0), "10": (N_SIGMA_SHIFT, 0), "209": (N_MASK_VIDEO, 0)}
DROP = {"22", "23"}

PRESETS = ("kent", "rings", "stripes", "checker", "pulse", "split")


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
    print("installing ComfyUI and downloading ~80 GB of weights; waiting for ComfyUI …")
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
        die(f"--{name} must be an image file or gen:<prompt>")
    path = Path(src).expanduser()
    if not path.exists():
        die(f"no such file: {src}")
    return path


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


UPSCALERS = {
    "flashvsr": ("fal-ai/flashvsr/upscale/video", {"upscale_factor": 2, "preserve_audio": True}),
    "seedvr": ("fal-ai/seedvr/upscale/video", {"upscale_factor": 2}),
}


def fal_upscale(src, upscaler, out):
    """2x video upscale on fal (queue API); the video is sent inline as a data URI."""
    model, args = UPSCALERS[upscaler]
    headers = {"Authorization": f"Key {env('FAL_KEY')}"}
    uri = "data:video/mp4;base64," + base64.b64encode(Path(src).read_bytes()).decode()
    r = requests.post(f"https://queue.fal.run/{model}", json={"video_url": uri, **args}, headers=headers, timeout=300)
    if r.status_code >= 400:
        die(f"fal {model}: {r.status_code} {r.text[:300]}")
    job = r.json()
    print(f"upscaling 2x with {upscaler} on fal …")
    start = time.time()
    while True:
        status = requests.get(job["status_url"], headers=headers, timeout=60).json().get("status")
        if status == "COMPLETED":
            break
        if time.time() - start > 1800:
            die(f"fal {model}: not done after 30 min (request {job['request_id']})")
        time.sleep(5)
    res = requests.get(job["response_url"], headers=headers, timeout=120)
    if res.status_code >= 400:
        die(f"fal {model}: {res.status_code} {res.text[:300]}")
    video = requests.get(res.json()["video"]["url"], timeout=300)
    video.raise_for_status()
    Path(out).write_bytes(video.content)
    print(f"upscaled in {(time.time() - start) / 60:.1f} min → {out}")


def square_image(src, out, size):
    """Center-cropped size x size PNG."""
    run("ffmpeg", "-y", "-v", "error", "-i", str(src), "-frames:v", "1",
        "-vf", f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size}", str(out))
    return out


def fit_mask(src, out, size, frames):
    """Mask video at size x size, FPS, exactly `frames` frames (last frame held)."""
    vf = f"fps={FPS},scale={size}:{size}:flags=neighbor,tpad=stop_mode=clone:stop={frames}"
    run("ffmpeg", "-y", "-v", "error", "-i", str(src), "-an", "-vf", vf, "-frames:v", str(frames),
        "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv444p", str(out))
    return out


def preset_index(preset, n):
    """ffmpeg geq expression: region index per pixel (0 = canvas only, 1..n = colors).

    X, Y: pixel; W, H: frame size; N: frame number. Bands are 1/6 of the frame
    and move one band per second, so every color crosses the frame.
    """
    k = n + 1
    band = "(W/6)"
    t = f"(N/{FPS})"
    expr = {
        "rings": f"floor(hypot(X-W/2,Y-H/2)/{band}-{t})",
        "stripes": f"floor(X/{band}+{t})",
        "checker": f"floor(X/{band})+floor(Y/{band})+floor({t})",
        "pulse": f"floor({t})",
        "split": f"floor(atan2(Y-H/2,X-W/2)/(2*PI)*{k}+{t}/2)",
    }[preset]
    # ffmpeg's mod keeps the sign of the dividend; offset so it stays >= 0.
    return f"mod({expr}+{1000 * k},{k})"


def make_mask(preset, colors, out, size, frames):
    """Animated mask video for a preset, with one color per region in `colors`."""
    idx = preset_index(preset, len(colors))

    def channel(c):
        on = [i + 1 for i, name in enumerate(colors) if COLORS[name][c]]
        expr = "+".join(f"eq({idx},{i})" for i in on) or "0"
        return f"255*gte({expr},1)"

    geq = f"geq=r='{channel(0)}':g='{channel(1)}':b='{channel(2)}'"
    run("ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=black:s={size}x{size}:r={FPS}",
        "-vf", f"format=rgb24,{geq}", "-frames:v", str(frames),
        "-c:v", "libx264rgb", "-crf", "0", str(out))
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
    dropped = set(REROUTE) | DROP
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
            src, slot = REROUTE.get(str(src), (str(src), slot))
            inputs[inp["name"]] = [src, slot]
        api[nid] = {"class_type": node["type"], "inputs": inputs}
    api["smash_out"] = {"class_type": "VHS_VideoCombine", "inputs": {
        "images": [N_VIDEO_DECODE, 0], "audio": [N_AUDIO_DECODE, 0], "frame_rate": FPS,
        "loop_count": 0, "filename_prefix": "smash", "format": "video/h264-mp4",
        "pingpong": False, "save_output": True,
    }}
    return api


def add_branch(api, color):
    """Clone the red branch for another mask color; return its node ids."""
    ids = {}
    for role, nid in BRANCHES["red"].items():
        if role == "prompt":
            continue
        ids[role] = f"{color}_{role}"
        api[ids[role]] = json.loads(json.dumps(api[nid]))
    api[ids["i2v"]]["inputs"]["first_frame"] = [ids["image"], 0]
    api[ids["color"]]["inputs"].update(zip(("red", "green", "blue"), COLORS[color]))
    api[ids["profile"]]["inputs"]["mask"] = [ids["color"], 0]
    api[ids["setmask"]]["inputs"]["conditioning"] = [ids["i2v"], 0]
    api[ids["setmask"]]["inputs"]["mask"] = [ids["profile"], 0]
    return ids


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


# ---------------------------------------------------------------- canvas


def cmd_canvas(a):
    colors = [c for c in COLORS if getattr(a, c)]
    if colors[:2] != ["red", "green"]:
        die("--red and --green are required")
    if a.mask_preset == "kent" and not a.mask and colors != ["red", "green"]:
        die("Kent's mask has only red and green; use --mask-preset or --mask for more colors")
    if a.upscale != "none":
        env("FAL_KEY")  # fail before renting GPU time

    url = base_url(load_state()["id"])
    tag = uuid.uuid4().hex[:8]
    names = {}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for role in ["canvas", *colors]:
            png = square_image(image_source(getattr(a, role), work, role), work / f"{role}.png", a.size)
            names[role] = upload(url, png, f"smash-{tag}-{role}.png")
            print(f"{role}: {getattr(a, role)}")
        if a.mask:
            mask = fit_mask(Path(a.mask).expanduser(), work / "mask.mp4", a.size, a.frames)
        elif a.mask_preset == "kent":
            mask = fit_mask(KENT_MASK, work / "mask.mp4", a.size, a.frames)
        else:
            mask = make_mask(a.mask_preset, colors, work / "mask.mp4", a.size, a.frames)
        if a.save_mask:
            shutil.copy(mask, a.save_mask)
        names["mask"] = upload(url, mask, f"smash-{tag}-mask.mp4")

    object_info = comfy(url, "GET", "/object_info").json()
    api = to_api(json.loads(WORKFLOW.read_text()), object_info)

    # Branches: Kent's red and green, plus clones for extra colors.
    branches = dict(BRANCHES)
    combine = N_COLOR_COMBINE
    for color in colors[2:]:
        branches[color] = add_branch(api, color)
        node = f"{color}_combine"
        api[node] = {"class_type": "ConditioningCombine", "inputs": {
            "conditioning_1": [combine, 0], "conditioning_2": [branches[color]["setmask"], 0]}}
        combine = node
    api[N_FINAL_COMBINE]["inputs"]["conditioning_1"] = [combine, 0]

    for color in colors:
        b = branches[color]
        api[b["image"]]["inputs"]["image"] = names[color]
        text = getattr(a, f"{color}_prompt")
        if color in BRANCHES:
            if text:
                api[b["prompt"]]["inputs"]["value"] = text
        else:
            # Clones take the prompt directly; default to Kent's red prompt.
            api[b["i2v"]]["inputs"]["prompt"] = text or api[BRANCHES["red"]["prompt"]]["inputs"]["value"]
    api[N_CANVAS_IMAGE]["inputs"]["image"] = names["canvas"]
    if a.canvas_prompt:
        api[N_CANVAS_PROMPT]["inputs"]["value"] = a.canvas_prompt
    api[N_CANVAS_STRENGTH]["inputs"]["strength"] = a.canvas_strength
    api[N_MASK_VIDEO]["inputs"].update(video=names["mask"], custom_width=a.size, custom_height=a.size,
                                       force_rate=FPS, frame_load_cap=a.frames)
    api[N_WIDTH]["inputs"]["value"] = a.size
    api[N_HEIGHT]["inputs"]["value"] = a.size
    api[N_LENGTH]["inputs"]["value"] = a.frames
    api[N_UNET]["inputs"]["unet_name"] = MODELS[a.model]
    api[N_VAE]["inputs"]["vae_name"] = VAES[a.vae]
    if a.seed is not None:
        api[N_SEED]["inputs"]["noise_seed"] = a.seed
    if a.guider == "regional":
        # A global (unmasked) branch takes over the whole frame with this
        # guider, so the canvas gets the black mask areas, like a region.
        red = BRANCHES["red"]
        for role in ("color", "profile", "setmask"):
            api[f"canvas_{role}"] = json.loads(json.dumps(api[red[role]]))
        api["canvas_color"]["inputs"].update(red=0, green=0, blue=0)
        api["canvas_profile"]["inputs"]["mask"] = ["canvas_color", 0]
        api["canvas_setmask"]["inputs"].update(conditioning=[N_CANVAS_STRENGTH, 0], mask=["canvas_profile", 0])
        api[N_FINAL_COMBINE]["inputs"]["conditioning_2"] = ["canvas_setmask", 0]
        g = api[N_GUIDER]["inputs"]
        api[N_GUIDER] = {"class_type": "H3ExperimentsRegionalInteractionGuider", "inputs": {
            "model": g["model"], "conditioning": g["conditioning"], "mask_space": "frames",
            "query_chunk": 512, "interaction": a.interaction, "mask_cache_mb": 256}}

    out = Path(a.out)
    raw = out.with_name(out.stem + "-raw" + out.suffix) if a.upscale != "none" else out
    generate(url, api, raw, f"{a.frames} frames at {a.size}x{a.size}, {len(colors)} regions, "
                            f"{a.guider} guider, {a.model} model, {a.vae} VAE")
    if a.upscale != "none":
        fal_upscale(raw, a.upscale, out)


def main():
    for tool in ("ffmpeg", "ffprobe"):
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

    c = sub.add_parser("canvas", help="canvas + 2-5 color regions -> 1 video (Kent's concentric canvas)",
                       description="Each image is a file or gen:<prompt> (image made on fal).")
    c.add_argument("--canvas", required=True, help="global background image")
    c.add_argument("--canvas-prompt")
    for color in COLORS:
        need = color in ("red", "green")
        c.add_argument(f"--{color}", required=need, help=f"image for the {color} mask region")
        c.add_argument(f"--{color}-prompt")
    c.add_argument("--mask-preset", choices=PRESETS, default="kent",
                   help="kent: Kent's red/green circle; others are generated for all given colors")
    c.add_argument("--mask", help="your own mask video (pure red/green/blue/yellow/magenta on black)")
    c.add_argument("--save-mask", help="also write the mask video used to this path")
    c.add_argument("--guider", choices=["overlap", "regional"], default="overlap",
                   help="overlap: Kent's masked guider (embedding overlap); regional: his regional attention guider (beta)")
    c.add_argument("--interaction", type=float, default=1.0,
                   help="regional guider only: 1 = regions share attention, 0 = regions kept apart")
    c.add_argument("--model", choices=list(MODELS), default="pruned")
    c.add_argument("--vae", choices=list(VAES), default="int8")
    c.add_argument("--size", type=int, default=768)
    c.add_argument("--frames", type=int, default=158)
    c.add_argument("--canvas-strength", type=float, default=0.65)
    c.add_argument("--seed", type=int)
    c.add_argument("--upscale", choices=[*UPSCALERS, "none"], default="none",
                   help="2x video upscale on fal after generation (needs FAL_KEY); the raw video is kept as <out>-raw.mp4")
    c.add_argument("--out", default="smash.mp4")
    c.set_defaults(fn=cmd_canvas)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
