# smash-cli

Run **context smashing** with MiniMax H3 from your terminal on a rented GPU. You do not need your own GPU.

The CLI starts a RunPod GPU pod, runs Kentskooking's ComfyUI workflow on it unchanged, and deletes the pod when you finish, or by itself when it is idle.

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

1. `up` creates a pod with an RTX PRO 6000 Blackwell (96 GB) GPU. The H3 text encoder is NVFP4, so the GPU must be Blackwell. `pod/boot.sh` installs pinned ComfyUI, Kent's nodes, VideoHelperSuite, and KJNodes, downloads about 42 GB of weights, and starts ComfyUI.
2. `run` prepares three sources. Each one becomes a square frame:
   - **canvas:** the global background
   - **red:** the red mask region
   - **green:** the green mask region

   The red/green mask video is fitted to the output length. The CLI then submits Kent's workflow with your images and prompts and downloads the MP4.
3. `down` deletes the pod. The pod also deletes itself after `--idle-minutes` (default 20) with no queued job.

Each region is an H3 image-to-video branch. Kent's masked conditioning guider mixes the branches inside one generation, so the regions blend in the model. They are not composited afterwards.

Three changes from Kent's file (the same in every run):
- The optional SageAttention and tiny-VAE preview patches are bypassed, as his example README describes.
- The `comfyui_essentials` "hold last frame" node is replaced by ffmpeg padding of the mask.
- The video is saved with VideoHelperSuite instead of `CreateVideo`/`SaveVideo`, because their widgets change between ComfyUI versions.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/), `ffmpeg`, `yt-dlp`, and a RunPod account.

```sh
cp .env.example .env    # add RUNPOD_API_KEY (and FAL_KEY for gen: sources)
set -a; . ./.env; set +a
```

## Use

```sh
uv run smash-cli up                 # ~10-20 min the first time; prints the hourly price
uv run smash-cli run \
  --canvas party.jpg \
  --red "https://www.youtube.com/watch?v=..." \
  --green "gen:black and white photo of hands pressing into a face" \
  --red-prompt "The woman dances, jumping and bouncing" \
  --out smash.mp4
uv run smash-cli status             # pod state and cost so far
uv run smash-cli down               # delete the pod
```

Each source can be:
- an image file
- a video file (the frame at `--frame-at` seconds is used)
- a URL (yt-dlp downloads only two seconds around `--frame-at`)
- `gen:<prompt>` (the image is made on fal with `SMASH_IMAGE_MODEL`, default `alibaba/qwen-image-3/text-to-image`)

Other options: `--mask` (your own red/green mask video), `--size` (default 512), `--frames` (default 158 at 24 fps), `--canvas-strength` (default 0.65), `--seed`, and `--cookies` (cookies.txt for yt-dlp).

## Notes

- **Cost:** you pay RunPod GPU time while the pod exists. For example, an RTX PRO 6000 costs about $1.69–2.49/h. There is no other cost. `status` shows the cost so far.
- **Security:** ComfyUI is reachable through RunPod's HTTPS proxy at `https://<pod-id>-8188.proxy.runpod.net`, and it has no authentication. Do not keep the pod running when you do not use it.
- **YouTube** may ask for sign-in on some networks. Export a cookies.txt and pass `--cookies`.

## License

MIT. The vendored files keep Kent's MIT license (`vendor/kentskooking/LICENSE`).
