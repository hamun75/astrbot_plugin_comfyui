# Put your workflow here

1. In ComfyUI, open your Flux workflow.
2. Use the menu action **Save (API Format)** to export it as `.json`.
3. Place that file in this folder.
4. Set its filename as `workflow_file` in the plugin's config (default expected name: `workflow_api.json`).
5. Enable ComfyUI's developer mode to see node IDs displayed above each node's title,
   and set `positive_prompt_node_id` / `output_node_id` (and optionally
   `negative_prompt_node_id` / `seed_node_id`) in the plugin config to match.
