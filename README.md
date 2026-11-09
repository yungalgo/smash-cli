# smash-cli

Run **context smashing** with MiniMax H3 from your terminal on a rented GPU. You do not need your own GPU.

The CLI starts a RunPod GPU pod, runs Kentskooking's Concentric Canvas workflow on it, and deletes the pod when you finish, or by itself when it is idle.

**Inputs:** 1 canvas image + 2 to 5 region images (red, green, blue, yellow, magenta) + one prompt for each + an animated color mask. **Output:** 1 video with audio.

Each image becomes an H3 image-to-video branch. The mask decides where each branch is active, and the branches are mixed inside one generation. They are not composited afterwards. Kent's regional nodes support image and text branches only, not video references, so the inputs are images, not videos.

## Credit and provenance

- **Method and nodes:** [Kentskooking](https://x.com/kentskooking).
  - Original post: ["Context Smashing in H3 is really strange…"](https://x.com/kentskooking/status/2117008470230630586)
  - Explainer: ["This is what caused such bizarre results…"](https://x.com/kentskooking/status/2119198338306515136)
  - Nodes and workflow: [Kentskooking/ComfyUI-MiniMaxH3-Experiment](https://github.com/Kentskooking/ComfyUI-MiniMaxH3-Experiment) (MIT, pinned at `14cc482`).
- **Original idea:** Kent credits [Purz](https://x.com/PurzBeats) for the color-mask method, about two years ago ([purz-comfyui-workflows](https://github.com/purzbeats/purz-comfyui-workflows)).
- **Model:** [MiniMax H3](https://huggingface.co/MiniMaxAI/MiniMax-H3), via the [Comfy-Org repackage](https://huggingface.co/Comfy-Org/MiniMax-H3).
- **Vendored files:** `vendor/kentskooking/` has Kent's `MiniMax_H3_Concentric_RedGreen_Canvas_8step.json` and `Concentric_Center_Circle_RedGreen_150f.mp4`, byte-for-byte, with his MIT `LICENSE`.

This repo only adds the plumbing: pod lifecycle, input preparation, and workflow submission.

## How it works

1. `up` creates a pod with an RTX PRO 6000 Blackwell (96 GB) GPU. The NVFP4 text encoder needs Blackwell. `pod/boot.sh` installs pinned ComfyUI, Kent's nodes, VideoHelperSuite, and KJNodes, downloads about 80 GB of weights, and starts ComfyUI.
2. `canvas` makes each image square, prepares the mask (Kent's, a generated preset, or your own), and fits it to the output length. It then submits Kent's workflow. Region colors beyond red and green are copies of Kent's red branch with their own color, image, and prompt.
3. `down` deletes the pod. The pod also deletes itself after `--idle-minutes` (default 20) with no queued job.

Changes from Kent's file (the same in every run):
- The optional SageAttention and tiny-VAE preview patches are bypassed, as his example README describes.
- The `comfyui_essentials` "hold last frame" node is replaced by ffmpeg padding of the mask.
- The video is saved with VideoHelperSuite instead of `CreateVideo`/`SaveVideo`, because their widgets change between ComfyUI versions.

Options that change his graph only when you select them:
- `--guider regional` uses his H3 Regional Interaction Guider (beta) instead of the masked conditioning guider. The canvas branch then gets the black mask areas, because an unmasked branch takes over the whole frame with this guider.
- `--model full` / `--vae fp16` select the higher-precision Comfy-Org weights instead of pruned INT8 / INT8.

`--upscale flashvsr|seedvr` adds a 2× upscale (768² → 1536²) on fal after generation (about $0.19 per clip) and keeps the raw video as `<out>-raw.mp4`. It is off by default: in our tests, the gain was small.

What we found in tests (same inputs and seed, 768²):
- Kent's overlap guider gives the fused "smash" look, but it adds a fine cross-hatch grain.
- `--model full --vae fp16` looks almost the same as the defaults, at the same speed.
- `--guider regional` is clean and about 3× faster, but the regions stay more separate. With moving presets, it settles on one source.
- FlashVSR reduces the grain a little in 1:1 crops, but at normal viewing size the difference is small. SeedVR is sharper but keeps the grain lines.
- Mix inputs: put one texture together with motion subjects (people, animals, crowds). Three texture images or presets with large black bands give weak results, because the canvas then dominates.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/), `ffmpeg`, and a RunPod account.

```sh
cp .env.example .env    # add RUNPOD_API_KEY and FAL_KEY
set -a; . ./.env; set +a
```

## Use

```sh
uv run smash-cli up                 # ~2-6 min; prints the hourly price

uv run smash-cli canvas \
  --canvas lava.png \
  --red coral.png --red-prompt "Coral tentacles sway in the current" \
  --green "gen:crumpled holographic foil" \
  --blue moss.png \
  --mask-preset rings \
  --out smash.mp4

uv run smash-cli status             # pod state and cost so far
uv run smash-cli down               # delete the pod
```

Images: an image file, or `gen:<prompt>` (the image is made on fal with `SMASH_IMAGE_MODEL`, default `alibaba/qwen-image-3/text-to-image`).

Masks (`--mask-preset`):
- `kent`: Kent's red/green circle. This is the default, for two regions only.
- `rings`, `stripes`, `checker`, `pulse`, `split`: generated for all the colors that you give. Black areas get only the canvas.
- `--mask FILE`: your own video in pure red/green/blue/yellow/magenta on black.
- `--save-mask FILE`: also writes the mask that was used.

Other options: `--upscale flashvsr|seedvr|none` (default none), `--guider overlap|regional`, `--interaction` (regional only; 1 = regions share attention, 0 = regions kept apart), `--model pruned|full`, `--vae int8|fp16`, `--size` (default 768), `--frames` (default 158 at 24 fps), `--canvas-strength` (default 0.65), and `--seed`.

Prompts: describe the motion and the material, for example "Slow-motion boxing footage. The boxer throws fast punches, sweat spraying". Kent's prompts use the same style.

## Notes

- **Cost:** RunPod GPU time while the pod exists (an RTX PRO 6000 costs about $1.69–2.49/h), and fal for the upscale (~$0.19 per clip) and for `gen:` images (~$0.04 each). `status` shows the pod cost so far. One 768² clip with two regions takes about 10 min on the GPU, and each extra region adds more.
- **Security:** ComfyUI is reachable through RunPod's HTTPS proxy at `https://<pod-id>-8188.proxy.runpod.net`, and it has no authentication. Do not keep the pod running when you do not use it.
- **Beta:** Kent marks the regional guider as beta. His docs say that "full-model visual quality is unverified".

## License

MIT. The vendored files keep Kent's MIT license (`vendor/kentskooking/LICENSE`).
