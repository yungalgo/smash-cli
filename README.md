# smash-cli

Make **context smashing** videos with MiniMax H3 from your terminal. You do not need your own GPU.

smash-cli rents a GPU on RunPod, runs [Kentskooking's](https://x.com/kentskooking) Context Smashing workflow on it, and deletes the GPU when you finish.

**You give:** 3 images (a canvas and two regions, red and green) and a prompt for each one.
**You get:** one 6.6-second video with audio, where the three images mix together inside one generation.

Context smashing is Kent's method. See [credit](#credit-and-provenance).

---

## What you need

| Item | Why | Where to get it |
|---|---|---|
| **RunPod account with credit** | The GPU (RTX PRO 6000, about $2.49 per hour) | [runpod.io](https://www.runpod.io). Add at least $10 of credit, then make an API key at **Settings → API Keys** |
| **fal API key** (optional) | Only if you want to make the input images from text (`gen:`), or use `--upscale` | [fal.ai/dashboard/keys](https://fal.ai/dashboard/keys) |
| **uv** | Runs the CLI | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **ffmpeg** | Prepares the images and the mask on your computer | macOS: `brew install ffmpeg`. Ubuntu: `sudo apt install ffmpeg` |

You do not need Python, ComfyUI, or the model weights on your computer. All of that runs on the rented GPU.

## Setup (one time)

```sh
git clone https://github.com/yungalgo/smash-cli
cd smash-cli
cp .env.example .env
```

Open `.env` and add your keys:

```sh
RUNPOD_API_KEY=your-runpod-key
FAL_KEY=your-fal-key        # optional
```

Load the keys into your terminal. Do this again in each new terminal window:

```sh
set -a; . ./.env; set +a
```

## Make your first video

**1. Start the GPU.** This takes about 2 to 6 minutes. It installs everything and downloads about 80 GB of model files on the GPU machine. Billing starts now.

```sh
uv run smash-cli up
```

When it prints `ready after … min`, continue.

**2. Make a video.** This takes about 10 minutes.

```sh
uv run smash-cli canvas \
  --canvas moshpit.jpg \
  --canvas-prompt "Shaky handheld concert footage. The mosh pit surges, people jump and crash." \
  --red boxer.jpg \
  --red-prompt "Slow-motion boxing footage. The boxer throws fast punches, sweat spraying." \
  --green lava.jpg \
  --green-prompt "Extreme macro footage. Molten lava glows and flows through cracks in black rock." \
  --canvas-strength 0.3 \
  --out smash.mp4
```

You do not have images? Use `gen:` to make them from text on fal (needs `FAL_KEY`, about $0.04 per image):

```sh
uv run smash-cli canvas \
  --canvas "gen:wide photo of a packed punk concert mosh pit, stage lights" \
  --red "gen:action photo of a boxer throwing a punch, sweat spraying" \
  --green "gen:macro photo of cracked black rock with glowing lava in the cracks" \
  --out smash.mp4
```

You can make as many videos as you want while the GPU is on.

**3. Stop the GPU** when you finish:

```sh
uv run smash-cli down
```

If you forget, the GPU deletes itself after 20 minutes with no work. Check the state and cost at any time with `uv run smash-cli status`.

## Commands

| Command | What it does |
|---|---|
| `up` | Rents the GPU and waits until it is ready. Options: `--idle-minutes N` (auto-delete time, default 20), `--cloud COMMUNITY` (cheaper, less reliable), `--gpu "<RunPod GPU id>"` |
| `canvas` | Makes one video (see the options below) |
| `status` | Shows if the GPU is running and the cost so far |
| `down` | Deletes the GPU. Billing stops |

## `canvas` options

**Images and prompts**

| Option | Meaning |
|---|---|
| `--canvas IMAGE` | Background image. **Required** |
| `--red IMAGE`, `--green IMAGE` | Images for the red and green mask areas. **Required** |
| `--blue`, `--yellow`, `--magenta IMAGE` | Optional extra regions (up to 5). They need a mask with these colors (`--mask-preset` or `--mask`) |
| `--canvas-prompt`, `--red-prompt`, … | Text that describes the motion of each image. If you leave it out, Kent's general prompt is used |

`IMAGE` is a file path (`.jpg`, `.png`, …) or `gen:<text>` to make the image on fal. The CLI crops each image to a square from the center.

**Mask**

The mask is an animated color video. Red areas show the red image, green areas show the green image, and so on. Black areas show only the canvas.

| Option | Meaning |
|---|---|
| `--mask-preset kent` | Kent's growing red/green circle (default, two regions only) |
| `--mask-preset rings\|stripes\|checker\|pulse\|split` | Generated animated patterns for all the colors that you give |
| `--mask FILE` | Your own mask video: pure red, green, blue, yellow, magenta on black |
| `--save-mask FILE` | Also saves the mask that was used, so you can look at it |

**Look and quality**

| Option | Default | Meaning |
|---|---|---|
| `--canvas-strength` | 0.65 | How much the canvas affects the whole video. **0.3 often looks better**: the regions show more |
| `--seed N` | random | Same seed + same inputs = same video. Useful to compare one change |
| `--size` | 768 | Output width and height in pixels |
| `--frames` | 158 | Number of frames at 24 fps (158 ≈ 6.6 s) |
| `--guider regional` | overlap | Kent's newer guider (beta): cleaner and about 3× faster, but the images mix less |
| `--interaction` | 1.0 | Only with `--guider regional`: 1 = regions mix, 0 = regions stay apart |
| `--upscale flashvsr\|seedvr` | none | 2× upscale on fal after generation (about $0.19). Keeps the original as `<out>-raw.mp4` |
| `--model full`, `--vae fp16` | pruned, int8 | Higher-precision weights. In our tests: almost no visible difference |

## Tips for good results

- **Mix motion and texture.** The best results came from two very different motion scenes plus one texture that can turn into an effect. Example: mosh pit + boxer + lava (the lava became fire in the crowd). Three texture images give a calm, weak result.
- **Textures that can "act" work best:** lava → fire, ink → smoke, honey → drips. Flat textures (foil, wool) only cover surfaces.
- **Use `--canvas-strength 0.3`** if the video shows only the canvas image.
- **Avoid similar scenes:** a surfer and horses in water merge into one water scene.
- **Write prompts about motion:** "Slow-motion boxing footage. The boxer throws fast punches, sweat spraying." This is the same style as Kent's prompts.
- **Use 2 regions first.** Each extra region makes the render much slower: 3 regions took about 34 minutes.

## Cost and time

| Item | Cost | Time |
|---|---|---|
| GPU (RunPod RTX PRO 6000, secure cloud) | about $2.49 per hour while it is on | `up` takes 2 to 6 min |
| One video, 2 regions, 768² | about $0.40 of GPU time | about 10 min |
| `gen:` image on fal | about $0.04 each | about 1 to 2 min |
| `--upscale` on fal | about $0.19 per video | about 2 to 7 min |

## Troubleshooting

| Problem | What to do |
|---|---|
| `RUNPOD_API_KEY is not set` | Run `set -a; . ./.env; set +a` in this terminal |
| `no pod; run smash-cli up first` | Start the GPU with `up`. If it deleted itself after the idle time, run `up` again |
| `a pod is already recorded` | Run `down`, then `up` |
| `up` fails with a RunPod error about availability | No RTX PRO 6000 is free now. Try `--cloud COMMUNITY`, or wait and try again |
| `ComfyUI not ready after 45 min` | The GPU is still running. Run `down`, then `up` again |
| The video shows almost only the canvas | Use `--canvas-strength 0.3`, and choose more different images |
| `Kent's mask has only red and green` | You gave `--blue`/`--yellow`/`--magenta`. Add `--mask-preset rings` (or another preset) |

**Security note:** while the GPU runs, its ComfyUI server is reachable at `https://<pod-id>-8188.proxy.runpod.net` without a password. Do not leave the GPU on when you do not use it.

---

## How it works

1. `up` creates a RunPod pod with an RTX PRO 6000 Blackwell (96 GB) GPU. The H3 text encoder is NVFP4, so the GPU must be Blackwell. `pod/boot.sh` installs pinned ComfyUI, Kent's nodes, VideoHelperSuite, and KJNodes, downloads the weights from Hugging Face, and starts ComfyUI. A watchdog on the pod deletes it after the idle time.
2. `canvas` makes each image square, prepares the mask, fits it to the output length, uploads everything, and submits Kent's workflow to ComfyUI. Each image becomes an H3 image-to-video branch. The mask decides where each branch is active, and the branches are mixed inside one generation. They are not composited afterwards. Region colors beyond red and green are copies of Kent's red branch with their own color, image, and prompt.
3. `down` deletes the pod.

The inputs are images, not videos, because Kent's regional nodes support image and text branches only.

Changes from Kent's workflow file (the same in every run):
- The optional SageAttention and tiny-VAE preview patches are bypassed, as his example README describes.
- The `comfyui_essentials` "hold last frame" node is replaced by ffmpeg padding of the mask.
- The video is saved with VideoHelperSuite instead of `CreateVideo`/`SaveVideo`, because their settings change between ComfyUI versions.
- With `--guider regional`, the canvas branch gets the black mask areas, because an unmasked branch takes over the whole frame with that guider.

What we found in tests (same inputs and seed, 768²):
- Kent's default guider gives the fused "smash" look, but it adds a fine cross-hatch grain.
- `--model full --vae fp16` looks almost the same as the defaults.
- `--guider regional` is clean and about 3× faster, but the regions stay more separate. With moving presets, it settles on one source.
- FlashVSR reduces the grain a little in 1:1 crops, but at normal viewing size the difference is small. SeedVR is sharper but keeps the grain lines.

## Credit and provenance

- **Method and nodes:** [Kentskooking](https://x.com/kentskooking).
  - Original post: ["Context Smashing in H3 is really strange…"](https://x.com/kentskooking/status/2117008470230630586)
  - Explainer: ["This is what caused such bizarre results…"](https://x.com/kentskooking/status/2119198338306515136)
  - Nodes and workflow: [Kentskooking/ComfyUI-MiniMaxH3-Experiment](https://github.com/Kentskooking/ComfyUI-MiniMaxH3-Experiment) (MIT, pinned at `14cc482`).
- **Original idea:** Kent credits [Purz](https://x.com/PurzBeats) for the color-mask method, about two years ago ([purz-comfyui-workflows](https://github.com/purzbeats/purz-comfyui-workflows)).
- **Model:** [MiniMax H3](https://huggingface.co/MiniMaxAI/MiniMax-H3), via the [Comfy-Org repackage](https://huggingface.co/Comfy-Org/MiniMax-H3).
- **Vendored files:** `vendor/kentskooking/` has Kent's `MiniMax_H3_Concentric_RedGreen_Canvas_8step.json` and `Concentric_Center_Circle_RedGreen_150f.mp4`, byte-for-byte, with his MIT `LICENSE`.

This repo only adds the plumbing: GPU lifecycle, input preparation, and workflow submission.

## License

MIT. The vendored files keep Kent's MIT license (`vendor/kentskooking/LICENSE`).
