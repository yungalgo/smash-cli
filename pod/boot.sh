#!/bin/bash
# Runs on the RunPod pod at start (passed in by `smash-cli up`).
# Installs pinned ComfyUI + custom nodes, downloads the MiniMax H3 weights,
# starts an idle watchdog, then serves ComfyUI on port 8188.
set -euo pipefail

: "${SMASH_IDLE_MINUTES:?SMASH_IDLE_MINUTES is required}"
: "${RUNPOD_POD_ID:?RUNPOD_POD_ID is required (set by RunPod)}"

COMFYUI_SHA=0decb39ee5b5e17c200334589fc317dbbe98310e
KENT_SHA=14cc4824d965f7c900b427b4d2d0241c8e7df58c
VHS_SHA=4d907bee61e92c2e65af3bd6383a4e4d356126d1

clone() { # url dir sha
  git clone -q "$1" "$2"
  git -C "$2" checkout -q "$3"
}

# Idle watchdog: delete this pod when ComfyUI has had no queued or running job
# for SMASH_IDLE_MINUTES. Time before ComfyUI answers (setup) counts as busy.
watchdog() {
  local last
  last=$(date +%s)
  while sleep 60; do
    local q
    if ! q=$(curl -sf http://127.0.0.1:8188/queue); then
      last=$(date +%s)
      continue
    fi
    if python3 -c 'import json,sys;d=json.loads(sys.argv[1]);sys.exit(0 if d["queue_running"] or d["queue_pending"] else 1)' "$q"; then
      last=$(date +%s)
    fi
    if (( $(date +%s) - last > SMASH_IDLE_MINUTES * 60 )); then
      echo "idle for ${SMASH_IDLE_MINUTES} min: removing pod $RUNPOD_POD_ID"
      runpodctl remove pod "$RUNPOD_POD_ID"
    fi
  done
}
watchdog &

cd /root
clone https://github.com/comfyanonymous/ComfyUI.git ComfyUI "$COMFYUI_SHA"
clone https://github.com/Kentskooking/ComfyUI-MiniMaxH3-Experiment.git ComfyUI/custom_nodes/ComfyUI-MiniMaxH3-Experiments "$KENT_SHA"
clone https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git ComfyUI/custom_nodes/ComfyUI-VideoHelperSuite "$VHS_SHA"

pip install -q --no-cache-dir -r ComfyUI/requirements.txt
pip install -q --no-cache-dir -r ComfyUI/custom_nodes/ComfyUI-MiniMaxH3-Experiments/requirements.txt
pip install -q --no-cache-dir -r ComfyUI/custom_nodes/ComfyUI-VideoHelperSuite/requirements.txt
pip install -q --no-cache-dir "huggingface_hub[hf_xet]>=0.34"

# Official ComfyUI repackage of MiniMax H3; folder layout matches ComfyUI/models.
hf download Comfy-Org/MiniMax-H3 \
  diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors \
  loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors \
  text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors \
  vae/minimax_h3_video_vae_int8_convrot.safetensors \
  vae/minimax_h3_audio_vae_fp32.safetensors \
  --local-dir ComfyUI/models

cd ComfyUI
exec python3 main.py --listen 0.0.0.0 --port 8188
