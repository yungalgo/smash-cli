# smash-cli

Run **context smashing** with MiniMax H3 from your terminal on a rented GPU. You do not need your own GPU.

The CLI starts a RunPod GPU pod, runs Kentskooking's ComfyUI workflows on it, and deletes the pod when you finish, or by itself when it is idle.

| Command | Kent's workflow | Inputs | Output |
|---|---|---|---|
| `canvas` | Concentric Canvas: the effect in his posts | 3 images (canvas, red, green) + 3 prompts + red/green mask video | 1 video with audio |
| `blend` | Omni Video Blend | 2 videos (files or URLs) + 2 prompts | 1 video with audio. No spatial mask: the two videos are mixed everywhere. |

Kent's regional mask guider supports image and text branches only, not video references. So it is not possible to put 3 real videos into mask regions with this code.

## Credit and provenance

- **Method and nodes:** [Kentskooking](https://x.com/kentskooking).
  - Original post: ["Context Smashing in H3 is really strange…"](https://x.com/kentskooking/status/2117008470230630586)
  - Explainer: ["This is what caused such bizarre results…"](https://x.com/kentskooking/status/2119198338306515136)
  - Nodes and workflow: [Kentskooking/ComfyUI-MiniMaxH3-Experiment](https://github.com/Kentskooking/ComfyUI-MiniMaxH3-Experiment) (MIT, pinned at `14cc482`).
- **Original idea:** Kent credits [Purz](https://x.com/PurzBeats) for the color-mask method, about two years ago ([purz-comfyui-workflows](https://github.com/purzbeats/purz-comfyui-workflows)).
- **Model:** [MiniMax H3](https://huggingface.co/MiniMaxAI/MiniMax-H3), via the [Comfy-Org repackage](https://huggingface.co/Comfy-Org/MiniMax-H3). For `blend`: NVFP4 ref2va from [FenomAI/MiniMax-H3](https://huggingface.co/FenomAI/MiniMax-H3) and the 8-step ref2v LoRA from [lightx2v/Minimax-h3-Turbo](https://huggingface.co/lightx2v/Minimax-h3-Turbo). These are the files that Kent's examples select.
- **Vendored files:** `vendor/kentskooking/` has Kent's `MiniMax_H3_Concentric_RedGreen_Canvas_8step.json`, `MiniMax_H3_Omni_Video_Blend_NVFP4_8step.json`, and `Concentric_Center_Circle_RedGreen_150f.mp4`, byte-for-byte, with his MIT `LICENSE`.

This repo only adds the plumbing: pod lifecycle, input preparation, and workflow submission.

## How it works

1. `up` creates a pod with an RTX PRO 6000 Blackwell (96 GB) GPU. The NVFP4 weights need Blackwell. `pod/boot.sh` installs pinned ComfyUI, Kent's nodes, VideoHelperSuite, and KJNodes, downloads about 57 GB of weights, and starts ComfyUI.
2. `canvas` makes each image square and fits the mask video to the output length. Each region is an H3 image-to-video branch. Kent's masked conditioning guider mixes the branches inside one generation, so the regions blend in the model. They are not composited afterwards.
3. `blend` downloads only the needed seconds of each video (yt-dlp, on your machine), cuts each clip to 768² at 24 fps and 124 frames, and runs Kent's Omni blend.
4. `down` deletes the pod. The pod also deletes itself after `--idle-minutes` (default 20) with no queued job.

Changes from Kent's files (the same in every run):
- The optional SageAttention and tiny-VAE preview patches are bypassed, as his example README describes.
- The `comfyui_essentials` "hold last frame" nodes are replaced by ffmpeg padding.
- The video is saved with VideoHelperSuite instead of `CreateVideo`/`SaveVideo`, because their widgets change between ComfyUI versions.
- `blend`: the `PreviewAny` debug node is removed.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/), `ffmpeg`, `yt-dlp`, and a RunPod account.

```sh
cp .env.example .env    # add RUNPOD_API_KEY (and FAL_KEY for gen: sources)
set -a; . ./.env; set +a
```

## Use

```sh
uv run smash-cli up                 # ~3-6 min; prints the hourly price

uv run smash-cli canvas \
  --canvas party.jpg \
  --red tiger.png \
  --green "gen:black and white photo of hands pressing into a face" \
  --red-prompt "The woman dances, jumping and bouncing" \
  --out smash-canvas.mp4

uv run smash-cli blend \
  --a "https://www.youtube.com/watch?v=..." --a-start 50 \
  --b ~/Videos/snowboard.mp4 --b-start 12 \
  --out smash-blend.mp4

uv run smash-cli status             # pod state and cost so far
uv run smash-cli down               # delete the pod
```

`canvas` sources: an image file, or `gen:<prompt>` (the image is made on fal with `SMASH_IMAGE_MODEL`, default `alibaba/qwen-image-3/text-to-image`). Options: `--mask` (your own red/green mask video), `--size` (default 512), `--frames` (default 158 at 24 fps), `--canvas-strength` (default 0.65), and `--seed`.

`blend` sources: a video file or a URL. If the download fails, the CLI tells you to download the video yourself and pass the file. Options: `--a-start`/`--b-start`, `--a-prompt`/`--b-prompt` (Kent's default prompts refer to each clip as `<Video 1>`), `--size` (default 1024), `--frames` (default 124), `--seed`, and `--cookies`.

## Notes

- **Cost:** you pay RunPod GPU time while the pod exists. For example, an RTX PRO 6000 costs about $1.69–2.49/h. There is no other cost. `status` shows the cost so far.
- **Security:** ComfyUI is reachable through RunPod's HTTPS proxy at `https://<pod-id>-8188.proxy.runpod.net`, and it has no authentication. Do not keep the pod running when you do not use it.
- **YouTube** may ask for sign-in on some networks. Export a cookies.txt and pass `--cookies`.

## License

MIT. The vendored files keep Kent's MIT license (`vendor/kentskooking/LICENSE`).
