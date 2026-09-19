# AstrBot Plugin: ComfyUI Bridge

A small, English-only AstrBot plugin that connects AstrBot to a
self-hosted ComfyUI instance for text-to-image generation, using a
workflow you export yourself from ComfyUI. No specific model,
checkpoint, or LoRA is assumed — it just injects a prompt into the
node IDs you configure and returns whatever image ComfyUI produces.

Built as a minimal alternative to the (excellent, but Chinese-language)
`astrbot_plugin_comfyui_pro` — same core idea, far fewer moving parts,
because it doesn't need multi-workflow switching, LoRA catalog
scanning, or sensitive-word filtering.

## What it does

- **`/draw <prompt>`** — direct command. Always works, regardless of
  whether your model provider supports tool calling.
- **Natural language** — registers a `generate_image` tool the LLM can
  call automatically when someone asks for a picture (e.g. "draw me a
  cat"). Requires the model provider to support tool/function calls.
  See the note in "Known limitation" below.

## Installation

1. Copy this folder into AstrBot's plugin directory (or install it as
   a plugin from your GitHub repo once published, via AstrBot's
   Extensions tab).
2. Reload plugins in the AstrBot dashboard.

## Setting up your workflow

1. In ComfyUI, open the workflow you want AstrBot to use.
2. Menu action **Save (API Format)** → export as `.json`.
3. Place that file in this plugin's `workflow/` folder.
4. Enable ComfyUI's **developer mode** — node IDs will show above each
   node's title.
5. In the AstrBot dashboard, open this plugin's settings and fill in:
   - `server_address` — your ComfyUI host and port, e.g. `192.168.1.15:8188`.
     **Important:** if AstrBot and ComfyUI run in separate Docker
     containers, `127.0.0.1` points at AstrBot itself, not ComfyUI —
     use the LAN IP or a reachable hostname instead.
   - `workflow_file` — the filename you placed in `workflow/`.
   - `positive_prompt_node_id` — the ID of your `CLIP Text Encode` (or
     equivalent) node.
   - `output_node_id` — the ID of your `Save Image` / `Preview Image` node.
   - `negative_prompt_node_id` / `seed_node_id` — optional, leave blank
     if not applicable. Seed node is auto-detected if left blank.

## Testing

Test in this order — each step isolates a different layer:

1. **`/draw a red bicycle on a beach`** — if this fails, the problem is
   ComfyUI connectivity or node IDs, not the LLM. Check the AstrBot
   logs for the exact error (they're written to be readable, not raw
   stack traces).
2. **Natural language** ("draw me a red bicycle") — if `/draw` works
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
| "Workflow file not found" | `workflow_file` name doesn't match what's in `workflow/` |
| "node id not found in workflow" | Wrong node ID — re-check ComfyUI developer mode |
| Times out waiting for ComfyUI | ComfyUI queue busy, or `server_address` wrong/unreachable |
| "produced no images" | `output_node_id` doesn't point at a Save/Preview Image node |
| Image always looks identical | No seed node found/randomized — set `seed_node_id` explicitly |

## License

MIT — do whatever you like with it.
