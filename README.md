# AstrBot Plugin: ComfyUI Bridge

A small, English-only AstrBot plugin that connects AstrBot to a
self-hosted ComfyUI instance for **text-to-image and image-to-image**
generation, using workflows you export yourself from ComfyUI. No
specific model, checkpoint, or LoRA is assumed — it just injects a
prompt (and, for img2img, an uploaded photo) into the node IDs you
configure and returns whatever image ComfyUI produces.

Built as a minimal alternative to the (excellent, but Chinese-language)
`astrbot_plugin_comfyui_pro` — same core idea, far fewer moving parts,
because it doesn't need multi-workflow switching, LoRA catalog
scanning, or sensitive-word filtering.

## What it does

- **`/draw <prompt>`** — text-to-image. Direct command, always works
  regardless of whether your model provider supports tool calling.
- **Attach a photo + `/draw <prompt>`** — image-to-image. Same command,
  auto-detected based on whether an image is attached to the message.
  Requires the image-to-image workflow to be configured (see below);
  if it isn't, the plugin falls back to text-to-image and tells you why.
- **Natural language** — registers a `generate_image` tool the LLM can
  call automatically when someone asks for a picture (e.g. "draw me a
  cat", or "turn this into a painting" with a photo attached). Requires
  the model provider to support tool/function calls. See the note in
  "Known limitation" below.

**Scope note:** only an image attached to the *same* message as the
request is used. Referring back to a photo sent earlier in the
conversation ("use the one I sent before") isn't supported.

## Installation

**Option A — upload the zip via the AstrBot dashboard (simplest):**

1. After exporting your workflow (see below), make sure your `.json`
   file is inside this plugin's `workflow/` folder *before* zipping.
2. Zip the whole `astrbot_plugin_comfyui_bridge` folder. On Windows:
   right-click the folder → **Send to → Compressed (zipped) folder**.
   Make sure `main.py` etc. end up at the **top level inside the zip**
   — not nested one level deeper (e.g. avoid the zip containing
   `astrbot_plugin_comfyui_bridge/astrbot_plugin_comfyui_bridge/main.py`,
   which can happen depending on how the files were selected before
   compressing).
3. In the AstrBot dashboard, go to **Extensions** → look for an
   Install/Upload option that takes a local `.zip` (as opposed to
   installing from the marketplace by name).
4. Upload, then **reload plugins** so AstrBot picks it up.

**Option B — copy the folder directly onto the server:**

Copy this folder into AstrBot's plugin directory yourself (e.g. via
`scp` onto `qwen`, or directly if AstrBot runs on the same machine you're
editing on), then reload plugins in the dashboard. Same end result as
Option A — pick whichever is easier given how you access the server.

## Setting up your workflow

**Recommended — paste it directly into the AstrBot config (no file upload, no re-zipping):**

1. In ComfyUI, open the workflow you want AstrBot to use.
2. **File → Export Workflow (API)** → saves as a `.json` file.
   (Older ComfyUI versions called this "Save (API Format)" and required
   developer mode to be enabled first — current versions don't require
   that for the export itself.)
3. Open that `.json` file in any text editor, select all, copy.
4. In the AstrBot dashboard, open this plugin's settings and paste it
   into the **Workflow JSON** field.
5. Enable ComfyUI's **developer mode** (gear icon next to "Queue Size" →
   "Enable Dev mode Options") — this is a separate setting needed so
   node IDs display above each node's title, for the node ID fields below.
6. Fill in the remaining fields:
   - `server_address` — your ComfyUI host and port, e.g. `192.168.1.15:8188`.
     **Important:** if AstrBot and ComfyUI run in separate Docker
     containers, `127.0.0.1` points at AstrBot itself, not ComfyUI —
     use the LAN IP or a reachable hostname instead.
   - `positive_prompt_node_id` — the ID of your `CLIP Text Encode` (or
     equivalent) node.
   - `output_node_id` — the ID of your `Save Image` / `Preview Image` node.
   - `negative_prompt_node_id` / `seed_node_id` — optional, leave blank
     if not applicable. Seed node is auto-detected if left blank.

From now on, switching to a different workflow (e.g. comparing two
checkpoints) is just: export, copy, paste over the old text, Save And
Close — no zip, no re-upload, no touching the server's filesystem.

**Fallback — file-based method:**

If you'd rather manage it as an actual file (e.g. version-controlling
workflow files alongside the plugin), leave **Workflow JSON** blank and
use `workflow_file` instead: place the exported `.json` in this
plugin's `workflow/` folder and set `workflow_file` to match its name.
This requires re-zipping and re-uploading (or copying to the server)
whenever the workflow changes. `workflow_json` takes priority if both
are set.

## Setting up image-to-image (optional)

Image-to-image is **off by default** — leave `img2img_workflow_json`
blank and attached images are ignored (treated as text-to-image only).
To enable it:

**1. Build an image-to-image workflow in ComfyUI.** The key structural
difference from text-to-image: instead of an `EmptyLatentImage` node
feeding the sampler, you need a `LoadImage` node → `VAEEncode` node
feeding the sampler's `latent_image` input. The easiest way to build
this:
- Open your working text-to-image workflow.
- Delete the `EmptyLatentImage` node.
- Add a `LoadImage` node and a `VAEEncode` node (connect the `LoadImage`
  output to `VAEEncode`'s image input, and the same VAE your checkpoint
  uses to `VAEEncode`'s vae input).
- Connect `VAEEncode`'s output to the sampler's `latent_image` input
  (replacing where `EmptyLatentImage` used to connect).
- The sampler's existing `denoise` input controls how much of the
  original image survives — this plugin sets it automatically per
  request, so leave it at any value in the UI.

**2. Export it** the same way as before: File → Export Workflow (API).

**3. Paste it into the AstrBot config**, in the **Image-to-Image
Workflow JSON** field — separate from the text-to-image one.

**4. Fill in the img2img-specific node IDs** (developer mode still
needed to see them):
- `img2img_load_image_node_id` — the `LoadImage` node. **Required.**
- `img2img_output_node_id` — the `Save Image`/`Preview Image` node.
  **Required.**
- `img2img_positive_prompt_node_id` — optional; if set, the request's
  prompt is written here (useful if your img2img workflow still takes
  a text prompt alongside the image). Leave blank if your workflow is
  driven purely by the image.
- `img2img_negative_prompt_node_id` — optional.
- `img2img_seed_node_id` / `img2img_denoise_node_id` — optional,
  auto-detected (looks for `seed`/`noise_seed` and `denoise` inputs on
  any node) if left blank.
- `default_denoise` — how much the result is allowed to differ from
  the source image (0–1). Lower stays close to the original; higher
  allows bigger reinterpretation. 0.4–0.75 is the typical usable range;
  start around 0.6 and adjust based on results.

## Testing

Test in this order — each step isolates a different layer:

1. **`/draw a red bicycle on a beach`** (text-to-image) — if this
   fails, the problem is ComfyUI connectivity or node IDs, not the LLM.
   Check the AstrBot logs for the exact error (they're written to be
   readable, not raw stack traces).
2. **Attach a photo + `/draw make it look like a watercolor painting`**
   (image-to-image, if configured) — isolates img2img-specific issues
   (upload, `LoadImage` node ID, denoise) separately from the
   text-to-image path.
3. **Natural language** ("draw me a red bicycle", or "turn this photo
   into a painting" with an attachment) — if the command forms work
   but this doesn't, see "Known limitation" below.

## Known limitation

The natural-language tool (`generate_image`) is written assuming
AstrBot auto-injects the current message `event` into `@llm_tool`
methods when a parameter is named/typed `AstrMessageEvent` — this is a
common convention across AstrBot plugins, but wasn't independently
confirmed against your specific AstrBot version at the time this
plugin was written. If natural-language requests silently do nothing,
check the logs for a `TypeError` around missing/unexpected arguments.

`/draw` doesn't depend on this at all — it uses the plain, officially
documented command-handler pattern, so it's the reliable path either
way while you sort out tool-calling behavior.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Workflow file not found" / "No workflow configured" | Neither `workflow_json` nor `workflow_file` is set correctly |
| "node id not found in workflow" | Wrong node ID — re-check ComfyUI developer mode |
| Times out waiting for ComfyUI | ComfyUI queue busy, or `server_address` wrong/unreachable |
| "produced no images" | Output node id doesn't point at a Save/Preview Image node |
| Image always looks identical | No seed node found/randomized — set the seed node id explicitly |
| Attached image ignored, generates from text only | Image-to-image not configured — see "Setting up image-to-image" |
| "Image-to-image isn't configured" error | `img2img_workflow_json`/`img2img_workflow_file` blank, or `img2img_load_image_node_id`/`img2img_output_node_id` not set |
| img2img result barely changes from source | `default_denoise` too low — try raising toward 0.7–0.8 |
| img2img result barely resembles source | `default_denoise` too high — try lowering toward 0.3–0.4 |

## License

MIT — do whatever you like with it.
